use tauri::{AppHandle, Manager, Runtime, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

use crate::WakeSignal;

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
pub(crate) fn open_chat_window<R: Runtime>(
    app: &AppHandle<R>,
    path: Option<String>,
) -> Result<(), String> {
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
pub(crate) fn new_window<R: Runtime>(
    app: AppHandle<R>,
    path: Option<String>,
) -> Result<(), String> {
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
pub(crate) fn serve_new_window<R: Runtime>(
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
pub(crate) fn serve_navigation<R: Runtime>(app: &AppHandle<R>, target: &str) -> bool {
    if is_editor_link(target) {
        open_editor_link(app, target);
        return false;
    }
    true
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
