"""Bounded CDN queries and fixed signed API requests; never follow redirects."""
import base64
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from .catalog import CatalogError
from .kodo_objects import KodoObjectsAdapter, _NoRedirect
from .qiniu import _regular_owned

DOMAIN = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
TOKEN = re.compile(r'[a-zA-Z0-9_-]{1,128}')
DOMAIN_FIELDS = ('name', 'type', 'cname', 'protocol', 'platform', 'product', 'geoCover',
                 'operationType', 'operatingState', 'freezeType', 'createAt', 'modifyAt')
CERT_FIELDS = ('certid', 'name', 'common_name', 'dnsnames', 'not_before', 'not_after', 'create_time')

def domain_name(value):
    if not isinstance(value, str) or not DOMAIN.fullmatch(value):
        raise CatalogError('one exact lowercase CDN domain is required')
    return value

def selected(data, fields):
    if not isinstance(data, dict):
        raise CatalogError('invalid CDN response')
    return {k: data[k] for k in fields if k in data}

def page_args(marker, limit):
    if (not isinstance(marker, str) or len(marker) > 2048 or any(ord(c)<32 for c in marker)
            or type(limit) is not int or not 1 <= limit <= 100):
        raise CatalogError('invalid bounded CDN page')
    return urllib.parse.urlencode({'marker': marker, 'limit': limit})

