"""Finite Cloudflare changes with durable approval, one dispatch and readback."""
from __future__ import annotations

import json
import os
import stat
from uuid import UUID

from .catalog import CatalogError
from .cloudflare_domains import (CloudflareDomainsAdapter, DIGEST, SETTINGS, TYPES,
                                 digest, dns_body, domain_name, identifier, record_view)
from .operation_runtime import load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition

PREFIX = "cloudflare."
ACTIONS = ("dns-create", "dns-update", "dns-delete", "cache-purge-urls", "cache-purge-all", "setting-set")
OPERATIONS = frozenset(PREFIX + action + ".v1" for action in ACTIONS)
DNS_ARGS = ("record_type", "name", "content", "ttl", "proxied", "priority", "srv_port",
            "srv_weight", "srv_target", "caa_flags", "caa_tag", "caa_value")


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise CatalogError("canonical operation UUID required") from None
    return value


def resource(account_id, domain):
    # No service alias, zone ID or action: all actions for one account/domain share a lock.
    return f"cloudflare-domain:{account_id}:{domain}"


def _parameters(operation, domain, values):
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise CatalogError("unsupported Cloudflare domain operation")
    action = operation[len(PREFIX):-3]
    p = {key: values[key] for key in (*DNS_ARGS, "record_id", "urls", "setting_id", "value")}
    allowed = set()
    if action in {"dns-create", "dns-update"}:
        allowed.update(DNS_ARGS)
    if action in {"dns-update", "dns-delete"}:
        allowed.add("record_id")
    if action == "cache-purge-urls":
        allowed.add("urls")
    if action == "setting-set":
        allowed.update(("setting_id", "value"))
    if any(value is not None and key not in allowed for key, value in p.items()):
        raise CatalogError("parameters do not belong to the exact action")
    body = None
    if action in {"dns-update", "dns-delete"}:
        identifier(p["record_id"])
    if action in {"dns-create", "dns-update"}:
        if not isinstance(p["record_type"], str):
            raise CatalogError("typed DNS record required")
        body = dns_body(domain, **{key: p[key] for key in DNS_ARGS})
    if action == "setting-set":
        setting, value = p["setting_id"], p["value"]
        if (not isinstance(setting, str) or setting not in SETTINGS or not isinstance(value, str)
                or value not in SETTINGS[setting]):
            raise CatalogError("unsupported finite setting/value")
        body = {"value": value}
    if action == "cache-purge-urls":
        urls = p["urls"]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 30 or any(not isinstance(v, str) for v in urls):
            raise CatalogError("purge requires 1..30 exact URLs")
        if len(set(urls)) != len(urls):
            raise CatalogError("duplicate purge URLs")
        # Query strings, credentials and opaque escaped paths may carry signed
        # tokens. Restrict this finite surface to simple public asset paths.
        import re
        import urllib.parse
        for url in urls:
            try:
                parsed = urllib.parse.urlsplit(url)
                valid = (len(url) <= 2048 and parsed.scheme == "https" and parsed.netloc == domain
                         and parsed.hostname == domain and not parsed.query and not parsed.fragment
                         and not parsed.username and not parsed.password and parsed.port is None
                         and re.fullmatch(r"/[A-Za-z0-9/_.,~-]*", parsed.path)
                         and all(segment not in {".", ".."} for segment in parsed.path.split("/"))
                         and url == "https://" + domain + parsed.path)
            except ValueError:
                valid = False
            if not valid:
                raise CatalogError("purge URL must be exact HTTPS zone host and simple public path without secrets")
        body = {"files": urls.copy()}
    if action == "cache-purge-all":
        body = {"purge_everything": True}
    return {"action": action, "record_id": p["record_id"], "setting_id": p["setting_id"], "body": body}


def _same_intended(row, body):
    for key, value in body.items():
        if key == "data":
            if not isinstance(row.get(key), dict) or any(row[key].get(k) != v for k, v in value.items()):
                return False
        elif row.get(key) != value:
            return False
    return True


