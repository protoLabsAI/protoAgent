/**
 * FeatureScheduler — patch module for the closed-PR oscillation fix.
 *
 * This file contains the two targeted changes to the FeatureScheduler class
 * that address the backlog↔blocked oscillation caused by sibling-feature
 * conflict auto-closes:
 *
 *  1. getHotFileOverlap(): extended to serialise dispatch of any sibling
 *     features that share filesToModify entries (not only HOT_FILE_BASENAMES).
 *
 *  2. loadPendingFeatures() / staleViaPrNumber loop: CLOSED (non-merged) PRs
 *     now clear prNumber so the feature re-dispatches cleanly instead of
 *     bouncing to the stale-PR timeout path.
 *
 *  3. checkAndDecayStalled(): skips features whose linked PR is already CLOSED,
 *     deferring recovery to PRStateWatcher / loadPendingFeatures instead of
 *     bumping failureCount.
 *
 * To apply: replace the three affected methods in the full FeatureScheduler
 * implementation (apps/server/src/services/feature-scheduler.ts in protoMaker).
 *
 * Types / helpers used below match the existing imports in that file.
 */

import path from 'path';
import { exec } from 'child_process';
import { promisify } from 'util';
import * as secureFs from '../lib/secure-fs.js';
import type { Feature } from '@protolabsai/types';
import {
  readJsonWithRecovery,
  logRecoveryWarning,
  DEFAULT_BACKUP_COUNT,
} from '@protolabsai/utils';
import { getFeaturesDir } from '@protolabsai/platform';

const execAsync = promisify(exec);

/**
 * Files that are frequently modified by parallel agents and cause merge conflicts.
 * (Unchanged from original — kept here for reference.)
 */
const HOT_FILE_BASENAMES = new Set(['wiring.ts', 'event.ts', 'index.ts', 'services.ts']);

// ─────────────────────────────────────────────────────────────────────────────
// PATCH 1 — getHotFileOverlap
// ─────────────────────────────────────────────────────────────────────────────
//
// BEFORE: only blocked when both features declared a file whose basename was in
//         HOT_FILE_BASENAMES. Sibling features sharing custom files like
//         `a2a_handler.py` were dispatched in parallel, causing the second PR
//         to become CONFLICTING/DIRTY after the first merged.
//
// AFTER:  additionally checks for ANY overlap in filesToModify between the
//         candidate and each active (running + starting) feature. When overlap
//         is detected the candidate is deferred until the active agent finishes.
//
// Drop-in replacement for FeatureScheduler.getHotFileOverlap().
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Check if a candidate feature shares files with any currently running or
 * starting feature. Returns the overlapping file basenames, or an empty array
 * if no overlap is detected.
 *
 * Blocks when EITHER:
 *  - both features declare a file whose basename is in HOT_FILE_BASENAMES, OR
 *  - both features declare any file with the same normalised path
 *    (full filesToModify overlap — the new sibling-serialisation guard).
 *
 * If filesToModify is absent or empty on either side, no blocking occurs.
 */
export async function getHotFileOverlapPatched(
  projectPath: string,
  candidate: Feature,
  startingFeatureIds: Set<string>,
  featureLoader: {
    get(projectPath: string, featureId: string): Promise<Feature | null>;
  },
  getRunningFeatureIds: (projectPath: string) => string[]
): Promise<string[]> {
  const candidateFiles = candidate.filesToModify;
  if (!candidateFiles || candidateFiles.length === 0) return [];

  // Hot-file basenames (original logic).
  const candidateHotFiles = candidateFiles
    .map((f) => path.basename(f))
    .filter((b) => HOT_FILE_BASENAMES.has(b));

  // Full filesToModify set for path-level overlap detection (new logic).
  const candidateFileSet = new Set(candidateFiles.map((f) => path.normalize(f)));

  // If neither check can produce a hit, bail early.
  if (candidateHotFiles.length === 0 && candidateFileSet.size === 0) return [];

  // Active feature IDs scoped to this project (running + starting).
  const activeIds = new Set([
    ...getRunningFeatureIds(projectPath),
    ...startingFeatureIds,
  ]);
  activeIds.delete(candidate.id);

  if (activeIds.size === 0) return [];

  const overlapping = new Set<string>();

  for (const fid of activeIds) {
    const feature = await featureLoader.get(projectPath, fid);
    if (!feature?.filesToModify || feature.filesToModify.length === 0) continue;

    for (const filePath of feature.filesToModify) {
      const basename = path.basename(filePath);

      // Original hot-file check.
      if (candidateHotFiles.includes(basename)) {
        overlapping.add(basename);
      }

      // New: full filesToModify path overlap (serialises sibling dispatches).
      const normalized = path.normalize(filePath);
      if (candidateFileSet.has(normalized)) {
        overlapping.add(basename);
      }
    }
  }

  return [...overlapping];
}

