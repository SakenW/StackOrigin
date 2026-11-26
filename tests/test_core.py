import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from mcp.server import MCPServer

from stackorion.catalog import Catalog, CatalogError, Service, set_enabled
from stackorion.aliyun import AliyunECSAdapter
from stackorion.cloudflare import CloudflareZoneAdapter
from stackorion.external import probe_external
from stackorion.qiniu import QiniuQVMAdapter
from stackorion.server import build_server
from stackorion.spaceship import SpaceshipDomainsAdapter
from stackorion.tencent import TencentEdgeOneAdapter
from stackorion.tencent_compute import TencentComputeAdapter


class CatalogTests(unittest.TestCase):
    def test_account_bindings_reject_cross_account_resources(self):
        account = {"id": "qiniu-main", "provider": "qiniu", "label": "个人账号",
                   "scope": "personal", "verification": "declared"}
        service = Service("qiniu-default", "python-adapter", "qiniu", True, {}, "qiniu-qvm", "qiniu-main")
        resource = {"id": "qvm-main", "service_id": "qiniu-default", "account": "qiniu-main",
                    "region": "cn-hangzhou", "type": "qvm", "provider_id": "qvm-1", "verification": "declared"}
        catalog = Catalog([service], [resource], accounts=[account])
        self.assertEqual(catalog.accounts()[0]["enabled_service_ids"], ["qiniu-default"])
        with self.assertRaisesRegex(CatalogError, "mismatched account"):
            Catalog([Service("wrong", "python-adapter", "other", True, {}, "qiniu-qvm", "qiniu-main")], accounts=[account])
        with self.assertRaisesRegex(CatalogError, "resource account"):
            Catalog([service], [{**resource, "account": "another"}], accounts=[account])
        with self.assertRaisesRegex(CatalogError, "duplicate account"):
            Catalog([service], accounts=[account, account])

    def test_account_schema_rejects_invalid_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services.json"
            data = {"schema_version": 1, "services": [], "accounts": [
                {"id": "qiniu-main", "provider": "qiniu", "label": "个人账号",
                 "scope": "personal", "verification": "declared"}]}
            path.write_text(json.dumps(data))
            self.assertEqual(Catalog.load(path).accounts()[0]["id"], "qiniu-main")
            data["accounts"][0]["scope"] = []
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(CatalogError, "scope"):
                Catalog.load(path)

    def test_resource_identity_and_relation_references(self):
        services = [Service("qiniu-default", "python-adapter", "qiniu", False, {}, "qiniu-qvm"),
                    Service("aliyun-default", "external-mcp", "aliyun", False, {"url": "https://example.test/mcp"})]
        first = {"id": "web-qiniu", "service_id": "qiniu-default", "account": "personal", "region": "cn-hangzhou", "type": "vm", "provider_id": "same-name", "verification": "declared"}
        second = {**first, "id": "web-aliyun", "service_id": "aliyun-default"}
        catalog = Catalog(services, [first, second], [{"from": "web-qiniu", "to": "web-aliyun", "type": "related"}])
        self.assertEqual(len(catalog.resources()), 2)
        self.assertEqual(len(catalog.resources("qiniu-default")), 1)
        self.assertEqual(len(catalog.relations("web-qiniu")), 1)
        with self.assertRaisesRegex(CatalogError, "provider resource identity"):
            Catalog(services, [first, {**first, "id": "duplicate-qiniu"}])
        with self.assertRaisesRegex(CatalogError, "unknown resource"):
            Catalog(services, [first], [{"from": "web-qiniu", "to": "missing", "type": "related"}])

    def test_enable_disable_is_local_and_dormant_cannot_be_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services.json"
            path.write_text(json.dumps({"schema_version": 1, "services": [
                {"id": "external-test", "kind": "external-mcp", "provider": "test", "enabled": False,
                 "settings": {"url": "https://example.test/mcp"}},
                {"id": "jdcloud-dormant", "kind": "dormant", "provider": "jdcloud", "enabled": False, "settings": {}},
            ]}))
            path.chmod(0o600)
            set_enabled(path, "external-test", True)
            self.assertTrue(Catalog.load(path).get("external-test").enabled)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            set_enabled(path, "external-test", False)
            self.assertFalse(Catalog.load(path).get("external-test").enabled)
            with self.assertRaisesRegex(CatalogError, "requires a connector"):
                set_enabled(path, "jdcloud-dormant", True)

    def test_disabled_and_dormant_services_do_not_load_adapters(self):
        catalog = Catalog([
            Service("qiniu-default", "python-adapter", "qiniu", False, {}, "qiniu-qvm"),
            Service("jdcloud-dormant", "dormant", "jdcloud", False, {}),
        ])
        self.assertEqual([row["id"] for row in catalog.list()], ["qiniu-default", "jdcloud-dormant"])
        with self.assertRaisesRegex(CatalogError, "disabled"):
            catalog.adapter(catalog.get("qiniu-default"))

    def test_catalog_rejects_duplicate_ids_and_enabled_dormant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services.json"
            row = {"id": "qiniu-default", "kind": "dormant", "provider": "qiniu", "enabled": False, "settings": {}}
            path.write_text(json.dumps({"schema_version": 1, "services": [row, row]}))
            with self.assertRaisesRegex(CatalogError, "duplicate"):
                Catalog.load(path)
            row["enabled"] = True
            path.write_text(json.dumps({"schema_version": 1, "services": [row]}))
            with self.assertRaisesRegex(CatalogError, "without a connector"):
                Catalog.load(path)

    def test_catalog_rejects_secret_fields_in_external_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services.json"
            path.write_text(json.dumps({"schema_version": 1, "services": [{
                "id": "cloudflare-default", "kind": "external-mcp", "provider": "cloudflare",
                "enabled": True, "settings": {"url": "https://mcp.cloudflare.com/mcp", "token": "unsafe"},
            }]}))
            with self.assertRaisesRegex(CatalogError, "only url"):
                Catalog.load(path)
            data = json.loads(path.read_text())
            data["services"][0]["settings"] = {"url": "https://user:secret@example.test/mcp"}
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(CatalogError, "HTTPS URL"):
                Catalog.load(path)

    def test_client_mcp_is_metadata_only_and_rejects_extra_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services.json"
            row = {"id": "clarity-transhub", "kind": "client-mcp", "provider": "clarity",
                   "enabled": True, "settings": {"client": "codex", "server_id": "clarity-official",
                                                 "enabled_tools": ["query-analytics-dashboard"]}}
            path.write_text(json.dumps({"schema_version": 1, "services": [row]}))
            catalog = Catalog.load(path)
            self.assertEqual(catalog.list()[0]["execution_scope"], "client-direct")
            with self.assertRaisesRegex(CatalogError, "no local adapter"):
                catalog.adapter(catalog.get("clarity-transhub"))
            with self.assertRaisesRegex(CatalogError, "owning client"):
                set_enabled(path, "clarity-transhub", False)
            row["settings"]["token"] = "unsafe"
            path.write_text(json.dumps({"schema_version": 1, "services": [row]}))
            with self.assertRaisesRegex(CatalogError, "known client binding"):
                Catalog.load(path)


