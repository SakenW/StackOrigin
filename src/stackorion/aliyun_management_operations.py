"""Single-resource approvals, persistent fences and one-shot Alibaba dispatch."""
from __future__ import annotations

import ipaddress
import json
import os
import re
from uuid import UUID

from .aliyun_management import (AliyunManagementAdapter, PreDispatchError, digest, hostname, identifier,
                               matched, numeric, domain_identifier, private_file, safe_row)
from .catalog import CatalogError
from .operation_runtime import load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition

ACTIONS = ('instance-start','instance-stop','instance-reboot','instance-rename',
           'sg-authorize','sg-revoke','dns-create','dns-update','dns-delete','dns-status',
           'cdn-refresh','cdn-preheat','snapshot-create','receipt-adopt')
OPERATIONS = frozenset('aliyun.'+x+'.v1' for x in ACTIONS)
FIELDS = ('domain','record_id','record_type','rr','value','ttl','priority','status',
          'name','direction','rule_id','protocol','port_from','port_to','cidr',
          'rule_priority','policy','url','acknowledge_billable_creation','source_operation_id')
DNS_TYPES = ('A','AAAA','CNAME','MX','NS','TXT')


def canonical_uuid(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:
            raise ValueError()
    except (ValueError,AttributeError):
        raise CatalogError('canonical Alibaba operation UUID required') from None
    return value


def integer(value,low,high):
    if type(value) is not int or not low<=value<=high:
        raise CatalogError('Alibaba integer out of range')
    return value


def parameters(operation,target_id,values):
    if not isinstance(operation,str) or operation not in OPERATIONS:
        raise CatalogError('unsupported Alibaba operation')
    action = operation[7:-3]
    p = {k:values.get(k) for k in FIELDS}
    allowed = set()
    if action.startswith('dns-'):
        domain_identifier(target_id)
        hostname(p['domain'])
        allowed.add('domain')
        if action!='dns-create':
            allowed.add('record_id')
            numeric(p['record_id'])
        if action in {'dns-create','dns-update'}:
            allowed.update(('record_type','rr','value','ttl','priority'))
        if action=='dns-status':
            allowed.add('status')
    elif action.startswith('instance-'):
        identifier(target_id,'i')
        if action=='instance-rename':
            allowed.add('name')
    elif action.startswith('sg-'):
        identifier(target_id,'sg')
        allowed.update(('direction','protocol','port_from','port_to','cidr','rule_priority','policy'))
        if action=='sg-revoke':
            allowed.add('rule_id')
            identifier(p['rule_id'],'sgr')
    elif action.startswith('cdn-'):
        hostname(target_id)
        allowed.add('url')
    elif action=='snapshot-create':
        identifier(target_id,'d')
        allowed.update(('name','acknowledge_billable_creation'))
    elif action=='receipt-adopt':
        allowed.add('source_operation_id')
    if any(v is not None and k not in allowed for k,v in p.items()):
        raise CatalogError('Alibaba parameters do not belong to this action')
    payload = {}
    if action == 'receipt-adopt':
        canonical_uuid(p['source_operation_id'])
    if action in {'dns-create','dns-update'}:
        if p['record_type'] not in DNS_TYPES:
            raise CatalogError('supported DNS types: A AAAA CNAME MX NS TXT')
        rr = matched(p['rr'],r'@|\*|(?:\*\.)?[a-z0-9_](?:[a-z0-9_.-]{0,251}[a-z0-9_])?','DNS RR')
        if '..' in rr or any(len(x)>63 for x in rr.split('.')):
            raise CatalogError('invalid DNS RR')
        if p['record_type'] in {'A','AAAA'}:
            try:
                ip = ipaddress.ip_address(p['value'])
                if ip.version!=(4 if p['record_type']=='A' else 6):
                    raise ValueError()
                value = str(ip)
            except (ValueError,TypeError):
                raise CatalogError('DNS address type mismatch') from None
        elif p['record_type'] == 'TXT':
            value = p['value']
            if (not isinstance(value,str)
                    or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value)
                    or not 1 <= len(value.encode('utf-8')) <= 512):
                raise CatalogError('DNS TXT requires 1..512 UTF-8 bytes without control characters')
        else:
            value = hostname(p['value'])
        payload = {'RR':rr,'Type':p['record_type'],'Value':value,
                   'TTL':integer(p['ttl'],1,86400),'Line':'default'}
        if p['record_type']=='MX':
            payload['Priority'] = integer(p['priority'],1,50)
        elif p['priority'] is not None:
            raise CatalogError('DNS priority only valid for MX')
    if action=='dns-status':
        if p['status'] not in ('Enable','Disable'):
            raise CatalogError('DNS status must be Enable or Disable')
        payload = {'Status':p['status']}
    if action in {'instance-rename','snapshot-create'}:
        name = matched(p['name'],r'[A-Za-z][A-Za-z0-9_.-]{1,127}','resource name')
        if action=='snapshot-create' and name.lower().startswith('auto'):
            raise CatalogError('snapshot name cannot begin with auto')
        payload = {'InstanceName' if action=='instance-rename' else 'SnapshotName':name}
    if action=='snapshot-create' and p['acknowledge_billable_creation'] is not True:
        raise CatalogError('explicit billable snapshot creation acknowledgement required')
    if action.startswith('sg-'):
        if p['direction'] not in ('ingress','egress') or p['protocol'] not in ('tcp','udp') or p['policy'] not in ('accept','drop'):
            raise CatalogError('finite SG direction/protocol/policy required')
        low, high = integer(p['port_from'],1,65535), integer(p['port_to'],1,65535)
        if low>high:
            raise CatalogError('invalid SG port range')
        try:
            net = ipaddress.ip_network(p['cidr'],strict=True)
        except (ValueError,TypeError):
            raise CatalogError('canonical SG CIDR required') from None
        side = 'Source' if p['direction']=='ingress' else 'Dest'
        payload = {'IpProtocol':p['protocol'],'PortRange':f'{low}/{high}',
                   'Priority':str(integer(p['rule_priority'],1,100)), 'Policy':p['policy'],
                   'NicType':'intranet', ('Ipv6' if net.version==6 else '')+side+'CidrIp':str(net)}
    if action.startswith('cdn-'):
        url = matched(p['url'],r'https://[a-z0-9.-]+/[A-Za-z0-9/_.~-]*','public CDN URL')
        if not url.startswith('https://'+target_id+'/') or any(x in {'.','..'} for x in url.split('/')[3:]) or len(url)>2048:
            raise CatalogError('CDN URL must use exact domain and simple public path')
        payload = {'ObjectPath':url}
        if action=='cdn-refresh':
            payload['ObjectType']='File'
    if action == 'receipt-adopt':
        return {'action':action,'source_operation_id':p['source_operation_id']}
    return {'action':action,'domain':p['domain'],'record_id':p['record_id'],
            'rule_id':p['rule_id'],'direction':p['direction'],'payload':payload}


