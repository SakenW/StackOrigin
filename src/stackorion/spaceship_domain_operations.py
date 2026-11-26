"""Bounded domain stages and explicit local resolution using Foundation's journal."""

from contextlib import ExitStack
import json
import os
import re
import stat

from .catalog import CatalogError
from .operation_runtime import load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition
from .spaceship_management import (ASYNC_ID, DIGEST, OPERATIONS, SpaceshipManagementAdapter,
                                   canonical_uuid, digest, domain_name, parameters as validate_parameters, record_key, record_identity, dns_owner)


def _root(*, create=True):
    # Unlike the general receipt helper, never fall back to the home directory.
    if not os.environ.get("STACKORION_OPERATION_ROOT"):
        raise CatalogError("explicit private STACKORION_OPERATION_ROOT required")
    return state_root(create=create)


def _receipt(operation_id):
    canonical_uuid(operation_id)
    _root(create=False)
    return read_receipt(operation_id, OPERATIONS)


def _bounded_preview(plan):
    # Runtime receipt reader has a 2 MiB cap. Leave room for all stages and a
    # later local resolution; never dispatch an operation we cannot read back.
    if len(json.dumps({"stages": {"approved-state": {"result": plan}}}, ensure_ascii=True, indent=2).encode("utf-8")) > 524288:
        raise CatalogError("complete approved snapshot exceeds safe journal budget; no mutation allowed")
    return plan


def _expected(snapshot, action, values):
    # A complete observation is safe on custom NS; DNS writes are not.
    if action.startswith("dns-") and snapshot.get("nameservers", {}).get("provider") != "basic":
        raise CatalogError("DNS mutation requires verified basic nameservers")
    if action == "dns-replace":
        old = values["record"]
        exact = {**old, "group": "custom"}
        if snapshot["records"].count(exact) != 1:
            raise CatalogError("one exact reviewed custom source including TTL required")
        intermediate = _expected(snapshot, "dns-delete", {"record": {k: v for k, v in old.items() if k != "ttl"}})
        return _expected(intermediate, "dns-add", {"record": values["replacement"]})
    result = json.loads(json.dumps(snapshot))
    if action.startswith("dns-"):
        key = record_key(values["record"])
        matches = [r for r in result["records"] if record_key(r) == key]
        aliases = [r for r in result["records"] if record_identity(r) == record_identity(values["record"])]
        if aliases != matches:
            raise CatalogError("record spelling aliases an existing tuple; exact reviewed selector required")
        if action == "dns-add":
            if matches:
                raise CatalogError("record already exists; add cannot update it")
            # A CNAME conflicts with every other type at its owner.
            if any(dns_owner(r) == dns_owner(values["record"]) and
                   "CNAME" in (r["type"], values["record"]["type"]) for r in result["records"]):
                raise CatalogError("record conflicts with existing owner")
            result["records"].append({**values["record"], "group": "custom"})
        else:
            if len(matches) != 1 or matches[0]["group"] != "custom":
                raise CatalogError("exactly one existing custom record required")
            old = matches[0]
            if action == "dns-update-ttl":
                if old != {**values["record"], "group": "custom"}:
                    raise CatalogError("reviewed record TTL does not match")
                old["ttl"] = values["ttl"]
            else:
                result["records"].remove(old)
        result["records"].sort(key=digest)
    elif action == "nameservers":
        if values["provider"] == "basic" and snapshot["nameservers"]["provider"] == "basic":
            raise CatalogError("basic nameservers are already configured")
        result["nameservers"] = values
    else:
        field, parameter = {"auto-renew": ("autoRenew", "isEnabled"),
                            "transfer-lock": ("transferLocked", "isLocked"),
                            "privacy": ("privacyLevel", "privacyLevel")}[action]
        result[field] = values[parameter]
    if result == snapshot:
        raise CatalogError("requested state is already present")
    return result


def _matches(observed, expected, action, values):
    if action == "nameservers" and values["provider"] == "basic":
        observed = json.loads(json.dumps(observed))
        ns = observed.get("nameservers", {})
        if ns.get("provider") != "basic" or not ns.get("hosts"):
            return False
        observed["nameservers"] = {"provider": "basic"}
    return observed == expected


def _marker_paths(root, resources):
    directory = root / "spaceship-resources"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CatalogError("unsafe Spaceship resource state")
    return [directory / (digest(resource) + ".json") for resource in resources]


