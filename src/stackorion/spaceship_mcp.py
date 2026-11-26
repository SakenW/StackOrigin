"""Spaceship's official MCP client. Only reviewed tools can cross this boundary."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import fcntl
import json
import os
from pathlib import Path
import stat
import time

import httpx2
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (OAuthClientInformationFull, OAuthClientMetadata,
                             OAuthToken, OAuthMetadata, ProtectedResourceMetadata)

from .catalog import CatalogError
from .provider_paths import private_runtime_directory, runtime_root

URL = 'https://mcp.spaceship.com/mcp'
READS = frozenset({'domains_list', 'dns_records_get', 'async_operation_get'})
WRITES = frozenset({'domain_set_nameservers', 'dns_records_save', 'dns_records_delete'})
TOOLS = READS | WRITES
LIMIT = 1_048_576
CONTRACT_SHA256 = 'de3e43feca5d4afbea585a055ba743e2729c893482d2841a539ec871a582b5ba'


class Storage:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else runtime_root() / "providers/spaceship-oauth/session.json"
        if not self.path.is_absolute() or '..' in self.path.parts:
            raise CatalogError('Spaceship OAuth storage must be an absolute path')

    def read(self):
        try:
            if (not self.path.is_absolute() or '..' in self.path.parts or
                    any(p.is_symlink() for p in (self.path, *self.path.parents))):
                raise ValueError()
            for path in (self.path.parent, self.path):
                info = path.lstat()
                if path.is_symlink() or info.st_uid != os.getuid() or (
                        stat.S_IMODE(info.st_mode) != (0o600 if path == self.path else 0o700)):
                    raise ValueError()
            if self.path.stat().st_size > 65536: raise ValueError()
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except Exception:
            raise CatalogError('Spaceship OAuth store unavailable or unsafe') from None

    def write(self, values):
        if len(json.dumps(values).encode()) > 65536:
            raise CatalogError("Spaceship OAuth state exceeded its bound")
        if (not self.path.is_absolute() or '..' in self.path.parts or
                any(p.is_symlink() for p in (self.path, *self.path.parents))):
            raise CatalogError('Spaceship OAuth path is unsafe')
        private_runtime_directory(self.path.parent)
        self.read()  # Reject an unsafe existing destination.
        temp = self.path.with_name(self.path.name + '.new-' + str(os.getpid()))
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(values, stream); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    async def get_tokens(self):
        values = self.read()
        if not values.get('tokens'): return None
        token = OAuthToken.model_validate(values['tokens'])
        if values.get('expires_at') is not None:
            token.expires_in = max(0, int(values['expires_at'] - time.time()))
        return token

    async def set_tokens(self, token):
        values = self.read()
        values['tokens'] = token.model_dump(mode='json')
        values['expires_at'] = time.time() + token.expires_in if token.expires_in is not None else None
        self.write(values)

    async def get_client_info(self):
        values = self.read()
        return OAuthClientInformationFull.model_validate(values['client']) if values.get('client') else None

    async def set_client_info(self, client):
        values = self.read(); values['client'] = client.model_dump(mode='json'); self.write(values)


async def _no_interactive_login(*args):
    raise CatalogError('Spaceship official MCP needs OAuth login; run stackorion-spaceship-login')


class Provider(OAuthClientProvider):
    async def _initialize(self):
        await super()._initialize()
        values = self.context.storage.read()
        # mcp 2.1.1 loads saved tokens without restoring their expiry or discovery
        # context. Restore only that context; the SDK still performs the refresh.
        self.context.token_expiry_time = values.get('expires_at')
        if self.context.current_tokens and self.context.current_tokens.refresh_token:
            metadata = values.get('discovery')
            if metadata is None:
                async with httpx2.AsyncClient(trust_env=False,follow_redirects=False,timeout=15) as http:
                    prm = await http.get('https://mcp.spaceship.com/.well-known/oauth-protected-resource')
                    issuer = await http.get('https://id.service.spaceship.com/.well-known/oauth-authorization-server')
                    prm.raise_for_status(); issuer.raise_for_status()
                    metadata = {'protected':prm.json(),'issuer':issuer.json()}
                values['discovery'] = metadata; self.context.storage.write(values)
            protected = ProtectedResourceMetadata.model_validate(metadata['protected'])
            issuer = OAuthMetadata.model_validate(metadata['issuer'])
            if (str(protected.resource) != 'https://mcp.spaceship.com/' or
                    str(issuer.issuer).rstrip('/') != 'https://id.service.spaceship.com' or
                    str(issuer.token_endpoint) != 'https://id.service.spaceship.com/connect/token'):
                raise CatalogError('Spaceship OAuth discovery identity changed')
            await self._validate_resource_match(protected)
            self.context.protected_resource_metadata = protected
            self.context.oauth_metadata = issuer
            self.context.auth_server_url = str(issuer.issuer)


def oauth(storage, *, redirect_uri=None, redirect_handler=None, callback_handler=None):
    values = storage.read()
    redirects = values.get('client', {}).get('redirect_uris')
    metadata = OAuthClientMetadata(client_name='StackOrion',
        redirect_uris=[redirect_uri] if redirect_uri else redirects or ['http://127.0.0.1:8765/callback'],
        token_endpoint_auth_method='none', scope='openid offline_access mcp.spaceship.com')
    return Provider(URL, metadata, storage,
        redirect_handler=redirect_handler or _no_interactive_login,
        callback_handler=callback_handler or _no_interactive_login)


def dispatch_guard():
    """No OAuth recovery, client retry or reconnect may repeat a tool request."""
    dispatched = set()
    async def guard(request):
        if str(request.url) == URL and request.method == 'POST':
            message = json.loads(request.content)
            if message.get('method') == 'tools/call':
                tool = message.get('params', {}).get('name')
                if tool not in TOOLS:
                    raise CatalogError('Spaceship upstream tool is not allowlisted')
                identity = (message.get('id'), tool)
                if identity in dispatched:
                    raise CatalogError('Spaceship tool dispatch cannot be replayed')
                dispatched.add(identity)
    return guard


@asynccontextmanager
async def connect(storage, **auth_callbacks):
    guard = dispatch_guard()
    private_runtime_directory(storage.path.parent)
    storage.read()
    lock = storage.path.with_suffix('.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CatalogError('unsafe Spaceship OAuth lock')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise CatalogError('Spaceship OAuth session is busy; no dispatch') from None
        async with httpx2.AsyncClient(auth=oauth(storage, **auth_callbacks), trust_env=False,
                follow_redirects=False, timeout=20, event_hooks={'request': [guard]}) as http:
            async with Client(streamable_http_client(URL, http_client=http),
                              mode='legacy', cache=None, read_timeout_seconds=1800 if auth_callbacks else 20) as client:
                yield client
    finally:
        os.close(fd)


def data(result):
    if getattr(result, 'result_type', 'complete') != 'complete':
        raise CatalogError('Spaceship MCP operation requires further input; not completed')
    if result.is_error: raise CatalogError('Spaceship MCP tool rejected request; details suppressed')
    value = result.structured_content
    if value is None:
        texts = [c.text for c in result.content if getattr(c, 'type', None) == 'text']
        if len(texts) != 1: raise CatalogError('Spaceship MCP structured result unavailable')
        value = json.loads(texts[0])
    if not isinstance(value, dict) or len(json.dumps(value).encode()) > LIMIT:
        raise CatalogError('Spaceship MCP response incomplete or too large')
    return value


async def _call(storage, name, args):
    if name not in TOOLS or not isinstance(args, dict):
        raise CatalogError('Spaceship upstream tool is not allowlisted')
    if not storage.read().get('tokens'):
        raise CatalogError('Spaceship official MCP needs OAuth login; run stackorion-spaceship-login')
    async with connect(storage) as client:
        catalog = await client.list_tools()
        observed = contracts(catalog.tools)
        expected = storage.read().get('tool_contracts')
        if not expected or observed != expected:
            raise CatalogError('Spaceship MCP tool contracts changed; verify before use')
        return data(await client.call_tool(name, args))


def call(path, name, args):
    # Adapter methods run synchronously in the existing gateway worker.
    try: return asyncio.run(_call(Storage(path), name, args))
    except Exception:
        raise CatalogError('Spaceship official MCP unavailable or result unknown; do not replay') from None


def binding(path):
    values = Storage(path).read()
    if not values.get('tokens') or not values.get('client') or not values.get('tool_contracts'):
        raise CatalogError('Spaceship official MCP requires OAuth login')
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def contracts(tools):
    selected = {t.name: {'inputSchema': t.input_schema, 'outputSchema': t.output_schema}
                for t in tools if t.name in TOOLS}
    if set(selected) != TOOLS:
        raise CatalogError('Spaceship official MCP does not expose required tools')
    if hashlib.sha256(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()).hexdigest() != CONTRACT_SHA256:
        raise CatalogError('Spaceship official MCP tool contract differs from reviewed baseline')
    return selected
