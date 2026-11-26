"""Discover an upstream MCP without proxying credentials or write tools."""

from __future__ import annotations

from typing import Any, Callable

from mcp import Client

from .catalog import CatalogError, Service


async def probe_external(service: Service, client_factory: Callable[[Any], Any] = Client) -> dict[str, Any]:
    if not service.enabled or service.kind != "external-mcp":
        raise CatalogError("external service is unavailable")
    url = service.settings["url"]
    try:
        async with client_factory(url) as client:
            result = await client.list_tools()
    except Exception as exc:
        raise CatalogError("external MCP unavailable or requires authentication") from exc
    return {
        "service": service.id,
        "status": "reachable",
        "tool_count": len(result.tools),
        "coverage": "tool listing only; account and permissions not verified",
    }