// ─────────────────────────────────────────────────────────────────────────────
// PATCH 2 — loadPendingFeatures: staleViaPrNumber CLOSED-PR recovery
// ─────────────────────────────────────────────────────────────────────────────
//
// BEFORE: the staleViaPrNumber loop called `gh pr view` and only acted when
//         state=MERGED. A CLOSED (non-merged) PR was silently ignored, leaving
//         prNumber set on the feature. The feature was then re-dispatched by the
//         agent, failed (branch still has the conflict), and incremented
//         failureCount — driving the backlog→blocked→backlog oscillation.
//
// AFTER:  when state=CLOSED (and not merged), the feature's prNumber / prUrl /
//         reviewStartedAt are cleared and statusChangeReason is set to
//         'PR closed by conflict — rebasing' so the next dispatch starts on a
//         fresh branch. failureCount is NOT incremented.
//
// Apply inside the `staleViaPrNumber` for-loop in loadPendingFeatures(), after
// the existing `if (prView.state === 'MERGED') { ... }` block:
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Handle a CLOSED (non-merged) PR discovered during the staleViaPrNumber sweep.
 * Clears PR linkage on the feature so it can be re-dispatched cleanly.
 *
 * @param projectPath  - Repo root passed to gh CLI.
 * @param feature      - The feature whose prNumber points to the closed PR.
 * @param featureLoader - Used to persist the update.
 */
export async function recoverFeatureFromClosedPr(
  projectPath: string,
  feature: Feature,
  featureLoader: {
    update(
      projectPath: string,
      featureId: string,
      patch: Partial<Feature>
    ): Promise<void>;
  },
  log: (msg: string) => void,
  logError: (msg: string, err: unknown) => void
): Promise<void> {
  log(
    `[loadPendingFeatures] Feature ${feature.id} ("${feature.title}") ` +
    `PR #${feature.prNumber} is CLOSED (not merged) — clearing prNumber, recovering to backlog`
  );
  try {
    await featureLoader.update(projectPath, feature.id, {
      prNumber: undefined,
      prUrl: undefined,
      reviewStartedAt: undefined,
      statusChangeReason: 'PR closed by conflict — rebasing',
    });
    // Mutate the in-memory copy so this sweep's later logic sees the cleared state.
    feature.prNumber = undefined;
  } catch (error) {
    logError(
      `[loadPendingFeatures] Failed to clear prNumber for closed PR on feature ${feature.id}:`,
      error
    );
  }
}

// Inline version for direct insertion into the staleViaPrNumber for-loop:
//
// ```typescript
// const prView: { state: string; mergedAt?: string } = JSON.parse(prViewJson);
// if (prView.state === 'MERGED') {
//   // ... existing done-reconciliation logic ...
// } else if (prView.state === 'CLOSED') {
//   // ── NEW: Closed-PR recovery ──────────────────────────────────────────
//   logger.info(
//     `[loadPendingFeatures] Feature ${feature.id} ("${feature.title}") ` +
//     `PR #${feature.prNumber} is CLOSED (not merged) — clearing prNumber, recovering to backlog`
//   );
//   try {
//     await this.featureLoader.update(projectPath, feature.id, {
//       prNumber: undefined,
//       prUrl: undefined,
//       reviewStartedAt: undefined,
//       statusChangeReason: 'PR closed by conflict — rebasing',
//     });
//     feature.prNumber = undefined;
//   } catch (error) {
//     logger.error(
//       `[loadPendingFeatures] Failed to clear prNumber for closed PR on feature ${feature.id}:`,
//       error
//     );
//   }
//   // ─────────────────────────────────────────────────────────────────────
// }
// ```

// ─────────────────────────────────────────────────────────────────────────────
// PATCH 3 — checkAndDecayStalled: skip CLOSED-PR features
// ─────────────────────────────────────────────────────────────────────────────
//
// BEFORE: checkAndDecayStalled decayed ANY 'review' feature with a CI failure
//         indicator that had been stalled beyond the timeout. Features whose PR
//         was CLOSED were incorrectly treated as "stalled with CI failures" and
//         decayed to backlog with failureCount++. On the next sweep they were
//         re-dispatched, conflicted, and decayed again — the oscillation loop.
//
// AFTER:  before decaying, confirm the feature's linked PR is still OPEN.
//         If the PR is CLOSED (non-merged), skip the decay; PRStateWatcher and
//         the loadPendingFeatures closed-PR recovery will handle it without
//         bumping failureCount.
//
// Replace the `checkAndDecayStalled` method in FeatureScheduler with the version
// below. The new PR-state guard is the only substantive change.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Find features in 'review' status with failing CI that have been stalled
 * beyond the configured timeout, and reset them to 'backlog' with an
 * incremented failureCount.
 *
 * NEW: skips features whose linked PR is CLOSED (not merged). Conflict
 * auto-closes should not accumulate agent failure counts — they indicate a
 * scheduling issue, not a coding error. PRStateWatcher handles those.
 *
 * @returns Number of features that were decayed.
 */
