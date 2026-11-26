import copy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stackorion.dashboard import demo_inspection
from stackorion.work_items import WorkItems


class WorkItemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = WorkItems(self.root)

    def test_replay_partial_observation_resolution_and_restart(self):
        first = self.store.record(demo_inspection("daily"))
        self.assertEqual(len(first["items"]), 5)
        key = next(item["id"] for item in first["items"] if item["evidence"]["kind"] == "ssh")
        self.assertFalse(self.store.record(demo_inspection("daily"))["recorded"])

        second = self.store.record(demo_inspection("repeat"))
        self.assertEqual(len(second["items"]), 5)
        repeated = self.store.detail(key)
        self.assertEqual(repeated["first_seen"], first["report"]["observed_at"])
        self.assertEqual(repeated["last_seen"], second["report"]["observed_at"])
        self.assertEqual(repeated["evidence"]["instances"], ["web-prod-01", "web-prod-02"])

        self.store.record(demo_inspection("missed"))
        retained = WorkItems(self.root).detail(key)
        self.assertEqual(retained["status"], "open")
        self.assertEqual(retained["last_seen"], repeated["last_seen"])
        self.assertEqual(retained["evidence"], repeated["evidence"])
        self.assertEqual([row["event"] for row in retained["history"]],
                         ["retained", "confirmed", "opened"])

        final_report = demo_inspection("empty")
        self.store.record(final_report)
        restored = WorkItems(self.root)
        resolved = restored.detail(key)
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolved_at"], final_report["observed_at"])
        self.assertEqual(resolved["history"][0]["event"], "resolved")
        self.assertFalse(restored.record(demo_inspection("repeat"))["recorded"])
        self.assertEqual(restored.detail(key), resolved)
        self.assertTrue(all(item["status"] == "resolved" for item in restored.snapshot()["items"]))

    def test_same_rule_in_two_accounts_and_reopen(self):
        report = demo_inspection("daily")
        rule = next(row for row in report["findings"] if row["kind"] == "ssh")
        other = {**rule, "account": "tencent-sandbox"}
        report["findings"].append(other)
        first = self.store.record(report)
        items = [row for row in first["items"] if row["evidence"]["kind"] == "ssh"]
        self.assertEqual(len({row["id"] for row in items}), 2)

        complete = demo_inspection("empty")
        self.store.record(complete)
        later = copy.deepcopy(report)
        later["observed_at"] = "2026-11-27T09:30:00+08:00"
        self.store.record(later)
        for row in items:
            detail = self.store.detail(row["id"])
            self.assertEqual(detail["status"], "open")
            self.assertIsNone(detail["resolved_at"])
            self.assertEqual([event["event"] for event in detail["history"]],
                             ["reopened", "resolved", "opened"])

    def test_inspection_history_backfills_latest_and_pages_accepted_runs_once(self):
        first = demo_inspection("daily")
        self.store.record(first)
        with self.store.connection() as db:
            db.execute("DROP TABLE inspection_run")
        self.store = WorkItems(self.root)
        [legacy] = self.store.inspection_history()["runs"]
        self.assertEqual(legacy["trigger"], "legacy")
        self.assertEqual(legacy["observed_at"], first["observed_at"])
        self.assertNotIn("findings", legacy)
        self.assertNotIn("balance", legacy["accounts"][0])
        self.assertEqual(self.store.snapshot()["report"], first)
        self.assertFalse(self.store.record(first)["recorded"])
        self.assertEqual(WorkItems(self.root).inspection_history()["runs"], [legacy])

        observed = datetime.fromisoformat(first["observed_at"])
        for minutes in range(1, 22):
            self.store.record({**first, "observed_at":(observed + timedelta(minutes=minutes)).isoformat()})
        page = self.store.inspection_history()
        self.assertEqual(len(page["runs"]), 20)
        self.assertEqual(page["runs"][0]["trigger"], "manual")
        self.store.record({**first, "observed_at":(observed + timedelta(minutes=22)).isoformat()})
        remaining = self.store.inspection_history(page["before"])
        self.assertEqual(len(remaining["runs"]), 2)
        self.assertEqual(remaining["runs"][-1], legacy)
        self.assertIsNone(remaining["before"])

    def test_known_exception_keeps_evidence_expires_and_does_not_follow_reopened_item(self):
        report = demo_inspection("daily")
        key = next(row["id"] for row in self.store.record(report)["items"] if row["evidence"]["kind"] == "ssh")
        reason = "本周接入迁移，复查后关闭公网规则"
        with patch("stackorion.work_items.review_today", return_value=date(2026, 11, 23)):
            with self.assertRaises(ValueError):
                self.store.update_exception(key, reason, "2026-11-23")
            self.assertEqual(self.store.update_exception(key, reason, "2026-11-26"), "saved")
            self.assertEqual(self.store.update_exception(key, reason, "2026-11-26"), "unchanged")
            self.store.record(demo_inspection("repeat"))
            self.store.record(demo_inspection("missed"))
            retained = WorkItems(self.root).detail(key)
            self.assertEqual(retained["exception"], {"reason":reason, "review_on":"2026-11-26", "active":True})
            self.assertEqual(retained["evidence"]["instances"], ["web-prod-01", "web-prod-02"])
            self.assertEqual([row["event"] for row in retained["history"]],
                             ["retained", "confirmed", "exception_set", "opened"])

        with patch("stackorion.work_items.review_today", return_value=date(2026, 11, 26)):
            restored = WorkItems(self.root)
            due = next(row for row in restored.snapshot()["items"] if row["id"] == key)
            self.assertEqual(due["status"], "open")
            self.assertEqual(due["exception"], {**retained["exception"], "active":False})
            restored.record(demo_inspection("empty"))
            self.assertNotIn("exception", restored.detail(key))
            self.assertEqual(restored.update_exception(key, None, None), "closed")
            restored.record({**report, "observed_at":"2026-11-27T09:30:00+08:00"})
            reopened = restored.detail(key)
            self.assertEqual(reopened["status"], "open")
            self.assertNotIn("exception", reopened)
            self.assertIn(reason, next(row["detail"] for row in reopened["history"] if row["event"] == "exception_set"))

    def test_unqueried_account_and_unrelated_complete_check_cannot_close_rule(self):
        first = self.store.record(demo_inspection("daily"))
        key = next(item["id"] for item in first["items"] if item["evidence"]["kind"] == "ssh")
        report = demo_inspection("empty")
        account = next(row for row in report["accounts"] if row["id"] == "tencent-prod")
        account["checks"].pop("安全组")
        self.store.record(report)
        self.assertEqual(self.store.detail(key)["history"][0]["event"], "retained")
