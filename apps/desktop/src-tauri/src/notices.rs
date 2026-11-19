//! Native notice receiver (ADR 0135 W2, "Transport by device class": desktop app).
//!
//! While the app runs — window open or closed to the tray — this reads the local hub's
//! operator event stream (`GET /api/events`, the same one the console holds) with the
//! shell's own credential and shows every `notice` event as a native notification. It is
//! deliberately not a second channel (D8): no new hub route, no push binding, and nothing
//! that depends on WKWebView/WebView2 supporting Web Push or on webview timers staying
//! alive while the window is hidden.
//!
//! * **Replay.** The stream's `id:` is the bus seq. A reconnect sends it back as
//!   `Last-Event-ID`, so a notice raised while the connection was down is replayed.
//! * **Once per notice.** Replay can re-deliver, so presentation is keyed on the notice id.
//! * **Expiry.** A notice past its `expires_at` (UTC Unix seconds) is skipped: replaying
//!   an hour-old "needs a decision" after a long sleep helps nobody.
//! * **Generic text.** The banner never carries the notice's own text; the operator opens
//!   the console to read it.
//! * **Presentation ownership.** In the desktop app this receiver owns notice presentation;
//!   `apps/web/src/lib/notify.ts` stands down for the alerts that mirror a notice.
//!
//! Quit ends it (`stop`): the hub cannot wake a terminated shell in this design.

use std::collections::{HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::plugin::PermissionState;
use tauri::{AppHandle, Emitter, Manager, Runtime};
use tauri_plugin_notification::NotificationExt;

/// Notice ids remembered for de-duplication. A replay is bounded by the hub's change-log
/// retention and a notice lives at most a day, so this only has to outlast that overlap.
const SEEN_CAP: usize = 1024;
/// One SSE line may not grow past this. The hub caps a payload at 64 KiB; anything larger
/// is not ours to buffer.
const MAX_LINE_BYTES: usize = 1024 * 1024;
/// The hub sends a keepalive comment every 15 s, so a read this quiet means the connection
/// is dead (a laptop that slept) and it is time to reconnect and replay.
const READ_TIMEOUT: Duration = Duration::from_secs(45);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
/// How long after a notice is shown an app re-activation counts as "the operator clicked
/// that notification" (see `on_reopen`). Never longer than the notice's own lifetime.
const CLICK_WINDOW_SECS: f64 = 10.0 * 60.0;
const TITLE: &str = "protoAgent";

/// One `notice` bus event, as published by `notify_chat_surfaces` (W2b1).
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Notice {
    pub id: String,
    pub kind: String,
    pub link: String,
    /// UTC Unix seconds; 0 means the event carried no expiry.
    pub expires_at: f64,
}

impl Notice {
    fn expired(&self, now: f64) -> bool {
        self.expires_at > 0.0 && now >= self.expires_at
    }
}

/// The banner text for a notice kind. Generic on purpose (ADR 0135 W2): the notice's own
/// text can quote a chat, and a banner is readable on a locked screen.
fn body_for(kind: &str) -> &'static str {
    match kind {
        "question" => "An agent needs a decision from you.",
        "review" => "There is a result ready to review.",
        _ => "There is a new notice in the console.",
    }
}

/// A stable native notification id for a notice id (FNV-1a, folded to a non-negative
/// i32). Stable across runs and platforms — unlike std's `DefaultHasher` — so the same
/// notice always maps to the same OS notification.
fn native_id(notice_id: &str) -> i32 {
    let mut hash: u32 = 0x811c_9dc5;
    for byte in notice_id.as_bytes() {
        hash ^= u32::from(*byte);
        hash = hash.wrapping_mul(0x0100_0193);
    }
    (hash & 0x7fff_ffff) as i32
}

fn now_unix() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ── SSE framing ─────────────────────────────────────────────────────────────

#[derive(Debug, PartialEq)]
struct SseFrame {
    id: Option<u64>,
    data: String,
}

/// Incremental parser for the hub's SSE text: `id:` / `data:` lines ended by a blank
/// line, `:` comments (`: connected`, `: keepalive`) ignored. Fed decoded chunks of any
/// size — a frame split across chunks comes out whole.
#[derive(Default)]
struct SseParser {
    line: String,
    /// The current line outgrew `MAX_LINE_BYTES`; drop it up to its newline.
    overflow: bool,
    id: Option<u64>,
    data: Vec<String>,
}

