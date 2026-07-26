use std::net::TcpListener;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, RunEvent, Runtime, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_global_shortcut::{Shortcut, ShortcutState};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
use tauri_plugin_updater::UpdaterExt;

/// The web client's zero-handoff fallback port (apps/web/src/lib/api.ts) —
/// preferred so the no-handoff path still lands on the live server.
const DEFAULT_PORT: u16 = 7870;

/// The sidecar's port: the fixed default when it's free, else an OS-assigned free
/// port. Launching straight at an occupied 7870 — an orphaned sidecar, a headless
/// dev server, any unrelated app — meant the new sidecar died at bind and the
/// webview loaded a dead/foreign server with no error (#1668). The chosen port
/// reaches the page via `?__apiPort=` on the webview URL (primary — the URL is
/// always visible to the page, unlike the injected global) plus the
/// `__PROTOAGENT_API_BASE__` init script. Bind-probe-then-release has a tiny
/// TOCTOU window — acceptable for a single local launch.
fn choose_port() -> u16 {
    if TcpListener::bind(("127.0.0.1", DEFAULT_PORT)).is_ok() {
        return DEFAULT_PORT;
    }
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|addr| addr.port())
        .unwrap_or(DEFAULT_PORT)
}

/// The shell's OS-global hotkeys (#1675): stable id → default chord, in the
/// global-hotkey string grammar ("super+shift+p"). The quick launcher is ⌥Space on
/// macOS (the Raycast-familiar default) and Ctrl+Alt+Space elsewhere — plain
/// Alt+Space is the Windows window system-menu accelerator (and PowerToys Run's
/// default), a guaranteed conflict (#1670). Operator overrides persist in
/// `<app-config>/hotkeys.json`, edited from Settings ▸ Keyboard.
const HOTKEY_CONSOLE: &str = "console_toggle";
const HOTKEY_LAUNCHER: &str = "quick_launcher";

fn default_hotkeys() -> Vec<(&'static str, String)> {
    let launcher = if cfg!(target_os = "macos") {
        "alt+space"
    } else {
        "ctrl+alt+space"
    };
    vec![
        (HOTKEY_CONSOLE, "super+shift+p".to_string()),
        (HOTKEY_LAUNCHER, launcher.to_string()),
    ]
}

/// One OS-global hotkey's live status — what Settings ▸ Keyboard renders: the
/// chord, whether it's actually registered, and the denial when it isn't
/// (typically "already registered": another app owns the chord).
#[derive(Clone, serde::Serialize)]
struct HotkeyStatus {
    id: String,
    chord: String,
    registered: bool,
    error: Option<String>,
}

/// Managed registry of the shell's global hotkeys (#1675).
#[derive(Default)]
struct Hotkeys(Mutex<Vec<HotkeyStatus>>);

fn hotkeys_file<R: Runtime>(app: &AppHandle<R>) -> Option<std::path::PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|d| d.join("hotkeys.json"))
}

