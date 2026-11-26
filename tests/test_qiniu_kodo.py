import unittest
from unittest.mock import patch

from stackorion.catalog import CatalogError
from stackorion.qiniu_kodo import QiniuKodoAdapter


class KodoAdapterTests(unittest.TestCase):
    def adapter(self):
        return QiniuKodoAdapter({"cli_path": "/helper.py", "credential_file": "/secret.env",
                                "targets": [{"alias": "backup", "bucket": "owned-backup"}]})

    def test_metadata_only_and_exact_bucket(self):
        with patch("stackorion.qiniu_kodo.qshell_query",
                   side_effect=[["owned-backup"], ["example.test"]]) as run:
            result = self.adapter().inspect("backup")
        self.assertEqual(run.call_count,2)
        self.assertEqual(run.call_args.args[2:], ("domains", "owned-backup"))
        self.assertNotIn("hidden", str(result))
        with self.assertRaisesRegex(CatalogError, "unknown"):
            self.adapter().inspect("missing")

    def test_mismatched_result_is_rejected(self):
        with patch("stackorion.qiniu_kodo.qshell_query", return_value=["other"]):
            with self.assertRaisesRegex(CatalogError, "owned"):
                self.adapter().inspect("backup")


if __name__ == "__main__":
    unittest.main()