def _check_markers(paths, operation_id):
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or
                    info.st_nlink != 1 or info.st_size > 256):
                raise CatalogError("unsafe Spaceship resource marker")
            with os.fdopen(fd, "r", closefd=False) as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or set(value) != {"operation_id"}:
                raise CatalogError("invalid Spaceship resource marker")
            canonical_uuid(value["operation_id"])
            if value["operation_id"] != operation_id:
                # A completed receipt permits a subsequent operation even if a
                # crash happened before clearing its marker. Unknown never does.
                old = _receipt(value["operation_id"])
                if not _finished(old):
                    raise CatalogError("domain has an unresolved operation; observe its original receipt")
        except (OSError, ValueError, TypeError):
            raise CatalogError("Spaceship resource marker unavailable") from None
        finally:
            os.close(fd)


def _resolution(receipt):
    stage = (receipt.get("stages") or {}).get("receipt-resolution", {})
    return stage.get("result") if stage.get("status") == "success" else None


def _finished(receipt):
    resolved = _resolution(receipt)
    return receipt["status"] == "success" or (isinstance(resolved, dict) and
        resolved.get("outcome") in {"certified-success", "adopted-observed-state"})


def _phase(job, adapter, domain, operation, values, credentials, desired, name, acceptance_name):
    """Caller enters a durable running stage before this single dispatch."""
    response = None
    try:
        response = adapter._dispatch(domain, operation, values, credentials)
    except Exception:
        pass
    job.stage(acceptance_name, lambda: {"accepted": response is not None,
              "http_status": response.status if response else None,
              "async_id": response.async_id if response else None})
    observed = None
    try:
        observed = adapter._snapshot(domain, operation.removeprefix("spaceship.").removesuffix(".v1"), credentials)
    except Exception:
        pass
    action = operation.removeprefix("spaceship.").removesuffix(".v1")
    verified = observed is not None and _matches(observed, desired, action, values)
    success = response is not None and response.status != 202 and response.async_id is None and verified
    return {"status": "success" if success else "needs-reconciliation", "phase": name,
            "mutation_invoked": True, "api_accepted": response is not None,
            "provider_async_id": response.async_id if response else None,
            "registrar_state_matches": verified, "observed_digest": digest(observed) if observed is not None else None,
            "replayed": False, "dns_propagation_verified": False, "https_health_verified": False}


