use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, RunEvent, Runtime, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_global_shortcut::ShortcutState;

mod downloads;
mod hotkeys;
mod navigation;
mod sidecar;
mod updater;

use hotkeys::{
    default_hotkeys, hotkey_id_for, load_hotkey_overrides, sync_hotkeys, HotkeyStatus, Hotkeys,
    HOTKEY_LAUNCHER,
};
use navigation::{open_chat_window, serve_navigation, serve_new_window};
use sidecar::{
    choose_port, spawn_sidecar, stop_sidecar, SidecarProcess, DEFAULT_PORT, PORT_RETRY_WINDOW,
};
use updater::{
    request_update_from_tray, spawn_launch_update_check, LaunchUpdateState, UpdateCheckCoordinator,
    UpdateRequestState,
};

/// Desktop log retention (#3504). tauri-plugin-log's defaults are a 40,000-byte file under
/// `RotationStrategy::KeepOne`, which DELETES the file at each rotation: with the console
/// open that kept under a minute of history, so the hub boot, member spawns, `updater:`
/// lines and a crash traceback were gone before anyone looked. 5 MB a file, and four dated
/// files kept beside the active one (`KeepSome(n)` keeps n plus the active file), caps the
/// log directory at about 25 MB.
const LOG_MAX_FILE_BYTES: u128 = 5 * 1024 * 1024;
const LOG_KEEP_ROTATED: usize = 4;
// The plugin computes `n - 1` on every rotation, so `KeepSome(0)` would underflow.
const _: () = assert!(LOG_KEEP_ROTATED >= 1);

/// Holds the sidecar port + a throttle clock for the `system.wake` lifecycle event
/// (ADR 0074). The window's `Focused(true)` fires on every alt-tab, so `last_wake`
/// debounces it down to "came back after being away".
struct WakeSignal {
    port: u16,
    last_wake: Mutex<Instant>,
}

/// Debounced `system.wake` (ADR 0074): the desktop window regained focus (a proxy for
/// the shell coming back to the foreground). Emitted at most once per `WAKE_THROTTLE` so
/// a quick tab-flick doesn't spam it. Best-effort, fire-and-forget: POST `system.wake` to
/// the sidecar's `/api/events/publish`, which broadcasts it on the event bus (ADR 0039) so
/// lifecycle hooks / config reactions can respond. A dead/booting sidecar just logs.
///
/// The POST carries the operator bearer. When this was first drafted (PR #1797) it didn't
/// need to — the operator API trusted loopback. ADR 0089 closed that hole, and the
/// middleware is explicit that "trust = the matched secret, never the path/Origin/
/// loopback" (a2a_impl/auth.py, R5), so a tokenless publish is now a 401 on any instance
/// with a token configured — i.e. the wake event would silently never fire, which is the
/// worst failure shape for a fire-and-forget signal. Same token the shell already hands
/// the webview; the response status is logged so a future auth change can't fail silently
/// the way this one would have.
fn maybe_signal_wake<R: Runtime>(app: &AppHandle<R>) {
    const WAKE_THROTTLE: Duration = Duration::from_secs(60);
    let Some(state) = app.try_state::<WakeSignal>() else {
        return;
    };
    // Take the throttle decision under the lock, then drop it before the await.
    {
        let mut last = state.last_wake.lock().unwrap();
        if last.elapsed() < WAKE_THROTTLE {
            return;
        }
        *last = Instant::now();
    }
    let port = state.port;
    let token = resolve_auth_token(app);
    tauri::async_runtime::spawn(async move {
        let url = format!("http://127.0.0.1:{port}/api/events/publish");
        let body = serde_json::json!({
            "topic": "system.wake",
            "data": { "previous_state": "background", "source": "desktop" },
        });
        let mut req = reqwest::Client::new().post(&url).json(&body);
        if let Some(t) = token.filter(|t| !t.is_empty()) {
            req = req.header("Authorization", format!("Bearer {t}"));
        }
        match req.send().await {
            Err(e) => log::debug!("desktop: system.wake POST failed (sidecar down/booting?): {e}"),
            Ok(resp) if !resp.status().is_success() => {
                log::warn!("desktop: system.wake rejected — HTTP {}", resp.status().as_u16());
            }
            Ok(_) => {}
        }
    });
}

fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn hide_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
}

fn toggle_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        match window.is_visible() {
            Ok(true) => {
                let _ = window.hide();
            }
            _ => show_main_window(app),
        }
    }
}