class QiniuTests(unittest.TestCase):
    def test_audit_uses_exact_target_and_private_credential_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "qvm.py"
            script.write_text(
                "import json,os,sys\n"
                "open(os.path.join(os.path.dirname(__file__),'call.json'),'w').write(json.dumps({"
                "'argv':sys.argv[1:],'credential_path':os.environ.get('QINIU_ENV_FILE')}))\n"
                "print(json.dumps({'instance': {'instance_id': sys.argv[2], 'access_key': 'should-not-return'}, "
                "'security_group_ingress': {}, 'credential_path': os.environ.get('QINIU_ENV_FILE')}))\n"
            )
            credential = root / "qiniu.env"
            credential.write_text("QINIU_AK=test\nQINIU_SK=test\n")
            credential.chmod(0o600)
            adapter = QiniuQVMAdapter({"script_path": str(script), "credential_file": str(credential),
                                      "targets": [{"alias": "production", "instance_id": "qvm-123"}]})
            observed = adapter.inspect("production")
            call = json.loads((root / "call.json").read_text())
            self.assertEqual(call["argv"], ["--instance-id", "qvm-123", "audit"])
            self.assertEqual(call["credential_path"], str(credential))
            self.assertNotIn("credential_path", observed["data"])
            self.assertNotIn("access_key", observed["data"]["instance"])
            with self.assertRaisesRegex(CatalogError, "unknown"):
                adapter.inspect("other")
            credential.chmod(0o644)
            with self.assertRaisesRegex(CatalogError, "0600"):
                adapter.inspect("production")

    def test_audit_rejects_mismatched_target_and_hides_script_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "qvm.py"
            credential = root / "qiniu.env"
            credential.write_text("QINIU_AK=test\n")
            credential.chmod(0o600)
            adapter = QiniuQVMAdapter({"script_path": str(script), "credential_file": str(credential),
                                      "targets": [{"alias": "production", "instance_id": "qvm-123"}]})
            script.write_text("print('{\"instance\": {\"instance_id\": \"another\"}}')\n")
            with self.assertRaisesRegex(CatalogError, "mismatch"):
                adapter.inspect("production")
            script.write_text("import sys\nprint('secret value', file=sys.stderr)\nsys.exit(3)\n")
            with self.assertRaises(CatalogError) as caught:
                adapter.inspect("production")
            self.assertNotIn("secret value", str(caught.exception))


