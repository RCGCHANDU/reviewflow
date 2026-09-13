/**
 * Synthesis dispositions, keyed by issue id.
 *
 * Dispositions are the synthesis agent's decisions (accepted, rejected, merged) attached to
 * the reviewers' own findings. They are rendered alongside each issue, never used to hide
 * one: a rejected finding stays visible with the reason it was rejected.
 */

import type { IssueMap } from '../api/types'

export function dispositionMap(synthesis: IssueMap | null): Record<string, string> {
  const map: Record<string, string> = {}
  for (const item of synthesis?.accepted_issues ?? []) map[item.issue_id] = 'accepted'
  for (const item of synthesis?.rejected_issues ?? []) map[item.issue_id] = 'rejected'
  for (const merged of synthesis?.merged_issues ?? []) {
    for (const id of merged.issue_ids ?? []) map[id] = 'merged'
  }
  return map
}