def resource(account,region,target,p):
    # DNS and CDN are account-global: changing CLI region cannot evade their fence.
    if p['action'].startswith('dns-'):
        return f'aliyun:{account}:domain:{p["domain"]}'
    if p['action'].startswith('cdn-'):
        return f'aliyun:{account}:domain:{target}'
    return f'aliyun:{account}:{region}:{target}'


def exact(rows,key,value):
    matches = [r for r in rows if r.get(key)==value]
    if len(matches)!=1:
        raise CatalogError('Alibaba exact target missing or ambiguous')
    return matches[0]


def rule_matches(row,p):
    if row.get('Direction')!=p['direction']:
        return False
    for key,value in p['payload'].items():
        actual = row.get(key)
        if key in {'IpProtocol','Policy','NicType'} and isinstance(actual,str):
            actual=actual.lower()
        if str(actual)!=str(value):
            return False
    # Rules with extra selectors must never be deleted by a reduced tuple.
    return not any(row.get(k) for k in ('SourceGroupId','DestGroupId','SourcePrefixListId','DestPrefixListId',
                                      'PortRangeListId','SourcePortRange')) and not any(
        row.get(k) for k in ('SourceCidrIp','DestCidrIp','Ipv6SourceCidrIp','Ipv6DestCidrIp') if k not in p['payload'])