class SpaceshipDomainOperations:
    provider = "spaceship"
    definitions = tuple(OperationDefinition(name, "plan_service_operation",
                                            "apply_service_operation", "get_service_operation")
                        for name in sorted(OPERATIONS))

    def __init__(self, catalog):
        self.catalog = catalog

    def operation_upstream(self, service_id, operation):
        if operation.startswith("spaceship.dns-"):
            return self.adapter(service_id)[1].dns_upstream
        return None

    def adapter(self, service_id):
        service = self.catalog.get(service_id)
        if (service.provider != self.provider or service.adapter != "spaceship-management" or
                not service.enabled or service.kind != "python-adapter"):
            raise CatalogError("service does not support Spaceship management")
        adapter = self.catalog.adapter(service)
        if not isinstance(adapter, SpaceshipManagementAdapter):
            raise CatalogError("unexpected Spaceship management adapter")
        return service, adapter

    @staticmethod
    def _intent(service, adapter, operation, domain, values):
        return {"operation": operation, "service": service.id, "account_id": service.account_id,
                "domain": domain, "parameters": values, **adapter._paths()}

    @staticmethod
    def _preview(intent, adapter, action, values, credentials):
        if action == "receipt-resolve":
            return _resolution_preview(intent, adapter, values, credentials)
        binding = adapter._binding(credentials)
        current = adapter._snapshot(intent["domain"], action, credentials)
        desired = _expected(current, action, values)
        basis = {**intent, "binding": binding, "current": current, "desired": desired}
        if action == "dns-replace":
            basis["intermediate"] = _expected(current, "dns-delete", {
                "record": {k: v for k, v in values["record"].items() if k != "ttl"}})
            basis["resolution_gap"] = "Exact delete precedes exact add; DNS resolution may be unavailable between phases. No automatic rollback or resume."
        return _bounded_preview({**basis, "state_digest": digest(basis), "status": "planned", "mutation_invoked": False,
                **({"upstream": adapter.dns_upstream} if action.startswith("dns-") else {}),
                "external_writers_not_locked": True, "dns_propagation_verified": False,
                "https_health_verified": False})

    def plan(self, service_id: str, operation: str, domain: str, parameters: dict) -> dict:
        domain_name(domain)
        action, values = validate_parameters(operation, parameters)
        _root()
        service, adapter = self.adapter(service_id)
        return self._preview(self._intent(service, adapter, operation, domain, values), adapter,
                             action, values, adapter._credentials())

    def apply(self, service_id: str, operation: str, domain: str, parameters: dict,
              expected_state: str, confirm_domain: str, operation_id: str) -> dict:
        canonical_uuid(operation_id)
        domain_name(domain)
        action, values = validate_parameters(operation, parameters)
        if (confirm_domain != domain or not isinstance(expected_state, str) or
                not DIGEST.fullmatch(expected_state)):
            raise CatalogError("exact domain confirmation and preview digest required")
        if action == "receipt-resolve" and values["original_operation_id"] == operation_id:
            raise CatalogError("local resolution needs a distinct UUID plus the original UUID")
        root = _root()
        service, adapter = self.adapter(service_id)
        intent = self._intent(service, adapter, operation, domain, values)
        identity = {**intent, "expected_state": expected_state}
        # Local identity and successful stage cache precede credentials, helper,
        # owned-domain existence and DNS-record preconditions, including delete.
        if (root / operation_id).exists() or (root / operation_id).is_symlink():
            prior = _receipt(operation_id)
            if prior["identity"] != identity:
                raise CatalogError("operation identity differs from original receipt")
            result = (prior.get("stages") or {}).get("dispatch", {})
            if _resolution(prior):
                return {"status": "resolved", "operation_id": operation_id,
                        "resolved_outcome": _resolution(prior), "original_dispatch": result,
                        "reused_completed_receipt": True, "live_state_verified_this_invocation": False,
                        "replayed": False}
            if prior["status"] == "success" and result.get("status") == "success":
                return {**result["result"], "operation_id": operation_id,
                        "reused_completed_receipt": True, "live_state_verified_this_invocation": False}
            return {"status": "needs-reconciliation", "operation_id": operation_id, "replayed": False,
                    "reason": "existing unfinished receipt; use operation-status, never replay"}
        credentials = adapter._credentials()
        binding = adapter._binding(credentials)
        # Account lock survives key rotation; key lock also covers duplicate service
        # aliases or absent account_id. Neither includes the action or service ID.
        resources = [json.dumps(["spaceship-domain-key", binding["api_key_sha256"], domain])]
        resources.append(json.dumps(["spaceship-domain-credential", binding["credential_path"], domain]))
        if service.account_id:
            resources.append(json.dumps(["spaceship-domain-account", service.account_id, domain]))
        try:
            with ExitStack() as stack:
                for resource in sorted(resources):
                    stack.enter_context(resource_lock(root, resource))
                paths = _marker_paths(root, resources)
                _check_markers(paths, values["original_operation_id"] if action == "receipt-resolve" else operation_id)
                journal = load_journal(adapter.foundation_root / "scripts/foundation_operation.py")
                # resume=False closes the same-UUID race between the cache check
                # above and acquiring the resource lock. Never ask stage to retry.
                with journal.Operation(root, operation_id, identity, resume=False) as job:
                    plan = self._preview(intent, adapter, action, values, credentials)
                    if plan["state_digest"] != expected_state:
                        raise CatalogError("preview state changed; no request dispatched")
                    job.stage("approved-state", lambda: plan)
                    if action == "receipt-resolve":
                        result = job.stage("dispatch", lambda: _resolve_original(
                            journal, root, operation_id, intent, adapter, values, credentials, plan))
                        job.complete()
                        return {**result, "operation_id": operation_id, "reused_completed_receipt": False,
                                "live_state_verified_this_invocation": True}
                    if (adapter._binding(adapter._credentials()) != binding or
                            adapter._snapshot(domain, action, credentials) != plan["current"]):
                        raise CatalogError("target or binding changed before dispatch")
                    # Durable fence precedes the durable running stage. A crash or
                    # failed journal write from here onward blocks every new ID.
                    for path in paths:
                        journal.atomic_json(path, {"operation_id": operation_id})

                    def dispatch():
                        if action != "dns-replace":
                            return _phase(job, adapter, domain, operation, values, credentials,
                                          plan["desired"], "dispatch", "acceptance")
                        delete_values = {"record": {k: v for k, v in values["record"].items() if k != "ttl"}}
                        deleted = job.stage("delete-old", lambda: _phase(
                            job, adapter, domain, "spaceship.dns-delete.v1", delete_values, credentials,
                            plan["intermediate"], "delete-old", "delete-acceptance"))
                        if deleted["status"] != "success":
                            return {**deleted, "registrar_state_matches": False,
                                    "intermediate_state_matches": deleted["registrar_state_matches"]}
                        # Never continue after drift, changed credentials, or an interrupted invocation.
                        if (adapter._binding(adapter._credentials()) != binding or
                                adapter._snapshot(domain, action, credentials) != plan["intermediate"]):
                            raise CatalogError("state changed after delete; add not dispatched")
                        return job.stage("add-replacement", lambda: _phase(
                            job, adapter, domain, "spaceship.dns-add.v1", {"record": values["replacement"]},
                            credentials, plan["desired"], "add-replacement", "add-acceptance"))

                    result = job.stage("dispatch", dispatch)
                    if result["status"] == "success":
                        job.complete()
                    else:
                        job.value["status"] = "needs-reconciliation"
                        job.save()
                    # Leave markers in place; the completed receipt is the sole
                    # authority for allowing another ID. No crash-sensitive unlink.
                    return {**result, "operation_id": operation_id, "reused_completed_receipt": False,
                            "live_state_verified_this_invocation": result["registrar_state_matches"]}
        except Exception:
            # Includes journal/lock/preflight failures; never leak raw helper errors
            # suggesting a new operation ID or claiming a request was not sent.
            return {"status": "needs-reconciliation", "operation_id": operation_id, "replayed": False,
                    "reason": "operation stopped; inspect original receipt and read-only observation"}

    def receipt(self, operation_id: str) -> dict:
        return _receipt(operation_id)


