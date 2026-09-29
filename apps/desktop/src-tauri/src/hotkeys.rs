use std::sync::Mutex;

use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_global_shortcut::Shortcut;

/// The shell's OS-global hotkeys (#1675): stable id → default chord, in the
/// global-hotkey string grammar ("super+shift+p"). The quick launcher is ⌥Space on
/// macOS (the Raycast-familiar default) and Ctrl+Alt+Space elsewhere — plain
/// Alt+Space is the Windows window system-menu accelerator (and PowerToys Run's
/// default), a guaranteed conflict (#1670). Operator overrides persist in
/// `<app-config>/hotkeys.json`, edited from Settings ▸ Keyboard.
const HOTKEY_CONSOLE: &str = "console_toggle";
pub(crate) const HOTKEY_LAUNCHER: &str = "quick_launcher";

pub(crate) fn default_hotkeys() -> Vec<(&'static str, String)> {
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
pub(crate) struct HotkeyStatus {
    pub(crate) id: String,
    pub(crate) chord: String,
    pub(crate) registered: bool,
    pub(crate) error: Option<String>,
}

/// Managed registry of the shell's global hotkeys (#1675).
#[derive(Default)]
pub(crate) struct Hotkeys(pub(crate) Mutex<Vec<HotkeyStatus>>);

fn hotkeys_file<R: Runtime>(app: &AppHandle<R>) -> Option<std::path::PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|d| d.join("hotkeys.json"))
}

/// Operator chord overrides (`{id: chord}`) — best-effort read; absent/garbled
/// files just mean defaults.
pub(crate) fn load_hotkey_overrides<R: Runtime>(
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
pub(crate) fn sync_hotkeys<R: Runtime>(app: &AppHandle<R>) {
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
pub(crate) fn hotkey_id_for<R: Runtime>(app: &AppHandle<R>, fired: &Shortcut) -> Option<String> {
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
pub(crate) fn hotkeys_status(state: tauri::State<'_, Hotkeys>) -> Vec<HotkeyStatus> {
    state.0.lock().unwrap().clone()
}

/// Rebind one shell global (#1675): validate the chord, release the old one,
/// persist, then re-register fallibly — a chord another app owns comes back as
/// `registered:false` + error rather than an exception, matching launch behavior.
#[tauri::command]
pub(crate) fn hotkeys_set<R: Runtime>(
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