def snapshot(adapter,region,target,p,*,precondition=True):
    adapter._identity(region)
    action = p['action']
    if action.startswith('dns-'):
        domain = adapter._domain(region,target,p['domain'])
        capability = None
        if action in {'dns-create','dns-update'}:
            capability = adapter._domain_ttl(region,target,p['domain'])
            if precondition and p['payload']['TTL'] < capability['MinTtl']:
                raise CatalogError('DNS TTL below exact domain package minimum')
        rows = adapter._pages('dns-records',region,DomainName=p['domain'])
        if precondition and p['record_id']:
            row = exact(rows,'RecordId',p['record_id'])
            if row.get('Type') not in DNS_TYPES or row.get('Line') != 'default':
                raise CatalogError('DNS unsupported source type or non-default line')
            if action=='dns-update' and row.get('Type')!=p['payload']['Type']:
                raise CatalogError('DNS type conversion or non-default line not supported')
            if row.get('Locked') is True:
                raise CatalogError('DNS record locked')
        if precondition and action in {'dns-create','dns-update'}:
            body = p['payload']
            for row in rows:
                if row.get('RecordId')==p['record_id'] or row.get('RR')!=body['RR']:
                    continue
                if 'CNAME' in {row.get('Type'),body['Type']} or (row.get('Type')==body['Type'] and row.get('Value')==body['Value']):
                    raise CatalogError('DNS conflicting or duplicate record')
        return {'domain':domain,'rows':rows,**({'ttl_capability':capability} if capability else {})}
    if action.startswith('instance-'):
        rows = adapter._pages('instances',region,RegionId=region)
        row = exact(rows,'InstanceId',target)
        if precondition:
            required = {'instance-start':'Stopped','instance-stop':'Running','instance-reboot':'Running'}.get(action)
            if required and row.get('Status')!=required:
                raise CatalogError('Alibaba instance lifecycle precondition failed')
            if action=='instance-rename' and row.get('InstanceName')==p['payload']['InstanceName']:
                raise CatalogError('Alibaba instance already has intended name')
        return {'rows':rows}
    if action.startswith('sg-'):
        groups = adapter._pages('security-groups',region,RegionId=region)
        group = exact(groups,'SecurityGroupId',target)
        if not group.get('VpcId'):
            raise CatalogError('SG writes require a VPC group')
        rules = adapter._rules(region,target)
        instances = adapter._pages('instances',region,RegionId=region)
        interfaces = adapter._pages('interfaces',region,RegionId=region)
        if precondition and action=='sg-revoke' and not rule_matches(exact(rules['rules'],'SecurityGroupRuleId',p['rule_id']),p):
            raise CatalogError('Alibaba exact SG rule ID/tuple mismatch')
        if precondition and action=='sg-authorize' and any(rule_matches(r,p) for r in rules['rules']):
            raise CatalogError('Alibaba SG rule already exists')
        for row in instances + interfaces:
            groups = row.get('SecurityGroupIds',{}).get('SecurityGroupId')
            if not isinstance(groups,list):
                raise CatalogError('complete instance and ENI security-group references required')
            for group_id in groups:
                identifier(group_id,'sg')
        affected = sorted({r['InstanceId'] for r in instances if target in r.get('SecurityGroupIds',{}).get('SecurityGroupId',[])} |
                          {r['InstanceId'] for r in interfaces if r.get('InstanceId') and target in r.get('SecurityGroupIds',{}).get('SecurityGroupId',[])})
        return {'group':group,'metadata':rules['metadata'],'rows':rules['rules'],
                'instances':instances,'interfaces':interfaces,'affected_instances':affected}
    if action=='snapshot-create':
        disks = adapter._pages('disks',region,RegionId=region)
        disk = exact(disks,'DiskId',target)
        if precondition and disk.get('Status') not in {'In_use','Available'}:
            raise CatalogError('Alibaba snapshot disk state unsupported')
        return {'disks':disks,'rows':adapter._pages('snapshots',region,RegionId=region)}
    domain = adapter._cdn_domain(region,target)
    if precondition and domain.get('DomainStatus')!='online':
        raise CatalogError('Alibaba CDN domain is not online')
    return {'domain':domain}


def safe_snapshot(state,p):
    action=p['action']
    result={'fingerprint':digest(state)}
    kind = ('dns-records' if action.startswith('dns-') else 'instances' if action.startswith('instance-') else
            'rules' if action.startswith('sg-') else 'snapshots' if action=='snapshot-create' else None)
    if kind:
        result['rows']=[safe_row(kind,r) for r in state['rows']]
    if action.startswith('sg-'):
        result['affected_instance_ids']=[identifier(v,'i') for v in state['affected_instances']]
        result['shared_security_group_effect']=True
        result['attached_and_detached_enis_bound']=True
    if 'ttl_capability' in state:
        result['ttl_capability'] = state['ttl_capability']
    return result


def postcondition(before,after,target,p,result_id):
    action=p['action']
    if action.startswith('cdn-') or action=='instance-reboot':
        return False
    if action.startswith('instance-'):
        old={r['InstanceId']:r for r in before['rows']}; new={r['InstanceId']:r for r in after['rows']}
        if target not in new or {k:v for k,v in old.items() if k!=target}!={k:v for k,v in new.items() if k!=target}:
            return False
        if action=='instance-rename':
            return (new[target].get('InstanceName')==p['payload']['InstanceName'] and
                    {k:v for k,v in old[target].items() if k!='InstanceName'}=={k:v for k,v in new[target].items() if k!='InstanceName'})
        return new[target].get('Status')==('Running' if action=='instance-start' else 'Stopped')
    if action=='snapshot-create':
        old={r['SnapshotId']:r for r in before['rows']}; new={r['SnapshotId']:r for r in after['rows']}
        return (before['disks']==after['disks'] and result_id not in old and result_id in new
                and {k:v for k,v in new.items() if k!=result_id}==old
                and new[result_id].get('SourceDiskId')==target and new[result_id].get('Status')=='accomplished'
                and new[result_id].get('Progress')=='100%'
                and new[result_id].get('SnapshotName')==p['payload']['SnapshotName'])
    dns=action.startswith('dns-')
    key='RecordId' if dns else 'SecurityGroupRuleId'
    source=p['record_id'] if dns else p['rule_id']
    for k in before:
        if k=='domain' and dns:
            old_domain,new_domain=before[k],after.get(k,{})
            if {x:v for x,v in old_domain.items() if x!='RecordCount'} != {x:v for x,v in new_domain.items() if x!='RecordCount'}:
                return False
            if 'RecordCount' in old_domain or 'RecordCount' in new_domain:
                count=old_domain.get('RecordCount')
                delta=1 if action=='dns-create' else -1 if action=='dns-delete' else 0
                if type(count) is not int or new_domain.get('RecordCount')!=count+delta:
                    return False
        elif k!='rows' and before[k]!=after.get(k):
            return False
    old={r[key]:r for r in before['rows']}; new={r[key]:r for r in after['rows']}
    if action in {'dns-delete','sg-revoke'}:
        return source not in new and new=={k:v for k,v in old.items() if k!=source}
    if action=='sg-authorize':
        extra=set(new)-set(old)
        return len(extra)==1 and all(new.get(k)==v for k,v in old.items()) and rule_matches(new[next(iter(extra))],p)
    rid=result_id if action=='dns-create' else source
    if rid not in new or (action=='dns-create' and rid in old):
        return False
    if {k:v for k,v in new.items() if k!=rid}!={k:v for k,v in old.items() if k!=source}:
        return False
    body=p['payload']
    if any((str(new[rid].get(k)).lower() if k=='Status' else str(new[rid].get(k))) !=
           (str(v).lower() if k=='Status' else str(v)) for k,v in body.items()):
        return False
    return action=='dns-create' or {k:v for k,v in old[source].items() if k not in body}=={k:v for k,v in new[rid].items() if k not in body}


