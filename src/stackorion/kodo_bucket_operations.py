"""Exact bucket privacy changes; no bulk object or prefix deletion."""
import json
from uuid import UUID
from .catalog import CatalogError
from .kodo_admin import KodoAdminAdapter, UC_HOST
from .kodo_operations import DIGEST, digest
from .operation_runtime import load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition

class KodoBucketOperations:
    provider='qiniu'
    definitions=(OperationDefinition('kodo.bucket-privacy.v1','plan_kodo_bucket_privacy','apply_kodo_bucket_privacy','get_kodo_bucket_operation'),)

    def __init__(self,catalog):self.catalog=catalog

    def adapter(self,service_id):
        service=self.catalog.get(service_id)
        if service.provider!='qiniu' or service.adapter!='qiniu-kodo-bucket-policy':
            raise CatalogError('service does not support bucket privacy management')
        adapter=self.catalog.adapter(service)
        if not isinstance(adapter,KodoAdminAdapter):raise CatalogError('unexpected bucket policy adapter')
        return service,adapter

    def _plan(self,service_id,operation,alias,private):
        if operation!='kodo.bucket-privacy.v1' or type(private) is not int or private not in {0,1}:
            raise CatalogError('bucket privacy must be 0 (public) or 1 (private)')
        service,adapter=self.adapter(service_id)
        current=adapter.bucket_info(alias);_,fingerprint=adapter.foundation()
        basis={'operation':operation,'service':service_id,'account_id':service.account_id,
               'target':alias,'bucket':current['bucket'],'private':private,'current':current['info'],
               'foundation_sha256':fingerprint}
        return {**basis,'state_digest':digest(basis),'status':'planned','mutation_invoked':False,
                'scope':'all existing and future objects in the exact bucket',
                'impact':'public access may expose objects' if private==0 else 'unsigned links may stop working',
                'external_writers_not_locked':True},adapter

    def plan(self,service_id,operation,alias,private):return self._plan(service_id,operation,alias,private)[0]

    def apply(self,service_id,operation,alias,private,expected_state,confirm_bucket,operation_id):
        try:
            if not isinstance(operation_id,str) or str(UUID(operation_id))!=operation_id:raise ValueError()
        except (ValueError,AttributeError):raise CatalogError('canonical operation UUID required') from None
        service,adapter=self.adapter(service_id)
        if (operation!='kodo.bucket-privacy.v1' or type(private) is not int or private not in {0,1} or
                alias not in adapter.targets or adapter.targets[alias]['bucket']!=confirm_bucket or
                not isinstance(expected_state,str) or not DIGEST.fullmatch(expected_state)):
            raise CatalogError('exact bucket, privacy and preview digest confirmation required')
        _,fingerprint=adapter.foundation()
        identity={'operation':operation,'service':service_id,'account_id':service.account_id,'target':alias,
                  'bucket':confirm_bucket,'private':private,'expected_state':expected_state,'foundation_sha256':fingerprint}
        root=state_root();journal=load_journal(adapter.foundation_root/'scripts/foundation_operation.py')
        with resource_lock(root,json.dumps(['bucket-policy',service.account_id,confirm_bucket],separators=(',',':'))):
            try:
                with journal.Operation(root,operation_id,identity,resume=True) as job:
                    reused=job.value['stages'].get('change',{}).get('status')=='success'
                    def change():
                        plan,current_adapter=self._plan(service_id,operation,alias,private)
                        if plan['state_digest']!=expected_state:raise CatalogError('bucket privacy state changed; preview again')
                        job.stage('approved-state',lambda:plan)
                        target,client,*_=current_adapter.context(alias)
                        # Context discovery is read-only; recheck privacy after it.
                        if current_adapter.bucket_info(alias)['info']!=plan['current']:
                            raise CatalogError('bucket configuration changed before dispatch')
                        accepted=False
                        try:
                            current_adapter._request(client,'POST',UC_HOST,'/private',params={'bucket':confirm_bucket,'private':str(private)},mutation=True)
                            accepted=True
                        except CatalogError:pass
                        try:after=current_adapter.bucket_info(alias)['info']
                        except CatalogError:after=None
                        verified=bool(accepted and isinstance(after,dict) and after.get('private')==private and
                                      {k:v for k,v in after.items() if k!='private'}=={k:v for k,v in plan['current'].items() if k!='private'})
                        return {'status':'success' if verified else 'needs-reconciliation','mutation_invoked':True,
                                'bucket':confirm_bucket,'private':private,'observed':after,'verified':verified,
                                'dispatch_outcome':'accepted' if accepted else 'unknown','replayed':False}
                    result=job.stage('change',change)
                    if result['status']=='success':job.complete()
                    else:job.value['status']=result['status'];job.save()
                    return {**result,'operation_id':operation_id,'reused_completed_receipt':reused,
                            'live_state_verified_this_invocation':not reused and result['verified']}
            except (journal.OperationError,CatalogError,OSError):
                return {'status':'needs-reconciliation','operation_id':operation_id,'replayed':False,
                        'reason':'inspect retained receipt and bucket info; request was not replayed'}

    def receipt(self,operation_id):return read_receipt(operation_id,{'kodo.bucket-privacy.v1'})
