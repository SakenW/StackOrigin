"""Finite commands routed through the official cf CLI, with no REST fallback."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from urllib.parse import urlencode

from .catalog import CatalogError
from . import cli_process

VERSION = '1.0.0-rc.2'
LIMIT = 2_097_152


def installation(root):
    root = Path(root)
    package = root / 'node_modules/cf'
    try:
        if not root.is_absolute() or '..' in root.parts:
            raise ValueError()
        for path in (root, package, package / 'package.json', package / 'bin/cf'):
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError()
            info = path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError()
        manifest = json.loads((package / 'package.json').read_text())
        if manifest.get('name') != 'cf' or manifest.get('version') != VERSION:
            raise ValueError()
        # Bind all cf code to the operation preview, not only a version label.
        fingerprint = hashlib.sha256()
        paths = [package / 'package.json', package / 'bin/cf', *sorted((package / 'dist').rglob('*.mjs'))]
        for path in paths:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o022 or info.st_nlink != 1):
                raise ValueError()
            fingerprint.update(str(path.relative_to(package)).encode())
            fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
        runner = Path(__file__).with_name('cloudflare_cli_runner.mjs')
        fingerprint.update(hashlib.sha256(runner.read_bytes()).digest())
        node = shutil.which('node')
        if not node:
            raise ValueError()
        return Path(node).resolve(), package / 'bin/cf', runner, fingerprint.hexdigest()
    except Exception:
        raise CatalogError('pinned Cloudflare CLI installation unavailable or unsafe') from None


def command(method, path, params, body):
    """Only private adapter routes; this is not a generic CLI/MCP tool."""
    params = params or {}
    zone = re.fullmatch(r'/zones/([0-9a-f]{32})(.*)', path)
    if path == '/zones' and method == 'GET':
        args = ['zones', 'list']
        allowed = {'account.id': 'account-id', 'match': 'match', 'order': 'order',
                   'direction': 'direction', 'page': 'page', 'per_page': 'per-page'}
    elif path == '/user/tokens/verify' and method == 'GET':
        args, allowed = ['user', 'tokens', 'verify'], {}
    elif re.fullmatch(r'/accounts/[0-9a-f]{32}/tokens/verify', path) and method == 'GET':
        args, allowed = ['accounts', 'tokens', 'verify'], {}
    elif zone:
        zone_id, tail = zone.groups()
        fixed = {('GET', ''): ['zones', 'get'],
                 ('GET', '/dnssec'): ['dns', 'dnssec', 'get'],
                 ('GET', '/dns_records'): ['dns', 'records', 'list'],
                 ('POST', '/dns_records'): ['dns', 'records', 'create'],
                 # The gateway already approves the exact purge before dispatch.
                 ('POST', '/purge_cache'): ['cache', 'purge', '--force']}
        args = fixed.get((method, tail))
        record = re.fullmatch(r'/dns_records/([0-9a-f]{32})', tail)
        setting = re.fullmatch(r'/settings/(ssl|always_use_https|min_tls_version)', tail)
        if record and method in {'GET', 'PATCH', 'DELETE'}:
            args = ['dns', 'records', {'GET': 'get', 'PATCH': 'edit', 'DELETE': 'delete'}[method], record[1]]
            if method == 'DELETE': args += ['--force']
        if setting and method in {'GET', 'PATCH'}:
            args = ['zones', 'settings', 'get' if method == 'GET' else 'edit', setting[1]]
        if args is None:
            raise CatalogError('Cloudflare CLI route is not allowlisted')
        args = [*args, '--zone', zone_id]
        allowed = {'order': 'order', 'direction': 'direction', 'page': 'page', 'per_page': 'per-page'} if tail == '/dns_records' and method == 'GET' else {}
    else:
        raise CatalogError('Cloudflare CLI route is not allowlisted')
    if set(params) - set(allowed) or (method == 'GET' and body is not None):
        raise CatalogError('Cloudflare CLI arguments are outside route')
    for key, value in params.items(): args += ['--' + allowed[key], str(value)]
    if body is not None:
        if method not in {'POST', 'PATCH'} or not isinstance(body, dict):
            raise CatalogError('invalid Cloudflare CLI body')
        args += ['--body', json.dumps(body, separators=(',', ':'), allow_nan=False)]
    return args


def run(root, token, account_id, method, path, *, params=None, body=None):
    args = command(method, path, params, body)
    node, entry, runner, _ = installation(root)
    payload = {'entry': str(entry), 'argv': args, 'token': token, 'account_id': account_id,
               'method': method, 'body': body,
               'url': 'https://api.cloudflare.com/client/v4' + path + ('?' + urlencode(params) if params else '')}
    # No caller NODE_OPTIONS, proxy, credentials, project .env or CLI profiles.
    runtime = Path(root) / 'isolated'
    runtime.mkdir(mode=0o700, exist_ok=True)
    if runtime.is_symlink() or stat.S_IMODE(runtime.stat().st_mode) != 0o700:
        raise CatalogError('unsafe Cloudflare CLI runtime')
    env = {'PATH': str(node.parent) + ':/usr/bin:/bin', 'HOME': str(runtime),
           'XDG_CONFIG_HOME': str(runtime), 'TMPDIR': str(runtime), 'CI': '1',
           'CF_SEND_TELEMETRY': 'false', 'WRANGLER_SEND_METRICS': 'false', 'NO_COLOR': '1'}
    try:
        raw = cli_process.run([str(node), str(runner)], cwd=runtime, env=env,
                              input_data=json.dumps(payload, allow_nan=False).encode(), limit=LIMIT)
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('success') is not True or value.get('errors'):
            raise ValueError()
        return value
    except Exception:
        raise CatalogError('Cloudflare CLI failed or response incomplete; details suppressed') from None
