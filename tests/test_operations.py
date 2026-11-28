import unittest
from unittest.mock import Mock, patch

from mcp import Client

from stackorion.catalog import Catalog, CatalogError, Service
from stackorion.operations import OperationDefinition, OperationRegistry
from stackorion.qiniu_operations import QiniuQVMOperations
from stackorion.server import build_server

QVM_SCRIPT = {"script_path": "/fixture/qvm.py", "credential_file": "/fixture/qvm.env", "targets": []}


class OperationRegistryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog([
            Service("qiniu-main", "python-adapter", "qiniu", True, QVM_SCRIPT, "qiniu-qvm"),
            Service("qiniu-off", "python-adapter", "qiniu", False, {}, "qiniu-qvm"),
            Service("cloudflare-main", "python-adapter", "cloudflare", True, {}, "cloudflare-zone"),
            Service("wrong-main", "python-adapter", "cloudflare", True, {}, "qiniu-qvm"),
        ])
        self.registry = OperationRegistry(self.catalog)
        self.ep = Mock(name="plugin_entry")
        self.ep.name = "qiniu-qvm"
        self.ep.load.return_value = QiniuQVMOperations

    def test_discovery_and_disabled_provider_isolation_do_not_dispatch(self):
        with patch("stackorion.operations.entry_points", return_value=[self.ep]), \
                patch("stackorion.qiniu_operations.plan_port_access") as plan:
            self.assertEqual(self.registry.describe("qiniu-main")["operations"][0]["id"], "qvm.allow-port")
            self.assertEqual(self.registry.describe("cloudflare-main")["status"], "unsupported")
            for service, reason in [("qiniu-off", "disabled"), ("wrong-main", "provider")]:
                with self.assertRaisesRegex(CatalogError, reason):
                    self.registry.plan(service, "qvm.allow-port")
            with self.assertRaisesRegex(CatalogError, "not registered"):
                self.registry.apply("qiniu-main", "qvm.create-instance")
            plan.assert_not_called()

    def test_plugin_loading_failure_is_local_and_redacted(self):
        self.ep.load.side_effect = RuntimeError("secret credential location")
        with patch("stackorion.operations.entry_points", return_value=[self.ep]):
            with self.assertRaisesRegex(CatalogError, "could not be loaded") as caught:
                self.registry.describe("qiniu-main")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(self.registry.describe("cloudflare-main")["operations"], [])
        with patch("stackorion.operations.entry_points", return_value=[self.ep, self.ep]):
            with self.assertRaisesRegex(CatalogError, "ambiguous"):
                self.registry.describe("qiniu-main")

    def test_plan_apply_and_receipt_forward_without_changing_parameters(self):
        params = {"alias": "prod", "port": 443, "cidr": "192.0.2.1/32", "description": "approved"}
        execution = {**params, "expected_state": "a" * 64, "confirm_instance_id": "i-one",
                     "operation_id": "reviewed-001"}
        with patch("stackorion.operations.entry_points", return_value=[self.ep]), \
                patch("stackorion.qiniu_operations.plan_port_access", return_value={"status": "planned"}) as plan, \
                patch("stackorion.qiniu_operations.apply_port_access", return_value={"status": "success"}) as apply, \
                patch("stackorion.qiniu_operations.operation_status", return_value={"status": "success"}) as receipt:
            self.registry.plan("qiniu-main", "qvm.allow-port", **params)
            self.registry.apply("qiniu-main", "qvm.allow-port", **execution)
            self.registry.receipt("qiniu-qvm", "reviewed-001")
            plan.assert_called_once_with(self.catalog, "qiniu-main", **params)
            apply.assert_called_once_with(self.catalog, "qiniu-main", **execution)
            receipt.assert_called_once_with("reviewed-001")

    def test_invalid_plugin_contract_rejected(self):
        class InvalidPlugin:
            provider = "qiniu"
            definitions = (OperationDefinition("duplicated", "plan", "apply", "receipt"),) * 2

        self.ep.load.return_value = InvalidPlugin
        with patch("stackorion.operations.entry_points", return_value=[self.ep]):
            with self.assertRaisesRegex(CatalogError, "contract"):
                self.registry.describe("qiniu-main")


class OperationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_receipt_checks_service_even_after_disable(self):
        catalog = Catalog([
            Service("qiniu-main", "python-adapter", "qiniu", False, {}, "qiniu-qvm"),
            Service("qiniu-other", "python-adapter", "qiniu", True, {}, "qiniu-qvm"),
            Service("other-provider", "python-adapter", "cloudflare", True, {}, "qiniu-qvm"),
        ])
        receipt = {"status": "success", "identity": {"service": "qiniu-main", "operation": "qvm.allow-port"}}
        with patch("stackorion.qiniu_operations.operation_status", return_value=receipt) as read:
            async with Client(build_server(catalog)) as client:
                async def get_receipt(service_id):
                    result = await client.call_tool("get_service_operation", {
                        "service_id": service_id, "operation_id": "reviewed-001",
                    })
                    return result.structured_content

                self.assertEqual(await get_receipt("qiniu-main"), receipt)
                self.assertEqual(await get_receipt("qiniu-other"), {
                    "status": "unavailable", "reason": "operation receipt is unavailable for this service",
                })
                read.reset_mock()
                self.assertEqual((await get_receipt("other-provider"))["status"], "unavailable")
                read.assert_not_called()

                read.return_value = {"status": "success", "identity": {}}
                self.assertEqual((await get_receipt("qiniu-main"))["status"], "unavailable")

    async def test_existing_tool_contract_and_readonly_discovery(self):
        catalog = Catalog([Service("qiniu-main", "python-adapter", "qiniu", True, QVM_SCRIPT, "qiniu-qvm")])
        ep = Mock()
        ep.name = "qiniu-qvm"
        ep.load.return_value = QiniuQVMOperations
        with patch("stackorion.operations.entry_points", return_value=[ep]), \
                patch("stackorion.qiniu_operations.plan_port_access", return_value={"status": "planned"}) as plan:
            async with Client(build_server(catalog)) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                annotation = tools["apply_qiniu_port_access"].annotations
                self.assertFalse(annotation.read_only_hint)
                self.assertTrue(annotation.destructive_hint)
                self.assertFalse(annotation.idempotent_hint)
                self.assertTrue(tools["list_service_operations"].annotations.read_only_hint)
                deletion = tools["apply_qiniu_rule_deletion"]
                self.assertFalse(deletion.annotations.read_only_hint)
                self.assertTrue(deletion.annotations.destructive_hint)
                self.assertFalse(deletion.annotations.idempotent_hint)
                for field in ("rule_id", "security_group_id", "expected_state", "operation_id", "confirm_instance_id"):
                    self.assertIn(field, deletion.input_schema["required"])
                self.assertIn("expected_state", tools["apply_qiniu_port_access"].input_schema["required"])
                await client.call_tool("list_service_operations", {"service_id": "qiniu-main"})
                plan.assert_not_called()
                await client.call_tool("plan_qiniu_port_access", {"service_id": "qiniu-main", "target_alias": "prod",
                                                                 "port": 443, "cidr": "192.0.2.1/32"})
                plan.assert_called_once_with(catalog, "qiniu-main", alias="prod", port=443,
                                             cidr="192.0.2.1/32", description="operator-approved TCP access")
