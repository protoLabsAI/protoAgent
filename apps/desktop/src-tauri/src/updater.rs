use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager, Runtime};
use tauri_plugin_updater::UpdaterExt;

use crate::show_main_window;
use crate::sidecar::stop_sidecar;

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
pub(crate) struct UpdateRequestState(Mutex<UpdateRequestSequence>);

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

pub(crate) fn request_update_from_tray<R: Runtime>(app: &AppHandle<R>) {
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

#[derive(serde::Serialize, Clone)]
pub(crate) struct UpdateInfo {
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
pub(crate) struct UpdateCheckCoordinator {
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
pub(crate) struct LaunchUpdateResult {
    done: bool,
    update: Option<UpdateInfo>,
}

/// Managed state for the launch check — written once by `spawn_launch_update_check`,
/// read (cheaply, no network) by the `updater_launch_result` command.
#[derive(Default)]
pub(crate) struct LaunchUpdateState(Mutex<LaunchUpdateResult>);

/// Kick off the update check CONCURRENTLY with sidecar/engine startup (#2203): the old
/// silent launch check was removed to avoid double-prompting (native dialog + web pill),
/// which left the first prompt waiting on webview boot + a 10s settle timer — you sat
/// through engine startup before learning a newer build existed. This check runs in
/// parallel with `spawn_sidecar`, never blocks window creation, and shows NO native
/// dialog: the result lands in `LaunchUpdateState`, where the web `UpdateNotice` pulls
/// it as soon as it mounts and owns the entire prompt UX (one prompt path, unchanged).
pub(crate) fn spawn_launch_update_check<R: Runtime>(app: AppHandle<R>) {
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
pub(crate) fn updater_launch_result<R: Runtime>(app: AppHandle<R>) -> LaunchUpdateResult {
    app.try_state::<LaunchUpdateState>()
        .map(|s| s.0.lock().unwrap().clone())
        .unwrap_or_default()
}

#[derive(serde::Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DownloadProgress {
    chunk_length: u64,
    content_length: Option<u64>,
}

/// Check the updater manifest for a newer build, returning its version + notes for the
/// in-app UpdateNotice (the web pill renders the changelog). None when up to date;
/// Err on failure so an interactive tray request can surface useful feedback.
#[tauri::command]
pub(crate) async fn updater_check<R: Runtime>(app: AppHandle<R>) -> Result<Option<UpdateInfo>, String> {
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
pub(crate) fn updater_consume_request<R: Runtime>(
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
pub(crate) fn updater_ack_request<R: Runtime>(
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
pub(crate) enum InstallUpdateResult {
    Superseded { update: UpdateInfo },
    UpToDate,
}

/// Download + install the available update (signature-verified by the plugin against the
/// embedded pubkey), streaming progress to the webview over an IPC Channel, then relaunch.
#[tauri::command]
pub(crate) async fn updater_install<R: Runtime>(
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
