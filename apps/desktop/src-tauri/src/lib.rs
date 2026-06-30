use std::net::TcpListener;
use std::sync::Mutex;

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, RunEvent, Runtime, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
use tauri_plugin_updater::UpdaterExt;

/// Fallback port if probing for a free one fails (matches the historical
/// hardcoded default + the web client's last-resort base).
const FALLBACK_PORT: u16 = 7870;

/// Pick a free localhost port for the bundled sidecar, so several agents (and a
/// pre-existing server on 7870) can coexist without a collision. We bind :0,
/// read the OS-assigned port, then drop the listener and hand the port to the
/// sidecar — a tiny TOCTOU window, acceptable for a single local launch.
fn pick_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|addr| addr.port())
        .unwrap_or(FALLBACK_PORT)
}

/// Holds the running sidecar so it can be killed when the app exits.
#[derive(Default)]
struct SidecarProcess(Mutex<Option<CommandChild>>);

/// Split a `:`-delimited PATH string and append each new, non-empty dir to `entries`,
/// preserving order and skipping duplicates.
#[cfg(target_os = "macos")]
fn dedup_push_path(entries: &mut Vec<String>, raw: &str) {
    for dir in raw.split(':') {
        if !dir.is_empty() && !entries.iter().any(|e| e == dir) {
            entries.push(dir.to_string());
        }
    }
}

/// Ask the user's interactive login shell for its `PATH`
/// (`$SHELL -ilc 'printf %s "$PATH"'`). `None` if `$SHELL` is unknown, the shell
/// errors, or it returns nothing — callers fall back to a fixed prefix.
#[cfg(target_os = "macos")]
fn login_shell_path() -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());
    let output = std::process::Command::new(&shell)
        .args(["-ilc", "printf %s \"$PATH\""])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if path.is_empty() {
        None
    } else {
        Some(path)
    }
}

/// The PATH to hand the bundled sidecar on macOS. A GUI app launched from
/// Finder/Dock/`launchd` only inherits `launchd`'s minimal PATH
/// (`/usr/bin:/bin:/usr/sbin:/sbin`), so Homebrew (`/opt/homebrew/bin`), nvm, Volta,
/// and asdf bin dirs — where `npx`, `node`, and ACP coding-agent adapters live — are
/// invisible to the server, and a delegate launch command like `npx` fails with
/// "binary not on PATH" (#1299). Compose: the login-shell PATH (covers nvm/Volta/asdf),
/// then the common Homebrew/local dirs (belt-and-suspenders if shell resolution failed),
/// then whatever the process already inherited (never drop a dir that already worked).
#[cfg(target_os = "macos")]
fn augmented_sidecar_path() -> String {
    let mut entries: Vec<String> = Vec::new();
    if let Some(shell_path) = login_shell_path() {
        dedup_push_path(&mut entries, &shell_path);
    }
    dedup_push_path(&mut entries, "/opt/homebrew/bin:/usr/local/bin");
    if let Ok(existing) = std::env::var("PATH") {
        dedup_push_path(&mut entries, &existing);
    }
    entries.join(":")
}

