"""Route scoped operations to installed plugins independently of MCP transport."""

from copy import deepcopy
from dataclasses import dataclass, field
import inspect
from importlib.metadata import entry_points
from typing import Any

from .catalog import Catalog, CatalogError


@dataclass(frozen=True)
class OperationDefinition:
    id: str
    plan_tool: str
    apply_tool: str
    receipt_tool: str
    parameter_contract: dict[str, Any] | None = field(default=None, hash=False)

    def public(self) -> dict[str, Any]:
        result = {"id": self.id, "mutates": True, "requires_client_approval": True,
                  "plan_tool": self.plan_tool, "apply_tool": self.apply_tool,
                  "receipt_tool": self.receipt_tool}
        if self.parameter_contract is not None:
            result["parameter_contract"] = deepcopy(self.parameter_contract)
        return result


class OperationRegistry:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def _plugin_type(self, adapter_name: str, *, optional: bool = False):
        matches = [ep for ep in entry_points(group="stackorion.operations")
                   if ep.name == adapter_name]
        if not matches and optional:
            return None
        if len(matches) != 1:
            raise CatalogError("operation plugin is missing or ambiguous")
        try:
            plugin = matches[0].load()
        except Exception as exc:
            # Loading errors can contain private paths or provider settings.
            raise CatalogError("operation plugin could not be loaded") from exc
        if (not isinstance(plugin, type) or not isinstance(getattr(plugin, "provider", None), str)
                or not isinstance(getattr(plugin, "definitions", None), tuple)
                or not all(isinstance(d, OperationDefinition) for d in plugin.definitions)
                or len({d.id for d in plugin.definitions}) != len(plugin.definitions)
                or not all(callable(getattr(plugin, name, None)) for name in ("plan", "apply", "receipt"))):
            raise CatalogError("invalid operation plugin contract")
        return plugin

    def _service_plugin(self, service_id: str, *, optional: bool = False, allow_disabled: bool = False):
        service = self.catalog.get(service_id)
        if not service.enabled and not allow_disabled:
            raise CatalogError("service is disabled")
        if service.kind != "python-adapter":
            raise CatalogError("service has no local operation plugin")
        plugin = self._plugin_type(service.adapter, optional=optional)
        if plugin is not None and plugin.provider != service.provider:
            raise CatalogError("operation plugin provider does not match service")
        return plugin

    def describe(self, service_id: str) -> dict[str, Any]:
        plugin = self._service_plugin(service_id, optional=True)
        service = self.catalog.get(service_id)
        if service.adapter == "qiniu-qvm" and "script_path" not in service.settings:
            plugin = None
        result={"service": service_id, "status": "available" if plugin else "unsupported",
                "operations": [d.public() for d in plugin.definitions] if plugin else []}
        if plugin is not None:
            if callable(getattr(plugin, 'operation_upstream', None)):
                upstream = plugin(self.catalog).operation_upstream
                for row in result['operations']:
                    route = upstream(service_id, row['id'])
                    if route:
                        row['upstream'] = route
            contracts={}
            for phase in ('plan','apply'):
                signature=inspect.signature(getattr(plugin,phase));fields=[]
                for p in signature.parameters.values():
                    if p.name in {'self','service_id','operation'} or p.kind in {p.VAR_POSITIONAL,p.VAR_KEYWORD}:continue
                    fields.append({'name':p.name,'required':p.default is inspect.Parameter.empty,
                                   **({} if p.default is inspect.Parameter.empty else {'default':p.default})})
                contracts[phase]={'parameters':fields,'extra_parameters_validated_by_plugin':any(p.kind==p.VAR_KEYWORD for p in signature.parameters.values())}
            result['parameter_contract']=contracts
        return result

    def _operation(self, service_id: str, operation: str):
        plugin = self._service_plugin(service_id)
        if operation not in {d.id for d in plugin.definitions}:
            raise CatalogError("operation is not registered for service")
        return plugin(self.catalog)

    def plan(self, service_id: str, operation: str, **parameters) -> dict[str, Any]:
        return self._operation(service_id, operation).plan(service_id, operation, **parameters)

    def apply(self, service_id: str, operation: str, **parameters) -> dict[str, Any]:
        return self._operation(service_id, operation).apply(service_id, operation, **parameters)

    def receipt(self, adapter_name: str, operation_id: str) -> dict[str, Any]:
        # Readback remains available after disabling a service. Never dispatch here.
        return self._plugin_type(adapter_name)(self.catalog).receipt(operation_id)

    def service_receipt(self, service_id: str, operation_id: str) -> dict[str, Any]:
        plugin = self._service_plugin(service_id, allow_disabled=True)
        receipt = plugin(self.catalog).receipt(operation_id)
        identity = receipt.get("identity")
        if not isinstance(identity, dict) or identity.get("service") != service_id:
            raise CatalogError("operation receipt is unavailable for this service")
        return receipt

    def observe(self, service_id: str, method: str, **parameters) -> dict[str, Any]:
        # Only these audited operation observers are read-only; arbitrary plugin
        # methods are never exposed by the protocol layer.
        service = self.catalog.get(service_id)
        allowed = {'qiniu-kodo-admin': 'reconcile', 'qiniu-instance-lifecycle': 'query'}
        if allowed.get(service.adapter) != method:
            raise CatalogError('read-only operation observer is not registered')
        plugin = self._service_plugin(service_id)(self.catalog)
        return getattr(plugin, method)(service_id, **parameters)
