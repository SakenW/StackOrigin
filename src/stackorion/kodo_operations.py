"""Exact Kodo operations with plans, locks, Foundation journal and readback."""

import hashlib
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .catalog import CatalogError
from .kodo_objects import KodoObjectsAdapter, file_hashes, object_key
from .operation_runtime import OPERATION_ID, load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition

DIGEST = re.compile(r"^[0-9a-f]{64}$")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class KodoOperations:
    provider = "qiniu"
    definitions = (OperationDefinition("kodo.put.v1", "plan_kodo_upload", "apply_kodo_upload", "get_kodo_operation"),
                   OperationDefinition("kodo.download.v1", "plan_kodo_download", "apply_kodo_download", "get_kodo_operation"),
                   OperationDefinition("kodo.delete-object.v1", "plan_kodo_object_deletion", "apply_kodo_object_deletion", "get_kodo_operation"),
                   OperationDefinition("kodo.copy-object.v1", "plan_kodo_copy", "apply_kodo_copy", "get_kodo_operation"),
                   OperationDefinition("kodo.rename-object.v1", "plan_kodo_rename", "apply_kodo_rename", "get_kodo_operation"))

    def __init__(self, catalog):
        self.catalog = catalog

    def adapter(self, service_id):
        service = self.catalog.get(service_id)
        if service.provider != "qiniu" or service.adapter != "qiniu-kodo-objects":
            raise CatalogError("service does not support Kodo object operations")
        adapter = self.catalog.adapter(service)
        if not isinstance(adapter, KodoObjectsAdapter):
            raise CatalogError("unexpected Kodo object adapter")
        return service, adapter

    @staticmethod
    def check(operation, key, parameters):
        object_key(key)
        if operation == "kodo.put.v1":
            if set(parameters) != {"source_path", "mime_type"}:
                raise CatalogError("invalid upload parameters")
            if (not isinstance(parameters["source_path"], str) or len(parameters["source_path"]) > 4096
                    or not Path(parameters["source_path"]).is_absolute()
                    or not isinstance(parameters["mime_type"], str)
                    or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", parameters["mime_type"])
                    or len(parameters["mime_type"]) > 128):
                raise CatalogError("invalid upload file or MIME type")
        elif operation == "kodo.download.v1":
            if set(parameters) != {"filename"} or not isinstance(parameters["filename"], str):
                raise CatalogError("invalid download parameters")
        elif operation == "kodo.delete-object.v1":
            if parameters or any(c in key for c in '*?') or key.endswith('/'):
                raise CatalogError("deletion requires one exact object key without wildcard or directory syntax")
        elif operation in {"kodo.copy-object.v1", "kodo.rename-object.v1"}:
            if set(parameters) != {"destination_key"}:
                raise CatalogError("one exact destination key is required")
            destination = object_key(parameters['destination_key'])
            if destination == key or any(c in key + destination for c in '*?') or key.endswith('/') or destination.endswith('/'):
                raise CatalogError("distinct exact source and destination keys without wildcard or directory syntax are required")
        else:
            raise CatalogError("unsupported Kodo operation")

    def _plan(self, service_id, operation, alias, key, parameters):
        self.check(operation, key, parameters)
        service, adapter = self.adapter(service_id)
        target, client, module, fingerprint, endpoints = adapter.context(alias)
        remote = adapter.stat_with_client(client, target["bucket"], key)
        basis = {"operation": operation, "service": service_id, "account_id": service.account_id,
                 "target": alias, "bucket": target["bucket"], "key": key, "parameters": parameters,
                 "foundation_sha256": fingerprint, "remote": remote, "endpoints": endpoints,
                 "s3_bucket": target["s3_bucket"], "max_transfer_bytes": adapter.limit}
        if operation == "kodo.put.v1":
            if remote is not None:
                raise CatalogError("object already exists; uploads do not overwrite")
            source = adapter.upload_source(parameters["source_path"])
            basis["local"] = {"path": str(source), **file_hashes(source)}
            if not 0 < basis["local"]["size"] <= adapter.limit:
                raise CatalogError("upload source exceeds transfer limit")
        elif operation == "kodo.delete-object.v1":
            if remote is None:
                raise CatalogError("object does not exist; no deletion can be planned")
            if type(remote.get("putTime")) is not int or remote["putTime"] <= 0:
                raise CatalogError("object upload time is unavailable; deletion cannot be planned")
            basis["impact"] = {"object_count": 1, "irreversible": True,
                               "storage_type": remote.get("type"),
                               "concurrency": "metadata is rechecked before dispatch; provider delete has no conditional version match"}
        elif operation in {"kodo.copy-object.v1", "kodo.rename-object.v1"}:
            if remote is None or type(remote.get('putTime')) is not int or remote['putTime'] <= 0:
                raise CatalogError("source object or upload time is unavailable")
            destination = adapter.stat_with_client(client, target['bucket'], parameters['destination_key'])
            if destination is not None:
                raise CatalogError("destination object exists; no overwrite is allowed")
            basis['destination'] = None
            basis['impact'] = {'source_count': 1, 'destination_count': 1, 'overwrite': False,
                               'source_removed': operation == 'kodo.rename-object.v1',
                               'concurrency': 'source metadata is rechecked; external writers are not locked by this client'}
        else:
            if remote is None:
                raise CatalogError("object does not exist")
            if remote["fsize"] > adapter.limit:
                raise CatalogError("download exceeds transfer limit")
            output = adapter.output_path(parameters["filename"])
            if shutil.disk_usage(output.parent).free < remote["fsize"] + 16 * 1024 * 1024:
                raise CatalogError("insufficient free disk space for download")
            basis["local"] = {"path": str(output), "destination_exists": False}
        authorization = 'execution requires client approval of exact bucket, key and local file'
        if operation == 'kodo.delete-object.v1':
            authorization = 'execution requires client approval of exact bucket and key'
        elif operation in {'kodo.copy-object.v1', 'kodo.rename-object.v1'}:
            authorization = 'execution requires client approval of exact bucket and source/destination keys'
        return {"status": "planned", "mode": "plan", **basis, "state_digest": digest(basis),
                "mutation_invoked": False,
                "authorization": authorization}, (adapter, client, module, target, endpoints)

    def plan(self, service_id, operation, alias, key, **parameters):
        return self._plan(service_id, operation, alias, key, parameters)[0]

    def _delete(self, plan, context):
        adapter, client, module, target, endpoints = context
        accepted = False
        try:
            # Existing Foundation primitive, exactly one key and no safe-retry flag.
            client.delete_keys(plan["bucket"], [plan["key"]])
            accepted = True
        except Exception:
            pass
        remaining = adapter.stat_with_client(client, plan["bucket"], plan["key"])
        if remaining is not None:
            raise CatalogError("deletion is unverified or object was recreated; reconcile without replay")
        return {"status": "success", "operation": plan["operation"], "bucket": plan["bucket"],
                "key": plan["key"], "deleted_count": 1, "mutation_invoked": True,
                "verification": "exact key is absent in remote metadata readback",
                "dispatch_outcome": "accepted" if accepted else "unknown-readback-verified"}

    def _change_key(self, plan, context):
        adapter, client, module, target, endpoints = context
        destination = plan['parameters']['destination_key']
        rename = plan['operation'] == 'kodo.rename-object.v1'
        accepted = False
        try:
            adapter.change_key(client, plan['bucket'], plan['key'], destination, rename=rename)
            accepted = True
        except Exception:
            pass
        after_destination = adapter.stat_with_client(client, plan['bucket'], destination)
        after_source = adapter.stat_with_client(client, plan['bucket'], plan['key'])
        destination_matches = (after_destination is not None and
                               all(after_destination.get(k) == plan['remote'].get(k) for k in ('hash', 'fsize', 'mimeType')))
        source_matches = after_source is None if rename else after_source == plan['remote']
        if not destination_matches or not source_matches:
            raise CatalogError('source/destination outcome is unverified; reconcile without replay')
        return {'status': 'success', 'operation': plan['operation'], 'bucket': plan['bucket'],
                'key': plan['key'], 'destination_key': destination, 'hash': after_destination['hash'],
                'size': after_destination['fsize'], 'source_removed': rename, 'overwrite': False,
                'mutation_invoked': True, 'verification': 'source/destination metadata satisfy approved operation',
                'dispatch_outcome': 'accepted' if accepted else 'unknown-readback-verified'}

    def _transfer(self, plan, context):
        adapter, client, module, target, endpoints = context
        if plan["operation"] == "kodo.put.v1":
            local = plan["local"]
            returned_hash = None
            try:
                returned_hash = client.put_file(plan["bucket"], plan["key"], Path(local["path"]),
                         plan["parameters"]["mime_type"], expected_size=local["size"],
                         expected_identity=tuple(local["file_identity"]))
            except Exception:
                # One dispatch only. A read-only probe may settle an unknown result.
                pass
            remote = adapter.stat_with_client(client, plan["bucket"], plan["key"])
            if (remote is None or remote["hash"] != local["hash"] or remote["fsize"] != local["size"]
                    or (returned_hash is not None and returned_hash != local["hash"])):
                raise CatalogError("upload result is unknown or does not match approved file; reconcile without replay")
            return {"status": "success", "operation": plan["operation"], "bucket": plan["bucket"], "key": plan["key"],
                    "size": local["size"], "hash": local["hash"], "sha256": local["sha256"], "mutation_invoked": True,
                    "verification": "remote size and Qiniu content hash match approved local file",
                    "dispatch_outcome": "accepted" if returned_hash else "unknown-readback-verified"}
        destination = Path(plan["local"]["path"])
        # Validate the destination again before creating a uniquely owned temporary file.
        adapter.output_path(plan["parameters"]["filename"])
        fd, name = tempfile.mkstemp(prefix=".kodo-download-", dir=adapter.download_root)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                adapter.download_stream(client, module, target, endpoints, plan["key"], handle, plan["remote"]["fsize"])
                handle.flush()
                os.fsync(handle.fileno())
            hashes = file_hashes(temporary)
            if hashes["size"] != plan["remote"]["fsize"] or hashes["hash"] != plan["remote"]["hash"]:
                raise CatalogError("downloaded content does not match approved object metadata")
            # link fails if another writer created the destination; never replace a user file.
            os.link(temporary, destination, follow_symlinks=False)
            return {"status": "success", "operation": plan["operation"], "bucket": plan["bucket"], "key": plan["key"],
                    "file": str(destination), "size": hashes["size"], "hash": hashes["hash"], "sha256": hashes["sha256"],
                    "mutation_invoked": True, "verification": "local size and Qiniu content hash match approved object"}
        finally:
            temporary.unlink(missing_ok=True)

    def apply(self, service_id, operation, alias, key, expected_state, confirm_bucket, operation_id,
              confirm_key=None, confirm_destination_key=None, **parameters):
        self.check(operation, key, parameters)
        exact_key_operations = {'kodo.delete-object.v1', 'kodo.copy-object.v1', 'kodo.rename-object.v1'}
        key_changes = {'kodo.copy-object.v1', 'kodo.rename-object.v1'}
        if operation in exact_key_operations and confirm_key != key:
            raise CatalogError("exact object key confirmation is required")
        if operation not in exact_key_operations and confirm_key is not None:
            raise CatalogError("object key confirmation is only accepted for exact object mutations")
        if operation in key_changes and confirm_destination_key != parameters['destination_key']:
            raise CatalogError('exact destination key confirmation is required')
        if operation not in key_changes and confirm_destination_key is not None:
            raise CatalogError('destination confirmation is only accepted for key changes')
        service, adapter = self.adapter(service_id)
        if alias not in adapter.targets or confirm_bucket != adapter.targets[alias]["bucket"]:
            raise CatalogError("exact configured bucket confirmation is required")
        if not isinstance(expected_state, str) or not DIGEST.fullmatch(expected_state):
            raise CatalogError("approved state digest is required")
        if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
            raise CatalogError("invalid operation id")
        _, fingerprint = adapter.foundation()
        identity = {"operation": operation, "service": service_id, "account_id": service.account_id,
                    "bucket": confirm_bucket, "target": alias, "key": key, "parameters": parameters,
                    "expected_state": expected_state, "foundation_sha256": fingerprint}
        journal = load_journal(adapter.foundation_root / "scripts/foundation_operation.py")
        root = state_root()
        keys = [key, parameters['destination_key']] if operation in key_changes else [key]
        resources = sorted({json.dumps([service.account_id or service_id, confirm_bucket, k], separators=(',', ':')) for k in keys})
        stage = {'kodo.delete-object.v1': 'delete', 'kodo.copy-object.v1': 'copy', 'kodo.rename-object.v1': 'rename'}.get(operation, 'transfer')
        with ExitStack() as locks:
            for resource in resources:
                locks.enter_context(resource_lock(root, resource))
            try:
                with journal.Operation(root, operation_id, identity, resume=True) as job:
                    reused = job.value["stages"].get(stage, {}).get("status") == "success"

                    def execute():
                        plan, context = self._plan(service_id, operation, alias, key, parameters)
                        if plan["state_digest"] != expected_state:
                            raise CatalogError("approved Kodo state changed; no transfer invoked")
                        if operation == 'kodo.delete-object.v1':
                            return self._delete(plan, context)
                        if operation in key_changes:
                            return self._change_key(plan, context)
                        return self._transfer(plan, context)

                    result = job.stage(stage, execute)
                    job.complete()
                    return {**result, "operation_id": operation_id, "reused_completed_receipt": reused,
                            "live_state_verified_this_invocation": not reused}
            except (journal.OperationError, CatalogError, OSError) as exc:
                return {"status": "needs-reconciliation", "operation_id": operation_id,
                        "reason": str(exc) if isinstance(exc, CatalogError) else "operation interrupted or receipt requires reconciliation",
                        "replayed": False}

    def receipt(self, operation_id):
        return read_receipt(operation_id, {"kodo.put.v1", "kodo.download.v1", "kodo.delete-object.v1", "kodo.copy-object.v1", "kodo.rename-object.v1"})