// ── Raycast-style quick launcher ────────────────────────────────────────────
// A second, frameless, always-on-top window that hosts ONLY the command palette
// (the web boots into launcher mode off the injected `__PROTOAGENT_LAUNCHER__`).
// Summoned by a global hotkey from anywhere, dismissed on blur / Escape; the
// palette's navigation commands hand off to the main window (a `palette:navigate`
// event the main webview listens for) and then hide the launcher.

/// Re-center, reveal + focus the launcher, and tell its webview to reset the palette
/// to root + refocus the search field (it stays mounted between summons).
fn show_launcher<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("launcher") {
        let _ = window.center();
        let _ = window.show();
        let _ = window.set_focus();
        // Global emit — the launcher webview listens; the main one ignores it.
        let _ = app.emit("launcher:shown", ());
    }
}

fn hide_launcher_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("launcher") {
        let _ = window.hide();
    }
}

fn toggle_launcher<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("launcher") {
        match window.is_visible() {
            Ok(true) => {
                let _ = window.hide();
            }
            _ => show_launcher(app),
        }
    }
}

/// Hide the launcher — invoked by its webview on Escape / after a navigation handoff.
#[tauri::command]
fn hide_launcher<R: Runtime>(app: AppHandle<R>) {
    hide_launcher_window(&app);
}

/// Bring the main console window to the front — invoked by the launcher webview when a
/// navigation command hands a surface off to the main window.
#[tauri::command]
fn focus_main<R: Runtime>(app: AppHandle<R>) {
    show_main_window(&app);
}


/// Extract `auth.token` from a protoAgent `secrets.yaml`.
///
/// A deliberate hand-scan rather than a YAML dependency: the file is written by our own
/// Python (ruamel) with a fixed shape, and the desktop binary shouldn't grow a parser for
/// one two-line lookup. Kept narrow on purpose — a top-level `auth:` block, then an indented
/// `token:` before the block ends. Anything else returns None and the console falls back to
/// its normal token prompt.
fn parse_auth_token(yaml: &str) -> Option<String> {
    let mut in_auth = false;
    for line in yaml.lines() {
        let trimmed = line.trim_end();
        if trimmed.trim_start().starts_with('#') || trimmed.trim().is_empty() {
            continue;
        }
        let indented = line.starts_with(' ') || line.starts_with('\t');
        if !indented {
            // A new top-level key ends the auth block (and starts it, if it IS auth).
            in_auth = trimmed.trim_end_matches(':').trim() == "auth" && trimmed.ends_with(':');
            continue;
        }
        if !in_auth {
            continue;
        }
        let (key, value) = match trimmed.split_once(':') {
            Some(pair) => pair,
            None => continue,
        };
        if key.trim() != "token" {
            continue;
        }
        // Strip an inline comment, then surrounding quotes.
        let mut v = value.trim();
        if let Some(hash) = v.find(" #") {
            v = v[..hash].trim();
        }
        let v = v.trim_matches('"').trim_matches('\'').trim();
        if v.is_empty() {
            return None;
        }
        return Some(v.to_string());
    }
    None
}

/// The operator token this app's own server is configured with, if any.
///
/// The desktop app SPAWNS the sidecar and sets its `PROTOAGENT_HOME`, so it already has
/// filesystem access to that server's config — it should never make the operator go hunting
/// for a secret to unlock an app running on their own machine. The console calls this when it
/// is running in the desktop shell and hits a 401 (issue #2055).
///
/// Delivered over `invoke` rather than `initialization_script`: injection proved unreliable
/// across Tauri v2 webview contexts (see the API-base handoff above), and a token must never
/// ride the webview URL, which is visible to the page and anything it embeds.
///
/// Returns None when no token is configured — the common, correct case for a loopback-only
/// install — and the console keeps its existing behaviour.
#[tauri::command]
fn auth_token<R: Runtime>(app: AppHandle<R>) -> Option<String> {
    let found = resolve_auth_token(&app);
    // Logged at INFO without the value: this is the one place that answers "did the shell
    // hand the webview a token, or is the operator being asked for one the app already had?"
    log::info!("desktop: auth_token requested — configured: {}", found.is_some());
    found
}

