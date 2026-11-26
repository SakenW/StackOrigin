"""Bounded process I/O for private, provider-owned finite CLI dispatchers."""
import os
import selectors
import subprocess
import time

from .catalog import CatalogError


def run(argv, *, cwd, env, input_data=b'', timeout=25, limit=2_097_152, accepted_failure=None):
    process = None
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdin.write(input_data); process.stdin.close()
        chunks, errors, size = [], [], 0
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError()
                for event, _ in selector.select(min(remaining, .25)):
                    data = os.read(event.fileobj.fileno(), 65536)
                    if not data: selector.unregister(event.fileobj)
                    else:
                        size += len(data)
                        if size > limit: raise ValueError()
                        if event.fileobj is process.stdout: chunks.append(data)
                        else: errors.append(data)
        process.wait(timeout=max(.01, deadline-time.monotonic()))
        if process.returncode != 0 and not (accepted_failure and
                accepted_failure(process.returncode,b''.join(chunks),b''.join(errors))):
            raise ValueError()
        return b''.join(chunks)
    except Exception:
        raise CatalogError('provider CLI failed or exceeded bounds; details suppressed') from None
    finally:
        if process is not None:
            if process.poll() is None: process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream: stream.close()
