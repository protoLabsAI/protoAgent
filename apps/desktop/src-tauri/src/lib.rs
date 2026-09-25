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

/// How long a launch keeps re-probing a held 7870 before the #1668 fallback (#3503).
/// Long enough to outlast the path that frees the port when the previous shell could
/// not stop its sidecar itself (a crash, Windows, a stop that ran out of time): the
/// orphaned server's parent-death watchdog polls every 2 s, then reaps its trees with
/// a 1 s grace before it exits (server/__init__.py). A listener that stays, which is
/// what #1668 is for, costs a launch this long before it falls back as it always did.
const PORT_RETRY_WINDOW: Duration = Duration::from_secs(4);
const PORT_PROBE_INTERVAL: Duration = Duration::from_millis(250);

fn port_is_free(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// Probe until `is_free` says yes or `window` has passed, sleeping `interval` between
/// probes. Returns how long it waited when the port came free, None when it never did.
/// The sleeps are the clock (a probe is a bind that costs microseconds), so tests drive
/// it with a fake one. Always bounded: at most `window / interval` sleeps.
fn wait_until_free(
    mut is_free: impl FnMut() -> bool,
    mut sleep: impl FnMut(Duration),
    window: Duration,
    interval: Duration,
) -> Option<Duration> {
    let mut waited = Duration::ZERO;
    loop {
        if is_free() {
            return Some(waited);
        }
        if waited >= window {
            return None;
        }
        // A zero interval would never advance the clock: spend the rest of the window.
        let step = if interval.is_zero() {
            window - waited
        } else {
            interval.min(window - waited)
        };
        sleep(step);
        waited += step;
    }
}

/// The sidecar's port: the fixed default when it's free, else an OS-assigned free
/// port. Launching straight at an occupied 7870 — an orphaned sidecar, a headless
/// dev server, any unrelated app — meant the new sidecar died at bind and the
/// webview loaded a dead/foreign server with no error (#1668). The chosen port
/// reaches the page via `?__apiPort=` on the webview URL (primary — the URL is
/// always visible to the page, unlike the injected global) plus the
/// `__PROTOAGENT_API_BASE__` init script. Bind-probe-then-release has a tiny
/// TOCTOU window — acceptable for a single local launch.
///
/// A held 7870 is re-probed for `PORT_RETRY_WINDOW` before falling back (#3503). The
/// holder is often OUR previous sidecar on its way out: an update's restart relaunches
/// about 0.6 s after the old shell exits, and a hub that fell back stayed on a random
/// port for the whole session, so anything addressing 127.0.0.1:7870 found nothing.
/// The retry is unconditional rather than gated on evidence of a restart: the cases it
/// exists for (a crashed shell, a Windows installer relaunch, a stop that ran out of
/// time) are exactly the ones that leave no evidence behind.
fn choose_port_with(
    default_is_free: impl FnMut() -> bool,
    sleep: impl FnMut(Duration),
    fallback: impl FnOnce() -> Option<u16>,
) -> u16 {
    match wait_until_free(default_is_free, sleep, PORT_RETRY_WINDOW, PORT_PROBE_INTERVAL) {
        Some(waited) => {
            if !waited.is_zero() {
                log::info!(
                    "desktop: port {DEFAULT_PORT} came free after {} ms (a previous sidecar exiting)",
                    waited.as_millis()
                );
            }
            DEFAULT_PORT
        }
        None => fallback().unwrap_or(DEFAULT_PORT),
    }
}

fn choose_port() -> u16 {
    choose_port_with(
        || port_is_free(DEFAULT_PORT),
        std::thread::sleep,
        || {
            TcpListener::bind("127.0.0.1:0")
                .and_then(|l| l.local_addr())
                .map(|addr| addr.port())
                .ok()
        },
    )
}

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

/// The level a captured sidecar output line is logged at (#3504).
///
/// Every request the hub serves or proxies prints two INFO lines on the sidecar's output:
/// uvicorn's access line and the proxy's httpx client line. An open console makes several
/// a second, and they rotated everything else out of the desktop log. Exactly those two
/// shapes, and only at INFO, go to DEBUG, below the file's INFO threshold. Every other line
/// stays at INFO: boot and lifecycle, a WARNING or ERROR that happens to mention a request,
/// and each line of a traceback.
fn sidecar_line_level(line: &str) -> log::Level {
    if is_uvicorn_access_line(line) || is_httpx_request_line(line) {
        log::Level::Debug
    } else {
        log::Level::Info
    }
}

/// uvicorn's default access format, `%(levelprefix)s %(client_addr)s - "%(request_line)s"
/// %(status_code)s`, uncoloured because stdout is a pipe:
/// `INFO:     127.0.0.1:54910 - "GET /api/fleet HTTP/1.1" 200 OK`. The level prefix is part
/// of the shape, so uvicorn's WARNING/ERROR lines never match.
fn is_uvicorn_access_line(line: &str) -> bool {
    let Some(rest) = line.strip_prefix("INFO:") else {
        return false;
    };
    let Some((client, request)) = rest.trim_start().split_once(" - \"") else {
        return false;
    };
    // `host:port`, one token; then a quoted `METHOD path HTTP/x`.
    client.contains(':') && !client.contains(char::is_whitespace) && request.contains(" HTTP/")
}

/// The httpx client's per-request line under the server's log format
/// (`%(asctime)s %(levelname)s %(name)s %(message)s`, observability/logging_config.py):
/// `2026-09-13 21:52:46,403 INFO httpx HTTP Request: GET http://127.0.0.1:7881/... "HTTP/1.1 200 OK"`.
/// Level and logger are matched by field, so the same message at WARNING, or another
/// logger quoting it, stays at INFO.
fn is_httpx_request_line(line: &str) -> bool {
    let mut fields = line.splitn(5, ' ');
    let (Some(_date), Some(_time), Some(level), Some(logger), Some(message)) = (
        fields.next(),
        fields.next(),
        fields.next(),
        fields.next(),
        fields.next(),
    ) else {
        return false;
    };
    level == "INFO" && logger == "httpx" && message.starts_with("HTTP Request: ")
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
#[cfg(unix)]
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
#[cfg(unix)]
fn login_shell_path() -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
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
#[cfg(unix)]
fn augmented_sidecar_path() -> String {
    let mut entries: Vec<String> = Vec::new();
    if let Some(shell_path) = login_shell_path() {
        dedup_push_path(&mut entries, &shell_path);
    }
    dedup_push_path(&mut entries, "/opt/homebrew/bin:/usr/local/bin");
    // Per-user tool dirs the login shell usually adds but a .desktop/launchd launch
    // never sees: `br` (cargo), `gh`/`claude-agent-acp` installed per-user (pip/npm
    // `--user`, Linux Homebrew). Same belt-and-suspenders as the Homebrew dirs above.
    if let Ok(home) = std::env::var("HOME") {
        dedup_push_path(
            &mut entries,
            &format!("{home}/.cargo/bin:{home}/.local/bin:/home/linuxbrew/.linuxbrew/bin"),
        );
    }
    if let Ok(existing) = std::env::var("PATH") {
        dedup_push_path(&mut entries, &existing);
    }
    entries.join(":")
}

/// Launch the bundled protoAgent server (console UI tier) as a sidecar.
///
/// The frozen binary is read-only, so its writable state (live config,
/// secrets, setup marker) is pointed at the per-user app-config dir via
/// `PROTOAGENT_HOME` and `PROTOAGENT_BOX_ROOT` — the per-user dir becomes both
/// the instance root and machine-shared box root, so all writable state remains
/// inside the app-config directory. Failures are logged, not fatal — the window still
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
    let mut command = command
        // The desktop renders the React operator console, so run the server in
        // its 'console' UI tier (API + A2A + console, no Gradio) — ADR 0010.
        // (Was the now-deprecated --headless / PROTOAGENT_HEADLESS alias.)
        .args(["--ui", "console", "--port", &port_arg])
        .env("PROTOAGENT_UI", "console")
        // So the sidecar exits if we die without a clean kill (the frozen
        // onefile's child process otherwise outlives us, holding its port).
        .env("PROTOAGENT_PARENT_PID", std::process::id().to_string())
        .env("PROTOAGENT_HOME", config_dir.to_string_lossy().to_string())
        .env("PROTOAGENT_BOX_ROOT", config_dir.to_string_lossy().to_string());

    // A Finder/Dock/launchd launch (macOS) or a .desktop launch (Linux) strips PATH
    // down to the session's minimal set, hiding Homebrew/nvm/Volta/asdf/cargo — so
    // delegate launch commands (`npx`, ACP adapters) and the board's `br`/`gh` fail
    // with "binary not on PATH" (#1299). Hand the sidecar the user's real PATH.
    #[cfg(unix)]
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
                    // One event per line (the shell plugin reads line by line), so each
                    // is classified on its own: per-request lines drop below the log
                    // file's INFO threshold (#3504).
                    let line = String::from_utf8_lossy(&bytes);
                    let line = line.trim_end();
                    log::log!(sidecar_line_level(line), "[sidecar] {line}");
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

/// How long stopping the sidecar waits for it to give its port back (#3503). Measured
/// on macOS with the frozen sidecar: the listener closes 0.4-0.6 s after SIGTERM.
#[cfg(unix)]
const SIDECAR_STOP_GRACE: Duration = Duration::from_secs(3);
#[cfg(unix)]
const SIDECAR_STOP_POLL: Duration = Duration::from_millis(50);

/// Stop the sidecar and give its port back before this process goes away (#3503).
/// Called on app exit and before an update's restart; idempotent.
///
/// The tracked child is the PyInstaller onefile BOOTLOADER; the server holding the port
/// is its child. `child.kill()` is SIGKILL on Unix, which the bootloader can't forward:
/// it died alone, the server kept the port until its parent-death watchdog noticed this
/// process was gone, and a relaunch inside that window (an update's restart comes about
/// 0.6 s after) found 7870 taken and fell back to a random port for the whole session.
/// SIGTERM IS forwarded: uvicorn closes its listener at once and starts its own teardown
/// (measured: port free in about 0.5 s, the whole tree gone in about 1 s, the onefile
/// extraction dir cleaned up).
///
/// So on Unix: SIGTERM, then wait, bounded and never a hang, until the port binds. Once
/// it does, the bootloader is left to finish on its own; a SIGKILL would cut the
/// server's teardown short and strand its extraction dir, and the server's watchdog
/// still ends it within seconds of our exit if that teardown stalls. If the port is
/// still held at the deadline, fall back to the old kill; the relaunch's own retry
/// (`PORT_RETRY_WINDOW`) covers the watchdog path from there. Windows keeps the plain
/// kill: TerminateProcess can't be forwarded either, so no wait here could succeed, and
/// the relaunch's retry covers it.
fn stop_sidecar<R: Runtime>(app: &AppHandle<R>) {
    // The stop fires the sidecar's Terminated event: mark the shutdown first so it
    // isn't alerted as an unexpected server death.
    QUITTING.store(true, std::sync::atomic::Ordering::Relaxed);
    let Some(state) = app.try_state::<SidecarProcess>() else {
        return;
    };
    let Some(child) = state.0.lock().unwrap().take() else {
        return;
    };
    #[cfg(unix)]
    {
        // WakeSignal carries the port the sidecar was spawned on (see open_chat_window).
        if let Some(port) = app.try_state::<WakeSignal>().map(|s| s.port) {
            if terminate_and_wait(child.pid(), port) {
                return;
            }
        }
    }
    let _ = child.kill();
}

/// SIGTERM the sidecar and wait for its port. True once the port binds; false when the
/// signal couldn't be sent or the port is still held after `SIDECAR_STOP_GRACE`.
#[cfg(unix)]
fn terminate_and_wait(pid: u32, port: u16) -> bool {
    let Ok(pid) = libc::pid_t::try_from(pid) else {
        return false;
    };
    // SAFETY: kill(2) takes no pointers. The pid is our own un-reaped child: the shell
    // plugin reaps it only after it exits, so the pid can't have been reused yet.
    if unsafe { libc::kill(pid, libc::SIGTERM) } != 0 {
        log::warn!(
            "sidecar: SIGTERM failed ({}), killing it instead",
            std::io::Error::last_os_error()
        );
        return false;
    }
    match wait_until_free(
        || port_is_free(port),
        std::thread::sleep,
        SIDECAR_STOP_GRACE,
        SIDECAR_STOP_POLL,
    ) {
        Some(waited) => {
            log::info!("sidecar: stopped, port {port} free after {} ms", waited.as_millis());
            true
        }
        None => {
            log::warn!(
                "sidecar: port {port} still held {} s after SIGTERM, killing it",
                SIDECAR_STOP_GRACE.as_secs()
            );
            false
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

/// What the pre-install freshness recheck (#2832) decided. Pure so the
/// dialog-open-while-releases-advance contract is unit-testable: an offer for A
/// must never install A once B is Latest without renewed confirmation.
#[derive(Debug, PartialEq)]
enum Freshness {
    /// The offer is still Latest — install the FRESH object (fresh URLs/signature).
    Install,
    /// Latest moved past the offer while the dialog sat open — re-offer, never
    /// silently install either version.
    Superseded,
    /// The endpoint no longer offers anything (we're current after all).
    UpToDate,
}

fn classify_recheck(offered: &str, fresh: Option<&str>) -> Freshness {
    match fresh {
        None => Freshness::UpToDate,
        Some(v) if v == offered => Freshness::Install,
        Some(_) => Freshness::Superseded,
    }
}

const PRIMARY_WINDOW_LABEL: &str = "main";
const UPDATE_REQUEST_EVENT: &str = "updater:check-requested";

#[derive(Default)]
struct UpdateRequestSequence {
    next_id: u64,
    pending: Option<u64>,
}

/// Monotonic request id plus a one-slot durable inbox. A tray click can arrive before
/// React has registered its event listener during desktop boot; retaining the latest id
/// lets the primary window pull it after subscribing. Repeated clicks intentionally
/// coalesce — one fresh check is enough.
#[derive(Default)]
struct UpdateRequestState(Mutex<UpdateRequestSequence>);

impl UpdateRequestState {
    fn record(&self) -> u64 {
        let mut state = self.0.lock().unwrap();
        state.next_id = state.next_id.saturating_add(1);
        state.pending = Some(state.next_id);
        state.next_id
    }

    fn consume(&self) -> Option<u64> {
        self.0.lock().unwrap().pending.take()
    }

    fn acknowledge(&self, request_id: u64) {
        let mut state = self.0.lock().unwrap();
        if state.pending == Some(request_id) {
            state.pending = None;
        }
    }
}

fn request_update_from_tray<R: Runtime>(app: &AppHandle<R>) {
    show_main_window(app);
    let Some(state) = app.try_state::<UpdateRequestState>() else {
        log::warn!("updater: tray request state unavailable");
        return;
    };
    let request_id = state.record();
    if let Err(e) = app.emit_to(
        tauri::EventTarget::webview_window(PRIMARY_WINDOW_LABEL),
        UPDATE_REQUEST_EVENT,
        request_id,
    ) {
        // The durable pending id remains available for the webview to pull after boot.
        log::warn!("updater: couldn't notify the primary window: {e}");
    }
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

#[derive(serde::Serialize, Clone)]
struct UpdateInfo {
    version: String,
    current: String,
    /// The release notes / changelog (latest.json `notes`) — shown in the in-app pill.
    notes: String,
}

#[derive(Clone)]
enum UpdateCheckOutcome {
    Available(UpdateInfo),
    Current,
    Error(String),
}

#[derive(Default)]
struct UpdateCheckSnapshot {
    generation: u64,
    outcome: Option<UpdateCheckOutcome>,
}

impl UpdateCheckSnapshot {
    fn completed_after(&self, observed_generation: u64) -> Option<UpdateCheckOutcome> {
        if self.generation > observed_generation {
            self.outcome.clone()
        } else {
            None
        }
    }

    fn record(&mut self, outcome: UpdateCheckOutcome) {
        self.generation = self.generation.saturating_add(1);
        self.outcome = Some(outcome);
    }
}

/// Serialize updater manifest reads and share the result among callers that overlap.
/// Launch, periodic, and tray checks can otherwise race through the updater plugin and
/// produce duplicate prompts. A caller that starts after the prior check completed still
/// performs a fresh read, preserving the six-hour and explicit-interaction semantics.
#[derive(Default)]
struct UpdateCheckCoordinator {
    gate: tauri::async_runtime::Mutex<()>,
    snapshot: Mutex<UpdateCheckSnapshot>,
}

fn update_info<R: Runtime>(app: &AppHandle<R>, update: &tauri_plugin_updater::Update) -> UpdateInfo {
    UpdateInfo {
        version: update.version.clone(),
        current: app.package_info().version.to_string(),
        notes: update.body.clone().unwrap_or_default(),
    }
}

async fn perform_update_check<R: Runtime>(app: &AppHandle<R>) -> UpdateCheckOutcome {
    let updater = match app.updater() {
        Ok(updater) => updater,
        Err(e) => return UpdateCheckOutcome::Error(e.to_string()),
    };
    match updater.check().await {
        Ok(Some(update)) => UpdateCheckOutcome::Available(update_info(app, &update)),
        Ok(None) => UpdateCheckOutcome::Current,
        Err(e) => UpdateCheckOutcome::Error(e.to_string()),
    }
}

async fn coordinated_update_check<R: Runtime>(app: &AppHandle<R>) -> UpdateCheckOutcome {
    let Some(coordinator) = app.try_state::<UpdateCheckCoordinator>() else {
        return perform_update_check(app).await;
    };
    let observed_generation = coordinator.snapshot.lock().unwrap().generation;
    let _guard = coordinator.gate.lock().await;

    // Another caller completed while this one waited: reuse exactly that result instead
    // of issuing an overlapping request. The next non-overlapping caller observes the new
    // generation before taking the gate and therefore performs its own fresh check.
    {
        let snapshot = coordinator.snapshot.lock().unwrap();
        if let Some(outcome) = snapshot.completed_after(observed_generation) {
            return outcome;
        }
    }

    let outcome = perform_update_check(app).await;
    coordinator.snapshot.lock().unwrap().record(outcome.clone());
    outcome
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
        let outcome = match coordinated_update_check(&app).await {
            UpdateCheckOutcome::Available(update) => {
                log::info!(
                    "updater: {} available at launch (running {})",
                    update.version,
                    update.current
                );
                Some(update)
            }
            UpdateCheckOutcome::Current => {
                log::info!("updater: up to date (launch check)");
                None
            }
            UpdateCheckOutcome::Error(e) => {
                log::warn!("updater: launch check failed or unavailable: {e}");
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
        // Hand drag-and-drop to the WEB CONTENT (#3197). Tauri's drag-drop handler is on by
        // default, and on macOS wry implements it by overriding the webview's
        // NSDraggingDestination methods and returning "accepted" WITHOUT calling super — so
        // WKWebView's own drop handling never runs and the page receives no dragover/drop at
        // all. Tauri's own docs say the same for Windows. Nothing in the console listens for
        // the native `tauri://drag-*` events; three surfaces DO use HTML5 drop — the fleet
        // roster reorder, the chat composer's file attach, and the knowledge store's — and all
        // three were dead in the desktop build while working in every browser.
        .disable_drag_drop_handler()
        // The gap #3316 was filed for: this ran on the MAIN window only, so a link clicked in
        // a second window bypassed the triage entirely and spawned an unmanaged child webview.
        .on_new_window({
            let app = app.clone();
            move |url, _features| serve_new_window(&app, port, url.as_str())
        })
        .on_navigation({
            let app = app.clone();
            move |url| serve_navigation(&app, url.as_str())
        })
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

/// What a new-window request should become (#1706, generalised in #3316). Pure, so the triage
/// is unit-tested without a webview.
#[derive(Debug, PartialEq, Eq)]
enum NewWindow {
    /// Ours — open a real managed window, optionally already at this in-app route.
    Managed(Option<String>),
    /// The wider web — hand it to the system browser.
    External,
    /// An editor deep link (`zed://file/…`, `vscode://file/…`, `cursor://file/…`) from a
    /// tool card's "open in editor" link — hand it to the OS, which launches the editor.
    Editor,
    /// Some other scheme we don't serve (mailto:, a custom protocol): drop it.
    Ignore,
}

fn route_new_window(target: &str, port: u16) -> NewWindow {
    if is_own_origin(target, port) {
        NewWindow::Managed(own_origin_path(target))
    } else if target.starts_with("http://") || target.starts_with("https://") {
        NewWindow::External
    } else if is_editor_link(target) {
        NewWindow::Editor
    } else {
        NewWindow::Ignore
    }
}

/// Serve a new-window request ourselves, then always `Deny`.
///
/// **Every window the shell builds must wire this, not just the main one (#3316).** A window
/// without it lets the webview spawn an unmanaged child, which gets none of the setup a window
/// needs: no `initialization_script` (so no `__PROTOAGENT_API_BASE__` — it boots the setup
/// wizard against the wrong origin), no title-bar style, and no `disable_drag_drop_handler`
/// (#3197). `on_new_window` used to sit on the main window alone, so a link clicked in a
/// SECONDARY window fell through to exactly that — including `window.open` on an OAuth
/// provider login, which belongs in the system browser and instead rendered chrome-less
/// inside the app.
fn serve_new_window<R: Runtime>(
    app: &AppHandle<R>,
    port: u16,
    target: &str,
) -> tauri::webview::NewWindowResponse<R> {
    match route_new_window(target, port) {
        NewWindow::Managed(path) => {
            // Our OWN origin asking for a new window means "open this in a second window" —
            // serve it with a real Tauri window rather than a chrome-less child (#1706).
            if let Err(e) = open_chat_window(app, path) {
                log::error!("desktop: failed to open a new chat window: {e}");
            }
        }
        NewWindow::External => {
            if let Err(e) = app.opener().open_url(target, None::<&str>) {
                log::error!("desktop: failed to open external link {target}: {e}");
            }
        }
        NewWindow::Editor => open_editor_link(app, target),
        NewWindow::Ignore => {}
    }
    // Deny either way: we've already served the request ourselves.
    tauri::webview::NewWindowResponse::Deny
}

/// The ONLY custom schemes the shell will hand to the OS: the console's "open in editor"
/// links (apps/web/src/lib/editorLinks.ts). A STRICT allowlist on purpose — the webview
/// hosts plugin views and agent-authored content, and a generic custom-scheme passthrough
/// would let any of it launch an arbitrary registered URL handler (`ms-settings:`,
/// `file:`, some other app's protocol). Only the `://file/` form is accepted, which is
/// exactly what the console emits; anything else (other schemes, a look-alike prefix such
/// as `zedx:`, a non-file editor action) is not an editor link.
const EDITOR_SCHEMES: [&str; 3] = ["zed", "vscode", "cursor"];

fn is_editor_link(target: &str) -> bool {
    let Some((scheme, rest)) = target.split_once(':') else {
        return false;
    };
    EDITOR_SCHEMES
        .iter()
        .any(|s| scheme.eq_ignore_ascii_case(s))
        && rest.starts_with("//file/")
}

/// Strict percent-decoding (UTF-8). `None` on a malformed escape or invalid UTF-8 — a
/// link we can't decode unambiguously is not opened.
fn percent_decode(s: &str) -> Option<String> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let hex = b.get(i + 1..i + 3)?;
            if !hex.iter().all(u8::is_ascii_hexdigit) {
                return None;
            }
            out.push(u8::from_str_radix(std::str::from_utf8(hex).ok()?, 16).ok()?);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// The local file an allowlisted editor link points at — `Some` ONLY when the decoded path
/// (minus an optional `:line[:col]` suffix) is an absolute path to an EXISTING REGULAR FILE,
/// with no `..` component and no query/fragment. The scheme allowlist alone would still let
/// any script in the window (a plugin view, agent-authored content) launch the editor on
/// an arbitrary path with no click — and a DIRECTORY opens as a workspace, whose project
/// settings (`.zed/settings.json`, `.vscode/tasks.json`) an agent with fenced write access
/// could have planted. The console only ever links files, so nothing legitimate is lost.
fn editor_link_file(target: &str) -> Option<std::path::PathBuf> {
    use std::path::{Component, Path};
    if !is_editor_link(target) {
        return None;
    }
    let (_, rest) = target.split_once(':')?;
    let raw = rest.strip_prefix("//file")?; // keeps the leading '/'
    if raw.contains('?') || raw.contains('#') {
        return None;
    }
    let decoded = percent_decode(raw)?;
    if decoded.contains('\0') {
        return None;
    }
    // `vscode://file/C:/x` → `/C:/x`: drop the slash before a Windows drive letter.
    #[cfg(windows)]
    let decoded = {
        let b = decoded.as_bytes();
        if b.len() >= 3 && b[0] == b'/' && b[1].is_ascii_alphabetic() && b[2] == b':' {
            decoded[1..].to_string()
        } else {
            decoded
        }
    };
    let mut cand = decoded.as_str();
    // The path itself, then with up to two trailing `:<digits>` (line, column) removed.
    for _ in 0..3 {
        let p = Path::new(cand);
        if p.is_absolute()
            && is_local_disk_path(p)
            && !p.components().any(|c| matches!(c, Component::ParentDir))
            && !is_workspace_file(p)
            && p.is_file()
        {
            return Some(p.to_path_buf());
        }
        match cand.rsplit_once(':') {
            Some((head, tail)) if !tail.is_empty() && tail.bytes().all(|c| c.is_ascii_digit()) => {
                cand = head
            }
            _ => return None,
        }
    }
    None
}

/// Windows: only a drive-letter path (`C:\…`, `\\?\C:\…`). A UNC path
/// (`//attacker/share/x`) would make even the `is_file()` probe reach out over SMB and hand
/// the host the user's NTLM credentials — so it's refused BEFORE any filesystem access.
/// Elsewhere every absolute path is local.
fn is_local_disk_path(p: &std::path::Path) -> bool {
    #[cfg(windows)]
    {
        use std::path::{Component, Prefix};
        matches!(
            p.components().next(),
            Some(Component::Prefix(pre)) if matches!(pre.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))
        )
    }
    #[cfg(not(windows))]
    {
        let _ = p;
        true
    }
}

/// A `.code-workspace` file opens as a WORKSPACE in VS Code / Cursor (its settings, tasks
/// and extension recommendations apply) — the same hazard as opening a directory.
fn is_workspace_file(p: &std::path::Path) -> bool {
    p.extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("code-workspace"))
}

fn open_editor_link<R: Runtime>(app: &AppHandle<R>, target: &str) {
    if editor_link_file(target).is_none() {
        log::warn!("desktop: refused editor link (not an existing file): {target}");
        return;
    }
    if let Err(e) = app.opener().open_url(target, None::<&str>) {
        log::error!("desktop: failed to open editor link {target}: {e}");
    }
}

/// Same-window navigation guard, wired on every shell-built window alongside
/// `serve_new_window`. The console's editor links are plain `<a href>` with no target (a
/// browser hands a custom scheme to the OS without unloading the page), so in the webview
/// they arrive as a NAVIGATION, not a new-window request — and WKWebView/WebView2 can't
/// load `zed://`, so the click did nothing. Allowlisted editor links go to the OS and the
/// navigation is cancelled; everything else proceeds exactly as before (returns true).
fn serve_navigation<R: Runtime>(app: &AppHandle<R>, target: &str) -> bool {
    if is_editor_link(target) {
        open_editor_link(app, target);
        return false;
    }
    true
}

/// Check the updater manifest for a newer build, returning its version + notes for the
/// in-app UpdateNotice (the web pill renders the changelog). None when up to date;
/// Err on failure so an interactive tray request can surface useful feedback.
#[tauri::command]
async fn updater_check<R: Runtime>(app: AppHandle<R>) -> Result<Option<UpdateInfo>, String> {
    match coordinated_update_check(&app).await {
        UpdateCheckOutcome::Available(update) => Ok(Some(update)),
        UpdateCheckOutcome::Current => Ok(None),
        UpdateCheckOutcome::Error(e) => Err(e),
    }
}

/// Consume the latest tray request after the frontend listener is registered. Only the
/// primary window may consume it, so an already-open secondary chat window cannot steal
/// a boot-time request or open a duplicate dialog.
#[tauri::command]
fn updater_consume_request<R: Runtime>(
    app: AppHandle<R>,
    window: tauri::WebviewWindow<R>,
) -> Option<u64> {
    if window.label() != PRIMARY_WINDOW_LABEL {
        return None;
    }
    app.try_state::<UpdateRequestState>()
        .and_then(|state| state.consume())
}

/// Clear a request delivered over the live event path. The id comparison is critical:
/// an acknowledgement delayed behind a newer tray click must not erase that newer
/// request from the durable boot inbox.
#[tauri::command]
fn updater_ack_request<R: Runtime>(
    app: AppHandle<R>,
    window: tauri::WebviewWindow<R>,
    request_id: u64,
) {
    if window.label() != PRIMARY_WINDOW_LABEL {
        return;
    }
    if let Some(state) = app.try_state::<UpdateRequestState>() {
        state.acknowledge(request_id);
    }
}

#[derive(serde::Serialize)]
#[serde(tag = "status", rename_all = "camelCase")]
enum InstallUpdateResult {
    Superseded { update: UpdateInfo },
    UpToDate,
}

/// Download + install the available update (signature-verified by the plugin against the
/// embedded pubkey), streaming progress to the webview over an IPC Channel, then relaunch.
#[tauri::command]
async fn updater_install<R: Runtime>(
    app: AppHandle<R>,
    expected_version: String,
    on_progress: tauri::ipc::Channel<DownloadProgress>,
) -> Result<InstallUpdateResult, String> {
    // Share the same gate as manifest checks: no launch/periodic/tray read can race the
    // operator's final freshness check and download. We still perform a NEW check here;
    // the dialog may have remained open across multiple releases (#2832).
    let coordinator = app.state::<UpdateCheckCoordinator>();
    let _guard = coordinator.gate.lock().await;
    let updater = app.updater().map_err(|e| e.to_string())?;
    let fresh = updater.check().await.map_err(|e| e.to_string())?;
    match classify_recheck(&expected_version, fresh.as_ref().map(|u| u.version.as_str())) {
        Freshness::Superseded => {
            let update = fresh.expect("Superseded implies a fresh update");
            log::info!(
                "updater: offer {expected_version} superseded by {} — returning it for renewed confirmation",
                update.version
            );
            return Ok(InstallUpdateResult::Superseded {
                update: update_info(&app, &update),
            });
        }
        Freshness::UpToDate => {
            log::info!("updater: offer {expected_version} withdrawn — endpoint reports up to date");
            return Ok(InstallUpdateResult::UpToDate);
        }
        Freshness::Install => {}
    }
    // Install the FRESH object — same version the operator confirmed, but with the
    // endpoint's current URLs and signature.
    let update = fresh.expect("Install implies a fresh update");
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
    // Give the sidecar's port back BEFORE relaunching (#3503): the new launch probes 7870
    // about 0.6 s after this process exits. (Windows never gets here: its installer exits
    // the app inside `download_and_install`, and the relaunch's retry covers that path.)
    // stop_sidecar blocks for up to SIDECAR_STOP_GRACE, so it runs off the async runtime.
    // The restart's own RunEvent::Exit calls it again, as a no-op.
    let stopping = app.clone();
    let _ = tauri::async_runtime::spawn_blocking(move || stop_sidecar(&stopping)).await;
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
            updater_consume_request,
            updater_ack_request,
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
mod updater_freshness_tests {
    use super::{classify_recheck, Freshness};

    // #2832 acceptance: dialog opened for A -> endpoint advances to B ->
    // the install action must NEVER install A without renewed confirmation.
    #[test]
    fn superseded_offer_is_never_installed() {
        assert_eq!(classify_recheck("0.137.1", Some("0.139.0")), Freshness::Superseded);
    }

    #[test]
    fn still_latest_installs_the_fresh_object() {
        assert_eq!(classify_recheck("0.140.0", Some("0.140.0")), Freshness::Install);
    }

    #[test]
    fn withdrawn_offer_reports_up_to_date() {
        assert_eq!(classify_recheck("0.140.0", None), Freshness::UpToDate);
    }

    #[test]
    fn even_a_downgrade_offer_counts_as_superseded() {
        // The endpoint is authoritative in both directions: never install an
        // object the endpoint no longer serves.
        assert_eq!(classify_recheck("0.141.0", Some("0.140.1")), Freshness::Superseded);
    }
}

#[cfg(test)]
mod update_request_tests {
    use super::UpdateRequestState;

    #[test]
    fn repeated_tray_clicks_coalesce_to_the_latest_pending_request() {
        let state = UpdateRequestState::default();

        assert_eq!(state.record(), 1);
        assert_eq!(state.record(), 2);
        assert_eq!(state.consume(), Some(2));
        assert_eq!(state.consume(), None);
    }

    #[test]
    fn request_ids_remain_monotonic_after_a_request_is_taken() {
        let state = UpdateRequestState::default();

        assert_eq!(state.record(), 1);
        assert_eq!(state.consume(), Some(1));
        assert_eq!(state.record(), 2);
        assert_eq!(state.consume(), Some(2));
    }

    #[test]
    fn a_late_acknowledgement_cannot_erase_a_newer_request() {
        let state = UpdateRequestState::default();

        let first = state.record();
        let second = state.record();
        state.acknowledge(first);
        assert_eq!(state.consume(), Some(second));

        let third = state.record();
        state.acknowledge(third);
        assert_eq!(state.consume(), None);
    }
}

#[cfg(test)]
mod update_check_coordinator_tests {
    use super::{UpdateCheckOutcome, UpdateCheckSnapshot};

    #[test]
    fn a_waiter_reuses_only_a_check_completed_after_it_started_waiting() {
        let mut snapshot = UpdateCheckSnapshot::default();
        let observed = snapshot.generation;

        assert!(snapshot.completed_after(observed).is_none());
        snapshot.record(UpdateCheckOutcome::Current);
        assert!(matches!(
            snapshot.completed_after(observed),
            Some(UpdateCheckOutcome::Current)
        ));
        assert!(snapshot.completed_after(snapshot.generation).is_none());
    }
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
    use super::{
        editor_link_file, is_editor_link, is_own_origin, own_origin_path, percent_decode,
        route_new_window, NewWindow,
    };

    /// `<scheme>://file/<abs>` the way the console builds it: each byte outside the
    /// unreserved set (and `/`, `:`) percent-encoded, a Windows drive given a leading `/`.
    fn link_for(scheme: &str, p: &std::path::Path, suffix: &str) -> String {
        let s = p.to_string_lossy().replace('\\', "/");
        let s = if s.starts_with('/') {
            s
        } else {
            format!("/{s}")
        };
        let mut enc = String::new();
        for b in s.bytes() {
            if b.is_ascii_alphanumeric() || b"/:.-_~".contains(&b) {
                enc.push(b as char);
            } else {
                enc.push_str(&format!("%{b:02X}"));
            }
        }
        format!("{scheme}://file{enc}{suffix}")
    }

    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("pa-editor-link-{tag}-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    // ── #3596 review: the allowlist opens only an EXISTING REGULAR FILE ──────────
    #[test]
    fn editor_link_resolves_an_existing_file_with_position() {
        let d = scratch_dir("file");
        let f = d.join("a b é#1.rs");
        std::fs::write(&f, "x").unwrap();
        for (scheme, suffix) in [
            ("zed", ""),
            ("zed", ":42"),
            ("vscode", ":42:7"),
            ("cursor", ":1"),
        ] {
            let link = link_for(scheme, &f, suffix);
            assert_eq!(
                editor_link_file(&link).as_deref(),
                Some(f.as_path()),
                "{link}"
            );
        }
    }

    #[test]
    fn editor_link_refuses_dirs_missing_files_and_odd_shapes() {
        let d = scratch_dir("refuse");
        let f = d.join("ok.rs");
        std::fs::write(&f, "x").unwrap();
        let file_link = link_for("zed", &f, "");
        for bad in [
            link_for("zed", &d, ""),                      // a directory = a workspace
            link_for("vscode", &d, ":3"),                 // …even with a line
            link_for("zed", &d.join("missing.rs"), ":1"), // not there
            format!("{file_link}?windowId=_blank"),       // query smuggling
            format!("{file_link}#frag"),
            link_for(
                "zed",
                &d.join("..").join(d.file_name().unwrap()).join("ok.rs"),
                "",
            ), // `..`
            file_link.replace("ok.rs", "%2E%2E/ok.rs"), // encoded `..`
            file_link.replace("ok.rs", "ok%zz.rs"),     // malformed escape
            file_link.replace("ok.rs", "ok.rs:x"),      // non-numeric suffix
            link_for("zedx", &f, ""),                   // scheme still gated
        ] {
            assert_eq!(editor_link_file(&bad), None, "{bad} must be refused");
        }
    }

    #[test]
    fn editor_link_refuses_a_code_workspace_file() {
        let d = scratch_dir("ws");
        for name in ["evil.code-workspace", "EVIL.Code-Workspace"] {
            let f = d.join(name);
            std::fs::write(&f, "{}").unwrap();
            let link = link_for("vscode", &f, "");
            assert_eq!(editor_link_file(&link), None, "{link} must be refused");
        }
    }

    #[cfg(windows)]
    #[test]
    fn editor_link_refuses_unc_paths_before_touching_them() {
        for bad in [
            "vscode://file//attacker/share/x.rs",
            "zed://file/%5C%5Cattacker%5Cshare%5Cx.rs",
            "cursor://file//%3F/UNC/attacker/share/x.rs",
        ] {
            assert_eq!(editor_link_file(bad), None, "{bad} must be refused");
        }
        assert!(super::is_local_disk_path(std::path::Path::new(r"C:\x\y.rs")));
        assert!(!super::is_local_disk_path(std::path::Path::new(r"\\srv\share\y.rs")));
    }

    #[test]
    fn percent_decode_is_strict() {
        assert_eq!(percent_decode("a%20b%C3%A9").as_deref(), Some("a bé"));
        assert_eq!(percent_decode("%2"), None);
        assert_eq!(percent_decode("%+1"), None);
        assert_eq!(percent_decode("%FF"), None); // not UTF-8
    }

    // ── #3596: "open in editor" links — a STRICT scheme allowlist ─────────────
    #[test]
    fn editor_file_links_are_allowlisted() {
        assert!(is_editor_link("zed://file/Users/me/app/src/main.ts:42:7"));
        assert!(is_editor_link("vscode://file/C:/proj/a%20b.ts:10"));
        assert!(is_editor_link("cursor://file/home/jos%C3%A9/x.rs:1"));
        // Url normalizes the scheme's case; accept either spelling.
        assert!(is_editor_link("ZED://file/tmp/x"));
    }

    #[test]
    fn everything_else_is_not_an_editor_link() {
        for target in [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "ms-settings:privacy",
            "zedx://file/tmp/x", // look-alike prefix
            "xzed://file/tmp/x",
            "zed:file/tmp/x",   // not the //file/ form
            "zed://ssh/host/x", // an editor action, not a file open
            "vscode://ms-vscode.remote/x",
            "cursor:",
            "zed",
            "",
            "https://zed.dev/",
            "mailto:hi@example.com",
        ] {
            assert!(
                !is_editor_link(target),
                "{target} must not pass the allowlist"
            );
        }
    }

    #[test]
    fn editor_links_route_to_the_os_not_a_window() {
        assert_eq!(
            route_new_window("zed://file/Users/me/x.py:3", 7870),
            NewWindow::Editor
        );
        assert_eq!(route_new_window("zedx://file/x", 7870), NewWindow::Ignore);
        assert_eq!(
            route_new_window("ms-settings:privacy", 7870),
            NewWindow::Ignore
        );
    }

    // ── #3316: the triage every shell-built window shares ─────────────────────
    // These ran on the MAIN window only; a link clicked in a second window skipped
    // them and got an unmanaged child webview with no API base, no title bar and no
    // drag-drop fix. The routing is pure so the DECISION is testable without a webview.
    // The WIRING — that every builder actually calls it — is covered by no test and
    // cannot be without a real webview; it is a review-visible one line per window,
    // which is exactly how it went missing in the first place.
    #[test]
    fn our_own_origin_becomes_a_managed_window_at_its_route() {
        assert_eq!(
            route_new_window("http://127.0.0.1:7870/app/agent/roxy-1a2b/", 7870),
            NewWindow::Managed(Some("agent/roxy-1a2b/".into())),
        );
        // The bare app root opens a default console window, not a routed one.
        assert_eq!(
            route_new_window("tauri://localhost/app/", 7870),
            NewWindow::Managed(None)
        );
    }

    #[test]
    fn the_wider_web_goes_to_the_system_browser() {
        // The AppDrawer's doc links, a published link — and the one that matters most,
        // an OAuth provider login: it must never render chrome-less inside the app.
        assert_eq!(
            route_new_window("https://docs.protolabs.studio/", 7870),
            NewWindow::External
        );
        assert_eq!(
            route_new_window("https://github.com/login/oauth/authorize?x=1", 7870),
            NewWindow::External
        );
        // Loopback on somebody else's port is somebody else's server.
        assert_eq!(
            route_new_window("http://127.0.0.1:5173/", 7870),
            NewWindow::External
        );
    }

    #[test]
    fn a_scheme_we_dont_serve_is_dropped_rather_than_opened() {
        assert_eq!(
            route_new_window("mailto:hi@example.com", 7870),
            NewWindow::Ignore
        );
        assert_eq!(
            route_new_window("javascript:alert(1)", 7870),
            NewWindow::Ignore
        );
    }

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

#[cfg(test)]
mod sidecar_log_tests {
    use super::sidecar_line_level;
    use log::Level::{Debug, Info};

    // #3504: lines copied from the desktop log of 2026-09-13, the file that had rotated
    // everything but these away within a minute.
    #[test]
    fn uvicorn_access_lines_are_demoted() {
        for line in [
            r#"INFO:     127.0.0.1:54910 - "GET /api/fleet HTTP/1.1" 200 OK"#,
            r#"INFO:     127.0.0.1:54910 - "GET /agents/merchantAgent-6604/api/plugins/notes/note HTTP/1.1" 200 OK"#,
            r#"INFO:     127.0.0.1:54374 - "GET /agents/merchantAgent-6604/api/events?token=eyJz&since=5 HTTP/1.1" 200 OK"#,
            r#"INFO:     127.0.0.1:61022 - "POST /a2a HTTP/1.1" 500 Internal Server Error"#,
            r#"INFO:     ::1:61022 - "GET /api/fleet HTTP/1.1" 404 Not Found"#,
        ] {
            assert_eq!(sidecar_line_level(line), Debug, "{line}");
        }
    }

    #[test]
    fn httpx_request_lines_are_demoted() {
        for line in [
            r#"2026-09-13 21:52:46,403 INFO httpx HTTP Request: GET http://127.0.0.1:7881/api/plugins/notes/note "HTTP/1.1 200 OK""#,
            r#"2026-09-13 21:57:22,217 INFO httpx HTTP Request: GET http://127.0.0.1:7903/.well-known/agent-card.json "HTTP/1.0 200 OK""#,
            r#"2026-09-13 21:57:22,217 INFO httpx HTTP Request: POST http://127.0.0.1:7881/a2a "HTTP/1.1 502 Bad Gateway""#,
        ] {
            assert_eq!(sidecar_line_level(line), Debug, "{line}");
        }
    }

    #[test]
    fn boot_and_lifecycle_lines_stay_in_the_file() {
        for line in [
            "INFO:     Started server process [69481]",
            "INFO:     Waiting for application startup.",
            "INFO:     Uvicorn running on http://127.0.0.1:7870 (Press CTRL+C to quit)",
            "INFO:     Shutting down",
            "INFO:     Finished server process [69481]",
            "2026-09-13 21:52:40,001 INFO server.fleet [fleet] spawned member merchantAgent on :7881",
            "2026-09-13 21:52:40,001 INFO server [watchdog] launcher pid 4242 gone — exiting sidecar",
        ] {
            assert_eq!(sidecar_line_level(line), Info, "{line}");
        }
    }

    #[test]
    fn warnings_and_errors_that_mention_a_request_are_never_demoted() {
        for line in [
            r#"2026-09-13 21:52:46,403 WARNING httpx HTTP Request: GET http://127.0.0.1:7881/api/x "HTTP/1.1 200 OK""#,
            r#"2026-09-13 21:52:46,403 ERROR server.fleet proxy GET http://127.0.0.1:7881/api/x failed: "HTTP/1.1 502 Bad Gateway""#,
            r#"WARNING:  127.0.0.1:54910 - "GET /api/fleet HTTP/1.1" 200 OK"#,
            "WARNING:  Invalid HTTP request received.",
            "ERROR:    Exception in ASGI application",
            // Another logger quoting httpx's message is not httpx's request line.
            r#"2026-09-13 21:52:46,403 INFO server.proxy HTTP Request: GET http://127.0.0.1:7881/ "HTTP/1.1 200 OK""#,
        ] {
            assert_eq!(sidecar_line_level(line), Info, "{line}");
        }
    }

    #[test]
    fn every_line_of_a_traceback_stays_in_the_file() {
        for line in [
            "Traceback (most recent call last):",
            r#"  File "httpx/_transports/default.py", line 101, in map_httpcore_exceptions"#,
            "    yield",
            "httpx.ConnectError: [Errno 61] Connection refused",
            "",
        ] {
            assert_eq!(sidecar_line_level(line), Info, "{line:?}");
        }
    }
}

#[cfg(test)]
mod port_choice_tests {
    use super::{
        choose_port_with, port_is_free, wait_until_free, DEFAULT_PORT, PORT_RETRY_WINDOW,
    };
    use std::cell::Cell;
    use std::net::TcpListener;
    use std::time::Duration;

    const FALLBACK: u16 = 58614;

    /// A fake clock: `sleep` advances it, and the probe reads it.
    struct Clock(Cell<Duration>);

    impl Clock {
        fn new() -> Self {
            Clock(Cell::new(Duration::ZERO))
        }
        fn sleep(&self, d: Duration) {
            self.0.set(self.0.get() + d);
        }
        fn now(&self) -> Duration {
            self.0.get()
        }
    }

    #[test]
    fn a_free_port_is_taken_at_once() {
        let clock = Clock::new();
        let port = choose_port_with(|| true, |d| clock.sleep(d), || panic!("no fallback"));
        assert_eq!(port, DEFAULT_PORT);
        assert_eq!(clock.now(), Duration::ZERO);
    }

    // #3503: the relaunch raced its own exiting sidecar, found 7870 held, and fell back
    // to 58614 for the whole session.
    #[test]
    fn a_port_released_inside_the_window_is_still_7870() {
        let clock = Clock::new();
        let released_at = Duration::from_millis(1500);
        let port = choose_port_with(
            || clock.now() >= released_at,
            |d| clock.sleep(d),
            || panic!("no fallback while the port frees inside the window"),
        );
        assert_eq!(port, DEFAULT_PORT);
        assert_eq!(clock.now(), released_at);
    }

    #[test]
    fn a_release_at_the_very_end_of_the_window_still_counts() {
        let clock = Clock::new();
        let port = choose_port_with(
            || clock.now() >= PORT_RETRY_WINDOW,
            |d| clock.sleep(d),
            || panic!("the last probe comes after the last sleep"),
        );
        assert_eq!(port, DEFAULT_PORT);
    }

    // #1668 still holds for a listener that stays: fall back, just after the window.
    #[test]
    fn a_port_held_throughout_falls_back_after_the_window() {
        let clock = Clock::new();
        let fallbacks = Cell::new(0);
        let port = choose_port_with(
            || false,
            |d| clock.sleep(d),
            || {
                fallbacks.set(fallbacks.get() + 1);
                Some(FALLBACK)
            },
        );
        assert_eq!(port, FALLBACK);
        assert_eq!(fallbacks.get(), 1);
        assert_eq!(clock.now(), PORT_RETRY_WINDOW, "waits the window, and no longer");
    }

    #[test]
    fn a_failed_fallback_keeps_the_old_default() {
        let clock = Clock::new();
        assert_eq!(choose_port_with(|| false, |d| clock.sleep(d), || None), DEFAULT_PORT);
    }

    #[test]
    fn the_wait_is_bounded_even_with_a_zero_interval() {
        let clock = Clock::new();
        let window = Duration::from_secs(3);
        let waited = wait_until_free(|| false, |d| clock.sleep(d), window, Duration::ZERO);
        assert_eq!(waited, None);
        assert_eq!(clock.now(), window);
    }

    #[test]
    fn the_wait_reports_how_long_the_port_took() {
        let clock = Clock::new();
        let waited = wait_until_free(
            || clock.now() >= Duration::from_millis(500),
            |d| clock.sleep(d),
            Duration::from_secs(3),
            Duration::from_millis(50),
        );
        assert_eq!(waited, Some(Duration::from_millis(500)));
    }

    // The real probe, against a real socket: held while a listener owns it, free once
    // that listener is gone. (The same probe decides when a stopped sidecar let go.)
    #[test]
    fn the_probe_sees_a_real_listener_come_and_go() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(!port_is_free(port));
        drop(listener);
        assert!(port_is_free(port));
    }
}