def _observe_bound(adapter, domain, receipt, credentials):
    identity = receipt["identity"]
    if identity.get("domain") != domain or any(identity.get(k) != v for k, v in adapter._paths().items()):
        raise CatalogError("operation receipt target or configuration mismatch")
    stages = receipt.get("stages") or {}
    approval = stages.get("approved-state", {})
    if approval.get("status") != "success" or not isinstance(approval.get("result"), dict):
        raise CatalogError("operation has no approved state to observe")
    plan = approval["result"]
    if adapter._binding(credentials) != plan.get("binding"):
        raise CatalogError("operation credential or helper binding changed")
    action, values = validate_parameters(identity["operation"], identity["parameters"])
    if action == "receipt-resolve":
        raise CatalogError("observe the original cloud operation UUID")
    acceptance_name, final_phase = "acceptance", True
    if action == "dns-replace":
        final_phase = "add-replacement" in stages
        acceptance_name = "add-acceptance" if final_phase else "delete-acceptance"
    acceptance = stages.get(acceptance_name, {})
    evidence = acceptance.get("result", {}) if acceptance.get("status") == "success" else {}
    async_id = evidence.get("async_id")
    async_status = None
    association = {
        "request_binding": "retained receipt acceptance ID" if async_id else "no retained async ID",
        "response_type": "unverified",
        "response_domain": "unverified",
        "response_association_verified": False,
        "coverage": "AsyncOperationDetails defines no domain fields; the schema defines no async type mapping "
                    "for these management operations. Receipt binding and state readback do not verify response-side association.",
    }
    if async_id is not None:
        if (not isinstance(async_id, str) or not ASYNC_ID.fullmatch(async_id) or
                evidence.get("accepted") is not True):
            raise CatalogError("invalid retained async acceptance")
        try:
            data = adapter._request("GET", f"/async-operations/{async_id}", credentials).data
            async_status = data.get("status") if data.get("status") in ("pending", "success", "failed") else "unavailable"
            kind = data.get("type")
            if not isinstance(kind, str) or not 1 <= len(kind) <= 255 or not re.fullmatch(r"\w+", kind):
                association["response_type"] = "invalid"
                async_status = "unavailable"
            elif kind == "domains_Create":
                # The sole documented type example is registration. None of our
                # supported operations (including either replace phase) registers a domain.
                association["response_type"] = "contradictory"
                async_status = "unavailable"
            # Unknown type strings and arbitrary details keys are not evidence
            # of a domain/action association; never infer a wire type from operationId.
        except Exception:
            async_status = "unavailable"
    try:
        observed = adapter._snapshot(domain, action, credentials)
        matches = _matches(observed, plan["desired"], action, values)
        availability = "complete"
    except Exception:
        observed, matches, availability = None, False, "unavailable"
    delete_proven = action != "dns-replace" or stages.get("delete-old", {}).get("result", {}).get("status") == "success"
    report = {"provider": "spaceship", "domain": domain, "operation_id": receipt["operation_id"],
              "status": availability, "receipt_status": receipt["status"], "provider_status": async_status,
              "provider_async_id": async_id, "acceptance_stage": acceptance_name,
              "async_association": association,
              "certifiable_success": bool(async_id and async_status == "success" and matches and final_phase and delete_proven
                                          and association["response_association_verified"]),
              "registrar_state_matches": matches, "observed_digest": digest(observed) if observed is not None else None,
              "mutation_invoked": False, "replayed": False, "receipt_changed": False,
              "resolved_outcome": _resolution(receipt),
              "dns_propagation_verified": False, "https_health_verified": False,
              "coverage": "read-only observation; does not change receipt or resource fence"}
    return plan, observed, report


