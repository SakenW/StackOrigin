import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stackorion.dashboard import DashboardData
from stackorion.monitor_schedule import timestamp
from stackorion.website_monitor import DEMO_MONITOR, Monitor, probe, simulate


class WebsiteMonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_demo_history_and_work_item_close_on_confirmed_recovery(self):
        data = DashboardData(self.root, None, True)
        data.run("daily", True)

        def check(scenario):
            return data.run_website(DEMO_MONITOR.id, True, scenario)["history"]

        first = check("mismatch")[0]
        self.assertEqual(first["http_status"], 200)
        self.assertEqual(first["content"], "mismatch")
        self.assertEqual(set(first["phases"]), {"dns", "tcp", "tls", "http"})
        self.assertIn("expires_at", first["certificate"])
        last_failure = check("mismatch")[0]
        items = [row for row in data.snapshot(True)["items"] if row["evidence"]["kind"] == "website"]
        self.assertEqual(len(items), 1)
        key = items[0]["id"]
        check("timeout")
        self.assertEqual(data.detail(key, True)["status"], "open")
        data.run("empty", True)
        self.assertEqual(data.detail(key, True)["status"], "open")

        data = DashboardData(self.root, None, True)
        self.assertEqual(len(data.website_targets(True)["targets"][0]["history"]), 3)
        recovered = check("recovered")[0]
        self.assertEqual(recovered["status"], "pass")
        self.assertEqual(data.detail(key, True)["status"], "resolved")
        self.assertEqual([entry["event"] for entry in data.detail(key, True)["history"]],
                         ["resolved", "retained", "confirmed", "opened"])
        detail = data.detail(key, True)
        self.assertEqual(detail["monitor_target"], DEMO_MONITOR.id)
        self.assertEqual(detail["failed_observation"], last_failure)
        with data.demo.connection() as db:
            db.execute("DELETE FROM monitor_run")
        detail = data.detail(key, True)
        self.assertIsNone(detail["failed_observation"])
        self.assertEqual(detail["evidence"]["content"], "mismatch")
        self.assertIsNone(data.detail(key, False))
        self.assertEqual(data.website_targets(False)["targets"], [])

    def test_only_local_registration_can_trigger_real_probe(self):
        config = self.root / "websites.json"
        config.write_text(json.dumps([{"id": "health", "url": "https://example.com/health", "expected_status": 200,
                                      "contains": "private-marker"}]))
        data = DashboardData(self.root / "state", None, False, config)
        self.assertFalse(data.website_targets(True)["editable"])
        self.assertNotIn("private-marker", json.dumps(data.website_targets(True)))
        with patch("stackorion.dashboard.probe") as run:
            self.assertIsNone(data.run_website("unregistered", True, "mismatch"))
            self.assertIsNone(data.run_website(DEMO_MONITOR.id, True, "mismatch"))
            run.assert_not_called()

    def test_recheck_runs_the_current_definition_of_the_exact_work_item(self):
        data = DashboardData(self.root, None, True)
        registered = data.register_website({"url":"https://example.com/health", "expected_status":200, "contains":"marker"})
        monitor = data.monitors[registered["id"]]
        data.live.record_monitor(monitor, simulate(monitor, "mismatch"), False)
        key = data.snapshot(False)["items"][0]["id"]
        self.assertTrue(data.detail(key, False)["can_recheck"])
        request = {"show_mock":False, "scenario":"recovered"}
        self.assertIsNone(data.recheck_work_item("0" * 64, request))
        with patch("stackorion.dashboard.probe", return_value=simulate(monitor, "timeout")) as run:
            result = data.recheck_work_item(key, request)
            run.assert_called_once_with(monitor)
        self.assertEqual(result["result"]["status"], "unknown")
        self.assertEqual(result["item"]["status"], "open")
        self.assertEqual(result["item"]["evidence"]["content"], "mismatch")
        data.update_website_conditions(monitor.id, {"revision":0, "expected_status":204,
                                                  "content_action":"keep", "contains":None})
        with patch("stackorion.dashboard.probe") as run:
            self.assertEqual(data.recheck_work_item(key, request), {"status":"unavailable"})
            run.assert_not_called()

    def test_online_certificate_is_independent_of_http_and_keeps_last_evidence_on_miss(self):
        data = DashboardData(self.root, None, True)

        def check(scenario):
            return data.run_website(DEMO_MONITOR.id, True, scenario)

        due = check("certificate-due")
        self.assertEqual(due["history"][0]["status"], "pass")
        self.assertEqual(due["certificate_status"]["days_remaining"], 12)
        self.assertEqual(due["certificate_status"]["status"], "due")
        check("certificate-urgent")
        items = data.snapshot(True)["items"]
        self.assertEqual(len(items), 1)
        key = items[0]["id"]
        self.assertEqual(items[0]["evidence"]["kind"], "website-certificate")
        self.assertEqual(items[0]["evidence"]["status"], "urgent")
        self.assertEqual(check("certificate-missing")["certificate_status"], {"status": "unknown"})
        data.run("daily", True)
        detail = data.detail(key, True)
        self.assertEqual(detail["status"], "open")
        self.assertEqual(detail["evidence"]["days_remaining"], 5)
        self.assertEqual(detail["monitor_target"], DEMO_MONITOR.id)
        self.assertIsNone(data.detail(key, False))

        data = DashboardData(self.root, None, True)
        healthy_cert = check("mismatch")
        self.assertEqual(healthy_cert["certificate_status"]["status"], "valid")
        self.assertEqual(healthy_cert["history"][0]["status"], "fail")
        detail = data.detail(key, True)
        self.assertEqual(detail["status"], "resolved")
        self.assertEqual([entry["event"] for entry in detail["history"]],
                         ["resolved", "retained", "confirmed", "opened"])

    def test_dashboard_registration_survives_restart_and_preserves_first_settings(self):
        data = DashboardData(self.root, None, False)
        clock = 1000
        data.schedules.clock = lambda: clock
        registration = {"url": "https://EXAMPLE.COM", "expected_status": 200, "contains": "private-marker"}
        with patch("stackorion.dashboard.probe", side_effect=lambda monitor: simulate(monitor, "recovered")) as run:
            saved = data.register_website(registration)
            duplicate = data.register_website({**registration, "url":"https://example.com/", "expected_status":204})
            self.assertEqual(saved["status"], "registered")
            self.assertEqual(duplicate, {"id":saved["id"], "status":"existing"})
            run.assert_not_called()
            self.assertFalse(data.website_targets(False)["targets"][0]["schedule"]["enabled"])
            self.assertEqual(data.run_website(saved["id"], False, "recovered")["history"][0]["status"], "pass")
            run.assert_called_once()
        data.configure_website_schedule(saved["id"], 1, True)
        clock += 60
        data.schedules.run_due(data.monitors.values(), lambda monitor: simulate(monitor, "recovered"))
        reopened = DashboardData(self.root, None, False)
        targets = reopened.website_targets(False)["targets"]
        self.assertEqual(len(targets), 1)
        self.assertEqual((targets[0]["id"], targets[0]["expected_status"]), (saved["id"], 200))
        self.assertEqual(len(targets[0]["history"]), 2)
        self.assertEqual(targets[0]["schedule"]["passed"], 1)
        self.assertNotIn("private-marker", json.dumps(targets))

    def test_dashboard_rejects_unsupported_registration(self):
        data = DashboardData(self.root, None, False)
        for url in ("http://example.com", "https://localhost", "https://example.com:444/", "https://user@example.com/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                data.register_website({"url":url, "expected_status":200, "contains":None})
        self.assertEqual(data.website_targets(False)["targets"], [])

    def test_condition_edit_preserves_evidence_and_starts_new_schedule_statistics(self):
        data = DashboardData(self.root, None, False)
        now = 1000
        data.schedules.clock = lambda: now
        target = data.register_website({"url":"https://example.com/", "expected_status":200,
                                        "contains":"private-marker"})["id"]
        old = data.monitors[target]
        data.configure_website_schedule(target, 1, True)
        now += 60
        failure = {**simulate(old, "mismatch"), "observed_at":timestamp(now)}
        data.schedules.run_due([old], lambda _: failure)
        prior = data.snapshot(False)["items"][0]
        change = {"revision":0, "expected_status":204, "content_action":"keep", "contains":None}
        self.assertEqual(data.update_website_conditions(target, {**change, "expected_status":200}), "unchanged")
        self.assertEqual(data.update_website_conditions(target, change), "updated")
        self.assertEqual(data.update_website_conditions(target, change), "stale")
        current = data.monitors[target]
        self.assertEqual((current.revision, current.expected_status, current.contains), (1, 204, "private-marker"))
        self.assertNotEqual(old.key, current.key)
        old_schedule = data.schedules.describe(old.key)
        self.assertEqual((old_schedule["enabled"], old_schedule["failed"]), (False, 1))
        with self.assertRaises(ValueError):
            data.schedules.configure(old.key, 5, True)
        new_schedule = data.schedules.describe(current.key)
        self.assertEqual((new_schedule["enabled"], new_schedule["interval_minutes"], new_schedule["failed"],
                          new_schedule["next_at"]), (True, 1, 0, timestamp(now + 60)))
        ended = data.detail(prior["id"], False)
        self.assertEqual((ended["status"], ended["history"][0]["event"]), ("superseded", "superseded"))
        self.assertEqual(ended["monitor_target"], target)
        self.assertEqual(ended["failed_observation"]["expected_status"], 200)
        data.live.record_monitor(old, {**failure, "observed_at":timestamp(now + 1)}, False)
        self.assertEqual(data.detail(prior["id"], False)["last_seen"], timestamp(now))
        self.assertEqual(data.update_website_conditions(target, {**change, "revision":1, "expected_status":200}), "updated")
        reopened = DashboardData(self.root, None, False)
        self.assertNotEqual(reopened.monitors[target].key, old.key)
        snapshot = reopened.website_targets(False)["targets"][0]
        self.assertEqual((snapshot["id"], snapshot["revision"], snapshot["history"]), (target, 2, []))
        self.assertEqual([row["expected_status"] for row in snapshot["previous_history"]], [200, 200])
        self.assertNotIn("private-marker", json.dumps(snapshot))

    def test_real_probe_pins_address_and_checks_body_without_recording_it(self):
        monitor = Monitor("health", "https://example.com/health", 200, "healthy-marker")
        raw = Mock()
        tls = Mock()
        tls.getpeercert.return_value = {
            "notAfter": "Feb 28 00:00:00 2027 GMT",
            "issuer": ((("commonName", "Offline CA"),),),
        }
        tls.version.return_value = "TLSv1.3"
        tls_context = Mock()
        socket_context = Mock()
        socket_context.__enter__ = Mock(return_value=raw)
        socket_context.__exit__ = Mock(return_value=False)
        wrap = Mock()
        wrap.__enter__ = Mock(return_value=tls)
        wrap.__exit__ = Mock(return_value=False)
        tls_context.wrap_socket.return_value = wrap

        def response(body, declared_length=None):
            length = len(body) if declared_length is None else declared_length
            wire = bytearray(f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n".encode() + body)
            def recv(buffer):
                size = min(len(wire), len(buffer))
                buffer[:size] = wire[:size]
                del wire[:size]
                return size
            tls.recv_into.side_effect = recv
            return probe(monitor)

        with patch("stackorion.website_monitor.dig", return_value={"values": ["93.184.216.34"]}), \
                patch("stackorion.website_monitor.socket.create_connection", return_value=socket_context) as connect, \
                patch("stackorion.website_monitor.ssl.create_default_context", return_value=tls_context):
            failed = response(b"wrong-body")
            passed = response(b"healthy-marker")
            unknown = response(b"incomplete-body", declared_length=400)
        self.assertEqual(connect.call_args.args, (("93.184.216.34", 443),))
        tls_context.wrap_socket.assert_called_with(raw, server_hostname="example.com")
        self.assertIn(b"GET /health HTTP", tls.sendall.call_args.args[0])
        self.assertEqual((failed["status"], failed["http_status"], failed["content"]), ("fail", 200, "mismatch"))
        self.assertEqual(passed["status"], "pass")
        self.assertEqual(unknown["status"], "unknown")
        self.assertNotIn("wrong-body", json.dumps(failed))
        self.assertNotIn("healthy-marker", json.dumps(passed))

    def test_nonpublic_resolution_never_connects(self):
        with patch("stackorion.website_monitor.dig", return_value={"values": ["127.0.0.1"]}), \
                patch("stackorion.website_monitor.socket.create_connection") as connect:
            self.assertEqual(probe(DEMO_MONITOR)["status"], "unknown")
            connect.assert_not_called()