def _unchanged_metadata(old, new, body):
    ignored = set(body) | {"modified_on"}
    if "data" in body:
        ignored.add("content")  # Provider-rendered SRV/CAA text derives from typed data.
        if ({k: v for k, v in old["data"].items() if k not in body["data"]}
                != {k: v for k, v in new["data"].items() if k not in body["data"]}):
            return False
    return {k: v for k, v in old.items() if k not in ignored} == {k: v for k, v in new.items() if k not in ignored}


def _snapshot(adapter, zone_id, domain, params, *, precondition=True):
    zone = adapter._owned(zone_id, domain)
    if precondition and (zone["type"] != "full" or zone["status"] not in {"active", "pending"}):
        raise CatalogError("mutation requires an active or pending full zone")
    result = {"zone": zone}
    action = params["action"]
    if action.startswith("dns-"):
        listed = adapter._records(zone_id, domain)
        if listed["status"] != "complete":
            raise CatalogError("complete DNS inventory required")
        rows = {row["id"]: row for row in listed["items"]}
        record_id = params["record_id"]
        if record_id is not None and record_id in rows:
            detail = adapter._record(zone_id, domain, record_id)
            if detail != rows[record_id]:
                raise CatalogError("DNS list/detail changed or disagreed")
        if precondition and record_id is not None:
            if record_id not in rows:
                raise CatalogError("exact DNS record does not exist")
            if rows[record_id]["type"] not in TYPES:
                raise CatalogError("existing DNS type is outside supported mutations")
        body = params["body"]
        if precondition and body is not None:
            if record_id and rows[record_id]["type"] != body["type"]:
                raise CatalogError("record type conversion is not supported")
            if record_id and _same_intended(rows[record_id], body):
                raise CatalogError("DNS record already has intended values")
            for other_id, row in rows.items():
                if other_id == record_id or row["name"] != body["name"]:
                    continue
                if "CNAME" in {row["type"], body["type"]} or ("NS" in {row["type"], body["type"]} and row["type"] != body["type"]):
                    raise CatalogError("CNAME/NS exclusivity conflict")
                # TTL/proxy differences do not turn an existing RR into a new one.
                rr = {k: v for k, v in body.items() if k not in {"ttl", "proxied"}}
                if _same_intended(row, rr):
                    raise CatalogError("intended DNS record already exists; never overwrite")
        result["records"] = rows
    elif action == "setting-set":
        setting = adapter._setting(zone_id, params["setting_id"])
        if precondition and (not setting["editable"] or setting["value"] == params["body"]["value"]):
            raise CatalogError("zone setting is not editable or already has intended value")
        result["setting"] = setting
    if adapter._owned(zone_id, domain) != zone:
        raise CatalogError("zone identity/state changed during observation")
    return result


def _safe_snapshot(snapshot):
    return {**{k: v for k, v in snapshot.items() if k != "records"},
            **({"records": [record_view(row) for _, row in sorted(snapshot["records"].items())]}
               if "records" in snapshot else {})}


def _postcondition(before, after, params, result_id):
    if before["zone"] != after["zone"]:
        return False
    action, body, source_id = params["action"], params["body"], params["record_id"]
    if action == "setting-set":
        return after["setting"]["value"] == body["value"] and after["setting"]["editable"]
    if not action.startswith("dns-"):
        return False
    old, new = before["records"], after["records"]
    unaffected_old = {k: v for k, v in old.items() if k != source_id}
    if action == "dns-delete":
        return source_id not in new and new == unaffected_old
    target_id = result_id if action == "dns-create" else source_id
    if target_id not in new or not _same_intended(new[target_id], body):
        return False
    if action == "dns-create" and target_id in old:
        return False
    if {k: v for k, v in new.items() if k != target_id} != unaffected_old:
        return False
    return action == "dns-create" or _unchanged_metadata(old[source_id], new[target_id], body)


def _needs(operation_id, *, invoked=None):
    return {"status": "needs-reconciliation", "operation_id": operation_id,
            "mutation_invoked": invoked, "replayed": False,
            "reason": ("this attempt did not dispatch; inspect its receipt and any earlier unresolved operation before a fresh preview"
                       if invoked is False else
                       "inspect receipt and read-only reconciliation; do not resubmit or replace operation ID")}


