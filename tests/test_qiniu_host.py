import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from stackorion.catalog import CatalogError
from stackorion.qiniu_host import QiniuHostCapacityAdapter


class HostAdapterTests(unittest.TestCase):
    def test_identity_and_no_mutation_contract_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, connection = root / "helper.py", root / ".agent-env"
            script.write_text("fake")
            connection.write_text("fake")
            connection.chmod(0o600)
            adapter = QiniuHostCapacityAdapter({"script_path": str(script), "connection_file": str(connection),
                                                "targets": [{"alias": "main", "instance_id": "i-one"}]})
            data = {"instance_id": "i-one", "schema": "foundation.host-capacity-audit.v1",
                    "mutation_invoked": False, "filesystem": {"used_percent": 58, "secret": "hidden"}}
            with patch("stackorion.qiniu_host.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(data), "")):
                self.assertNotIn("hidden", str(adapter.inspect("main")))
            for field, value in (("instance_id", "i-other"), ("mutation_invoked", True)):
                invalid = {**data, field: value}
                with patch("stackorion.qiniu_host.subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(invalid), "")):
                    with self.assertRaisesRegex(CatalogError, "mismatch"):
                        adapter.inspect("main")


if __name__ == "__main__":
    unittest.main()
