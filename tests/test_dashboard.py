import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import Client

from stackorion.dashboard import DashboardData, build_demo_mcp, demo_assets, demo_inspection
from stackorion.inspection import balance_status, expiry_status, is_public_ssh


class InspectionTests(unittest.TestCase):
    def test_balance_below_its_own_threshold(self):
        for amount, expected in [("99.99", "low"), ("100.00", "normal"), ("100.01", "normal")]:
            self.assertEqual(balance_status(amount, "100.00"), expected)
        self.assertEqual(balance_status("0", None), "unconfigured")

    def test_expiry_bounded_to_seven_days_and_absolute_times(self):
        now = datetime.fromisoformat("2026-11-23T09:30:00+08:00")
        for value, billing, expected in [
            ("2026-11-22T09:30:00+08:00", "prepaid", "expired"),
            ("2026-11-30T09:30:00+08:00", "prepaid", "due"),
            ("2026-11-30T01:30:00+00:00", "prepaid", "due"),
            ("2026-11-30T09:30:01+08:00", "prepaid", "normal"),
            (None, "postpaid", "not_applicable"),
            (None, "prepaid", "unknown"),
        ]:
            self.assertEqual(expiry_status(value, billing, now), expected)

    def test_public_ssh_matches_rule_not_connectivity(self):
        for rule, expected in [
            (("ingress", "allow", "tcp", "0.0.0.0/0", 22, 22), True),
            (("ingress", "allow", "tcp", "::/0", 20, 24), True),
            (("ingress", "allow", "all", "::/0", 0, 65535), True),
            (("egress", "allow", "tcp", "0.0.0.0/0", 22, 22), False),
            (("ingress", "deny", "tcp", "0.0.0.0/0", 22, 22), False),
            (("ingress", "allow", "tcp", "192.0.2.0/24", 22, 22), False),
            (("ingress", "allow", "udp", "0.0.0.0/0", 22, 22), False),
        ]:
            self.assertIs(is_public_ssh(*rule), expected)

    def test_asset_types_and_provider_products_remain_separate(self):
        catalog = demo_assets()
        self.assertEqual(catalog["source"], "synthetic")
        assets = {row["id"]: row for row in catalog["assets"]}
        self.assertEqual(len(assets), 8)
        self.assertEqual(assets["domain-example-com"]["registrar"], "Spaceship")
        self.assertEqual(assets["domain-example-com"]["dns_provider"], "腾讯云 DNSPod")
        self.assertEqual(assets["lhins-demo-01"]["server_type"], "lighthouse")
        self.assertEqual(assets["lhins-demo-01"]["product"], "腾讯云 Lighthouse")
        self.assertEqual(assets["ins-web-prod-01"]["server_type"], "cloud")
        self.assertEqual(assets["qvm-edge-01"]["server_type"], "cloud")
        self.assertEqual(assets["sg-prod-web"]["kind"], "security_group")

    def test_fixture_counts_and_coverage(self):
        report = demo_inspection()
        self.assertEqual(report["counts"], {"balance": 1, "expiry": 1, "ssh": 1, "coverage": 2})
        self.assertEqual(report["expected_accounts"], 5)
        self.assertEqual(report["coverage"], {"complete": 3, "partial": 1, "unavailable": 1, "disabled": 1})
        self.assertEqual({row["status"] for row in report["findings"] if row["kind"] == "coverage"},
                         {"partial", "unavailable"})
        empty = demo_inspection("empty")
        self.assertEqual(empty["findings"], [])
        self.assertEqual(empty["coverage"]["complete"], 5)

    def test_dashboard_inspection_summary_reads_same_run_and_opts_in_to_demo(self):
        with TemporaryDirectory() as temporary:
            data = DashboardData(Path(temporary), None, True)
            self.assertEqual(data.inspection_summary(False), {"status":"empty", "expected_accounts":0})
            snapshot = data.run("daily", True)["report"]
            real = data.inspection_summary(False)
            self.assertEqual(real["expected_accounts"], 0)
            self.assertNotIn("simulated_at", real)
            summary = data.inspection_summary(True)
            for key in ("counts", "coverage", "expected_accounts", "simulated_at"):
                self.assertEqual(summary[key], snapshot[key])
            self.assertTrue(all(row["data_source"] == "synthetic" for row in summary["accounts"]))
            self.assertNotIn("findings", summary)


class DemoMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_report_and_read_only_demo_surface(self):
        async with Client(build_demo_mcp()) as client:
            tools = await client.list_tools()
            self.assertEqual([tool.name for tool in tools.tools], ["run_demo_inspection"])
            self.assertTrue(tools.tools[0].annotations.read_only_hint)
            response = await client.call_tool("run_demo_inspection", {})
            self.assertEqual(response.structured_content, demo_inspection())