/// Launch the bundled protoAgent server (console UI tier) as a sidecar.
///
/// The frozen binary is read-only, so its writable state (live config,
/// secrets, setup marker) is pointed at the per-user app-config dir via
/// `PROTOAGENT_CONFIG_DIR`. Failures are logged, not fatal — the window still
/// opens (and shows the API error) rather than the whole app refusing to boot.
fn spawn_sidecar<R: Runtime>(app: &AppHandle<R>, port: u16) {
    let config_dir = match app.path().app_config_dir() {
        Ok(dir) => dir,
        Err(e) => {
            log::error!("sidecar: cannot resolve app config dir: {e}");
            return;
        }
    };
    if let Err(e) = std::fs::create_dir_all(&config_dir) {
        log::error!("sidecar: cannot create config dir {config_dir:?}: {e}");
        return;
    }

    let command = match app.shell().sidecar("protoagent-server") {
        Ok(cmd) => cmd,
        Err(e) => {
            log::error!("sidecar: binary not found (run apps/desktop/sidecar/build_sidecar.py): {e}");
            return;
        }
    };
    let port_arg = port.to_string();
    #[allow(unused_mut)] // `mut` is only used on the macOS PATH branch below.
    let mut command = command
        // The desktop renders the React operator console, so run the server in
        // its 'console' UI tier (API + A2A + console, no Gradio) — ADR 0010.
        // (Was the now-deprecated --headless / PROTOAGENT_HEADLESS alias.)
        .args(["--ui", "console", "--port", &port_arg])
        .env("PROTOAGENT_UI", "console")
        // So the sidecar exits if we die without a clean kill (the frozen
        // onefile's child process otherwise outlives us, holding its port).
        .env("PROTOAGENT_PARENT_PID", std::process::id().to_string())
        .env("PROTOAGENT_CONFIG_DIR", config_dir.to_string_lossy().to_string());

    // A Finder/Dock/launchd launch strips PATH down to launchd's minimal set, hiding
    // Homebrew/nvm/Volta/asdf — so delegate launch commands (`npx`, ACP adapters) fail
    // with "binary not on PATH" (#1299). Hand the sidecar the user's real PATH.
    #[cfg(target_os = "macos")]
    {
        command = command.env("PATH", augmented_sidecar_path());
    }

    let (mut rx, child) = match command.spawn() {
        Ok(pair) => pair,
        Err(e) => {
            log::error!("sidecar: spawn failed: {e}");
            return;
        }
    };

    if let Some(state) = app.try_state::<SidecarProcess>() {
        *state.0.lock().unwrap() = Some(child);
    }

    // Drain stdout/stderr so the OS pipe buffer never fills and stalls the child.
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                    log::info!("[sidecar] {}", String::from_utf8_lossy(&bytes).trim_end());
                }
                CommandEvent::Terminated(payload) => {
                    log::warn!("[sidecar] terminated: {payload:?}");
                    break;
                }
                _ => {}
            }
        }
    });
}

