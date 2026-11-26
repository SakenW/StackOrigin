"""Bounded public DNS and pinned-IP HTTPS observations after provider ownership checks."""
from datetime import datetime, timezone
import ipaddress
import re
import socket
import ssl
import subprocess
import time

from .catalog import CatalogError

HOST=re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')

def hostname(value):
    if not isinstance(value,str) or not HOST.fullmatch(value):raise CatalogError('exact canonical public hostname required')
    return value

def dig(name,record_type,server=None):
    hostname(name)
    if record_type not in {'NS','A','SOA','DS'}:raise CatalogError('unsupported DNS observation')
    args=['/usr/bin/dig','+time=2','+tries=1','+noall','+answer','+comments']
    if server is not None:
        try:address=ipaddress.ip_address(server)
        except ValueError as exc:raise CatalogError('invalid DNS authority address') from exc
        if not address.is_global:raise CatalogError('DNS authority address must be public')
        args+=['+norecurse','@'+str(address)]
    args+=[name,record_type]
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=5)
        if p.returncode or len(p.stdout)>32768:raise ValueError()
    except (OSError,subprocess.TimeoutExpired,ValueError) as exc:
        raise CatalogError('bounded DNS observation unavailable') from exc
    values=[]
    for line in p.stdout.splitlines():
        fields=line.split()
        if len(fields)>=5 and fields[2]=='IN' and fields[3]==record_type:
            if record_type=='NS':
                if fields[0].lower().rstrip('.') == name:
                    value=fields[4].lower().rstrip('.');hostname(value);values.append(value)
            elif record_type=='A':
                try:value=ipaddress.ip_address(fields[4])
                except ValueError as exc:raise CatalogError('invalid public DNS address') from exc
                if value.version==4 and value.is_global:values.append(str(value))
            elif record_type=='SOA':values.append({'owner':fields[0].rstrip('.').lower(),'serial':fields[6] if len(fields)>6 else None})
            elif len(fields)==8 and all(v.isdigit() for v in fields[4:7]) and re.fullmatch(r'[0-9A-Fa-f]+',fields[7]):
                values.append({'key_tag':int(fields[4]),'algorithm':int(fields[5]),'digest_type':int(fields[6]),'digest':fields[7].lower()})
    match=re.search(r'flags:\s*([^;]+);',p.stdout)
    return {'values':values[:50],'authoritative':bool(match and 'aa' in match[1].split()),
            'rcode':re.search(r'status:\s*([A-Z]+)',p.stdout).group(1) if re.search(r'status:\s*([A-Z]+)',p.stdout) else 'unknown'}

def https_head(domain,address):
    hostname(domain)
    try:ip=ipaddress.ip_address(address)
    except ValueError as exc:raise CatalogError('invalid HTTPS address') from exc
    if not ip.is_global:raise CatalogError('HTTPS address must be public')
    # Pin to the observed public IP. TLS still validates the original hostname;
    # DNS rebinding and redirects cannot move this probe to private addresses.
    deadline=time.monotonic()+10
    def remaining():
        budget=deadline-time.monotonic()
        if budget<=0:raise TimeoutError('HTTPS observation deadline')
        return min(5,budget)
    try:
        with socket.create_connection((str(ip),443),timeout=remaining()) as raw:
            raw.settimeout(remaining())
            with ssl.create_default_context().wrap_socket(raw,server_hostname=domain) as connection:
                connection.settimeout(remaining())
                connection.sendall(('HEAD / HTTP/1.1\r\nHost: '+domain+'\r\nConnection: close\r\nUser-Agent: stackorion-observation\r\n\r\n').encode('ascii'))
                data=b''
                while b'\r\n\r\n' not in data and len(data)<=8192:
                    connection.settimeout(remaining())
                    chunk=connection.recv(min(1024,8193-len(data)))
                    if not chunk:break
                    data+=chunk
                if len(data)>8192 or b'\r\n\r\n' not in data:raise ValueError()
                status=re.match(rb'HTTP/1\.[01] ([0-9]{3})(?: |\r)',data)
                if not status:raise ValueError()
                return {'status':'observed','http_status':int(status[1]),'tls_hostname_verified':True,
                        'certificate_not_after':connection.getpeercert().get('notAfter'),
                        'coverage':'HEAD / only; redirects not followed; no content or application health claim'}
    except (OSError,ssl.SSLError,ValueError):
        return {'status':'unavailable','tls_hostname_verified':False,'reason':'public HTTPS handshake or bounded HEAD failed'}

def observe(domain,expected_nameservers):
    hostname(domain)
    if not isinstance(expected_nameservers,list) or len(expected_nameservers)>12:
        raise CatalogError('invalid provider nameserver metadata')
    expected=sorted({hostname(n.lower().rstrip('.')) for n in expected_nameservers})
    result={'domain':domain,'mutation_invoked':False,'observed_at':datetime.now(timezone.utc).isoformat(),
            'coverage':'public delivery observations independent of provider control-plane acceptance'}
    servers = []
    try:
        current = dig(domain, 'NS')
        servers = sorted(set(current['values'])) if current['rcode'] == 'NOERROR' else []
        result['recursive_ns'] = {'nameservers': servers, 'expected': expected,
                                  'matches_provider': servers == expected if servers and expected else None,
                                  'rcode': current['rcode']}
    except CatalogError:
        pass
    try:
        ds=dig(domain,'DS')
        result['parent_ds_observation']={'records':ds['values'],'rcode':ds['rcode'],
                                        'coverage':'recursive DS observation; does not certify registrar DNSSEC configuration or absence'}
    except CatalogError:
        pass
    authority = {'status': 'unavailable', 'reason': 'no public nameserver address'}
    try:
        if servers:
            addresses=dig(servers[0],'A')['values']
            if addresses:
                answer=dig(domain,'SOA',addresses[0])
                authority={'status':'observed','nameserver':servers[0],'authoritative':answer['authoritative'],
                           'zone_soa_present':any(r['owner']==domain for r in answer['values']),'rcode':answer['rcode'],
                           'coverage':'one nameserver only; does not prove every resolver has converged'}
    except CatalogError:
        authority = {'status': 'unavailable', 'reason': 'authoritative DNS observation unavailable'}
    result['authority'] = authority
    try:
        addresses=dig(domain,'A')['values']
        result['https']=https_head(domain,addresses[0]) if addresses else {'status':'unavailable','reason':'no public IPv4 observed; IPv6-only domains not probed'}
    except CatalogError:
        result['https'] = {'status': 'unavailable', 'reason': 'public IPv4 observation unavailable'}
    if 'recursive_ns' in result or result['https']['status'] == 'observed':
        result['status'] = 'observed'
    else:
        result.update(status='unavailable', reason='public DNS and HTTPS observations unavailable')
    return result