impl SseParser {
    fn feed(&mut self, text: &str) -> Vec<SseFrame> {
        let mut out = Vec::new();
        for ch in text.chars() {
            if ch != '\n' {
                if self.line.len() >= MAX_LINE_BYTES {
                    self.overflow = true;
                    self.line.clear();
                }
                if !self.overflow {
                    self.line.push(ch);
                }
                continue;
            }
            let line = std::mem::take(&mut self.line);
            if std::mem::take(&mut self.overflow) {
                // An oversized line poisons its whole frame, not just itself.
                self.data.clear();
                self.id = None;
                continue;
            }
            self.end_line(line.trim_end_matches('\r'), &mut out);
        }
        out
    }

    fn end_line(&mut self, line: &str, out: &mut Vec<SseFrame>) {
        if line.is_empty() {
            let id = self.id.take();
            if !self.data.is_empty() {
                out.push(SseFrame { id, data: self.data.join("\n") });
                self.data.clear();
            }
            return;
        }
        if line.starts_with(':') {
            return;
        }
        let (field, value) = match line.split_once(':') {
            Some((f, v)) => (f, v.strip_prefix(' ').unwrap_or(v)),
            None => (line, ""),
        };
        match field {
            "id" => self.id = value.trim().parse().ok(),
            "data" => self.data.push(value.to_string()),
            _ => {}
        }
    }
}

// ── Cursor + de-duplication ─────────────────────────────────────────────────

/// What survives a reconnect: the replay cursor and the notice ids already shown.
#[derive(Default)]
struct Tracker {
    /// Seq of the last frame received — sent back as `Last-Event-ID`.
    last_seq: Option<u64>,
    seen_order: VecDeque<String>,
    seen: HashSet<String>,
}

impl Tracker {
    /// Advance the cursor past `frame` and return the notice it carries, if it is one this
    /// device should show now: a `notice` topic, an id not shown before, not expired.
    fn accept(&mut self, frame: &SseFrame, now: f64) -> Option<Notice> {
        let value: Option<serde_json::Value> = serde_json::from_str(&frame.data).ok();
        // The cursor follows every frame, not just notices, and takes the hub's value as
        // is (never a max): after `resync_required` or a hub whose log was wiped the seq
        // legitimately moves backwards, and a stale high cursor would keep re-asking.
        let seq = frame
            .id
            .or_else(|| value.as_ref().and_then(|v| v.get("seq")).and_then(|s| s.as_u64()));
        if seq.is_some() {
            self.last_seq = seq;
        }
        let value = value?;
        if value.get("topic")?.as_str()? != "notice" {
            return None;
        }
        let data = value.get("data")?;
        let id = data.get("notice_id")?.as_str()?.trim();
        if id.is_empty() {
            return None; // no identity → no way to keep it to one banner
        }
        let text = |key: &str| data.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let notice = Notice {
            id: id.to_string(),
            kind: text("kind"),
            link: text("link"),
            expires_at: data.get("expires_at").and_then(|v| v.as_f64()).unwrap_or(0.0),
        };
        if notice.expired(now) || !self.claim(&notice.id) {
            return None;
        }
        Some(notice)
    }

    /// Record `id` as shown; false when it already was.
    fn claim(&mut self, id: &str) -> bool {
        if !self.seen.insert(id.to_string()) {
            return false;
        }
        self.seen_order.push_back(id.to_string());
        while self.seen_order.len() > SEEN_CAP {
            if let Some(old) = self.seen_order.pop_front() {
                self.seen.remove(&old);
            }
        }
        true
    }
}

// ── Presentation ────────────────────────────────────────────────────────────

/// Where an accepted notice goes. The stream loop is generic over this so tests can
/// collect notices without an OS notification center.
pub(crate) trait Presenter: Send {
    fn present(&mut self, notice: &Notice);
}

/// The OS notification surface, reduced to what the receiver needs (and a test can fake).
trait NotificationBackend: Send {
    fn permission(&self) -> Result<PermissionState, String>;
    fn request_permission(&self) -> Result<PermissionState, String>;
    fn show(&self, id: i32, title: &str, body: &str) -> Result<(), String>;
}

struct TauriBackend<R: Runtime> {
    app: AppHandle<R>,
}

