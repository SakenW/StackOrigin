"""Finite Tencent Cloud inventory. Constructors never read files or contact a cloud.

Provider data is untrusted. Only typed public metadata and full-state digests leave
this module; names, descriptions, DNS values and arbitrary settings stay private.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

from tencentcloud.common.common_client import CommonClient
from tencentcloud.common.credential import Credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.retry import NoopRetryer

from .catalog import CatalogError

VERSIONS = {"sts": "2018-08-13", "cvm": "2017-03-12", "lighthouse": "2020-03-24",
            "cbs": "2017-03-12", "vpc": "2017-03-12", "dnspod": "2021-03-23",
            "teo": "2022-09-01", "billing": "2018-07-09", "ssl": "2019-12-05"}
PAGE_SIZE, MAX_PAGES = 100, 100
DIGEST = re.compile(r"[0-9a-f]{64}")
# Transport routing is fixed, independent of logical global resource fences.
GLOBAL_SDK_REGIONS = {('sts', 'GetCallerIdentity'): 'ap-guangzhou'}
SAFE_ERROR_CODES = frozenset({'MissingParameter', 'PermissionDenied', 'UnauthorizedOperation',
    'AuthFailure', 'InvalidParameter', 'InvalidParameterValue', 'FailedOperation',
    'InternalError', 'LimitExceeded', 'RequestLimitExceeded', 'ResourceNotFound',
    'ResourceUnavailable', 'UnsupportedOperation', 'OperationDenied'})


def _backup_time(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?', value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
# Internal allowlist, not a public API bridge. No caller-supplied host/version/action.
CALLS = {
    "sts": {"GetCallerIdentity"},
    "cvm": {"DescribeRegions", "DescribeInstances", "StartInstances", "StopInstances", "RebootInstances"},
    "lighthouse": {"DescribeRegions", "DescribeInstances", "DescribeDisks", "DescribeSnapshots",
                   "StartInstances", "StopInstances", "RebootInstances", "DescribeFirewallRules",
                   "CreateFirewallRules", "DeleteFirewallRules", "ModifyFirewallRuleDescription",
                   "CreateInstanceSnapshot"},
    "cbs": {"DescribeDisks", "DescribeSnapshots", "DescribeAutoSnapshotPolicies", "CreateSnapshot"},
    "vpc": {"DescribeSecurityGroups", "DescribeSecurityGroupPolicies", "DescribeSecurityGroupReferences",
            "DescribeSecurityGroupAssociationStatistics", "CreateSecurityGroupPolicies", "DeleteSecurityGroupPolicies",
            "DescribeVpcs", "DescribeSubnets", "DescribeAddresses"},
    "ssl": {"DescribeCertificates"},
    "dnspod": {"DescribeDomainList", "DescribeDomain", "DescribeDomainPurview", "DescribeRecordList", "CreateRecord", "ModifyRecord", "DeleteRecord"},
    "teo": {"DescribeZones", "DescribeAccelerationDomains", "DescribeZoneSetting", "DescribePurgeTasks",
            "DescribePrefetchTasks", "CreatePurgeTask", "CreatePrefetchTask", "ModifyAccelerationDomain"},
    "billing": {"DescribeAccountBalance", "DescribeBillSummaryByProduct"},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def integer(value, low=1, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        raise CatalogError("bounded integer required")
    return value


def enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise CatalogError("unsupported fixed value")
    return value


def identifier(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r"-[a-z0-9]{4,64}", value):
        raise CatalogError("exact provider resource ID required")
    return value


def domain_name(value):
    if (not isinstance(value, str) or len(value) > 253 or value != value.lower()
            or len(value.split('.')) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s)
                                               for s in value.split('.'))):
        raise CatalogError("canonical ASCII domain required")
    return value


def text_value(value, maximum=64):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CatalogError("bounded text required")
    return value


def private_read(path, *, credential=False):
    """Walk with directory FDs and O_NOFOLLOW, including every ancestor."""
    path = Path(path)
    descriptors = []
    try:
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError()
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(fd)
        for part in path.parts[1:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        parent = os.fstat(fd)
        if credential and (parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700):
            raise ValueError()
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(leaf)
        info = os.fstat(leaf)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_size > (8192 if credential else 2_097_152)
                or (stat.S_IMODE(info.st_mode) != 0o600 if credential else info.st_mode & 0o022)):
            raise ValueError()
        with os.fdopen(leaf, 'rb', closefd=False) as stream:
            raw = stream.read((8192 if credential else 2_097_152) + 1)
        if len(raw) != info.st_size or os.fstat(leaf).st_mtime_ns != info.st_mtime_ns:
            raise ValueError()
        return raw
    except Exception:
        raise CatalogError("Tencent private file unavailable or unsafe") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def credentials(path):
    try:
        values = {}
        for raw in private_read(path, credential=True).decode().splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if key not in {'TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY'}:
                continue
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            if key in values or not re.fullmatch(r'[!-~]{1,512}', value):
                raise ValueError()
            values[key] = value
        if len(values) != 2:
            raise ValueError()
        return values['TENCENTCLOUD_SECRET_ID'], values['TENCENTCLOUD_SECRET_KEY']
    except Exception:
        raise CatalogError("Tencent credentials unavailable or invalid") from None


ID_FIELDS = {'InstanceId': ('ins', 'lhins'), 'DiskId': ('disk', 'lhdisk'),
             'SnapshotId': ('snap', 'lhsnap'), 'SecurityGroupId': ('sg',), 'ZoneId': ('zone',)}
STATE_FIELDS = {'InstanceState', 'DiskState', 'SnapshotState', 'Status', 'LatestOperationState',
                'LatestOperation', 'Type', 'Protocol', 'Action', 'OriginProtocol'}
STATES = set(('PENDING LAUNCH_FAILED RUNNING STOPPED STARTING STOPPING REBOOTING TERMINATING '
              'TERMINATED ERROR NORMAL ROLLBACKING INIT UNATTACHED ATTACHED CREATING CREATED '
              'SUCCESS FAILED OPERATING ENABLE DISABLE active pending deactivated moved online offline '
              'processing success failed timeout canceled finished A AAAA CNAME MX NS TXT CAA SRV '
              'TCP UDP ICMP ICMPv6 ALL ACCEPT DROP HTTP HTTPS FOLLOW full partial '
              'StartInstances StopInstances RebootInstances').split())


def safe_row(row):
    if not isinstance(row, dict):
        raise CatalogError("invalid Tencent resource shape")
    out = {'snapshot_digest': digest(row)}
    for key, value in row.items():
        if key in ID_FIELDS and isinstance(value, str):
            if any(re.fullmatch(p + r'-[a-z0-9]{4,64}', value) for p in ID_FIELDS[key]):
                out[key] = value
        elif key in {'DomainId', 'RecordId', 'PolicyIndex', 'TTL', 'DiskSize', 'Percent', 'Progress', 'HttpOriginPort', 'HttpsOriginPort'}:
            if type(value) is int and 0 <= value <= 2**63 - 1:
                out[key] = value
        elif key in STATE_FIELDS and isinstance(value, str) and value in STATES:
            out[key] = value
        elif key in {'DomainName', 'ZoneName'}:
            try:
                out[key] = domain_name(value)
            except CatalogError:
                pass
        elif key in {'Region', 'Zone'} and isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9-]{2,63}', value):
            out[key] = value
        elif key == 'Port' and isinstance(value, str) and re.fullmatch(r'(?:[0-9,-]{1,64}|ALL|all)', value):
            out[key] = value
        elif key in {'CidrBlock', 'Ipv6CidrBlock'} and isinstance(value, str):
            try:
                out[key] = str(ipaddress.ip_network(value, strict=True))
            except ValueError:
                pass
        elif key == 'LineId' and isinstance(value, str) and re.fullmatch(r'[0-9=]{1,20}', value):
            out[key] = value
        elif key == 'Name' and 'RecordId' in row and isinstance(value, str) and (value == '@' or re.fullmatch(r'[a-z0-9_.*-]{1,200}', value)):
            out[key] = value
        elif key == 'Placement' and isinstance(value, dict):
            out[key] = safe_row({k: v for k, v in value.items() if k in {'Zone'}})
    return out


def safe_snapshot(value):
    # Arbitrary provider fields never leave memory, including settings and task targets.
    if isinstance(value, list):
        return [safe_snapshot(v) for v in value]
    if isinstance(value, dict):
        return {'snapshot_digest': digest(value),
                'resources': {k: safe_snapshot(v) for k, v in value.items() if k in
                              {'instances', 'disks', 'snapshots', 'records', 'domains', 'zones', 'rules', 'owners', 'target', 'policies', 'Ingress', 'Egress'}},
                'metadata': safe_row(value)}
    return {'snapshot_digest': digest(value)}


class TencentManagementAdapter:
    provider = 'tencent'
    dns_upstream = 'official-sdk'
    query_upstreams = {'dns-domains': dns_upstream, 'dns-records': dns_upstream}
    query_methods = {'identity': 'identity', 'regions': 'regions_query', 'instances': 'instances',
                     'disks': 'disks', 'snapshots': 'snapshots', 'security-groups': 'security_groups',
                     'security-group': 'security_group', 'firewall': 'firewall',
                     'dns-domains': 'dns_domains', 'dns-records': 'dns_records',
                     'edge-zones': 'edge_zones', 'edge-domains': 'edge_domains',
                     'edge-settings': 'edge_settings', 'edge-tasks': 'edge_tasks', 'balance': 'balance', 'billing-summary': 'billing_summary',
                     'observe-operation': 'observe_operation', 'vpcs': 'vpcs', 'subnets': 'subnets',
                     'addresses': 'addresses', 'certificates': 'certificates'}

    def __init__(self, settings):
        required = {'credential_file', 'account_id', 'regions', 'foundation_root'}
        if (not isinstance(settings, dict) or not required <= settings.keys()
                or settings.keys() - required - {'backup_inventory', 'billing_anomaly'}
                or any(type(settings.get(key, False)) is not bool for key in ('backup_inventory', 'billing_anomaly'))):
            raise CatalogError('invalid Tencent management settings')
        if not isinstance(settings['account_id'], str) or not re.fullmatch(r'[1-9][0-9]{4,19}', settings['account_id']):
            raise CatalogError('expected main-account UIN required')
        for key in ('credential_file', 'foundation_root'):
            if not isinstance(settings[key], str) or not Path(settings[key]).is_absolute() or '..' in Path(settings[key]).parts:
                raise CatalogError('absolute private credential and Foundation paths required')
        regions = settings['regions']
        if (not isinstance(regions, list) or not 1 <= len(regions) <= 100
                or any(not isinstance(r, str) or not re.fullmatch(r'[a-z][a-z0-9-]{2,63}', r) for r in regions)
                or len(set(regions)) != len(regions)):
            raise CatalogError('unique configured regions required')
        self.account_id = settings['account_id']
        self.regions = frozenset(regions)
        self.credential = Path(settings['credential_file'])
        self.foundation_root = Path(settings['foundation_root'])
        self.backup_inventory = settings.get('backup_inventory', False)
        if self.backup_inventory:
            self.query_methods = {**self.query_methods, 'snapshot-backup-status': 'snapshot_backup_status'}
        self._secrets = None

    def _region(self, region):
        if not isinstance(region, str) or region not in self.regions:
            raise CatalogError('region outside configured scope')
        return region

    def _locator(self):
        return digest([str(self.credential), str(self.foundation_root), sorted(self.regions)])

    def _session(self):
        bound = copy.copy(self)
        bound._secrets = credentials(self.credential)
        bound._identity()
        return bound

    def _bound(self):
        bound = self._session()
        helper = self.foundation_root / 'scripts/foundation_operation.py'
        return bound, {'locator': self._locator(), 'credential_sha256': digest(bound._secrets),
                       'journal_sha256': hashlib.sha256(private_read(helper)).hexdigest()}

    def _prepare(self, product, region, action, body):
        if product not in CALLS or action not in CALLS[product] or self._secrets is None:
            raise CatalogError('unsupported or unbound Tencent request')
        if product in {'cvm', 'lighthouse', 'cbs', 'vpc'} and action != 'DescribeRegions':
            self._region(region)
        elif region != '':
            raise CatalogError('global Tencent request requires empty region')
        try:
            # These VPC APIs declare string paging parameters, unlike EIP/SG.
            body = copy.deepcopy(body)
            if product == 'vpc' and action in {'DescribeVpcs', 'DescribeSubnets', 'DescribeSecurityGroups'}:
                for field in ('Offset', 'Limit'):
                    if field in body:
                        body[field] = str(integer(body[field], 0, PAGE_SIZE * MAX_PAGES))
            json.dumps(body, allow_nan=False)
            profile = ClientProfile(retryer=NoopRetryer(), disable_region_breaker=True)
            profile.httpProfile = HttpProfile(endpoint=product + '.tencentcloudapi.com', reqTimeout=15)
            sdk_region = GLOBAL_SDK_REGIONS.get((product, action), region)
            client = CommonClient(product, VERSIONS[product], Credential(*self._secrets), sdk_region, profile)
            return client, action, copy.deepcopy(body)
        except Exception:
            raise CatalogError('Tencent request preparation failed') from None

    @staticmethod
    def _send(prepared):
        client, action, body = prepared
        code = None
        try:
            envelope = client.call_json(action, body)
            response = envelope.get('Response') if isinstance(envelope, dict) else None
            if isinstance(response, dict) and isinstance(response.get('Error'), dict):
                code = response['Error'].get('Code')
            if not isinstance(response, dict) or 'Error' in response or len(json.dumps(response)) > 2_097_152:
                raise ValueError()
            return response
        except TencentCloudSDKException as exc:
            code = exc.get_code()
        except Exception:
            pass
        # Exact known codes only; never stringify exceptions, URLs or headers.
        suffix = ': ' + code if isinstance(code, str) and code in SAFE_ERROR_CODES else ''
        raise CatalogError('Tencent request failed' + suffix) from None

    def _call(self, product, region, action, body):
        return self._send(self._prepare(product, region, action, body))

    def _identity(self):
        row = self._call('sts', '', 'GetCallerIdentity', {})
        if row.get('AccountId') != self.account_id:
            raise CatalogError('Tencent main-account identity mismatch')
        return {'provider': self.provider, 'account_id': self.account_id, 'identity_verified': True}

    def _pages(self, product, region, action, body, rows_key, id_key=None, total_path=('TotalCount',), version_key=None):
        rows, total, version, seen = [], None, None, set()
        for _ in range(MAX_PAGES):
            response = self._call(product, region, action, {**body, 'Offset': len(rows), 'Limit': PAGE_SIZE})
            count = response
            for key in total_path:
                count = count.get(key) if isinstance(count, dict) else None
            batch = response.get(rows_key)
            if (type(count) is not int or not 0 <= count <= PAGE_SIZE * MAX_PAGES
                    or not isinstance(batch, list) or len(batch) > PAGE_SIZE
                    or any(not isinstance(row, dict) for row in batch)):
                raise CatalogError('invalid or incomplete Tencent pagination')
            if total is not None and count != total:
                raise CatalogError('Tencent inventory changed across pages')
            total = count
            if version_key:
                current = response.get(version_key)
                if type(current) is not int or (version is not None and current != version):
                    raise CatalogError('Tencent firewall changed across pages')
                version = current
            for row in batch:
                key = row.get(id_key) if id_key else digest(row)
                if not isinstance(key, (str, int)) or isinstance(key, bool) or key in seen:
                    raise CatalogError('missing or repeated resource in Tencent pages')
                seen.add(key)
            rows.extend(batch)
            if len(rows) == total:
                return sorted(rows, key=lambda r: str(r[id_key]) if id_key else digest(r)), version
            if not batch or len(rows) > total:
                raise CatalogError('incomplete Tencent inventory')
        raise CatalogError('Tencent pagination limit exceeded')

    def _list(self, product, region, kind):
        enum(product, {'cvm', 'lighthouse'} if kind == 'instances' else {'cbs', 'lighthouse'})
        action, rows, key = {'instances': ('DescribeInstances', 'InstanceSet', 'InstanceId'),
                             'disks': ('DescribeDisks', 'DiskSet', 'DiskId'),
                             'snapshots': ('DescribeSnapshots', 'SnapshotSet', 'SnapshotId')}[kind]
        result = self._pages(product, self._region(region), action, {}, rows, key)[0]
        for row in result:
            zone = row.get('Zone') or (row.get('Placement') or {}).get('Zone')
            if zone is not None and (not isinstance(zone, str) or not zone.startswith(region + '-')):
                raise CatalogError('Tencent returned a resource in a different region')
        return result

    @staticmethod
    def _exact(rows, key, value):
        matches = [r for r in rows if r.get(key) == value]
        if len(matches) != 1:
            raise CatalogError('exact Tencent resource absent or ambiguous')
        return matches[0]

    def _instance(self, product, region, instance_id):
        identifier(instance_id, 'ins' if product == 'cvm' else 'lhins')
        return self._exact(self._list(product, region, 'instances'), 'InstanceId', instance_id)

    def _firewall(self, region, instance_id):
        instance = self._instance('lighthouse', region, instance_id)
        rows, version = self._pages('lighthouse', region, 'DescribeFirewallRules', {'InstanceId': instance_id},
                                    'FirewallRuleSet', version_key='FirewallVersion')
        return {'target': instance, 'rules': rows, 'version': version, 'owners': [{'InstanceId': instance_id}]}

    def _security_group(self, region, group_id):
        identifier(group_id, 'sg')
        groups = self._pages('vpc', region, 'DescribeSecurityGroups', {}, 'SecurityGroupSet', 'SecurityGroupId')[0]
        group = self._exact(groups, 'SecurityGroupId', group_id)
        response = self._call('vpc', region, 'DescribeSecurityGroupPolicies', {'SecurityGroupId': group_id})
        policies = response.get('SecurityGroupPolicySet')
        if (not isinstance(policies, dict) or not isinstance(policies.get('Version'), str)
                or not all(isinstance(policies.get(k), list) for k in ('Ingress', 'Egress'))):
            raise CatalogError('invalid security group policies')
        for direction in ('Ingress', 'Egress'):
            indices = [r.get('PolicyIndex') for r in policies[direction] if isinstance(r, dict)]
            if (len(indices) != len(policies[direction]) or any(type(i) is not int or i < 0 for i in indices)
                    or len(set(indices)) != len(indices)):
                raise CatalogError('invalid security group selectors')
        instances = self._list('cvm', region, 'instances')
        owners = [r for r in instances if group_id in r.get('SecurityGroupIds', [])]
        stats = self._call('vpc', region, 'DescribeSecurityGroupAssociationStatistics', {'SecurityGroupIds': [group_id]})
        refs = self._call('vpc', region, 'DescribeSecurityGroupReferences', {'SecurityGroupIds': [group_id]})
        return {'target': group, 'policies': policies, 'owners': owners,
                'statistics': {k: v for k, v in stats.items() if k != 'RequestId'},
                'references': {k: v for k, v in refs.items() if k != 'RequestId'}}

    def _dns_domains(self):
        return self._pages('dnspod', '', 'DescribeDomainList', {'Type': 'ALL'}, 'DomainList', 'DomainId',
                           ('DomainCountInfo', 'DomainTotal'))[0]

    def _dns(self, domain_id, domain):
        integer(domain_id)
        domain_name(domain)
        row = self._exact(self._dns_domains(), 'DomainId', domain_id)
        if row.get('Name') != domain:
            raise CatalogError('DNS domain ID/name mismatch')
        info = self._call('dnspod', '', 'DescribeDomain', {'Domain': domain, 'DomainId': domain_id}).get('DomainInfo')
        if not isinstance(info, dict) or info.get('DomainId') != domain_id or info.get('Domain') != domain:
            raise CatalogError('DNS domain ownership mismatch')
        rows = self._pages('dnspod', '', 'DescribeRecordList', {'Domain': domain, 'DomainId': domain_id, 'ErrorOnEmpty': 'no'},
                           'RecordList', 'RecordId', ('RecordCountInfo', 'TotalCount'))[0]
        return {'target': info, 'records': rows}

    def _dns_ttl_policy(self, info):
        if info.get('Uin') != self.account_id or info.get('Status') not in ('enable', 'ENABLE'):
            raise CatalogError('DNS TTL policy requires owned enabled domain')
        binding = {'Domain': domain_name(info.get('Domain')), 'DomainId': integer(info.get('DomainId'))}
        rows = self._call('dnspod', '', 'DescribeDomainPurview', binding).get('PurviewList')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CatalogError('DNS package TTL minimum unavailable')
        floors = [r.get('Value') for r in rows if isinstance(r.get('Name'), str)
                  and r['Name'].replace(' ', '') == '记录TTL最低']
        if len(floors) != 1 or not isinstance(floors[0], str) or not re.fullmatch(r'[1-9][0-9]{0,5}', floors[0]):
            raise CatalogError('DNS package TTL minimum unavailable')
        return {**binding, 'account_id': self.account_id, 'minimum': integer(int(floors[0]), 1, 604800),
                'purview_digest': digest(sorted(rows, key=digest))}

    def _zones(self):
        return self._pages('teo', '', 'DescribeZones', {}, 'Zones', 'ZoneId')[0]

    def _edge(self, zone_id, domain=None):
        identifier(zone_id, 'zone')
        zone = self._exact(self._zones(), 'ZoneId', zone_id)
        domains = self._pages('teo', '', 'DescribeAccelerationDomains', {'ZoneId': zone_id},
                              'AccelerationDomains', 'DomainName')[0]
        if domain is not None:
            domain_name(domain)
            self._exact(domains, 'DomainName', domain)
        return {'target': zone, 'domains': domains}

    def _tasks(self, zone_id, task_kind, job_id):
        enum(task_kind, {'purge', 'preheat'})
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', job_id):
            raise CatalogError('exact EdgeOne job ID required')
        rows = self._pages('teo', '', 'DescribePurgeTasks' if task_kind == 'purge' else 'DescribePrefetchTasks',
                           {'ZoneId': zone_id, 'Filters': [{'Name': 'job-id', 'Values': [job_id]}]}, 'Tasks')[0]
        if any(r.get('JobId') != job_id for r in rows):
            raise CatalogError('EdgeOne task identity mismatch')
        return rows

    def _public(self, rows):
        return {'status': 'complete', 'provider': 'tencent', 'account_id': self.account_id,
                'items': [safe_row(r) for r in rows], 'count': len(rows)}

    def identity(self):
        return self._session()._identity()

    def regions_query(self, product: str):
        enum(product, {'cvm', 'lighthouse'})
        bound = self._session()
        rows = bound._call(product, '', 'DescribeRegions', {}).get('RegionSet')
        if not isinstance(rows, list):
            raise CatalogError('invalid Tencent regions')
        return {**self._public(rows), 'configured_regions': sorted(self.regions)}

    def instances(self, product: str, region: str):
        return self._public(self._session()._list(product, region, 'instances'))

    def disks(self, product: str, region: str):
        return self._public(self._session()._list(product, region, 'disks'))

    def snapshots(self, product: str, region: str):
        return self._public(self._session()._list(product, region, 'snapshots'))

    def snapshot_backup_status(self, region: str):
        if not self.backup_inventory:
            raise CatalogError('Tencent snapshot backup inventory is disabled')
        self._region(region)
        bound = self._session()
        policies, _ = bound._pages('cbs', region, 'DescribeAutoSnapshotPolicies', {},
                                    'AutoSnapshotPolicySet', 'AutoSnapshotPolicyId')
        bindings = {}
        status = 'complete'
        for policy in policies:
            policy_id = identifier(policy.get('AutoSnapshotPolicyId'), 'asp')
            disks = policy.get('DiskIdSet')
            active = policy.get('IsActivated')
            if not isinstance(disks, list) or type(active) is not bool:
                raise CatalogError('invalid Tencent snapshot policy')
            policy_state = policy.get('AutoSnapshotPolicyState')
            if active and policy_state not in {'NORMAL', 'ISOLATED'}:
                active = None
                status = 'partial'
            else:
                active = active and policy_state == 'NORMAL'
            next_trigger = _backup_time(policy.get('NextTriggerTime')) if active else None
            if active and disks and next_trigger is None:
                status = 'partial'
            for disk in disks:
                disk_id = identifier(disk, 'disk')
                bindings[policy_id, disk_id] = {
                    'policy_id': policy_id, 'disk_id': disk_id, 'active': active,
                    'next_trigger': next_trigger.isoformat() if next_trigger else None,
                    'snapshot_status': 'not_found',
                }
        if not bindings:
            return {'status': status, 'items': []}
        try:
            snapshots = bound._list('cbs', region, 'snapshots')
        except (CatalogError, OSError):
            for item in bindings.values():
                item['snapshot_status'] = 'unavailable'
            return {'status': 'partial', 'items': list(bindings.values())}
        latest, incomplete = {}, set()
        for snapshot in snapshots:
            key = snapshot.get('AutoSnapshotPolicyId'), snapshot.get('DiskId')
            if (not all(isinstance(part, str) for part in key) or key not in bindings
                    or snapshot.get('CreateType') == 'ManualSnapshot'
                    or snapshot.get('CopyFromRemote') is True or snapshot.get('SnapshotType') == 'SHARED_SNAPSHOT'
                    or snapshot.get('SnapshotState') != 'NORMAL' or snapshot.get('Percent') != 100):
                continue
            captured = _backup_time(snapshot.get('CreateTime'))
            existing = latest.get(key)
            if captured is None or (existing and (existing[0].tzinfo is None) != (captured.tzinfo is None)):
                incomplete.add(key)
            elif existing is None or captured > existing[0]:
                latest[key] = captured, identifier(snapshot.get('SnapshotId'), 'snap')
        for key, item in bindings.items():
            if key in incomplete:
                status = 'partial'
                item['snapshot_status'] = 'unavailable'
            elif key in latest:
                captured, snapshot_id = latest[key]
                item.update(snapshot_status='observed', snapshot_id=snapshot_id, snapshot_created_at=captured.isoformat())
        return {'status': status, 'items': list(bindings.values())}

    def _network_inventory(self, region, action, rows_key, key, prefix):
        self._region(region)
        rows = self._session()._pages('vpc', region, action, {}, rows_key, key)[0]
        items = []
        for row in rows:
            item = {'snapshot_digest': digest(row), key: identifier(row.get(key), prefix)}
            for field, resource_prefix in (('VpcId', 'vpc'), ('SubnetId', 'subnet'), ('InstanceId', 'ins')):
                if row.get(field):
                    item[field] = identifier(row[field], resource_prefix)
            if row.get('Zone'):
                zone = row['Zone']
                if not isinstance(zone, str) or not (zone == region or zone.startswith(region + '-')):
                    raise CatalogError('Tencent network resource region mismatch')
                item['Zone'] = zone
            for field in ('CidrBlock', 'AddressIp'):
                if row.get(field):
                    try:
                        item[field] = str(ipaddress.ip_network(row[field], strict=True) if field == 'CidrBlock'
                                          else ipaddress.ip_address(row[field]))
                    except (ValueError, TypeError):
                        raise CatalogError('invalid Tencent network address') from None
            items.append(item)
        return {'status': 'complete', 'provider': self.provider, 'account_id': self.account_id,
                'region': region, 'items': items, 'count': len(items)}

    def vpcs(self, region: str):
        return self._network_inventory(region, 'DescribeVpcs', 'VpcSet', 'VpcId', 'vpc')

    def subnets(self, region: str):
        return self._network_inventory(region, 'DescribeSubnets', 'SubnetSet', 'SubnetId', 'subnet')

    def addresses(self, region: str):
        return self._network_inventory(region, 'DescribeAddresses', 'AddressSet', 'AddressId', 'eip')

    def certificates(self):
        rows = self._session()._pages('ssl', '', 'DescribeCertificates', {}, 'Certificates', 'CertificateId')[0]
        items = []
        for row in rows:
            certificate_id = row.get('CertificateId')
            if not isinstance(certificate_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', certificate_id):
                raise CatalogError('invalid Tencent certificate ID')
            item = {'CertificateId': certificate_id, 'snapshot_digest': digest(row)}
            for field in ('CertBeginTime', 'CertEndTime'):
                value = row.get(field)
                if value not in (None, ''):
                    try:
                        if not isinstance(value, str) or len(value) > 35:
                            raise ValueError()
                        datetime.fromisoformat(value)
                    except ValueError:
                        raise CatalogError('invalid Tencent certificate date') from None
                    item[field] = value
            if type(row.get('Status')) is int:
                item['Status'] = integer(row['Status'], 0, 100)
            if row.get('Domain'):
                value = row['Domain']
                if not isinstance(value, str):
                    raise CatalogError('invalid Tencent certificate hostname')
                item['Domain'] = ('*.' if value.startswith('*.') else '') + domain_name(value.removeprefix('*.'))
            items.append(item)
        return {'status': 'complete', 'provider': self.provider, 'account_id': self.account_id,
                'items': items, 'count': len(items), 'private_material_excluded': True}

    def security_groups(self, region: str):
        self._region(region)
        return self._public(self._session()._pages('vpc', region, 'DescribeSecurityGroups', {}, 'SecurityGroupSet', 'SecurityGroupId')[0])

    def security_group(self, region: str, security_group_id: str):
        self._region(region)
        return safe_snapshot(self._session()._security_group(region, security_group_id))

    def firewall(self, region: str, instance_id: str):
        self._region(region)
        return safe_snapshot(self._session()._firewall(region, instance_id))

    def dns_domains(self):
        rows = self._session()._dns_domains()
        return self._public([{**r, 'DomainName': r.get('Name')} for r in rows])

    def dns_records(self, domain_id: int, domain: str):
        return self._public(self._session()._dns(domain_id, domain)['records'])

    def edge_zones(self):
        return self._public(self._session()._zones())

    def edge_domains(self, zone_id: str):
        return self._public(self._session()._edge(zone_id)['domains'])

    def edge_settings(self, zone_id: str):
        bound = self._session()
        bound._edge(zone_id)
        response = bound._call('teo', '', 'DescribeZoneSetting', {'ZoneId': zone_id})
        if not isinstance(response.get('ZoneSetting'), dict):
            raise CatalogError('EdgeOne settings unavailable')
        return {'status': 'complete', 'zone_id': zone_id, 'settings_digest': digest({k: v for k, v in response.items() if k != 'RequestId'}),
                'settings_redacted': True}

    def edge_tasks(self, zone_id: str, task_kind: str, job_id: str):
        bound = self._session()
        bound._edge(zone_id)
        return self._public(bound._tasks(zone_id, task_kind, job_id))

    def balance(self):
        response = self._session()._call('billing', '', 'DescribeAccountBalance', {})
        fields = ('Balance', 'RealBalance', 'CashAccountBalance', 'PresentAccountBalance', 'FreezeAmount', 'OweAmount')
        if type(response.get('RealBalance')) is not int:
            raise CatalogError('invalid Tencent balance')
        return {'status': 'complete', 'account_id': self.account_id, 'unit': 'CNY fen',
                'amounts': {k: v for k, v in response.items() if k in fields and type(v) is int}}

    def billing_summary(self, month: str):
        if not isinstance(month, str) or not re.fullmatch(r'20[0-9]{2}-(?:0[1-9]|1[0-2])', month):
            raise CatalogError('exact billing month YYYY-MM required')
        response = self._session()._call('billing', '', 'DescribeBillSummaryByProduct',
                                        {'BeginTime': month, 'EndTime': month, 'PayerUin': self.account_id, 'PayType': 'consume'})
        if type(response.get('Ready')) is not int or response['Ready'] not in {0, 1}:
            raise CatalogError('invalid billing readiness')
        if response['Ready'] == 0:
            return {'status': 'pending', 'account_id': self.account_id, 'month': month, 'ready': False}
        rows = response.get('SummaryOverview')
        if not isinstance(rows, list) or len(rows) > 10000 or not isinstance(response.get('SummaryTotal'), dict):
            raise CatalogError('incomplete billing summary')
        def amounts(row):
            if not isinstance(row, dict):
                raise CatalogError('invalid billing summary item')
            out = {'snapshot_digest': digest(row)}
            if row.get('BillMonth') not in (None, '', month):
                raise CatalogError('unexpected Tencent billing month')
            for key in ('RealTotalCost', 'CashPayAmount', 'IncentivePayAmount', 'VoucherPayAmount', 'TransferPayAmount'):
                value = row.get(key)
                if value is not None:
                    if not isinstance(value, str) or not re.fullmatch(r'-?[0-9]{1,20}(?:[.][0-9]{1,8})?', value):
                        raise CatalogError('invalid billing amount')
                    out[key] = value
            code = row.get('BusinessCode')
            if isinstance(code, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{1,63}', code):
                out['BusinessCode'] = code
            return out
        return {'status': 'complete', 'account_id': self.account_id, 'month': month, 'unit': 'CNY yuan',
                'ready': True, 'summary': amounts(response['SummaryTotal']), 'products': [amounts(r) for r in rows],
                'coverage': 'monthly consumption cost by product; not metered resource usage'}

    def observe_operation(self, operation_id: str, expected_state: str, confirm_account_id: str,
                          confirm_region: str, confirm_resource_id: str):
        from .tencent_management_operations import observe_operation
        return observe_operation(self, operation_id, expected_state, confirm_account_id, confirm_region, confirm_resource_id)
