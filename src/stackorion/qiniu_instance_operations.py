"""One-dispatch lifecycle operations using the existing Foundation journal and locks."""

from contextlib import ExitStack
from uuid import UUID

from .catalog import CatalogError
from .operation_runtime import load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition
from .qiniu_instance import ADAPTER_NAME, ACTIONS, DIGEST, QiniuInstanceAdapter, canonical_hash, owned, valid_token

OPERATIONS = frozenset("qvm." + action for action in ACTIONS)


def operation_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise CatalogError("canonical operation UUID required") from None
    return value


class QiniuInstanceOperations:
    provider = "qiniu"
    definitions = tuple(OperationDefinition("qvm." + action, "plan_qiniu_instance_action",
                                           "apply_qiniu_instance_action", "get_qiniu_instance_operation")
                        for action in ACTIONS)

    def __init__(self, catalog):
        self.catalog = catalog

    def adapter(self, service_id):
        service = self.catalog.get(service_id)
        if (not service.enabled or service.kind != "python-adapter" or service.provider != "qiniu"
                or service.adapter != ADAPTER_NAME):
            raise CatalogError("service does not support QVM instance lifecycle; LAS unsupported")
        adapter = self.catalog.adapter(service)
        if not isinstance(adapter, QiniuInstanceAdapter):
            raise CatalogError("unexpected instance lifecycle adapter")
        return service, adapter

    @staticmethod
    def action(operation):
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise CatalogError("unsupported instance operation; LAS lifecycle unsupported")
        return operation.split(".")[1]

    def _identity(self, service, adapter, alias, operation):
        target = adapter.target(alias)
        return {"service": service.id, "account_id": service.account_id, "operation": operation,
                "instance_id": target["instance_id"], "region_id": target["region_id"],
                "helper_sha256": adapter.fingerprint(), "credential_sha256": adapter.credential_fingerprint()}

    def _plan(self, service, adapter, operation, alias):
        identity = self._identity(service, adapter, alias, operation)
        plan = adapter.preview(alias, self.action(operation))
        if identity != self._identity(service, adapter, alias, operation):
            raise CatalogError("lifecycle helper or credential changed during preview")
        return {**plan, "state_digest": canonical_hash({**identity, "helper_state": plan["state_digest"]}),
                "service": service.id, "account_id": service.account_id,
                "authorization": "client approval of this exact target, action and digest required"}, plan["state_digest"]

    def plan(self, service_id: str, operation: str, *, alias: str) -> dict:
        self.action(operation)
        service, adapter = self.adapter(service_id)
        return self._plan(service, adapter, operation, alias)[0]

    def _resources(self, service, target):
        # Old QVM plugin keys locks by service+instance. Include every matching legacy
        # service in this catalog as well as an instance-global lock for new adapters.
        keys = {service.id + target["instance_id"], "qvm-instance:" + target["instance_id"]}
        for public in self.catalog.list():
            candidate = self.catalog.get(public["id"])
            if candidate.provider == "qiniu" and candidate.adapter == "qiniu-qvm":
                if any(isinstance(t, dict) and t.get("instance_id") == target["instance_id"]
                       for t in candidate.settings.get("targets", [])):
                    keys.add(candidate.id + target["instance_id"])
        return sorted(keys)

    def apply(self, service_id: str, operation: str, *, alias: str, expected_state: str,
              confirm_instance_id: str, operation_id: str) -> dict:
        action = self.action(operation)
        operation_uuid(operation_id)
        service, adapter = self.adapter(service_id)
        target = adapter.target(alias)
        if (confirm_instance_id != target["instance_id"] or not isinstance(expected_state, str)
                or not DIGEST.fullmatch(expected_state)):
            raise CatalogError("exact instance confirmation and preview digest required")
        identity = {**self._identity(service, adapter, alias, operation), "expected_state": expected_state}
        source = adapter.script.parent / "qiniu-server-foundation/scripts/foundation_operation.py"
        owned(source)
        journal = load_journal(source)
        root = state_root()
        try:
            with ExitStack() as locks:
                for key in self._resources(service, target):
                    locks.enter_context(resource_lock(root, key))
                with journal.Operation(root, operation_id, identity, resume=True) as job:
                    reused = job.value["stages"].get("dispatch", {}).get("status") == "success"

                    def dispatch():
                        plan, helper_state = self._plan(service, adapter, operation, alias)
                        if plan["state_digest"] != expected_state:
                            raise CatalogError("approved lifecycle state changed")
                        if identity != {**self._identity(service, adapter, alias, operation), "expected_state": expected_state}:
                            raise CatalogError("lifecycle identity changed before dispatch")
                        result = adapter._run(adapter.args(alias, action) + ["--execute", "--expected-state", helper_state,
                                              "--confirm-instance-id", confirm_instance_id, "--operation-id", operation_id])
                        return self._clean_result(result, identity, operation_id)

                    result = job.stage("dispatch", dispatch)
                    # Stage success means a saved receipt, never a completed lifecycle transition.
                    job.value["status"] = result["status"]
                    job.save()
                    return {**result, "reused_completed_receipt": reused,
                            "live_state_verified_this_invocation": False,
                            "reconciliation": {"method": "query", "service_id": service_id, "alias": alias,
                                               "operation_id": operation_id, "read_only": True}}
        except Exception:
            # The journal has already made any entered stage non-retryable. Do not
            # let unexpected provider/helper errors leak payloads to the transport.
            return {"status": "needs-reconciliation", "operation_id": operation_id, "replayed": False,
                    "completion_verified": False, "health_verified": False,
                    "reason": "dispatch blocked or outcome unknown; inspect receipt and query without replay"}

    @staticmethod
    def _clean_result(result, identity, operation_id):
        if (not isinstance(result, dict)
                or any(result.get(k) != identity[k] for k in ("operation", "instance_id", "region_id"))
                or result.get("operation_id") != operation_id
                or result.get("status") not in {"accepted", "needs-reconciliation"}
                or result.get("mutation_invoked") is not True
                or result.get("completion_verified") is not False or result.get("health_verified") is not False
                or result.get("replayed") is not False
                or (result.get("status") == "accepted" and not valid_token(result.get("request_id")))):
            raise CatalogError("lifecycle dispatch receipt invalid; outcome unknown")
        return {k: result[k] for k in ("operation", "instance_id", "region_id", "operation_id", "status",
                                       "mutation_invoked", "completion_verified", "health_verified", "replayed")} | {
            "request_id": result.get("request_id") if result["status"] == "accepted" else None}

    def receipt(self, operation_id: str) -> dict:
        value = read_receipt(operation_uuid(operation_id), OPERATIONS)
        value["identity"].pop("credential_sha256", None)
        return value

    def query(self, service_id: str, *, alias: str, operation_id: str) -> dict:
        """Read-only reconciliation: a current Running state cannot prove a reboot."""
        receipt = read_receipt(operation_uuid(operation_id), OPERATIONS)
        service, adapter = self.adapter(service_id)
        target = adapter.target(alias)
        identity = receipt["identity"]
        if (identity.get("service") != service_id or identity.get("account_id") != service.account_id
                or any(identity.get(k) != target[k] for k in ("instance_id", "region_id"))
                or identity.get("credential_sha256") != adapter.credential_fingerprint()):
            raise CatalogError("reconciliation target differs from receipt")
        observation = adapter.inspect(alias)
        if identity.get("credential_sha256") != adapter.credential_fingerprint():
            raise CatalogError("reconciliation credential changed during query")
        return {"operation_id": operation_id, "receipt_status": receipt["status"], "observation": observation,
                "status": "observed", "completion_verified": False, "health_verified": False,
                "mutation_invoked": False, "replayed": False,
                "reason": "current state alone does not prove operation completion or application health"}
