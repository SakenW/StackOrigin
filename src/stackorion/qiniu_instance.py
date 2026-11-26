"""Exact-region QVM lifecycle adapter; registration is owned by the parent integrator."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .catalog import CatalogError
from .qiniu import _regular_owned

ADAPTER_NAME = "qiniu-instance-lifecycle"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
DIGEST = re.compile(r"[0-9a-f]{64}")
ACTIONS = {"start": "Stopped", "stop": "Running", "reboot": "Running"}
STATES = {"Creating", "Changing", "Running", "Starting", "Stopping", "Stopped", "Rebooting", "Pending", "Deleted"}
INSTANCE_FIELDS = ("instance_id", "region_id", "status", "instance_type", "zone_id", "create_time")


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def owned(path, *, secret=False):
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise CatalogError("configured path must be absolute without symlinks")
    _regular_owned(path, secret=secret)
    if path.stat().st_mode & 0o022:
        raise CatalogError("configured file is writable by another identity")


def valid_token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


class QiniuInstanceAdapter:
    def __init__(self, settings: dict):
        if not isinstance(settings, dict) or set(settings) != {"script_path", "credential_file", "targets"}:
            raise CatalogError("invalid instance lifecycle adapter settings")
        if not all(isinstance(settings[k], str) for k in ("script_path", "credential_file")):
            raise CatalogError("invalid lifecycle paths")
        self.script, self.credential = Path(settings["script_path"]), Path(settings["credential_file"])
        if (not self.script.is_absolute() or not self.credential.is_absolute()
                or self.script.name != "qiniu_instance_actions.py"):
            raise CatalogError("absolute owned lifecycle helper and credential paths required")
        if not isinstance(settings["targets"], list):
            raise CatalogError("targets must be a list")
        self.targets = {}
        identities = set()
        for target in settings["targets"]:
            if (not isinstance(target, dict) or set(target) != {"alias", "instance_id", "region_id"}
                    or not all(valid_token(v) for v in target.values())):
                raise CatalogError("exact alias, instance_id and region_id required; LAS unsupported")
            identity = (target["instance_id"], target["region_id"])
            if target["alias"] in self.targets or identity in identities:
                raise CatalogError("duplicate lifecycle target")
            identities.add(identity)
            self.targets[target["alias"]] = dict(target)

    def list_targets(self) -> list[str]:
        return sorted(self.targets)

    def target(self, alias: str) -> dict:
        if not isinstance(alias, str) or alias not in self.targets:
            raise CatalogError("unknown lifecycle target")
        return self.targets[alias].copy()

    def fingerprint(self) -> str:
        hashes = {}
        for path in (self.script, self.script.with_name("qiniu_qvm_tool.py")):
            owned(path)
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return canonical_hash(hashes)

    def credential_fingerprint(self) -> str:
        owned(self.credential, secret=True)
        return hashlib.sha256(self.credential.read_bytes()).hexdigest()

    def _run(self, args: list[str]) -> dict:
        self.fingerprint()
        owned(self.credential, secret=True)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "TMPDIR") if k in os.environ}
        env.update(QINIU_ENV_FILE=str(self.credential), PYTHONDONTWRITEBYTECODE="1")
        try:
            result = subprocess.run([sys.executable, "-B", "-I", str(self.script), *args],
                                    env=env, capture_output=True, text=True, timeout=90, check=False)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise CatalogError("lifecycle helper unavailable; dispatch outcome may be unknown") from None
        if result.returncode or len(result.stdout) > 2_097_152:
            raise CatalogError("lifecycle helper failed or output exceeded limit")
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise CatalogError("invalid lifecycle helper response") from None
        if not isinstance(value, dict):
            raise CatalogError("invalid lifecycle helper response")
        return value

    def args(self, alias: str, command: str) -> list[str]:
        if command not in {*ACTIONS, "inspect"}:
            raise CatalogError("unsupported lifecycle command")
        target = self.target(alias)
        return [command, "--instance-id", target["instance_id"], "--region-id", target["region_id"]]

    def clean_instance(self, value: dict, alias: str) -> dict:
        target = self.target(alias)
        if (not isinstance(value, dict) or value.get("instance_id") != target["instance_id"]
                or value.get("region_id") != target["region_id"] or value.get("status") not in STATES):
            raise CatalogError("instance target or state mismatch")
        clean = {}
        for k in INSTANCE_FIELDS:
            if k in value:
                v = value[k]
                if not isinstance(v, str) or len(v) > 256 or any(ord(c) < 32 for c in v):
                    raise CatalogError("invalid instance field")
                clean[k] = v
        return clean

    def inspect(self, alias: str) -> dict:
        result = self._run(self.args(alias, "inspect"))
        if result.get("status") != "observed" or result.get("mutation_invoked") is not False:
            raise CatalogError("instance observation unavailable")
        return {"status": "observed", "target": alias, "instance": self.clean_instance(result.get("instance"), alias),
                "mutation_invoked": False, "completion_verified": False, "health_verified": False}

    def discover(self) -> dict:
        """Fresh configured-target catalog; failed reads remain explicit, never empty successes."""
        rows = []
        for alias in self.list_targets():
            try:
                rows.append(self.inspect(alias))
            except CatalogError:
                rows.append({"target": alias, "status": "unavailable", "reason": "instance observation unavailable"})
        count = sum(row["status"] == "observed" for row in rows)
        return {"status": "observed" if count == len(rows) else "partial" if count else "unavailable",
                "scope": "configured-targets", "targets": rows, "total": len(rows), "observed_count": count,
                "mutation_invoked": False, "cached": False}

    def preview(self, alias: str, action: str) -> dict:
        if action not in ACTIONS:
            raise CatalogError("unsupported QVM action; LAS lifecycle unsupported")
        result = self._run(self.args(alias, action))
        target = self.target(alias)
        body = {"region_id": target["region_id"]}
        if action == "start":
            body["init_local_disk"] = False
        else:
            body["force_stop"] = False
            if action == "stop":
                body.update(stopped_mode="KeepCharging", confirm_stop=False)
        state = {"operation": "qvm." + action, "instance_id": target["instance_id"], "region_id": target["region_id"],
                 "instance": self.clean_instance(result.get("instance"), alias), "body": body,
                 "helper_sha256": self.fingerprint()}
        if (result.get("status") != "preview" or result.get("mutation_invoked") is not False
                or any(result.get(k) != v for k, v in state.items())
                or result.get("state_digest") != canonical_hash(state)
                or state["instance"]["status"] != ACTIONS[action]):
            raise CatalogError("invalid lifecycle preview or helper drift")
        return {**state, "state_digest": result["state_digest"], "status": "preview", "mutation_invoked": False}
