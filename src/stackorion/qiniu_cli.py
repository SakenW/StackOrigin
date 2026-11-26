"""Official qshell metadata queries; account setup never appears in argv."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .catalog import CatalogError
from .provider_paths import private_runtime_directory, runtime_root
from .qiniu import _regular_owned
from . import cli_process

VERSION = 'v2.22.1'
BINARY_SHA256 = '3157d0c873bc17d0ddd7968440e7a8bbe8fa0aa661f03f8be4b037f0f0d14476'
BUCKET = re.compile(r'[a-z0-9][a-z0-9-]{1,62}')


def empty_domains(code, stdout, stderr):
    # v2.22.1 reports a successful empty /v3/domains response as exit 1.
    # A request/permission error is appended after => and must remain an error.
    return code == 1 and not stdout and bool(re.fullmatch(
        rb'\[E\]  Get domains error: domain list is empty =>\s*',stderr))


def credentials(path):
    path = Path(path); _regular_owned(path, secret=True)
    if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_size > 8192:
        raise CatalogError('unsafe Qiniu credentials')
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip().removeprefix('export ')
        if not line or line.startswith('#'): continue
        name, separator, value = line.partition('=')
        if name.strip() in {'QINIU_AK','QINIU_SK'}:
            name = name.strip(); value = value.strip().strip('"\'')
            if name in values or not separator or not re.fullmatch(r'[A-Za-z0-9_-]{10,512}',value):
                raise CatalogError('invalid Qiniu credentials')
            values[name] = value
    if set(values) != {'QINIU_AK','QINIU_SK'}: raise CatalogError('missing Qiniu credentials')
    return values


def query(binary, credential_file, command, bucket=None):
    binary = Path(binary)
    if command not in {'buckets','domains'} or (command=='domains' and
            (not isinstance(bucket,str) or not BUCKET.fullmatch(bucket))) or (command=='buckets' and bucket is not None):
        raise CatalogError('qshell metadata command is not allowlisted')
    _regular_owned(binary, secret=False)
    if (any(p.is_symlink() for p in (binary,*binary.parents)) or
            hashlib.sha256(binary.read_bytes()).hexdigest() != BINARY_SHA256):
        raise CatalogError('pinned official qshell binary unavailable')
    values=credentials(credential_file)
    temporary_root = private_runtime_directory(runtime_root() / "tmp/qshell")
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        root=Path(directory); config=root/'credentials.json'
        fd=os.open(config,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump({'access_key':values['QINIU_AK'],'secret_key':values['QINIU_SK'],'use_https':True},stream)
        argv=[str(binary),'-C',str(config),command]
        if bucket is not None: argv.append(bucket)
        raw=cli_process.run(argv,cwd=root,env={'HOME':str(root),'TMPDIR':str(root),
                'PATH':'/usr/bin:/bin','LANG':'C'},timeout=25,limit=1_048_576,
                accepted_failure=empty_domains if command=='domains' else None)
    try:
        rows=raw.decode('utf-8').splitlines()
        if command == 'buckets' and rows and [v.strip() for v in rows[0].split('\t')] == ['Region','FileNum','StorageSize','Bucket']:
            rows = rows[1:]
        if len(rows)>1000 or len(rows)!=len(set(rows)) or any(not r or any(ord(c)<33 for c in r) for r in rows):
            raise ValueError()
        if command=='buckets' and any(not BUCKET.fullmatch(r) for r in rows): raise ValueError()
        if command=='domains' and any(not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}',r) for r in rows): raise ValueError()
        return sorted(rows)
    except Exception: raise CatalogError('qshell metadata output incomplete') from None