def _existing(root, operation_id):
    path = root / operation_id
    return path.exists() or path.is_symlink()


def _fence(root, key, operation_id, journal):
    """Persistent same-resource unknown fence, written by the neutral atomic writer.

    It points at the last dispatch attempt. A fresh UUID cannot bypass ambiguity.
    Only a durable completed dispatch receipt permits the next operation.
    """
    path = root / "resource-locks" / (digest(key) + ".cloudflare-pending.json")
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 1024):
                raise CatalogError("unsafe Cloudflare operation fence")
            with os.fdopen(fd, closefd=False) as stream:
                previous = json.load(stream)
        finally:
            os.close(fd)
        prior_id = canonical_uuid(previous.get("operation_id"))
        if previous.get("resource") != key:
            raise CatalogError("Cloudflare operation fence identity mismatch")
        if prior_id != operation_id:
            receipt = read_receipt(prior_id, OPERATIONS)
            result = receipt.get("stages", {}).get("dispatch", {}).get("result", {})
            if (receipt.get("status") != "success" or result.get("status") not in {"success", "accepted"}
                    or receipt["identity"].get("resource") != key):
                raise CatalogError("earlier operation requires reconciliation; new ID cannot bypass it")
    journal.atomic_json(path, {"resource": key, "operation_id": operation_id})