def dispatch_args(region,target,p,operation_id):
    action=p['action']; body=p['payload'].copy()
    if action.startswith('instance-'):
        api={'instance-start':'StartInstance','instance-stop':'StopInstance','instance-reboot':'RebootInstance','instance-rename':'ModifyInstanceAttribute'}[action]
        if action in {'instance-stop','instance-reboot'}:
            body['ForceStop']='false'
        if action=='instance-stop':
            body['StoppedMode']='KeepCharging'
        return 'ecs',api,{'InstanceId':target,**body}
    if action=='snapshot-create':
        return 'ecs','CreateSnapshot',{'DiskId':target,'ClientToken':operation_id,**body}
    if action.startswith('sg-'):
        suffix='Egress' if p['direction']=='egress' else ''
        if action=='sg-revoke':
            return 'ecs','RevokeSecurityGroup'+suffix,{'RegionId':region,'SecurityGroupId':target,'SecurityGroupRuleId.1':p['rule_id']}
        return 'ecs','AuthorizeSecurityGroup'+suffix,{'RegionId':region,'SecurityGroupId':target,**{'Permissions.1.'+k:v for k,v in body.items()}}
    if action.startswith('dns-'):
        api={'dns-create':'AddDomainRecord','dns-update':'UpdateDomainRecord','dns-delete':'DeleteDomainRecord','dns-status':'SetDomainRecordStatus'}[action]
        return 'alidns',api,{('DomainName' if action=='dns-create' else 'RecordId'):(p['domain'] if action=='dns-create' else p['record_id']),**body}
    return 'cdn',('RefreshObjectCaches' if action=='cdn-refresh' else 'PushObjectCache'),body


def fence_check(root,key):
    path=root/'resource-locks'/(digest(key)+'.aliyun-pending.json')
    if path.exists() or path.is_symlink():
        private_file(path)
        try:
            data=json.loads(path.read_text())
            receipt=read_receipt(canonical_uuid(data['operation_id']),OPERATIONS)
            stage=receipt.get('stages',{}).get('dispatch',{})
            result=stage.get('result',{})
            if (data['resource']!=key or receipt['identity'].get('resource')!=key
                    or not ((receipt['status']=='success' and stage.get('status')=='success' and (
                        (result.get('status')=='success' and result.get('verified') is True) or
                        (result.get('status')=='pre-dispatch-failed' and result.get('mutation_invoked') is False
                         and result.get('dispatch_outcome')=='not-dispatched')))
                        or adoption_proven(data,receipt,key))):
                raise ValueError()
        except Exception:
            raise CatalogError('Alibaba resource has unresolved dispatch; new UUID cannot bypass fence') from None
    return path


def accepted_source(receipt):
    stages = receipt.get('stages',{})
    result = stages.get('dispatch',{}).get('result',{})
    if (stages.get('approved-state',{}).get('status') != 'success'
            or stages.get('dispatch',{}).get('status') != 'success'
            or result.get('status') != 'accepted' or result.get('dispatch_outcome') != 'accepted'
            or result.get('mutation_invoked') is not True or result.get('verified') is not False):
        raise CatalogError('Alibaba adoption requires durably retained positive accepted dispatch')
    return stages['approved-state']['result'], result


