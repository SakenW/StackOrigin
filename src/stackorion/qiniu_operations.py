"""Scoped QVM operations with state checks and the existing Foundation journal."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re

from .catalog import CatalogError
from .operations import OperationDefinition
from .operation_runtime import state_root as _state_root, resource_lock, load_journal
from .qiniu import QiniuQVMAdapter, _regular_owned

OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class QiniuQVMOperations:
    """QVM implementation; transport and plugin discovery live outside this module."""

    provider = "qiniu"
    definitions = (OperationDefinition("qvm.allow-port", "plan_qiniu_port_access",
                                       "apply_qiniu_port_access", "get_qiniu_operation"),
                   OperationDefinition("qvm.delete-ingress-rule", "plan_qiniu_rule_deletion",
                                       "apply_qiniu_rule_deletion", "get_qiniu_operation"))

    def __init__(self, catalog):
        self.catalog = catalog

    @staticmethod
    def _check(operation):
        if operation not in {"qvm.allow-port", "qvm.delete-ingress-rule"}:
            raise CatalogError("unsupported QVM operation")

    def plan(self, service_id, operation, **parameters):
        self._check(operation)
        handler = plan_port_access if operation == "qvm.allow-port" else plan_rule_deletion
        return handler(self.catalog, service_id, **parameters)

    def apply(self, service_id, operation, **parameters):
        self._check(operation)
        handler = apply_port_access if operation == "qvm.allow-port" else apply_rule_deletion
        return handler(self.catalog, service_id, **parameters)

    def receipt(self, operation_id):
        return operation_status(operation_id)


def _adapter(catalog, service_id):
    service = catalog.get(service_id)
    if service.provider != "qiniu" or service.adapter != "qiniu-qvm":
        raise CatalogError("service does not support QVM port access operations")
    adapter = catalog.adapter(service)
    if not isinstance(adapter, QiniuQVMAdapter):
        raise CatalogError("unexpected QVM operation adapter")
    return service, adapter


def _args(adapter, alias, port, cidr, description):
    if alias not in adapter.targets:
        raise CatalogError("unknown QVM operation target")
    if type(port) is not int or not 1 <= port <= 65535:
        raise CatalogError("invalid TCP port")
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise CatalogError("invalid source CIDR") from exc
    if network.version != 4:
        raise CatalogError("this operation supports IPv4 CIDR only")
    if not isinstance(description, str) or len(description) > 255 or any(ord(c) < 32 for c in description):
        raise CatalogError("invalid rule description")
    return ["--instance-id", adapter.targets[alias], "allow-port", "--port", str(port),
            "--cidr", str(network), "--description", description]


def plan_port_access(catalog, service_id, alias, port, cidr, description):
    service, adapter = _adapter(catalog, service_id)
    result = adapter._run(_args(adapter, alias, port, cidr, description))
    if (result.get("instance_id") != adapter.targets[alias] or result.get("mutation_invoked") is not False
            or result.get("operation") != "qvm.allow-port" or not DIGEST.fullmatch(str(result.get("state_digest", "")))):
        raise CatalogError("invalid QVM operation preview")
    return {**result, "service": service_id, "account_id": service.account_id,
            "authorization": "preview only; execution requires approval of these exact parameters in the client"}


def _delete_args(adapter, alias, security_group_id, rule_id):
    if alias not in adapter.targets:
        raise CatalogError("unknown QVM operation target")
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
               for value in (security_group_id, rule_id)):
        raise CatalogError("exact security group and rule IDs are required")
    return ["--instance-id", adapter.targets[alias], "delete-ingress-rule",
            "--security-group-id", security_group_id, "--rule-id", rule_id]


def plan_rule_deletion(catalog, service_id, alias, security_group_id, rule_id):
    service, adapter = _adapter(catalog, service_id)
    result = adapter._run(_delete_args(adapter, alias, security_group_id, rule_id))
    if (result.get("instance_id") != adapter.targets[alias] or result.get("mutation_invoked") is not False
            or result.get("operation") != "qvm.delete-ingress-rule"
            or result.get("security_group_id") != security_group_id or result.get("rule_id") != rule_id
            or not DIGEST.fullmatch(str(result.get("state_digest", "")))):
        raise CatalogError("invalid QVM rule deletion preview")
    return {**result, "service": service_id, "account_id": service.account_id,
            "authorization": "preview only; execution requires client approval of this exact rule and group-wide impact"}


def apply_rule_deletion(catalog, service_id, alias, security_group_id, rule_id,
                        expected_state, confirm_instance_id, operation_id):
    service, adapter = _adapter(catalog, service_id)
    args = _delete_args(adapter, alias, security_group_id, rule_id)
    return _apply_operation(service, adapter, args, "qvm.delete-ingress-rule", "delete-ingress-rule",
                            {"security_group_id": security_group_id, "rule_id": rule_id},
                            expected_state, confirm_instance_id, operation_id)


def _journal_module(adapter):
    source = adapter.script.parent / "qiniu-server-foundation/scripts/foundation_operation.py"
    _regular_owned(source, secret=False)
    return load_journal(source)


def apply_port_access(catalog, service_id, alias, port, cidr, description,
                      expected_state, confirm_instance_id, operation_id):
    service, adapter = _adapter(catalog, service_id)
    args = _args(adapter, alias, port, cidr, description)
    return _apply_operation(service, adapter, args, "qvm.allow-port", "allow-port",
                            {"port": port, "cidr": str(ipaddress.ip_network(cidr, strict=False)), "description": description},
                            expected_state, confirm_instance_id, operation_id)


def _apply_operation(service, adapter, args, operation_name, stage_name, parameters,
                     expected_state, confirm_instance_id, operation_id):
    service_id = service.id
    if confirm_instance_id != args[1] or not isinstance(expected_state, str) or not DIGEST.fullmatch(expected_state):
        raise CatalogError("exact instance confirmation and preview state digest are required")
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise CatalogError("invalid operation id")
    _regular_owned(adapter.script, secret=False)
    journal = _journal_module(adapter)
    root = _state_root()
    identity = {"operation": operation_name, "service": service_id, "account_id": service.account_id,
                "instance_id": confirm_instance_id, **parameters, "expected_state": expected_state,
                "helper_sha256": hashlib.sha256(adapter.script.read_bytes()).hexdigest()}
    with resource_lock(root, service_id + confirm_instance_id):
        try:
            with journal.Operation(root, operation_id, identity, resume=True) as operation:
                reused = operation.value["stages"].get(stage_name, {}).get("status") == "success"

                def execute():
                    if hashlib.sha256(adapter.script.read_bytes()).hexdigest() != identity["helper_sha256"]:
                        raise CatalogError("QVM helper changed before execution")
                    receipt = adapter._run(args + ["--execute", "--expected-state", expected_state,
                                                   "--confirm-instance-id", confirm_instance_id])
                    if receipt.get("instance_id") != confirm_instance_id or receipt.get("operation") != operation_name:
                        raise CatalogError("QVM execution target mismatch")
                    if receipt.get("status") not in {"success", "needs-reconciliation"}:
                        raise CatalogError("QVM execution result is unknown")
                    if operation_name == "qvm.delete-ingress-rule":
                        rows = receipt.get("results")
                        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                                or rows[0].get("security_group_id") != parameters["security_group_id"]
                                or rows[0].get("rule_id") != parameters["rule_id"]
                                or (receipt["status"] == "success" and rows[0].get("status") != "verified")):
                            raise CatalogError("QVM deletion readback target mismatch")
                    return {key: receipt[key] for key in ("operation", "instance_id", "status", "results", "mutation_invoked")}

                result = operation.stage(stage_name, execute)
                if result["status"] == "success":
                    operation.complete()
                else:
                    operation.value["status"] = "needs-reconciliation"
                    operation.save()
                return {**result, "operation_id": operation_id, "operation_file": str(operation.path),
                        "reused_completed_receipt": reused,
                        "live_state_verified_this_invocation": not reused and result["status"] == "success"}
        except (journal.OperationError, CatalogError) as exc:
            return {"status": "needs-reconciliation", "operation_id": operation_id, "reason": str(exc),
                    "replayed": False}

def operation_status(operation_id):
    if not OPERATION_ID.fullmatch(operation_id):
        raise CatalogError("invalid operation id")
    path = _state_root(create=False) / operation_id / "operation.json"
    _regular_owned(path, secret=True)
    data = json.loads(path.read_text())
    return {"operation_id": operation_id, "status": data.get("status"), "identity": data.get("identity"),
            "stages": data.get("stages"), "updated_at": data.get("updated_at")}
