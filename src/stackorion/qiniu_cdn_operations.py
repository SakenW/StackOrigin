"""Exact CDN changes with preview, drift check and durable one-dispatch receipt."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import urllib.parse

from .catalog import CatalogError
from .kodo_operations import DIGEST, digest
from .operation_runtime import OPERATION_ID, load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition
from .qiniu import _regular_owned
from .qiniu_cdn import QiniuCDNAdapter, TOKEN, domain_name

class QiniuCDNOperations:
    provider='qiniu'
    definitions=(OperationDefinition('cdn.change.v1','plan_qiniu_cdn_change','apply_qiniu_cdn_change','get_qiniu_cdn_operation'),)

    def __init__(self,catalog): self.catalog=catalog

    def adapter(self,service_id):
        service=self.catalog.get(service_id)
        if service.provider!='qiniu' or service.adapter!='qiniu-cdn':
            raise CatalogError('service does not support CDN operations')
        adapter=self.catalog.adapter(service)
        if not isinstance(adapter,QiniuCDNAdapter):raise CatalogError('unexpected CDN adapter')
        return service,adapter

    @staticmethod
    def parameters(domain,action,urls,cert_id,force_https,http2):
        domain_name(domain)
        if action not in {'refresh','refresh-directory','prefetch','online','offline','https'}:
            raise CatalogError('unsupported exact CDN action')
        if not isinstance(urls,list) or any(not isinstance(v,str) for v in urls) or len(set(urls))!=len(urls):
            raise CatalogError('invalid or duplicate CDN URLs')
        if type(force_https) is not bool or type(http2) is not bool or not isinstance(cert_id,str):
            raise CatalogError('invalid CDN HTTPS settings')
        if action in {'refresh','refresh-directory','prefetch'}:
            if not 1<=len(urls)<= (5 if action=='refresh-directory' else 20) or cert_id or force_https or http2:
                raise CatalogError('invalid CDN cache operation parameters')
            for value in urls:
                parsed=urllib.parse.urlsplit(value)
                try: port=parsed.port
                except ValueError as exc:raise CatalogError('invalid CDN URL port') from exc
                if (len(value)>2048 or parsed.scheme not in {'http','https'} or parsed.netloc!=domain or
                        parsed.hostname!=domain or parsed.username or parsed.password or port or parsed.fragment or
                        not parsed.path.startswith('/') or '*' in value or '?' in parsed.path or
                        any(ord(c)<33 or ord(c)>126 for c in value) or '\\' in value or
                        (action=='refresh-directory' and (not parsed.path.endswith('/') or parsed.query)) or
                        (action=='prefetch' and parsed.path.endswith('/'))):
                    raise CatalogError('each URL must target the exact owned domain without credentials, port or wildcard')
                try:
                    pairs=urllib.parse.parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True,errors='strict')
                except (ValueError,UnicodeError) as exc:
                    raise CatalogError('invalid CDN URL query parameters') from exc
                if (len(pairs)>2 or len({k for k,v in pairs})!=len(pairs) or
                        any(k not in {'v','version'} or not re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,9}){0,3}',v) for k,v in pairs)):
                    raise CatalogError('CDN URLs may contain only non-sensitive numeric v/version query parameters')
        elif action=='https':
            if urls or not TOKEN.fullmatch(cert_id):raise CatalogError('one existing certificate id is required')
        elif urls or cert_id or force_https or http2:
            raise CatalogError('domain state actions do not accept cache or HTTPS settings')
        return {'action':action,'urls':urls.copy(),'cert_id':cert_id,'force_https':force_https,'http2':http2}

    def _plan(self,service_id,operation,domain,parameters):
        if operation!='cdn.change.v1':raise CatalogError('unsupported CDN operation')
        parameters=self.parameters(domain,**parameters)
        service,adapter=self.adapter(service_id)
        current=adapter.domain(domain)['domain']
        if current.get('operatingState') not in {'success','offline','offlined'}:
            raise CatalogError('CDN domain is not in a settled supported state')
        source=adapter.foundation_root/'scripts/foundation_operation.py'
        _regular_owned(source,secret=False)
        cert=None
        if parameters['action']=='https':
            cert=adapter.certificate(parameters['cert_id'])
            names=cert.get('dnsnames') or [cert.get('common_name')]
            matches=lambda n: n==domain or (isinstance(n,str) and n.startswith('*.') and domain.split('.',1)[-1]==n[2:])
            now=datetime.now(timezone.utc).timestamp()
            if (not isinstance(names,list) or not any(matches(n) for n in names) or
                    type(cert.get('not_before')) not in (int,float) or type(cert.get('not_after')) not in (int,float) or
                    not cert['not_before']<=now<cert['not_after']):
                raise CatalogError('certificate is not valid now or does not cover the exact domain')
        basis={'operation':operation,'service':service_id,'account_id':service.account_id,'domain':domain,
               'parameters':parameters,'current':current,'certificate':cert,
               'journal_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
        return {'status':'planned','basis':basis,'expected_state':digest(basis),'mutation_invoked':False,
                'effects':{'asynchronous':True,'dispatch_is_not_completion':True,
                    'interrupts_access':parameters['action']=='offline','may_consume_origin_traffic':parameters['action'] in {'refresh','refresh-directory','prefetch'},
                    'external_writers_not_locked':True}}

    def plan(self,service_id,operation,domain,action,urls=None,cert_id='',force_https=False,http2=False):
        params=self.parameters(domain,action,[] if urls is None else urls,cert_id,force_https,http2)
        return self._plan(service_id,operation,domain,params)

    def apply(self,service_id,operation,domain,action,expected_state,confirm_domain,operation_id,
              urls=None,cert_id='',force_https=False,http2=False):
        if (confirm_domain!=domain or not isinstance(expected_state,str) or not DIGEST.fullmatch(expected_state)
                or not isinstance(operation_id,str) or not OPERATION_ID.fullmatch(operation_id) or operation!='cdn.change.v1'):
            raise CatalogError('exact domain, preview digest and operation id confirmation required')
        params=self.parameters(domain,action,[] if urls is None else urls,cert_id,force_https,http2)
        service,adapter=self.adapter(service_id)
        source=adapter.foundation_root/'scripts/foundation_operation.py'; _regular_owned(source,secret=False)
        journal=load_journal(source); root=state_root()
        identity={'operation':operation,'service':service_id,'account_id':service.account_id,'domain':domain,
                  'parameters':params,'expected_state':expected_state,'journal_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
        with resource_lock(root,f'cdn:{service.account_id}:{domain}'):
            try:
                with journal.Operation(root,operation_id,identity,resume=True) as job:
                    reused=job.value['stages'].get('dispatch',{}).get('status')=='success'
                    def dispatch():
                        preview=self._plan(service_id,operation,domain,params)
                        if preview['expected_state']!=expected_state:
                            raise CatalogError('CDN preview state changed; create a new preview')
                        if action in {'refresh','refresh-directory','prefetch'}:
                            api_action='prefetch' if action=='prefetch' else 'refresh'
                            body={'dirs' if action=='refresh-directory' else 'urls':params['urls']}
                            raw=adapter.request('fusion.qiniuapi.com','POST','/v2/tune/'+api_action,body)
                            request_id=raw.get('requestId'); tasks=raw.get('taskIds')
                            if (not isinstance(request_id,str) or not TOKEN.fullmatch(request_id) or
                                    not isinstance(tasks,dict) or set(tasks)!=set(params['urls']) or
                                    any(not isinstance(v,str) or not TOKEN.fullmatch(v) for v in tasks.values()) or
                                    raw.get('invalidUrls') or raw.get('invalidDirs')):
                                raise CatalogError('cache dispatch result incomplete; reconcile without resubmission')
                            return {'status':'accepted','request_id':request_id,'task_ids':tasks,'task_action':api_action,'mutation_invoked':True,'remote_completion_verified':False}
                        if action=='https':
                            current=preview['basis']['current']
                            path='httpsconf' if current.get('protocol')=='https' else 'sslize'
                            raw=adapter.request('api.qiniu.com','PUT',f'/domain/{domain}/{path}',
                                {'certId':cert_id,'forceHttps':force_https,'http2Enable':http2})
                        else:
                            raw=adapter.request('api.qiniu.com','POST',f'/domain/{domain}/{action}')
                        if raw.get('code')!=200:raise CatalogError('CDN dispatch result unknown')
                        return {'status':'accepted','domain':domain,'action':action,'mutation_invoked':True,'remote_completion_verified':False}
                    result=job.stage('dispatch',dispatch)
                    job.complete()  # Completion means durable dispatch receipt, not CDN propagation.
                    return {**result,'operation_id':operation_id,'reused_completed_receipt':reused,
                            'operation_file':str(job.path),'receipt_scope':'request acceptance only; query domain or tasks for completion'}
            except (journal.OperationError,CatalogError,OSError) as exc:
                return {'status':'needs-reconciliation','operation_id':operation_id,'replayed':False,
                        'reason':'dispatch not repeated; inspect operation receipt and remote state'}

    def receipt(self,operation_id):return read_receipt(operation_id,{'cdn.change.v1'})