/// Kill the sidecar if it's still running (called on app exit).
fn kill_sidecar<R: Runtime>(app: &AppHandle<R>) {
    if let Some(state) = app.try_state::<SidecarProcess>() {
        if let Some(child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
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

/// Check the GitHub Release updater manifest (latest.json) for a newer build;
/// prompt, download + install, then relaunch. Signatures are verified against
/// the org minisign pubkey baked into tauri.conf.json.
///
/// `interactive` = invoked from the tray item: "up to date" and errors surface
/// as dialogs. The silent launch check only logs. On Linux the updater manages
/// AppImage installs only (a .deb belongs to apt) — that limitation comes back
/// as an error from the plugin and is handled like any other.
fn check_for_updates<R: Runtime>(app: AppHandle<R>, interactive: bool) {
    tauri::async_runtime::spawn(async move {
        let updater = match app.updater() {
            Ok(u) => u,
            Err(e) => {
                log::info!("updater: unavailable for this install: {e}");
                if interactive {
                    app.dialog()
                        .message(format!("Updates aren't managed in-app for this install.\n\n{e}"))
                        .title("protoAgent updates")
                        .show(|_| {});
                }
                return;
            }
        };
        match updater.check().await {
            Ok(Some(update)) => {
                let current = app.package_info().version.to_string();
                let version = update.version.clone();
                log::info!("updater: {version} available (running {current})");
                let app_for_install = app.clone();
                app.dialog()
                    .message(format!(
                        "protoAgent {version} is available (you have {current}).\n\n\
                         Download and install now? The app relaunches when it finishes \
                         and your agent data is untouched."
                    ))
                    .title("Update available")
                    .buttons(MessageDialogButtons::OkCancelCustom(
                        "Install and Relaunch".to_string(),
                        "Later".to_string(),
                    ))
                    .show(move |confirmed| {
                        if !confirmed {
                            return;
                        }
                        tauri::async_runtime::spawn(async move {
                            match update.download_and_install(|_, _| {}, || {}).await {
                                Ok(()) => {
                                    log::info!("updater: installed, relaunching");
                                    app_for_install.restart();
                                }
                                Err(e) => {
                                    log::error!("updater: install failed: {e}");
                                    app_for_install
                                        .dialog()
                                        .message(format!("The update failed to install.\n\n{e}"))
                                        .title("protoAgent updates")
                                        .show(|_| {});
                                }
                            }
                        });
                    });
            }
            Ok(None) => {
                log::info!("updater: up to date");
                if interactive {
                    app.dialog()
                        .message("You're on the latest version.")
                        .title("protoAgent updates")
                        .show(|_| {});
                }
            }
            Err(e) => {
                log::warn!("updater: check failed: {e}");
                if interactive {
                    app.dialog()
                        .message(format!("Couldn't check for updates.\n\n{e}"))
                        .title("protoAgent updates")
                        .show(|_| {});
                }
            }
        }
    });
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show protoAgent", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
    let updates = MenuItem::with_id(app, "updates", "Check for Updates…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&show, &hide, &separator, &updates, &quit])?;

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
            "hide" => hide_main_window(app),
            "updates" => check_for_updates(app.clone(), true),
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
        if on_event.send(String::from_utf8_lossy(&bytes).into_owned()).is_err() {
            break;
        }
    }
    Ok(())
}

#[derive(serde::Serialize, Clone)]
struct UpdateInfo {
    version: String,
    current: String,
    /// The release notes / changelog (latest.json `notes`) — shown in the in-app pill.
    notes: String,
}

#[derive(serde::Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct DownloadProgress {
    chunk_length: u64,
    content_length: Option<u64>,
}

/// Check the updater manifest for a newer build, returning its version + notes for the
/// in-app UpdateNotice (the web pill renders the changelog) — the typed counterpart to
/// the tray's native-dialog `check_for_updates`. None when up to date; Err on failure.
#[tauri::command]
async fn updater_check<R: Runtime>(app: AppHandle<R>) -> Result<Option<UpdateInfo>, String> {
    let updater = app.updater().map_err(|e| e.to_string())?;
    let current = app.package_info().version.to_string();
    match updater.check().await.map_err(|e| e.to_string())? {
        Some(u) => Ok(Some(UpdateInfo {
            version: u.version.clone(),
            current,
            notes: u.body.clone().unwrap_or_default(),
        })),
        None => Ok(None),
    }
}

/// Download + install the available update (signature-verified by the plugin against the
/// embedded pubkey), streaming progress to the webview over an IPC Channel, then relaunch.
#[tauri::command]
async fn updater_install<R: Runtime>(
    app: AppHandle<R>,
    on_progress: tauri::ipc::Channel<DownloadProgress>,
) -> Result<(), String> {
    let updater = app.updater().map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "no update available".to_string())?;
    update
        .download_and_install(
            move |chunk, total| {
                let _ = on_progress.send(DownloadProgress {
                    chunk_length: chunk as u64,
                    content_length: total,
                });
            },
            || {},
        )
        .await
        .map_err(|e| e.to_string())?;
    app.restart();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            chat_stream,
            updater_check,
            updater_install,
            hide_launcher,
            focus_main
        ])
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        // In-app updates: checks the latest.json manifest on GitHub Releases,
        // verifies the minisign signature, installs, relaunches.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Notifications — bridges the web Notification API in the webview so the
        // console can alert (e.g. a HITL form awaiting input) even when the
        // menu-bar window is hidden.
        .plugin(tauri_plugin_notification::init())
        .plugin(
            // Two global, system-wide hotkeys (fire even when the app is unfocused or
            // hidden in the menu bar):
            //   ⌘⇧P    — toggle the full console window.
            //   ⌥Space — summon the Raycast-style quick launcher (just the palette).
            // ⌥Space is Raycast's familiar alt-default; to rebind, change `launcher_hotkey`
            // here AND the comparison in the handler (e.g. SUPER|SHIFT + Space if you'd
            // rather keep Option+Space free for the non-breaking space it normally types).
            tauri_plugin_global_shortcut::Builder::new()
                .with_shortcuts([
                    Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::KeyP),
                    Shortcut::new(Some(Modifiers::ALT), Code::Space),
                ])
                .expect("valid global shortcuts")
                .with_handler(|app, shortcut, event| {
                    if event.state != ShortcutState::Pressed {
                        return;
                    }
                    let launcher_hotkey = Shortcut::new(Some(Modifiers::ALT), Code::Space);
                    if shortcut == &launcher_hotkey {
                        toggle_launcher(app);
                    } else {
                        toggle_main_window(app);
                    }
                })
                .build(),
        )
        .setup(|app| {
            // Init logging in RELEASE too (was debug-only): a release build that
            // wrote no logs is exactly why the v0.35.0 sidecar failure was opaque
            // — "no logs?". tauri-plugin-log's default targets include the OS log
            // dir (~/Library/Logs/studio.protolabs.protoagent/), so the captured
            // `[sidecar]` stdout/stderr (incl. a boot crash) lands on disk.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .build(),
            )?;
            app.manage(SidecarProcess::default());

            // Pin the sidecar to the fixed port the web client falls back to in
            // the Tauri context (apps/web/src/lib/api.ts → http://127.0.0.1:7870).
            // The dynamic-free-port + window-injection handoff proved unreliable
            // across Tauri v2 webview contexts: the page couldn't see the injected
            // `__PROTOAGENT_API_BASE__`, fell back to a (then-dead) port, and every
            // request failed ("Load failed"). A fixed port makes the fallback the
            // live server — no handoff needed. (Trades multi-agent port
            // coexistence for a desktop console that actually connects.)
            let port: u16 = 7870;
            spawn_sidecar(app.handle(), port);
            let init = format!(
                "window.__PROTOAGENT_API_BASE__ = \"http://127.0.0.1:{port}\";"
            );
            // A `target="_blank"` / `window.open` from a (sandboxed) plugin iframe asks
            // the host to spawn a child window. We don't host child windows, so without
            // a handler WKWebView silently drops the request and the click does nothing
            // — e.g. the GitHub plugin's PR/issue links were dead in the desktop app.
            // Open external http(s) links in the system browser (shell:allow-open) and
            // deny the in-app window. (Browsers handle this implicitly via allow-popups;
            // the desktop shell has to do it explicitly.)
            let link_opener = app.handle().clone();
            let mut win = WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("protoAgent")
                .inner_size(1280.0, 820.0)
                .min_inner_size(980.0, 640.0)
                .resizable(true)
                .center()
                .initialization_script(&init)
                .on_new_window(move |url, _features| {
                    let target = url.as_str();
                    if target.starts_with("http://") || target.starts_with("https://") {
                        if let Err(e) = link_opener.shell().open(target, None) {
                            log::error!("desktop: failed to open external link {target}: {e}");
                        }
                    }
                    tauri::webview::NewWindowResponse::Deny
                });
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
            // off `__PROTOAGENT_LAUNCHER__`). Created HIDDEN and reused — the ⌥Space
            // global shortcut reveals/centers it; it hides on blur (see on_window_event)
            // or Escape. Same fixed-port API base as the main window.
            let launcher_init = format!(
                "window.__PROTOAGENT_API_BASE__ = \"http://127.0.0.1:{port}\"; \
                 window.__PROTOAGENT_LAUNCHER__ = true;"
            );
            WebviewWindowBuilder::new(app, "launcher", WebviewUrl::default())
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

            // Ambient update checks are now owned by the web UpdateNotice (an in-app
            // pill + changelog, polling `updater_check` ~10s after boot then every 6h) —
            // so the old silent launch check is gone, to avoid double-prompting (a native
            // dialog AND the pill). The tray "Check for updates" still does an interactive
            // native check (see the tray handler) as a manual fallback.
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
            // Tear the bundled server down with the app rather than orphaning it.
            if let RunEvent::Exit = event {
                kill_sidecar(app_handle);
            }
        });
}
