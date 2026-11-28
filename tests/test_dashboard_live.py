import json
from datetime import datetime, timedelta, timezone
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from stackorion.catalog import Catalog, CatalogError
from stackorion.billing_inspection import compare_closed_months
from stackorion.dashboard import DashboardData, DashboardHandler, demo_inspection, ops_endpoints


class DashboardLiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.credential = self.root / "local.key"
        self.credential.write_text("private-test-value")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "schema_version": 1,
            "services": [
                {"id": "tencent-test", "kind": "python-adapter", "provider": "tencent", "enabled": True,
                 "adapter": "tencent-management",
                 "settings": {"credential_file": str(self.credential), "regions": ["ap-guangzhou"]}},
                {"id": "qiniu-no-key", "kind": "python-adapter", "provider": "qiniu", "enabled": True,
                 "adapter": "qiniu-qvm", "settings": {"credential_file": str(self.root / "missing.key")}},
            ],
        }))
        self.adapter = Mock()
        self.adapter.balance.return_value = {"amounts": {"RealBalance": 8640}}
        self.adapter.security_groups.return_value = {"items": []}
        self.adapter.certificates.return_value = {"status": "complete", "items": []}

    def test_workbench_and_operations_screen_share_a_read_only_preview_api(self):
        state = self.root / "state"
        data = DashboardData(state, None, True)
        panel = ThreadingHTTPServer(("127.0.0.1", 0),
                                    partial(DashboardHandler, data=data, ops_endpoints=ops_endpoints(state, None, None)))
        self.addCleanup(panel.server_close)
        Thread(target=panel.serve_forever, daemon=True).start()
        self.addCleanup(panel.shutdown)
        client = HTTPConnection("127.0.0.1", panel.server_port, timeout=3)
        self.addCleanup(client.close)

        def request(method, path, headers=None):
            client.request(method, path, headers=headers or {})
            response = client.getresponse()
            return response.status, response.read()

        status, workbench = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/ops"', workbench)
        self.assertIn(b'id="work-items"', workbench)
        status, operations = request("GET", "/ops")
        self.assertEqual(status, 200)
        self.assertIn(b'id="ops-close"', operations)
        self.assertIn(b'href="/" aria-keyshortcuts="Escape"', operations)
        status, snapshot = request("GET", "/api/ops-preview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(snapshot)["inventory"]["source"], "synthetic")
        self.assertEqual(request("GET", "/api/ops-preview", {"Host": "other.test"})[0], 403)
        self.assertGreaterEqual(request("POST", "/api/ops-preview")[0], 400)

    def test_real_operations_screen_uses_registered_workbench_data(self):
        state = self.root / "state"
        data = DashboardData(state, None, False)
        data.register_website({"url": "https://example.com/health", "expected_status": 200, "contains": None})
        snapshot = ops_endpoints(state, None, None, workbench=data)["/api/ops-preview"]()
        self.assertEqual(snapshot["source"], "live")
        self.assertEqual(snapshot["inspection"]["status"], "empty")
        self.assertEqual(snapshot["items"], [])
        self.assertEqual(snapshot["inventory"]["assets"], [])
        self.assertEqual(snapshot["monitoring"]["websites"], [{
            "domain": "example.com", "observation": None, "unavailable": True, "certificate": None,
        }])

    def test_development_uses_live_and_only_falls_back_for_missing_credentials(self):
        data = DashboardData(self.root / "state", self.config, True)
        with patch.object(Catalog, "adapter", return_value=self.adapter) as lookup:
            state = data.run("daily", True)
        self.assertEqual({call.args[0].id for call in lookup.call_args_list}, {"tencent-test"})
        self.adapter.balance.assert_called_once_with()
        self.adapter.snapshot_backup_status.assert_not_called()
        account = next(row for row in state["report"]["accounts"] if row["id"].startswith("live:tencent"))
        self.assertEqual(account["balance"], {"metric": "RealBalance", "currency": "CNY", "amount": "86.4"})
        self.assertFalse(any(row.get("data_source") == "synthetic" and row["provider"] == "腾讯云"
                             for row in state["report"]["accounts"]))
        self.assertTrue(any(row.get("data_source") == "synthetic" and row["provider"] == "七牛云"
                            for row in state["report"]["accounts"]))
        raw = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("private-test-value", raw)
        self.assertNotIn(str(self.credential), raw)
        self.assertTrue(all(row.get("data_source") != "synthetic" for row in data.snapshot(False)["report"]["accounts"]))
        self.assertFalse(data.assets(False)["assets"])
        self.assertTrue(data.assets(True)["assets"])

    def test_domain_delivery_accepts_only_the_registered_live_connection_and_domain(self):
        services = [
            {"id": service_id, "kind": "python-adapter", "provider": provider, "enabled": True,
             "adapter": adapter, "settings": {"credential_file": str(self.credential)}}
            for service_id, provider, adapter in (
                ("registrar", "spaceship", "spaceship-management"),
                ("dns", "cloudflare", "cloudflare-domains"),
            )
        ]
        config = {"schema_version": 1, "services": services}
        self.config.write_text(json.dumps(config))
        data = DashboardData(self.root / "state", self.config, True)
        registrar, dns = Mock(), Mock()
        registrar.discover.return_value = {"status": "observed", "data": [
            {"name": "example.com", "nameservers": {"hosts": ["NS1.EXAMPLE.NET.", "ns2.example.net"]}},
        ]}
        dns.discover.return_value = {"status": "complete", "items": [
            {"name": "example.com", "name_servers": ["ns1.example.org", "ns2.example.org"]},
        ]}
        with patch.object(Catalog, "adapter", autospec=True, side_effect=lambda _, svc: (
            registrar if svc.id == "registrar" else dns
        )):
            data.run("daily", False)
        snapshot = data.assets(False)
        self.assertEqual({asset["service_id"] for asset in snapshot["assets"]}, {"registrar", "dns"})

        panel = ThreadingHTTPServer(("127.0.0.1", 0), partial(DashboardHandler, data=data))
        self.addCleanup(panel.server_close)
        Thread(target=panel.serve_forever, daemon=True).start()
        self.addCleanup(panel.shutdown)
        client = HTTPConnection("127.0.0.1", panel.server_port, timeout=3)
        self.addCleanup(client.close)
        origin = f"http://127.0.0.1:{panel.server_port}"

        def check(service_id, domain, request_origin=origin):
            client.request("POST", "/api/domain-delivery/check", json.dumps({
                "service_id": service_id, "domain": domain,
            }), {"Content-Type": "application/json", "Origin": request_origin})
            response = client.getresponse()
            return response.status, response.read()

        with patch("stackorion.dashboard.observe", return_value={"status": "observed"}) as observe:
            self.assertEqual(check("dns", "example.com", "https://other.example")[0], 403)
            self.assertEqual(check("dns", "example.net")[0], 404)
            self.assertEqual(check("dns", "127.0.0.1")[0], 404)
            observe.assert_not_called()
            status, result = check("dns", "example.com")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(result)["provider_observed_at"], snapshot["snapshot_at"])
            observe.assert_called_once_with("example.com", ["ns1.example.org", "ns2.example.org"])
            observe.reset_mock()
            status, result = check("registrar", "example.com")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(result)["provider"], "Spaceship")
            observe.assert_called_once_with("example.com", ["ns1.example.net", "ns2.example.net"])
            observe.reset_mock()
            services[0]["enabled"] = False
            self.config.write_text(json.dumps(config))
            self.assertEqual(check("registrar", "example.com")[0], 404)
            observe.assert_not_called()

    def test_asset_links_distinguish_accounts_regions_and_domain_scopes(self):
        panel = DashboardData(self.root / "route-state", self.config, True)
        report = demo_inspection("daily")
        account = report["accounts"][0]["id"]
        server = {"kind": "server", "id": "same-id", "name": "example", "account": account,
                  "region": "ap-guangzhou", "data_source": "live"}
        domain = {"kind": "domain", "id": "example.com", "name": "example.com", "account": account,
                  "scope": "cf:域名:configured", "data_source": "live"}
        legacy_domain = {**domain, "account": report["accounts"][1]["id"]}
        del legacy_domain["scope"]
        report["assets"] = [server, {**server, "region": "ap-shanghai"}, domain,
                            {**domain, "scope": "spaceship:域名:configured"}, legacy_domain]
        self.assertTrue(panel.live.record(report)["recorded"])
        assets = panel.assets(False)["assets"]
        self.assertEqual(len({asset["route_id"] for asset in assets}), 5)
        self.assertTrue(all(len(asset["route_id"]) == 64 for asset in assets))
        routes = [asset["route_id"] for asset in assets]
        report["observed_at"] = (datetime.fromisoformat(report["observed_at"]) + timedelta(minutes=1)).isoformat()
        report["assets"][0] = {**server, "name": "renamed"}
        report["assets"][4] = {**legacy_domain, "scope": "other-cf:域名:configured"}
        self.assertTrue(panel.live.record(report)["recorded"])
        self.assertEqual([asset["route_id"] for asset in panel.assets(False)["assets"]], routes)
        report["observed_at"] = (datetime.fromisoformat(report["observed_at"]) + timedelta(minutes=1)).isoformat()
        report["assets"].append(report["assets"][0].copy())
        self.assertTrue(panel.live.record(report)["recorded"])
        self.assertEqual(sum(asset["route_id"] is None for asset in panel.assets(False)["assets"]), 2)

    def test_snapshot_backup_inventory_only_for_opted_in_tencent_service(self):
        config = json.loads(self.config.read_text())
        config["services"][0]["settings"]["backup_inventory"] = True
        self.config.write_text(json.dumps(config))
        data = DashboardData(self.root / "state", self.config, False)
        self.assertEqual(data.assets(False)["backup_inventory_status"], "pending")
        self.adapter.snapshot_backup_status.return_value = {
            "status": "partial", "items": [{"policy_id": "asp-fixture", "disk_id": "disk-fixture",
                "active": True, "next_trigger": "2026-11-24T08:00:00", "snapshot_status": "unavailable"}],
        }
        with patch.object(Catalog, "adapter", return_value=self.adapter):
            report = data.run("daily", False)["report"]
        self.adapter.snapshot_backup_status.assert_called_once_with("ap-guangzhou")
        self.assertEqual(report["accounts"][0]["checks"]["云盘备份"], "partial")
        asset, = data.assets(False)["assets"]
        self.assertEqual((asset["kind"], asset["region"], asset["id"]),
                         ("backup", "ap-guangzhou", "asp-fixture:disk-fixture"))
        self.assertEqual(data.assets(False)["backup_inventory_status"], "partial")

    def test_snapshot_backup_empty_inventory_distinguishes_disabled_and_missing(self):
        data = DashboardData(self.root / "state", self.config, False)
        self.assertEqual(data.assets(False)["backup_inventory_status"], "disabled")
        config = json.loads(self.config.read_text())
        config["services"][0]["settings"]["backup_inventory"] = True
        self.config.write_text(json.dumps(config))
        self.assertEqual(data.assets(False)["backup_inventory_status"], "pending")
        with patch.object(Catalog, "adapter", return_value=self.adapter):
            self.adapter.snapshot_backup_status.return_value = {"status": "complete", "items": []}
            data.run("daily", False)
            self.assertEqual(data.assets(False)["backup_inventory_status"], "complete")
            self.adapter.snapshot_backup_status.side_effect = CatalogError("Tencent request failed: PermissionDenied")
            data.run("daily", False)
            self.assertEqual(data.assets(False)["backup_inventory_status"], "unavailable")

    def test_billing_anomaly_retains_evidence_until_same_product_is_comparable(self):
        config = json.loads(self.config.read_text())
        config["services"][0]["settings"]["billing_anomaly"] = True
        self.config.write_text(json.dumps(config))
        data = DashboardData(self.root / "state", self.config, False)

        def bills(month, amount, products):
            return {"status": "complete", "month": month, "unit": "CNY yuan",
                    "summary": {"RealTotalCost": amount},
                    "products": [{"BusinessCode": code, "RealTotalCost": cost} for code, cost in products.items()]}

        with patch.object(Catalog, "adapter", return_value=self.adapter):
            self.adapter.billing_summary.side_effect = [
                bills("2026-10", "390", {"p_cvm": "300", "p_new": "90"}),
                bills("2026-09", "200", {"p_cvm": "100", "p_cbs": "100"}),
                {"status": "pending"},
                bills("2026-09", "200", {"p_cvm": "100", "p_cbs": "100"}),
                bills("2026-10", "215", {"p_new": "215"}),
                bills("2026-09", "200", {"p_cvm": "100", "p_cbs": "100"}),
                bills("2026-10", "205", {"p_cvm": "105", "p_cbs": "100"}),
                bills("2026-09", "200", {"p_cvm": "100", "p_cbs": "100"}),
            ]
            with patch("stackorion.live_dashboard.datetime", wraps=datetime) as clock:
                moment = datetime(2026, 11, 1, tzinfo=timezone.utc)
                clock.now.side_effect = [moment + timedelta(seconds=n) for n in range(4)]
                first = data.run("daily", False)
                self.assertEqual(first["report"]["counts"]["billing"], 2)
                comparison, = first["report"]["accounts"][0]["billing_checks"]
                self.assertEqual(comparison["unmatched_products"], 2)
                expenses = {item["evidence"]["id"]: item["id"] for item in first["items"]
                            if item["evidence"]["kind"] == "billing"}
                pending = data.run("daily", False)
                self.assertEqual(pending["report"]["accounts"][0]["billing_checks"][0]["recent"]["status"], "pending")
                self.assertTrue(all(data.detail(key, False)["status"] == "open" for key in expenses.values()))
                data.run("daily", False)
                self.assertEqual(data.detail(expenses["account-total"], False)["status"], "resolved")
                self.assertEqual(data.detail(expenses["product:p_cvm"], False)["status"], "open")
                data.run("daily", False)
        self.assertEqual(data.detail(expenses["product:p_cvm"], False)["status"], "resolved")

    def test_billing_month_boundary_and_both_alert_thresholds(self):
        def bill(month):
            recent = month == "2026-10"
            costs = {"p_zero": "100" if recent else "0", "p_small": "1099" if recent else "1000",
                     "p_slow": "1220" if recent else "1000"}
            return {"status": "complete", "unit": "CNY yuan", "month": month,
                    "summary": {"RealTotalCost": str(sum(int(value) for value in costs.values()))},
                    "products": [{"BusinessCode": code, "RealTotalCost": cost} for code, cost in costs.items()]}

        self.adapter.billing_summary.side_effect = bill
        now = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
        status, evidence, hits = compare_closed_months(self.adapter, now)
        self.assertEqual(status, "complete")
        self.assertEqual((evidence["recent"]["month"], evidence["prior"]["month"]), ("2026-10", "2026-09"))
        self.assertEqual([(row["id"], row["growth_percent"]) for row in hits], [("product:p_zero", None)])

    def test_existing_credentials_with_permission_failure_are_not_simulated(self):
        self.adapter.balance.side_effect = CatalogError("private-test-value")
        data = DashboardData(self.root / "state", self.config, True)
        with patch.object(Catalog, "adapter", return_value=self.adapter):
            state = data.run("daily", True)
        report = state["report"]
        account = next(row for row in report["accounts"] if row["provider"] == "腾讯云")
        self.assertEqual(account["checks"]["账务"], "unavailable")
        self.assertEqual(account["status"], "partial")
        self.assertNotIn("private-test-value", json.dumps(report))

    def test_development_disabled_ignores_mock_request(self):
        root = self.root / "state"
        with patch.object(Catalog, "adapter", return_value=self.adapter):
            demo = DashboardData(root, self.config, True)
            mixed = demo.run("daily", True)
            demo_id = next(row["id"] for row in mixed["items"] if row["evidence"]["data_source"] == "synthetic")
            data = DashboardData(root, self.config, False)
            state = data.run("daily", True)
        self.assertFalse(state["development"])
        self.assertTrue(all(row.get("data_source") != "synthetic" for row in state["report"]["accounts"]))
        self.assertEqual(data.assets(True)["assets"], [])
        self.assertIsNone(data.detail(demo_id, True))

    def test_domain_renewal_requires_known_date_from_same_registrar_connection(self):
        services = [
            {"id": "registrar", "kind": "python-adapter", "provider": "spaceship", "enabled": True,
             "adapter": "spaceship-management", "settings": {"credential_file": str(self.credential)}},
            {"id": "dns-host", "kind": "python-adapter", "provider": "cloudflare", "enabled": True,
             "adapter": "cloudflare-domains", "settings": {"credential_file": str(self.credential)}},
        ]
        self.config.write_text(json.dumps({"schema_version": 1, "services": services}))
        registrar = Mock()
        dns = Mock()
        name = "example.com"
        dns.discover.return_value = {"status": "complete", "items": [{"name": name, "expirationDate": "2040-01-01T00:00:00Z"}]}
        now = datetime.now(timezone.utc)
        near, renewed = [(now + timedelta(days=days)).isoformat() for days in (12, 365)]
        registrar.discover.side_effect = [
            {"status": "complete", "data": [{"name": name, "expirationDate": near, "autoRenew": True}]},
            {"status": "complete", "data": [{"name": name, "expirationDate": "invalid", "autoRenew": True}]},
            {"status": "complete", "data": []},
            {"status": "complete", "data": [{"name": name, "expirationDate": renewed, "autoRenew": False}]},
        ]
        data = DashboardData(self.root / "state", self.config, False)
        with patch.object(Catalog, "adapter", side_effect=lambda service: registrar if service.id == "registrar" else dns):
            first = data.run("daily", False)
            item, = first["items"]
            self.assertEqual(item["evidence"]["resource_kind"], "domain")
            self.assertTrue(item["evidence"]["auto_renew"])
            self.assertEqual(item["evidence"]["expires_at"], near)
            self.assertEqual(first["report"]["counts"]["expiry"], 1)
            unknown = data.run("daily", False)
            self.assertEqual(unknown["report"]["coverage"]["partial"], 1)
            self.assertEqual(data.detail(item["id"], False)["status"], "open")
            data.run("daily", False)
            self.assertEqual(data.detail(item["id"], False)["status"], "open")
            data.run("daily", False)
        detail = data.detail(item["id"], False)
        self.assertEqual(detail["status"], "resolved")
        self.assertEqual([row["event"] for row in detail["history"]], ["resolved", "retained", "retained", "opened"])

    def test_inventory_certificate_rechecks_exact_id_once_per_account(self):
        data = DashboardData(self.root / "state", self.config, False)
        config = json.loads(self.config.read_text())
        config["services"][0]["settings"]["regions"].append("ap-shanghai")
        self.config.write_text(json.dumps(config))
        local_now = datetime.now(timezone(timedelta(hours=8)))
        near, renewed = [(local_now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") for days in (5, 90)]
        old = {"CertificateId": "old-cert", "Domain": "example.com"}
        other = {"CertificateId": "new-cert", "Domain": "example.com", "CertEndTime": renewed}
        self.adapter.certificates.side_effect = [
            {"status": "complete", "items": [{**old, "CertEndTime": near}]},
            {"status": "complete", "items": [old, other]},
            {"status": "complete", "items": [other]},
            {"status": "complete", "items": [{**old, "CertEndTime": renewed}, other]},
        ]
        with patch.object(Catalog, "adapter", return_value=self.adapter):
            first = data.run("daily", False)
            item, = [entry for entry in first["items"] if entry["evidence"]["kind"] == "expiry"]
            self.assertEqual(item["evidence"]["resource_kind"], "certificate")
            self.assertEqual(item["evidence"]["expires_at"], near.replace(" ", "T") + "+08:00")
            self.adapter.certificates.assert_called_once_with()
            self.assertEqual(data.assets(False)["assets"][0]["kind"], "certificate")
            self.assertEqual(data.run("daily", False)["report"]["accounts"][0]["checks"]["库存证书"], "partial")
            self.assertEqual(data.detail(item["id"], False)["status"], "open")
            data.run("daily", False)
            self.assertEqual(data.detail(item["id"], False)["status"], "open")
            data.run("daily", False)
        self.assertEqual(data.detail(item["id"], False)["status"], "resolved")
