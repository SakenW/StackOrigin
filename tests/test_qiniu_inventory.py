import unittest
from unittest.mock import patch

from stackorion.catalog import CatalogError
from stackorion.qiniu import QiniuQVMAdapter


class QiniuInventoryTests(unittest.TestCase):
    def adapter(self):
        return QiniuQVMAdapter({"script_path": "/helper.py", "credential_file": "/credential.env",
                                "targets": [{"alias": "main", "instance_id": "vm-one"}]})

    def test_discovery_keeps_partial_coverage_and_filters_secrets(self):
        payload = {"collections": {
            "instances": {"status": "observed", "total": 1, "returned_count": 1, "pages": 1,
                          "data": [{"instance_id": "vm-one", "password": "secret"}]},
            "eips": {"status": "unavailable", "total": None, "returned_count": 0, "reason": "http_403", "data": []},
            "vpcs": {"status": "observed", "total": 0, "returned_count": 0, "data": []},
        }}
        with patch.object(QiniuQVMAdapter, "_run", return_value=payload) as run:
            observed = self.adapter().discover()
        run.assert_called_once_with(["inventory"])
        self.assertEqual(observed["status"], "partial")
        self.assertEqual(observed["data"]["eips"]["reason"], "http_403")
        self.assertNotIn("secret", str(observed))

    def test_audit_does_not_turn_failed_rules_into_healthy_empty_rules(self):
        payload = {"instance": {"instance_id": "vm-one"}, "security_group_ingress": {"sg-one": []},
                   "coverage": {"ingress:sg-one": {"status": "unavailable", "reason": "http_403"}}}
        with patch.object(QiniuQVMAdapter, "_run", return_value=payload):
            observed = self.adapter().inspect("main")
        self.assertEqual(observed["status"], "partial")
        self.assertEqual(observed["coverage"]["ingress:sg-one"]["status"], "unavailable")
        payload["instance"]["instance_id"] = "other"
        with patch.object(QiniuQVMAdapter, "_run", return_value=payload):
            with self.assertRaisesRegex(CatalogError, "mismatch"):
                self.adapter().inspect("main")


if __name__ == "__main__":
    unittest.main()
