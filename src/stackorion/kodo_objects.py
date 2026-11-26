"""Scoped Kodo object queries and streaming transport; no MCP dependency."""

import base64
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request

from .catalog import CatalogError, ID
from .qiniu import _regular_owned
from .qiniu_kodo import QiniuKodoAdapter

OBJECT_FIELDS = ("key", "hash", "fsize", "mimeType", "putTime", "type", "status", "restoreStatus")
REGIONS = {"cn-east-1", "cn-east-2", "cn-north-1", "cn-south-1", "us-north-1",
           "ap-southeast-1", "ap-southeast-2", "ap-southeast-3"}


def object_key(value, *, allow_empty=False):
    if (not isinstance(value, str) or (not value and not allow_empty) or len(value.encode()) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or any(s in {".", ".."} for s in value.split("/"))):
        raise CatalogError("invalid Kodo object key or prefix")
    return value


def page_marker(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CatalogError("invalid Kodo pagination marker")
    return value


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_hashes(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise CatalogError("transfer file must be a regular file without links")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if file_identity(os.fstat(fd)) != file_identity(before):
            raise CatalogError("transfer file changed before hashing")
        sha256, parts = hashlib.sha256(), []
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                sha256.update(chunk)
                parts.append(hashlib.sha1(chunk).digest())
        if not parts:
            parts = [hashlib.sha1(b"").digest()]
        etag = b"\x16" + parts[0] if len(parts) == 1 else b"\x96" + hashlib.sha1(b"".join(parts)).digest()
        if file_identity(os.fstat(fd)) != file_identity(before) or file_identity(path.lstat()) != file_identity(before):
            raise CatalogError("transfer file changed during hashing")
        return {"size": before.st_size, "sha256": sha256.hexdigest(), "hash": base64.urlsafe_b64encode(etag).decode(),
                "file_identity": list(file_identity(before))}
    finally:
        os.close(fd)


def safe_directory(path):
    if (not path.is_absolute() or str(path) != os.path.normpath(str(path))
            or any(p.is_symlink() for p in [path, *path.parents]) or not path.is_dir()
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise CatalogError("transfer directory must be an absolute private directory without symlinks")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class KodoObjectsAdapter:
    def change_key(self, client, bucket, key, destination_key, *, rename=False):
        source = base64.urlsafe_b64encode(f'{bucket}:{key}'.encode()).decode()
        destination = base64.urlsafe_b64encode(f'{bucket}:{destination_key}'.encode()).decode()
        action = 'move' if rename else 'copy'
        # Server-side no-overwrite flag; never use safe retries for a mutation.
        return client._request('POST', client.rs_host, f'/{action}/{source}/{destination}/force/false')

    def __init__(self, settings):
        expected = {"metadata_cli_path", "credential_file", "foundation_root", "targets",
                    "upload_roots", "download_root", "max_transfer_bytes"}
        if not isinstance(settings, dict) or set(settings) != expected:
            raise CatalogError("invalid Kodo object settings")
        for name in ("metadata_cli_path", "credential_file", "foundation_root", "download_root"):
            if not isinstance(settings[name], str) or not Path(settings[name]).is_absolute():
                raise CatalogError("Kodo object paths must be absolute")
        roots = settings["upload_roots"]
        if not isinstance(roots, list) or not roots or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
            raise CatalogError("invalid upload roots")
        if type(settings["max_transfer_bytes"]) is not int or not 0 < settings["max_transfer_bytes"] <= 5 * 1024**3:
            raise CatalogError("invalid transfer size limit")
        if not isinstance(settings["targets"], list):
            raise CatalogError("invalid Kodo object targets")
        self.targets = {}
        for row in settings["targets"]:
            if (not isinstance(row, dict) or set(row) != {"alias", "bucket", "s3_bucket"}
                    or not all(isinstance(v, str) for v in row.values())
                    or not ID.fullmatch(row["alias"]) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", row["bucket"])
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", row["s3_bucket"])
                    or row["alias"] in self.targets or any(t["bucket"] == row["bucket"] for t in self.targets.values())):
                raise CatalogError("invalid or duplicate Kodo object target")
            self.targets[row["alias"]] = row.copy()
        self.credential = Path(settings["credential_file"])
        self.foundation_root = Path(settings["foundation_root"])
        self.upload_roots = [Path(p) for p in roots]
        self.download_root = Path(settings["download_root"])
        self.limit = settings["max_transfer_bytes"]
        self.metadata = QiniuKodoAdapter({"cli_path": settings["metadata_cli_path"],
                                         "credential_file": settings["credential_file"],
                                         "targets": [{"alias": t["alias"], "bucket": t["bucket"]} for t in self.targets.values()]})

    def list_targets(self):
        return sorted(self.targets)

    def discover(self):
        return self.metadata.discover()

    def inspect(self, alias):
        return self.metadata.inspect(alias)

    def foundation(self):
        source = self.foundation_root / "scripts/foundation-kodo.py"
        _regular_owned(source, secret=False)
        if any(p.is_symlink() for p in [self.foundation_root, *self.foundation_root.parents]):
            raise CatalogError("Foundation root must not use symlinks")
        spec = importlib.util.spec_from_file_location("stackorion_foundation_kodo", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Per-module HTTP facade: do not patch the shared urllib module or forward
        # signed requests through redirects, including HTTPS -> HTTP redirects.
        def signed_urlopen(request, *, timeout=60, context=None):
            opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))
            return opener.open(request, timeout=timeout)
        module.urllib = SimpleNamespace(parse=urllib.parse, error=urllib.error,
                       request=SimpleNamespace(Request=urllib.request.Request, urlopen=signed_urlopen))
        return module, hashlib.sha256(source.read_bytes()).hexdigest()

    def credentials(self):
        _regular_owned(self.credential, secret=True)
        if self.credential.stat().st_size > 8192:
            raise CatalogError("Kodo credential file is too large")
        values = {}
        for raw in self.credential.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                raise CatalogError("invalid Kodo credential format")
            name, value = line.split("=", 1)
            if name.strip() in {"QINIU_AK", "QINIU_SK"}:
                name, value = name.strip(), value.strip().strip("\"'")
                if name in values or not re.fullmatch(r"[A-Za-z0-9_-]{10,512}", value):
                    raise CatalogError("invalid Kodo credential value")
                values[name] = value
        if set(values) != {"QINIU_AK", "QINIU_SK"}:
            raise CatalogError("Kodo credentials are missing")
        return values["QINIU_AK"], values["QINIU_SK"]

    def endpoints(self, bucket, access_key):
        url = "https://uc.qiniuapi.com/v4/query?" + urllib.parse.urlencode({"ak": access_key, "bucket": bucket})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(url, timeout=20) as response:
                raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError()
            hosts = json.loads(raw).get("hosts", [])
            if len(hosts) != 1 or not isinstance(hosts[0], dict):
                raise ValueError()
            data = hosts[0]
            result = {k: data[k]["domains"][0] for k in ("rs", "rsf", "up")}
            for host in result.values():
                if (not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host)
                        or not host.endswith((".qbox.me", ".qiniuapi.com", ".qiniup.com"))):
                    raise ValueError()
            region = data["s3"]["region_alias"]
            if region not in REGIONS:
                raise ValueError()
            result["s3_region"] = region
            result["s3_host"] = "s3." + region + ".qiniucs.com"
            if result["s3_host"] not in data["s3"]["domains"]:
                raise ValueError()
            return result
        except Exception as exc:
            raise CatalogError("Kodo region endpoints could not be verified") from exc

    def context(self, alias):
        if alias not in self.targets:
            raise CatalogError("unknown Kodo object target")
        target = self.targets[alias]
        owned = self.metadata.discover()
        if target["bucket"] not in {r["bucket"] for r in owned["data"]}:
            raise CatalogError("Kodo bucket is not owned by configured credential")
        module, fingerprint = self.foundation()
        ak, sk = self.credentials()
        endpoints = self.endpoints(target["bucket"], ak)
        client = module.QiniuClient(access_key=ak, secret_key=sk, rs_host=endpoints["rs"],
                                    rsf_host=endpoints["rsf"], up_host=endpoints["up"])
        return target, client, module, fingerprint, endpoints

    @staticmethod
    def metadata_result(value, key):
        if value is None:
            return None
        if (not isinstance(value, dict) or not isinstance(value.get("hash"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", value["hash"])
                or type(value.get("fsize")) is not int or value["fsize"] < 0):
            raise CatalogError("Kodo object metadata is incomplete")
        return {"key": key, **{k: value[k] for k in OBJECT_FIELDS if k != "key" and k in value}}

    def stat_with_client(self, client, bucket, key):
        try:
            return self.metadata_result(client.stat(bucket, key), key)
        except Exception as exc:
            raise CatalogError("Kodo object metadata query failed") from exc

    def stat_object(self, alias, key):
        object_key(key)
        target, client, *_ = self.context(alias)
        value = self.stat_with_client(client, target["bucket"], key)
        return {"status": "observed", "target": alias, "bucket": target["bucket"], "exists": value is not None, "object": value}

    def list_objects(self, alias, prefix="", marker="", limit=100):
        object_key(prefix, allow_empty=True)
        page_marker(marker)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CatalogError("Kodo page size must be between 1 and 1000")
        target, client, *_ = self.context(alias)
        params = {"bucket": target["bucket"], "prefix": prefix, "limit": str(limit)}
        if marker:
            params["marker"] = marker
        try:
            payload = client._request("GET", client.rsf_host, "/list", params=params, retry_safe=True)
            rows = payload["items"]
            next_marker = page_marker(payload.get("marker") if payload.get("marker") is not None else "")
            if not isinstance(rows, list) or len(rows) > limit or not isinstance(next_marker, str) or (marker and next_marker == marker):
                raise ValueError()
            items = []
            for row in rows:
                key = object_key(row["key"])
                if not key.startswith(prefix) or key in {r["key"] for r in items}:
                    raise ValueError()
                items.append(self.metadata_result(row, key))
            return {"status": "observed", "coverage": "one bounded page; continue with next_marker",
                    "target": alias, "bucket": target["bucket"], "prefix": prefix,
                    "items": items, "returned_count": len(items), "next_marker": next_marker, "has_more": bool(next_marker)}
        except Exception as exc:
            raise CatalogError("Kodo object page could not be verified") from exc

    def upload_source(self, raw):
        if not isinstance(raw, str):
            raise CatalogError("invalid upload source")
        path = Path(raw)
        if not path.is_absolute() or raw != os.path.normpath(raw) or any(p.is_symlink() for p in [path, *path.parents]):
            raise CatalogError("upload source must be absolute without symlinks")
        for root in self.upload_roots:
            safe_directory(root)
        if not any(path.is_relative_to(root) for root in self.upload_roots):
            raise CatalogError("upload source is outside configured staging roots")
        if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > self.limit:
            raise CatalogError("upload source is empty, missing or exceeds transfer limit")
        return path

    def output_path(self, filename):
        safe_directory(self.download_root)
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", filename):
            raise CatalogError("download filename must be one safe filename")
        path = self.download_root / filename
        if path.exists() or path.is_symlink():
            raise CatalogError("download destination already exists")
        return path

    def download_stream(self, client, module, target, endpoints, key, handle, size):
        uri = "/" + target["s3_bucket"] + "/" + urllib.parse.quote(key, safe="/")
        auth, date, body_hash = module.s3_authorization(method="GET", host=endpoints["s3_host"],
                   region=endpoints["s3_region"], uri=uri, access_key=client.access_key, secret_key=client.secret_key)
        connection = http.client.HTTPSConnection(endpoints["s3_host"], timeout=120)
        try:
            connection.request("GET", uri, headers={"Authorization": auth, "x-amz-date": date,
                                                    "x-amz-content-sha256": body_hash})
            response = connection.getresponse()
            if response.status != 200:
                raise CatalogError("Kodo download did not return HTTP 200")
            count = 0
            while chunk := response.read(min(1048576, size - count + 1)):
                count += len(chunk)
                if count > size:
                    raise CatalogError("Kodo download exceeded approved size")
                handle.write(chunk)
            if count != size:
                raise CatalogError("Kodo download was incomplete")
        except Exception as exc:
            raise CatalogError("Kodo download could not be verified") from exc
        finally:
            connection.close()
