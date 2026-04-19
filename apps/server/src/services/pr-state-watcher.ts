/**
 * PR State Watcher — Detects CLOSED (non-merged) PR transitions and triggers
 * feature recovery.
 *
 * Addresses the root-cause of the backlog↔blocked oscillation described in:
 * https://github.com/protolabsai/protoMaker/issues/XXXX
 *
 * Problem:
 *   When two sibling features concurrently modify the same files and the first
 *   merges, GitHub auto-closes the second PR with mergeable=CONFLICTING /
 *   mergeState=DIRTY. The pipeline had no real-time handler for this OPEN→CLOSED
 *   transition: the feature stayed in 'review' with a dead prNumber, the
 *   stale-PR timeout fired, set it to 'blocked', and repeated every sweep.
 *
 * Fix:
 *   This service polls features in 'review' and 'blocked' status every
 *   PR_STATE_POLL_INTERVAL_MS. When it finds a linked PR in state=CLOSED (and
 *   not merged), it atomically:
 *     (a) nulls out prNumber / prUrl / reviewStartedAt on the feature
 *     (b) resets status to 'backlog'
 *     (c) sets statusChangeReason = 'PR closed by conflict — rebasing'
 *     (d) does NOT increment failureCount (conflict auto-close is not an agent failure)
 *
 * Integration:
 *   Instantiate once in the server wiring (apps/server/src/wiring.ts) and call
 *   start(projectPath) after auto-mode is enabled for a project. Multiple projects
 *   can be registered; each runs an independent poll cycle.
 */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createLogger } from '@protolabsai/utils';
import type { Feature } from '@protolabsai/types';
import type { FeatureLoader } from './feature-loader.js';

const execFileAsync = promisify(execFile);
const logger = createLogger('PRStateWatcher');

/** Poll interval for closed-PR detection (default: 90 seconds). */
const PR_STATE_POLL_INTERVAL_MS = parseInt(
  process.env.PR_STATE_POLL_INTERVAL_MS ?? '90000',
  10
);

/** Statuses that warrant a closed-PR check (feature has a linked PR that could be CLOSED). */
const WATCHABLE_STATUSES = new Set<string>(['review', 'blocked']);

/** In-memory record of PR numbers already recovered this session (idempotency guard). */
const recoveredPrNumbers = new Set<number>();

interface ProjectWatcher {
  projectPath: string;
  timer: ReturnType<typeof setInterval> | null;
}

export class PRStateWatcher {
  private readonly watchers = new Map<string, ProjectWatcher>();

  constructor(private readonly featureLoader: FeatureLoader) {}

  /**
   * Begin watching a project for CLOSED PR transitions.
   * Safe to call multiple times for the same project — subsequent calls are no-ops.
   */
  start(projectPath: string): void {
    if (this.watchers.has(projectPath)) {
      logger.debug(`PRStateWatcher already watching ${projectPath}`);
      return;
    }

    logger.info(`PRStateWatcher: starting closed-PR poll for ${projectPath} (interval=${PR_STATE_POLL_INTERVAL_MS}ms)`);

    const watcher: ProjectWatcher = { projectPath, timer: null };
    this.watchers.set(projectPath, watcher);

    watcher.timer = setInterval(() => {
      void this.pollProject(projectPath).catch((err) => {
        logger.warn(`PRStateWatcher poll error for ${projectPath}:`, err);
      });
    }, PR_STATE_POLL_INTERVAL_MS);

    // Run an immediate first sweep so we don't wait a full interval on startup.
    void this.pollProject(projectPath).catch((err) => {
      logger.warn(`PRStateWatcher initial sweep error for ${projectPath}:`, err);
    });
  }

  /** Stop watching a project (e.g. when auto-mode is disabled). */
  stop(projectPath: string): void {
    const watcher = this.watchers.get(projectPath);
    if (!watcher) return;
    if (watcher.timer) clearInterval(watcher.timer);
    this.watchers.delete(projectPath);
    logger.info(`PRStateWatcher: stopped watching ${projectPath}`);
  }

  /** Stop all watchers (graceful shutdown). */
  stopAll(): void {
    for (const { projectPath } of this.watchers.values()) {
      this.stop(projectPath);
    }
  }

  // ── Private ───────────────────────────────────────────────────────────────

  private async pollProject(projectPath: string): Promise<void> {
    const features = await this.featureLoader.getAll(projectPath);

    const candidates = features.filter(
      (f) =>
        f.prNumber != null &&
        f.status != null &&
        WATCHABLE_STATUSES.has(f.status)
    );

    if (candidates.length === 0) return;

    logger.debug(
      `PRStateWatcher: checking ${candidates.length} candidate(s) in ${projectPath}`
    );

    for (const feature of candidates) {
      await this.checkFeature(projectPath, feature);
    }
  }

  /**
   * Check a single feature's linked PR.
   * If CLOSED (not merged), trigger recovery atomically.
   */
  private async checkFeature(projectPath: string, feature: Feature): Promise<void> {
    const prNumber = feature.prNumber!;

    // Skip PRs we already recovered in this server session (idempotency).
    if (recoveredPrNumbers.has(prNumber)) return;

    try {
      const { stdout } = await execFileAsync(
        'gh',
        ['pr', 'view', String(prNumber), '--json', 'state,merged,mergedAt'],
        { encoding: 'utf-8', timeout: 10_000, cwd: projectPath }
      );

      const prData = JSON.parse(stdout) as {
        state: string;
        merged: boolean;
        mergedAt?: string | null;
      };

      if (prData.state !== 'CLOSED') return; // still OPEN or MERGED — nothing to do
      if (prData.merged) return;              // merged via a different path — scheduler handles it

      // PR is CLOSED without merging — recover the feature.
      logger.warn(
        `PRStateWatcher: PR #${prNumber} for feature "${feature.title}" (${feature.id}) ` +
        `is CLOSED without merging — recovering to backlog`
      );

      await this.recoverFeature(projectPath, feature);
      recoveredPrNumbers.add(prNumber);
    } catch (err) {
      // gh CLI unavailable, network error, or PR not found — skip silently.
      logger.debug(
        `PRStateWatcher: could not check PR #${prNumber} for feature ${feature.id}: ${err}`
      );
    }
  }

  /**
   * Atomically reset a feature whose PR was closed by conflict.
   *
   * Deliberately does NOT increment failureCount — a conflict auto-close is
   * caused by sibling dispatch ordering, not an agent coding error.
   */
  private async recoverFeature(projectPath: string, feature: Feature): Promise<void> {
    try {
      await this.featureLoader.update(projectPath, feature.id, {
        status: 'backlog',
        prNumber: undefined,
        prUrl: undefined,
        reviewStartedAt: undefined,
        prTrackedSince: undefined,
        statusChangeReason: 'PR closed by conflict — rebasing',
      });

      logger.info(
        `PRStateWatcher: recovered feature ${feature.id} ("${feature.title}") ` +
        `to backlog after closed PR #${feature.prNumber}`
      );
    } catch (err) {
      logger.error(
        `PRStateWatcher: failed to recover feature ${feature.id} after closed PR #${feature.prNumber}:`,
        err
      );
    }
  }
}