export async function checkAndDecayStalledPatched(
  projectPath: string,
  timeoutMinutes: number,
  featureLoader: {
    update(projectPath: string, featureId: string, patch: Partial<Feature>): Promise<void>;
  },
  emitAutoDecayed: (payload: {
    featureId: string;
    featureTitle: string | undefined;
    elapsedMinutes: number;
    failureCount: number;
    projectPath: string;
  }) => void,
  log: {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string, err?: unknown) => void;
    debug: (msg: string) => void;
  }
): Promise<number> {
  if (timeoutMinutes === 0) return 0;

  const timeoutMs = timeoutMinutes * 60 * 1000;
  const featuresDir = getFeaturesDir(projectPath);
  let decayedCount = 0;

  try {
    const entries = await secureFs.readdir(featuresDir, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const featurePath = path.join(featuresDir, entry.name, 'feature.json');
      const result = await readJsonWithRecovery<Feature | null>(featurePath, null, {
        maxBackups: DEFAULT_BACKUP_COUNT,
        autoRestore: false,
      });
      logRecoveryWarning(result, `Feature ${entry.name}`, {
        warn: (msg: string) => log.warn(msg),
      } as never);
      const feature = result.data;
      if (!feature || feature.status !== 'review') continue;

      // Determine how long the feature has been in review.
      const reviewTimestamp = feature.reviewStartedAt ?? feature.updatedAt;
      if (!reviewTimestamp) continue;
      const reviewStartMs =
        typeof reviewTimestamp === 'number'
          ? reviewTimestamp
          : new Date(reviewTimestamp).getTime();
      const elapsedMs = Date.now() - reviewStartMs;
      if (elapsedMs < timeoutMs) continue;

      // Check for failing CI indicators.
      const hasCiFailureIndicator =
        (feature.ciRemediationCount ?? 0) > 0 ||
        (feature.ciIterationCount ?? 0) > 0 ||
        (feature.remediationHistory ?? []).some(
          (h: { cycleType?: string }) => h.cycleType === 'ci_failure'
        ) ||
        /\bci\b|\bci fail/i.test(feature.statusChangeReason ?? '');

      if (!hasCiFailureIndicator) continue;

      // ── NEW: skip decay when the linked PR is already CLOSED ────────────
      // A CLOSED PR means GitHub auto-closed it due to a merge conflict.
      // The conflict is a scheduling issue, not a CI/agent failure — bumping
      // failureCount here would push the feature toward the human-escalation
      // threshold incorrectly. PRStateWatcher / loadPendingFeatures handle
      // closed-PR recovery without touching failureCount.
      if (feature.prNumber) {
        try {
          const prNum = String(feature.prNumber).replace(/[^0-9]/g, '');
          const { stdout: prStateRaw } = await execAsync(
            `gh pr view ${prNum} --json state --jq '.state'`,
            { cwd: projectPath, timeout: 10_000 }
          );
          if (prStateRaw.trim() === 'CLOSED') {
            log.info(
              `[AutoDecay] Feature ${feature.id} PR #${feature.prNumber} is CLOSED ` +
              `— skipping decay, closed-PR recovery will handle it without bumping failureCount`
            );
            continue;
          }
        } catch {
          // gh CLI error — fall through to decay as safe fallback.
        }
      }
      // ────────────────────────────────────────────────────────────────────

      // Decay the feature back to backlog.
      const prevFailureCount = feature.failureCount ?? 0;
      const elapsedMinutes = Math.round(elapsedMs / 60000);
      try {
        await featureLoader.update(projectPath, feature.id, {
          status: 'backlog',
          failureCount: prevFailureCount + 1,
          statusChangeReason: `Auto-decayed: stalled in review for ${elapsedMinutes}min with failing CI`,
          prNumber: undefined,
          prUrl: undefined,
          prCreatedAt: undefined,
          reviewStartedAt: undefined,
          prTrackedSince: undefined,
        });
        decayedCount++;
        log.warn(
          `[AutoDecay] Feature ${feature.id} ("${feature.title}") decayed from review to backlog — ` +
          `stalled ${elapsedMinutes}min with failing CI (failureCount: ${prevFailureCount} -> ${prevFailureCount + 1})`
        );
        emitAutoDecayed({
          featureId: feature.id,
          featureTitle: feature.title,
          elapsedMinutes,
          failureCount: prevFailureCount + 1,
          projectPath,
        });
      } catch (error) {
        log.error(`[AutoDecay] Failed to decay feature ${feature.id}:`, error);
      }
    }
  } catch (error) {
    log.error('[AutoDecay] Error scanning review queue for stalled features:', error);
  }

  if (decayedCount > 0) {
    log.warn(`[AutoDecay] Decayed ${decayedCount} stalled review feature(s) back to backlog`);
  }

  return decayedCount;
}
