"""Tests for orchestrator.prune.prune_summary.

These tests build plan-shaped fixtures directly rather than exercising
``plan_prune`` against a real git repo. A kept candidate only needs ``branch``
and ``keep_reason`` for ``prune_summary`` to count it.
"""

import unittest

from orchestrator.prune import prune_summary


def _plan(kept=None, available=True):
    return {
        "prunable": [],
        "kept": kept or [],
        "total": len(kept or []),
        "available": available,
        "reason": None if available else "git is not installed or not on PATH",
    }


def _candidate(branch, keep_reason):
    return {"branch": branch, "keep_reason": keep_reason}


class PruneSummaryTest(unittest.TestCase):
    def test_unavailable_plan_returns_all_zero_counts(self):
        plan = _plan(available=False)
        self.assertEqual(
            prune_summary(plan),
            {
                "checked_out": 0,
                "too_new": 0,
                "dirty_worktree": 0,
                "no_recorded_run": 0,
                "run_failed": 0,
                "other": 0,
            },
        )

    def test_empty_kept_returns_all_zero_counts(self):
        plan = _plan(kept=[])
        self.assertEqual(
            prune_summary(plan),
            {
                "checked_out": 0,
                "too_new": 0,
                "dirty_worktree": 0,
                "no_recorded_run": 0,
                "run_failed": 0,
                "other": 0,
            },
        )

    def test_missing_kept_key_returns_all_zero_counts(self):
        plan = {"available": True}
        self.assertEqual(
            prune_summary(plan),
            {
                "checked_out": 0,
                "too_new": 0,
                "dirty_worktree": 0,
                "no_recorded_run": 0,
                "run_failed": 0,
                "other": 0,
            },
        )

    def test_checked_out_category(self):
        plan = _plan(kept=[_candidate("main", "checked out in the main repository")])
        self.assertEqual(prune_summary(plan)["checked_out"], 1)

    def test_too_new_category(self):
        plan = _plan(kept=[_candidate("feature/a", "newer than the threshold (3d old)")])
        self.assertEqual(prune_summary(plan)["too_new"], 1)

    def test_dirty_worktree_category(self):
        plan = _plan(
            kept=[_candidate("feature/b", "its worktree still holds uncommitted changes")]
        )
        self.assertEqual(prune_summary(plan)["dirty_worktree"], 1)

    def test_no_recorded_run_category(self):
        plan = _plan(
            kept=[
                _candidate(
                    "feature/c", "no recorded run, so success cannot be established"
                )
            ]
        )
        self.assertEqual(prune_summary(plan)["no_recorded_run"], 1)

    def test_run_failed_category(self):
        plan = _plan(kept=[_candidate("feature/d", "run did not succeed (status: FAIL)")])
        self.assertEqual(prune_summary(plan)["run_failed"], 1)

    def test_unknown_reason_lands_in_other(self):
        plan = _plan(kept=[_candidate("feature/e", "some future reason")])
        self.assertEqual(prune_summary(plan)["other"], 1)

    def test_same_category_with_dynamic_age_fragments_into_one_group(self):
        plan = _plan(
            kept=[
                _candidate("feature/f", "newer than the threshold (3d old)"),
                _candidate("feature/g", "newer than the threshold (10d old)"),
            ]
        )
        summary = prune_summary(plan)
        self.assertEqual(summary["too_new"], 2)
        self.assertEqual(summary["other"], 0)

    def test_same_category_with_dynamic_status_fragments_into_one_group(self):
        plan = _plan(
            kept=[
                _candidate("feature/h", "run did not succeed (status: FAIL)"),
                _candidate("feature/i", "run did not succeed (status: ERROR)"),
            ]
        )
        summary = prune_summary(plan)
        self.assertEqual(summary["run_failed"], 2)
        self.assertEqual(summary["other"], 0)

    def test_total_reconciles_with_kept_length(self):
        kept = [
            _candidate("main", "checked out in the main repository"),
            _candidate("feature/j", "newer than the threshold (2d old)"),
            _candidate("feature/k", "its worktree still holds uncommitted changes"),
            _candidate(
                "feature/l", "no recorded run, so success cannot be established"
            ),
            _candidate("feature/m", "run did not succeed (status: FAIL)"),
            _candidate("feature/n", "some future reason"),
        ]
        plan = _plan(kept=kept)
        summary = prune_summary(plan)
        self.assertEqual(sum(summary.values()), len(kept))
        self.assertEqual(summary["too_new"], 1)
        self.assertEqual(summary["other"], 1)


if __name__ == "__main__":
    unittest.main()
