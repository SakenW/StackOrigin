"""MCP handoff tools for the full server and the scoped dashboard entrypoint."""

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import dashboard_bridge
from .catalog import CatalogError


def register_dashboard_tools(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def list_dashboard_operation_targets() -> dict[str, Any]:
        """Find enabled dashboard preview bindings with their account, resource and action. This reads local bindings only."""
        try:
            return dashboard_bridge.list_operation_targets()
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_dashboard_inspection(include_demo: bool = False) -> dict[str, Any]:
        """Read the dashboard's most recent inspection coverage and findings counts. This does not run an inspection. Set include_demo only when reviewing synthetic data."""
        try:
            return dashboard_bridge.get_inspection(include_demo)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_dashboard_work_item(work_item_id: str) -> dict[str, Any]:
        """Read an exact item and its evidence from the configured local dashboard."""
        try:
            return dashboard_bridge.get_work_item(work_item_id)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    def prepare_dashboard_operation(binding_id: str, parameters: dict[str, Any], work_item_id: str | None = None) -> dict[str, Any]:
        """Save a read-only provider preview in the local dashboard and return its review link. Pass {} for QVM reboot, or name, content and ttl for DNS. This never dispatches a change."""
        try:
            return dashboard_bridge.prepare_operation(binding_id, parameters, work_item_id)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def get_dashboard_operation(record_id: str) -> dict[str, Any]:
        """Read the exact preview or receipt from the configured local dashboard without resubmitting it."""
        try:
            return dashboard_bridge.get_operation(record_id)
        except CatalogError as exc:
            return {"status": "unavailable", "reason": str(exc)}


def main() -> None:
    mcp = MCPServer("StackOrion Dashboard", title="StackOrigin 运维面板",
                    instructions="Read dashboard work items and operation records, and save previews for review on the dashboard.")
    register_dashboard_tools(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