def observe_operation(adapter: SpaceshipManagementAdapter, domain: str, operation_id: str) -> dict:
    domain_name(domain)
    return _observe_bound(adapter, domain, _receipt(operation_id), adapter._credentials())[2]


def _resolution_preview(intent, adapter, values, credentials):
    original = _receipt(values["original_operation_id"])
    if any(original["identity"].get(k) != intent[k] for k in
           ("service", "account_id", "domain", "credential_path", "foundation_path")):
        raise CatalogError("resolution service, permission or original target mismatch")
    if _finished(original):
        raise CatalogError("original receipt is already complete or resolved")
    plan, observed, report = _observe_bound(adapter, intent["domain"], original, credentials)
    if observed is None:
        raise CatalogError("complete readback required for local resolution")
    if values["mode"] == "certify-success":
        if not report["certifiable_success"]:
            raise CatalogError("certification requires verified response association, accepted async success and exact desired readback")
    elif report["provider_status"] in {"pending", "unavailable"}:
        raise CatalogError("known asynchronous operation is still pending or unavailable; cannot adopt")
    basis = {**intent, "binding": plan["binding"], "current": observed,
             "desired": plan["desired"], "observation": report,
             "original_receipt_digest": digest(original),
             "outcome": "certified-success" if values["mode"] == "certify-success" else "adopted-observed-state"}
    return _bounded_preview({**basis, "state_digest": digest(basis), "status": "planned", "mutation_invoked": False,
            "local_receipt_write": True, "external_writers_not_locked": True,
            "adoption_notice": "Adoption accepts the exact observed state; it does not attribute success to the original mutation or resume any phase.",
            "dns_propagation_verified": False, "https_health_verified": False})


def _resolve_original(journal, root, resolution_id, intent, adapter, values, credentials, approved):
    # GET only, under the same domain locks as mutations. Recheck after approval.
    if (adapter._binding(adapter._credentials()) != approved["binding"] or
            _resolution_preview(intent, adapter, values, credentials)["state_digest"] != approved["state_digest"]):
        raise CatalogError("resolution state changed before local write")
    original = _receipt(values["original_operation_id"])
    if digest(original) != approved["original_receipt_digest"]:
        raise CatalogError("original receipt changed")
    with journal.Operation(root, values["original_operation_id"], original["identity"], resume=True) as job:
        if digest(_receipt(values["original_operation_id"])) != approved["original_receipt_digest"]:
            raise CatalogError("original receipt changed while locking")
        evidence = {"outcome": approved["outcome"], "mode": values["mode"],
                    "resolution_operation_id": resolution_id, "original_receipt_status": original["status"],
                    "approved_state_digest": approved["state_digest"],
                    "original_receipt_digest": approved["original_receipt_digest"],
                    "observed_digest": digest(approved["current"]),
                    "provider_async_id": approved["observation"]["provider_async_id"],
                    "provider_status": approved["observation"]["provider_status"],
                    "mutation_invoked": False, "replayed": False}
        # Append once, preserving all original dispatch/acceptance/failed/running stages.
        job.stage("receipt-resolution", lambda: evidence)
        job.value["status"] = "resolved"
        job.save()
    return {"status": "success", "resolved_outcome": evidence, "mutation_invoked": False,
            "registrar_state_matches": approved["observation"]["registrar_state_matches"],
            "replayed": False, "dns_propagation_verified": False, "https_health_verified": False}
