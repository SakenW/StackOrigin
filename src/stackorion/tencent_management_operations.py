"""Exact Tencent intents: durable approval, one dispatch, complete readback.

No automatic write retry or rollback. An uncertain or pending dispatch fences the
whole resource across service aliases and operation UUIDs, including after restart.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import ipaddress
import json
import os
import re
from uuid import UUID

from .catalog import CatalogError
from .operation_runtime import load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition
from .tencent_management import (TencentManagementAdapter, DIGEST, digest, domain_name, enum,
                                 identifier, integer, private_read, safe_row, safe_snapshot, text_value)

ACTIONS = ('cvm-start', 'cvm-stop', 'cvm-reboot', 'lighthouse-start', 'lighthouse-stop', 'lighthouse-reboot',
           'cbs-snapshot-create', 'lighthouse-snapshot-create', 'firewall-add', 'firewall-delete',
           'firewall-description', 'sg-add', 'sg-delete', 'dns-create', 'dns-update', 'dns-delete',
           'edge-purge', 'edge-preheat', 'edge-origin-protocol', 'receipt-adopt')
OPERATIONS = frozenset('tencent.' + action + '.v1' for action in ACTIONS)
PARAMETERS = ('domain', 'record_id', 'record_type', 'name', 'value', 'ttl', 'mx', 'line_id',
              'protocol', 'port', 'cidr', 'rule_action', 'description', 'direction', 'policy_index',
              'owner_instance_ids', 'url', 'origin_protocol', 'http_port', 'https_port',
              'snapshot_name', 'acknowledge_billable_creation', 'source_operation_id')


GLOBAL_REGION = 'global'
DNS_DOMAIN_ID = r'^[1-9][0-9]{0,18}$'
DNS_RECORD_TYPES = ('A', 'AAAA', 'CNAME', 'MX', 'TXT')
DNS_DEFAULT_LINE = '0'
DNS_MAX_TTL = 604800
DNS_MAX_MX = 65535
DNS_RECORD_FIELDS = {
    'domain': {'python_type': 'str', 'description': 'Exact lower-case ASCII domain.'},
    'record_id': {'python_type': 'int', 'minimum': 1, 'maximum': 2**63 - 1},
    'record_type': {'python_type': 'str', 'enum': DNS_RECORD_TYPES},
    'name': {'python_type': 'str', 'description': 'Relative record name or @. Wildcards are unsupported.'},
    'value': {'python_type': 'str', 'description': 'RDATA matching record_type. The preview displays its digest.'},
    'ttl': {'python_type': 'int', 'minimum': 1, 'maximum': DNS_MAX_TTL,
            'description': 'Seconds. The domain package may impose a higher minimum.'},
    'line_id': {'python_type': 'str', 'enum': (DNS_DEFAULT_LINE,)},
    'mx': {'python_type': 'int', 'minimum': 0, 'maximum': DNS_MAX_MX, 'required': False,
           'required_when': {'record_type': 'MX'}, 'description': 'Only accepted for MX records.'},
}
DNS_WRITE_FIELDS = ('record_type', 'name', 'value', 'ttl', 'line_id', 'mx')
DNS_ACTION_FIELDS = {
    'dns-create': ('domain', *DNS_WRITE_FIELDS),
    'dns-update': ('domain', 'record_id', *DNS_WRITE_FIELDS),
    'dns-delete': ('domain', 'record_id'),
}


def _dns_parameter_contract(action):
    region = {'python_type': 'str', 'enum': (GLOBAL_REGION,)}
    domain_id = {'python_type': 'str', 'pattern': DNS_DOMAIN_ID,
                 'description': 'Canonical decimal DomainId in the signed 64-bit positive range.'}
    plan = [
        {'name': 'region', 'required': True, **region},
        {'name': 'resource_id', 'required': True, **domain_id},
        *({'name': field, 'required': True, **DNS_RECORD_FIELDS[field]} for field in DNS_ACTION_FIELDS[action]),
    ]
    apply = [
        *plan,
        {'name': 'expected_state', 'required': True, 'python_type': 'str', 'description': 'Preview state_digest.'},
        {'name': 'confirm_account_id', 'required': True, 'python_type': 'str', 'description': 'Preview basis.account_id.'},
        {'name': 'confirm_region', 'required': True, **region},
        {'name': 'confirm_resource_id', 'required': True, **domain_id},
        {'name': 'operation_id', 'required': True, 'python_type': 'str', 'description': 'Canonical UUID.'},
    ]
    return {
        'plan': {'parameters': plan, 'extra_parameters_validated_by_plugin': False},
        'apply': {'parameters': apply, 'extra_parameters_validated_by_plugin': False},
    }


def _bounded_evidence(value):
    # Keep both approval and readback below the neutral receipt reader's 2 MiB
    # ceiling, including journal indentation and stage metadata.
    if len(json.dumps(value, indent=4, ensure_ascii=True).encode()) > 400_000:
        raise CatalogError('Tencent evidence exceeds bounded receipt capacity')
    return value


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise CatalogError('canonical UUID required') from None
    return value


def resource(account, region, resource_id, action):
    kind = ('dns' if action.startswith('dns-') else 'edge' if action.startswith('edge-') else
            'sg' if action.startswith('sg-') else 'disk' if action.startswith('cbs-') else 'instance')
    return f'tencent:{account}:{region}:{kind}:{resource_id}'


def _params(operation, region, resource_id, values):
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise CatalogError('unsupported fixed Tencent operation')
    action = operation[8:-3]
    if action == 'receipt-adopt':
        canonical_uuid(values['source_operation_id'])
        if any(values[k] is not None for k in PARAMETERS if k != 'source_operation_id'):
            raise CatalogError('receipt adoption accepts only source operation ID')
        return {'action': action, 'region': region, 'resource_id': resource_id, 'source_operation_id': values['source_operation_id']}
    allowed = set()
    if action in DNS_ACTION_FIELDS:
        allowed.update(DNS_ACTION_FIELDS[action])
        for field in DNS_ACTION_FIELDS[action]:
            if DNS_RECORD_FIELDS[field].get('required', True) and values[field] is None:
                raise CatalogError(f'required DNS parameter missing: {field}')
    if action.startswith('firewall-'):
        allowed.update(('protocol', 'port', 'cidr', 'rule_action', 'owner_instance_ids'))
        if action != 'firewall-delete':
            allowed.add('description')
    if action.startswith('sg-'):
        allowed.update(('direction', 'policy_index', 'owner_instance_ids'))
        if action == 'sg-add':
            allowed.update(('protocol', 'port', 'cidr', 'rule_action', 'description'))
    if action.startswith('edge-'):
        allowed.add('domain')
        allowed.update(('origin_protocol', 'http_port', 'https_port') if action == 'edge-origin-protocol' else ('url',))
    if action.endswith('snapshot-create'):
        allowed.update(('snapshot_name', 'acknowledge_billable_creation'))
    if any(values[k] is not None and k not in allowed for k in PARAMETERS):
        raise CatalogError('parameters outside selected Tencent action')
    p = {k: values[k] for k in sorted(allowed)}
    p.update(action=action, region=region, resource_id=resource_id)
    if action.startswith(('dns-', 'edge-')):
        if region != GLOBAL_REGION:
            raise CatalogError('global service requires explicit global region')
        domain_name(p['domain'])
    if action.startswith('dns-'):
        if not isinstance(resource_id, str) or not re.fullmatch(DNS_DOMAIN_ID, resource_id):
            raise CatalogError('DNS resource_id must be canonical decimal DomainId')
        integer(int(resource_id))
        if action != 'dns-create':
            integer(p['record_id'])
        if action != 'dns-delete':
            enum(p['record_type'], DNS_RECORD_TYPES)
            name = p['name']
            if (not isinstance(name, str) or len(name) > 200 or
                    (name != '@' and any(not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?', s) for s in name.split('.')))):
                raise CatalogError('literal DNS owner required; wildcard unsupported')
            text_value(p['value'], 1024)
            if p['record_type'] in {'A', 'AAAA'}:
                try:
                    address = ipaddress.ip_address(p['value'])
                    if address.version != (4 if p['record_type'] == 'A' else 6) or str(address) != p['value']:
                        raise ValueError()
                except ValueError:
                    raise CatalogError('canonical DNS address required') from None
            elif p['record_type'] in {'CNAME', 'MX'}:
                domain_name(p['value'].removesuffix('.'))
            integer(p['ttl'], 1, DNS_MAX_TTL)
            if p['record_type'] == 'MX':
                integer(p['mx'], 0, DNS_MAX_MX)
            elif p['mx'] is not None:
                raise CatalogError('MX priority only allowed for MX record')
            if p['line_id'] != DNS_DEFAULT_LINE:
                raise CatalogError('this slice supports exact default DNS line ID 0 only')
    elif action.startswith('edge-'):
        identifier(resource_id, 'zone')
        if action == 'edge-origin-protocol':
            enum(p['origin_protocol'], {'HTTP', 'HTTPS', 'FOLLOW'})
            for port in ('http_port', 'https_port'):
                if p[port] is not None:
                    integer(p[port], 1, 65535)
            if ((p['http_port'] is not None and p['origin_protocol'] == 'HTTPS') or
                    (p['https_port'] is not None and p['origin_protocol'] == 'HTTP')):
                raise CatalogError('port does not apply to selected origin protocol')
        else:
            url = p['url']
            if (not isinstance(url, str) or len(url) > 2048 or
                    not re.fullmatch(r'https://' + re.escape(p['domain']) + r'/[A-Za-z0-9/_.,~-]*', url)
                    or any(s in {'.', '..'} for s in url.split('/')[3:])):
                raise CatalogError('one exact public HTTPS URL on confirmed domain required')
    else:
        prefix = 'sg' if action.startswith('sg-') else 'disk' if action.startswith('cbs-') else 'ins' if action.startswith('cvm-') else 'lhins'
        identifier(resource_id, prefix)
        if not isinstance(region, str) or not re.fullmatch(r'[a-z][a-z0-9-]{2,63}', region):
            raise CatalogError('explicit regional target required')
    if action.startswith(('firewall-', 'sg-')):
        owners = p['owner_instance_ids']
        if (not isinstance(owners, list) or not owners or len(owners) > 100
                or any(not isinstance(owner, str) for owner in owners) or len(set(owners)) != len(owners)):
            raise CatalogError('explicit owning instance impact references required')
        for owner in owners:
            identifier(owner, 'ins' if action.startswith('sg-') else 'lhins')
        p['owner_instance_ids'] = sorted(owners)
        if action.startswith('sg-'):
            enum(p['direction'], {'Ingress', 'Egress'})
            integer(p['policy_index'], 0, 1000)
        if action != 'sg-delete':
            enum(p['protocol'], {'TCP', 'UDP'})
            integer(p['port'], 1, 65535)
            enum(p['rule_action'], {'ACCEPT', 'DROP'})
            try:
                network = ipaddress.ip_network(p['cidr'], strict=True)
                if network.version != 4 or network.prefixlen == 0 or str(network) != p['cidr']:
                    raise ValueError()
            except (ValueError, TypeError):
                raise CatalogError('canonical bounded IPv4 CIDR required; all-internet rule unsupported') from None
        if 'description' in p:
            text_value(p['description'])
    if action.endswith('snapshot-create'):
        text_value(p['snapshot_name'], 60 if action == 'lighthouse-snapshot-create' else 64)
        if p['acknowledge_billable_creation'] is not True:
            raise CatalogError('snapshot creation is billable; explicit creation acknowledgement required')
    return p


def _rule(p, *, sg=False):
    return {'Protocol': p['protocol'], 'Port': str(p['port']), 'CidrBlock': p['cidr'],
            'Action': p['rule_action'],
            **({('PolicyDescription' if sg else 'FirewallRuleDescription'): p['description']} if 'description' in p else {})}


def _selector(row):
    return {k: row.get(k) for k in ('Protocol', 'Port', 'CidrBlock', 'Action')}


def _dns_values(p):
    return {'Name': p['name'], 'Type': p['record_type'], 'Value': p['value'], 'TTL': p['ttl'],
            'MX': p['mx'] if p['record_type'] == 'MX' else 0, 'LineId': p['line_id']}


def _dns_preview_record(row):
    metadata = safe_row(row)
    result = {key: metadata[key] for key in ('RecordId', 'Name', 'Type', 'TTL', 'LineId', 'Status')
              if key in metadata}
    if isinstance(row.get('Value'), str):
        result['ValueDigest'] = digest(row['Value'])
    if row.get('Type') == 'MX' and type(row.get('MX')) is int and 0 <= row['MX'] <= DNS_MAX_MX:
        result['MX'] = row['MX']
    return result


def _snapshot(adapter, p, *, precondition=True):
    adapter._identity()
    action, region, rid = p['action'], p['region'], p['resource_id']
    if action.startswith('dns-'):
        snap = adapter._dns(int(rid), p['domain'])
        if action != 'dns-delete':
            snap['ttl_policy'] = adapter._dns_ttl_policy(snap['target'])
        if precondition:
            if snap['target'].get('Uin') != adapter.account_id or snap['target'].get('Status') not in ('enable', 'ENABLE'):
                raise CatalogError('DNS domain must be enabled and owned by confirmed main account')
            target = None
            if action != 'dns-create':
                target = adapter._exact(snap['records'], 'RecordId', p['record_id'])
                if target.get('DefaultNS') or target.get('Type') not in DNS_RECORD_TYPES:
                    raise CatalogError('protected or unsupported DNS record')
                if (target.get('LineId') != DNS_DEFAULT_LINE or target.get('Weight') not in {None, 0}
                        or target.get('MonitorStatus') not in {None, ''}):
                    raise CatalogError('nondefault line, weighted or monitored DNS records unsupported')
            if action != 'dns-delete':
                if p['ttl'] < snap['ttl_policy']['minimum']:
                    raise CatalogError('TTL below owned domain package minimum')
                desired = _dns_values(p)
                if target and target.get('Type') != p['record_type']:
                    raise CatalogError('DNS type conversion unsupported')
                if target and all(target.get(k) == v for k, v in desired.items()):
                    raise CatalogError('DNS already has intended values')
                for other in snap['records']:
                    if other is target or other.get('Name') != p['name']:
                        continue
                    if 'CNAME' in {other.get('Type'), p['record_type']} or other.get('Type') == 'NS':
                        raise CatalogError('DNS owner exclusivity conflict')
                    if all(other.get(k) == v for k, v in desired.items() if k != 'TTL'):
                        raise CatalogError('DNS duplicate record')
        return snap
    if action.startswith('edge-'):
        snap = adapter._edge(rid, p['domain'])
        if precondition:
            target = adapter._exact(snap['domains'], 'DomainName', p['domain'])
            if target.get('DomainStatus', target.get('Status')) not in {'online', 'active'}:
                raise CatalogError('EdgeOne domain must be online')
        return snap
    if action.startswith('firewall-'):
        snap = adapter._firewall(region, rid)
        if precondition:
            if p['owner_instance_ids'] != [rid]:
                raise CatalogError('firewall owning instance confirmation mismatch')
            matches = [r for r in snap['rules'] if _selector(r) == _selector(_rule(p))]
            if (action == 'firewall-add' and matches) or (action != 'firewall-add' and len(matches) != 1):
                raise CatalogError('firewall exact selector absent, duplicate or already exists')
            if action == 'firewall-description' and matches[0].get('FirewallRuleDescription') == p['description']:
                raise CatalogError('firewall description unchanged')
        return snap
    if action.startswith('sg-'):
        snap = adapter._security_group(region, rid)
        if precondition:
            owners = sorted(r['InstanceId'] for r in snap['owners'])
            stats = adapter._exact(snap['statistics'].get('SecurityGroupAssociationStatisticsSet', []), 'SecurityGroupId', rid)
            if (owners != p['owner_instance_ids'] or stats.get('TotalCount') != len(owners)
                    or stats.get('CVM') != len(owners) or any(stats.get(k) != 0 for k in ('CDB', 'ENI', 'SG', 'CLB'))
                    or any(r.get('InstanceType') != 'CVM' and r.get('InstanceCount') != 0 for r in stats.get('InstanceStatistics', []))):
                raise CatalogError('SG impacts must resolve completely to confirmed CVM instances only')
            refs = snap['references'].get('ReferredSecurityGroupSet')
            if (not isinstance(refs, list) or len(refs) != 1 or refs[0].get('SecurityGroupId') != rid
                    or refs[0].get('ReferredSecurityGroupIds') != []):
                raise CatalogError('SG reference impact is unsupported or incomplete')
            policies = snap['policies'][p['direction']]
            if action == 'sg-delete':
                adapter._exact(policies, 'PolicyIndex', p['policy_index'])
            else:
                if p['policy_index'] > len(policies) or any(_selector(r) == _selector(_rule(p)) for r in policies):
                    raise CatalogError('SG insert index invalid or duplicate rule')
        return snap
    product = 'cbs' if action.startswith('cbs-') else 'cvm' if action.startswith('cvm-') else 'lighthouse'
    if action == 'cbs-snapshot-create':
        disks = adapter._list(product, region, 'disks')
        target = adapter._exact(disks, 'DiskId', rid)
        snap = {'target': target, 'disks': disks, 'snapshots': adapter._list(product, region, 'snapshots')}
    else:
        instances = adapter._list(product, region, 'instances')
        target = adapter._exact(instances, 'InstanceId', rid)
        snap = {'target': target, 'instances': instances}
        if action.endswith('snapshot-create'):
            snap['snapshots'] = adapter._list(product, region, 'snapshots')
    if precondition:
        verb = action.rsplit('-', 1)[-1]
        if verb in {'start', 'stop', 'reboot'}:
            required = 'STOPPED' if verb == 'start' else 'RUNNING'
            if target.get('InstanceState') != required:
                raise CatalogError('instance state incompatible with requested lifecycle action')
        elif action == 'cbs-snapshot-create' and target.get('DiskState') not in {'ATTACHED', 'UNATTACHED'}:
            raise CatalogError('disk state incompatible with snapshot creation')
        elif action == 'lighthouse-snapshot-create':
            if target.get('InstanceState') not in {'RUNNING', 'STOPPED'} or not isinstance(target.get('SystemDisk', {}).get('DiskId'), str):
                raise CatalogError('instance state or source system disk incompatible with snapshot creation')
    return snap


def _request(p, before):
    action, rid, region = p['action'], p['resource_id'], p['region']
    if action.startswith('dns-'):
        body = {'Domain': p['domain'], 'DomainId': int(rid)}
        if action != 'dns-create':
            body['RecordId'] = p['record_id']
        if action != 'dns-delete':
            body.update(RecordType=p['record_type'], SubDomain=p['name'], Value=p['value'], TTL=p['ttl'],
                        RecordLine='默认', RecordLineId=p['line_id'])
            if p['record_type'] == 'MX':
                body['MX'] = p['mx']
            if action == 'dns-update':
                row = TencentManagementAdapter._exact(before['records'], 'RecordId', p['record_id'])
                body.update(Status=enum(row.get('Status'), {'ENABLE', 'DISABLE'}), Weight=row.get('Weight') or 0)
                # Explicit preservation, not a free-text modification route.
                if isinstance(row.get('Remark'), str):
                    body['Remark'] = row['Remark']
        return 'dnspod', '', {'dns-create': 'CreateRecord', 'dns-update': 'ModifyRecord', 'dns-delete': 'DeleteRecord'}[action], body
    if action.startswith('firewall-'):
        rule = _rule(p)
        if action == 'firewall-delete':
            row = next(r for r in before['rules'] if _selector(r) == _selector(rule))
            rule = {k: v for k, v in row.items() if k in {'Protocol', 'Port', 'CidrBlock', 'Action', 'FirewallRuleDescription'}}
        return 'lighthouse', region, {'firewall-add': 'CreateFirewallRules', 'firewall-delete': 'DeleteFirewallRules',
                                     'firewall-description': 'ModifyFirewallRuleDescription'}[action], {
            'InstanceId': rid, 'FirewallVersion': before['version'],
            **({'FirewallRule': rule} if action == 'firewall-description' else {'FirewallRules': [rule]})}
    if action.startswith('sg-'):
        rule = {'PolicyIndex': p['policy_index']}
        if action == 'sg-add':
            rule.update(_rule(p, sg=True))
        return 'vpc', region, 'CreateSecurityGroupPolicies' if action == 'sg-add' else 'DeleteSecurityGroupPolicies', {
            'SecurityGroupId': rid, 'SecurityGroupPolicySet': {'Version': before['policies']['Version'], p['direction']: [rule]}}
    if action.startswith('edge-'):
        if action == 'edge-origin-protocol':
            body = {'ZoneId': rid, 'DomainName': p['domain'], 'OriginProtocol': p['origin_protocol']}
            for key, wire in (('http_port', 'HttpOriginPort'), ('https_port', 'HttpsOriginPort')):
                if p[key] is not None:
                    body[wire] = p[key]
            return 'teo', '', 'ModifyAccelerationDomain', body
        return 'teo', '', 'CreatePurgeTask' if action == 'edge-purge' else 'CreatePrefetchTask', {
            'ZoneId': rid, 'Targets': [p['url']], **({'Type': 'purge_url'} if action == 'edge-purge' else {'Mode': 'default'})}
    product = action.split('-')[0]
    if action.endswith('snapshot-create'):
        return product, region, 'CreateSnapshot' if product == 'cbs' else 'CreateInstanceSnapshot', {
            ('DiskId' if product == 'cbs' else 'InstanceId'): rid, 'SnapshotName': p['snapshot_name']}
    verb = action.split('-')[1]
    return product, region, {'start': 'StartInstances', 'stop': 'StopInstances', 'reboot': 'RebootInstances'}[verb], {
        'InstanceIds': [rid], **({'StopType': 'SOFT'} if verb != 'start' else {}),
        **({'StoppedMode': 'KEEP_CHARGING'} if product == 'cvm' and verb == 'stop' else {})}


def _without(row, fields):
    return {k: v for k, v in row.items() if k not in fields}


def _counter(rows, ignored=()):
    return Counter(digest(_without(r, ignored)) for r in rows)


def _verified(before, after, p, response):
    action, rid = p['action'], p['resource_id']
    if action.startswith(('dns-', 'firewall-', 'sg-')):
        return _verify_recovery(_recovery_contract(before, p), after, p, response)
    if action.startswith('edge-'):
        if action != 'edge-origin-protocol':
            return False  # Task completion requires separate, exact JobId readback.
        old = TencentManagementAdapter._exact(before['domains'], 'DomainName', p['domain'])
        new = TencentManagementAdapter._exact(after['domains'], 'DomainName', p['domain'])
        body = _request(p, before)[3]
        fields = set(body) - {'ZoneId', 'DomainName'}
        return (all(new.get(k) == body[k] for k in fields)
                and _without(old, fields | {'ModifiedOn'}) == _without(new, fields | {'ModifiedOn'})
                and [r for r in before['domains'] if r is not old] == [r for r in after['domains'] if r is not new]
                and before['target'] == after['target'])
    if action.endswith('snapshot-create'):
        snap_id = response.get('SnapshotId')
        new = [r for r in after['snapshots'] if r.get('SnapshotId') == snap_id]
        return (isinstance(snap_id, str) and len(new) == 1
                and not any(r.get('SnapshotId') == snap_id for r in before['snapshots'])
                and new[0].get('SnapshotState') == 'NORMAL'
                and new[0].get('DiskId') == (rid if action.startswith('cbs-') else before['target'].get('SystemDisk', {}).get('DiskId'))
                and new[0].get('SnapshotName') == p['snapshot_name']
                and _counter(r for r in after['snapshots'] if r is not new[0]) == _counter(before['snapshots'])
                and _without(before['target'], _LIFECYCLE_VOLATILE) == _without(after['target'], _LIFECYCLE_VOLATILE)
                and _snapshot_others(before, p) == _snapshot_others(after, p))
    # A RUNNING reboot readback alone is not evidence of a completed reboot.
    verb = action.rsplit('-', 1)[-1]
    old, new = before['target'], after['target']
    expected_action = {'start': 'StartInstances', 'stop': 'StopInstances', 'reboot': 'RebootInstances'}[verb]
    allowed = {'InstanceState', 'LatestOperation', 'LatestOperationState', 'LatestOperationRequestId', 'LatestOperationStartedTime'}
    return (new.get('InstanceState') == ('STOPPED' if verb == 'stop' else 'RUNNING')
            and new.get('LatestOperation') == expected_action and new.get('LatestOperationState') == 'SUCCESS'
            and new.get('LatestOperationRequestId') == response.get('RequestId')
            and _without(old, allowed) == _without(new, allowed)
            and [r for r in before['instances'] if r.get('InstanceId') != rid]
                == [r for r in after['instances'] if r.get('InstanceId') != rid])


def _fence_path(root, key):
    return root / 'resource-locks' / (digest(key) + '.tencent-pending.json')


def _check_fence(root, key):
    path = _fence_path(root, key)
    if path.exists() or path.is_symlink():
        try:
            value = json.loads(private_read(path, credential=True))
            prior = read_receipt(canonical_uuid(value['operation_id']), OPERATIONS)
            stage = prior.get('stages', {}).get('dispatch', {})
            result = stage.get('result', {})
            completed = (prior.get('status') == 'success' and result.get('status') == 'success'
                         and result.get('verified') is True)
            not_dispatched = (prior.get('status') == 'not-dispatched' and result.get('status') == 'not-dispatched'
                              and result.get('mutation_invoked') is False and result.get('verified') is False
                              and result.get('dispatch_outcome') == 'not-dispatched')
            if (value.get('resource') != key or prior['identity'].get('resource') != key
                    or stage.get('status') != 'success' or not (completed or not_dispatched)):
                raise ValueError()
        except Exception:
            raise CatalogError('earlier Tencent operation unresolved; new UUID cannot bypass resource fence') from None


def _needs(operation_id, invoked=None):
    return {'status': 'needs-reconciliation' if invoked is not False else 'not-dispatched',
            'operation_id': operation_id, 'mutation_invoked': invoked, 'verified': False, 'replayed': False}


class TencentManagementOperations:
    provider = 'tencent'
    definitions = tuple(
        OperationDefinition(
            'tencent.' + action + '.v1', 'plan_service_operation',
            'apply_service_operation', 'get_service_operation',
            parameter_contract=_dns_parameter_contract(action) if action in DNS_ACTION_FIELDS else None,
        )
        for action in sorted(ACTIONS)
    )

    def __init__(self, catalog):
        self.catalog = catalog

    @staticmethod
    def operation_upstream(service_id, operation):
        if operation[8:-3] in DNS_ACTION_FIELDS:
            return TencentManagementAdapter.dns_upstream
        return None

    def adapter(self, service_id):
        service = self.catalog.get(service_id)
        if (service.provider != self.provider or service.adapter != 'tencent-management'
                or not service.enabled or service.kind != 'python-adapter'):
            raise CatalogError('service does not support Tencent management')
        adapter = self.catalog.adapter(service)
        if not isinstance(adapter, TencentManagementAdapter):
            raise CatalogError('unexpected Tencent management adapter')
        return adapter

    @staticmethod
    def _preview(bound, bindings, service_id, operation, p):
        if p['region'] != 'global':
            bound._region(p['region'])
        if p['action'] == 'receipt-adopt':
            return _adoption_preview(bound, bindings, service_id, operation, p)
        before = _snapshot(bound, p)
        _request(p, before)  # Fail local schema/preservation errors before approval.
        basis = {'service': service_id, 'operation': operation, 'account_id': bound.account_id,
                 'region': p['region'], 'resource_id': p['resource_id'], 'binding': bindings,
                 'parameters_digest': digest(p), 'current': safe_snapshot(before)}
        preview = {'status': 'planned', 'state_digest': digest(basis), 'basis': basis,
                   'target': {k: p[k] for k in ('action', 'resource_id', 'region', 'domain', 'record_id',
                             'direction', 'policy_index', 'owner_instance_ids') if k in p},
                   'desired_digest': digest(p), 'mutation_invoked': False,
                   'effects': {'external_writers_not_locked': True, 'automatic_rollback': False,
                               'estimated_cost': 'unavailable',
                               'billable_creation': p['action'].endswith('snapshot-create'),
                               'may_interrupt_service': not p['action'].endswith('snapshot-create'),
                               'stopped_mode': 'KEEP_CHARGING' if p['action'] == 'cvm-stop' else None,
                               'may_increase_origin_traffic': p['action'].startswith('edge-')}}
        if p['action'] in DNS_ACTION_FIELDS:
            preview['upstream'] = bound.dns_upstream
            record_id = p.get('record_id')
            current = (bound._exact(before['records'], 'RecordId', record_id)
                       if record_id is not None else None)
            desired = None
            if p['action'] != 'dns-delete':
                desired = _dns_preview_record({**(current or {}), 'RecordId': record_id, **_dns_values(p)})
            preview['dns_change'] = {
                'before': _dns_preview_record(current) if current is not None else None,
                'after': desired,
            }
            if desired is not None:
                preview['dns_change']['minimum_ttl'] = before['ttl_policy']['minimum']
        preview['recovery_contract'] = _recovery_contract(before, p)
        basis['recovery_contract_digest'] = digest(preview['recovery_contract'])
        basis['target_digest'] = digest(preview['target'])
        preview['state_digest'] = digest(basis)
        return _bounded_evidence(preview), before

    def plan(self, service_id: str, operation: str, region: str, resource_id: str, *,
             domain: str | None = None, record_id: int | None = None, record_type: str | None = None,
             name: str | None = None, value: str | None = None, ttl: int | None = None, mx: int | None = None,
             line_id: str | None = None, protocol: str | None = None, port: int | None = None,
             cidr: str | None = None, rule_action: str | None = None, description: str | None = None,
             direction: str | None = None, policy_index: int | None = None,
             owner_instance_ids: list[str] | None = None, url: str | None = None,
             origin_protocol: str | None = None, http_port: int | None = None, https_port: int | None = None,
             snapshot_name: str | None = None, acknowledge_billable_creation: bool | None = None, source_operation_id: str | None = None):
        p = _params(operation, region, resource_id, locals())
        bound, bindings = self.adapter(service_id)._bound()
        return self._preview(bound, bindings, service_id, operation, p)[0]

    def apply(self, service_id: str, operation: str, region: str, resource_id: str,
              expected_state: str, confirm_account_id: str, confirm_region: str, confirm_resource_id: str,
              operation_id: str, *, domain: str | None = None, record_id: int | None = None,
              record_type: str | None = None, name: str | None = None, value: str | None = None,
              ttl: int | None = None, mx: int | None = None, line_id: str | None = None,
              protocol: str | None = None, port: int | None = None, cidr: str | None = None,
              rule_action: str | None = None, description: str | None = None, direction: str | None = None,
              policy_index: int | None = None, owner_instance_ids: list[str] | None = None,
              url: str | None = None, origin_protocol: str | None = None,
              http_port: int | None = None, https_port: int | None = None,
              snapshot_name: str | None = None, acknowledge_billable_creation: bool | None = None, source_operation_id: str | None = None):
        canonical_uuid(operation_id)
        p = _params(operation, region, resource_id, locals())
        adapter = self.adapter(service_id)
        if (confirm_account_id != adapter.account_id or confirm_region != region or confirm_resource_id != resource_id
                or not isinstance(expected_state, str) or not DIGEST.fullmatch(expected_state)):
            raise CatalogError('exact account/region/resource and preview digest confirmations required')
        identity = {'operation': operation, 'service': service_id, 'account_id': adapter.account_id,
                    'region': region, 'resource_id': resource_id,
                    'resource': resource(adapter.account_id, region, resource_id, p['action']),
                    'parameters_digest': digest(p), 'expected_state': expected_state, 'locator': adapter._locator()}
        if not os.environ.get('STACKORION_OPERATION_ROOT'):
            raise CatalogError('explicit private STACKORION_OPERATION_ROOT required')
        root = state_root()
        if p['action'] == 'receipt-adopt':
            source = _source(adapter, p, service_id)
            identity['resource'] = source['identity']['resource']
            return _apply_adoption(self, adapter, root, identity, p, operation_id, expected_state)
        with resource_lock(root, identity['resource']):
            existing = root / operation_id
            if existing.exists() or existing.is_symlink():
                receipt = self.receipt(operation_id)
                if receipt['identity'] != identity:
                    raise CatalogError('Tencent operation UUID identity mismatch')
                result = receipt.get('stages', {}).get('dispatch', {}).get('result', {})
                if receipt.get('status') == 'success' and result.get('status') == 'success' and result.get('verified') is True:
                    return {**result, 'operation_id': operation_id, 'reused_completed_receipt': True,
                            'live_state_verified_this_invocation': False}
                return _needs(operation_id)
            # Check persistent ambiguity before credential reads, snapshots or new approval.
            _check_fence(root, identity['resource'])
            invoked = False
            try:
                bound, bindings = adapter._bound()
                journal = load_journal(adapter.foundation_root / 'scripts/foundation_operation.py')
                # Verify the loaded source still matches its preview binding.
                if hashlib.sha256(private_read(adapter.foundation_root / 'scripts/foundation_operation.py')).hexdigest() != bindings['journal_sha256']:
                    raise CatalogError('Foundation helper changed during load')
                with journal.Operation(root, operation_id, identity, resume=False) as job:
                    captured = {}

                    def approve():
                        preview, before = self._preview(bound, bindings, service_id, operation, p)
                        if preview['state_digest'] != expected_state:
                            raise CatalogError('preview drift')
                        captured['before'] = before
                        return preview

                    job.stage('approved-state', approve)

                    def dispatch():
                        nonlocal invoked
                        fresh, fresh_bindings = adapter._bound()
                        check, before = self._preview(fresh, fresh_bindings, service_id, operation, p)
                        if check['state_digest'] != expected_state or before != captured['before']:
                            raise CatalogError('state or credential/helper drift before dispatch')
                        prepared = fresh._prepare(*_request(p, before))
                        # replace() may succeed before atomic_json raises on directory fsync.
                        # Only a durable dispatch-stage proof can release that fence; never unlink it.
                        try:
                            journal.atomic_json(_fence_path(root, identity['resource']),
                                                {'resource': identity['resource'], 'operation_id': operation_id})
                        except Exception:
                            return {'status': 'not-dispatched', 'mutation_invoked': False, 'verified': False,
                                    'dispatch_outcome': 'not-dispatched', 'replayed': False}
                        invoked = True
                        response, accepted = {}, False
                        try:
                            response = fresh._send(prepared)
                            request_id = response.get('RequestId')
                            accepted = isinstance(request_id, str) and bool(re.fullmatch(r'[a-zA-Z0-9-]{1,100}', request_id))
                            if p['action'].startswith('edge-') and p['action'] != 'edge-origin-protocol':
                                accepted = accepted and not response.get('FailedList') and isinstance(response.get('JobId'), str)
                        except Exception:
                            pass
                        after, verified, task_rows = None, False, None
                        try:
                            after = _snapshot(fresh, p, precondition=False)
                            verified = accepted and _verified(before, after, p, response)
                            if accepted and p['action'] in {'edge-purge', 'edge-preheat'}:
                                task_rows = fresh._tasks(resource_id, 'purge' if p['action'] == 'edge-purge' else 'preheat', response['JobId'])
                                verified = (len(task_rows) == 1 and task_rows[0].get('Target') == p['url']
                                            and task_rows[0].get('Status') == 'success' and after == before)
                        except Exception:
                            verified = False
                        status = 'success' if verified else 'accepted-pending' if accepted else 'needs-reconciliation'
                        safe_response = {}
                        for key in ('RequestId', 'JobId', 'SnapshotId', 'RecordId'):
                            value = response.get(key)
                            if type(value) is int or (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9-]{1,100}', value)):
                                safe_response[key] = value
                        return _bounded_evidence({'status': status, 'mutation_invoked': True, 'verified': verified, 'replayed': False,
                                'dispatch_outcome': 'accepted' if accepted else 'unknown', 'response': safe_response,
                                'readback': safe_snapshot(after) if after else None,
                                'tasks': safe_snapshot(task_rows) if task_rows is not None else None,
                                'dns_propagation_verified': False, 'application_health_verified': False})

                    result = job.stage('dispatch', dispatch)
                    if result['status'] == 'success':
                        job.complete()
                    else:
                        job.value['status'] = result['status']
                        job.save()
                    return {**result, 'operation_id': operation_id, 'reused_completed_receipt': False,
                            'live_state_verified_this_invocation': result['verified']}
            except Exception:
                return _needs(operation_id, invoked)

    def receipt(self, operation_id: str):
        canonical_uuid(operation_id)
        if not os.environ.get('STACKORION_OPERATION_ROOT'):
            raise CatalogError('explicit private STACKORION_OPERATION_ROOT required')
        return read_receipt(operation_id, OPERATIONS)


def observe_operation(adapter, operation_id, expected_state, confirm_account_id, confirm_region, confirm_resource_id):
    canonical_uuid(operation_id)
    if not os.environ.get('STACKORION_OPERATION_ROOT'):
        raise CatalogError('explicit private STACKORION_OPERATION_ROOT required')
    receipt = read_receipt(operation_id, OPERATIONS)
    identity = receipt['identity']
    if (confirm_account_id != adapter.account_id or identity.get('account_id') != adapter.account_id
            or identity.get('locator') != adapter._locator() or identity.get('expected_state') != expected_state
            or confirm_region != identity.get('region') or confirm_resource_id != identity.get('resource_id')):
        raise CatalogError('exact original preview and resource confirmations required for observation')
    preview = receipt.get('stages', {}).get('approved-state', {}).get('result')
    if not isinstance(preview, dict):
        raise CatalogError('no durable approved state available')
    if identity['operation'] == 'tencent.receipt-adopt.v1':
        return {'status': 'observed', 'operation_id': operation_id, 'receipt': receipt,
                'receipt_changed': False, 'mutation_invoked': False, 'live_state_verified': False}
    # Read-only observation can never clear a fence or attribute a mutation.
    bound = adapter._session()
    p = preview['target']
    after = _snapshot(bound, p, precondition=False)
    return {'status': 'observed', 'operation_id': operation_id, 'receipt_status': receipt['status'],
            'approved_state': preview['basis']['current'], 'observed_state': safe_snapshot(after),
            'receipt_changed': False, 'mutation_invoked': False, 'attribution_verified': False}


_LIFECYCLE_VOLATILE = {'InstanceState', 'LatestOperation', 'LatestOperationState', 'LatestOperationRequestId', 'LatestOperationStartedTime'}


def _snapshot_others(snapshot, p):
    collection, key = ('disks', 'DiskId') if p['action'].startswith('cbs-') else ('instances', 'InstanceId')
    # No fields of unrelated resources are changed by our snapshot request.
    return digest(sorted(digest(r) for r in snapshot[collection] if r[key] != p['resource_id']))


def _dns_record_proof(row):
    stable = _without(row, {'UpdatedOn', 'RecordId'})
    # DNSPod returns CNAME RDATA as an absolute name; one terminal root dot
    # is equivalent to the canonical name accepted at our request boundary.
    if stable.get('Type') == 'CNAME' and isinstance(stable.get('Value'), str) and stable['Value'].endswith('.'):
        stable['Value'] = stable['Value'][:-1]
    # Provider nullable defaults; everything else, including unknown fields, is hashed.
    if stable.get('Weight') is None:
        stable['Weight'] = 0
    if stable.get('MX') is None and stable.get('Type') != 'MX':
        stable['MX'] = 0
    return digest(stable)


def _rule_proof(row, *, sg=False):
    stable = _without(row, {'PolicyIndex', 'ModifyTime', 'Priority'} if sg else set())
    # Absent optional alternative selectors are equivalent to empty SDK defaults.
    for key in ('Ipv6CidrBlock', 'SecurityGroupId', 'AddressTemplate', 'ServiceTemplate') if sg else ('Ipv6CidrBlock',):
        if key in stable and stable[key] in (None, '', {}):
            del stable[key]
    return digest(stable)


def _verify_recovery(contract, after, p, response):
    kind, action = contract['kind'], p['action']
    if kind == 'dns':
        domain = _without(after['target'], {'UpdatedOn'})
        domain_matches = digest(domain) == contract['domain_digest']
        # DNSPod changes the domain record count on create/delete. Accept only
        # that exact delta; the complete record set is still checked below.
        if not domain_matches and action in {'dns-create', 'dns-delete'}:
            count = domain.get('RecordCount')
            if type(count) is int:
                before_count = count - (1 if action == 'dns-create' else -1)
                if before_count >= 0:
                    domain_matches = digest({**domain, 'RecordCount': before_count}) == contract['domain_digest']
        if not domain_matches or digest(after.get('ttl_policy')) != contract['policy_digest']:
            return False
        result_id = response.get('RecordId') if action == 'dns-create' else p['record_id']
        if type(result_id) is not int or result_id <= 0:
            return False
        if 'RecordId' in response and (type(response['RecordId']) is not int or response['RecordId'] != result_id):
            return False
        records = {str(r['RecordId']): digest(r) for r in after['records']}
        if action == 'dns-delete':
            return str(result_id) not in records and records == contract['others']
        rows = [r for r in after['records'] if r['RecordId'] == result_id]
        return (str(result_id) not in contract['others'] and len(rows) == 1
                and _dns_record_proof(rows[0]) == contract['record_digest']
                and {k: v for k, v in records.items() if k != str(result_id)} == contract['others'])
    if kind == 'firewall':
        return (digest(after['target']) == contract['target_digest']
                and digest(after['owners']) == contract['owners_digest']
                and after['version'] > contract['version']
                and sorted(_rule_proof(r) for r in after['rules']) == contract['rules'])
    if kind == 'sg':
        return (digest(_without(after['target'], {'UpdateTime'})) == contract['target_digest']
                and digest({k: after[k] for k in ('owners', 'statistics', 'references')}) == contract['context_digest']
                and after['policies']['Version'] != contract['version']
                and {k: [_rule_proof(r, sg=True) for r in sorted(after['policies'][k], key=lambda r: r['PolicyIndex'])]
                     for k in ('Ingress', 'Egress')} == contract['policies'])
    return False


def _recovery_contract(before, p):
    action, rid = p['action'], p['resource_id']
    if action.startswith('dns-'):
        source = p.get('record_id')
        expected = None
        if action != 'dns-delete':
            expected = (copy.deepcopy(TencentManagementAdapter._exact(before['records'], 'RecordId', source))
                        if source is not None else {'Status': 'ENABLE', 'Line': '默认', 'Weight': 0,
                                                   'MonitorStatus': '', 'Remark': '', 'DefaultNS': False})
            expected.update(_dns_values(p))
        return {'kind': 'dns', 'domain_digest': digest(_without(before['target'], {'UpdatedOn'})),
                'policy_digest': digest(before.get('ttl_policy')),
                'others': {str(r['RecordId']): digest(r) for r in before['records'] if r['RecordId'] != source},
                'record_digest': _dns_record_proof(expected) if expected is not None else None}
    if action.startswith('firewall-'):
        expected = copy.deepcopy(before['rules'])
        desired = _rule(p)
        if action == 'firewall-add':
            expected.append(desired)
        elif action == 'firewall-delete':
            expected = [r for r in expected if _selector(r) != _selector(desired)]
        else:
            next(r for r in expected if _selector(r) == _selector(desired))['FirewallRuleDescription'] = p['description']
        return {'kind': 'firewall', 'target_digest': digest(before['target']),
                'owners_digest': digest(before['owners']), 'version': before['version'],
                'rules': sorted(_rule_proof(r) for r in expected)}
    if action.startswith('sg-'):
        expected = {k: copy.deepcopy(sorted(before['policies'][k], key=lambda r: r['PolicyIndex']))
                    for k in ('Ingress', 'Egress')}
        if action == 'sg-add':
            expected[p['direction']].insert(p['policy_index'], _rule(p, sg=True))
        else:
            expected[p['direction']] = [r for r in expected[p['direction']] if r['PolicyIndex'] != p['policy_index']]
        return {'kind': 'sg', 'target_digest': digest(_without(before['target'], {'UpdateTime'})),
                'context_digest': digest({k: before[k] for k in ('owners', 'statistics', 'references')}),
                'version': before['policies']['Version'],
                'policies': {k: [_rule_proof(r, sg=True) for r in expected[k]] for k in expected}}
    if action in {'edge-purge', 'edge-preheat'}:
        return {'kind': 'edge-task', 'before_digest': digest(before), 'url_digest': digest(p['url'])}
    if action.endswith('snapshot-create'):
        return {'kind': 'snapshot', 'target_digest': digest(_without(before['target'], _LIFECYCLE_VOLATILE)),
                'snapshots': {r['SnapshotId']: digest(r) for r in before['snapshots']},
                'name_digest': digest(p['snapshot_name']), 'others_digest': _snapshot_others(before, p),
                'disk_id': rid if action.startswith('cbs-') else before['target'].get('SystemDisk', {}).get('DiskId')}
    if action.rsplit('-', 1)[-1] in {'start', 'stop', 'reboot'}:
        return {'kind': 'lifecycle', 'target_digest': digest(_without(before['target'], _LIFECYCLE_VOLATILE)),
                'others_digest': digest([r for r in before['instances'] if r['InstanceId'] != rid])}
    return None


def _source(adapter, p, service_id):
    if not os.environ.get('STACKORION_OPERATION_ROOT'):
        raise CatalogError('explicit private STACKORION_OPERATION_ROOT required')
    source = read_receipt(canonical_uuid(p['source_operation_id']), OPERATIONS)
    identity = source['identity']
    approved = source.get('stages', {}).get('approved-state', {}).get('result', {})
    dispatch = source.get('stages', {}).get('dispatch', {}).get('result', {})
    if (identity.get('account_id') != adapter.account_id or identity.get('locator') != adapter._locator()
            or identity.get('region') != p['region'] or identity.get('resource_id') != p['resource_id']
            or identity.get('service') != service_id
            or source.get('status') not in {'accepted-pending', 'running', 'failed', 'needs-reconciliation'}
            or source.get('stages', {}).get('dispatch', {}).get('status') != 'success'
            or source.get('stages', {}).get('approved-state', {}).get('status') != 'success'
            or dispatch.get('dispatch_outcome') != 'accepted' or dispatch.get('mutation_invoked') is not True
            or not isinstance(approved.get('recovery_contract'), dict)):
        raise CatalogError('only durably accepted finite receipts in exact service context can be adopted')
    basis = approved.get('basis', {})
    target = approved.get('target', {})
    if (identity.get('expected_state') != digest(basis) or approved.get('state_digest') != digest(basis)
            or basis.get('recovery_contract_digest') != digest(approved['recovery_contract'])
            or basis.get('target_digest') != digest(target)
            or identity.get('parameters_digest') != approved.get('desired_digest')
            or identity.get('parameters_digest') != basis.get('parameters_digest')
            or any(basis.get(k) != identity.get(k) for k in ('service', 'operation', 'account_id', 'region', 'resource_id'))
            or target.get('region') != p['region'] or target.get('resource_id') != p['resource_id']
            or identity.get('operation') != 'tencent.' + target.get('action', '') + '.v1'
            or identity.get('resource') != resource(adapter.account_id, p['region'], p['resource_id'], target.get('action', ''))):
        raise CatalogError('source approval binding is incomplete or mismatched; fence preserved')
    return source


def _adoption_preview(adapter, bindings, service_id, operation, p):
    source = _source(adapter, p, service_id)
    approved = source['stages']['approved-state']['result']
    dispatch = source['stages']['dispatch']['result']
    params, contract = approved['target'], approved['recovery_contract']
    after = _snapshot(adapter, params, precondition=False)
    response, rid, action = dispatch['response'], p['resource_id'], params['action']
    tasks, verified = None, False
    if contract['kind'] in {'dns', 'firewall', 'sg'}:
        verified = _verify_recovery(contract, after, params, response)
    elif contract['kind'] == 'lifecycle':
        row = after['target']
        verb = action.rsplit('-', 1)[-1]
        verified = (row.get('InstanceState') == ('STOPPED' if verb == 'stop' else 'RUNNING')
                    and row.get('LatestOperation') == {'start': 'StartInstances', 'stop': 'StopInstances', 'reboot': 'RebootInstances'}[verb]
                    and row.get('LatestOperationState') == 'SUCCESS'
                    and row.get('LatestOperationRequestId') == response.get('RequestId')
                    and digest(_without(row, _LIFECYCLE_VOLATILE)) == contract['target_digest']
                    and digest([r for r in after['instances'] if r['InstanceId'] != rid]) == contract['others_digest'])
    elif contract['kind'] == 'snapshot':
        sid = response.get('SnapshotId')
        rows = [r for r in after['snapshots'] if r.get('SnapshotId') == sid]
        verified = (isinstance(sid, str) and sid not in contract['snapshots'] and len(rows) == 1
                    and rows[0].get('SnapshotState') == 'NORMAL'
                    and rows[0].get('DiskId') == contract['disk_id'] and isinstance(contract['disk_id'], str)
                    and digest(rows[0].get('SnapshotName')) == contract['name_digest']
                    and digest(_without(after['target'], _LIFECYCLE_VOLATILE)) == contract['target_digest']
                    and _snapshot_others(after, params) == contract.get('others_digest')
                    and {r['SnapshotId']: digest(r) for r in after['snapshots'] if r.get('SnapshotId') != sid} == contract['snapshots'])
    elif contract['kind'] == 'edge-task':
        tasks = adapter._tasks(rid, 'purge' if action == 'edge-purge' else 'preheat', response.get('JobId'))
        verified = (len(tasks) == 1 and tasks[0].get('Status') == 'success'
                    and digest(tasks[0].get('Target')) == contract['url_digest'] and digest(after) == contract['before_digest'])
    if not verified:
        raise CatalogError('accepted operation has no matching complete readback; fence preserved')
    basis = {'operation': operation, 'service': service_id, 'account_id': adapter.account_id,
             'region': p['region'], 'resource_id': rid, 'source_operation_id': p['source_operation_id'],
             'source_receipt_digest': digest(source), 'binding': bindings, 'current': safe_snapshot(after),
             'tasks': safe_snapshot(tasks) if tasks is not None else None}
    return _bounded_evidence({'status': 'planned', 'state_digest': digest(basis), 'basis': basis, 'target': p,
            'mutation_invoked': False, 'local_receipt_adoption_only': True,
            'source_evidence_preserved': True, 'verified_readback': True}), after


def _adoption_fence(root, identity, p, operation_id, journal):
    path = _fence_path(root, identity['resource'])
    value = json.loads(private_read(path, credential=True))
    if value.get('resource') != identity['resource']:
        raise CatalogError('resource fence identity mismatch')
    if value.get('operation_id') not in {p['source_operation_id'], operation_id}:
        prior = read_receipt(canonical_uuid(value.get('operation_id')), OPERATIONS)
        prior_target = prior.get('stages', {}).get('approved-state', {}).get('result', {}).get('target', {})
        if (prior['identity'].get('operation') != 'tencent.receipt-adopt.v1'
                or prior['identity'].get('resource') != identity['resource']
                or prior_target.get('source_operation_id') != p['source_operation_id']):
            raise CatalogError('resource fence no longer points to adoption source')
    journal.atomic_json(path, {'resource': identity['resource'], 'operation_id': operation_id})


def _apply_adoption(plugin, adapter, root, identity, p, operation_id, expected_state):
    if operation_id == p['source_operation_id']:
        raise CatalogError('adoption requires a new UUID; source evidence is immutable')
    with resource_lock(root, identity['resource']):
        existing = root / operation_id
        if existing.exists() or existing.is_symlink():
            receipt = plugin.receipt(operation_id)
            if receipt['identity'] != identity:
                raise CatalogError('receipt adoption UUID identity mismatch')
            result = receipt.get('stages', {}).get('dispatch', {}).get('result', {})
            if receipt.get('status') == 'success' and result.get('verified') is True:
                # Completed result is available without credentials/helper/cloud checks.
                return {**result, 'operation_id': operation_id, 'reused_completed_receipt': True,
                        'live_state_verified_this_invocation': False}
            return _needs(operation_id, False)
        try:
            bound, bindings = adapter._bound()
            journal = load_journal(adapter.foundation_root / 'scripts/foundation_operation.py')
            with journal.Operation(root, operation_id, identity, resume=False) as job:
                def approve():
                    preview, _ = _adoption_preview(bound, bindings, identity['service'], identity['operation'], p)
                    if preview['state_digest'] != expected_state:
                        raise CatalogError('adoption readback or source receipt drift')
                    return preview
                job.stage('approved-state', approve)

                def adopt():
                    fresh, new_bindings = adapter._bound()
                    preview, _ = _adoption_preview(fresh, new_bindings, identity['service'], identity['operation'], p)
                    if preview['state_digest'] != expected_state:
                        raise CatalogError('adoption state drift after durable approval')
                    # Point at this still-incomplete local receipt before its completion.
                    # A crash blocks new writes; it never clears an unknown cloud fence.
                    _adoption_fence(root, identity, p, operation_id, journal)
                    return {'status': 'success', 'verified': True, 'mutation_invoked': False,
                            'local_receipt_adoption_only': True, 'source_operation_id': p['source_operation_id'],
                            'source_evidence_preserved': True, 'readback': preview['basis']['current'], 'replayed': False}
                result = job.stage('dispatch', adopt)
                job.complete()
                return {**result, 'operation_id': operation_id, 'reused_completed_receipt': False,
                        'live_state_verified_this_invocation': True}
        except Exception:
            return _needs(operation_id, False)
