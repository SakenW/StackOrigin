"""Read-only LAS audit through the server-management owned CLI."""
from pathlib import Path
import re

from .catalog import CatalogError
from .host_contract import HostReading, HostTarget
from .qiniu import run_qiniu_helper


class QiniuLASAdapter:
    provider = "qiniu"

    def __init__(self, settings):
        if set(settings) != {'script_path', 'credential_file', 'targets'}:
            raise CatalogError('invalid LAS adapter settings')
        for field in ('script_path', 'credential_file'):
            if not isinstance(settings[field], str) or not Path(settings[field]).is_absolute():
                raise CatalogError('LAS paths must be absolute')
        self.script = Path(settings['script_path'])
        self.credential = Path(settings['credential_file'])
        if not isinstance(settings['targets'], list):
            raise CatalogError('LAS targets must be a list')
        self.targets = {}
        identities = set()
        for target in settings['targets']:
            if not isinstance(target, dict) or set(target) != {'alias', 'region', 'instance_id'}:
                raise CatalogError('invalid LAS target')
            alias, region, instance = target['alias'], target['region'], target['instance_id']
            if (not isinstance(alias, str) or not alias or
                not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}-[a-z]+-\d+', region) or
                not isinstance(instance, str) or not re.fullmatch(r'i-[0-9a-f]+', instance)):
                raise CatalogError('invalid LAS target identifier')
            if alias in self.targets or (region, instance) in identities:
                raise CatalogError('duplicate LAS target')
            self.targets[alias] = (region, instance)
            identities.add((region, instance))

    def list_targets(self):
        return sorted(self.targets)

    def target_inventory(self):
        return {'status': 'configured', 'provider': 'qiniu', 'product': 'las',
                'targets': self.list_targets(), 'coverage': 'configured aliases; not live cloud discovery'}

    def host_target(self, alias):
        if alias not in self.targets:
            raise CatalogError("unknown LAS target")
        region, instance = self.targets[alias]
        return HostTarget(instance, "las", region)

    def host_reading(self, alias):
        result = self.inspect(alias)
        state = result["data"].get("state")
        if isinstance(state, str):
            state = state.lower()
        # The LAS instance audit exposes configured specs and state, not usage samples.
        return HostReading(result.get("observed_at"), "las-api", {}, state)

    def inspect(self, alias):
        if alias not in self.targets:
            raise CatalogError('unknown LAS target')
        region, instance = self.targets[alias]
        value = run_qiniu_helper(self.script, self.credential,
                                ['--region', region, '--instance-id', instance, 'audit'])
        row = value.get('instance')
        if (value.get('product') != 'las' or value.get('status') != 'observed' or
            value.get('region') != region or not isinstance(row, dict) or
            row.get('instanceID') != instance or
            not isinstance(row.get('region'), dict) or row['region'].get('id') != region):
            raise CatalogError('LAS audit target or region mismatch')
        # Helper already projects a whitelist. Do not expose arbitrary helper fields.
        data = {key: row[key] for key in (
            'instanceID', 'name', 'state', 'networkType', 'privateIPAddresses',
            'publicIPAddresses', 'createdAt', 'expiredAt', 'internetMaxBandwidth') if key in row}
        for key, fields in {
            'region': ('id', 'name'),
            'instanceTypeInfo': ('instanceType', 'cpu', 'memory', 'instanceTypeFamily'),
            'systemDisk': ('id', 'diskType', 'size'),
            'image': ('id', 'name', 'osType', 'osName'),
        }.items():
            if isinstance(row.get(key), dict):
                data[key] = {field: row[key][field] for field in fields if field in row[key]}
        access = row.get('publicAccess', [])
        if not isinstance(access, list) or any(not isinstance(item, dict) for item in access):
            raise CatalogError('invalid LAS public access data')
        data['publicAccess'] = []
        for item in access:
            clean = {key: item[key] for key in ('id', 'type', 'internalPort', 'endpoint', 'state') if key in item}
            if 'endpoints' in item:
                endpoints = item['endpoints']
                if not isinstance(endpoints, list) or any(not isinstance(x, dict) for x in endpoints):
                    raise CatalogError('invalid LAS port forward endpoints')
                clean['endpoints'] = [{key: point[key] for key in ('endpoint', 'isp') if key in point}
                                      for point in endpoints]
            data['publicAccess'].append(clean)
        return {'provider': 'qiniu', 'product': 'las', 'target': alias,
                'observed_at': value.get('observed_at'), 'status': 'observed', 'data': data,
                'coverage': {'cloud_instance': 'observed', 'ssh_authentication': 'not_checked',
                             'host_key': 'not_checked', 'http_backend': 'not_checked'}}