class SpaceshipTests(unittest.TestCase):
    def test_domain_discovery_paginates_and_omits_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "spaceship.env"
            credential.write_text("SPACESHIP_API_KEY=test-key\nSPACESHIP_API_SECRET=test-secret\n")
            credential.chmod(0o600)
            adapter = SpaceshipDomainsAdapter({"credential_file": str(credential), "targets": []})
            urls = []

            def fake_get_json(url, key, secret):
                urls.append(url)
                if "skip=0" in url:
                    item = {"name": "first.example", "autoRenew": True, "contacts": {"registrant": "private"}}
                else:
                    item = {"name": "second.example", "autoRenew": False, "contacts": {"registrant": "private"}}
                return {"items": [item], "total": 2}

            with patch.object(SpaceshipDomainsAdapter, "_get_json", side_effect=fake_get_json):
                observed = adapter.discover()
            self.assertEqual(observed["returned_count"], 2)
            self.assertEqual(observed["status"], "observed")
            self.assertEqual(urls, [
                "https://spaceship.dev/api/v1/domains?take=100&skip=0",
                "https://spaceship.dev/api/v1/domains?take=100&skip=100",
            ])
            self.assertTrue(all("contacts" not in item for item in observed["data"]))

    def test_exact_domain_read_filters_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "spaceship.env"
            credential.write_text("SPACESHIP_API_KEY=test-key\nSPACESHIP_API_SECRET=test-secret\n")
            credential.chmod(0o600)
            adapter = SpaceshipDomainsAdapter({"credential_file": str(credential),
                                                "targets": [{"alias": "main", "domain": "example.com"}]})
            called = []

            def fake_get_json(url, key, secret):
                called.append(url)
                return {"name": "example.com", "autoRenew": True,
                        "expirationDate": "2030-01-01T00:00:00Z",
                        "contacts": {"registrant": "private"}}

            with patch.object(SpaceshipDomainsAdapter, "_get_json", side_effect=fake_get_json):
                observed = adapter.inspect("main")
            self.assertEqual(called, ["https://spaceship.dev/api/v1/domains/example.com"])
            self.assertEqual(observed["data"]["expirationDate"], "2030-01-01T00:00:00Z")
            self.assertNotIn("contacts", observed["data"])
            with self.assertRaisesRegex(CatalogError, "unknown"):
                adapter.inspect("unlisted")

    def test_rejects_mismatched_domain_and_unsafe_credential_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "spaceship.env"
            credential.write_text("SPACESHIP_API_KEY=test-key\nSPACESHIP_API_SECRET=test-secret\n")
            credential.chmod(0o600)
            adapter = SpaceshipDomainsAdapter({"credential_file": str(credential),
                                                "targets": [{"alias": "main", "domain": "example.com"}]})
            with patch.object(SpaceshipDomainsAdapter, "_get_json",
                              return_value={"name": "other.example"}):
                with self.assertRaisesRegex(CatalogError, "mismatch"):
                    adapter.inspect("main")
            credential.chmod(0o644)
            with self.assertRaisesRegex(CatalogError, "0600"):
                adapter.inspect("main")

    def test_curl_transport_keeps_credentials_out_of_arguments_and_errors(self):
        import subprocess

        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "curl")
            self.assertNotIn("test-key", " ".join(args))
            self.assertNotIn("test-secret", " ".join(args))
            self.assertIn(b"X-API-Key: test-key", kwargs["input"])
            self.assertIn(b"X-API-Secret: test-secret", kwargs["input"])
            return subprocess.CompletedProcess(args, 0, b'{"items":[],"total":0}\nSTACKORION_HTTP_STATUS:200', b"")

        with patch("stackorion.spaceship.subprocess.run", side_effect=fake_run):
            self.assertEqual(SpaceshipDomainsAdapter._get_json("https://spaceship.dev/api/v1/domains", "test-key", "test-secret")["total"], 0)

        with patch("stackorion.spaceship.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, b'{"detail":"test-secret"}\nSTACKORION_HTTP_STATUS:403', b""
        )):
            with self.assertRaisesRegex(CatalogError, "Spaceship API HTTP 403") as caught:
                SpaceshipDomainsAdapter._get_json("https://spaceship.dev/api/v1/domains", "test-key", "test-secret")
            self.assertNotIn("test-secret", str(caught.exception))