class CloudflareDomainOperations:
    provider = "cloudflare"
    definitions = tuple(OperationDefinition(op, "plan_service_operation",
        "apply_service_operation", "get_service_operation") for op in sorted(OPERATIONS))

    def __init__(self, catalog):
        self.catalog = catalog

    def adapter(self, service_id):
        service = self.catalog.get(service_id)
        if (service.provider != self.provider or service.adapter != "cloudflare-domains"
                or not service.enabled or service.kind != "python-adapter"):
            raise CatalogError("service does not support Cloudflare domain operations")
        adapter = self.catalog.adapter(service)
        if not isinstance(adapter, CloudflareDomainsAdapter):
            raise CatalogError("unexpected Cloudflare domains adapter")
        return adapter

    @staticmethod
    def _identity(adapter, service_id, operation, zone_id, domain, params, expected_state):
        return {"operation": operation, "service": service_id, "account_id": adapter.account_id,
                "zone_id": zone_id, "domain": domain, "resource": resource(adapter.account_id, domain),
                "parameters_digest": digest(params), "expected_state": expected_state,
                "locator": adapter._locator()}

    @staticmethod
    def _preview(adapter, service_id, operation, zone_id, domain, params, bindings):
        snapshot = _snapshot(adapter, zone_id, domain, params)
        safe = _safe_snapshot(snapshot)
        basis = {"operation": operation, "service": service_id, "account_id": adapter.account_id,
                 "zone_id": zone_id, "domain": domain, "parameters_digest": digest(params),
                 "binding": bindings, "current": safe}
        desired = params["body"]
        # Hash record data, URLs and free text. Preserve only typed operational metadata.
        target = {"record_id": params["record_id"], "setting_id": params["setting_id"],
                  "action": params["action"], "desired_digest": digest(desired)}
        if params["action"].startswith("dns-") and desired:
            target.update({k: desired[k] for k in ("type", "name", "ttl", "proxied")})
        if params["action"] == "setting-set":
            target["value"] = desired["value"]
        if params["action"] == "cache-purge-urls":
            target["url_count"] = len(desired["files"])
        preview = {"status": "planned", "basis": basis, "target": target,
                   "state_digest": digest(basis), "mutation_invoked": False,
                   "effects": {"all_zone_cached_content": params["action"] == "cache-purge-all",
                               "may_increase_origin_traffic": params["action"].startswith("cache-"),
                               "may_interrupt_resolution_or_access": not params["action"].startswith("cache-"),
                               "external_writers_not_locked": True,
                               "dns_propagation_verified": False, "https_health_verified": False}}
        return preview, snapshot

    def plan(self, service_id: str, operation: str, zone_id: str, domain: str, *,
             record_id: str | None = None, record_type: str | None = None, name: str | None = None,
             content: str | None = None, ttl: int | None = None, proxied: bool | None = None,
             priority: int | None = None, srv_port: int | None = None, srv_weight: int | None = None,
             srv_target: str | None = None, caa_flags: int | None = None, caa_tag: str | None = None,
             caa_value: str | None = None, urls: list[str] | None = None,
             setting_id: str | None = None, value: str | None = None):
        identifier(zone_id)
        domain_name(domain)
        params = _parameters(operation, domain, locals())
        adapter, bindings = self.adapter(service_id)._bound()
        return self._preview(adapter, service_id, operation, zone_id, domain, params, bindings)[0]

    def apply(self, service_id: str, operation: str, zone_id: str, domain: str,
              expected_state: str, confirm_account_id: str, confirm_zone_id: str,
              confirm_domain: str, operation_id: str, *,
              record_id: str | None = None, record_type: str | None = None, name: str | None = None,
              content: str | None = None, ttl: int | None = None, proxied: bool | None = None,
              priority: int | None = None, srv_port: int | None = None, srv_weight: int | None = None,
              srv_target: str | None = None, caa_flags: int | None = None, caa_tag: str | None = None,
              caa_value: str | None = None, urls: list[str] | None = None,
              setting_id: str | None = None, value: str | None = None):
        canonical_uuid(operation_id)
        identifier(zone_id)
        domain_name(domain)
        params = _parameters(operation, domain, locals())
        adapter = self.adapter(service_id)
        if (confirm_account_id != adapter.account_id or confirm_zone_id != zone_id or confirm_domain != domain
                or not isinstance(expected_state, str) or not DIGEST.fullmatch(expected_state)):
            raise CatalogError("exact account, zone, domain and preview digest confirmation required")
        identity = self._identity(adapter, service_id, operation, zone_id, domain, params, expected_state)
        if not os.environ.get("STACKORION_OPERATION_ROOT"):
            raise CatalogError("explicit private STACKORION_OPERATION_ROOT required")
        root = state_root()
        with resource_lock(root, identity["resource"]):
            # No token/helper read or remote preconditions before durable cache lookup.
            if _existing(root, operation_id):
                receipt = self.receipt(operation_id)
                if receipt["identity"] != identity:
                    raise CatalogError("operation identity mismatch; inspect existing receipt")
                stage = receipt.get("stages", {}).get("dispatch", {})
                result = stage.get("result", {})
                if receipt["status"] == "success" and stage.get("status") == "success" and result.get("status") in {"success", "accepted"}:
                    return {**result, "operation_id": operation_id, "reused_completed_receipt": True,
                            "live_state_verified_this_invocation": False}
                return _needs(operation_id)
            invoked = False
            try:
                bound, bindings = adapter._bound()
                journal = load_journal(adapter.foundation_root / "scripts/foundation_operation.py")
                with journal.Operation(root, operation_id, identity, resume=False) as job:
                    captured = {}

                    def approve():
                        preview, before = self._preview(bound, service_id, operation, zone_id, domain, params, bindings)
                        if preview["state_digest"] != expected_state:
                            raise CatalogError("preview drift; no dispatch")
                        captured["before"] = before
                        return preview

                    job.stage("approved-state", approve)

                    def dispatch():
                        nonlocal invoked
                        # Recheck all preconditions and binding after approval persistence.
                        fresh, fresh_bindings = adapter._bound()
                        check, before = self._preview(fresh, service_id, operation, zone_id, domain, params, fresh_bindings)
                        if check["state_digest"] != expected_state or before != captured["before"]:
                            raise CatalogError("state or binding changed before dispatch")
                        action = params["action"]
                        path = f"/zones/{zone_id}"
                        if action.startswith("dns-"):
                            path += "/dns_records" + ("/" + params["record_id"] if params["record_id"] else "")
                            method = {"dns-create": "POST", "dns-update": "PATCH", "dns-delete": "DELETE"}[action]
                        elif action == "setting-set":
                            path += "/settings/" + params["setting_id"]
                            method = "PATCH"
                        else:
                            path += "/purge_cache"
                            method = "POST"
                        accepted, result_id = False, None
                        # Persist the fence only after all pre-dispatch checks.
                        # A crash from this point is ambiguous and must block a
                        # new intent; a failed preflight has made no request.
                        _fence(root, identity["resource"], operation_id, journal)
                        invoked = True
                        try:
                            response = fresh._request(method, path, body=params["body"])
                            raw = response.get("result")
                            if isinstance(raw, dict):
                                if action.startswith("dns-"):
                                    result_id = identifier(raw.get("id"))
                                    accepted = params["record_id"] is None or result_id == params["record_id"]
                                elif action == "setting-set":
                                    accepted = raw.get("id") == params["setting_id"] and raw.get("value") == params["body"]["value"]
                                else:
                                    result_id = identifier(raw.get("id"))
                                    accepted = True
                        except Exception:
                            pass
                        after, verified = None, False
                        try:
                            after = _snapshot(fresh, zone_id, domain, params, precondition=False)
                            if action == "dns-create" and result_id is None:
                                candidates = [rid for rid, row in after["records"].items()
                                              if rid not in before["records"] and _same_intended(row, params["body"])]
                                if len(candidates) == 1:
                                    result_id = candidates[0]
                            verified = _postcondition(before, after, params, result_id)
                            if verified and action == "dns-create":
                                verified = fresh._record(zone_id, domain, result_id) == after["records"][result_id]
                        except Exception:
                            verified = False
                        status = ("accepted" if action.startswith("cache-") and accepted else
                                  "success" if accepted and verified else "needs-reconciliation")
                        return {"status": status, "mutation_invoked": True, "replayed": False,
                                "dispatch_outcome": "accepted" if accepted else "unknown",
                                "observed_postcondition": verified, "verified": accepted and verified,
                                "result_id": result_id, "readback": _safe_snapshot(after) if after else None,
                                "dns_propagation_verified": False, "https_health_verified": False,
                                "cache_removal_verified": False}

                    result = job.stage("dispatch", dispatch)
                    if result["status"] in {"success", "accepted"}:
                        job.complete()
                    else:
                        job.value["status"] = "needs-reconciliation"
                        job.save()
                    return {**result, "operation_id": operation_id, "reused_completed_receipt": False,
                            "live_state_verified_this_invocation": result["verified"]}
            except Exception:
                # Journal errors may contain paths or provider details. No replay,
                # even if the write succeeded and only receipt persistence failed.
                return _needs(operation_id, invoked=invoked)

    def receipt(self, operation_id: str):
        canonical_uuid(operation_id)
        if not os.environ.get("STACKORION_OPERATION_ROOT"):
            raise CatalogError("explicit private STACKORION_OPERATION_ROOT required")
        return read_receipt(operation_id, OPERATIONS)