impl<R: Runtime> NotificationBackend for TauriBackend<R> {
    fn permission(&self) -> Result<PermissionState, String> {
        self.app.notification().permission_state().map_err(|e| e.to_string())
    }

    fn request_permission(&self) -> Result<PermissionState, String> {
        self.app.notification().request_permission().map_err(|e| e.to_string())
    }

    fn show(&self, id: i32, title: &str, body: &str) -> Result<(), String> {
        self.app
            .notification()
            .builder()
            .id(id)
            .title(title)
            .body(body)
            .show()
            .map_err(|e| e.to_string())
    }
}

/// The console place a clicked notice opens, valid until `valid_until` (Unix seconds).
#[derive(Clone, Debug, PartialEq)]
struct ClickTarget {
    /// The chat session the notice's link names (`…/app/?session=<id>`), if any. A link
    /// to the console root has none: the click just raises the window.
    session: Option<String>,
    valid_until: f64,
}

type ClickSlot = Arc<Mutex<Option<ClickTarget>>>;

/// The chat session a notice link points at. Links are `<origin>/app/?session=<id>` (a
/// decision, a scheduled result) or `<origin>/app/` / empty (fleet health, spend cap); the
/// origin is whatever the hub advertises and is ignored — the shell opens its own console.
fn session_of(link: &str) -> Option<String> {
    let base = tauri::Url::parse("http://console.invalid/").ok()?;
    let url = base.join(link.trim()).ok()?;
    let (_, session) = url.query_pairs().find(|(key, _)| key == "session")?;
    let session = session.trim();
    (!session.is_empty() && session.len() <= 256).then(|| session.to_string())
}

/// Take the pending click target if it is still valid at `now`.
fn take_click_target(slot: &Mutex<Option<ClickTarget>>, now: f64) -> Option<ClickTarget> {
    let target = slot.lock().unwrap_or_else(|e| e.into_inner()).take()?;
    (now < target.valid_until).then_some(target)
}

/// Shows accepted notices through the OS, and remembers the latest one's console route for
/// a click. A refused permission or a failing notification center is logged ONCE and the
/// receiver keeps running: the next notice tries again, quietly.
struct NativePresenter<B: NotificationBackend> {
    backend: B,
    click: ClickSlot,
    /// Whether the "cannot show notifications" line has been logged for the current outage.
    warned: bool,
}

impl<B: NotificationBackend> NativePresenter<B> {
    fn new(backend: B, click: ClickSlot) -> Self {
        Self { backend, click, warned: false }
    }

    fn allowed(&self) -> Result<(), String> {
        let state = match self.backend.permission()? {
            PermissionState::Granted => return Ok(()),
            PermissionState::Denied => PermissionState::Denied,
            // Not decided yet: ask once per notice until the OS has an answer.
            _ => self.backend.request_permission()?,
        };
        match state {
            PermissionState::Granted => Ok(()),
            other => Err(format!("notification permission is {other}")),
        }
    }

    fn warn_once(&mut self, reason: &str) {
        if !self.warned {
            self.warned = true;
            log::warn!("desktop: notices cannot be shown as native notifications — {reason}");
        }
    }
}

impl<B: NotificationBackend> Presenter for NativePresenter<B> {
    fn present(&mut self, notice: &Notice) {
        let shown = self
            .allowed()
            .and_then(|()| self.backend.show(native_id(&notice.id), TITLE, body_for(&notice.kind)));
        if let Err(reason) = shown {
            self.warn_once(&reason);
            return;
        }
        self.warned = false;
        let now = now_unix();
        let mut valid_until = now + CLICK_WINDOW_SECS;
        if notice.expires_at > 0.0 {
            valid_until = valid_until.min(notice.expires_at);
        }
        *self.click.lock().unwrap_or_else(|e| e.into_inner()) =
            Some(ClickTarget { session: session_of(&notice.link), valid_until });
    }
}

// ── The stream loop ─────────────────────────────────────────────────────────

/// The hub stream to read: its URL and the credential to present, resolved afresh on
/// every connect (an operator can configure a token while the app is running).
struct Source {
    url: String,
    token: Box<dyn Fn() -> Option<String> + Send + Sync>,
}

#[derive(Clone, Copy)]
struct Backoff {
    initial: Duration,
    max: Duration,
}

impl Default for Backoff {
    fn default() -> Self {
        // Matches the console's own event-stream reconnect (lib/events.ts): 1 s doubling
        // to 30 s, so a booting or stopped sidecar is not hammered.
        Self { initial: Duration::from_secs(1), max: Duration::from_secs(30) }
    }
}

