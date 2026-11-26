from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from stackorion.website_monitor import DEMO_MONITOR, simulate
from stackorion.monitor_schedule import MonitorSchedules, timestamp
from stackorion.work_items import WorkItems


class WebsiteScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = WorkItems(Path(directory.name))
        self.now = 1000
        self.monitor = DEMO_MONITOR
        self.key = self.monitor.key
        self.schedules = MonitorSchedules(self.store, self.store._record_monitor, clock=lambda: self.now)

    def tick(self, at, scenario):
        self.now = at
        self.schedules.run_due([self.monitor], lambda monitor: self.observation(scenario))

    def observation(self, scenario):
        result = simulate(self.monitor, scenario)
        if certificate := result["certificate"]:
            lifetime = (datetime.fromisoformat(certificate["expires_at"]) -
                        datetime.fromisoformat(result["observed_at"]))
            certificate["expires_at"] = (datetime.fromisoformat(timestamp(self.now)) + lifetime).isoformat()
        return {**result, "observed_at": timestamp(self.now)}

    def test_enable_interval_change_and_pause_persist(self):
        initial = self.schedules.describe(self.key)
        self.assertFalse(initial["enabled"])
        self.assertIsNone(initial["next_at"])
        for minutes, enabled in [(True, True), (0, True), (1441, True), (1.5, True), (1, 1)]:
            with self.subTest(minutes=minutes, enabled=enabled), self.assertRaises(ValueError):
                self.schedules.configure(self.key, minutes, enabled)
        self.schedules.configure(self.key, 2, True)
        self.now += 10
        self.assertEqual(self.schedules.configure(self.key, 2, True)["next_at"], timestamp(1120))
        self.assertEqual(self.schedules.configure(self.key, 1, True)["next_at"], timestamp(1070))
        self.tick(1069, "recovered")
        self.assertEqual(self.store.monitor_history(self.monitor), [])
        self.tick(1070, "recovered")
        self.schedules.configure(self.key, 1, False)
        self.tick(3000, "mismatch")
        store = WorkItems(self.store.path.parent)
        restarted = MonitorSchedules(store, store._record_monitor, clock=lambda: self.now)
        state = restarted.describe(self.key)
        self.assertEqual((state["passed"], state["failed"], state["missed"]), (1, 0, 0))
        self.assertEqual(state["since"], timestamp(1000))
        self.assertIsNone(state["next_at"])
        self.assertEqual(len(self.store.monitor_history(self.monitor)), 1)
        self.assertEqual(restarted.configure(self.key, 1, True)["next_at"], timestamp(3060))

    def test_skipped_slots_use_one_probe_and_expired_claim_is_missed(self):
        self.schedules.configure(self.key, 1, True)
        self.tick(1182, "recovered")
        self.assertEqual(self.schedules.describe(self.key)["missed"], 2)
        self.assertEqual(len(self.store.monitor_history(self.monitor)), 1)
        self.now = 1240
        self.assertEqual(self.schedules.claim(self.key), 1240)
        self.assertIsNone(self.schedules.claim(self.key))
        self.now = 1271
        restarted = MonitorSchedules(self.store, self.store._record_monitor, clock=lambda: self.now)
        self.assertIsNone(restarted.claim(self.key))
        self.assertEqual(restarted.describe(self.key)["missed"], 3)
        restarted.finish(self.monitor, 1240, self.observation("recovered"))
        self.assertEqual(len(self.store.monitor_history(self.monitor)), 1)
        self.assertEqual(restarted.describe(self.key)["next_at"], timestamp(1300))

    def test_scheduled_anomalies_deduplicate_and_only_good_recheck_closes(self):
        self.schedules.configure(self.key, 1, True)
        self.store.record_monitor(self.monitor, self.observation("recovered"), synthetic=True)
        self.assertEqual(self.schedules.describe(self.key)["passed"], 0)
        self.tick(1060, "mismatch")
        self.tick(1120, "mismatch")
        self.tick(1180, "timeout")
        items = self.store.snapshot()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "open")
        self.assertEqual(items[0]["evidence"]["http_status"], 200)
        self.tick(1240, "recovered")
        resolved = self.store.detail(items[0]["id"])
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual([entry["event"] for entry in resolved["history"]],
                         ["resolved", "retained", "confirmed", "opened"])
        state = self.schedules.describe(self.key)
        self.assertEqual((state["passed"], state["failed"], state["missed"]), (1, 2, 1))
        history = self.store.monitor_history(self.monitor)
        self.assertEqual([row.get("trigger") for row in history], ["scheduled"] * 4 + [None])

    def test_scheduled_certificate_alert_does_not_reduce_website_availability(self):
        self.schedules.configure(self.key, 1, True)
        self.tick(1060, "certificate-due")
        item, = self.store.snapshot()["items"]
        self.assertEqual(item["evidence"]["status"], "due")
        self.tick(1120, "certificate-urgent")
        self.assertEqual(self.store.detail(item["id"])["evidence"]["status"], "urgent")
        self.tick(1180, "certificate-missing")
        self.assertEqual(self.store.detail(item["id"])["status"], "open")
        self.tick(1240, "recovered")
        self.assertEqual(self.store.detail(item["id"])["status"], "resolved")
        schedule = self.schedules.describe(self.key)
        self.assertEqual((schedule["passed"], schedule["failed"], schedule["missed"]), (3, 0, 1))
