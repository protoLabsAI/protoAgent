//! Reclaim WebKit's localStorage write-ahead log at launch (#4974, macOS).
//!
//! WKWebView keeps each origin's localStorage in a SQLite database, in WAL mode, under
//! `~/Library/WebKit/<bundle id>/WebsiteData/Default/<top>/<origin>/LocalStorage/`.
//! While the app runs, WebKit can stop checkpointing that WAL, so it grew to 250 MB in one
//! day (2026-11-09). And because the app rarely closes WebKit cleanly (self-updates,
//! quits), the file is never truncated: it sits at its high-water mark from one launch to
//! the next. SQLite reuses it from the start after a relaunch, but never gives the disk
//! space back.
//!
//! So at launch, BEFORE any webview exists, a WAL above `RECLAIM_ABOVE_BYTES` is checkpointed
//! and truncated with the system `sqlite3`, the same library WebKit uses. That's an ordinary
//! SQLite operation under SQLite's own locking: if a WebKit process does hold the database
//! (another instance of the app), the checkpoint reports busy and changes nothing.

use std::path::{Path, PathBuf};
use std::process::Command;

/// A WAL smaller than this is left alone; WebKit's own cycling keeps a healthy one ~2 MB.
pub const RECLAIM_ABOVE_BYTES: u64 = 16 * 1024 * 1024;

const SYSTEM_SQLITE3: &str = "/usr/bin/sqlite3";

/// Every localStorage database under a WebKit data dir (`.../WebKit/<bundle id>`).
pub fn localstorage_dbs(webkit_dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let default = webkit_dir.join("WebsiteData").join("Default");
    let Ok(tops) = std::fs::read_dir(&default) else {
        return out;
    };
    for top in tops.flatten() {
        let Ok(origins) = std::fs::read_dir(top.path()) else {
            continue;
        };
        for origin in origins.flatten() {
            let db = origin
                .path()
                .join("LocalStorage")
                .join("localstorage.sqlite3");
            if db.is_file() {
                out.push(db);
            }
        }
    }
    out.sort();
    out
}

fn wal_path(db: &Path) -> PathBuf {
    let mut name = db.as_os_str().to_owned();
    name.push("-wal");
    PathBuf::from(name)
}

/// The WAL's size when it's over the reclaim threshold, else `None`.
pub fn oversized_wal(db: &Path, threshold: u64) -> Option<u64> {
    let size = std::fs::metadata(wal_path(db)).ok()?.len();
    (size > threshold).then_some(size)
}

/// Checkpoint and truncate one database's WAL with `sqlite3`. Returns the checkpoint's
/// `busy|log|checkpointed` line; `busy` = 1 means another process holds it (nothing changed).
pub fn reclaim(db: &Path, sqlite3: &Path) -> Result<String, String> {
    let output = Command::new(sqlite3)
        .args(["-batch", "-cmd", ".timeout 2000"])
        .arg(db)
        .arg("PRAGMA wal_checkpoint(TRUNCATE);")
        .output()
        .map_err(|e| format!("could not run {}: {e}", sqlite3.display()))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Reclaim every oversized localStorage WAL of this app. Never fails the launch: each
/// outcome is logged and the app carries on.
pub fn reclaim_at_launch(identifier: &str) {
    let Ok(home) = std::env::var("HOME") else {
        return;
    };
    let sqlite3 = Path::new(SYSTEM_SQLITE3);
    if !sqlite3.is_file() {
        return;
    }
    let webkit_dir = Path::new(&home)
        .join("Library")
        .join("WebKit")
        .join(identifier);
    for db in localstorage_dbs(&webkit_dir) {
        let Some(before) = oversized_wal(&db, RECLAIM_ABOVE_BYTES) else {
            continue;
        };
        match reclaim(&db, sqlite3) {
            Ok(result) => {
                let after = std::fs::metadata(wal_path(&db))
                    .map(|m| m.len())
                    .unwrap_or(0);
                log::info!(
                    "[webstorage] reclaimed localStorage WAL {} ({before} -> {after} bytes, checkpoint {result})",
                    db.display()
                );
            }
            Err(err) => log::warn!("[webstorage] could not reclaim {}: {err}", db.display()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("pa-webstorage-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn finds_every_origins_localstorage_db() {
        let root = tmp("find");
        for (top, origin) in [("t1", "t1"), ("t1", "o2"), ("t2", "t2")] {
            let dir = root
                .join("WebsiteData/Default")
                .join(top)
                .join(origin)
                .join("LocalStorage");
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("localstorage.sqlite3"), b"").unwrap();
        }
        // An origin with no localStorage yet.
        std::fs::create_dir_all(root.join("WebsiteData/Default/t3/t3")).unwrap();
        let found: Vec<String> = localstorage_dbs(&root)
            .iter()
            .map(|p| {
                p.strip_prefix(&root)
                    .unwrap()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect();
        assert_eq!(
            found,
            [
                "WebsiteData/Default/t1/o2/LocalStorage/localstorage.sqlite3",
                "WebsiteData/Default/t1/t1/LocalStorage/localstorage.sqlite3",
                "WebsiteData/Default/t2/t2/LocalStorage/localstorage.sqlite3",
            ]
        );
        assert!(localstorage_dbs(&root.join("missing")).is_empty());
    }

    #[test]
    fn only_a_wal_over_the_threshold_is_reclaimed() {
        let root = tmp("threshold");
        let db = root.join("localstorage.sqlite3");
        std::fs::write(&db, b"").unwrap();
        assert_eq!(oversized_wal(&db, 10), None); // no WAL at all
        std::fs::write(wal_path(&db), vec![0u8; 11]).unwrap();
        assert_eq!(oversized_wal(&db, 10), Some(11));
        assert_eq!(oversized_wal(&db, 11), None);
    }

    /// A WAL left behind by a process that died mid-session (the app is rarely closed
    /// cleanly) is checkpointed into the database and truncated; the data survives.
    #[cfg(target_os = "macos")]
    #[test]
    fn reclaims_a_leftover_wal_and_keeps_the_data() {
        let sqlite3 = Path::new(SYSTEM_SQLITE3);
        if !sqlite3.is_file() {
            return;
        }
        let root = tmp("reclaim");
        let db = root.join("localstorage.sqlite3");
        let run = |sql: &[&str]| {
            Command::new(sqlite3)
                .arg("-batch")
                .arg(&db)
                .args(sql)
                .output()
                .unwrap()
        };
        run(&[
            "PRAGMA journal_mode=WAL;",
            "CREATE TABLE ItemTable(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);",
        ]);
        // Write with checkpoints off, then die without closing: the WAL stays behind.
        run(&[
            "PRAGMA wal_autocheckpoint=0;",
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<200) \
             INSERT INTO ItemTable SELECT 'k'||(i%3), randomblob(50000) FROM n;",
            ".system kill -9 $PPID",
        ]);
        assert!(
            oversized_wal(&db, 100_000).is_some(),
            "the test needs a leftover WAL"
        );

        let result = reclaim(&db, sqlite3).unwrap();
        assert!(result.starts_with("0|"), "checkpoint not busy: {result}");
        assert_eq!(
            std::fs::metadata(wal_path(&db))
                .map(|m| m.len())
                .unwrap_or(0),
            0
        );
        let rows = run(&["SELECT count(*) FROM ItemTable;"]);
        assert_eq!(String::from_utf8_lossy(&rows.stdout).trim(), "3");
    }
}
