from contextlib import redirect_stdout
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
            "password": "unexpected-field",
        }
        self.routes = {
            "/v1/vm/instance": {"total": 1, "data": [self.vm]},
            "/v1/vm/eip": {"total": 0, "data": []},
            "/v1/vm/vpc": {"total": 0, "data": []},
            "/v1/vm/instance/i-fixture": {"data": self.vm},
            "/v1/vm/disk": {"total": 1, "data": [{"disk_id": "d-fixture", "size": 50}]},
            "/v1/vpc/common_bandwidth": {"total": 1, "data": [{"cb_id": "cb-fixture", "name": "fixture"}]},
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
        result = self.adapter().inspect("main")
        self.assertEqual(self.requests[0], ("/v1/vm/instance/i-fixture", {}))
        related = dict(self.requests[1:])
        for params in related.values():
            self.assertEqual(params["region_id"], ["cn-fixture"])
        for path in (disk_path, "/v1/vm/eip"):
            self.assertEqual(related[path]["instance_id"], ["i-fixture"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["disks"]["reason"], "http_403")
        self.assertEqual(result["coverage"]["egress:sg-fixture"]["status"], "observed")
        self.assertEqual(result["data"]["security_group_ingress"]["sg-fixture"][0]["protocol"], "TCP")
        self.assertEqual(result["data"]["common_bandwidth"][0]["bandwidth_package_id"], "cb-fixture")
        self.assertNotIn("unexpected-field", json.dumps(result))
        self.requests.clear()
        self.routes["/v1/vm/instance/i-fixture"] = {"data": {"instance_id": "i-other"}}
        with self.assertRaisesRegex(CatalogError, "target mismatch"):
            self.adapter().inspect("main")
        with self.assertRaisesRegex(CatalogError, "unknown QVM target"):
            self.adapter().inspect("i-other")
        self.assertEqual(len(self.requests), 1)

    def test_native_cannot_use_legacy_operations_and_explicit_helper_does_not_fallback(self):
        registry = OperationRegistry(self.catalog)
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
                             {"inventory": "official-api", "instance": "official-api"})
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