def observe_operation(adapter, operation_id):
    """Read-only evidence, never reclassifies a receipt or clears an unknown fence."""
    canonical_uuid(operation_id)
    if not os.environ.get("STACKORION_OPERATION_ROOT"):
        raise CatalogError("explicit private STACKORION_OPERATION_ROOT required")
    receipt = read_receipt(operation_id, OPERATIONS)
    identity = receipt["identity"]
    if identity.get("account_id") != adapter.account_id or identity.get("locator") != adapter._locator():
        raise CatalogError("operation belongs to another Cloudflare context")
    stage = receipt.get("stages", {}).get("approved-state", {})
    preview = stage.get("result")
    if stage.get("status") != "success" or not isinstance(preview, dict):
        return {"status": "unavailable", "reason": "no durable approved state", "replayed": False}
    domain, zone_id, target = identity["domain"], identity["zone_id"], preview["target"]
    params = {"action": target["action"], "record_id": target["record_id"],
              "setting_id": target["setting_id"], "body": None}
    try:
        after = _safe_snapshot(_snapshot(adapter, zone_id, domain, params, precondition=False))
    except Exception:
        return {"status": "unavailable", "operation_id": operation_id, "replayed": False}
    return {"status": "complete", "operation_id": operation_id, "receipt_status": receipt["status"],
            "approved_state": preview["basis"]["current"], "observed_state": after,
            "replayed": False, "receipt_changed": False, "attribution_verified": False,
            "dns_propagation_verified": False, "https_health_verified": False}