class QiniuCDNAdapter:
    def __init__(self, settings):
        if not isinstance(settings, dict) or set(settings) != {'credential_file', 'foundation_root'}:
            raise CatalogError('invalid CDN settings')
        self.credential = Path(settings['credential_file'])
        self.foundation_root = Path(settings['foundation_root'])
        if not self.credential.is_absolute() or not self.foundation_root.is_absolute():
            raise CatalogError('CDN paths must be absolute')

    credentials = KodoObjectsAdapter.credentials

    def list_targets(self):
        return []  # Owned domains are explicitly queried; not silently registered from a partial page.

    def request(self, host, method, path, body=None):
        if host not in {'api.qiniu.com', 'fusion.qiniuapi.com'} or not path.startswith('/') or '#' in path:
            raise CatalogError('invalid CDN endpoint')
        ak, sk = self.credentials()
        data = None if body is None else json.dumps(body, separators=(',', ':'), ensure_ascii=False).encode()
        headers = {'Host': host}
        if data is not None or method in {'POST','PUT'}:
            headers['Content-Type'] = 'application/json'
        if host == 'fusion.qiniuapi.com':
            sign = (path+'\n').encode(); scheme = 'QBox'
        else:
            headers['X-Qiniu-Date'] = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            sign = f'{method} {path}\nHost: {host}'
            if 'Content-Type' in headers:
                sign += '\nContent-Type: application/json'
            sign += '\nX-Qiniu-Date: '+headers['X-Qiniu-Date']+'\n\n'
            sign = sign.encode()+(data or b''); scheme = 'Qiniu'
        token = base64.urlsafe_b64encode(hmac.new(sk.encode(), sign, hashlib.sha1).digest()).decode()
        headers['Authorization'] = f'{scheme} {ak}:{token}'
        request = urllib.request.Request('https://'+host+path, data=data, headers=headers, method=method)
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=25) as response:
                raw = response.read(1048577)
            if len(raw)>1048576:
                raise ValueError()
            value = json.loads(raw)
            # Current certificate reads return code=0 although the official
            # reference shows 200. Scope this compatibility to certificate GETs.
            success_codes={0,200} if host=='fusion.qiniuapi.com' and method=='GET' and path.split('?')[0].startswith('/sslcert') else {200}
            if not isinstance(value, dict) or value.get('code',200) not in success_codes:
                raise ValueError()
            return value
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise CatalogError('CDN API unavailable or response rejected; no automatic retry') from exc

    def domains(self, marker='', limit=100):
        value = self.request('api.qiniu.com','GET','/domain?'+page_args(marker,limit))
        rows=value.get('domains')
        if not isinstance(rows,list) or len(rows)>limit or not isinstance(value.get('marker',''),str):
            raise CatalogError('invalid CDN domain page')
        return {'domains':[selected(r,DOMAIN_FIELDS) for r in rows], 'next_marker':value.get('marker',''),
                'coverage':'one page', 'mutation_invoked':False}

    def discover(self):
        return self.domains()

    def inspect(self, alias):
        return self.domain(alias)

    def domain(self, domain):
        domain_name(domain)
        raw=self.request('api.qiniu.com','GET','/domain/'+domain)
        if raw.get('name') != domain:
            raise CatalogError('CDN domain ownership or identity mismatch')
        value=selected(raw,DOMAIN_FIELDS)
        value['https']=selected(raw.get('https') or {}, ('certId','forceHttps','http2Enable','tlsVersions'))
        return {'domain':value,'mutation_invoked':False}

    def certificates(self, marker='', limit=100):
        raw=self.request('fusion.qiniuapi.com','GET','/sslcert?'+page_args(marker,limit))
        rows=raw.get('certs')
        if not isinstance(rows,list) or len(rows)>limit or not isinstance(raw.get('marker',''),str):
            raise CatalogError('invalid certificate page')
        return {'certificates':[selected(r,CERT_FIELDS) for r in rows], 'next_marker':raw.get('marker',''),
                'mutation_invoked':False,'coverage':'one page; private keys and certificate bodies excluded'}

    def certificate(self, cert_id):
        if not isinstance(cert_id,str) or not TOKEN.fullmatch(cert_id):
            raise CatalogError('invalid certificate id')
        raw=self.request('fusion.qiniuapi.com','GET','/sslcert/'+cert_id)
        value=selected(raw.get('cert'),CERT_FIELDS)
        if value.get('certid')!=cert_id:
            raise CatalogError('certificate identity mismatch')
        return value

    def usage(self, domain, start_date, end_date, metric='flux', granularity='day'):
        self.domain(domain)
        try:
            first,last=date.fromisoformat(start_date),date.fromisoformat(end_date)
            if not 0 <= (last-first).days <= 30 or last>datetime.now(timezone.utc).date(): raise ValueError()
        except (TypeError,ValueError) as exc:
            raise CatalogError('CDN usage requires a past date range of at most 31 days') from exc
        if metric not in {'flux','bandwidth'} or granularity not in {'day','hour','5min'}:
            raise CatalogError('invalid CDN usage metric or granularity')
        raw=self.request('fusion.qiniuapi.com','POST','/v2/tune/'+metric,
                         {'domains':domain,'startDate':start_date,'endDate':end_date,'granularity':granularity})
        times=raw.get('time'); data=raw.get('data')
        if not isinstance(data,dict) or domain not in data:
            raise CatalogError('CDN statistics are missing the requested domain')
        rows=data[domain]
        if not isinstance(times,list) or len(times)>8928 or any(not isinstance(t,str) for t in times) or not isinstance(rows,dict):
            raise CatalogError('invalid bounded CDN statistics')
        values={}
        for region in ('china','oversea'):
            array=rows.get(region)
            if not isinstance(array,list) or len(array)!=len(times) or any(type(v) not in (int,float) or v<0 for v in array):
                raise CatalogError('invalid CDN usage series')
            values[region]=array
        return {'status':'observed' if times else 'no-data','domain':domain,'metric':metric,'unit':'bytes' if metric=='flux' else 'bps',
                'time':times,'data':values,'mutation_invoked':False}

    def tasks(self, action, request_id, page=0, limit=100):
        if action not in {'refresh','prefetch'} or not isinstance(request_id,str) or not TOKEN.fullmatch(request_id):
            raise CatalogError('invalid CDN task query')
        if type(page) is not int or not 0<=page<=1000 or type(limit) is not int or not 1<=limit<=100:
            raise CatalogError('invalid task page')
        raw=self.request('fusion.qiniuapi.com','POST',f'/v2/tune/{action}/list',
                         {'requestId':request_id,'pageNo':page,'pageSize':limit})
        rows=raw.get('items')
        if not isinstance(rows,list) or len(rows)>limit or any(r.get('requestId')!=request_id for r in rows if isinstance(r,dict)):
            raise CatalogError('invalid CDN task response')
        return {'request_id':request_id,'action':action,'total':raw.get('total'),'page':page,
                'items':[selected(r,('requestId','taskId','isDir','url','progress','state','createAt','beginAt','endAt')) for r in rows],
                'mutation_invoked':False}