/// The sidecar's operator token, resolved the way the server itself resolves it. Quiet:
/// the webview-facing `auth_token` command logs, but the shell's own server-to-server
/// callers (see `maybe_signal_wake`) would only add noise on a timer.
fn resolve_auth_token<R: Runtime>(app: &AppHandle<R>) -> Option<String> {
    // Env wins, mirroring the server's own precedence (a2a_impl/auth.py `configure`).
    if let Ok(t) = std::env::var("A2A_AUTH_TOKEN") {
        let t = t.trim().to_string();
        if !t.is_empty() {
            return Some(t);
        }
    }
    let dir = app.path().app_config_dir().ok()?;
    let path = dir.join("config").join("secrets.yaml");
    std::fs::read_to_string(&path).ok().and_then(|b| parse_auth_token(&b))
}

/// The real OS folder/file chooser for the console's path settings (#2265).
///
/// #2264 gave every path field a **Browse…** that walks the SERVER's filesystem over
/// `GET /api/fs/browse` — the only mechanism that works everywhere, because the console
/// routinely configures a machine it isn't running on (tailnet, fleet members, Docker),
/// and the browser-native pickers can't name a server path at all. That stays the
/// fallback and the default. This is the progressive enhancement for the one case where
/// the two machines are provably the same: the desktop app's HOST window, configuring
/// the instance the app itself runs. There the operator gets back everything the real
/// chooser gives for free — typing with autocomplete, `~` and `/` jumps, Finder/Explorer
/// favourites, network volumes.
///
/// The webview decides when to call this (see `pickPathNative` in lib/desktop.ts); the
/// shell just answers. Returns None when the operator cancels — the caller leaves the
/// field untouched rather than falling through to the in-app browser, since a cancel is
/// a decision, not a failure.
#[tauri::command]
async fn pick_path<R: Runtime>(app: AppHandle<R>, start: Option<String>, files: bool) -> Option<String> {
    let mut builder = app.dialog().file();
    // Seed the chooser at the field's current value when it names a real directory. A
    // stale or mistyped path is exactly when someone reaches for Browse, so a bad seed
    // must not dead-end the dialog — drop it and let the OS pick its own default.
    if let Some(dir) = start
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(std::path::PathBuf::from)
        .filter(|p| p.is_dir())
    {
        builder = builder.set_directory(dir);
    }

    // The dialog is callback-based and fires on the UI thread. A capacity-1 channel plus
    // `try_send` bridges it to this async command without ever blocking that thread —
    // the blocking_* variants panic when called from the main thread, and there is
    // exactly one send, so try_send cannot drop the result.
    let (tx, mut rx) = tauri::async_runtime::channel(1);
    let reply = move |picked: Option<tauri_plugin_dialog::FilePath>| {
        let _ = tx.try_send(picked);
    };
    if files {
        builder.pick_file(reply);
    } else {
        builder.pick_folder(reply);
    }

    let picked = rx.recv().await.flatten()?;
    // A native pick is always a real local path; `into_path` only fails for the
    // Android/iOS content-URI form, which this desktop-only command never sees.
    picked.into_path().ok().map(|p| p.to_string_lossy().into_owned())
}


fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show protoAgent", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
    // #1706: a discoverable way to get a second window. Two agents side by side, or one
    // window per task, without tab-switching — and it makes the capability visible
    // instead of hiding it behind a context-menu gesture nobody finds.
    let new_win = MenuItem::with_id(app, "new_window", "New Window", true, None::<&str>)?;
    let updates = MenuItem::with_id(app, "updates", "Check for Updates…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&show, &new_win, &hide, &separator, &updates, &quit])?;

    // The protoLabs robot mark, at the menu-bar size + template treatment Orbis
    // used for fleet agents (icons/tray-robot.png, 44×44; system-tinted). Each
    // protoLabs.studio app owns its own menu-bar item.
    let icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray-robot.png"))?;
    let builder = TrayIconBuilder::new()
        .icon(icon)
        .menu(&menu)
        .tooltip("protoAgent")
        .icon_as_template(true)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main_window(app),
            "new_window" => {
                if let Err(e) = open_chat_window(app, None) {
                    log::error!("desktop: New Window failed: {e}");
                }
            }
            "hide" => hide_main_window(app),
            "updates" => request_update_from_tray(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            }
            | TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } => show_main_window(&tray.app_handle()),
            _ => {}
        });

    builder.build(app)?;
    Ok(())
}

