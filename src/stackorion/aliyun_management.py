"""Finite Alibaba Cloud CLI adapter. Construction never reads credentials or calls cloud."""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time

from .aliyun import _credentials
from .catalog import CatalogError
from .provider_paths import private_runtime_directory, runtime_root

VERSIONS = {'ecs': '2014-05-26', 'alidns': '2015-01-09', 'cdn': '2018-05-10',
            'sts': '2015-04-01', 'bssopenapi': '2017-12-14', 'vpc': '2016-04-28', 'cas': '2020-04-07'}
LIMIT = 2_000_000
MAX_PAGES = 100
# Each internal route has an exact argument vocabulary; never expose this dispatcher.
PAGE = {'PageNumber', 'PageSize'}
ROUTES = {
 ('sts', 'GetCallerIdentity'): set(),
 ('bssopenapi', 'QueryAccountBalance'): set(),
 ('vpc', 'DescribeVpcs'): {'RegionId'} | PAGE,
 ('vpc', 'DescribeVSwitches'): {'RegionId'} | PAGE,
 ('vpc', 'DescribeEipAddresses'): {'RegionId'} | PAGE,
 ('cas', 'ListUserCertificateOrder'): {'OrderType', 'CurrentPage', 'ShowSize'},
 ('ecs', 'DescribeRegions'): set(),
 ('ecs', 'DescribeInstances'): {'RegionId', 'MaxResults', 'NextToken'},
 ('ecs', 'DescribeDisks'): {'RegionId', 'MaxResults', 'NextToken'},
 ('ecs', 'DescribeNetworkInterfaces'): {'RegionId', 'MaxResults', 'NextToken'},
 ('ecs', 'DescribeSnapshots'): {'RegionId', 'MaxResults', 'NextToken'},
 ('ecs', 'DescribeSecurityGroups'): {'RegionId', 'MaxResults', 'NextToken'},
 ('ecs', 'DescribeSecurityGroupAttribute'): {'RegionId', 'SecurityGroupId', 'Direction', 'MaxResults', 'NextToken'},
 ('alidns', 'DescribeDomains'): PAGE,
 ('alidns', 'DescribeDomainInfo'): {'DomainName', 'NeedDetailAttributes'},
 ('alidns', 'DescribeDomainRecords'): {'DomainName'} | PAGE,
 ('cdn', 'DescribeUserDomains'): PAGE,
 ('cdn', 'DescribeRefreshTasks'): {'DomainName', 'TaskId', 'ObjectType'} | PAGE,
 ('cdn', 'DescribeCdnDomainConfigs'): {'DomainName'},
 ('ecs', 'StartInstance'): {'InstanceId'},
 ('ecs', 'StopInstance'): {'InstanceId', 'ForceStop', 'StoppedMode'},
 ('ecs', 'RebootInstance'): {'InstanceId', 'ForceStop'},
 ('ecs', 'ModifyInstanceAttribute'): {'InstanceId', 'InstanceName'},
 ('ecs', 'CreateSnapshot'): {'DiskId', 'SnapshotName', 'ClientToken'},
 ('alidns', 'AddDomainRecord'): {'DomainName', 'RR', 'Type', 'Value', 'TTL', 'Priority', 'Line'},
 ('alidns', 'UpdateDomainRecord'): {'RecordId', 'RR', 'Type', 'Value', 'TTL', 'Priority', 'Line'},
 ('alidns', 'DeleteDomainRecord'): {'RecordId'},
 ('alidns', 'SetDomainRecordStatus'): {'RecordId', 'Status'},
 ('cdn', 'RefreshObjectCaches'): {'ObjectPath', 'ObjectType'},
 ('cdn', 'PushObjectCache'): {'ObjectPath'},
}
for _suffix in ('', 'Egress'):
    ROUTES['ecs', 'RevokeSecurityGroup' + _suffix] = {'RegionId', 'SecurityGroupId', 'SecurityGroupRuleId.1'}
    ROUTES['ecs', 'AuthorizeSecurityGroup' + _suffix] = {'RegionId', 'SecurityGroupId'} | {
        'Permissions.1.' + k for k in ('Policy', 'Priority', 'IpProtocol', 'PortRange', 'NicType',
                                      'SourceCidrIp', 'Ipv6SourceCidrIp', 'DestCidrIp', 'Ipv6DestCidrIp')}
