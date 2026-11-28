"""Explicit query allowlists, independent of MCP tool registration."""
import inspect
from .catalog import CatalogError

QUERY_UPSTREAMS = frozenset({
    'official-mcp', 'official-sdk', 'official-cli', 'official-api',
    'foundation', 'custom-api', 'local',
})

# Only audited methods are dispatchable. New trusted adapter modules may publish
# their own query_methods dict, so adding a plugin needs no new MCP tool schema.
BUILTIN_QUERIES={
    'qiniu-qvm':{'inventory':'discover','instance':'inspect','metrics':'metrics'},
    'qiniu-las':{'targets':'target_inventory','instance':'inspect'},
    'qiniu-kodo':{'buckets':'discover','bucket':'inspect'},
    'qiniu-kodo-objects':{'buckets':'discover','bucket':'inspect','objects':'list_objects','object':'stat_object'},
    'qiniu-host-capacity':{'capacity':'inspect'},
    'qiniu-host-status':{'services':'services','backups':'backups'},
    'qiniu-host-diagnostics':{'journal':'journal','container':'container'},
    'qiniu-cdn':{'domains':'domains','domain':'domain','certificates':'certificates','usage':'usage','tasks':'tasks'},
    'qiniu-kodo-admin':{'bucket':'bucket_info','domains':'bucket_domains','storage':'storage_statistics','object':'stat_object'},
    'qiniu-kodo-bucket-policy':{'bucket':'bucket_info','domains':'bucket_domains'},
    'qiniu-instance-lifecycle':{'targets':'discover','instance':'inspect'},
}

class QueryRegistry:
    def __init__(self,catalog):self.catalog=catalog

    def definitions(self,service_id):
        service=self.catalog.get(service_id)
        if service.adapter in BUILTIN_QUERIES and service.adapter.startswith('qiniu-') and service.provider!='qiniu':
            raise CatalogError('query adapter provider does not match service')
        adapter=self.catalog.adapter(service)
        methods=getattr(adapter,'query_methods',BUILTIN_QUERIES.get(service.adapter,{}))
        if (not isinstance(methods,dict) or any(not isinstance(k,str) or not isinstance(v,str) or
                v.startswith('_') or not callable(getattr(adapter,v,None)) for k,v in methods.items())):
            raise CatalogError('invalid registered query contract')
        return adapter,methods

    def describe(self,service_id):
        adapter,methods=self.definitions(service_id);rows=[]
        upstreams = getattr(adapter, 'query_upstreams', {})
        if not isinstance(upstreams, dict) or any(
            key not in methods or not isinstance(value, str) or value not in QUERY_UPSTREAMS
            for key, value in upstreams.items()
        ):
            raise CatalogError('invalid registered query upstream')
        for key,method in methods.items():
            signature=inspect.signature(getattr(adapter,method))
            parameters=[{'name':p.name,'required':p.default is inspect.Parameter.empty,
                         **({} if p.annotation is inspect.Parameter.empty else
                            {'python_type':p.annotation if isinstance(p.annotation,str) else inspect.formatannotation(p.annotation)}),
                         **({} if p.default is inspect.Parameter.empty else {'default':p.default})}
                        for p in signature.parameters.values() if p.kind not in {p.VAR_POSITIONAL,p.VAR_KEYWORD}]
            row = {'id':key,'parameters':parameters,'mutates':False}
            if key in upstreams:
                row['upstream'] = upstreams[key]
            rows.append(row)
        return {'service':service_id,'queries':rows}

    def query(self,service_id,query_id,parameters):
        adapter,methods=self.definitions(service_id)
        if query_id not in methods or not isinstance(parameters,dict) or len(parameters)>20:
            raise CatalogError('query is not registered for this service')
        method=getattr(adapter,methods[query_id]);signature=inspect.signature(method)
        if any(p.kind in {p.VAR_POSITIONAL,p.VAR_KEYWORD} for p in signature.parameters.values()):
            raise CatalogError('query cannot accept open-ended parameters')
        try:signature.bind(**parameters)
        except TypeError as exc:raise CatalogError('query parameters do not match the registered signature') from exc
        return method(**parameters)
