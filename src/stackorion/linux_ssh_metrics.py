"""Private, explicitly bound SSH transport for the fixed Linux metrics probe."""

from importlib.resources import files
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

from .catalog import CatalogError, ID
from .host_contract import RESOURCE_ID, HostReading, HostTarget


def _valid_host(value):
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", value))
    return True


def _check_ssh_file(path, forbidden):
    info = path.lstat()
    parent = path.parent.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & forbidden
            or parent.st_uid != os.getuid() or parent.st_mode & 0o022):
        raise CatalogError("Unsafe Linux SSH file permissions")


class LinuxSSHCollector:
    def __init__(self, settings):
        required = {"host", "user", "key_file", "known_hosts"}
        optional = {"port", "host_key_alias", "machine_id_sha256", "interface"}
        if not isinstance(settings, dict) or required - settings.keys() or settings.keys() - (required | optional):
            raise CatalogError("Invalid Linux SSH settings")
        self.host = settings["host"]
        self.user = settings["user"]
        self.port = settings.get("port", 22)
        if (not _valid_host(self.host) or not isinstance(self.user, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", self.user)
                or type(self.port) is not int or not 1 <= self.port <= 65535):
            raise CatalogError("Invalid Linux SSH target")
        for field in ("key_file", "known_hosts"):
            if not isinstance(settings[field], str) or not Path(settings[field]).is_absolute():
                raise CatalogError("Linux SSH paths must be absolute")
        self.key_file = Path(settings["key_file"])
        self.known_hosts = Path(settings["known_hosts"])
        self.host_key_alias = settings.get("host_key_alias")
        self.machine_id = settings.get("machine_id_sha256")
        self.interface = settings.get("interface", "")
        if (self.host_key_alias is not None and (not isinstance(self.host_key_alias, str)
                or not re.fullmatch(r"[A-Za-z0-9\[\]:._-]{1,260}", self.host_key_alias))):
            raise CatalogError("Invalid pinned SSH host alias")
        if self.machine_id is not None and (not isinstance(self.machine_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.machine_id)):
            raise CatalogError("Invalid machine ID digest")
        if not isinstance(self.interface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{0,15}", self.interface):
            raise CatalogError("Invalid Linux network interface")

    def read(self, resource_id):
        if not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id):
            raise CatalogError("Invalid host identity")
        _check_ssh_file(self.key_file, 0o077)
        _check_ssh_file(self.known_hosts, 0o022)
        identity = ("machine-id-sha256", self.machine_id) if self.machine_id else ("cloud-init", resource_id)
        probe = files("stackorion").joinpath("linux_ssh_probe.py").read_bytes()
        command = [
            "ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=8", "-o", "UpdateHostKeys=no",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UserKnownHostsFile=" + json.dumps(str(self.known_hosts), ensure_ascii=False),
            "-i", str(self.key_file), "-p", str(self.port), "-l", self.user,
        ]
        if self.host_key_alias:
            command.extend(("-o", "HostKeyAlias=" + self.host_key_alias))
        command.extend(("--", self.host, shlex.join(("python3", "-I", "-B", "-", *identity, self.interface))))
        try:
            process = subprocess.run(command, input=probe, capture_output=True, timeout=18, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CatalogError("Linux SSH metrics unavailable") from None
        if process.returncode or len(process.stdout) > 16_384:
            raise CatalogError("Linux SSH metrics or host identity unavailable")
        try:
            data = json.loads(process.stdout)
            metrics = data["metrics"]
            if not isinstance(metrics, dict):
                raise TypeError
            return HostReading(data["observed_at"], "linux-ssh", metrics)
        except (ValueError, KeyError, TypeError):
            raise CatalogError("Invalid Linux SSH reading") from None


class LinuxSSHAdapter:
    """Register a managed Linux host without a separate cloud catalog service."""

    provider = "linux"

    def __init__(self, settings):
        if set(settings) != {"alias", "resource_id", "product", "ssh"}:
            raise CatalogError("Linux host requires a target and SSH settings")
        for key in ("alias", "product"):
            if not isinstance(settings[key], str) or not ID.fullmatch(settings[key]):
                raise CatalogError("Invalid Linux host registration")
        if not isinstance(settings["resource_id"], str) or not RESOURCE_ID.fullmatch(settings["resource_id"]):
            raise CatalogError("Invalid host identity")
        self.alias = settings["alias"]
        self.target = HostTarget(settings["resource_id"], settings["product"])
        self.collector = LinuxSSHCollector(settings["ssh"])

    def host_target(self, alias):
        if alias != self.alias:
            raise CatalogError("Unknown Linux host target")
        return self.target

    def list_targets(self):
        return [self.alias]

    def host_reading(self, alias):
        return self.collector.read(self.host_target(alias).resource_id)
