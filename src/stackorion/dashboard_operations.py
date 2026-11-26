"""Bind finite Web previews to resource identities and keep dispatch in offline fixtures."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
import sqlite3
import stat
from threading import Lock
from time import monotonic
from uuid import uuid4

from .catalog import CatalogError
from .operations import OperationRegistry


ACTIONS = {
    "reboot": ("qiniu-instance-lifecycle", "qvm.reboot", "server", {"alias", "instance_id", "region"}),
    "dns": ("cloudflare-domains", "cloudflare.dns-create.v1", "domain", {"domain", "zone_id"}),
}


@dataclass(frozen=True)
class PreviewBinding:
    id: str
    action: str
    service: str
    target: dict


def _receipt_result(row):
    if row["result"] is not None:
        return {**json.loads(row["result"]), "status": row["status"]}
    return None


class OperationHistory:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Operation records require a private directory")
        self.path = directory / "operation-records.db"
        fd = os.open(self.path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Operation records require a private file")
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS operation_record (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, created TEXT NOT NULL,
                    status TEXT NOT NULL, preview TEXT NOT NULL, result TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS operation_record_scope ON operation_record(scope, created)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, scope, preview):
        with self.connect() as db:
            record = {key: value for key, value in preview.items() if key != "plan_id"}
            db.execute("INSERT INTO operation_record VALUES (?, ?, ?, 'preview', ?, NULL)",
                       (preview["record_id"], scope, datetime.now(timezone.utc).isoformat(), json.dumps(record)))

    def update(self, identifier, status, result):
        with self.connect() as db:
            db.execute("UPDATE operation_record SET status = ?, result = ? WHERE id = ?",
                       (status, json.dumps(result), identifier))

    def binding_record(self, identifier):
        with self.connect() as db:
            return db.execute("SELECT scope, created, status, preview, result FROM operation_record WHERE id = ?",
                              (identifier,)).fetchone()

    def recent(self, scopes, *, status=None, limit=30):
        if not scopes:
            return [], False
        placeholders = ", ".join("?" for _ in scopes)
        with self.connect() as db:
            rows = db.execute(f"""
                SELECT scope, created, status, preview, result FROM operation_record
                WHERE scope IN ({placeholders}) AND status = COALESCE(?, status)
                ORDER BY created DESC, id DESC LIMIT ?
            """, (*scopes, status, limit + 1)).fetchall()
        return ([{"created_at": row["created"], "status": row["status"], "preview": json.loads(row["preview"]),
                  "result": _receipt_result(row),
                  **({"resource": scopes[row["scope"]]} if scopes[row["scope"]] else {})}
                 for row in rows[:limit]], len(rows) > limit)


class DashboardOperations:
    def __init__(self, catalog, bindings, history_dir, *, offline_registry=None):
        self.catalog = catalog
        self.bindings = bindings
        self.history = OperationHistory(history_dir)
        # Only an injected offline transport enables dispatch. CLI configuration
        # never supplies this registry.
        self.offline_registry = offline_registry
        self.jobs = {}
        self.lock = Lock()

    @classmethod
    def from_file(cls, catalog, path, history_dir):
        if path.stat().st_size > 65_536:
            raise ValueError("Operation preview configuration is too large")
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Invalid operation preview configuration")
        bindings = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "action", "service", "target"}:
                raise ValueError("Invalid operation preview binding")
            binding = PreviewBinding(**row)
            if (not isinstance(binding.id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", binding.id)
                    or binding.id in bindings or not isinstance(binding.action, str) or binding.action not in ACTIONS
                    or not isinstance(binding.service, str) or not isinstance(binding.target, dict)
                    or set(binding.target) != ACTIONS[binding.action][3]
                    or any(not isinstance(v, str) or not v or len(v) > 253 for v in binding.target.values())):
                raise ValueError("Invalid operation preview binding")
            bindings[binding.id] = binding
        return cls(catalog, bindings, history_dir)

    def _service(self, catalog, binding, *, allow_disabled=False):
        service = catalog.get(binding.service)
        if (not service.enabled and not allow_disabled) or service.adapter != ACTIONS[binding.action][0]:
            raise CatalogError("Operation is unavailable for this service")
        return service

    def _scope(self, binding, service):
        identity = [binding.id, binding.action, service.id, service.account_id, service.settings.get("account_id"),
                    binding.target, self.offline_registry is not None]
        return sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def records(self, key):
        binding = self.bindings.get(key)
        if binding is None:
            raise ValueError("Unknown preview target")
        service = self._service(self.catalog(), binding, allow_disabled=True)
        return self.history.recent({self._scope(binding, service): None})[0]

    def record(self, identifier):
        with self.lock:
            row = self.history.binding_record(identifier)
            job = self.jobs.get(identifier)
        if row is None:
            raise ValueError("Unknown operation record")
        preview = json.loads(row["preview"])
        binding = self.bindings.get(preview["binding"])
        if binding is None:
            raise ValueError("Unknown operation binding")
        service = self._service(self.catalog(), binding, allow_disabled=True)
        if row["scope"] != self._scope(binding, service):
            raise ValueError("Operation record belongs to a different target")
        confirmable = bool(preview.get("handoff") and service.enabled and row["status"] == "preview"
                           and job is not None and monotonic() - job[0] <= 300)
        return {"created_at": row["created"], "status": row["status"], "preview": preview,
                "result": _receipt_result(row), "confirmable": confirmable}

    def _bound_services(self, *, allow_disabled=False):
        catalog = self.catalog()
        for binding in self.bindings.values():
            try:
                service = self._service(catalog, binding, allow_disabled=allow_disabled)
            except CatalogError:
                continue
            yield binding, service

    @staticmethod
    def _resource_option(binding, service):
        name = binding.target["alias"] if binding.action == "reboot" else binding.target["domain"]
        return {"id": binding.id, "name": name, "account": service.account_id or service.id,
                "action": binding.action}

    def preview_targets(self):
        return [{**self._resource_option(binding, service), "service": service.id, "provider": service.provider,
                 "target": {key: value for key, value in binding.target.items() if key != "zone_id"},
                 "simulation": self.offline_registry is not None}
                for binding, service in self._bound_services()]

    def ledger(self, filters):
        if (set(filters) - {"resource", "action", "status"} or any(len(v) != 1 for v in filters.values())):
            raise ValueError("Invalid operation filters")
        selected = {key: value[0] for key, value in filters.items()}
        resource, action, status = (selected.get(key) for key in ("resource", "action", "status"))
        if (action is not None and action not in ACTIONS) or (
                status is not None and status not in {"preview", "accepted", "success", "needs-reconciliation"}):
            raise ValueError("Unsupported operation filter")
        options, scopes = [], {}
        for binding, service in self._bound_services(allow_disabled=True):
            option = self._resource_option(binding, service)
            options.append(option)
            if (resource is None or resource == binding.id) and (action is None or action == binding.action):
                scopes[self._scope(binding, service)] = option
        records, truncated = self.history.recent(scopes, status=status, limit=100)
        return {"records": records, "resources": options, "truncated": truncated, "available": True}

    def for_asset(self, catalog, asset):
        if asset.get("data_source") != "live":
            return None
        matches = []
        for binding in self.bindings.values():
            if asset["kind"] != ACTIONS[binding.action][2]:
                continue
            target = binding.target
            identity_matches = (asset["id"] == target["instance_id"] and asset.get("region") == target["region"]
                                if binding.action == "reboot" else asset["id"] == target["domain"])
            if not identity_matches:
                continue
            try:
                service = self._service(catalog, binding, allow_disabled=True)
            except CatalogError:
                continue
            if asset["account"] == f"live:{service.provider}:{service.account_id or service.id}":
                matches.append((binding, service))
        if len(matches) == 1:
            binding, service = matches[0]
            return {"id": binding.id, "action": binding.action, "simulation": self.offline_registry is not None,
                    "preview_available": service.enabled}
        return None

    def preview(self, data, *, handoff=False, work_item_id=None):
        key = data.get("binding")
        binding = self.bindings.get(key) if isinstance(key, str) else None
        if binding is None:
            raise ValueError("Unknown preview target")
        catalog = self.catalog()
        service = self._service(catalog, binding)
        target = binding.target
        registry = self.offline_registry or OperationRegistry(catalog)
        operation = ACTIONS[binding.action][1]
        if binding.action == "reboot":
            if set(data) != {"binding"}:
                raise ValueError("Invalid reboot form")
            registered = [row for row in service.settings.get("targets", [])
                          if isinstance(row, dict) and row.get("alias") == target["alias"]]
            if registered != [{"alias": target["alias"], "instance_id": target["instance_id"], "region_id": target["region"]}]:
                raise CatalogError("Reboot registration changed")
            parameters = {"alias": target["alias"]}
            plan = registry.plan(service.id, operation, **parameters)
            displayed = {"instance": plan["instance_id"], "region": plan["region_id"],
                         "current": plan["instance"]["status"], "force_stop": plan["body"]["force_stop"]}
            confirmations = {"confirm_instance_id": plan["instance_id"]}
        else:
            if (set(data) != {"binding", "name", "content", "ttl"} or type(data["ttl"]) is not int or not 60 <= data["ttl"] <= 86400
                    or any(not isinstance(data[k], str) for k in ("name", "content"))):
                raise ValueError("Invalid DNS form")
            parameters = {"zone_id": target["zone_id"], "domain": target["domain"], "record_type": "A",
                          "name": data["name"], "content": data["content"], "ttl": data["ttl"], "proxied": False}
            plan = registry.plan(service.id, operation, **parameters)
            current = plan["basis"]["current"]
            planned_name = plan["target"]["name"]
            displayed = {"zone": plan["basis"]["zone_id"], "domain": plan["basis"]["domain"],
                         "zone_status": current["zone"]["status"],
                         "existing_a": sum(row["type"] == "A" and row["name"] == planned_name
                                           for row in current["records"]),
                         "name": planned_name, "content": data["content"], "ttl": plan["target"]["ttl"]}
            confirmations = {"confirm_account_id": plan["basis"]["account_id"],
                             "confirm_zone_id": target["zone_id"], "confirm_domain": target["domain"]}
        if plan["mutation_invoked"] is not False:
            raise CatalogError("Provider did not return a read-only plan")
        identifier = str(uuid4())
        offline = self.offline_registry is not None
        preview = {"action": binding.action, "service": service.id,
                   "account": plan["basis"]["account_id"] if binding.action == "dns" else service.account_id or service.id,
                   "display": displayed, "simulation": offline, "record_id": identifier, "binding": binding.id,
                   "plan_id": identifier if offline else None, "mutation_invoked": False}
        if handoff:
            preview["handoff"] = True
            if work_item_id is not None:
                preview["work_item_id"] = work_item_id
        scope = self._scope(binding, service)
        if offline:
            with self.lock:
                now = monotonic()
                self.jobs = {key: job for key, job in self.jobs.items() if now - job[0] <= 300}
                if len(self.jobs) >= 128:
                    raise CatalogError("Offline operation session is full")
                self.history.add(scope, preview)
                self.jobs[identifier] = (now, binding, parameters, confirmations, plan["state_digest"])
        else:
            self.history.add(scope, preview)
        return preview

    def execute(self, data):
        identifier = data.get("plan_id")
        if self.offline_registry is None or set(data) != {"plan_id", "confirmed"} or data["confirmed"] is not True:
            raise ValueError("Offline confirmation required")
        if not isinstance(identifier, str):
            raise ValueError("Invalid plan")
        record = self.record(identifier)
        handoff = record["preview"].get("handoff")
        if handoff and record["status"] != "preview":
            return record["result"]
        if handoff and not record["confirmable"]:
            raise ValueError("Plan no longer available for confirmation")
        unknown = {"operation_id": identifier, "status": "needs-reconciliation"}
        with self.lock:
            job = self.jobs.get(identifier)
            if job is None or monotonic() - job[0] > 300:
                if handoff:
                    current = self.history.binding_record(identifier)
                    if current and current["status"] != "preview" and current["result"]:
                        return _receipt_result(current)
                raise ValueError("Plan already used or expired")
            self.history.update(identifier, unknown["status"], unknown)
            self.jobs.pop(identifier)
        _, binding, parameters, confirmations, digest = job
        try:
            result = self.offline_registry.apply(binding.service, ACTIONS[binding.action][1],
                                                  **parameters, **confirmations, expected_state=digest,
                                                  operation_id=identifier)
        except (CatalogError, OSError):
            return unknown
        response = {"operation_id": identifier, "status": result["status"]}
        if binding.action == "dns":
            response.update(record_verified=result.get("verified") is True, propagation_verified=False)
        self.history.update(identifier, response["status"], response)
        return response

    def verify(self, data):
        identifier = data.get("operation_id")
        if self.offline_registry is None or set(data) != {"operation_id"} or not isinstance(identifier, str):
            raise ValueError("Invalid offline receipt request")
        row = self.history.binding_record(identifier)
        if row is None or row["status"] == "preview":
            raise ValueError("Unknown receipt")
        saved = json.loads(row["preview"])
        binding = self.bindings.get(saved["binding"])
        if binding is None or row["scope"] != self._scope(
                binding, self._service(self.catalog(), binding, allow_disabled=True)):
            raise ValueError("Receipt belongs to a different target")
        receipt = self.offline_registry.service_receipt(binding.service, identifier)
        result = receipt.get("stages", {}).get("dispatch", {}).get("result", {})
        response = {"status": receipt["status"], "operation_id": identifier}
        if binding.action == "reboot":
            observed = self.offline_registry.observe(binding.service, "query", alias=binding.target["alias"],
                                                     operation_id=identifier)
            response.update(current=observed["observation"]["instance"]["status"],
                            completion_verified=observed["completion_verified"])
        else:
            response.update(record_verified=result.get("verified") is True,
                            propagation_verified=result.get("dns_propagation_verified") is True)
        self.history.update(identifier, response["status"], response)
        return response