LISTS = {
 'vpcs': ('vpc', 'DescribeVpcs', 'Vpcs', 'Vpc', 'VpcId'),
 'vswitches': ('vpc', 'DescribeVSwitches', 'VSwitches', 'VSwitch', 'VSwitchId'),
 'addresses': ('vpc', 'DescribeEipAddresses', 'EipAddresses', 'EipAddress', 'AllocationId'),
 'instances': ('ecs', 'DescribeInstances', 'Instances', 'Instance', 'InstanceId'),
 'interfaces': ('ecs', 'DescribeNetworkInterfaces', 'NetworkInterfaceSets', 'NetworkInterfaceSet', 'NetworkInterfaceId'),
 'disks': ('ecs', 'DescribeDisks', 'Disks', 'Disk', 'DiskId'),
 'snapshots': ('ecs', 'DescribeSnapshots', 'Snapshots', 'Snapshot', 'SnapshotId'),
 'security-groups': ('ecs', 'DescribeSecurityGroups', 'SecurityGroups', 'SecurityGroup', 'SecurityGroupId'),
 'dns-domains': ('alidns', 'DescribeDomains', 'Domains', 'Domain', 'DomainId'),
 'dns-records': ('alidns', 'DescribeDomainRecords', 'DomainRecords', 'Record', 'RecordId'),
 'cdn-domains': ('cdn', 'DescribeUserDomains', 'Domains', 'PageData', 'DomainName'),
 'cdn-tasks': ('cdn', 'DescribeRefreshTasks', 'Tasks', 'CDNTask', 'TaskId'),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def matched(value, pattern, label='identifier'):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise CatalogError('invalid Alibaba ' + label)
    return value


def numeric(value):
    return matched(value, r'[1-9][0-9]{0,31}')


def domain_identifier(value):
    return matched(value, r'[A-Za-z0-9][A-Za-z0-9-]{0,63}', 'domain ID')


def identifier(value, prefix):
    return matched(value, re.escape(prefix) + r'-[a-zA-Z0-9]{1,64}')


def hostname(value):
    matched(value, r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', 'hostname')
    if '.' not in value or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in value.split('.')):
        raise CatalogError('invalid Alibaba hostname')
    return value


def private_file(path):
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise CatalogError('unsafe Alibaba private path')
    info, parent = path.stat(), path.parent.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700 or info.st_size > 8192):
        raise CatalogError('Alibaba credential requires owner 0600 and parent 0700')


def helper_digest(path, *, executable=False):
    # Homebrew's installed CLI symlink is resolved and its binary fingerprint pinned.
    source = path.resolve(strict=True) if executable else path
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise CatalogError('unsafe Alibaba helper path')
    info = source.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022 or info.st_size > 200_000_000
            or (executable and not os.access(source, os.X_OK))):
        raise CatalogError('unsafe Alibaba helper')
    h = hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class PreDispatchError(CatalogError):
    """The local process was never started; no cloud request could occur."""


def _capture(argv, env, cwd):
    """No shell, no unbounded communicate(), no disk output and no retry."""
    process = None
    try:
        try:
            process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise PreDispatchError('Alibaba CLI process was not started') from None
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                selector.register(pipe, selectors.EVENT_READ)
            buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
            deadline = time.monotonic() + 30
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise CatalogError('Alibaba request timed out; dispatch outcome may be unknown')
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        buffers[key.fileobj].extend(chunk)
                        if sum(map(len, buffers.values())) > LIMIT:
                            raise CatalogError('Alibaba response exceeded limit')
            return process.wait(timeout=max(0.1, deadline-time.monotonic())), bytes(buffers[process.stdout]), bytes(buffers[process.stderr])
    except CatalogError:
        raise
    except Exception:
        raise CatalogError('Alibaba CLI unavailable or request failed') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()


