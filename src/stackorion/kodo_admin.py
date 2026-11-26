"""Allowlisted Kodo administration, using the existing private Foundation context."""

import base64
from datetime import datetime, timedelta
import re

from .catalog import CatalogError
from .kodo_objects import KodoObjectsAdapter, object_key


REGION_IDS = frozenset({'z0', 'z1', 'z2', 'na0', 'as0', 'cn-east-2',
                        'ap-southeast-2', 'ap-southeast-3'})
RS_HOSTS = frozenset({'rs.qbox.me', 'rs.qiniuapi.com', 'rs-z0.qbox.me'} | {
    f'rs-{region}.qiniuapi.com' for region in REGION_IDS
})
UC_HOST = 'uc.qiniuapi.com'
API_HOST = 'api.qiniuapi.com'
MIME = re.compile(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+')
OPERATIONS = {
    'kodo.change-mime.v1': 'mime_type',
    'kodo.change-storage-class.v1': 'storage_type',
    'kodo.restore-archive.v1': 'freeze_after_days',
    'kodo.delete-after-days.v1': 'delete_after_days',
    'kodo.change-status.v1': 'status',
}
EXTRA_FIELDS = ('lastModify', 'expiration', 'transitionToIA', 'transitionToARCHIVE',
                'transitionToDeepArchive', 'transitionToArchiveIR', 'transitionToIntelligentTiering')


def encoded(value):
    return base64.urlsafe_b64encode(value.encode('utf-8')).decode('ascii')


def check_change(operation, key, parameters):
    object_key(key)
    if any(c in key for c in '*?') or key.endswith('/'):
        raise CatalogError('one exact object key without wildcard or directory syntax is required')
    name = OPERATIONS.get(operation)
    if name is None or set(parameters) != {name}:
        raise CatalogError('unsupported Kodo admin operation or parameters')
    value = parameters[name]
    if name == 'mime_type':
        valid = isinstance(value, str) and len(value) <= 128 and MIME.fullmatch(value)
    else:
        bounds = {'storage_type': (0, 5), 'freeze_after_days': (1, 7),
                  'delete_after_days': (0, 36500), 'status': (0, 1)}
        low, high = bounds[name]
        valid = type(value) is int and low <= value <= high
    if not valid:
        raise CatalogError('invalid Kodo admin parameter value')


class KodoAdminAdapter(KodoObjectsAdapter):
    """Same constructor/settings as KodoObjectsAdapter; no persistent query cache."""

    query_methods = {'bucket-info': 'bucket_info', 'bucket-config': 'bucket_config',
                     'bucket-domains': 'bucket_domains', 'storage-statistics': 'storage_statistics',
                     'object': 'stat_object'}

    def context(self, alias):
        try:
            context = super().context(alias)
            if context[1].rs_host not in RS_HOSTS:
                raise CatalogError('Kodo RS endpoint is not allowlisted')
            return context
        except Exception:
            raise CatalogError('Kodo admin context could not be verified') from None

    @staticmethod
    def _request(client, method, host, path, *, params=None, mutation=False, missing_ok=False):
        if host not in RS_HOSTS | {UC_HOST, API_HOST}:
            raise CatalogError('Kodo admin endpoint is not allowlisted')
        try:
            return client._request(method, host, path, params=params, retry_safe=not mutation,
                                   missing_ok=missing_ok)
        except Exception:
            raise CatalogError('Kodo admin request failed; provider details suppressed') from None

    @staticmethod
    def metadata_result(value, key):
        result = KodoObjectsAdapter.metadata_result(value, key)
        if result is None:
            return None
        if (type(value.get('putTime')) is not int or value['putTime'] <= 0
                or type(value.get('type')) is not int or value['type'] not in range(6)
                or not isinstance(value.get('mimeType'), str) or len(value['mimeType']) > 256
                or any(ord(c) < 32 for c in value['mimeType'])):
            raise CatalogError('Kodo admin metadata is incomplete')
        # Official stat omits status for enabled objects and restoreStatus for frozen objects.
        for field, allowed in (('status', (0, 1)), ('restoreStatus', (0, 1, 2))):
            v = value.get(field, 0)
            if type(v) is not int or v not in allowed:
                raise CatalogError('invalid Kodo object state')
            result[field] = v
        for field in EXTRA_FIELDS:
            if field in value:
                if type(value[field]) is not int or value[field] < 0:
                    raise CatalogError('invalid Kodo lifecycle metadata')
                result[field] = value[field]
        # Absent expiration is deliberately NOT normalized to zero: legacy objects
        # can have a deletion schedule that stat does not expose.
        return result

    def stat_with_client(self, client, bucket, key):
        object_key(key)
        value = self._request(client, 'GET', client.rs_host, '/stat/' + encoded(f'{bucket}:{key}'),
                              missing_ok=True)
        return self.metadata_result(value, key)

    def change_object(self, client, bucket, key, operation, parameters, old):
        check_change(operation, key, parameters)
        entry = encoded(f'{bucket}:{key}')
        if operation == 'kodo.change-mime.v1':
            condition = '&'.join(f'{name}={old[name]}' for name in ('hash', 'fsize', 'putTime'))
            path = f'/chgm/{entry}/mime/{encoded(parameters["mime_type"])}/cond/{encoded(condition)}'
        elif operation == 'kodo.change-storage-class.v1':
            path = f'/chtype/{entry}/type/{parameters["storage_type"]}'
        elif operation == 'kodo.restore-archive.v1':
            path = f'/restoreAr/{entry}/freezeAfterDays/{parameters["freeze_after_days"]}'
        elif operation == 'kodo.delete-after-days.v1':
            path = f'/deleteAfterDays/{entry}/{parameters["delete_after_days"]}'
        else:
            path = f'/chstatus/{entry}/status/{parameters["status"]}'
        value = self._request(client, 'POST', client.rs_host, path, mutation=True)
        if value not in (None, {}):
            raise CatalogError('unexpected Kodo mutation response; reconcile without replay')

    def bucket_info(self, alias):
        target, client, *_ = self.context(alias)
        # This read-only API uses POST in the official SDK.
        value = self._request(client, 'POST', UC_HOST, '/v2/bucketInfo',
                              params={'bucket': target['bucket']})
        if not isinstance(value, dict):
            raise CatalogError('invalid bucket information')
        result = {}
        for name in ('private', 'protected', 'no_index_page', 'token_anti_leech_mode',
                     'anti_leech_mode', 'max_age'):
            if name in value:
                v = value[name]
                upper = 2 if name == 'anti_leech_mode' else (2**63 - 1 if name == 'max_age' else 1)
                if type(v) is not int or not 0 <= v <= upper:
                    raise CatalogError('invalid bucket configuration')
                result[name] = v
        if 'private' not in result or value.get('region') not in REGION_IDS:
            raise CatalogError('bucket information lacks privacy or known region')
        result['region'] = value['region']
        # Never return mac_key, mac_key2, mirror URLs, arbitrary provider fields.
        return {'status': 'observed', 'target': alias, 'bucket': target['bucket'],
                'info': result, 'coverage': 'allowlisted configuration fields only'}

    def bucket_config(self, alias):
        return self.bucket_info(alias)

    def inspect(self, alias):
        return self.bucket_info(alias)

    def bucket_domains(self, alias):
        target, client, *_ = self.context(alias)
        value = self._request(client, 'GET', UC_HOST, '/v2/domains', params={'tbl': target['bucket']})
        domain = re.compile(r'(?=.{1,253}\Z)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}')
        if (not isinstance(value, list) or len(value) > 1000
                or any(not isinstance(v, str) or not domain.fullmatch(v) for v in value)):
            raise CatalogError('invalid bucket domain list')
        return {'status': 'observed', 'target': alias, 'bucket': target['bucket'],
                'domains': sorted(set(value)), 'coverage': 'binding list; no DNS or reachability claim'}

    def storage_statistics(self, alias, begin, end, granularity='day'):
        # Limit to documented daily standard-storage metrics, 31 days per request.
        try:
            if granularity != 'day' or any(not isinstance(v, str) or not re.fullmatch(r'[0-9]{14}', v)
                                           for v in (begin, end)):
                raise ValueError()
            start, stop = (datetime.strptime(v, '%Y%m%d%H%M%S') for v in (begin, end))
            if not timedelta(0) < stop - start <= timedelta(days=31):
                raise ValueError()
        except (ValueError, TypeError):
            raise CatalogError('statistics require valid 14-digit times, day granularity and at most 31 days') from None
        target, client, *_ = self.context(alias)
        value = self._request(client, 'GET', API_HOST, '/v6/space',
                              params={'bucket': target['bucket'], 'begin': begin, 'end': end, 'g': 'day'})
        if not isinstance(value, dict):
            raise CatalogError('invalid storage statistics')
        times, datas = value.get('times'), value.get('datas')
        if (not isinstance(times, list) or not isinstance(datas, list) or len(times) != len(datas)
                or len(times) > 32 or any(type(v) is not int or v < 0 for v in times + datas)
                or times != sorted(set(times))):
            raise CatalogError('invalid storage statistics series')
        return {'status': 'observed', 'target': alias, 'bucket': target['bucket'], 'begin': begin, 'end': end,
                'granularity': 'day', 'times': times, 'datas': datas, 'unit': 'bytes',
                'coverage': 'standard storage only; delayed provider statistics, not total inventory'}
