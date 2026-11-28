from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from stackorion.host_monitor import AdapterHostMonitor
from stackorion.host_contract import HostReading, HostTarget
from stackorion.ops_live_hosts import OpsLiveHosts
from stackorion.qiniu_account_reads import QiniuAccountReads


class QiniuAccountReadTests(unittest.TestCase):
    def setUp(self):
        self.account = QiniuAccountReads("/local/secret.env", ["cn-hongkong-2"])

    def test_cdn_paginates_and_projects_only_display_fields(self):
        domain = {"name": "cdn.example.org", "operatingState": "success", "protocol": "https", "oemMail": "hidden"}
        pages = [{"domains": [domain], "marker": "a+b"}, {"domains": [], "marker": ""}]
        with patch.object(self.account, "_get", side_effect=pages) as get:
            self.assertEqual(self.account._cdn(), [{"name": "cdn.example.org", "state": "success", "protocol": "https"}])
            self.assertEqual(get.call_args.args, ("api.qiniu.com", "/domain?limit=100&marker=a%2Bb"))

    def test_statement_uses_previous_available_period_and_exact_money(self):
        january_4 = datetime(2026, 1, 4, tzinfo=timezone.utc)
        row = {"product": "Kodo", "itemDesc": "Storage", "currency": "CNY", "type": "bill",
               "payStatus": "paid", "fee": 1995000001, "billID": "hidden"}
        with patch.object(self.account, "_finance", return_value=[row]) as fetch:
            result = self.account._bill(january_4)
        self.assertEqual(result["period"], "2025-11")
        self.assertEqual(result["rows"][0]["fee"], "19.95000001")
        self.assertNotIn("billID", result["rows"][0])
        self.assertIn("end=2025-12-01", fetch.call_args.args[0])

    def test_unavailable_region_is_not_an_empty_inventory(self):
        denied = HTTPError(None, 403, "Forbidden", {}, None)
        with patch.object(self.account, "_las", side_effect=denied), \
                patch.object(self.account, "_buckets", return_value=["bucket"]), \
                patch.object(self.account, "_cdn", return_value=[]), \
                patch.object(self.account, "_balance", return_value={}), \
                patch.object(self.account, "_bill", return_value={}):
            result = self.account.snapshot()
            self.assertEqual(result["las"]["cn-hongkong-2"], {"status": "permission_denied"})
            self.assertEqual(result["kodo"]["data"], ["bucket"])
            self.assertEqual(result["cdn"]["data"], [])
            self.assertIs(result, self.account.snapshot())


class OpsGuestMetricsTests(unittest.TestCase):
    def test_merges_only_guest_usage_for_the_same_account_and_target(self):
        target = HostTarget("instance", "qvm", "cn-hongkong-2")
        sample_time = "2026-11-28T10:00:00+00:00"
        collector = Mock()
        collector.read.return_value = HostReading(sample_time, "linux-ssh", {
            "cpu_percent": 99, "memory_percent": 22, "disk_percent": 44,
        })
        monitor = AdapterHostMonitor("monitor", "qvm", "qvm-readonly", "qiniu-qvm", "qiniu", "account", target,
                                     ssh_metrics=collector)
        catalog = Mock()
        catalog.list.return_value = [{"id": "qvm-readonly", "enabled": True, "adapter": "qiniu-qvm"}]
        service = catalog.get.return_value
        service.account_id = "different"
        adapter = catalog.adapter.return_value
        adapter.list_targets.return_value = ["card"]
        adapter.host_target.return_value = target
        adapter.metrics.side_effect = lambda *a, **kw: {"values": {"cpu_percent": 10}, "status": "observed"}
        self.assertEqual(OpsLiveHosts(catalog, [monitor]).snapshot()["hosts"][0]["sample"]["values"], {"cpu_percent": 10})
        collector.read.assert_not_called()
        service.account_id = "account"
        sample = OpsLiveHosts(catalog, [monitor]).snapshot()["hosts"][0]["sample"]
        collector.read.assert_called_once_with("instance")
        self.assertEqual(sample["values"], {"cpu_percent": 10, "memory_percent": 22, "disk_percent": 44})
        self.assertEqual(sample["guest_observed_at"], sample_time)