class CloudflareTests(unittest.TestCase):
    ZONE_ID = "a" * 32
    ACCOUNT_ID = "b" * 32

    def _adapter(self, root):
        credential = root / "cloudflare.env"
        credential.write_text("CLOUDFLARE_ACCOUNT_ID=metadata\nCLOUDFLARE_API_TOKEN=token-value\n")
        credential.chmod(0o600)
        return CloudflareZoneAdapter({"credential_file": str(credential), "targets": [{
            "alias": "website", "zone_id": self.ZONE_ID, "account_id": self.ACCOUNT_ID,
            "zone_name": "example.com",
        }]})

    def test_zone_read_uses_exact_id_and_filters_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = self._adapter(root)
            called = []

            def fake_urlopen(request, timeout):
                called.append((request.full_url, request.get_method(), timeout))
                return io.BytesIO(json.dumps({"success": True, "result": {
                    "id": self.ZONE_ID, "name": "example.com", "account": {"id": self.ACCOUNT_ID},
                    "status": "active", "name_servers": ["ns1.example.com"], "private_field": "hidden",
                }}).encode())

            with patch("stackorion.cloudflare.urllib.request.urlopen", fake_urlopen):
                observed = adapter.inspect("website")
            self.assertEqual(called, [("https://api.cloudflare.com/client/v4/zones/" + self.ZONE_ID, "GET", 12)])
            self.assertEqual(observed["data"]["status"], "active")
            self.assertNotIn("private_field", observed["data"])

    def test_rejects_account_mismatch_and_unknown_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(Path(tmp))
            with self.assertRaisesRegex(CatalogError, "unknown"):
                adapter.inspect("other")
            with patch("stackorion.cloudflare.urllib.request.urlopen", return_value=io.BytesIO(
                json.dumps({"success": True, "result": {"id": self.ZONE_ID,
                            "name": "example.com", "account": {"id": "c" * 32}}}).encode()
            )):
                with self.assertRaisesRegex(CatalogError, "mismatch"):
                    adapter.inspect("website")