/// One connection: open the stream (resuming from the tracker's cursor), present notices
/// until it ends. Returns whether the stream was opened, and why it stopped.
async fn stream_once<P: Presenter>(
    client: &reqwest::Client,
    source: &Source,
    tracker: &mut Tracker,
    presenter: &mut P,
    stop: &AtomicBool,
) -> (bool, String) {
    use futures_util::StreamExt;

    // Accept is load-bearing through a proxy and harmless on loopback; Last-Event-ID is
    // the replay cursor the route reads (operator_api `/api/events`).
    let mut req = client.get(&source.url).header("Accept", "text/event-stream");
    if let Some(token) = (source.token)().filter(|t| !t.is_empty()) {
        req = req.header("Authorization", format!("Bearer {token}"));
    }
    if let Some(seq) = tracker.last_seq {
        req = req.header("Last-Event-ID", seq.to_string());
    }
    let resp = match req.send().await {
        Ok(resp) => resp,
        Err(e) => return (false, e.to_string()),
    };
    if !resp.status().is_success() {
        return (false, format!("HTTP {}", resp.status().as_u16()));
    }
    let mut stream = resp.bytes_stream();
    let mut parser = SseParser::default();
    let mut pending: Vec<u8> = Vec::new();
    while let Some(chunk) = stream.next().await {
        if stop.load(Ordering::Relaxed) {
            return (true, "stopped".into());
        }
        let bytes = match chunk {
            Ok(bytes) => bytes,
            Err(e) => return (true, e.to_string()),
        };
        pending.extend_from_slice(&bytes);
        // Same split-character care as chat_stream: decode only complete UTF-8.
        let text = crate::take_utf8(&mut pending);
        for frame in parser.feed(&text) {
            if let Some(notice) = tracker.accept(&frame, now_unix()) {
                presenter.present(&notice);
            }
        }
    }
    (true, "stream ended".into())
}