def adoption_proven(fence, source, key):
    """Only a completed independent approval may release an accepted fence."""
    if not fence.get('adoption_id'):
        return False
    accepted_source(source)
    adopted = read_receipt(canonical_uuid(fence['adoption_id']), OPERATIONS)
    ident = adopted['identity']; stages = adopted.get('stages',{})
    approval = stages.get('approved-state',{})
    local = stages.get('local-adopt',{})
    proof = local.get('result',{})
    basis = approval.get('result',{}).get('basis',{})
    return (adopted['status']=='success' and ident.get('operation')=='aliyun.receipt-adopt.v1'
            and ident.get('resource')==key and ident.get('source_operation_id')==source['operation_id']
            and approval.get('status')=='success' and local.get('status')=='success'
            and digest(basis)==ident.get('expected_state')==proof.get('approval_digest')
            and basis.get('source_receipt_sha256')==digest(source)==proof.get('source_receipt_sha256')
            and proof.get('source_operation_id')==source['operation_id']
            and proof.get('status')=='success' and proof.get('mutation_invoked') is False)


def adoption_source(adapter, region, target, source_id):
    source = read_receipt(canonical_uuid(source_id),OPERATIONS)
    ident = source['identity']
    if (ident.get('account_id'),ident.get('region'),ident.get('target_id'),ident.get('locator')) != (
            adapter.account_id,region,target,adapter._locator()):
        raise CatalogError('Alibaba adoption source account, region, target or locator mismatch')
    preview, result = accepted_source(source)
    basis = preview['basis']; original = preview['target']; action = original['action']
    if (digest(basis)!=ident.get('expected_state') or preview['state_digest']!=ident['expected_state']
            or any(basis.get(k)!=ident.get(k) for k in ('operation','account_id','region','target_id','parameters_digest'))
            or original.get('target_id')!=target or ident['operation']!='aliyun.'+action+'.v1'):
        raise CatalogError('Alibaba adoption original context mismatch')
    if action not in {'instance-start','instance-stop','snapshot-create','cdn-refresh','cdn-preheat'}:
        raise CatalogError('Alibaba adoption unsupported for reboot or this action')
    if result.get('unsupported_task_reference'):
        raise CatalogError('Alibaba original task reference has no supported query contract')
    p = {k:original[k] for k in ('action','domain','record_id','rule_id','direction')}
    p['payload'] = {}
    if ident.get('resource')!=resource(adapter.account_id,region,target,p):
        raise CatalogError('Alibaba adoption source resource mismatch')
    return source, preview, result, p


def adoption_preview(adapter,service_id,region,target,source_id,binding):
    source, original, result, p = adoption_source(adapter,region,target,source_id)
    state = snapshot(adapter,region,target,p,precondition=False)
    action = p['action']
    proof = {'attribution_verified':False}
    if action in {'instance-start','instance-stop'}:
        desired = 'Running' if action=='instance-start' else 'Stopped'
        row = exact(state['rows'],'InstanceId',target)
        if row.get('Status')!=desired:
            raise CatalogError('Alibaba accepted instance has not reached desired terminal state')
        proof.update(kind='observed-state',state=desired)
    elif action=='snapshot-create':
        sid = identifier(result.get('result_id'),'s')
        row = exact(state['rows'],'SnapshotId',sid)
        exact(state['disks'],'DiskId',target)
        if (row.get('SourceDiskId')!=target or row.get('Status')!='accomplished' or row.get('Progress')!='100%'
                or digest({'SnapshotName':row.get('SnapshotName')})!=original['target']['desired_digest']):
            raise CatalogError('Alibaba captured snapshot lacks exact disk/name/completion proof')
        proof.update(kind='snapshot-completed',snapshot_id=sid,source_disk_id=target)
    else:
        task = numeric(result.get('result_id'))
        kind = 'file' if action=='cdn-refresh' else 'preload'
        rows = adapter._pages('cdn-tasks',region,DomainName=target,TaskId=task,ObjectType=kind)
        if len(rows)!=1:
            raise CatalogError('Alibaba exact captured CDN task missing or ambiguous')
        row = rows[0]
        payload = {'ObjectPath':row.get('ObjectPath'),**({'ObjectType':'File'} if kind=='file' else {})}
        if (row.get('TaskId')!=task or row.get('ObjectType')!=kind or row.get('Status')!='Complete'
                or row.get('Process')!='100%' or digest(payload)!=original['target']['desired_digest']):
            raise CatalogError('Alibaba captured CDN task lacks exact URL/action/completion proof')
        state['tasks'] = rows
        proof.update(kind='cdn-task-completed',task_id=task,object_type=kind)
    basis = {'service':service_id,'operation':'aliyun.receipt-adopt.v1','account_id':adapter.account_id,
             'region':region,'target_id':target,'source_operation_id':source_id,
             'source_receipt_sha256':digest(source),'resource':source['identity']['resource'],
             'binding':binding,'current':safe_snapshot(state,p),'proof':proof}
    return {'status':'planned','basis':basis,'state_digest':digest(basis),'mutation_invoked':False,
            'effects':{'local_receipt_only':True,'automatic_retry':False,'original_stages_preserved':True}}


