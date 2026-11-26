"""Domain protocol schemas; all provider policy and requests stay in plugins."""
from typing import Any
from mcp.types import ToolAnnotations
from .catalog import CatalogError
from .domain_delivery import inspect_delivery

def register_domain_tools(mcp,current_catalog):
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(read_only_hint=True))
    def inspect_domain_delivery(service_id: str, domain: str, zone_id: str = '') -> dict[str,Any]:
        """Read provider-owned domain, public NS, one authoritative SOA and pinned-IP HTTPS HEAD. Cloudflare requires exact zone_id. No DNS changes; acceptance, propagation and HTTPS are separate observations."""
        try:return inspect_delivery(current_catalog(),service_id,domain,zone_id)
        except (CatalogError,TypeError,ValueError) as exc:
            return {'status':'unavailable','reason':str(exc) if isinstance(exc,CatalogError) else 'invalid domain observation parameters'}