/// Stream a chat turn for the desktop shell. WKWebView won't deliver a streaming
/// SSE `fetch` body chunk-by-chunk, so the webview hands us the A2A request body and
/// we run the `/a2a` `SendStreamingMessage` POST here (reqwest streams fine), relaying
/// each raw response chunk to the frontend over an IPC `Channel`. The webview parses
/// the SSE + dispatches frames exactly like the browser path (`drainSseBuffer`), so
/// desktop gets real token-by-token + tool-call streaming. On any error the caller
/// falls back to the non-streaming `/api/chat` path — so this never regresses below
/// today's behavior.
#[tauri::command]
async fn chat_stream(
    url: String,
    body: serde_json::Value,
    auth: Option<String>,
    on_event: tauri::ipc::Channel<String>,
) -> Result<(), String> {
    use futures_util::StreamExt;

    let client = reqwest::Client::new();
    let mut req = client
        .post(&url)
        .header("Content-Type", "application/json")
        .header("A2A-Version", "1.0")
        .json(&body);
    if let Some(token) = auth.filter(|t| !t.is_empty()) {
        req = req.header("Authorization", token);
    }
    let resp = req.send().await.map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status().as_u16()));
    }
    let mut stream = resp.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let bytes = chunk.map_err(|e| e.to_string())?;
        // Relay raw bytes; the webview accumulates + parses SSE (handles frames split
        // across chunks). Stop if the frontend dropped the channel (window closed /
        // turn cancelled via the server-side CancelTask, which ends the stream).
        if on_event
            .send(String::from_utf8_lossy(&bytes).into_owned())
            .is_err()
        {
            break;
        }
    }
    Ok(())
}


