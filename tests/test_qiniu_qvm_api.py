from contextlib import redirect_stdout
import base64
from datetime import datetime, timedelta, timezone
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from mcp import Client

from stackorion.catalog import Catalog, CatalogError
from stackorion.cli import main
from stackorion.dashboard import DashboardData
from stackorion.operations import OperationRegistry
from stackorion.qiniu import QiniuQVMAdapter
from stackorion.server import build_server


class QVMNativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.credential = self.root / "test.env"
        self.credential.write_text('export QINIU_AK="fixture-access-abc"\nQINIU_SK=fixture-secret-abc\n')
        self.credential.chmod(0o600)
        self.settings = {"credential_file": str(self.credential),
                         "targets": [{"alias": "main", "instance_id": "i-fixture"}]}
        self.config = self.root / "services.json"
        self.config.write_text(json.dumps({"schema_version": 1, "services": [{
            "id": "qvm-test", "adapter": "qiniu-qvm", "provider": "qiniu", "enabled": True,
            "kind": "python-adapter", "settings": self.settings,
        }]}))
        self.catalog = Catalog.load(self.config)
        self.requests = []
        self.vm = {
            "instance_id": "i-fixture", "instance_name": "fixture", "region_id": "cn-fixture",
            "instance_charge_type": "PostPaid", "security_group_ids": {"security_group_id": ["sg-fixture"]},
            "eip_address": {"ip_address": "192.0.2.1", "password": "unexpected-field"},
            "vpc_attributes": {"vpc_id": "vpc-fixture"},
            "password": "unexpected-field",
        }
        binding = {"instance_id": "i-fixture", "region_id": "cn-fixture"}
        self.routes = {
            "/v1/vm/instance": {"total": 1, "data": [self.vm]},
            "/v1/vm/eip": {"total": 1, "data": [{"allocation_id": "eip-fixture", **binding}]},
            "/v1/vm/vpc": {"total": 1, "data": [{"vpc_id": "vpc-fixture", "region_id": "cn-fixture"}]},
            "/v1/vm/instance/i-fixture": {"data": self.vm},
            "/v1/vm/disk": {"total": 1, "data": [{"disk_id": "d-fixture", "size": 50, **binding}]},
            "/v1/vpc/common_bandwidth": {"total": 1, "data": [{
                "cb_id": "cb-fixture", "region_id": "cn-fixture", "public_address": [{"eip_id": "eip-fixture"}],
            }]},
            "/v1/vm/sgr/sg-fixture/ingress": {"total": 1, "data": [{
                "security_group_rule_id": "rule-fixture", "ip_protocol": "TCP",
                "policy": "Accept", "port_range": "22/22", "source_cidr_ip": "0.0.0.0/0",
            }]},
            "/v1/vm/sgr/sg-fixture/egress": {"total": 0, "data": []},
        }
        self.enterContext(patch.dict(os.environ, {"STACKORION_CONFIG": str(self.config)}))
        opener = Mock(open=self.open_fixture)
        self.enterContext(patch("stackorion.qiniu_qvm_api.build_opener", return_value=opener))
        self.helper = self.enterContext(patch("stackorion.qiniu.run_qiniu_helper"))

    def open_fixture(self, request, *, timeout):
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertGreater(timeout, 0)
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc), ("https", "api-qvm.qiniu.com"))
        self.assertTrue(request.get_header("Authorization").startswith("Qiniu fixture-access-abc:"))
        if url.path == "/v1/vm/instance" and url.query == "page=1&size=100":
            self.assertEqual(request.get_header("Authorization"),
                             "Qiniu fixture-access-abc:NZobZQ6k-NblvXWw52nFEzj9hOg=")
        params = parse_qs(url.query)
        self.requests.append((url.path, params))
        if url.path == "/v1/vm/monitor/instance":
            self.assertEqual(request.get_header("X-qiniu-regionid"), "cn-fixture")
            self.assertEqual(params["instance_id"], ["i-fixture"])
            self.assertEqual(int(params["end_time"][0]) - int(params["start_time"][0]), 900)
            message = f"GET {url.path}?{url.query}\nHost: api-qvm.qiniu.com\nX-Qiniu-Regionid: cn-fixture\n\n"
            signature = base64.urlsafe_b64encode(hmac.digest(b"fixture-secret-abc", message.encode(), "sha1")).decode()
            self.assertEqual(request.get_header("Authorization"), f"Qiniu fixture-access-abc:{signature}")
        if url.path == "/v1/vm/instance/i-fixture" and params.get("region_id") != ["cn-fixture"]:
            raise HTTPError(request.full_url, 400, "region_id is required", {}, io.BytesIO())
        value = self.routes[url.path]
        if callable(value):
            value = value(params)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(json.dumps(value).encode())

    def adapter(self):
        return self.catalog.adapter(self.catalog.get("qvm-test"))

    def test_paginated_inventory_keeps_rows_on_later_failure(self):
        def instances(params):
            if params["page"] == ["1"]:
                return {"page": 1, "size": 1, "total": 2, "data": [self.vm]}
            return HTTPError("", 503, "sensitive upstream error", {}, io.BytesIO())

        self.routes["/v1/vm/instance"] = instances
        self.routes["/v1/vm/eip"] = {"total": 1, "data": []}
        result = self.adapter().discover()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["instances"]["reason"], "http_503")
        self.assertEqual(result["data"]["instances"]["returned_count"], 1)
        self.assertEqual(result["data"]["eips"]["status"], "unavailable")
        self.assertEqual(result["data"]["vpcs"]["status"], "observed")
        self.assertNotIn("sensitive upstream error", json.dumps(result))
        self.assertNotIn("unexpected-field", json.dumps(result))

    def test_inventory_pagination_cap_and_duplicate_pages(self):
        def instances(params):
            page = int(params["page"][0])
            return {"total": 1001, "data": [
                {"instance_id": f"i-{page}-{row}"} for row in range(100)
            ]}

        self.routes["/v1/vm/instance"] = instances
        result = self.adapter().discover()["data"]["instances"]
        self.assertEqual((result["status"], result["reason"], result["returned_count"]),
                         ("partial", "page_limit", 1000))
        self.routes["/v1/vm/instance"] = {"total": 2, "size": 1, "data": [self.vm]}
        result = self.adapter().discover()["data"]["instances"]
        self.assertEqual((result["status"], result["reason"], result["returned_count"]),
                         ("partial", "duplicate_resource", 1))

    def test_exact_detail_and_related_request_scope(self):
        disk_path = "/v1/vm/disk"
        self.routes[disk_path] = HTTPError("", 403, "denied", {}, io.BytesIO())
        bandwidth = self.routes["/v1/vpc/common_bandwidth"]
        bandwidth["data"].append({"cb_id": "cb-other", "region_id": "cn-fixture",
                                  "public_address": [{"eip_id": "eip-other"}]})
        bandwidth["total"] = 2
        result = self.adapter().inspect("main")
        self.assertEqual(self.requests[:2], [
            ("/v1/vm/instance", {"instance_id": ["i-fixture"], "page": ["1"], "size": ["100"]}),
            ("/v1/vm/instance/i-fixture", {"region_id": ["cn-fixture"]}),
        ])
        related = dict(self.requests[2:])
        for params in related.values():
            self.assertEqual(params["region_id"], ["cn-fixture"])
        for path in (disk_path, "/v1/vm/eip"):
            self.assertEqual(related[path]["instance_id"], ["i-fixture"])
        self.assertEqual(related["/v1/vm/vpc"]["vpc_id"], ["vpc-fixture"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["disks"]["reason"], "http_403")
        self.assertEqual(result["coverage"]["egress:sg-fixture"]["status"], "observed")
        self.assertEqual(result["data"]["security_group_ingress"]["sg-fixture"][0]["protocol"], "TCP")
        self.assertEqual([row["bandwidth_package_id"] for row in result["data"]["common_bandwidth"]], ["cb-fixture"])
        self.assertEqual(result["coverage"]["common_bandwidth"]["status"], "observed")
        self.assertNotIn("unexpected-field", json.dumps(result))
        self.requests.clear()
        for substitution in ({"instance_id": "i-other"}, {"region_id": "cn-other"}):
            self.routes["/v1/vm/instance/i-fixture"] = {"data": {**self.vm, **substitution}}
            with self.assertRaisesRegex(CatalogError, "target mismatch"):
                self.adapter().inspect("main")
        with self.assertRaisesRegex(CatalogError, "unknown QVM target"):
            self.adapter().inspect("i-other")
        self.assertEqual(len(self.requests), 4)

    def test_instance_lookup_rejects_empty_ambiguous_and_unlocated_targets(self):
        for rows, reason in (
            ([], "one exact target"),
            ([self.vm, {**self.vm, "instance_id": "i-unrelated"}], "one exact target"),
            ([self.vm, {**self.vm, "region_id": "cn-other"}], "duplicate_resource"),
            ([{"instance_id": "i-fixture"}], "invalid region"),
        ):
            with self.subTest(reason=reason):
                self.requests.clear()
                self.routes["/v1/vm/instance"] = {"total": len(rows), "data": rows}
                with self.assertRaisesRegex(CatalogError, reason):
                    self.adapter().inspect("main")
                self.assertEqual([path for path, _ in self.requests], ["/v1/vm/instance"])

    def test_associations_keep_only_verified_rows_and_preserve_failed_page(self):
        eips = self.routes["/v1/vm/eip"]["data"]
        eips.extend([
            {"allocation_id": "foreign-eip", "region_id": "cn-fixture", "instance_id": "i-other"},
            {"allocation_id": "unverified-eip", "region_id": "cn-fixture"},
        ])
        self.routes["/v1/vm/eip"]["total"] = len(eips)
        vpcs = self.routes["/v1/vm/vpc"]["data"]
        vpcs.extend([{"vpc_id": "vpc-other", "region_id": "cn-fixture"}, {"vpc_id": "unknown-region"}])
        self.routes["/v1/vm/vpc"]["total"] = len(vpcs)
        disk = self.routes["/v1/vm/disk"]["data"][0]

        def disks(params):
            if params["page"] == ["1"]:
                return {"page": 1, "size": 1, "total": 2, "data": [disk]}
            return HTTPError("", 503, "denied", {}, io.BytesIO())

        self.routes["/v1/vm/disk"] = disks
        bandwidth = self.routes["/v1/vpc/common_bandwidth"]["data"]
        bandwidth.extend([
            {"cb_id": "unrelated", "region_id": "cn-fixture", "public_address": [{"eip_id": "foreign-eip"}]},
            {"cb_id": "membership-unknown", "region_id": "cn-fixture"},
            {"cb_id": "different-region", "region_id": "cn-other", "public_address": [{"eip_id": "eip-fixture"}]},
        ])
        self.routes["/v1/vpc/common_bandwidth"]["total"] = len(bandwidth)
        result = self.adapter().inspect("main")
        for name, key, value in (("eip_by_instance", "allocation_id", "eip-fixture"),
                                 ("vpcs", "vpc_id", "vpc-fixture"), ("disks", "disk_id", "d-fixture"),
                                 ("common_bandwidth", "bandwidth_package_id", "cb-fixture")):
            with self.subTest(collection=name):
                self.assertEqual([row[key] for row in result["data"][name]], [value])
                coverage = result["coverage"][name]
                self.assertEqual((coverage["status"], coverage["returned_count"]), ("partial", 1))
                self.assertEqual(coverage["reason"], "http_503" if name == "disks" else "association_unverified")

    def test_unverified_association_is_distinct_from_an_explicit_empty_binding(self):
        self.vm.pop("vpc_attributes")
        self.routes["/v1/vm/eip"] = {"total": 1, "data": [{"allocation_id": "missing-binding"}]}
        self.routes["/v1/vm/disk"] = {"total": 2, "data": [
            {"disk_id": "d-other", "instance_id": "i-other", "region_id": "cn-fixture"},
            {"disk_id": "d-unverified", "region_id": "cn-fixture"},
        ]}
        result = self.adapter().inspect("main")
        for name in ("eip_by_instance", "disks", "vpcs", "common_bandwidth"):
            self.assertEqual(result["data"][name], [])
            self.assertEqual(result["coverage"][name]["status"], "unavailable")
        self.assertFalse({"/v1/vm/vpc", "/v1/vpc/common_bandwidth"} & {path for path, _ in self.requests})
        self.vm["vpc_attributes"] = {"vpc_id": ""}
        self.routes["/v1/vm/eip"] = {"total": 0, "data": []}
        result = self.adapter().inspect("main")
        for name in ("eip_by_instance", "vpcs", "common_bandwidth"):
            self.assertEqual(result["coverage"][name]["status"], "observed")

    def test_detail_eip_reconciles_with_the_list_and_bandwidth_coverage(self):
        path = "/v1/vm/eip"
        [bound] = self.routes[path]["data"]
        for detail_id, rows, eip_state, bandwidth_state in (
            ("eip-fixture", [], "unavailable", "unavailable"),
            ("eip-detail", [bound], "partial", "partial"),
            ("", [], "observed", "observed"),
            ("eip-fixture", [bound], "observed", "observed"),
        ):
            with self.subTest(detail_id=detail_id, eip_state=eip_state):
                self.vm["eip_address"]["allocation_id"] = detail_id
                self.routes[path] = {"total": len(rows), "data": rows}
                self.requests.clear()
                result = self.adapter().inspect("main")
                eip, bandwidth = (result["coverage"][key] for key in ("eip_by_instance", "common_bandwidth"))
                self.assertEqual((eip["status"], bandwidth["status"]), (eip_state, bandwidth_state))
                self.assertEqual([row["allocation_id"] for row in result["data"]["eip_by_instance"]],
                                 [row["allocation_id"] for row in rows])
                self.assertEqual(result["status"], "observed" if eip_state == "observed" else "partial")
                if eip_state != "observed":
                    self.assertEqual(eip["reason"], "detail_eip_not_listed")
                    self.assertEqual(bandwidth["reason"], "eip_coverage_incomplete")
                self.assertEqual(any(route == "/v1/vpc/common_bandwidth" for route, _ in self.requests), bool(rows))
                self.assertEqual(result["data"]["disks"][0]["disk_id"], "d-fixture")
                self.assertEqual(result["data"]["vpcs"][0]["vpc_id"], "vpc-fixture")

        def failed_later(params):
            if params["page"] == ["1"]:
                return {"total": 2, "size": 1, "data": [bound]}
            return HTTPError("", 503, "denied", {}, io.BytesIO())

        self.vm["eip_address"]["allocation_id"] = "eip-detail"
        self.routes[path] = failed_later
        result = self.adapter().inspect("main")
        eip = result["coverage"]["eip_by_instance"]
        self.assertEqual((eip["status"], eip["reason"], eip["pages"], eip["returned_count"]),
                         ("partial", "http_503", 1, 1))
        self.assertEqual(result["coverage"]["common_bandwidth"]["status"], "partial")

    def test_asserted_bandwidth_membership_is_reconciled_by_both_ids(self):
        [eip] = self.routes["/v1/vm/eip"]["data"]
        eip["bandwidth_package_id"] = "cb-fixture"
        self.assertEqual(self.adapter().inspect("main")["coverage"]["common_bandwidth"]["status"], "observed")

        path = "/v1/vpc/common_bandwidth"
        self.routes[path] = {"total": 1, "data": [
            {"cb_id": "cb-fixture", "region_id": "cn-fixture", "public_address": [{"eip_id": "eip-other"}]},
        ]}
        result = self.adapter().inspect("main")
        self.assertEqual(result["data"]["common_bandwidth"], [])
        band = result["coverage"]["common_bandwidth"]
        self.assertEqual((band["status"], band["reason"]), ("unavailable", "eip_bandwidth_not_listed"))
        self.assertEqual(result["status"], "partial")

        self.routes[path]["data"].append({
            "cb_id": "cb-other", "region_id": "cn-fixture", "public_address": [{"eip_id": "eip-fixture"}],
        })
        self.routes[path]["total"] = 2
        result = self.adapter().inspect("main")
        self.assertEqual([row["bandwidth_package_id"] for row in result["data"]["common_bandwidth"]], ["cb-other"])
        band = result["coverage"]["common_bandwidth"]
        self.assertEqual((band["status"], band["reason"]), ("partial", "eip_bandwidth_not_listed"))

    def test_web_preserves_denied_partial_and_successful_empty_group_coverage(self):
        panel = DashboardData(self.root / "state", self.config, False)
        ingress = "/v1/vm/sgr/sg-fixture/ingress"
        success = self.routes[ingress]

        def collect():
            report = panel.run("daily", False)["report"]
            [account] = report["accounts"]
            return account, report["findings"]

        self.routes[ingress] = HTTPError("", 403, "denied", {}, io.BytesIO())
        account, _ = collect()
        self.assertEqual(account["checks"]["安全组"], "unavailable")
        [failed] = account["qvm_rule_coverage"]
        self.assertEqual((failed["status"], failed["denied"]), ("unavailable", True))
        self.assertNotIn("returned_count", failed)

        self.routes[ingress] = {"total": 0, "data": []}
        account, _ = collect()
        self.assertEqual(account["checks"]["安全组"], "partial")
        [empty] = account["qvm_rule_coverage"]
        self.assertEqual((empty["status"], empty["returned_count"]), ("observed", 0))
        self.assertEqual(account["complete_scopes"], ["qvm-test:实例:configured"])

        def partial(params):
            if params["page"] == ["1"]:
                return {**success, "size": 1, "total": 2}
            return HTTPError("", 403, "denied", {}, io.BytesIO())

        self.routes[ingress] = partial
        self.vm["security_group_ids"]["security_group_id"].append("sg-denied")
        self.routes["/v1/vm/sgr/sg-denied/ingress"] = HTTPError("", 403, "denied", {}, io.BytesIO())
        self.routes["/v1/vm/sgr/sg-denied/egress"] = {"total": 0, "data": []}
        account, findings = collect()
        incomplete, denied = account["qvm_rule_coverage"]
        self.assertEqual((incomplete["status"], incomplete["returned_count"]), ("partial", 1))
        self.assertEqual((denied["group"], denied["status"]), ("sg-denied", "unavailable"))
        self.assertEqual(account["checks"]["安全组"], "partial")
        self.assertTrue(any(row["kind"] == "ssh" and row["id"].startswith("sg-fixture:") for row in findings))

    def test_native_cannot_use_legacy_operations_and_explicit_helper_does_not_fallback(self):
        registry = OperationRegistry(self.catalog)
        self.assertEqual(registry.describe("qvm-test"),
                         {"service": "qvm-test", "status": "unsupported", "operations": []})
        receipt = {"identity": {"service": "qvm-test"}, "status": "success"}
        with patch("stackorion.qiniu_operations.operation_status", return_value=receipt):
            self.assertEqual(registry.service_receipt("qvm-test", "existing-receipt"), receipt)
        confirmation = {"expected_state": "a" * 64, "confirm_instance_id": "i-fixture",
                        "operation_id": "fixture-change"}
        for operation, params in (
            ("qvm.allow-port", {"port": 22, "cidr": "192.0.2.0/24", "description": "fixture"}),
            ("qvm.delete-ingress-rule", {"security_group_id": "sg-fixture", "rule_id": "rule-fixture"}),
        ):
            with self.assertRaisesRegex(CatalogError, "explicit script_path"):
                registry.plan("qvm-test", operation, alias="main", **params)
            with self.assertRaisesRegex(CatalogError, "explicit script_path"):
                registry.apply("qvm-test", operation, alias="main", **params, **confirmation)
        self.credential.chmod(0o644)
        with self.assertRaisesRegex(CatalogError, "credential file must be private"):
            self.adapter().discover()
        self.helper.assert_not_called()
        legacy = QiniuQVMAdapter({**self.settings, "script_path": "/invalid/legacy.py"})
        self.helper.side_effect = CatalogError("configured file is missing")
        with self.assertRaisesRegex(CatalogError, "configured file is missing"):
            legacy.discover()
        self.helper.assert_called_once_with(Path("/invalid/legacy.py"), self.credential, ["inventory"])
        self.assertFalse(self.requests)

    async def test_installed_cli_mcp_and_web_use_native_queries(self):
        for command, args in (("discover", []), ("inspect", ["main"])):
            with patch("sys.argv", ["stackorion", command, "qvm-test", *args]), redirect_stdout(io.StringIO()) as output:
                main()
            self.assertEqual(json.loads(output.getvalue())["status"], "observed")

        async with Client(build_server(self.catalog)) as client:
            result = await client.call_tool("list_service_capabilities", {"service_id": "qvm-test"})
            self.assertEqual({row["id"]: row["upstream"] for row in result.structured_content["queries"]},
                             {"inventory": "official-api", "instance": "official-api", "metrics": "official-api"})
            self.assertEqual(result.structured_content["operation_status"], "unsupported")
            self.assertEqual(result.structured_content["operations"], [])
            for tool, parameters in (
                ("discover_service", {}),
                ("inspect_target", {"target_alias": "main"}),
                ("query_service", {"query_id": "instance", "parameters": {"alias": "main"}}),
            ):
                result = await client.call_tool(tool, {"service_id": "qvm-test", **parameters})
                self.assertEqual(result.structured_content["status"], "observed")

        panel = DashboardData(self.root / "state", self.config, False)
        panel.run("daily", False)
        assets = panel.assets(False)["assets"]
        self.assertTrue(any(row["id"] == "i-fixture" for row in assets))
        self.assertTrue(any(row["id"] == "sg-fixture" for row in assets))
        self.helper.assert_not_called()

    async def test_metrics_from_cli_mcp_and_panel_without_helper_or_capacity_service(self):
        path = "/v1/vm/monitor/instance"
        stamp = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.routes[path] = {"data": {"monitor_data": {"instance_monitor_data": [
            {"time_stamp": stamp.isoformat(), "cpu": 0, "intranet_rx": 0, "intranet_tx": 42},
            {"time_stamp": (stamp - timedelta(minutes=1)).isoformat(), "cpu": 10},
        ]}}}
        with patch("sys.argv", ["stackorion", "metrics", "qvm-test", "main"]), redirect_stdout(io.StringIO()) as output:
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["values"], {"cpu_percent": 0, "network_rx_kbit": 0, "network_tx_kbit": 42})
        async with Client(build_server(self.catalog)) as client:
            reply = await client.call_tool("query_service", {
                "service_id": "qvm-test", "query_id": "metrics", "parameters": {"alias": "main"},
            })
            self.assertEqual(reply.structured_content, result)

        panel = DashboardData(self.root / "state", self.config, False)
        [target] = panel.available_hosts()["targets"]
        registered = panel.register_host({"id": target["id"]})
        panel.run_host(registered["id"], False, "healthy")
        [reading] = panel.host_history(registered["id"], False, 1)["history"]
        self.assertEqual({key: point["value"] for key, point in reading["metrics"].items()}, result["values"])
        self.assertNotIn("memory_percent", reading["metrics"])
        self.helper.assert_not_called()

    def test_metrics_distinguishes_missing_partial_permission_and_region_mismatch(self):
        path = "/v1/vm/monitor/instance"
        stamp = datetime.now(timezone.utc)
        rows = [{"time_stamp": (stamp - timedelta(minutes=6)).isoformat(), "cpu": 0}]
        self.routes[path] = {"data": {"monitor_data": {"instance_monitor_data": rows}}}
        self.assertEqual(self.adapter().metrics("main")["status"], "no_data")
        rows.append({"time_stamp": (stamp - timedelta(seconds=5)).isoformat(), "cpu": 0, "intranet_rx": "bad"})
        result = self.adapter().metrics("main")
        self.assertEqual((result["status"], result["values"]), ("partial", {"cpu_percent": 0}))
        self.routes[path] = HTTPError("https://api-qvm.qiniu.com", 403, "forbidden", {}, io.BytesIO())
        result = self.adapter().metrics("main")
        self.assertEqual((result["status"], result["reason"], result["values"]),
                         ("unavailable", "permission_denied", {}))
        self.settings["targets"][0]["region"] = "cn-other"
        adapter = QiniuQVMAdapter(self.settings)
        self.requests.clear()
        self.assertEqual(adapter.metrics("main")["reason"], "region_mismatch")
        self.assertEqual([path for path, _ in self.requests], ["/v1/vm/instance"])
