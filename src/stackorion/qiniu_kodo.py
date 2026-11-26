"""Read-only Kodo owned-bucket and exact-bucket domain metadata."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import CatalogError, ID
from .qiniu_cli import query as qshell_query


class QiniuKodoAdapter:
    def __init__(self, settings: dict[str, Any]):
        if set(settings) != {"cli_path", "credential_file", "targets"}:
            raise CatalogError("invalid Kodo settings")
        if not all(isinstance(settings[key], str) for key in ("cli_path", "credential_file")):
            raise CatalogError("invalid Kodo paths")
        self.cli, self.credential = Path(settings["cli_path"]), Path(settings["credential_file"])
        if not self.cli.is_absolute() or not self.credential.is_absolute():
            raise CatalogError("Kodo paths must be absolute")
        if not isinstance(settings["targets"], list):
            raise CatalogError("invalid Kodo targets")
        self.targets = {}
        for row in settings["targets"]:
            if not isinstance(row, dict) or set(row) != {"alias", "bucket"}:
                raise CatalogError("invalid Kodo target")
            alias, bucket = row["alias"], row["bucket"]
            if (not isinstance(alias, str) or not ID.fullmatch(alias) or not isinstance(bucket, str)
                    or not bucket or alias in self.targets or bucket in self.targets.values()):
                raise CatalogError("invalid or duplicate Kodo target")
            self.targets[alias] = bucket

    def list_targets(self):
        return sorted(self.targets)

    def discover(self):
        buckets = qshell_query(self.cli, self.credential, "buckets")
        if not isinstance(buckets, list) or any(not isinstance(bucket, str) for bucket in buckets):
            raise CatalogError("Kodo returned invalid bucket list")
        return {"provider": "qiniu", "status": "observed", "observed_at": datetime.now(timezone.utc).isoformat(),
                "coverage": "owned buckets visible to this credential; excludes shared buckets and object contents",
                "returned_count": len(buckets), "data": [{"bucket": bucket} for bucket in buckets]}

    def inspect(self, alias):
        if alias not in self.targets:
            raise CatalogError("unknown Kodo bucket target")
        bucket = self.targets[alias]
        if bucket not in qshell_query(self.cli, self.credential, "buckets"):
            raise CatalogError("Kodo target is not an owned bucket")
        domains = qshell_query(self.cli, self.credential, "domains", bucket)
        if not isinstance(domains, list) or any(not isinstance(domain, str) for domain in domains):
            raise CatalogError("Kodo returned invalid domains")
        return {"provider": "qiniu", "target": alias, "status": "observed",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "coverage": "bucket ownership and configured domain names only; no object or backup recovery verification",
                "data": {"bucket": self.targets[alias], "domains": domains}}
