"""Typed Qiniu tool schemas; requests and policy remain in plugins."""
from typing import Any, Literal
from mcp.types import ToolAnnotations

def register_qiniu_tools(mcp, query, operation, receipt, observe):
    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_bucket_privacy(service_id: str, target_alias: str, private: Literal[0,1]) -> dict[str, Any]:
        """Preview exact bucket privacy. Affects all current and future objects and unsigned URLs."""
        return operation(service_id,'kodo.bucket-privacy.v1','plan',alias=target_alias,private=private)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False,destructive_hint=True,idempotent_hint=False))
    def apply_kodo_bucket_privacy(service_id: str, target_alias: str, private: Literal[0,1], expected_state: str, confirm_bucket: str, operation_id: str) -> dict[str, Any]:
        """Execute reviewed bucket privacy once; public may expose objects, private may break unsigned links."""
        return operation(service_id,'kodo.bucket-privacy.v1','apply',alias=target_alias,private=private,expected_state=expected_state,confirm_bucket=confirm_bucket,operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_kodo_bucket_operation(operation_id: str) -> dict[str, Any]:
        """Read exact bucket policy operation receipt; no cloud mutation."""
        return receipt('qiniu-kodo-bucket-policy',operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def inspect_qiniu_journal(service_id: str, target_alias: str, unit: str, hours: int = 1, limit: int = 100) -> dict[str, Any]:
        """Read bounded fixed-unit journal severity metadata; no raw log messages."""
        return query(service_id, 'journal', alias=target_alias, unit=unit, hours=hours, limit=limit)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def inspect_qiniu_container(service_id: str, target_alias: str, container_id: str) -> dict[str, Any]:
        """Read health and restart metadata of one full exact container ID; no env or health log bodies."""
        return query(service_id, 'container', alias=target_alias, container_id=container_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def inspect_kodo_bucket_config(service_id: str, target_alias: str) -> dict[str, Any]:
        """Read owned bucket configuration metadata. No space modification."""
        return query(service_id, 'bucket_config', alias=target_alias)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def list_kodo_bucket_domains(service_id: str, target_alias: str) -> dict[str, Any]:
        """Read owned bucket domain metadata."""
        return query(service_id, 'bucket_domains', alias=target_alias)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def query_kodo_storage_statistics(service_id: str, target_alias: str, begin: str, end: str, granularity: str = 'day') -> dict[str, Any]:
        """Read bounded storage statistics using the documented date format."""
        return query(service_id, 'storage_statistics', alias=target_alias, begin=begin, end=end, granularity=granularity)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def list_qiniu_cdn_domains(service_id: str, marker: str = '', limit: int = 100) -> dict[str, Any]:
        """Read one owned CDN domain page."""
        return query(service_id, 'domains', marker=marker, limit=limit)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def inspect_qiniu_cdn_domain(service_id: str, domain: str) -> dict[str, Any]:
        """Read exact owned domain and filtered HTTPS metadata."""
        return query(service_id, 'domain', domain=domain)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def list_qiniu_cdn_certificates(service_id: str, marker: str = '', limit: int = 100) -> dict[str, Any]:
        """Read one certificate metadata page; private keys and certificate bodies excluded."""
        return query(service_id, 'certificates', marker=marker, limit=limit)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def query_qiniu_cdn_usage(service_id: str, domain: str, start_date: str, end_date: str, metric: Literal['flux','bandwidth'] = 'flux', granularity: Literal['day','hour','5min'] = 'day') -> dict[str, Any]:
        """Read bounded traffic or bandwidth series of one owned domain."""
        return query(service_id, 'usage', domain=domain, start_date=start_date, end_date=end_date, metric=metric, granularity=granularity)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def query_qiniu_cdn_tasks(service_id: str, action: Literal['refresh','prefetch'], request_id: str, page: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read one exact refresh or prefetch request task page; no redispatch."""
        return query(service_id, 'tasks', action=action, request_id=request_id, page=page, limit=limit)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_mime_change(service_id: str, target_alias: str, key: str, mime_type: str) -> dict[str, Any]:
        """Preview one exact object property change; no mutation."""
        return operation(service_id, 'kodo.change-mime.v1', "plan", alias=target_alias, key=key, mime_type=mime_type)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_kodo_mime_change(service_id: str, target_alias: str, key: str, mime_type: str, expected_state: str, confirm_bucket: str, confirm_key: str, operation_id: str) -> dict[str, Any]:
        """Execute the reviewed exact object change once; unknown results require read-only reconciliation."""
        return operation(service_id, 'kodo.change-mime.v1', "apply", alias=target_alias, key=key, mime_type=mime_type, expected_state=expected_state, confirm_bucket=confirm_bucket, confirm_key=confirm_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_storage_class_change(service_id: str, target_alias: str, key: str, storage_type: int) -> dict[str, Any]:
        """Preview one exact object property change; no mutation."""
        return operation(service_id, 'kodo.change-storage-class.v1', "plan", alias=target_alias, key=key, storage_type=storage_type)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_kodo_storage_class_change(service_id: str, target_alias: str, key: str, storage_type: int, expected_state: str, confirm_bucket: str, confirm_key: str, operation_id: str) -> dict[str, Any]:
        """Execute the reviewed exact object change once; unknown results require read-only reconciliation."""
        return operation(service_id, 'kodo.change-storage-class.v1', "apply", alias=target_alias, key=key, storage_type=storage_type, expected_state=expected_state, confirm_bucket=confirm_bucket, confirm_key=confirm_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_archive_restore(service_id: str, target_alias: str, key: str, freeze_after_days: int) -> dict[str, Any]:
        """Preview one exact object property change; no mutation."""
        return operation(service_id, 'kodo.restore-archive.v1', "plan", alias=target_alias, key=key, freeze_after_days=freeze_after_days)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_kodo_archive_restore(service_id: str, target_alias: str, key: str, freeze_after_days: int, expected_state: str, confirm_bucket: str, confirm_key: str, operation_id: str) -> dict[str, Any]:
        """Execute the reviewed exact object change once; unknown results require read-only reconciliation."""
        return operation(service_id, 'kodo.restore-archive.v1', "apply", alias=target_alias, key=key, freeze_after_days=freeze_after_days, expected_state=expected_state, confirm_bucket=confirm_bucket, confirm_key=confirm_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_delete_after_days(service_id: str, target_alias: str, key: str, delete_after_days: int) -> dict[str, Any]:
        """Preview one exact object property change; no mutation."""
        return operation(service_id, 'kodo.delete-after-days.v1', "plan", alias=target_alias, key=key, delete_after_days=delete_after_days)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_kodo_delete_after_days(service_id: str, target_alias: str, key: str, delete_after_days: int, expected_state: str, confirm_bucket: str, confirm_key: str, operation_id: str) -> dict[str, Any]:
        """Execute the reviewed exact object change once; unknown results require read-only reconciliation."""
        return operation(service_id, 'kodo.delete-after-days.v1', "apply", alias=target_alias, key=key, delete_after_days=delete_after_days, expected_state=expected_state, confirm_bucket=confirm_bucket, confirm_key=confirm_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_kodo_status_change(service_id: str, target_alias: str, key: str, status: int) -> dict[str, Any]:
        """Preview one exact object property change; no mutation."""
        return operation(service_id, 'kodo.change-status.v1', "plan", alias=target_alias, key=key, status=status)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_kodo_status_change(service_id: str, target_alias: str, key: str, status: int, expected_state: str, confirm_bucket: str, confirm_key: str, operation_id: str) -> dict[str, Any]:
        """Execute the reviewed exact object change once; unknown results require read-only reconciliation."""
        return operation(service_id, 'kodo.change-status.v1', "apply", alias=target_alias, key=key, status=status, expected_state=expected_state, confirm_bucket=confirm_bucket, confirm_key=confirm_key, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_kodo_admin_operation(operation_id: str) -> dict[str, Any]:
        """Read object administration receipt; never dispatch."""
        return receipt('qiniu-kodo-admin', operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def reconcile_kodo_admin_operation(service_id: str, operation_id: str) -> dict[str, Any]:
        """Observe exact object state from its receipt without replaying."""
        return observe(service_id, 'reconcile', operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_qiniu_instance_action(service_id: str, target_alias: str, action: Literal['start','stop','reboot']) -> dict[str, Any]:
        """Preview an exact instance action and its availability/billing effects."""
        return operation(service_id, 'qvm.'+action, 'plan', alias=target_alias)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_qiniu_instance_action(service_id: str, target_alias: str, action: Literal['start','stop','reboot'], expected_state: str, confirm_instance_id: str, operation_id: str) -> dict[str, Any]:
        """Dispatch the reviewed lifecycle action once. Acceptance does not prove completion or application health."""
        return operation(service_id, 'qvm.'+action, 'apply', alias=target_alias, expected_state=expected_state, confirm_instance_id=confirm_instance_id, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_qiniu_instance_operation(operation_id: str) -> dict[str, Any]:
        """Read lifecycle operation receipt without dispatch."""
        return receipt('qiniu-instance-lifecycle', operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def query_qiniu_instance_operation(service_id: str, target_alias: str, operation_id: str) -> dict[str, Any]:
        """Observe exact instance state tied to a receipt; never replay reboot."""
        return observe(service_id, 'query', alias=target_alias, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def plan_qiniu_cdn_change(service_id: str, domain: str, action: Literal['refresh','refresh-directory','prefetch','online','offline','https'], urls: list[str] | None = None, cert_id: str = '', force_https: bool = False, http2: bool = False) -> dict[str, Any]:
        """Preview exact CDN cache/domain/HTTPS changes and asynchronous effects."""
        return operation(service_id, 'cdn.change.v1', 'plan', domain=domain, action=action, urls=urls, cert_id=cert_id, force_https=force_https, http2=http2)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
    def apply_qiniu_cdn_change(service_id: str, domain: str, action: Literal['refresh','refresh-directory','prefetch','online','offline','https'], expected_state: str, confirm_domain: str, operation_id: str, urls: list[str] | None = None, cert_id: str = '', force_https: bool = False, http2: bool = False) -> dict[str, Any]:
        """Dispatch approved exact CDN change once; use task/domain queries to verify completion."""
        return operation(service_id, 'cdn.change.v1', 'apply', domain=domain, action=action, urls=urls, cert_id=cert_id, force_https=force_https, http2=http2, expected_state=expected_state, confirm_domain=confirm_domain, operation_id=operation_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_qiniu_cdn_operation(operation_id: str) -> dict[str, Any]:
        """Read CDN dispatch receipt, not proof of remote propagation."""
        return receipt('qiniu-cdn', operation_id)