#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            chat_stream,
            updater::updater_check,
            updater::updater_install,
            updater::updater_launch_result,
            updater::updater_consume_request,
            updater::updater_ack_request,
            hide_launcher,
            focus_main,
            hotkeys::hotkeys_status,
            hotkeys::hotkeys_set,
            auth_token,
            pick_path,
            navigation::new_window,
            downloads::open_download,
            downloads::reveal_download
        ])
        .manage(downloads::Downloads::default())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        // In-app updates: checks the latest.json manifest on GitHub Releases,
        // verifies the minisign signature, installs, relaunches.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Notifications — bridges the web Notification API in the webview so the
        // console can alert (e.g. a HITL form awaiting input) even when the
        // menu-bar window is hidden.
        .plugin(tauri_plugin_notification::init())
        .plugin(
            // The global-shortcut HANDLER only — registration happens fallibly in
            // setup(). Registering here (with_shortcuts) turned a hotkey another app
            // already owns (Discord, PowerToys, AutoHotkey…) into a
            // PluginInitialization error that panicked the whole launch at the
            // top-level .expect — before the window, sidecar, or even logging
            // existed, so the app just "didn't start" (#1670).
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state != ShortcutState::Pressed {
                        return;
                    }
                    // Chords are rebindable (#1675) — resolve the fired shortcut to
                    // its hotkey id via the managed registry, not a hardcoded compare.
                    match hotkey_id_for(app, shortcut).as_deref() {
                        Some(HOTKEY_LAUNCHER) => toggle_launcher(app),
                        _ => toggle_main_window(app),
                    }
                })
                .build(),
        )
        .setup(|app| {
            // Init logging in RELEASE too (was debug-only): a release build that
            // wrote no logs is exactly why the v0.35.0 sidecar failure was opaque
            // — "no logs?". tauri-plugin-log's default targets include the OS log
            // dir (~/Library/Logs/studio.protolabs.protoagent/), so the captured
            // `[sidecar]` stdout/stderr (incl. a boot crash) lands on disk. Sized and
            // rotated so that history is still there when someone looks (#3504); the
            // per-request sidecar lines are kept out by `sidecar_line_level`.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .max_file_size(LOG_MAX_FILE_BYTES)
                    .rotation_strategy(tauri_plugin_log::RotationStrategy::KeepSome(
                        LOG_KEEP_ROTATED,
                    ))
                    .build(),
            )?;
            app.manage(SidecarProcess::default());
            app.manage(UpdateCheckCoordinator::default());
            app.manage(UpdateRequestState::default());

            // Two global, system-wide hotkeys (fire even when the app is unfocused or
            // hidden in the menu bar): the console toggle and the quick launcher —
            // defaults in default_hotkeys(), operator overrides from hotkeys.json
            // (Settings ▸ Keyboard, #1675). FALLIBLE by design (#1670): a hotkey
            // another app already owns records its state for the settings UI and the
            // app stays fully usable via the window/tray — it must never abort the
            // launch. Registered here (after logging init) so warnings land on disk;
            // re-attempted on window focus (sync_hotkeys in the run handler).
            {
                let overrides = load_hotkey_overrides(app.handle());
                let entries: Vec<HotkeyStatus> = default_hotkeys()
                    .into_iter()
                    .map(|(id, default_chord)| HotkeyStatus {
                        id: id.to_string(),
                        chord: overrides.get(id).cloned().unwrap_or(default_chord),
                        registered: false,
                        error: None,
                    })
                    .collect();
                app.manage(Hotkeys(Mutex::new(entries)));
                sync_hotkeys(app.handle());
            }

            // The sidecar prefers the fixed port the web client falls back to in the
            // Tauri context (apps/web/src/lib/api.ts → http://127.0.0.1:7870) but
            // yields to a free port when 7870 is held (an orphaned sidecar, a headless
            // dev server — previously the new sidecar died at bind and the console
            // showed a dead/foreign server with zero diagnostics, #1668). The chosen
            // port travels on the webview URL as `?__apiPort=` — the handoff the web
            // client checks FIRST, chosen over the injected global precisely because
            // the URL is always visible to the page (the `__PROTOAGENT_API_BASE__`
            // injection proved unreliable across Tauri v2 webview contexts; it stays
            // as a secondary channel).
            let port: u16 = choose_port();
            if port != DEFAULT_PORT {
                log::warn!(
                    "desktop: port {DEFAULT_PORT} still in use after {} s — sidecar on {port} (handoff via ?__apiPort)",
                    PORT_RETRY_WINDOW.as_secs()
                );
            }
            spawn_sidecar(app.handle(), port);
            // Update check in PARALLEL with engine startup (#2203) — result stored for
            // the web UpdateNotice to pull the moment it mounts; see the fn docs.
            app.manage(LaunchUpdateState::default());
            spawn_launch_update_check(app.handle().clone());
            // Seed the wake-signal state (ADR 0074). last_wake starts "now" so the
            // window's own boot Focused(true) is inside the throttle and doesn't fire a
            // redundant system.wake right after app.loaded.
            app.manage(WakeSignal {
                port,
                last_wake: Mutex::new(Instant::now()),
            });
            let app_url = || WebviewUrl::App(format!("index.html?__apiPort={port}").into());
            let init = format!(
                "window.__PROTOAGENT_API_BASE__ = \"http://127.0.0.1:{port}\"; \
                 window.__PROTOAGENT_PRIMARY__ = true;"
            );
            // A `target="_blank"` / `window.open` from a (sandboxed) plugin iframe asks
            // the host to spawn a child window. We don't host child windows, so without
            // a handler WKWebView silently drops the request and the click does nothing
            // — e.g. the GitHub plugin's PR/issue links were dead in the desktop app.
            // Open external http(s) links in the system browser (the opener plugin) and
            // deny the in-app window. (Browsers handle this implicitly via allow-popups;
            // the desktop shell has to do it explicitly.)
            let link_opener = app.handle().clone();
            let sidecar_port = port;
            #[allow(unused_mut)] // `mut` is only used on the macOS title-bar branch below.
            let mut win = WebviewWindowBuilder::new(app, "main", app_url())
                .title("protoAgent")
                .inner_size(1280.0, 820.0)
                .min_inner_size(980.0, 640.0)
                .resizable(true)
                .center()
                // Same reason as open_chat_window above (#3197) — the console's HTML5 drop
                // surfaces are dead while Tauri's native handler owns the drop.
                .disable_drag_drop_handler()
                .initialization_script(&init)
                .on_new_window(move |url, _features| {
                    serve_new_window(&link_opener, sidecar_port, url.as_str())
                })
                .on_navigation({
                    let app = app.handle().clone();
                    move |url| serve_navigation(&app, url.as_str())
                })
                // Saves land in Downloads and the console hears when they finish.
                .on_download(downloads::handler);
            // Invisible title bar (macOS): no opaque chrome — content fills the
            // frame and the native traffic lights float top-left. The web shell
            // restores window-dragging + insets its topbar for the lights
            // (apps/web `.is-tauri`). ADR-adjacent polish for the desktop build.
            #[cfg(target_os = "macos")]
            {
                win = win
                    .title_bar_style(tauri::TitleBarStyle::Overlay)
                    .hidden_title(true);
            }
            win.build()?;

            // The Raycast-style quick launcher: a second, frameless, always-on-top
            // window hosting ONLY the command palette (the web boots into launcher mode
            // off `__PROTOAGENT_LAUNCHER__`). Created HIDDEN and reused — the
            // launcher_shortcut() global hotkey reveals/centers it; it hides on blur
            // (see on_window_event) or Escape. Same API-base handoff as the main window.
            let launcher_init = format!(
                "window.__PROTOAGENT_API_BASE__ = \"http://127.0.0.1:{port}\"; \
                 window.__PROTOAGENT_LAUNCHER__ = true;"
            );
            WebviewWindowBuilder::new(app, "launcher", app_url())
                .title("protoAgent — Quick Command")
                .inner_size(720.0, 480.0)
                .decorations(false)
                // Transparent + shadowless so the web shell can float a rounded, frosted
                // palette card with see-through margins (the window itself paints nothing;
                // the panel's CSS owns the radius + shadow). macOS needs the paired
                // `macOSPrivateApi` config flag + the `macos-private-api` cargo feature.
                .transparent(true)
                .shadow(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .resizable(false)
                .center()
                .visible(false)
                // Kept consistent with the other two windows (#3197 / #3316): the launcher
                // hosts the same web bundle, so it must not behave differently if a surface
                // lands there — a palette result that opens a link included.
                .disable_drag_drop_handler()
                .on_new_window({
                    let app = app.handle().clone();
                    move |url, _features| serve_new_window(&app, port, url.as_str())
                })
                .on_navigation({
                    let app = app.handle().clone();
                    move |url| serve_navigation(&app, url.as_str())
                })
                .initialization_script(&launcher_init)
                .build()?;

            // Menu-bar-only: build the tray, and only drop the dock icon
            // (Accessory) if it succeeds — so a tray failure leaves us reachable
            // in the dock rather than with no way to surface the window. Closing
            // the window then hides the UI while the app + sidecar keep running
            // in the menu bar; the tray's Quit is the real exit.
            match build_tray(app) {
                Ok(()) => {
                    #[cfg(target_os = "macos")]
                    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
                }
                Err(e) => log::error!("tray setup failed; staying in the dock: {e}"),
            }

            // Update-prompt ownership: the web UpdateNotice owns ALL ambient prompting
            // (the pill + changelog modal). It seeds from the launch check above
            // (`updater_launch_result`, #2203 — prompt lands before engine startup
            // finishes) and keeps its own 10s-settle + 6h `updater_check` cycle. The
            // tray targets a durable request at the primary webview, so manual checks
            // use that same release-notes/progress UX even during sidecar boot. The
            // shell never dialogs an available update on its own.
            Ok(())
        })
        .on_window_event(|window, event| match event {
            // Closing the main window hides the UI (the app + sidecar live on in the menu
            // bar); the tray's Quit is the real exit.
            WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            // Raycast behavior: the launcher dismisses the moment it loses focus (click
            // away, or a navigation command focusing the main window).
            WindowEvent::Focused(false) if window.label() == "launcher" => {
                let _ = window.hide();
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Re-acquire any global hotkey another app owned earlier but has since
            // released (#1675) — focus is a cheap, user-driven retry moment (no
            // polling); sync_hotkeys is a no-op when everything is registered.
            if let RunEvent::WindowEvent { event: WindowEvent::Focused(true), .. } = &event {
                sync_hotkeys(app_handle);
                // System woke to the foreground (ADR 0074) — debounced system.wake.
                maybe_signal_wake(app_handle);
            }
            // Tear the bundled server down with the app rather than orphaning it, and
            // wait (bounded) for its port so a quick relaunch lands on 7870 (#3503).
            if let RunEvent::Exit = event {
                stop_sidecar(app_handle);
            }
        });
}


#[cfg(test)]
mod auth_token_tests {
    use super::parse_auth_token;

    #[test]
    fn reads_a_plain_token() {
        assert_eq!(
            parse_auth_token("auth:\n  token: abc123\n").as_deref(),
            Some("abc123")
        );
    }

    #[test]
    fn tolerates_quotes_comments_and_other_blocks() {
        let y = "model:\n  api_base: https://x\n# a comment\nauth:\n  token: \"q-tok\"  # inline\n";
        assert_eq!(parse_auth_token(y).as_deref(), Some("q-tok"));
    }

    #[test]
    fn ignores_a_token_outside_the_auth_block() {
        // A `token:` under some OTHER key must not be mistaken for the operator bearer.
        let y = "gateway:\n  token: not-the-one\n";
        assert_eq!(parse_auth_token(y), None);
    }

    #[test]
    fn returns_none_when_absent_or_empty() {
        assert_eq!(parse_auth_token("model:\n  api_base: x\n"), None);
        assert_eq!(parse_auth_token("auth:\n  token:\n"), None);
        assert_eq!(parse_auth_token(""), None);
    }

}
