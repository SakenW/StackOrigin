"""Interactive OAuth through the official MCP SDK; callback never logs a code."""
import asyncio
from urllib.parse import parse_qs, urlsplit

from mcp.shared.auth import AuthorizationCodeResult
from .spaceship_mcp import Storage, connect, TOOLS, contracts


async def login():
    store = Storage()
    request = Storage(store.path.with_name('spaceship-login-request.json'))
    request_written = False
    future = asyncio.get_running_loop().create_future()
    async def callback(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            first = raw.split(b'\r\n', 1)[0].decode('ascii')
            method, target, _ = first.split(' ')
            url = urlsplit(target); args = parse_qs(url.query)
            if (method != 'GET' or url.path != '/callback' or not {'code', 'state'} <= set(args) or
                    len(args['code']) != 1 or len(args['state']) != 1):
                raise ValueError()
            if not future.done(): future.set_result(AuthorizationCodeResult(code=args['code'][0], state=args['state'][0], iss=args.get('iss', [None])[0]))
            body = b'OAuth callback received. Return to the terminal to verify the connection.'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: ' +
                         str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
        except Exception:
            writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        finally:
            await writer.drain(); writer.close(); await writer.wait_closed()
    server = await asyncio.start_server(callback, '127.0.0.1', 0, limit=16384)
    port = server.sockets[0].getsockname()[1]
    async def redirect(url):
        nonlocal request_written
        request.write({'authorization_url': url, 'callback_port': port})
        request_written = True
        print(f'Open the authorization_url from {request.path}. Keep this terminal open.', flush=True)
    async def receive(): return await asyncio.wait_for(future, 1800)
    try:
        async with server:
            async with connect(store, redirect_uri=f'http://127.0.0.1:{port}/callback',
                               redirect_handler=redirect, callback_handler=receive) as client:
                result = await client.list_tools()
                values = store.read()
                values['tool_contracts'] = contracts(result.tools)
                store.write(values)
                rows = [t.model_dump(mode='json') for t in result.tools if t.name in TOOLS]
                Storage(store.path.with_name('spaceship-official-tools.json')).write(rows)
                print('Spaceship official tools verified:', len(rows), flush=True)
    finally:
        if request_written:
            request.path.unlink(missing_ok=True)


def main():
    try: asyncio.run(login())
    except Exception: raise SystemExit('Spaceship OAuth not completed; details suppressed') from None


if __name__ == '__main__': main()