def safe_row(kind, row):
    """Only validated identifiers and fixed enums escape; all free values are hashes."""
    if not isinstance(row, dict):
        raise CatalogError('invalid Alibaba resource row')
    result = {'fingerprint': digest(row)}
    fields = {
        'vpcs': ('VpcId', 'RegionId', 'Status'),
        'vswitches': ('VSwitchId', 'VpcId', 'RegionId', 'Status'),
        'addresses': ('AllocationId', 'RegionId', 'Status'),
        'interfaces': ('NetworkInterfaceId', 'InstanceId'),
        'instances': ('InstanceId', 'RegionId', 'Status'), 'disks': ('DiskId', 'InstanceId', 'RegionId', 'Status'),
        'snapshots': ('SnapshotId', 'SourceDiskId', 'Status'),
        'security-groups': ('SecurityGroupId', 'VpcId'),
        'dns-domains': ('DomainId', 'DomainName'), 'dns-records': ('RecordId', 'DomainName'),
        'cdn-domains': ('DomainName', 'DomainStatus'), 'cdn-tasks': ('TaskId', 'Status'),
        'rules': ('SecurityGroupRuleId', 'Direction'),
    }[kind]
    for key in fields:
        value = row.get(key)
        if value in (None, ''):
            continue
        if key == 'DomainId':
            result[key] = domain_identifier(value)
        elif key in {'RecordId', 'TaskId'}:
            result[key] = numeric(value)
        elif key == 'DomainName':
            result[key] = hostname(value)
        elif key.endswith('Id') and key != 'RegionId':
            prefix = {'InstanceId':'i', 'DiskId':'d', 'SourceDiskId':'d', 'SnapshotId':'s',
                      'SecurityGroupId':'sg', 'SecurityGroupRuleId':'sgr', 'VpcId':'vpc', 'NetworkInterfaceId':'eni',
                      'VSwitchId':'vsw', 'AllocationId':'eip'}[key]
            result[key] = identifier(value, prefix)
        elif key == 'RegionId':
            result[key] = matched(value, r'[a-z][a-z0-9-]{2,63}')
        else:
            allowed = {'Running','Stopped','Starting','Stopping','Rebooting','In_use','Available',
                       'Attaching','Detaching','Creating','Accomplished','Failed','Progressing',
                       'accomplished','progressing','failed','online','offline','configuring',
                       'configure_failed','checking','check_failed','stopping','deleting',
                       'Complete','Refreshing','Failed','ingress','egress', 'Pending', 'InUse',
                       'Associating', 'Unassociating', 'Releasing'}
            result[key] = value if isinstance(value, str) and value in allowed else 'unrecognized'
    if kind in {'vpcs', 'vswitches', 'addresses'}:
        for key in ('CidrBlock', 'IpAddress'):
            if row.get(key):
                try:
                    result[key] = str(ipaddress.ip_network(row[key], strict=True) if key == 'CidrBlock'
                                      else ipaddress.ip_address(row[key]))
                except (TypeError, ValueError):
                    raise CatalogError('invalid Alibaba network address') from None
    if kind == 'dns-records':
        if row.get('Type') in ('A','AAAA','CNAME','MX','NS','TXT','SRV','CAA'):
            result['Type'] = row['Type']
        rr = row.get('RR')
        if isinstance(rr,str) and re.fullmatch(r'@|[A-Za-z0-9_*.-]{1,253}',rr):
            result['RR'] = rr
        if type(row.get('TTL')) is int and 1 <= row['TTL'] <= 86400:
            result['TTL'] = row['TTL']
        status = row.get('Status')
        if isinstance(status,str) and status.upper() in ('ENABLE','DISABLE'):
            result['Status'] = status.upper()
        result['value_digest'] = digest(row.get('Value'))
    if kind == 'rules':
        for key,allowed in (('IpProtocol',('tcp','udp','icmp','icmpv6','gre','all')),
                            ('Policy',('accept','drop')),('NicType',('intranet','internet'))):
            value = row.get(key)
            if isinstance(value,str) and value.lower() in allowed:
                result[key] = value.lower()
        for key in ('SourceCidrIp','DestCidrIp','Ipv6SourceCidrIp','Ipv6DestCidrIp'):
            if row.get(key):
                try:
                    result[key] = str(ipaddress.ip_network(row[key],strict=True))
                except (ValueError,TypeError):
                    raise CatalogError('invalid Alibaba rule CIDR') from None
        for key in ('PortRange','Priority'):
            value = str(row.get(key,''))
            if re.fullmatch(r'-?[0-9]{1,5}/-?[0-9]{1,5}' if key=='PortRange' else r'[0-9]{1,3}',value):
                result[key] = value
    return result