/// Read the hub's event stream until `stop`, reconnecting with backoff. Cursor and
/// shown-ids live here, across connections, so a reconnect replays the gap without
/// repeating a banner.
async fn run<P: Presenter>(source: Source, mut presenter: P, stop: Arc<AtomicBool>, backoff: Backoff) {
    let client = reqwest::Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .read_timeout(READ_TIMEOUT)
        .build()
        .unwrap_or_default();
    let mut tracker = Tracker::default();
    let mut delay = backoff.initial;
    // The last failure logged, so a sidecar that stays down logs one line, not one per try.
    let mut last_reason = String::new();
    while !stop.load(Ordering::Relaxed) {
        let (opened, reason) = stream_once(&client, &source, &mut tracker, &mut presenter, &stop).await;
        if stop.load(Ordering::Relaxed) {
            break;
        }
        if opened {
            delay = backoff.initial;
            last_reason.clear();
            log::debug!("desktop: notice stream closed ({reason}); reconnecting");
        } else if reason != last_reason {
            log::info!("desktop: notice stream not connected ({reason}); retrying with backoff");
            last_reason = reason;
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(backoff.max);
    }
}

// ── Shell wiring ────────────────────────────────────────────────────────────

/// Managed handle on the running receiver.
pub(crate) struct NoticeReceiver {
    stop: Arc<AtomicBool>,
    task: Mutex<Option<tauri::async_runtime::JoinHandle<()>>>,
    click: ClickSlot,
}

/// Start the receiver against the sidecar on `port`. Called once from setup; it runs for
/// the app's lifetime, independent of any window or webview.
pub(crate) fn start<R: Runtime>(app: &AppHandle<R>, port: u16) {
    let stop = Arc::new(AtomicBool::new(false));
    let click = ClickSlot::default();
    let source = Source {
        url: format!("http://127.0.0.1:{port}/api/events"),
        token: Box::new({
            let app = app.clone();
            move || crate::resolve_auth_token(&app)
        }),
    };
    let presenter = NativePresenter::new(TauriBackend { app: app.clone() }, click.clone());
    let task = tauri::async_runtime::spawn(run(source, presenter, stop.clone(), Backoff::default()));
    app.manage(NoticeReceiver { stop, task: Mutex::new(Some(task)), click });
}

/// Quit: end the receiver. Nothing is shown after this.
pub(crate) fn stop<R: Runtime>(app: &AppHandle<R>) {
    let Some(state) = app.try_state::<NoticeReceiver>() else {
        return;
    };
    state.stop.store(true, Ordering::Relaxed);
    let task = state.task.lock().unwrap_or_else(|e| e.into_inner()).take();
    if let Some(task) = task {
        task.abort();
    }
}

/// The app was re-activated (macOS `applicationShouldHandleReopen`). The notification
/// plugin has no click callback on desktop; what a click on one of our banners does is
/// activate the app, and for a menu-bar app that is what this event reports. So: raise the
/// console, and when a notice was shown recently, land on the chat it points at — through
/// the same `palette:navigate` intent the launcher forwards, which the main window
/// validates against its own store (an unknown session is ignored there).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn on_reopen<R: Runtime>(app: &AppHandle<R>, has_visible_windows: bool) {
    let target = app
        .try_state::<NoticeReceiver>()
        .and_then(|state| take_click_target(&state.click, now_unix()));
    match target {
        Some(target) => {
            crate::show_main_window(app);
            if let Some(session) = target.session {
                let intent = serde_json::json!({ "kind": "chat", "sessionId": session });
                let _ = app.emit("palette:navigate", intent);
            }
        }
        // A banner clicked after its window still has to bring the console back.
        None if !has_visible_windows => crate::show_main_window(app),
        None => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn notice_frame(seq: u64, id: &str, kind: &str, link: &str, expires_at: f64) -> String {
        let body = serde_json::json!({
            "topic": "notice",
            "seq": seq,
            "ts": 1.0,
            "data": {
                "kind": kind, "importance": "normal", "text": "secret text", "link": link,
                "notice_id": id, "created_at": 1.0, "expires_at": expires_at,
            },
        });
        format!("id: {seq}\ndata: {body}\n\n")
    }

    fn frames(text: &str) -> Vec<SseFrame> {
        SseParser::default().feed(text)
    }

    #[test]
    fn parser_reads_frames_and_ignores_comments() {
        let text = ": connected\n\nid: 7\ndata: {\"topic\":\"a\"}\n\n: keepalive\n\ndata: x\r\ndata: y\r\n\r\n";
        assert_eq!(
            frames(text),
            vec![
                SseFrame { id: Some(7), data: "{\"topic\":\"a\"}".into() },
                SseFrame { id: None, data: "x\ny".into() },
            ]
        );
    }

    #[test]
    fn parser_joins_a_frame_split_across_chunks() {
        let text = notice_frame(3, "n1", "notify", "", 0.0);
        for cut in 0..=text.len() {
            let mut parser = SseParser::default();
            let mut got = parser.feed(&text[..cut]);
            got.extend(parser.feed(&text[cut..]));
            assert_eq!(got.len(), 1, "split at {cut}");
            assert_eq!(got[0].id, Some(3));
        }
    }

    #[test]
    fn parser_drops_an_oversized_line_and_recovers() {
        let mut parser = SseParser::default();
        let huge = format!("id: 1\ndata: {}\n\n", "x".repeat(MAX_LINE_BYTES + 10));
        assert!(parser.feed(&huge).is_empty());
        assert_eq!(parser.feed("id: 2\ndata: ok\n\n"), vec![SseFrame { id: Some(2), data: "ok".into() }]);
    }

    #[test]
    fn a_notice_is_accepted_once_and_a_replayed_duplicate_is_not() {
        let mut tracker = Tracker::default();
        let first = &frames(&notice_frame(5, "abc", "question", "/app/?session=s1", 2000.0))[0];
        let notice = tracker.accept(first, 1000.0).expect("a fresh notice is shown");
        assert_eq!(notice.id, "abc");
        assert_eq!(notice.kind, "question");
        assert_eq!(tracker.last_seq, Some(5));
        // The same frame replayed, and the same notice id under a later seq.
        assert_eq!(tracker.accept(first, 1000.0), None);
        let again = &frames(&notice_frame(9, "abc", "question", "/app/?session=s1", 2000.0))[0];
        assert_eq!(tracker.accept(again, 1000.0), None);
        assert_eq!(tracker.last_seq, Some(9));
    }

    #[test]
    fn an_expired_notice_is_not_accepted() {
        let mut tracker = Tracker::default();
        let stale = &frames(&notice_frame(1, "old", "notify", "", 999.0))[0];
        assert_eq!(tracker.accept(stale, 1000.0), None);
        let at_edge = &frames(&notice_frame(2, "edge", "notify", "", 1000.0))[0];
        assert_eq!(tracker.accept(at_edge, 1000.0), None);
        let live = &frames(&notice_frame(3, "new", "notify", "", 1000.5))[0];
        assert!(tracker.accept(live, 1000.0).is_some());
        // No expiry on the event means it does not expire here.
        let open_ended = &frames(&notice_frame(4, "forever", "notify", "", 0.0))[0];
        assert!(tracker.accept(open_ended, 1000.0).is_some());
    }

    #[test]
    fn other_topics_and_malformed_frames_only_move_the_cursor() {
        let mut tracker = Tracker::default();
        let other = SseFrame { id: Some(4), data: r#"{"topic":"scheduler.completed","data":{}}"#.into() };
        assert_eq!(tracker.accept(&other, 1.0), None);
        assert_eq!(tracker.last_seq, Some(4));
        let garbage = SseFrame { id: Some(6), data: "not json".into() };
        assert_eq!(tracker.accept(&garbage, 1.0), None);
        assert_eq!(tracker.last_seq, Some(6));
        let no_id = SseFrame { id: Some(7), data: r#"{"topic":"notice","data":{"notice_id":" "}}"#.into() };
        assert_eq!(tracker.accept(&no_id, 1.0), None);
        // resync_required carries the hub's current seq, which may be BELOW the stale cursor.
        let resync = SseFrame { id: Some(2), data: r#"{"topic":"resync_required","data":{}}"#.into() };
        assert_eq!(tracker.accept(&resync, 1.0), None);
        assert_eq!(tracker.last_seq, Some(2));
        // A frame without an `id:` line still yields its payload seq.
        let payload_seq = SseFrame { id: None, data: r#"{"topic":"x","seq":11,"data":{}}"#.into() };
        tracker.accept(&payload_seq, 1.0);
        assert_eq!(tracker.last_seq, Some(11));
    }

    #[test]
    fn the_seen_set_is_bounded() {
        let mut tracker = Tracker::default();
        for i in 0..(SEEN_CAP + 10) {
            assert!(tracker.claim(&format!("n{i}")));
        }
        assert_eq!(tracker.seen.len(), SEEN_CAP);
        assert_eq!(tracker.seen_order.len(), SEEN_CAP);
        assert!(!tracker.claim(&format!("n{}", SEEN_CAP + 9)));
    }

    #[test]
    fn native_id_is_stable_and_text_is_generic() {
        assert_eq!(native_id("abc"), native_id("abc"));
        assert_ne!(native_id("abc"), native_id("abd"));
        assert_eq!(native_id(""), 0x011c_9dc5); // FNV-1a offset basis, folded
        assert!(native_id("ffffffffffffffffffffffffffffffff") >= 0);
        for kind in ["notify", "question", "review", "something-new"] {
            assert!(!body_for(kind).is_empty());
        }
        assert_ne!(body_for("question"), body_for("review"));
    }

    #[test]
    fn session_of_reads_the_console_link() {
        assert_eq!(session_of("http://127.0.0.1:7870/app/?session=chat-1").as_deref(), Some("chat-1"));
        assert_eq!(session_of("https://hub.example/app/?session=a%20b&x=1").as_deref(), Some("a b"));
        assert_eq!(session_of("/app/?session=rel").as_deref(), Some("rel"));
        assert_eq!(session_of("http://127.0.0.1:7870/app/"), None);
        assert_eq!(session_of("/app/?session="), None);
        assert_eq!(session_of(""), None);
    }

    #[test]
    fn a_click_target_is_taken_once_and_only_while_valid() {
        let slot = Mutex::new(Some(ClickTarget { session: Some("s".into()), valid_until: 100.0 }));
        assert_eq!(take_click_target(&slot, 50.0).and_then(|t| t.session).as_deref(), Some("s"));
        assert_eq!(take_click_target(&slot, 50.0), None);
        *slot.lock().unwrap() = Some(ClickTarget { session: None, valid_until: 100.0 });
        assert_eq!(take_click_target(&slot, 100.0), None);
    }

    // ── Presenter: permission + show failures ───────────────────────────────

    #[derive(Clone)]
    struct FakeBackend {
        permission: PermissionState,
        after_request: PermissionState,
        fail_show: bool,
        shown: Arc<Mutex<Vec<(i32, String, String)>>>,
        requests: Arc<Mutex<u32>>,
    }

    impl FakeBackend {
        fn new(permission: PermissionState) -> Self {
            Self {
                permission,
                after_request: permission,
                fail_show: false,
                shown: Arc::default(),
                requests: Arc::default(),
            }
        }
    }

    impl NotificationBackend for FakeBackend {
        fn permission(&self) -> Result<PermissionState, String> {
            Ok(self.permission)
        }
        fn request_permission(&self) -> Result<PermissionState, String> {
            *self.requests.lock().unwrap() += 1;
            Ok(self.after_request)
        }
        fn show(&self, id: i32, title: &str, body: &str) -> Result<(), String> {
            if self.fail_show {
                return Err("notification center unavailable".into());
            }
            self.shown.lock().unwrap().push((id, title.into(), body.into()));
            Ok(())
        }
    }

    fn notice(id: &str, link: &str) -> Notice {
        Notice { id: id.into(), kind: "question".into(), link: link.into(), expires_at: 0.0 }
    }

    #[test]
    fn a_granted_notice_shows_generic_text_and_records_its_route() {
        let backend = FakeBackend::new(PermissionState::Granted);
        let click = ClickSlot::default();
        let mut presenter = NativePresenter::new(backend.clone(), click.clone());
        presenter.present(&notice("n1", "http://127.0.0.1:7870/app/?session=chat-9"));
        let shown = backend.shown.lock().unwrap().clone();
        assert_eq!(shown, vec![(native_id("n1"), TITLE.to_string(), body_for("question").to_string())]);
        assert!(!shown[0].2.contains("secret"));
        let target = take_click_target(&click, now_unix()).expect("a click target is pending");
        assert_eq!(target.session.as_deref(), Some("chat-9"));
    }

    #[test]
    fn a_click_target_never_outlives_the_notice() {
        let click = ClickSlot::default();
        let mut presenter = NativePresenter::new(FakeBackend::new(PermissionState::Granted), click.clone());
        let soon = now_unix() + 5.0;
        presenter.present(&Notice { expires_at: soon, ..notice("n1", "/app/") });
        let target = click.lock().unwrap().clone().unwrap();
        assert_eq!(target.valid_until, soon);
        assert_eq!(target.session, None);
    }

    #[test]
    fn denied_permission_shows_nothing_warns_once_and_keeps_running() {
        let backend = FakeBackend::new(PermissionState::Denied);
        let click = ClickSlot::default();
        let mut presenter = NativePresenter::new(backend.clone(), click.clone());
        presenter.present(&notice("n1", "/app/?session=a"));
        assert!(presenter.warned);
        presenter.present(&notice("n2", "/app/?session=b"));
        assert!(presenter.warned);
        assert!(backend.shown.lock().unwrap().is_empty());
        assert_eq!(*backend.requests.lock().unwrap(), 0, "a refusal is not re-prompted");
        assert!(click.lock().unwrap().is_none(), "nothing shown → nothing to click");
    }

    #[test]
    fn an_undecided_permission_is_requested_then_honoured() {
        let mut backend = FakeBackend::new(PermissionState::Prompt);
        backend.after_request = PermissionState::Granted;
        let mut presenter = NativePresenter::new(backend.clone(), ClickSlot::default());
        presenter.present(&notice("n1", ""));
        assert_eq!(*backend.requests.lock().unwrap(), 1);
        assert_eq!(backend.shown.lock().unwrap().len(), 1);

        let refused = FakeBackend::new(PermissionState::Prompt); // the request comes back Prompt
        let mut presenter = NativePresenter::new(refused.clone(), ClickSlot::default());
        presenter.present(&notice("n2", ""));
        assert!(refused.shown.lock().unwrap().is_empty());
        assert!(presenter.warned);
    }

    #[test]
    fn a_failing_notification_center_does_not_stop_later_notices() {
        let mut backend = FakeBackend::new(PermissionState::Granted);
        backend.fail_show = true;
        let mut presenter = NativePresenter::new(backend, ClickSlot::default());
        presenter.present(&notice("n1", ""));
        assert!(presenter.warned);
        let working = FakeBackend::new(PermissionState::Granted);
        presenter.backend = working.clone();
        presenter.present(&notice("n2", ""));
        assert_eq!(working.shown.lock().unwrap().len(), 1);
        assert!(!presenter.warned, "a recovery re-arms the one-time warning");
    }

    // ── The stream loop against a fake hub ──────────────────────────────────

    struct Collect {
        got: Arc<Mutex<Vec<String>>>,
        stop: Arc<AtomicBool>,
        stop_after: String,
    }

    impl Presenter for Collect {
        fn present(&mut self, notice: &Notice) {
            self.got.lock().unwrap().push(notice.id.clone());
            if notice.id == self.stop_after {
                self.stop.store(true, Ordering::Relaxed);
            }
        }
    }

    /// Serve one canned SSE body per connection, closing after each (a dropped
    /// connection). Returns the lower-cased request head of every connection.
    fn fake_hub(bodies: Vec<String>) -> (String, std::thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/api/events", listener.local_addr().unwrap());
        let handle = std::thread::spawn(move || {
            let mut heads = Vec::new();
            for body in bodies {
                let (mut socket, _) = listener.accept().unwrap();
                let mut head = Vec::new();
                let mut buf = [0u8; 1024];
                while !head.windows(4).any(|w| w == b"\r\n\r\n") {
                    let n = socket.read(&mut buf).unwrap();
                    if n == 0 {
                        break;
                    }
                    head.extend_from_slice(&buf[..n]);
                }
                heads.push(String::from_utf8_lossy(&head).to_lowercase());
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n{body}"
                );
                socket.write_all(response.as_bytes()).unwrap();
                socket.flush().unwrap();
            }
            heads
        });
        (url, handle)
    }

    #[test]
    fn a_dropped_connection_resumes_from_the_last_event_id_without_repeating_a_notice() {
        let later = now_unix() + 3600.0;
        let earlier = now_unix() - 60.0;
        let first = format!(": connected\n\n{}", notice_frame(5, "A", "question", "/app/?session=s", later));
        // After the drop the hub replays: A again (same notice id), an expired notice, and
        // B — raised while the receiver was disconnected.
        let second = format!(
            ": connected\n\n{}{}{}",
            notice_frame(6, "A", "question", "/app/?session=s", later),
            notice_frame(7, "stale", "notify", "", earlier),
            notice_frame(8, "B", "review", "", later),
        );
        let (url, hub) = fake_hub(vec![first, second]);
        let got = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let presenter = Collect { got: got.clone(), stop: stop.clone(), stop_after: "B".into() };
        let source = Source { url, token: Box::new(|| Some("tok-123".into())) };
        let backoff = Backoff { initial: Duration::from_millis(10), max: Duration::from_millis(20) };
        tauri::async_runtime::block_on(run(source, presenter, stop, backoff));

        let heads = hub.join().unwrap();
        assert_eq!(*got.lock().unwrap(), vec!["A".to_string(), "B".to_string()]);
        assert_eq!(heads.len(), 2);
        assert!(heads[0].contains("authorization: bearer tok-123"), "{}", heads[0]);
        assert!(heads[0].contains("accept: text/event-stream"));
        assert!(!heads[0].contains("last-event-id"), "a first connect has no cursor");
        assert!(heads[1].contains("last-event-id: 5"), "{}", heads[1]);
    }

    #[test]
    fn a_refused_or_unreachable_hub_is_retried_until_it_answers() {
        // First connection is refused with a 401 (no token yet); the retry succeeds.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/api/events", listener.local_addr().unwrap());
        let later = now_unix() + 3600.0;
        let hub = std::thread::spawn(move || {
            let replies = [
                "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_string(),
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n{}",
                    notice_frame(1, "N", "notify", "", later)
                ),
            ];
            for reply in replies {
                let (mut socket, _) = listener.accept().unwrap();
                let mut buf = [0u8; 2048];
                let _ = socket.read(&mut buf).unwrap();
                socket.write_all(reply.as_bytes()).unwrap();
                socket.flush().unwrap();
            }
        });
        let got = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let presenter = Collect { got: got.clone(), stop: stop.clone(), stop_after: "N".into() };
        let source = Source { url, token: Box::new(|| None) };
        let backoff = Backoff { initial: Duration::from_millis(10), max: Duration::from_millis(20) };
        tauri::async_runtime::block_on(run(source, presenter, stop, backoff));
        hub.join().unwrap();
        assert_eq!(*got.lock().unwrap(), vec!["N".to_string()]);
    }
}