class TencentTests(unittest.TestCase):
    def test_edgeone_uses_fixed_read_action_and_exact_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "eo.py"
            credential = root / "tencent.env"
            credential.write_text("TENCENTCLOUD_SECRET_ID=test\nTENCENTCLOUD_SECRET_KEY=test\n")
            credential.chmod(0o600)
            script.write_text(
                "import json,sys\n"
                "body=json.loads(sys.argv[2])\n"
                "assert sys.argv[1]=='DescribeZones'\n"
                "assert body['Filters']==[{'Name':'zone-id','Values':['zone-example123']}]\n"
                "print(json.dumps({'Response': {'Zones': [{'ZoneId':'zone-example123','ZoneName':'example.com',"
                "'Status':'active','Token':'never-return'}]}}))\n"
            )
            adapter = TencentEdgeOneAdapter({"script_path": str(script), "credential_file": str(credential),
                                              "targets": [{"alias": "main", "zone_id": "zone-example123"}]})
            observed = adapter.inspect("main")
            self.assertEqual(observed["data"]["Status"], "active")
            self.assertNotIn("Token", observed["data"])
            with self.assertRaisesRegex(CatalogError, "unknown"):
                adapter.inspect("other")

    def test_edgeone_rejects_wrong_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "eo.py"
            credential = root / "tencent.env"
            credential.write_text("TENCENTCLOUD_SECRET_ID=test\n")
            credential.chmod(0o600)
            script.write_text("print('{\"Response\": {\"Zones\": [{\"ZoneId\": \"zone-other\"}]}}')\n")
            adapter = TencentEdgeOneAdapter({"script_path": str(script), "credential_file": str(credential),
                                              "targets": [{"alias": "main", "zone_id": "zone-example123"}]})
            with self.assertRaisesRegex(CatalogError, "mismatch"):
                adapter.inspect("main")


class TencentComputeTests(unittest.TestCase):
    def test_product_and_region_bound_paginated_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "tencent.env"
            credential.write_text("TENCENTCLOUD_SECRET_ID=id\nTENCENTCLOUD_SECRET_KEY=secret\nEDGEONE_ZONE_ID=other\n")
            credential.chmod(0o600)
            calls = []

            class FakeClient:
                def __init__(self, product, version, _credential, region, _profile):
                    calls.append((product, version, region))

                def call_json(self, action, params):
                    self_action = (action, params["Offset"], params["Limit"])
                    calls.append(self_action)
                    instance = {"InstanceId": "lhins-a" if params["Offset"] == 0 else "lhins-b",
                                "InstanceState": "RUNNING", "Secret": "never return"}
                    return {"Response": {"TotalCount": 2, "InstanceSet": [instance]}}

            adapter = TencentComputeAdapter({"credential_file": str(credential), "product": "lighthouse",
                                             "regions": ["ap-shanghai"]})
            with patch("stackorion.tencent_compute.CommonClient", FakeClient):
                observed = adapter.inspect("ap-shanghai")
            self.assertEqual(observed["returned_count"], 2)
            self.assertEqual(observed["status"], "observed")
            self.assertEqual(calls[0], ("lighthouse", "2020-03-24", "ap-shanghai"))
            self.assertEqual(calls[1:], [("DescribeInstances", 0, 100), ("DescribeInstances", 100, 100)])
            self.assertNotIn("Secret", observed["data"][0])
            with self.assertRaisesRegex(CatalogError, "unknown"):
                adapter.inspect("ap-beijing")


