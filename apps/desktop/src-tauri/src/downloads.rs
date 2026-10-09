//! Finished downloads reach the console as `download:finished`, so it can say the file
//! actually landed and offer to open it or show it in Finder.
//!
//! A webview download (a plugin's `<a download>`, a chat export) used to finish silently: the
//! page only knows it *started* one. Tauri's download hook knows both ends, but on macOS the
//! `Finished` event never carries the saved path. So the hook CHOOSES the path on `Requested`
//! (the OS Downloads folder, never overwriting an existing file), remembers it by URL, and
//! reports it on `Finished`.
//!
//! `open_download` / `reveal_download` act only on paths this hook itself reported, so page
//! script can't use them to open an arbitrary file on disk.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::Serialize;
use tauri::webview::DownloadEvent;
use tauri::{AppHandle, Emitter, Manager, Runtime, Webview};
use tauri_plugin_opener::OpenerExt;

/// Downloads in flight (URL → chosen path) and the paths that finished successfully.
#[derive(Default)]
pub(crate) struct Downloads {
    pending: Mutex<HashMap<String, PathBuf>>,
    finished: Mutex<HashSet<PathBuf>>,
}

#[derive(Clone, Serialize)]
struct Finished {
    success: bool,
    /// Absolute path the file was saved to (`None` when it failed before a path was chosen).
    path: Option<String>,
    /// File name for display.
    name: Option<String>,
}

/// `dir/name`, or `dir/stem (n).ext` for the first `n` that doesn't exist yet.
pub(crate) fn unique_path(dir: &Path, name: &str) -> PathBuf {
    let candidate = dir.join(name);
    if !candidate.exists() {
        return candidate;
    }
    let p = Path::new(name);
    let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("download");
    let ext = p.extension().and_then(|s| s.to_str());
    (1..)
        .map(|n| match ext {
            Some(ext) => dir.join(format!("{stem} ({n}).{ext}")),
            None => dir.join(format!("{stem} ({n})")),
        })
        .find(|c| !c.exists())
        .expect("an unused name exists")
}

/// The `on_download` hook every console window installs.
pub(crate) fn handler<R: Runtime>(webview: Webview<R>, event: DownloadEvent<'_>) -> bool {
    let state = webview.app_handle().state::<Downloads>();
    match event {
        DownloadEvent::Requested { url, destination } => {
            let name = destination
                .file_name()
                .and_then(|n| n.to_str())
                .filter(|n| !n.is_empty())
                .unwrap_or("download")
                .to_string();
            let dir = webview
                .app_handle()
                .path()
                .download_dir()
                .ok()
                .or_else(|| destination.parent().map(Path::to_path_buf));
            if let Some(dir) = dir {
                *destination = unique_path(&dir, &name);
            }
            if let Ok(mut pending) = state.pending.lock() {
                pending.insert(url.to_string(), destination.clone());
            }
        }
        DownloadEvent::Finished { url, path, success } => {
            let chosen = state
                .pending
                .lock()
                .ok()
                .and_then(|mut p| p.remove(url.as_str()));
            let saved = path.filter(|p| !p.as_os_str().is_empty()).or(chosen);
            if success {
                if let (Some(p), Ok(mut done)) = (saved.as_ref(), state.finished.lock()) {
                    done.insert(p.clone());
                }
            }
            let payload = Finished {
                success,
                name: saved
                    .as_ref()
                    .and_then(|p| p.file_name())
                    .map(|n| n.to_string_lossy().into_owned()),
                path: saved.map(|p| p.to_string_lossy().into_owned()),
            };
            let _ = webview.emit("download:finished", payload);
        }
        _ => {}
    }
    true
}

fn reported<R: Runtime>(app: &AppHandle<R>, path: &str) -> Result<PathBuf, String> {
    let p = PathBuf::from(path);
    let known = app
        .state::<Downloads>()
        .finished
        .lock()
        .map(|done| done.contains(&p))
        .unwrap_or(false);
    if known {
        Ok(p)
    } else {
        Err("not a download this app saved".into())
    }
}

/// Open a finished download with the OS default app for its type.
#[tauri::command]
pub(crate) fn open_download<R: Runtime>(app: AppHandle<R>, path: String) -> Result<(), String> {
    let p = reported(&app, &path)?;
    app.opener()
        .open_path(p.to_string_lossy(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// Show a finished download in Finder / Explorer / the file manager.
#[tauri::command]
pub(crate) fn reveal_download<R: Runtime>(app: AppHandle<R>, path: String) -> Result<(), String> {
    let p = reported(&app, &path)?;
    app.opener()
        .reveal_item_in_dir(p)
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::unique_path;

    #[test]
    fn unique_path_never_overwrites() {
        let dir = std::env::temp_dir().join(format!("pa-dl-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(unique_path(&dir, "sheet.pdf"), dir.join("sheet.pdf"));
        std::fs::write(dir.join("sheet.pdf"), b"x").unwrap();
        assert_eq!(unique_path(&dir, "sheet.pdf"), dir.join("sheet (1).pdf"));
        std::fs::write(dir.join("sheet (1).pdf"), b"x").unwrap();
        assert_eq!(unique_path(&dir, "sheet.pdf"), dir.join("sheet (2).pdf"));
        std::fs::write(dir.join("notes"), b"x").unwrap();
        assert_eq!(unique_path(&dir, "notes"), dir.join("notes (1)"));
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
