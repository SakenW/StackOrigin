"""StackOrigin service management through registered queries and scoped operations."""

from __future__ import annotations

from typing import Any
import os

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .catalog import Catalog, CatalogError, configured_path
from .external import probe_external
from .management import list_management_capabilities
from .operations import OperationRegistry
from .queries import QueryRegistry
from .qiniu_tools import register_qiniu_tools
from .domain_tools import register_domain_tools
from .dashboard_mcp import register_dashboard_tools


def build_server(catalog: Catalog) -> MCPServer:
    mcp = MCPServer("StackOrion", title="StackOrigin（源栈）",
                    instructions="Manage registered services through scoped queries and scoped operations. Review exact targets and parameters before mutation. Unknown dispatch outcomes must be reconciled, never replayed automatically.")

    def fresh_catalog():
        # Stable gateways observe newly configured plugins without rebuilding
        # the MCP tool list. Existing imported module code still needs one reload.
        return Catalog.load(configured_path()) if 'STACKORION_CONFIG' in os.environ else catalog

    def fresh_operations():
        return OperationRegistry(fresh_catalog())

    register_domain_tools(mcp,fresh_catalog)

    register_dashboard_tools(mcp)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def list_service_capabilities(service_id: str) -> dict[str, Any]:
        """List registered queries and operations. Use an operation's own parameter_contract when provided."""
        try:
            current=fresh_catalog()
            queries = QueryRegistry(current).describe(service_id)
            operations = OperationRegistry(current).describe(service_id)
            return {**queries, **operations, "operation_status": operations["status"],
                    "status": "available" if queries["queries"] else operations["status"]}
        except CatalogError as exc:return {"status":"unavailable","reason":str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def query_service(service_id: str, query_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Run one explicitly registered read-only query. List capabilities first. No arbitrary methods, API paths or shell."""
        try:return QueryRegistry(fresh_catalog()).query(service_id,query_id,parameters)
        except (CatalogError,TypeError,ValueError) as exc:return {"status":"unavailable","reason":str(exc) if isinstance(exc,CatalogError) else "invalid registered query parameters"}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_service_operation(service_id: str, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Preview one registered exact operation using current catalog. No mutation is dispatched."""
        try:return OperationRegistry(fresh_catalog()).plan(service_id,operation,**parameters)
        except (CatalogError,TypeError) as exc:return {"status":"unavailable","reason":str(exc) if isinstance(exc,CatalogError) else "parameters do not match the operation signature"}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False,destructive_hint=True,idempotent_hint=False))
    def apply_service_operation(service_id: str, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Execute one registered exact operation with its preview digest, target confirmations and operation ID. Requires specific user authorization; unknown outcomes must not be resubmitted."""
        try:return OperationRegistry(fresh_catalog()).apply(service_id,operation,**parameters)
        except (CatalogError,TypeError) as exc:return {"status":"unavailable","reason":str(exc) if isinstance(exc,CatalogError) else "parameters do not match the operation signature"}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_service_operation(service_id: str, operation_id: str) -> dict[str, Any]:
        """Read a durable receipt belonging to this service, even after it is disabled; never replay requests."""
        try:
            return fresh_operations().service_receipt(service_id, operation_id)
        except CatalogError as exc:return {"status":"unavailable","reason":str(exc)}

    def adapter_query(service_id, method, **parameters):
        try:
            current=fresh_catalog()
            adapter = current.adapter(current.get(service_id))
            handler = getattr(adapter, method, None)
            if not callable(handler):
                raise CatalogError("service does not support object queries")
            return handler(**parameters)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    def object_operation(service_id, operation, phase, **parameters):
        try:
            current=fresh_operations()
            handler = current.plan if phase == "plan" else current.apply
            return handler(service_id, operation, **parameters)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}
        except OSError:
            return {"status": "unavailable", "reason": "local transfer file is unavailable"}

    def plugin_receipt(adapter_name, operation_id):
        try:return fresh_operations().receipt(adapter_name,operation_id)
        except CatalogError as exc:return {"status":"unavailable","reason":str(exc)}

    def plugin_observation(service_id, method, **parameters):
        try:return fresh_operations().observe(service_id,method,**parameters)
        except CatalogError as exc:return {"status":"unavailable","reason":str(exc)}

    register_qiniu_tools(mcp,adapter_query,object_operation,plugin_receipt,plugin_observation)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def inspect_qiniu_services(service_id: str, target_alias: str) -> dict[str, Any]:
        """Read fixed systemd service and latest 50 container states on an exact host. No environment, commands or raw logs."""
        return adapter_query(service_id, "services", alias=target_alias)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def inspect_qiniu_backups(service_id: str, target_alias: str) -> dict[str, Any]:
        """Read the fixed local backup marker and nonempty artifact metadata. Does not run backup, retention or restore."""
        return adapter_query(service_id, "backups", alias=target_alias)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def list_kodo_objects(service_id: str, target_alias: str, prefix: str = "", marker: str = "", limit: int = 100) -> dict[str, Any]:
        """Read one object metadata page in a configured owned bucket; continue with next_marker. No object contents."""
        return adapter_query(service_id, "list_objects", alias=target_alias, prefix=prefix, marker=marker, limit=limit)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def stat_kodo_object(service_id: str, target_alias: str, key: str) -> dict[str, Any]:
        """Read metadata of one exact key; does not download object contents."""
        return adapter_query(service_id, "stat_object", alias=target_alias, key=key)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_upload(service_id: str, target_alias: str, key: str, source_path: str,
                         mime_type: str = "application/octet-stream") -> dict[str, Any]:
        """Preview a new object upload from approved local staging roots; reject existing keys; no upload."""
        return object_operation(service_id, "kodo.put.v1", "plan", alias=target_alias, key=key,
                                source_path=source_path, mime_type=mime_type)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
    def apply_kodo_upload(service_id: str, target_alias: str, key: str, source_path: str,
                          expected_state: str, confirm_bucket: str, operation_id: str,
                          mime_type: str = "application/octet-stream") -> dict[str, Any]:
        """Upload only after explicit client approval of exact local file and bucket/key. No overwrite; read back hash/size. Unknown outcomes are never replayed."""
        return object_operation(service_id, "kodo.put.v1", "apply", alias=target_alias, key=key, source_path=source_path,
                                expected_state=expected_state, confirm_bucket=confirm_bucket, operation_id=operation_id,
                                mime_type=mime_type)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_download(service_id: str, target_alias: str, key: str, filename: str) -> dict[str, Any]:
        """Preview exact object metadata and a new local filename inside the configured private download directory; no download."""
        return object_operation(service_id, "kodo.download.v1", "plan", alias=target_alias, key=key, filename=filename)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
    def apply_kodo_download(service_id: str, target_alias: str, key: str, filename: str,
                            expected_state: str, confirm_bucket: str, operation_id: str) -> dict[str, Any]:
        """Download approved exact object to configured directory after client approval; consumes egress and writes local data. No overwrite; verify content hash/size."""
        return object_operation(service_id, "kodo.download.v1", "apply", alias=target_alias, key=key, filename=filename,
                                expected_state=expected_state, confirm_bucket=confirm_bucket, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_copy(service_id: str, target_alias: str, key: str, destination_key: str) -> dict[str, Any]:
        """Preview copying one exact object within the same owned bucket. Destination must not exist; no copy."""
        return object_operation(service_id, 'kodo.copy-object.v1', 'plan', alias=target_alias, key=key, destination_key=destination_key)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
    def apply_kodo_copy(service_id: str, target_alias: str, key: str, destination_key: str, expected_state: str,
                         confirm_bucket: str, confirm_key: str, confirm_destination_key: str, operation_id: str) -> dict[str, Any]:
        """After approval copy one exact object without overwrite. Confirm bucket/source/destination and digest; unknown outcomes are not replayed."""
        return object_operation(service_id, 'kodo.copy-object.v1', 'apply', alias=target_alias, key=key, destination_key=destination_key,
                                expected_state=expected_state, confirm_bucket=confirm_bucket, confirm_key=confirm_key,
                                confirm_destination_key=confirm_destination_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_rename(service_id: str, target_alias: str, key: str, destination_key: str) -> dict[str, Any]:
        """Preview exact same-bucket rename; source will be removed, destination must not exist. No rename."""
        return object_operation(service_id, 'kodo.rename-object.v1', 'plan', alias=target_alias, key=key, destination_key=destination_key)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_kodo_rename(service_id: str, target_alias: str, key: str, destination_key: str, expected_state: str,
                           confirm_bucket: str, confirm_key: str, confirm_destination_key: str, operation_id: str) -> dict[str, Any]:
        """After approval rename one exact object without overwrite. Removes source; rechecks metadata and reads back both keys; no unknown-result replay."""
        return object_operation(service_id, 'kodo.rename-object.v1', 'apply', alias=target_alias, key=key, destination_key=destination_key,
                                expected_state=expected_state, confirm_bucket=confirm_bucket, confirm_key=confirm_key,
                                confirm_destination_key=confirm_destination_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_object_deletion(service_id: str, target_alias: str, key: str) -> dict[str, Any]:
        """Preview irreversible deletion of ONE exact existing object. No prefix, wildcard or batch expansion; no deletion."""
        return object_operation(service_id, "kodo.delete-object.v1", "plan", alias=target_alias, key=key)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_kodo_object_deletion(service_id: str, target_alias: str, key: str, expected_state: str,
                                    confirm_bucket: str, confirm_key: str, operation_id: str) -> dict[str, Any]:
        """After explicit approval delete ONE exact key, with bucket/key confirmation and approved plan digest. Recheck metadata; unknown outcomes must not be replayed. Provider offers no atomic version condition."""
        return object_operation(service_id, "kodo.delete-object.v1", "apply", alias=target_alias, key=key,
                                expected_state=expected_state, confirm_bucket=confirm_bucket,
                                confirm_key=confirm_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_kodo_operation(operation_id: str) -> dict[str, Any]:
        """Read a Kodo transfer or deletion receipt; never resume or replay the operation."""
        try:
            return fresh_operations().receipt("qiniu-kodo-objects", operation_id)
        except (CatalogError, OSError, ValueError):
            return {"status": "unavailable", "reason": "Kodo operation receipt is unavailable"}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True))
    def plan_qiniu_port_access(service_id: str, target_alias: str, port: int, cidr: str,
                               description: str = "operator-approved TCP access") -> dict[str, Any]:
        """Preview adding TCP ingress rules to an exact QVM; no cloud mutation."""
        try:
            return fresh_operations().plan(service_id, "qvm.allow-port", alias=target_alias,
                                   port=port, cidr=cidr, description=description)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
    def apply_qiniu_port_access(service_id: str, target_alias: str, port: int, cidr: str,
                                expected_state: str, confirm_instance_id: str, operation_id: str,
                                description: str = "operator-approved TCP access") -> dict[str, Any]:
        """Apply the reviewed port plan after explicit client approval; changes cloud firewall access. Exact instance, state and operation ID required."""
        try:
            return fresh_operations().apply(service_id, "qvm.allow-port", alias=target_alias,
                                    port=port, cidr=cidr, description=description,
                                    expected_state=expected_state, confirm_instance_id=confirm_instance_id,
                                    operation_id=operation_id)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True))
    def plan_qiniu_rule_deletion(service_id: str, target_alias: str, security_group_id: str,
                                 rule_id: str) -> dict[str, Any]:
        """Preview deletion of one exact IPv4 ingress rule; affects ALL instances attached to the group."""
        try:
            return fresh_operations().plan(service_id, "qvm.delete-ingress-rule", alias=target_alias,
                                   security_group_id=security_group_id, rule_id=rule_id)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
    def apply_qiniu_rule_deletion(service_id: str, target_alias: str, security_group_id: str,
                                  rule_id: str, expected_state: str, confirm_instance_id: str,
                                  operation_id: str) -> dict[str, Any]:
        """Delete the reviewed exact rule only after explicit client approval; group-wide impact, state and instance confirmation required."""
        try:
            return fresh_operations().apply(service_id, "qvm.delete-ingress-rule", alias=target_alias,
                                    security_group_id=security_group_id, rule_id=rule_id,
                                    expected_state=expected_state, confirm_instance_id=confirm_instance_id,
                                    operation_id=operation_id)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True))
    def get_qiniu_operation(operation_id: str) -> dict[str, Any]:
        """Read a local operation receipt without re-running its cloud changes."""
        try:
            return fresh_operations().receipt("qiniu-qvm", operation_id)
        except (CatalogError, OSError, ValueError) as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True))
    def list_service_operations(service_id: str) -> dict[str, Any]:
        """List installed operation routes. Use an operation's own parameter_contract when provided."""
        try:
            return fresh_operations().describe(service_id)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True)
    def list_server_management_capabilities() -> dict[str, Any]:
        """Show each server capability's actual MCP connection and execution status."""
        return list_management_capabilities()

    @mcp.tool(structured_output=True)
    def list_services() -> list[dict[str, Any]]:
        """List configured service aliases, connector kinds, and enabled state."""
        return fresh_catalog().list()

    @mcp.tool(structured_output=True)
    def list_accounts() -> list[dict[str, Any]]:
        """List declared account profiles and their enabled service bindings; no secrets."""
        return fresh_catalog().accounts()

    @mcp.tool(structured_output=True)
    def list_resources(service_id: str = "") -> dict[str, Any]:
        """List declared resources with account, region, and verification status."""
        try:
            return {"status": "ok", "resources": fresh_catalog().resources(service_id or None)}
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True)
    def list_relations(resource_id: str) -> dict[str, Any]:
        """List declared relationships involving one resource."""
        try:
            return {"status": "ok", "relations": fresh_catalog().relations(resource_id)}
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True)
    def list_targets(service_id: str) -> dict[str, Any]:
        """List allowed target aliases for one enabled local service."""
        try:
            current=fresh_catalog()
            service = current.get(service_id)
            adapter = current.adapter(service)
            handler=getattr(adapter,'list_targets',None)
            if not callable(handler):
                return {"status":"ok","service":service_id,"targets":[],"coverage":"no fixed aliases; use registered domain discovery queries"}
            return {"status": "ok", "service": service_id, "targets": handler()}
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True)
    def discover_service(service_id: str) -> dict[str, Any]:
        """Discover resources through an enabled adapter that supports safe enumeration."""
        try:
            current=fresh_catalog()
            adapter = current.adapter(current.get(service_id))
            if not hasattr(adapter, "discover"):
                return {"status": "unavailable", "reason": "service does not support discovery"}
            return adapter.discover()
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True)
    def inspect_target(service_id: str, target_alias: str) -> dict[str, Any]:
        """Run a read-only audit for one explicitly configured target."""
        try:
            current=fresh_catalog()
            service = current.get(service_id)
            adapter = current.adapter(service)
            handler=getattr(adapter,'inspect',None)
            if not callable(handler):raise CatalogError('service uses registered query signatures; list its capabilities first')
            return handler(target_alias)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True)
    async def probe_external_mcp(service_id: str) -> dict[str, Any]:
        """List an enabled external MCP's tools without executing any of them."""
        try:
            return await probe_external(fresh_catalog().get(service_id))
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    return mcp


def main() -> None:
    try:
        catalog = Catalog.load(configured_path())
    except CatalogError as exc:
        raise SystemExit(f"catalog error: {exc}") from exc
    build_server(catalog).run()


if __name__ == "__main__":
    main()