class AliyunManagementAdapter:
    provider = 'aliyun'
    query_methods = {'identity':'identity', 'balance':'balance', 'regions':'available_regions', 'instances':'instances',
                     'disks':'disks', 'snapshots':'snapshots', 'security-groups':'security_groups',
                     'security-group-rules':'security_group_rules', 'dns-domains':'dns_domains',
                     'dns-records':'dns_records', 'cdn-domains':'cdn_domains',
                     'cdn-tasks':'cdn_tasks', 'cdn-config':'cdn_config', 'reconcile':'reconcile',
                     'vpcs':'vpcs', 'vswitches':'vswitches', 'addresses':'addresses', 'certificates':'certificates'}

    def __init__(self, settings):
        if not isinstance(settings, dict) or set(settings) != {'credential_file','account_id','regions','foundation_root','cli_path'}:
            raise CatalogError('invalid Alibaba management settings')
        self.account_id = numeric(settings['account_id'])
        for key in ('credential_file','foundation_root','cli_path'):
            v = settings[key]
            if not isinstance(v, str) or not Path(v).is_absolute() or '..' in Path(v).parts:
                raise CatalogError('absolute Alibaba paths required')
        self.credential = Path(settings['credential_file'])
        self.foundation_root = Path(settings['foundation_root'])
        self.cli = Path(settings['cli_path'])
        regions = settings['regions']
        if not isinstance(regions, list) or not 1 <= len(regions) <= 100:
            raise CatalogError('configured Alibaba regions required')
        for region in regions:
            matched(region, r'[a-z][a-z0-9-]{2,63}', 'region')
        if len(set(regions)) != len(regions):
            raise CatalogError('duplicate Alibaba regions')
        self.regions = tuple(sorted(regions))
        self._auth = None

    def _region(self, region):
        if not isinstance(region, str) or region not in self.regions:
            raise CatalogError('Alibaba region is outside configured scope')
        return region

    def _locator(self):
        return digest([str(self.credential),str(self.foundation_root),str(self.cli),self.regions])

    def _bound(self):
        try:
            private_file(self.credential)
            before = self.credential.stat()
            creds = _credentials(self.credential)
            private_file(self.credential)
            after = self.credential.stat()
            if (before.st_ino,before.st_mtime_ns,before.st_size) != (after.st_ino,after.st_mtime_ns,after.st_size):
                raise ValueError()
            binding = {'locator':self._locator(), 'credential_sha256':digest(creds),
                       'journal_sha256':helper_digest(self.foundation_root / 'scripts/foundation_operation.py'),
                       'cli_sha256':helper_digest(self.cli, executable=True)}
        except Exception:
            raise CatalogError('Alibaba credential/helper binding unavailable or unsafe') from None
        bound = copy.copy(self)
        bound._auth = creds
        return bound, binding

    def _call(self, product, action, region, params):
        self._region(region)
        if (product, action) not in ROUTES or not isinstance(params, dict) or set(params) - ROUTES[product,action]:
            raise PreDispatchError('unsupported Alibaba route or arguments')
        if self._auth is None:
            raise PreDispatchError('Alibaba bound credential required')
        argv = [str(self.cli),product,action,'--version',VERSIONS[product],'--region',region,
                '--retry-count','0','--read-timeout','20','--connect-timeout','10','--secure']
        if product == 'bssopenapi':
            argv.extend(['--endpoint', 'business.aliyuncs.com'])
        for key, value in sorted(params.items()):
            if type(value) not in (str,int) or len(str(value)) > 2048 or any(ord(c)<32 for c in str(value)):
                raise PreDispatchError('invalid Alibaba argument value')
            argv.extend(['--'+key,str(value)])
        capture_entered = False
        try:
            try:
                temporary_root = private_runtime_directory(runtime_root() / "tmp/aliyun")
            except CatalogError as exc:
                raise PreDispatchError(str(exc)) from None
            with tempfile.TemporaryDirectory(dir=temporary_root, prefix='cli-') as tmp:
                env = {'PATH':'/usr/bin:/bin', 'HOME':tmp, 'TMPDIR':tmp, 'LANG':'en_US.UTF-8',
                       'ALIBABA_CLOUD_ACCESS_KEY_ID':self._auth[0],
                       'ALIBABA_CLOUD_ACCESS_KEY_SECRET':self._auth[1],
                       'ALIBABA_CLOUD_IGNORE_PROFILE':'TRUE', 'ALIBABA_CLOUD_RETRY_COUNT':'0',
                       'ALIBABA_CLOUD_CLI_PLUGIN_AUTO_INSTALL':'false'}
                capture_entered = True
                rc, out, err = _capture(argv, env, tmp)
            if rc:
                if b'Forbidden' in err or b'Forbidden' in out or b'AccessDenied' in err:
                    raise CatalogError('Alibaba permission denied ('+product+':'+action+')')
                raise CatalogError('Alibaba vendor request failed ('+product+':'+action+')')
            if len(out)+len(err)>LIMIT:
                raise ValueError()
            data = json.loads(out)
            if not isinstance(data, dict):
                raise ValueError()
            if (product, action) == ('bssopenapi', 'QueryAccountBalance'):
                if data.get('Code') != '200' or data.get('Success') is not True or not isinstance(data.get('Data'), dict):
                    raise CatalogError('Alibaba balance business response failed')
            elif 'Code' in data or 'Message' in data:
                raise ValueError()
            return data
        except CatalogError:
            raise
        except Exception:
            if not capture_entered:
                raise PreDispatchError('Alibaba local request preparation failed; no process started') from None
            raise CatalogError('Alibaba response invalid or transport unavailable') from None

    def _identity(self, region):
        data = self._call('sts','GetCallerIdentity',region,{})
        if data.get('AccountId') != self.account_id or data.get('IdentityType') not in {'Account','RAMUser','AssumedRoleUser'}:
            raise CatalogError('Alibaba STS account identity mismatch')
        return {'account_id':self.account_id,'identity_type':data['IdentityType']}

    def _pages(self, kind, region, **params):
        product, action, outer, inner, key = LISTS[kind]
        rows, total, seen, tokens = [], None, set(), set()
        next_token = None
        token_mode = product == 'ecs'
        for page in range(1, MAX_PAGES+1):
            paging = ({'MaxResults':50, **({'NextToken':next_token} if next_token else {})}
                      if token_mode else {'PageNumber':page,'PageSize':50})
            data = self._call(product,action,region,{**params,**paging})
            chunk = data.get(outer,{}).get(inner) if isinstance(data.get(outer),dict) else None
            if not isinstance(chunk,list) or len(chunk)>50 or data.get('RegionId',region)!=region:
                raise CatalogError('Alibaba invalid page or region mismatch')
            if token_mode:
                if 'TotalCount' in data:
                    count = data['TotalCount']
                    if (type(count) is not int or not 0 <= count <= MAX_PAGES * 50
                            or (total is not None and count != total)):
                        raise CatalogError('Alibaba invalid or unstable token total')
                    total = count
                elif total is not None:
                    raise CatalogError('Alibaba missing token total after declared count')
                if 'NextToken' not in data:
                    raise CatalogError('Alibaba incomplete token pagination')
                next_token = data['NextToken']
                if next_token not in (None,'') and (not isinstance(next_token,str) or
                        len(next_token)>1024 or next_token in tokens or not chunk):
                    raise CatalogError('Alibaba repeated or incomplete token pagination')
                if next_token:
                    tokens.add(next_token)
            else:
                count = data.get('TotalCount')
                if (type(count) is not int or count < 0 or count > 5000
                        or data.get('PageNumber') != page or data.get('PageSize') != 50
                        or (total is not None and count != total)):
                    raise CatalogError('Alibaba incomplete or unstable pagination')
                total = count
            for row in chunk:
                safe_row(kind,row)
                identity = row.get(key)
                # One CDN task may have several objects with the same TaskId.
                if kind == 'cdn-tasks':
                    identity = (row.get('TaskId'),digest(row.get('ObjectPath')))
                if not identity or identity in seen:
                    raise CatalogError('Alibaba duplicate or missing resource identity')
                if kind != 'cdn-tasks' and not isinstance(identity,str):
                    raise CatalogError('Alibaba missing resource identity')
                if row.get('RegionId',region) != region:
                    raise CatalogError('Alibaba resource region mismatch')
                if kind == 'instances' and row.get('RegionId') != region:
                    raise CatalogError('Alibaba instance region missing')
                if kind == 'dns-records' and row.get('DomainName') != params['DomainName']:
                    raise CatalogError('Alibaba DNS domain mismatch')
                seen.add(identity)
                rows.append(row)
            if total is not None and (len(rows) > total or (token_mode and not next_token and len(rows) != total)):
                raise CatalogError('Alibaba incomplete pagination total')
            if (token_mode and not next_token) or (not token_mode and len(rows)==total):
                return sorted(rows,key=lambda r:(r[key],digest(r)))
            if not chunk or (not token_mode and len(rows)>total):
                raise CatalogError('Alibaba incomplete pagination')
        raise CatalogError('Alibaba pagination limit reached; no complete inventory')

    def _rules(self, region, group_id):
        rows, tokens, ids = [], set(), set()
        params = {'RegionId':region,'SecurityGroupId':identifier(group_id,'sg'),'Direction':'all','MaxResults':100}
        meta = None
        for _ in range(MAX_PAGES):
            data = self._call('ecs','DescribeSecurityGroupAttribute',region,params)
            if data.get('SecurityGroupId') != group_id or data.get('RegionId') != region or 'NextToken' not in data:
                raise CatalogError('Alibaba security group identity or pagination mismatch')
            current = {k:v for k,v in data.items() if k not in {'RequestId','NextToken','Permissions'}}
            if meta is not None and meta != current:
                raise CatalogError('Alibaba security group changed during pagination')
            meta = current
            chunk = data.get('Permissions',{}).get('Permission')
            if not isinstance(chunk,list) or len(chunk)>100:
                raise CatalogError('Alibaba invalid security group rules')
            for row in chunk:
                rid = identifier(row.get('SecurityGroupRuleId'),'sgr')
                if rid in ids:
                    raise CatalogError('Alibaba duplicate security group rule')
                ids.add(rid)
                rows.append(row)
            token = data['NextToken']
            if token in (None,''):
                return {'metadata':meta,'rules':sorted(rows,key=lambda r:r['SecurityGroupRuleId'])}
            if not isinstance(token,str) or len(token)>1024 or token in tokens or not chunk:
                raise CatalogError('Alibaba incomplete security group pagination')
            tokens.add(token)
            params['NextToken'] = token
        raise CatalogError('Alibaba security group pagination limit')

    def _session(self, region):
        self._region(region)
        bound, _ = self._bound()
        bound._identity(region)
        return bound

    def _inventory(self,kind,region):
        self._region(region)
        bound = self._session(region)
        rows = bound._pages(kind,region,**({'RegionId':region} if LISTS[kind][0] in {'ecs','vpc'} else {}))
        return {'provider':'aliyun','account_id':self.account_id,'region':region,'status':'complete',
                'items':[safe_row(kind,r) for r in rows]}

    def identity(self, region):
        return self._session(region)._identity(region)

    def balance(self, region):
        data = self._session(region)._call('bssopenapi','QueryAccountBalance',region,{})['Data']
        if data.get('Currency') not in {'CNY','USD','JPY'}:
            raise CatalogError('Alibaba invalid balance currency')
        result = {'Currency':data['Currency']}
        fields = ('AvailableAmount','AvailableCashAmount','CreditAmount','MybankCreditAmount','QuotaLimit')
        for field in fields:
            if field in data:
                result[field] = matched(data[field],r'-?[0-9]{1,30}(?:\.[0-9]{1,18})?', 'balance decimal')
        if not all(field in result for field in ('AvailableAmount','AvailableCashAmount')):
            raise CatalogError('Alibaba incomplete balance response')
        return {'status':'complete','account_id':self.account_id,'balance':result}

    def available_regions(self, region):
        data = self._session(region)._call('ecs','DescribeRegions',region,{})
        rows = data.get('Regions',{}).get('Region')
        if not isinstance(rows,list):
            raise CatalogError('Alibaba invalid regions response')
        return {'status':'complete','regions':[matched(r.get('RegionId'),r'[a-z][a-z0-9-]{2,63}') for r in rows]}

    def instances(self, region):
        return self._inventory('instances',region)

    def disks(self, region):
        return self._inventory('disks',region)

    def snapshots(self, region):
        return self._inventory('snapshots',region)

    def vpcs(self, region: str):
        return self._inventory('vpcs', region)

    def vswitches(self, region: str):
        return self._inventory('vswitches', region)

    def addresses(self, region: str):
        return self._inventory('addresses', region)

    def certificates(self, region: str):
        bound = self._session(region)
        items, seen, total = [], set(), None
        for page in range(1, MAX_PAGES + 1):
            data = bound._call('cas', 'ListUserCertificateOrder', region,
                               {'OrderType': 'CERT', 'CurrentPage': page, 'ShowSize': 50})
            count, rows = data.get('TotalCount'), data.get('CertificateOrderList')
            if (type(count) is not int or not 0 <= count <= MAX_PAGES * 50 or
                    (total is not None and count != total) or data.get('CurrentPage') != page or
                    data.get('ShowSize') != 50 or not isinstance(rows, list) or len(rows) > 50):
                raise CatalogError('Alibaba incomplete certificate pagination')
            total = count
            for row in rows:
                if not isinstance(row, dict):
                    raise CatalogError('Alibaba invalid certificate row')
                cert_id = row.get('CertificateId')
                if type(cert_id) is not int or not 1 <= cert_id <= 2**63 - 1 or cert_id in seen:
                    raise CatalogError('Alibaba invalid or duplicate certificate ID')
                seen.add(cert_id)
                item = {'CertificateId': cert_id, 'fingerprint': digest(row)}
                for field in ('StartDate', 'EndDate'):
                    value = row.get(field)
                    if value not in (None, ''):
                        try:
                            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                                raise ValueError()
                            date.fromisoformat(value)
                        except ValueError:
                            raise CatalogError('Alibaba invalid certificate date') from None
                        item[field] = value
                if type(row.get('Expired')) is bool:
                    item['Expired'] = row['Expired']
                if row.get('CommonName'):
                    value = row['CommonName']
                    if not isinstance(value, str):
                        raise CatalogError('Alibaba invalid certificate hostname')
                    item['CommonName'] = ('*.' if value.startswith('*.') else '') + hostname(value.removeprefix('*.'))
                items.append(item)
            if len(items) > total or (not rows and len(items) < total):
                raise CatalogError('Alibaba incomplete certificate count')
            if len(items) == total:
                return {'status': 'complete', 'provider': self.provider, 'account_id': self.account_id,
                        'items': sorted(items, key=lambda r: r['CertificateId']), 'count': len(items),
                        'private_material_excluded': True, 'coverage': 'issued and uploaded certificates; no orders'}
        raise CatalogError('Alibaba certificate pagination limit reached')

    def security_groups(self, region):
        return self._inventory('security-groups',region)

    def security_group_rules(self, region, security_group_id):
        data = self._session(region)._rules(region,security_group_id)
        return {'status':'complete','metadata_digest':digest(data['metadata']),
                'items':[safe_row('rules',r) for r in data['rules']]}

    def dns_domains(self, region):
        return self._inventory('dns-domains',region)

    def _domain(self, region, domain_id, domain):
        domain_identifier(domain_id)
        hostname(domain)
        domains = self._pages('dns-domains',region)
        matches = [r for r in domains if r['DomainId']==domain_id and r.get('DomainName')==domain]
        if len(matches)!=1:
            raise CatalogError('Alibaba exact DNS domain ID/name not found')
        return matches[0]

    def dns_records(self, region, domain_id, domain):
        bound = self._session(region)
        bound._domain(region,domain_id,domain)
        return {'status':'complete','items':[safe_row('dns-records',r) for r in bound._pages('dns-records',region,DomainName=domain)]}

    def _domain_ttl(self, region, domain_id, domain):
        data = self._call('alidns','DescribeDomainInfo',region,
                          {'DomainName':domain,'NeedDetailAttributes':'true'})
        if data.get('DomainId') != domain_id or data.get('DomainName') != domain:
            raise CatalogError('Alibaba DNS capability domain mismatch')
        floor = data.get('MinTtl')
        if type(floor) is not int or not 1 <= floor <= 86400:
            raise CatalogError('Alibaba DNS minimum TTL capability unavailable')
        return {'DomainId':domain_id,'DomainName':domain,'MinTtl':floor,
                'contract_digest':digest({k:v for k,v in data.items() if k != 'RequestId'})}

    def cdn_domains(self, region):
        return self._inventory('cdn-domains',region)

    def _cdn_domain(self, region, domain):
        hostname(domain)
        rows = self._pages('cdn-domains',region)
        matches = [r for r in rows if r['DomainName']==domain]
        if len(matches)!=1:
            raise CatalogError('Alibaba exact CDN domain not found')
        return matches[0]

    def cdn_tasks(self, region, domain, task_id, object_type):
        numeric(task_id)
        if object_type not in ('file','preload'):
            raise CatalogError('finite CDN task type required')
        bound = self._session(region)
        bound._cdn_domain(region,domain)
        rows = bound._pages('cdn-tasks',region,DomainName=domain,TaskId=task_id,ObjectType=object_type)
        from urllib.parse import urlsplit
        if any(r.get('TaskId')!=task_id or urlsplit(r.get('ObjectPath','')).hostname!=domain for r in rows):
            raise CatalogError('Alibaba CDN task identity mismatch')
        return {'status':'complete','items':[safe_row('cdn-tasks',r) for r in rows]}

    def cdn_config(self, region, domain):
        bound = self._session(region)
        bound._cdn_domain(region,domain)
        data = bound._call('cdn','DescribeCdnDomainConfigs',region,{'DomainName':domain})
        rows = data.get('DomainConfigs',{}).get('DomainConfig')
        if not isinstance(rows,list):
            raise CatalogError('Alibaba invalid CDN configuration')
        # Configuration can contain signing keys and origin credentials: hashes only.
        return {'status':'complete','domain':domain,'configuration_count':len(rows),'configuration_digest':digest(rows)}

    def reconcile(self, operation_id, expected_state, confirm_account_id, confirm_region, confirm_target_id):
        from .aliyun_management_operations import observe_operation
        return observe_operation(self, operation_id, expected_state, confirm_account_id, confirm_region, confirm_target_id)
