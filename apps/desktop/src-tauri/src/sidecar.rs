use std::net::TcpListener;
use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

/// The web client's zero-handoff fallback port (apps/web/src/lib/api.ts) —
/// preferred so the no-handoff path still lands on the live server.
pub(crate) const DEFAULT_PORT: u16 = 7870;

/// How long a launch keeps re-probing a held 7870 before the #1668 fallback (#3503).
/// Long enough to outlast the path that frees the port when the previous shell could
/// not stop its sidecar itself (a crash, Windows, a stop that ran out of time): the
/// orphaned server's parent-death watchdog polls every 2 s, then reaps its trees with
/// a 1 s grace before it exits (server/__init__.py). A listener that stays, which is
/// what #1668 is for, costs a launch this long before it falls back as it always did.
pub(crate) const PORT_RETRY_WINDOW: Duration = Duration::from_secs(4);
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

pub(crate) fn choose_port() -> u16 {
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

/// Holds the running sidecar so it can be killed when the app exits.
#[derive(Default)]
pub(crate) struct SidecarProcess(Mutex<Option<CommandChild>>);

/// Set when the app is tearing down — a sidecar `Terminated` event during shutdown
/// is the clean kill, not a crash to alert on.
static QUITTING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

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
pub(crate) fn spawn_sidecar<R: Runtime>(app: &AppHandle<R>, port: u16) {
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
pub(crate) fn stop_sidecar<R: Runtime>(app: &AppHandle<R>) {
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
        if let Some(port) = app.try_state::<crate::WakeSignal>().map(|s| s.port) {
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
