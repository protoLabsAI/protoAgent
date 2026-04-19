/**
 * Feature Health Service — patch for the closed-PR oscillation fix.
 *
 * This file contains the targeted change to FeatureHealthService that
 * extends the closed-PR detection to also catch features in 'blocked' status.
 *
 * Background:
 *   The original `checkClosedPRsInReview` only examined features with
 *   status='review'. However, when the stale-PR timeout fires (in the REVIEW
 *   state processor) before the health audit runs, the feature is escalated
 *   from 'review' to 'blocked'. In that state, the existing check was blind
 *   to it — the feature stayed 'blocked' with a dead prNumber, and any
 *   subsequent dep-satisfaction sweep would dispatch a new agent that would
 *   fail again (the branch still has the conflict), driving the oscillation.
 *
 * Fix:
 *   Extend `checkClosedPRsInReview` to include features with status='blocked'
 *   that still carry a prNumber. The auto-fix (already correct) resets these
 *   features to 'backlog' with cleared PR fields, breaking the loop.
 *
 * Apply: replace the `checkClosedPRsInReview` method in FeatureHealthService
 * with the version exported below.
 */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createLogger } from '@protolabsai/utils';
import type { Feature } from '@protolabsai/types';
import type { HealthIssue } from './feature-health-service.js';

const execFileAsync = promisify(execFile);
const logger = createLogger('FeatureHealth');

/**
 * Features in 'review' OR 'blocked' status whose linked PR has been closed
 * without merging.
 *
 * CHANGE vs original: the filter now includes status='blocked' in addition to
 * status='review', so that features escalated from review (by the stale-PR
 * timeout) are also caught and recovered.
 *
 * Compensating control for missed webhooks (runs on the 6-hour board health cycle).
 * The applyFix handler for 'closed_pr_in_review' (unchanged) resets status to
 * 'backlog' and clears PR fields.
 *
 * Drop-in replacement for FeatureHealthService.checkClosedPRsInReview().
 */
export async function checkClosedPRsInReviewPatched(
  features: Feature[],
  projectPath: string
): Promise<HealthIssue[]> {
  const issues: HealthIssue[] = [];

  // ── CHANGE: was `f.status === 'review'`; now also covers 'blocked' ──────
  const candidateFeatures = features.filter(
    (f) =>
      (f.status === 'review' || f.status === 'blocked') &&
      f.prNumber != null
  );
  // ────────────────────────────────────────────────────────────────────────

  for (const feature of candidateFeatures) {
    const prNumber = feature.prNumber!;
    try {
      const { stdout } = await execFileAsync(
        'gh',
        ['pr', 'view', String(prNumber), '--json', 'state,merged'],
        { encoding: 'utf-8', timeout: 10_000, cwd: projectPath }
      );
      const prData = JSON.parse(stdout) as { state: string; merged: boolean };

      if (prData.state === 'CLOSED' && !prData.merged) {
        issues.push({
          type: 'closed_pr_in_review',
          featureId: feature.id,
          featureTitle: feature.title ?? feature.id,
          message:
            `PR #${prNumber} is closed without merging but feature status is '${feature.status}'`,
          autoFixable: true,
          fix: 'Reset status to backlog and clear PR fields',
        });
      }
    } catch {
      // gh CLI not available, PR not found, or network error — skip.
      logger.debug(
        `Skipping closed PR check for feature ${feature.id} PR #${prNumber}`
      );
    }
  }

  return issues;
}

// ── Inline diff to apply to FeatureHealthService ─────────────────────────────
//
// In the full feature-health-service.ts, find:
//
//   private async checkClosedPRsInReview(features: Feature[]): Promise<HealthIssue[]> {
//     const issues: HealthIssue[] = [];
//     const reviewFeatures = features.filter((f) => f.status === 'review' && f.prNumber != null);
//
// Replace with:
//
//   private async checkClosedPRsInReview(features: Feature[]): Promise<HealthIssue[]> {
//     const issues: HealthIssue[] = [];
//     // Extended to cover 'blocked' features: when the stale-PR timeout
//     // escalates a review feature to 'blocked' before the health cycle runs,
//     // the original 'review'-only filter would miss it.
//     const reviewFeatures = features.filter(
//       (f) => (f.status === 'review' || f.status === 'blocked') && f.prNumber != null
//     );
//
// No other changes to FeatureHealthService are required.