/// Operator chord overrides (`{id: chord}`) — best-effort read; absent/garbled
/// files just mean defaults.
fn load_hotkey_overrides<R: Runtime>(
    app: &AppHandle<R>,
) -> std::collections::HashMap<String, String> {
    hotkeys_file(app)
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_hotkey_overrides<R: Runtime>(app: &AppHandle<R>, entries: &[HotkeyStatus]) {
    let Some(path) = hotkeys_file(app) else {
        return;
    };
    let map: std::collections::HashMap<&str, &str> = entries
        .iter()
        .map(|e| (e.id.as_str(), e.chord.as_str()))
        .collect();
    if let Ok(json) = serde_json::to_string_pretty(&map) {
        if let Err(e) = std::fs::write(&path, json) {
            log::warn!("desktop: could not persist hotkeys to {path:?}: {e}");
        }
    }
}

/// (Re)register every hotkey that isn't currently live. FALLIBLE per hotkey
/// (#1670): a chord another app owns records `registered:false` + the error in
/// the managed state (Settings ▸ Keyboard shows it) and the app stays fully
/// usable via window/tray. Called at setup and again on window focus — a cheap,
/// user-driven retry moment — so a chord freed by the other app re-acquires
/// without a restart (#1675).
fn sync_hotkeys<R: Runtime>(app: &AppHandle<R>) {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;

    let Some(state) = app.try_state::<Hotkeys>() else {
        return;
    };
    let mut entries = state.0.lock().unwrap();
    for e in entries.iter_mut() {
        if e.registered {
            continue;
        }
        match app.global_shortcut().register(e.chord.as_str()) {
            Ok(()) => {
                log::info!("desktop: global hotkey {} registered ({})", e.id, e.chord);
                e.registered = true;
                e.error = None;
            }
            Err(err) => {
                if e.error.is_none() {
                    log::warn!(
                        "desktop: {} hotkey ({}) unavailable ({err}) — another app may own it; \
                         continuing without the global shortcut",
                        e.id,
                        e.chord
                    );
                }
                e.error = Some(err.to_string());
            }
        }
    }
}

/// Which registered hotkey id a fired shortcut belongs to, from the managed state.
fn hotkey_id_for<R: Runtime>(app: &AppHandle<R>, fired: &Shortcut) -> Option<String> {
    let state = app.try_state::<Hotkeys>()?;
    let entries = state.0.lock().unwrap();
    entries
        .iter()
        .find(|e| {
            e.chord
                .parse::<Shortcut>()
                .map(|s| s == *fired)
                .unwrap_or(false)
        })
        .map(|e| e.id.clone())
}

/// Settings ▸ Keyboard reads the shell globals' live status (#1675).
#[tauri::command]
fn hotkeys_status(state: tauri::State<'_, Hotkeys>) -> Vec<HotkeyStatus> {
    state.0.lock().unwrap().clone()
}

/// Rebind one shell global (#1675): validate the chord, release the old one,
/// persist, then re-register fallibly — a chord another app owns comes back as
/// `registered:false` + error rather than an exception, matching launch behavior.
#[tauri::command]
fn hotkeys_set<R: Runtime>(
    app: AppHandle<R>,
    id: String,
    chord: String,
) -> Result<Vec<HotkeyStatus>, String> {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;

    let chord = chord.trim().to_lowercase();
    chord
        .parse::<Shortcut>()
        .map_err(|e| format!("'{chord}' is not a valid chord: {e}"))?;
    {
        let state = app.state::<Hotkeys>();
        let mut entries = state.0.lock().unwrap();
        let Some(entry) = entries.iter_mut().find(|e| e.id == id) else {
            return Err(format!("unknown hotkey id '{id}'"));
        };
        if entry.registered {
            let _ = app.global_shortcut().unregister(entry.chord.as_str());
        }
        entry.chord = chord;
        entry.registered = false;
        entry.error = None;
        save_hotkey_overrides(&app, &entries);
    } // drop the lock — sync_hotkeys re-locks
    sync_hotkeys(&app);
    Ok(app.state::<Hotkeys>().0.lock().unwrap().clone())
}

/// Holds the running sidecar so it can be killed when the app exits.
#[derive(Default)]
struct SidecarProcess(Mutex<Option<CommandChild>>);

/// Set when the app is tearing down — a sidecar `Terminated` event during shutdown
/// is the clean kill, not a crash to alert on.
static QUITTING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

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

/// A blocking, user-visible "the server didn't come up / died" alert with the log
/// location — a launch that silently shows a dead console is undebuggable from the
/// UI alone (#1668: fresh Windows install, blank window, zero diagnostics).
fn sidecar_alert<R: Runtime>(app: &AppHandle<R>, detail: &str) {
    let log_dir = app
        .path()
        .app_log_dir()
        .map(|d| d.display().to_string())
        .unwrap_or_else(|_| "the app's log directory".to_string());
    app.dialog()
        .message(format!("{detail}\n\nLogs: {log_dir}"))
        .title("protoAgent server problem")
        .buttons(MessageDialogButtons::Ok)
        .show(|_| {});
}

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
/// `PROTOAGENT_HOME` — the per-user dir becomes the instance root, so config
/// lands under `<dir>/config`. Failures are logged, not fatal — the window still
/// opens (and shows the API error) rather than the whole app refusing to boot.
fn spawn_sidecar<R: Runtime>(app: &AppHandle<R>, port: u16) {
    let config_dir = match app.path().app_config_dir() {
        Ok(dir) => dir,
        Err(e) => {
            log::error!("sidecar: cannot resolve app config dir: {e}");
            sidecar_alert(
                app,
                &format!("The server can't start: no app config directory ({e})."),
            );
            return;
        }
    };
    if let Err(e) = std::fs::create_dir_all(&config_dir) {
        log::error!("sidecar: cannot create config dir {config_dir:?}: {e}");
        sidecar_alert(
            app,
            &format!("The server can't start: config directory {config_dir:?} ({e})."),
        );
        return;
    }

    let command = match app.shell().sidecar("protoagent-server") {
        Ok(cmd) => cmd,
        Err(e) => {
            log::error!(
                "sidecar: binary not found (run apps/desktop/sidecar/build_sidecar.py): {e}"
            );
            sidecar_alert(
                app,
                &format!("The bundled server binary is missing or unlaunchable ({e})."),
            );
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
        .env("PROTOAGENT_HOME", config_dir.to_string_lossy().to_string());

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
            sidecar_alert(app, &format!("The server failed to launch ({e})."));
            return;
        }
    };

    if let Some(state) = app.try_state::<SidecarProcess>() {
        *state.0.lock().unwrap() = Some(child);
    }

    // Drain stdout/stderr so the OS pipe buffer never fills and stalls the child.
    let alert_handle = app.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                    log::info!("[sidecar] {}", String::from_utf8_lossy(&bytes).trim_end());
                }
                CommandEvent::Terminated(payload) => {
                    log::warn!("[sidecar] terminated: {payload:?}");
                    // A death that ISN'T our shutdown kill leaves a console with no
                    // server behind it — say so instead of a silently dead window
                    // (#1668). Boot crashes (port races, bad config) land here too.
                    if !QUITTING.load(std::sync::atomic::Ordering::Relaxed) {
                        let code = payload
                            .code
                            .map_or("unknown".to_string(), |c| c.to_string());
                        sidecar_alert(
                            &alert_handle,
                            &format!("The server stopped unexpectedly (exit code {code})."),
                        );
                    }
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
                        .message(format!(
                            "Updates aren't managed in-app for this install.\n\n{e}"
                        ))
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
        if on_event
            .send(String::from_utf8_lossy(&bytes).into_owned())
            .is_err()
        {
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

/// The launch-time update check's outcome (#2203), held for the webview to pull.
/// `done: false` = still in flight; `done + update: None` = up to date / check failed
/// (both mean "nothing to prompt"); `done + update: Some` = prompt immediately.
#[derive(serde::Serialize, Clone, Default)]
struct LaunchUpdateResult {
    done: bool,
    update: Option<UpdateInfo>,
}

/// Managed state for the launch check — written once by `spawn_launch_update_check`,
/// read (cheaply, no network) by the `updater_launch_result` command.
#[derive(Default)]
struct LaunchUpdateState(Mutex<LaunchUpdateResult>);

/// Kick off the update check CONCURRENTLY with sidecar/engine startup (#2203): the old
/// silent launch check was removed to avoid double-prompting (native dialog + web pill),
/// which left the first prompt waiting on webview boot + a 10s settle timer — you sat
/// through engine startup before learning a newer build existed. This check runs in
/// parallel with `spawn_sidecar`, never blocks window creation, and shows NO native
/// dialog: the result lands in `LaunchUpdateState`, where the web `UpdateNotice` pulls
/// it as soon as it mounts and owns the entire prompt UX (one prompt path, unchanged).
fn spawn_launch_update_check<R: Runtime>(app: AppHandle<R>) {
    tauri::async_runtime::spawn(async move {
        let outcome = match app.updater() {
            Ok(updater) => match updater.check().await {
                Ok(Some(update)) => {
                    let current = app.package_info().version.to_string();
                    log::info!("updater: {} available at launch (running {current})", update.version);
                    Some(UpdateInfo {
                        version: update.version.clone(),
                        current,
                        notes: update.body.clone().unwrap_or_default(),
                    })
                }
                Ok(None) => {
                    log::info!("updater: up to date (launch check)");
                    None
                }
                Err(e) => {
                    log::warn!("updater: launch check failed: {e}");
                    None
                }
            },
            Err(e) => {
                log::info!("updater: unavailable for this install: {e}");
                None
            }
        };
        if let Some(state) = app.try_state::<LaunchUpdateState>() {
            *state.0.lock().unwrap() = LaunchUpdateResult { done: true, update: outcome };
        }
    });
}

/// The launch check's stored outcome — a mutex read, safe for the webview to poll
/// while `done` is false. Complements `updater_check` (a fresh network check).
#[tauri::command]
fn updater_launch_result<R: Runtime>(app: AppHandle<R>) -> LaunchUpdateResult {
    app.try_state::<LaunchUpdateState>()
        .map(|s| s.0.lock().unwrap().clone())
        .unwrap_or_default()
}

#[derive(serde::Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct DownloadProgress {
    chunk_length: u64,
    content_length: Option<u64>,
}

/// Monotonic suffix for extra chat-window labels. Tauri window labels must be UNIQUE for
/// the app's lifetime — reusing one that was closed collides — so this only ever climbs.
static NEXT_WINDOW_ID: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(2);

/// Open an additional chat window (#1706).
///
/// The menu item existed and did nothing: same-origin new-window requests were denied
/// outright by `on_new_window` (which only ever forwarded EXTERNAL http(s) links to the
/// system browser), so there was no path to a second window at all.
///
/// A new window is a full second webview against the SAME sidecar — one server, one
/// fleet, one set of stores. Session independence falls out of the console's own model:
/// each window boots its own chat store and mints its own session id, and the URL is the
/// source of truth for which agent it targets (ADR 0042 slug routing), so two windows can
/// sit on two agents without desyncing.
///
/// `path` is an optional in-app route (e.g. `agent/roxy-1a2b/`) so a caller can open a
/// window already pointed at something; empty opens the default console.
fn open_chat_window<R: Runtime>(app: &AppHandle<R>, path: Option<String>) -> Result<(), String> {
    // WakeSignal already carries the resolved sidecar port (managed in setup, after
    // choose_port) — no second source of truth for it.
    let port = app
        .try_state::<WakeSignal>()
        .map(|s| s.port)
        .ok_or_else(|| "sidecar port not resolved yet".to_string())?;
    let id = NEXT_WINDOW_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let label = format!("main-{id}");
    // Same handoff the primary window gets — without it the webview has no API base and
    // boots into the setup wizard against the wrong origin.
    let init = format!("window.__PROTOAGENT_API_BASE__ = \"http://127.0.0.1:{port}\";");
    let route = path.unwrap_or_default();
    let route = route.trim_start_matches('/');
    let url = if route.is_empty() {
        format!("index.html?__apiPort={port}")
    } else {
        format!("index.html?__apiPort={port}#/{route}")
    };
    #[allow(unused_mut)]
    let mut builder = WebviewWindowBuilder::new(app, &label, WebviewUrl::App(url.into()))
        .title("protoAgent")
        .inner_size(1280.0, 820.0)
        .min_inner_size(980.0, 640.0)
        .resizable(true)
        // Offset rather than centered: a second window landing exactly on the first looks
        // like nothing happened — the same "did that work?" the no-op menu item produced.
        .position(60.0 + f64::from(id % 5) * 28.0, 60.0 + f64::from(id % 5) * 28.0)
        .initialization_script(&init);
    #[cfg(target_os = "macos")]
    {
        builder = builder
            .title_bar_style(tauri::TitleBarStyle::Overlay)
            .hidden_title(true);
    }
    builder.build().map_err(|e| e.to_string())?;
    log::info!("desktop: opened chat window {label}");
    Ok(())
}

/// Open another chat window — the webview-facing half of #1706, so a console menu item
/// or keybinding can request one. (An init-script global would be unreliable here; the
/// shell invokes this, matching how the rest of the desktop bridge works.)
#[tauri::command]
fn new_window<R: Runtime>(app: AppHandle<R>, path: Option<String>) -> Result<(), String> {
    open_chat_window(&app, path)
}

/// Is this new-window target OUR app rather than the wider web (#1706)?
///
/// Two shapes reach here: the Tauri asset scheme the window itself is served from
/// (`tauri://localhost`, and `http://tauri.localhost` on Windows), and a same-port
/// loopback URL — a console link to `http://127.0.0.1:<sidecar>/app/…`. Anything else,
/// including loopback on a DIFFERENT port, is somebody else's server and belongs in the
/// system browser.
fn is_own_origin(target: &str, port: u16) -> bool {
    if target.starts_with("tauri://") || target.starts_with("http://tauri.localhost") {
        return true;
    }
    for host in ["127.0.0.1", "localhost"] {
        if target.starts_with(&format!("http://{host}:{port}/"))
            || target == format!("http://{host}:{port}")
        {
            return true;
        }
    }
    false
}

/// The in-app route out of a same-origin target, or None for the bare app root.
/// `…/app/agent/roxy-1a2b/` and `…#/agent/roxy-1a2b/` both yield `agent/roxy-1a2b/`, so a
/// link to a specific agent opens a window already pointed at it.
fn own_origin_path(target: &str) -> Option<String> {
    let rest = target.split_once('#').map(|(_, frag)| frag).unwrap_or_else(|| {
        target
            .split_once("/app/")
            .map(|(_, path)| path)
            .unwrap_or("")
    });
    let rest = rest.trim_start_matches('/').trim();
    if rest.is_empty() || rest.starts_with("index.html") {
        None
    } else {
        Some(rest.to_string())
    }
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
            updater_launch_result,
            hide_launcher,
            focus_main,
            hotkeys_status,
            hotkeys_set,
            auth_token,
            pick_path,
            new_window
        ])
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
            // `[sidecar]` stdout/stderr (incl. a boot crash) lands on disk.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .build(),
            )?;
            app.manage(SidecarProcess::default());

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
                    "desktop: port {DEFAULT_PORT} is in use — sidecar on {port} (handoff via ?__apiPort)"
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
                "window.__PROTOAGENT_API_BASE__ = \"http://127.0.0.1:{port}\";"
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
                .initialization_script(&init)
                .on_new_window(move |url, _features| {
                    let target = url.as_str();
                    // Our OWN origin asking for a new window is "open this in a second
                    // window", not an external link — that request used to fall through
                    // to Deny below, which is why the menu item did nothing (#1706).
                    // Serve it with a real Tauri window rather than letting the webview
                    // spawn a chrome-less child that never gets our init script.
                    if is_own_origin(target, sidecar_port) {
                        if let Err(e) = open_chat_window(&link_opener, own_origin_path(target)) {
                            log::error!("desktop: failed to open a new chat window: {e}");
                        }
                    } else if target.starts_with("http://") || target.starts_with("https://") {
                        if let Err(e) = link_opener.opener().open_url(target, None::<&str>) {
                            log::error!("desktop: failed to open external link {target}: {e}");
                        }
                    }
                    // Deny either way: we've already served the request ourselves, and an
                    // unmanaged child webview would have no API base and no title bar.
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
            // shell never dialogs an available update on its own — that double-prompt
            // is why the old silent launch check was removed. The tray "Check for
            // Updates…" stays as the interactive native fallback.
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
            // Tear the bundled server down with the app rather than orphaning it.
            if let RunEvent::Exit = event {
                // The kill below fires the sidecar's Terminated event — mark the
                // shutdown so it isn't alerted as an unexpected server death.
                QUITTING.store(true, std::sync::atomic::Ordering::Relaxed);
                kill_sidecar(app_handle);
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

#[cfg(test)]
mod new_window_tests {
    use super::{is_own_origin, own_origin_path};

    // ── #1706: which new-window targets are OURS ──────────────────────────────
    #[test]
    fn own_origin_matches_the_tauri_asset_scheme() {
        assert!(is_own_origin("tauri://localhost/app/", 7870));
        assert!(is_own_origin("http://tauri.localhost/app/", 7870));
    }

    #[test]
    fn own_origin_matches_loopback_on_the_sidecar_port() {
        assert!(is_own_origin("http://127.0.0.1:7870/app/", 7870));
        assert!(is_own_origin("http://localhost:7870/app/agent/roxy-1a2b/", 7870));
        assert!(is_own_origin("http://127.0.0.1:7870", 7870));
    }

    #[test]
    fn loopback_on_a_different_port_is_somebody_elses_server() {
        // A dev server, another fork, an unrelated app — belongs in the browser, not
        // in one of our windows.
        assert!(!is_own_origin("http://127.0.0.1:3000/app/", 7870));
        assert!(!is_own_origin("http://localhost:5173/", 7870));
    }

    #[test]
    fn external_links_are_not_own_origin() {
        assert!(!is_own_origin("https://github.com/protoLabsAI/protoAgent", 7870));
        assert!(!is_own_origin("https://127.0.0.1.evil.test/app/", 7870));
    }

    #[test]
    fn a_port_prefix_collision_is_not_a_match() {
        // 7870 must not match 78700 — a prefix compare without the trailing
        // delimiter would.
        assert!(!is_own_origin("http://127.0.0.1:78700/app/", 7870));
    }

    #[test]
    fn own_origin_path_extracts_an_in_app_route() {
        assert_eq!(
            own_origin_path("http://127.0.0.1:7870/app/agent/roxy-1a2b/"),
            Some("agent/roxy-1a2b/".to_string())
        );
        assert_eq!(
            own_origin_path("tauri://localhost/index.html?__apiPort=7870#/agent/ava-9f/"),
            Some("agent/ava-9f/".to_string())
        );
    }

    #[test]
    fn the_bare_app_root_yields_no_route() {
        assert_eq!(own_origin_path("http://127.0.0.1:7870/app/"), None);
        assert_eq!(own_origin_path("tauri://localhost/index.html?__apiPort=7870"), None);
        assert_eq!(own_origin_path("http://127.0.0.1:7870"), None);
    }
}
