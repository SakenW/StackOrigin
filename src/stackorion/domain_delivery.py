"""Observe public delivery only after an exact provider-owned domain query."""
from .catalog import CatalogError
from .queries import QueryRegistry
from .domain_observation import observe

def inspect_delivery(catalog,service_id,domain,zone_id=''):
    service=catalog.get(service_id)
    if service.adapter=='cloudflare-domains' and service.provider=='cloudflare':
        if not zone_id:raise CatalogError('exact Cloudflare zone id required')
        control=QueryRegistry(catalog).query(service_id,'zone',{'zone_id':zone_id,'domain':domain})
        row=control.get('zone')
        if not isinstance(row,dict) or row.get('name')!=domain or row.get('id')!=zone_id:
            raise CatalogError('provider domain identity unavailable')
        servers=row.get('name_servers',[])
    elif service.adapter=='spaceship-management' and service.provider=='spaceship':
        if zone_id:raise CatalogError('Spaceship does not accept a Cloudflare zone id')
        control=QueryRegistry(catalog).query(service_id,'domain',{'domain':domain})
        row=control.get('data')
        if not isinstance(row,dict) or row.get('name')!=domain:
            raise CatalogError('provider domain identity unavailable')
        servers=(row.get('nameservers') or {}).get('hosts',[])
    else:raise CatalogError('delivery observation requires an owned domain management adapter')
    return {'service':service_id,'provider':service.provider,'control_plane':control,
            'delivery':observe(domain,servers),'mutation_invoked':False}