class AliyunManagementOperations:
    provider='aliyun'
    definitions=tuple(OperationDefinition(op,'plan_service_operation','apply_service_operation',
                                           'get_service_operation') for op in sorted(OPERATIONS))

    def __init__(self,catalog):
        self.catalog=catalog

    def adapter(self,service_id):
        service=self.catalog.get(service_id)
        if service.provider!='aliyun' or service.adapter!='aliyun-management' or not service.enabled or service.kind!='python-adapter':
            raise CatalogError('service does not support Alibaba management')
        adapter=self.catalog.adapter(service)
        if not isinstance(adapter,AliyunManagementAdapter):
            raise CatalogError('unexpected Alibaba adapter')
        return adapter

    def _preview(self,adapter,service_id,operation,region,target_id,p,binding):
        before=snapshot(adapter,region,target_id,p)
        basis={'service':service_id,'operation':operation,'account_id':adapter.account_id,'region':region,
               'target_id':target_id,'binding':binding,'parameters_digest':digest(p),'current':safe_snapshot(before,p)}
        return {'status':'planned','basis':basis,'state_digest':digest(basis),'mutation_invoked':False,
                'target':{'action':p['action'],'target_id':target_id,'domain':p['domain'],
                          'record_id':p['record_id'],'rule_id':p['rule_id'],'direction':p['direction'],
                          'desired_digest':digest(p['payload']),
                          'typed_change':(p['payload'] if p['action'].startswith('sg-') else
                                          {k:v for k,v in p['payload'].items() if k in {'RR','Type','TTL','Line','Priority','Status'}})},
                'effects':{'single_resource':True,'external_writers_not_locked':True,
                           'automatic_retry':False,'automatic_rollback':False,
                           'may_interrupt_service':not p['action'].startswith('cdn-'),
                           'shared_sg_instance_ids':before.get('affected_instances',[]),
                           'billable_creation':p['action']=='snapshot-create',
                           'estimated_cost':'unavailable' if p['action']=='snapshot-create' else 'not-estimated',
                           'stopped_mode':'KeepCharging' if p['action']=='instance-stop' else None}},before

    def plan(self,service_id,operation,region,target_id,*,domain=None,record_id=None,record_type=None,
             rr=None,value=None,ttl=None,priority=None,status=None,name=None,direction=None,rule_id=None,
             protocol=None,port_from=None,port_to=None,cidr=None,rule_priority=None,policy=None,url=None,
             acknowledge_billable_creation=None,source_operation_id=None):
        p=parameters(operation,target_id,locals())
        adapter=self.adapter(service_id)
        adapter._region(region)
        bound,binding=adapter._bound()
        if p['action']=='receipt-adopt':
            return adoption_preview(bound,service_id,region,target_id,source_operation_id,binding)
        return self._preview(bound,service_id,operation,region,target_id,p,binding)[0]

    def apply(self,service_id,operation,region,target_id,expected_state,confirm_account_id,confirm_region,
              confirm_target_id,operation_id,*,domain=None,record_id=None,record_type=None,rr=None,value=None,
              ttl=None,priority=None,status=None,name=None,direction=None,rule_id=None,protocol=None,
              port_from=None,port_to=None,cidr=None,rule_priority=None,policy=None,url=None,
              acknowledge_billable_creation=None,source_operation_id=None):
        canonical_uuid(operation_id)
        p=parameters(operation,target_id,locals())
        adapter=self.adapter(service_id)
        adapter._region(region)
        matched(expected_state,r'[0-9a-f]{64}','preview digest')
        if (confirm_account_id,confirm_region,confirm_target_id)!=(adapter.account_id,region,target_id):
            raise CatalogError('exact Alibaba account, region and resource confirmations required')
        if p['action']=='receipt-adopt':
            return self._adopt(adapter,service_id,region,target_id,source_operation_id,expected_state,operation_id)
        identity={'service':service_id,'operation':operation,'account_id':adapter.account_id,'region':region,
                  'target_id':target_id,'parameters_digest':digest(p),'expected_state':expected_state,
                  'locator':adapter._locator(),'resource':resource(adapter.account_id,region,target_id,p)}
        root=state_root()
        with resource_lock(root,identity['resource']):
            if (root/operation_id).exists() or (root/operation_id).is_symlink():
                receipt=self.receipt(operation_id)
                if receipt['identity']!=identity:
                    raise CatalogError('Alibaba operation identity mismatch')
                result=receipt.get('stages',{}).get('dispatch',{}).get('result',{})
                if receipt['status']=='success' and result.get('status') in {'success','accepted','pre-dispatch-failed'}:
                    return {**result,'operation_id':operation_id,'reused_completed_receipt':True,
                            'live_state_verified_this_invocation':False}
                return {'status':'needs-reconciliation','operation_id':operation_id,'mutation_invoked':False,'replayed':False}
            path=fence_check(root,identity['resource'])
            invoked=False
            try:
                bound,binding=adapter._bound()
                journal=load_journal(adapter.foundation_root/'scripts/foundation_operation.py')
                with journal.Operation(root,operation_id,identity,resume=False) as job:
                    def approve():
                        preview,_=self._preview(bound,service_id,operation,region,target_id,p,binding)
                        if preview['state_digest']!=expected_state:
                            raise CatalogError('Alibaba preview drift; no dispatch')
                        return preview
                    job.stage('approved-state',approve)
                    def dispatch():
                        nonlocal invoked
                        fresh,fresh_binding=adapter._bound()
                        check,before=self._preview(fresh,service_id,operation,region,target_id,p,fresh_binding)
                        if check['state_digest']!=expected_state:
                            raise CatalogError('Alibaba state or binding changed before dispatch')
                        product,api,args=dispatch_args(region,target_id,p,operation_id)
                        try:
                            journal.atomic_json(path,{'operation_id':operation_id,'resource':identity['resource']})
                        except Exception:
                            # replace() may have succeeded before directory fsync failed.
                            # Persist proof through the dispatch stage; never unlink pending.
                            return {'status':'pre-dispatch-failed','verified':False,'mutation_invoked':False,
                                    'dispatch_outcome':'not-dispatched','replayed':False,'resource_fenced':False}
                        invoked=True
                        accepted=False; result_id=None; request_id=None; unsupported_task=False
                        try:
                            response=fresh._call(product,api,region,args)
                            accepted=isinstance(response.get('RequestId'),str) and bool(re.fullmatch(r'[A-Za-z0-9-]{8,128}',response['RequestId']))
                            request_id=response.get('RequestId') if accepted else None
                            unsupported_task=bool(response.get('TaskId')) and p['action'].startswith('instance-')
                            field=('RecordId' if p['action'].startswith('dns-') else 'SnapshotId' if p['action']=='snapshot-create'
                                   else 'RefreshTaskId' if p['action']=='cdn-refresh' else 'PushTaskId' if p['action']=='cdn-preheat' else None)
                            if field:
                                result_id=identifier(response.get(field),'s') if field=='SnapshotId' else numeric(response.get(field))
                                if p['record_id'] and result_id!=p['record_id']:
                                    accepted=False
                        except PreDispatchError:
                            invoked=False
                            return {'status':'pre-dispatch-failed','verified':False,'mutation_invoked':False,
                                    'dispatch_outcome':'not-dispatched','replayed':False,'resource_fenced':False}
                        except Exception:
                            accepted=False
                        after=None; verified=False; task_readback=None
                        try:
                            after=snapshot(fresh,region,target_id,p,precondition=False)
                            verified=accepted and not unsupported_task and postcondition(before,after,target_id,p,result_id)
                            if accepted and p['action'].startswith('cdn-'):
                                tasks=fresh._pages('cdn-tasks',region,DomainName=target_id,TaskId=result_id,
                                                   ObjectType='file' if p['action']=='cdn-refresh' else 'preload')
                                if not tasks or any(r.get('TaskId')!=result_id or r.get('ObjectPath')!=p['payload']['ObjectPath'] for r in tasks):
                                    raise CatalogError('CDN exact task readback unavailable')
                                task_readback=[safe_row('cdn-tasks',r) for r in tasks]
                        except Exception:
                            pass
                        async_action=p['action'] in {'instance-start','instance-stop','instance-reboot','snapshot-create','cdn-refresh','cdn-preheat'}
                        status='success' if verified else 'accepted' if accepted and async_action else 'needs-reconciliation'
                        return {'status':status,'verified':verified,'mutation_invoked':True,'replayed':False,
                                'dispatch_outcome':'accepted' if accepted else 'unknown','result_id':result_id,
                                'vendor_request_id':request_id,'unsupported_task_reference':unsupported_task,
                                'readback':safe_snapshot(after,p) if after else None,
                                'completion_pending':status=='accepted','resource_fenced':not verified,'task_readback':task_readback,
                                'dns_propagation_verified':False,'attribution_verified':False}
                    result=job.stage('dispatch',dispatch)
                    if result['status'] in {'success','accepted','pre-dispatch-failed'}:
                        job.complete()
                    else:
                        job.value['status']='needs-reconciliation'; job.save()
                    return {**result,'operation_id':operation_id,'reused_completed_receipt':False,
                            'live_state_verified_this_invocation':result['verified']}
            except Exception:
                return {'status':'needs-reconciliation' if invoked else 'pre-dispatch-failed',
                        'operation_id':operation_id,'mutation_invoked':invoked,'replayed':False,
                        'reason':'safe local preflight or journal failure; inspect receipt; no automatic retry'}

    def _adopt(self,adapter,service_id,region,target,source_id,expected_state,operation_id):
        if operation_id==source_id:
            raise CatalogError('Alibaba adoption requires an independent operation UUID')
        source, _, _, _ = adoption_source(adapter,region,target,source_id)
        key = source['identity']['resource']; root = state_root()
        identity = {'service':service_id,'operation':'aliyun.receipt-adopt.v1','account_id':adapter.account_id,
                    'region':region,'target_id':target,'source_operation_id':source_id,
                    'expected_state':expected_state,'locator':adapter._locator(),'resource':key}
        with resource_lock(root,key):
            existing = (root/operation_id).exists() or (root/operation_id).is_symlink()
            if existing:
                previous = self.receipt(operation_id)
                if previous['identity']!=identity:
                    raise CatalogError('Alibaba adoption operation identity mismatch')
                local = previous.get('stages',{}).get('local-adopt',{})
                if previous['status']=='success' and local.get('status')=='success':
                    return {**local['result'],'operation_id':operation_id,'reused_completed_receipt':True,
                            'live_state_verified_this_invocation':False}
            path = root/'resource-locks'/(digest(key)+'.aliyun-pending.json')
            try:
                private_file(path)
                fence = json.loads(path.read_text())
            except Exception:
                raise CatalogError('Alibaba adoption pending fence unavailable') from None
            if fence.get('operation_id')!=source_id or fence.get('resource')!=key:
                raise CatalogError('Alibaba adoption fence belongs to another source or approval')
            if fence.get('adoption_id') not in (None,operation_id):
                prior = self.receipt(fence['adoption_id'])
                if (prior['identity'].get('operation')!='aliyun.receipt-adopt.v1'
                        or prior['identity'].get('source_operation_id')!=source_id
                        or prior['identity'].get('resource')!=key or prior['status']=='success'):
                    raise CatalogError('Alibaba adoption cannot supersede this local receipt')
                # A new explicit approval can supersede an incomplete local-only
                # attempt after drift. Both receipts and the cloud source survive.
            try:
                bound,binding=adapter._bound()
                preview=adoption_preview(bound,service_id,region,target,source_id,binding)
                if preview['state_digest']!=expected_state:
                    raise CatalogError('Alibaba adoption preview drift')
                journal=load_journal(adapter.foundation_root/'scripts/foundation_operation.py')
                with journal.Operation(root,operation_id,identity,resume=existing) as job:
                    job.stage('approved-state',lambda:preview,retry_safe=True)
                    # Recheck even when a local stage was durably saved before an error.
                    fresh,fresh_binding=adapter._bound()
                    check=adoption_preview(fresh,service_id,region,target,source_id,fresh_binding)
                    if check['state_digest']!=expected_state:
                        raise CatalogError('Alibaba adoption state or binding changed before local save')
                    def adopt():
                        journal.atomic_json(path,{'operation_id':source_id,'resource':key,'adoption_id':operation_id})
                        return {'status':'success','mutation_invoked':False,'replayed':False,
                                'source_operation_id':source_id,'source_receipt_sha256':check['basis']['source_receipt_sha256'],
                                'approval_digest':expected_state,'proof':check['basis']['proof'],
                                'original_stages_preserved':True,'resource_fence_resolved':True}
                    result=job.stage('local-adopt',adopt,retry_safe=True)
                    job.complete()
                    return {**result,'operation_id':operation_id,'reused_completed_receipt':False,
                            'live_state_verified_this_invocation':True}
            except Exception:
                return {'status':'local-adoption-incomplete','operation_id':operation_id,'mutation_invoked':False,
                        'replayed':False,'resource_fence_resolved':False,
                        'reason':'inspect local receipt; explicitly retry same approval only if evidence still matches'}

    def receipt(self,operation_id):
        canonical_uuid(operation_id)
        if not os.environ.get('STACKORION_OPERATION_ROOT'):
            raise CatalogError('explicit private STACKORION_OPERATION_ROOT required')
        return read_receipt(operation_id,OPERATIONS)