class AliyunTests(unittest.TestCase):
    def test_ecs_paginates_and_keeps_region_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "aliyun.env"
            credential.write_text("ALIYUN_ACCESS_KEY_ID=test-key\nALIYUN_ACCESS_KEY_SECRET=test-secret\n")
            credential.chmod(0o600)
            cli = root / "aliyun"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "assert sys.argv[1:3]==['ecs','DescribeInstances']\n"
                "assert sys.argv[sys.argv.index('--region')+1]=='cn-hangzhou'\n"
                "if '--NextToken' in sys.argv:\n"
                " print(json.dumps({'Instances':{'Instance':[{'InstanceId':'i-2','RegionId':'cn-hangzhou',"
                "'Secret':'hidden'}]}}))\n"
                "else:\n"
                " print(json.dumps({'Instances':{'Instance':[{'InstanceId':'i-1','RegionId':'cn-hangzhou'}]},"
                "'NextToken':'page2'}))\n"
            )
            cli.chmod(0o700)
            adapter = AliyunECSAdapter({"cli_path": str(cli), "credential_file": str(credential),
                                        "regions": ["cn-hangzhou"]})
            observed = adapter.inspect("cn-hangzhou")
            self.assertEqual(observed["instance_count"], 2)
            self.assertEqual(observed["status"], "observed")
            self.assertNotIn("Secret", observed["data"][1])
            with self.assertRaisesRegex(CatalogError, "unknown"):
                adapter.inspect("ap-shanghai")

    def test_ecs_rejects_wrong_region_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "aliyun.env"
            credential.write_text("ALIYUN_ACCESS_KEY_ID=test-key\nALIYUN_ACCESS_KEY_SECRET=test-secret\n")
            credential.chmod(0o600)
            cli = root / "aliyun"
            cli.write_text("#!/usr/bin/env python3\nprint('{\"Instances\":{\"Instance\":[{\"InstanceId\":\"i-x\",\"RegionId\":\"other\"}]}}')\n")
            cli.chmod(0o700)
            adapter = AliyunECSAdapter({"cli_path": str(cli), "credential_file": str(credential),
                                        "regions": ["cn-hangzhou"]})
            with self.assertRaisesRegex(CatalogError, "mismatch"):
                adapter.inspect("cn-hangzhou")

    def test_ecs_reports_ram_denial_without_exposing_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "aliyun.env"
            credential.write_text("ALIYUN_ACCESS_KEY_ID=test-key\nALIYUN_ACCESS_KEY_SECRET=test-secret\n")
            credential.chmod(0o600)
            cli = root / "aliyun"
            cli.write_text("#!/usr/bin/env python3\nimport sys\nprint('ErrorCode: Forbidden.RAM\\nSecret: hidden',file=sys.stderr)\nsys.exit(1)\n")
            cli.chmod(0o700)
            adapter = AliyunECSAdapter({"cli_path": str(cli), "credential_file": str(credential),
                                        "regions": ["cn-hangzhou"]})
            with self.assertRaises(CatalogError) as caught:
                adapter.inspect("cn-hangzhou")
            self.assertIn("permission denied", str(caught.exception))
            self.assertNotIn("Secret", str(caught.exception))


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_server_exposes_catalog_without_enabling_dormant_service(self):
        server = build_server(Catalog([Service("jdcloud-dormant", "dormant", "jdcloud", False, {})]))
        async with Client(server) as client:
            tools = await client.list_tools()
            self.assertIn("list_services", [item.name for item in tools.tools])
            self.assertIn("list_accounts", [item.name for item in tools.tools])
            self.assertIn("discover_service", [item.name for item in tools.tools])
            result = await client.call_tool("list_services")
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["result"][0]["id"], "jdcloud-dormant")
            failed = await client.call_tool("list_targets", {"service_id": "jdcloud-dormant"})
            self.assertFalse(failed.is_error)
            self.assertEqual(failed.structured_content["status"], "unavailable")

    async def test_external_mcp_probe_lists_tools_only(self):
        upstream = MCPServer("upstream")
        invoked = []

        @upstream.tool()
        def dangerous_write() -> str:
            invoked.append(True)
            return "changed"

        service = Service("test-upstream", "external-mcp", "test", True,
                          {"url": "https://example.test/mcp"})
        result = await probe_external(service, client_factory=lambda _: Client(upstream))
        self.assertEqual(result["tool_count"], 1)
        self.assertEqual(invoked, [])


if __name__ == "__main__":
    unittest.main()
