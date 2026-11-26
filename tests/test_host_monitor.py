import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stackorion.catalog import CatalogError
from stackorion.dashboard import DashboardData
from stackorion.host_disk_trend import disk_projection
from stackorion.host_monitor import AdapterHostMonitor, DEMO_HOST, collect_host, simulate_host
from stackorion.monitor_schedule import MonitorSchedules
from stackorion.plugin_api import HostReading, HostTarget


class HostMonitorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = DashboardData(self.root / "state", None, True)

    def test_resource_links_bind_unique_account_region_and_instance(self):
        data = DashboardData(self.root / "links", None, False)
        target = HostTarget("same-id", "las", "ap-southeast-4")
        first = AdapterHostMonitor("first-host", "same-alias", "svc-a", "qiniu-las", "qiniu", "a", target)
        second = replace(first, id="second-host", service_id="svc-b", account_id="b")
        data.hosts = {entry.id: entry for entry in (first, second)}
        asset = {"account": "live:qiniu:b", "region": "ap-southeast-4", "id": "same-id"}
        self.assertEqual(data.host_link(asset), "second-host")
        self.assertIsNone(data.host_link({**asset, "region": "different"}))
        data.hosts["ambiguous"] = replace(second, id="ambiguous")
        self.assertIsNone(data.host_link(asset))

    def test_disk_todo_opens_on_second_hit_deduplicates_and_closes_on_valid_recheck(self):
        def run(scenario):
            return self.data.run_host(DEMO_HOST.id, True, scenario)

        self.assertIsNone(self.data.run_host(DEMO_HOST.id, False, "high"))
        self.assertIsNone(self.data.run_host("unregistered", True, "high"))
        run("high")
        self.assertFalse(self.data.snapshot(True)["items"])
        run("high")
        run("high")
        [item] = self.data.snapshot(True)["items"]
        self.assertEqual(item["evidence"]["used_percent"], 91)
        key = item["id"]
        run("missing")
        self.data.run("empty", True)
        self.assertEqual(self.data.detail(key, True)["status"], "open")
        self.assertIsNone(self.data.detail(key, False))
        self.data = DashboardData(self.root / "state", None, True)
        self.assertEqual(len(self.data.host_targets(True)["targets"][0]["history"]), 4)
        run("healthy")
        closed = self.data.detail(key, True)
        self.assertEqual(closed["status"], "resolved")
        self.assertEqual([entry["event"] for entry in closed["history"]],
                         ["resolved", "retained", "confirmed", "opened"])

    def test_disk_recheck_binds_the_host_and_threshold_that_opened_the_item(self):
        data = self.data
        target = HostTarget("same-id", "edge", "north")
        first = AdapterHostMonitor("first-host", "same-name", "svc-a", "example", "custom", "a", target, 85)
        second = replace(first, id="second-host", service_id="svc-b", account_id="b")
        data.hosts = {host.id: host for host in (first, second)}
        for host in (first, second):
            for _ in range(2):
                data.live.record_host(host, simulate_host("high"), False)
        keys = {row["evidence"]["account"]:row["id"] for row in data.snapshot(False)["items"]}
        request = {"show_mock":False, "scenario":"healthy"}
        with patch.object(data, "collect_server", return_value=simulate_host("missing")) as collect:
            result = data.recheck_work_item(keys["b"], request)
            collect.assert_called_once_with(second)
        self.assertIsNone(result["result"]["disk_percent"])
        self.assertEqual(result["item"]["evidence"]["used_percent"], 91)
        with patch.object(data, "collect_server", return_value=simulate_host("healthy")) as collect:
            result = data.recheck_work_item(keys["b"], request)
            collect.assert_called_once_with(second)
        self.assertEqual(result["item"]["status"], "resolved")
        self.assertEqual(result["item"]["latest_detection"]["disk_percent"], 66)
        self.assertEqual(data.detail(keys["a"], False)["status"], "open")

        data.hosts[first.id] = replace(first, disk_warn_percent=95)
        with patch.object(data, "collect_server") as collect:
            self.assertFalse(data.detail(keys["a"], False)["can_recheck"])
            self.assertEqual(data.recheck_work_item(keys["a"], request), {"status":"unavailable"})
            collect.assert_not_called()

    def test_schedule_counts_missed_slots_and_partial_samples_and_pause_stops_claims(self):
        now = 1000
        schedule = MonitorSchedules(self.data.demo, self.data.demo._record_host, synthetic=True, clock=lambda: now)
        schedule.configure(DEMO_HOST.key, 1, True)
        now = 1180
        schedule.run_due([DEMO_HOST], lambda _: simulate_host("missing"))
        history = self.data.demo.monitor_history(DEMO_HOST)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["trigger"], "scheduled")
        self.assertIsNone(history[0]["metrics"]["disk_percent"])
        self.assertEqual(schedule.describe(DEMO_HOST.key)["missed"], 3)
        schedule.configure(DEMO_HOST.key, 1, False)
        now = 1600
        schedule.run_due([DEMO_HOST], lambda _: self.fail("paused target ran"))
        self.assertEqual(schedule.describe(DEMO_HOST.key)["missed"], 3)

    def test_missed_intervals_break_the_pre_alert_disk_streak(self):
        def high(at):
            return {**simulate_host("high", datetime.fromtimestamp(at, timezone.utc)),
                    "trigger": "scheduled", "interval_minutes": 1}

        for at in (1000, 1120):
            self.data.demo.record_host(DEMO_HOST, high(at), synthetic=True)
        self.assertEqual(self.data.snapshot(True)["items"], [])
        self.data.demo.record_host(DEMO_HOST, high(1180), synthetic=True)
        self.assertEqual(len(self.data.snapshot(True)["items"]), 1)

    def test_generic_collector_uses_catalog_identity_and_only_valid_metrics(self):
        config = self.root / "services.json"
        config.write_text(json.dumps({"schema_version": 1, "services": [
            {"id": "edge-hosts", "kind": "python-adapter", "provider": "custom",
             "adapter": "example-monitor", "enabled": True, "settings": {}},
        ]}))
        monitors = self.root / "monitors.json"
        monitors.write_text(json.dumps([{"id": "edge-node", "alias": "test-node", "service_id": "edge-hosts",
                                         "resource_id": "rack-1.node-2", "product": "bare-metal"}]))

        class Adapter:
            provider = "custom"

            def __init__(self, _settings):
                pass

            def host_target(self, alias):
                return HostTarget("rack-1.node-2", "bare-metal")

            def host_reading(self, alias):
                return HostReading("2026-11-22T10:00:00Z", "example-probe",
                                   {"cpu_percent": 38, "memory_percent": 110, "extra": "internal"},
                                   "running")

        loader = Mock()
        loader.name = "example-monitor"
        loader.load.return_value = Adapter
        data = DashboardData(self.root / "generic", config, False, host_monitor_config=monitors)
        with patch("stackorion.catalog.entry_points", return_value=[loader]):
            target = data.run_host("edge-node", False, "healthy")
            history = target["history"][0]
            self.assertEqual(target["product"], "bare-metal")
            self.assertEqual(target["provider"], "custom")
            self.assertEqual(history["metrics"]["cpu_percent"]["value"], 38)
            self.assertIsNone(history["metrics"]["memory_percent"])
            self.assertNotIn("extra", history["metrics"])
            self.assertEqual(history["resource_state"], "running")
            self.assertEqual(history["status"], "unknown")
            with patch.object(Adapter, "host_target", return_value=HostTarget("other-node", "bare-metal")), \
                    patch.object(Adapter, "host_reading") as read:
                outcome = data.run_host("edge-node", False, "healthy")
                self.assertEqual(outcome["history"][0]["metrics"], {})
                read.assert_not_called()

    def test_panel_registration_binds_a_registered_collector_without_sampling(self):
        config = self.root / "services.json"
        config.write_text(json.dumps({"schema_version":1, "services":[
            {"id":f"edge-{name}", "kind":"python-adapter", "provider":"custom", "adapter":"example-monitor",
             "enabled":True, "settings":{"alias":name}} for name in ("alpha", "beta")
        ]}))
        original = config.read_bytes()
        reader = Mock(side_effect=lambda alias: HostReading(datetime.now(timezone.utc).isoformat(), "example-probe",
                      {"cpu_percent":25 if alias == "beta" else 95}, "running"))

        class Adapter:
            provider = "custom"

            def __init__(self, settings):
                self.alias = settings["alias"]

            def list_targets(self):
                return [self.alias]

            def host_target(self, alias):
                return HostTarget("same-node", "edge", "south-one")

            def host_reading(self, alias):
                return reader(alias)

        loader = Mock(name="loader")
        loader.name = "example-monitor"
        loader.load.return_value = Adapter
        state = self.root / "registered"
        with patch("stackorion.catalog.entry_points", return_value=[loader]):
            data = DashboardData(state, config, False)
            offers = {row["account"]:row for row in data.available_hosts()["targets"]}
            self.assertEqual(set(offers), {"live:custom:edge-alpha", "live:custom:edge-beta"})
            offer = offers["live:custom:edge-beta"]
            self.assertIsNone(data.register_host({"id":"host-" + 32 * "0"}))
            saved = data.register_host({"id":offer["id"]})
            self.assertEqual(data.register_host({"id":offer["id"]}), {"id":saved["id"], "status":"existing"})
            reader.assert_not_called()
            self.assertEqual([row["account"] for row in data.available_hosts()["targets"]], ["live:custom:edge-alpha"])
            self.assertEqual(data.host_link({"account":offer["account"], "region":offer["region"], "id":offer["resource_id"]}), saved["id"])
            data = DashboardData(state, config, False)
            self.assertFalse(data.host_targets(False)["targets"][0]["schedule"]["enabled"])
            data.run_host(saved["id"], False, "healthy")
            reader.assert_called_once_with("beta")
            [monitor] = data.host_targets(False)["targets"]
            self.assertEqual(monitor["history"][0]["metrics"]["cpu_percent"]["value"],25)
            with patch.object(Adapter, "host_target", return_value=HostTarget("wrong-node", "edge", "south-one")):
                self.assertEqual(data.run_host(saved["id"], False, "healthy")["history"][0]["metrics"], {})
            reader.assert_called_once()
        self.assertEqual(config.read_bytes(), original)

    def test_ssh_supplements_a_verified_target_and_preserves_cloud_state(self):
        stamp = "2026-11-23T00:00:00Z"
        target = HostTarget("registered-id", "las")
        adapter = Mock()
        adapter.host_target.return_value = target
        adapter.host_reading.return_value = HostReading(stamp, "las-api", {"cpu_percent": 8}, "running")
        catalog = Mock()
        catalog.get.return_value = Mock(id="las-service", adapter="test-adapter", provider="test", account_id="test")
        catalog.adapter.return_value = adapter
        ssh = Mock()
        ssh.read.return_value = HostReading(stamp, "linux-ssh", {
            "cpu_percent": 20, "memory_percent": 40, "disk_percent": 31,
            "network_rx_kbps": 0, "network_tx_kbps": 2,
        })
        monitor = AdapterHostMonitor("las-monitor", "fixture", "las-service", "test-adapter", "test",
                                     "test", target, None, ssh)

        result = collect_host(catalog, monitor)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["state_source"], "las-api")
        self.assertEqual(result["metrics"]["cpu_percent"]["value"], 8)
        self.assertEqual(result["metrics"]["network_rx_kbps"]["value"], 0)
        self.assertEqual(result["metrics"]["network_tx_kbps"]["source"], "linux-ssh")

        ssh.read.side_effect = CatalogError("identity unavailable")
        result = collect_host(catalog, monitor)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["resource_state"], "running")
        self.assertNotIn("disk_percent", result["metrics"])

        ssh.reset_mock()
        adapter.host_target.return_value = HostTarget("different-id", "las")
        self.assertEqual(collect_host(catalog, monitor)["metrics"], {})
        ssh.read.assert_not_called()

    def test_chart_history_respects_window_and_mock_visibility(self):
        old = datetime.now(timezone.utc) - timedelta(days=2)
        self.data.demo.record_host(DEMO_HOST, simulate_host("healthy", old), synthetic=True)
        self.data.run_host(DEMO_HOST.id, True, "healthy")
        self.assertIsNone(self.data.host_history(DEMO_HOST.id, False, 24))
        recent = self.data.host_history(DEMO_HOST.id, True, 1)
        self.assertEqual(len(recent["history"]), 1)
        self.assertFalse(recent["truncated"])
        self.assertEqual(recent["disk_projection"]["status"], "insufficient_samples")

    def test_disk_projection_uses_continuous_history_and_stops_after_missing_sample(self):
        now = datetime.now(timezone.utc)
        for hours, usage in reversed(list(enumerate((82, 79, 76, 73, 70)))):
            sample = simulate_host("healthy", now - timedelta(hours=hours))
            sample["metrics"]["disk_percent"]["value"] = usage
            self.data.demo.record_host(DEMO_HOST, sample, synthetic=True)
        long = self.data.host_history(DEMO_HOST.id, True, 24)["disk_projection"]
        self.assertEqual(long["status"], "projected")
        self.assertEqual((long["samples"], long["span_hours"], long["threshold"]), (5, 4, 85))
        self.assertEqual(long["projected_at"], (now + timedelta(hours=1)).isoformat())
        short = self.data.host_history(DEMO_HOST.id, True, 1)["disk_projection"]
        self.assertEqual(short["status"], "insufficient_samples")
        self.data.demo.record_host(DEMO_HOST, simulate_host("missing", now + timedelta(seconds=1)), synthetic=True)
        after = self.data.host_history(DEMO_HOST.id, True, 24)["disk_projection"]
        self.assertEqual(after["status"], "latest_missing")

    def test_disk_projection_suppresses_uncertain_extrapolations(self):
        now = datetime.now(timezone.utc)
        for values, expected in (
            ((72, 72, 72, 72, 72), "no_growth"),
            ((80, 69, 76, 64, 68), "unstable"),
            ((74, 73, 72, 71, 70), "outside_window"),
            ((85, 83, 81, 79, 77), "threshold_reached"),
        ):
            with self.subTest(expected=expected):
                history = [simulate_host("healthy", now - timedelta(hours=i)) for i in range(len(values))]
                for run, usage in zip(history, values):
                    run["metrics"]["disk_percent"]["value"] = usage
                estimate = disk_projection(history, 85, now, truncated=False)
                self.assertEqual(estimate["status"], expected)
                self.assertNotIn("projected_at", estimate)
        history[1]["metrics"]["disk_percent"]["source"] = "replacement"
        history[0]["metrics"]["disk_percent"]["value"] = 84
        self.assertEqual(disk_projection(history, 85, now, truncated=False)["status"], "source_changed")

    def fixture(self):
        config = self.root / "services.json"
        monitor = self.root / "host-monitors.json"
        settings = {"targets": [{"alias": "fixture-host", "instance_id": "i-registered"}]}
        conf = {"schema_version": 1, "accounts": [
            {"id": "qiniu-fixture", "provider": "qiniu", "label": "Fixture", "scope": "project",
             "verification": "declared"}],
            "services": [
                {"id": "capacity-fixture", "provider": "qiniu", "account_id": "qiniu-fixture",
                 "kind": "python-adapter", "adapter": "qiniu-host-capacity", "enabled": True,
                 "settings": {**settings, "script_path": "/tmp/capacity", "connection_file": "/tmp/connection"}},
                {"id": "qvm-fixture", "provider": "qiniu", "account_id": "qiniu-fixture",
                 "kind": "python-adapter", "adapter": "qiniu-qvm", "enabled": True,
                 "settings": {**settings, "script_path": "/tmp/qvm", "credential_file": "/tmp/credentials"}},
            ]}
        config.write_text(json.dumps(conf))
        monitor.write_text(json.dumps([{"id": "fixture", "alias": "fixture-host", "capacity_service": "capacity-fixture",
                                       "qvm_service": "qvm-fixture", "region": "cn-hangzhou"}]))
        return config, monitor, conf

    def test_live_binding_rejects_retarget_and_keeps_other_source_when_partial(self):
        config, monitor, conf = self.fixture()
        data = DashboardData(self.root / "live", config, False, host_monitor_config=monitor)
        stamp = "2026-01-01T00:00:00+00:00"
        point = {"value": 51, "source": "host", "observed_at": stamp}
        with patch("stackorion.qiniu_host_monitor.qvm_metrics", side_effect=CatalogError("unavailable")),                 patch("stackorion.qiniu_host_monitor.capacity_metrics", return_value={"memory_percent": point}) as capacity:
            row = data.run_host("fixture", False, "high")
            self.assertEqual(row["history"][0]["metrics"], {"memory_percent": point})
            self.assertEqual(row["history"][0]["status"], "unknown")
            self.assertNotIn("i-registered", json.dumps(row))
            for service in conf["services"]:
                service["settings"]["targets"] = [{"alias": "fixture-host", "instance_id": "i-new"}]
            config.write_text(json.dumps(conf))
            with patch("stackorion.qiniu_host_monitor.qvm_metrics") as qvm:
                self.assertEqual(data.run_host("fixture", False, "high")["history"][0]["metrics"], {})
                qvm.assert_not_called()
            self.assertEqual(capacity.call_count, 1)