def observe_operation(adapter,operation_id,expected_state,confirm_account_id,confirm_region,confirm_target_id):
    canonical_uuid(operation_id)
    if not os.environ.get('STACKORION_OPERATION_ROOT'):
        raise CatalogError('explicit private STACKORION_OPERATION_ROOT required')
    receipt=read_receipt(operation_id,OPERATIONS)
    identity=receipt['identity']
    if identity.get('operation')=='aliyun.receipt-adopt.v1':
        raise CatalogError('observe the original cloud operation; adoption is local-only')
    if (identity.get('account_id'),identity.get('region'),identity.get('target_id'),identity.get('expected_state'),identity.get('locator')) != (confirm_account_id,confirm_region,confirm_target_id,expected_state,adapter._locator()) or confirm_account_id!=adapter.account_id:
        raise CatalogError('exact original preview, account, region and target confirmation required')
    approved=receipt.get('stages',{}).get('approved-state',{})
    if approved.get('status')!='success':
        raise CatalogError('durable approved preview unavailable')
    preview=approved['result']; target=preview['target']
    p={k:target[k] for k in ('action','domain','record_id','rule_id','direction')}
    p['payload']={}
    bound,_=adapter._bound()
    state=safe_snapshot(snapshot(bound,confirm_region,confirm_target_id,p,precondition=False),p)
    return {'status':'observed','operation_id':operation_id,'approved_state':preview['basis']['current'],
            'observed_state':state,'observation_digest':digest(state),'receipt_changed':False,
            'resource_fence_cleared':False,'mutation_invoked':False,'attribution_verified':False}
