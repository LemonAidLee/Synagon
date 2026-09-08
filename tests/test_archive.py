"""Tests for orchestrator.archive - taking a run out of the store without destroying it.

Every test builds its own store in a temporary directory. Nothing here reads or writes the
project's real ``.orchestrator/runs``, which is the mistake this whole module exists to
clean up after.
"""

import json
import os
import shutil
import tempfile
import unittest

from orchestrator.archive import (
    ARCHIVE_META_FILENAME,
    REASON_MATCHED,
    REASON_SYNTHETIC,
    execute_archive,
    execute_restore,
    format_archive_plan,
    format_archived_list,
    is_synthetic,
    list_archived,
    plan_archive,
    plan_restore,
    run_evidence,
)


def _usage(available):
    return {
        "input_tokens": 10 if available else None,
        "output_tokens": 20 if available else None,
        "total_tokens": 30 if available else None,
        "available": bool(available),
    }


def _write_run(store, run_id, task="a task", status="completed", executions=(), events=None):
    """Write one run into `store`. `executions` is a list of (duration, reported_usage)."""
    run_dir = os.path.join(store, run_id)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as handle:
        json.dump({"run_id": run_id, "task": task, "status": status}, handle)

    lines = events
    if lines is None:
        lines = [{"sequence": 1, "run_id": run_id, "event": "run_started", "task": task}]
        for index, (duration, reported) in enumerate(executions):
            lines.append(
                {
                    "sequence": index + 2,
                    "run_id": run_id,
                    "event": "agent_result",
                    "result": {
                        "agent": "claude",
                        "role": "researcher",
                        "status": "success",
                        "duration_seconds": duration,
                        "token_usage": _usage(reported),
                    },
                }
            )
    with open(os.path.join(run_dir, "events.jsonl"), "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    return run_dir


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch-archive-")
        self.store = os.path.join(self.root, ".orchestrator", "runs")
        os.makedirs(self.store)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def archive_dir(self):
        return os.path.join(self.root, ".orchestrator", "archive", "runs")


class EvidenceTest(ArchiveTestCase):
    def test_reads_executions_durations_and_usage(self):
        run_dir = _write_run(
            self.store, "20260101T000000Z-aaaaaa", executions=[(0.01, False), (0.5, False)]
        )
        evidence = run_evidence(run_dir)
        self.assertTrue(evidence["readable"])
        self.assertEqual(evidence["executions"], 2)
        self.assertEqual(evidence["max_execution_seconds"], 0.5)
        self.assertFalse(evidence["token_usage_reported"])

    def test_one_reporting_execution_is_enough(self):
        run_dir = _write_run(
            self.store, "20260101T000000Z-bbbbbb", executions=[(0.01, False), (0.02, True)]
        )
        self.assertTrue(run_evidence(run_dir)["token_usage_reported"])

    def test_a_torn_line_does_not_break_the_read(self):
        run_dir = _write_run(self.store, "20260101T000000Z-cccccc", executions=[(0.01, False)])
        with open(os.path.join(run_dir, "events.jsonl"), "a", encoding="utf-8") as handle:
            handle.write('{"event": "agent_result", "result": {"dur')
        evidence = run_evidence(run_dir)
        self.assertTrue(evidence["readable"])
        self.assertEqual(evidence["executions"], 1)

    def test_a_missing_log_is_unreadable_rather_than_empty(self):
        evidence = run_evidence(os.path.join(self.store, "nothing-here"))
        self.assertFalse(evidence["readable"])


class IsSyntheticTest(unittest.TestCase):
    def test_fast_and_unreported_is_synthetic(self):
        self.assertTrue(
            is_synthetic(
                {
                    "readable": True,
                    "executions": 4,
                    "token_usage_reported": False,
                    "max_execution_seconds": 0.88,
                }
            )
        )

    def test_reported_usage_is_never_synthetic(self):
        self.assertFalse(
            is_synthetic(
                {
                    "readable": True,
                    "executions": 4,
                    "token_usage_reported": True,
                    "max_execution_seconds": 0.01,
                }
            )
        )

    def test_real_time_is_never_synthetic(self):
        self.assertFalse(
            is_synthetic(
                {
                    "readable": True,
                    "executions": 4,
                    "token_usage_reported": False,
                    "max_execution_seconds": 8.26,
                }
            )
        )

    def test_a_run_with_no_executions_is_not_synthetic(self):
        # An interrupted run recorded nothing; that is a real run, not a mocked one.
        self.assertFalse(
            is_synthetic(
                {
                    "readable": True,
                    "executions": 0,
                    "token_usage_reported": False,
                    "max_execution_seconds": 0.0,
                }
            )
        )

    def test_an_unreadable_run_is_not_synthetic(self):
        self.assertFalse(
            is_synthetic(
                {
                    "readable": False,
                    "executions": 0,
                    "token_usage_reported": False,
                    "max_execution_seconds": 0.0,
                }
            )
        )


class PlanArchiveTest(ArchiveTestCase):
    def _populate(self):
        _write_run(
            self.store,
            "20260101T000000Z-fake01",
            task="Verify trace sequence",
            executions=[(0.01, False)] * 4,
        )
        _write_run(
            self.store,
            "20260101T000100Z-fake02",
            task="Verify trace sequence",
            executions=[(0.01, False)] * 4,
        )
        _write_run(
            self.store,
            "20260101T000200Z-real01",
            task="Add format_budget_status()",
            executions=[(161.83, True)],
        )

    def test_selects_only_the_synthetic_runs(self):
        self._populate()
        plan = plan_archive(self.root)
        self.assertTrue(plan["available"])
        self.assertEqual(plan["total"], 3)
        self.assertEqual(
            sorted(c["run_id"] for c in plan["selected"]),
            ["20260101T000000Z-fake01", "20260101T000100Z-fake02"],
        )
        self.assertEqual([c["run_id"] for c in plan["kept"]], ["20260101T000200Z-real01"])
        self.assertEqual(plan["selected"][0]["reason"], REASON_SYNTHETIC)

    def test_every_kept_run_says_why_it_stayed(self):
        self._populate()
        plan = plan_archive(self.root)
        self.assertIn("reported real token usage", plan["kept"][0]["reason"])

    def test_matching_selects_a_real_run_the_synthetic_test_would_keep(self):
        self._populate()
        plan = plan_archive(self.root, synthetic=False, matching="format_budget")
        self.assertEqual([c["run_id"] for c in plan["selected"]], ["20260101T000200Z-real01"])
        self.assertEqual(plan["selected"][0]["reason"], REASON_MATCHED)

    def test_both_selectors_are_recorded_when_both_fire(self):
        self._populate()
        plan = plan_archive(self.root, matching="verify trace")
        for candidate in plan["selected"]:
            self.assertIn(REASON_SYNTHETIC, candidate["reason"])
            self.assertIn(REASON_MATCHED, candidate["reason"])

    def test_selecting_nothing_is_refused_rather_than_assumed(self):
        self._populate()
        plan = plan_archive(self.root, synthetic=False)
        self.assertFalse(plan["available"])
        self.assertIn("nothing was selected", plan["reason"])

    def test_a_missing_store_is_reported_not_raised(self):
        plan = plan_archive(os.path.join(self.root, "elsewhere"))
        self.assertFalse(plan["available"])
        self.assertIn("no run store", plan["reason"])

    def test_the_plan_renders_the_share_of_the_store_it_touches(self):
        self._populate()
        text = format_archive_plan(plan_archive(self.root))
        self.assertIn("3 recorded run(s); 2 to archive (67% of the store), 1 kept.", text)


class ExecuteArchiveTest(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        _write_run(
            self.store,
            "20260101T000000Z-fake01",
            task="Verify trace sequence",
            executions=[(0.01, False)] * 4,
        )
        _write_run(
            self.store,
            "20260101T000200Z-real01",
            task="Add format_budget_status()",
            executions=[(161.83, True)],
        )

    def test_the_run_leaves_the_store_and_arrives_whole(self):
        result = execute_archive(self.root, plan_archive(self.root))
        self.assertEqual(len(result["archived"]), 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(os.listdir(self.store), ["20260101T000200Z-real01"])
        archived = os.path.join(self.archive_dir(), "20260101T000000Z-fake01")
        self.assertTrue(os.path.isfile(os.path.join(archived, "run.json")))
        self.assertTrue(os.path.isfile(os.path.join(archived, "events.jsonl")))

    def test_the_archived_run_records_why_and_on_what_evidence(self):
        execute_archive(self.root, plan_archive(self.root))
        sidecar = os.path.join(
            self.archive_dir(), "20260101T000000Z-fake01", ARCHIVE_META_FILENAME
        )
        with open(sidecar, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["run_id"], "20260101T000000Z-fake01")
        self.assertEqual(meta["reason"], REASON_SYNTHETIC)
        self.assertEqual(meta["evidence"]["executions"], 4)
        self.assertFalse(meta["evidence"]["token_usage_reported"])
        self.assertTrue(meta["archived_at"])

    def test_the_store_stops_reporting_what_was_archived(self):
        from orchestrator.store import list_runs

        execute_archive(self.root, plan_archive(self.root))
        remaining = [r["run_id"] for r in list_runs(self.root, limit=0)]
        self.assertEqual(remaining, ["20260101T000200Z-real01"])

    def test_a_run_that_vanished_between_plan_and_move_is_reported_not_raised(self):
        plan = plan_archive(self.root)
        shutil.rmtree(os.path.join(self.store, "20260101T000000Z-fake01"))
        result = execute_archive(self.root, plan)
        self.assertEqual(result["archived"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("no longer in the store", result["failed"][0]["error"])

    def test_archiving_twice_moves_nothing_the_second_time(self):
        execute_archive(self.root, plan_archive(self.root))
        second = plan_archive(self.root)
        self.assertEqual(second["selected"], [])


class RestoreTest(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        _write_run(
            self.store,
            "20260101T000000Z-fake01",
            task="Verify trace sequence",
            executions=[(0.01, False)] * 4,
        )
        self.before = _read_tree(os.path.join(self.store, "20260101T000000Z-fake01"))
        execute_archive(self.root, plan_archive(self.root))

    def test_a_restored_run_is_byte_for_byte_what_it_was(self):
        result = execute_restore(plan_restore(self.root))
        self.assertEqual(len(result["restored"]), 1)
        self.assertEqual(result["failed"], [])
        after = _read_tree(os.path.join(self.store, "20260101T000000Z-fake01"))
        self.assertEqual(after, self.before)

    def test_restoring_removes_the_archive_sidecar(self):
        execute_restore(plan_restore(self.root))
        restored = os.path.join(self.store, "20260101T000000Z-fake01")
        self.assertFalse(os.path.exists(os.path.join(restored, ARCHIVE_META_FILENAME)))
        self.assertFalse(os.path.exists(os.path.join(self.archive_dir(), "20260101T000000Z-fake01")))

    def test_a_prefix_selects_the_run_it_names(self):
        plan = plan_restore(self.root, run_ids=["20260101T000000Z"])
        self.assertEqual([c["run_id"] for c in plan["selected"]], ["20260101T000000Z-fake01"])

    def test_an_unmatched_id_is_reported_rather_than_restoring_everything(self):
        plan = plan_restore(self.root, run_ids=["nope"])
        self.assertFalse(plan["available"])
        self.assertIn("no archived run matched", plan["reason"])

    def test_a_live_run_of_the_same_id_is_a_conflict_not_an_overwrite(self):
        _write_run(
            self.store,
            "20260101T000000Z-fake01",
            task="a different run entirely",
            executions=[(9.0, True)],
        )
        plan = plan_restore(self.root)
        self.assertEqual(plan["selected"], [])
        self.assertEqual(len(plan["conflicts"]), 1)
        result = execute_restore(plan)
        self.assertEqual(result["restored"], [])
        # The live run is untouched, and the archived copy is still archived.
        with open(
            os.path.join(self.store, "20260101T000000Z-fake01", "run.json"), encoding="utf-8"
        ) as handle:
            self.assertEqual(json.load(handle)["task"], "a different run entirely")
        self.assertTrue(
            os.path.isdir(os.path.join(self.archive_dir(), "20260101T000000Z-fake01"))
        )

    def test_the_archive_lists_what_it_holds(self):
        rows = list_archived(self.root)
        self.assertEqual([r["run_id"] for r in rows], ["20260101T000000Z-fake01"])
        self.assertEqual(rows[0]["task"], "Verify trace sequence")
        self.assertEqual(rows[0]["archive_reason"], REASON_SYNTHETIC)
        self.assertIn("20260101T000000Z-fake01", format_archived_list(rows))

    def test_an_empty_archive_says_so(self):
        execute_restore(plan_restore(self.root))
        self.assertEqual(list_archived(self.root), [])
        self.assertEqual(format_archived_list([]), "Nothing is archived.")


def _read_tree(directory):
    """Every file under `directory` as {relative path: bytes}."""
    contents = {}
    for base, _dirs, files in os.walk(directory):
        for name in files:
            full = os.path.join(base, name)
            with open(full, "rb") as handle:
                contents[os.path.relpath(full, directory)] = handle.read()
    return contents


if __name__ == "__main__":
    unittest.main()
