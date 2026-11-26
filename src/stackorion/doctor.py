"""Check local setup without loading provider adapters or touching persistent state."""

from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from shutil import which
import stat

from .catalog import Catalog, CatalogError, configured_path
from .dashboard_bridge import request_dashboard


def _state_directory():
    try:
        path = Path(os.environ["STACKORION_STATE_DIR"]).expanduser()
        info = path.lstat()
    except FileNotFoundError:
        return "attention", "状态目录尚未创建，首次启动时需使用可写的私有路径。"
    except (OSError, RuntimeError):
        return "error", "无法检查状态目录，请核对路径和权限。"
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not os.access(path, os.W_OK | os.X_OK)):
        return "error", "请使用当前用户拥有且可写的 0700 私有目录。"
    return "ok", "状态目录为当前用户的私有目录。"


def diagnose():
    checks = []
    adapters = set()

    def add(name, status, message, **details):
        checks.append({"check": name, "status": status, "message": message, **details})

    explicit_catalog = bool(os.environ.get("STACKORION_CONFIG"))
    if explicit_catalog:
        try:
            services = Catalog.load(configured_path()).list()
        except (CatalogError, OSError, RuntimeError):
            add("catalog", "error", "请核对 STACKORION_CONFIG 指向当前用户可读的绝对路径，并检查目录 JSON 格式。")
        else:
            enabled = [row for row in services if row["enabled"]]
            adapters.update(row["adapter"] for row in enabled if row["kind"] == "python-adapter")
            add("catalog", "ok" if enabled else "attention",
                "配置已读。" if enabled else "目录已读，当前没有启用的连接。",
                total=len(services), enabled=len(enabled))
    else:
        add("catalog", "ok", "CLI 未指定外部服务目录。可从面板的「服务接入」登记连接。")

    if os.environ.get("STACKORION_STATE_DIR"):
        add("state_dir", *_state_directory())

    address = os.environ.get("STACKORION_DASHBOARD_URL", "http://127.0.0.1:8765")
    try:
        _, panel = request_dashboard("/api/onboarding", address=address, timeout=2)
        connections = panel.get("connections")
        if type(panel.get("editable")) is not bool or not isinstance(connections, list):
            raise CatalogError("Unexpected dashboard response")
        if any(not isinstance(row, dict) or type(row.get("enabled")) is not bool for row in connections):
            raise CatalogError("Unexpected connection response")
    except CatalogError:
        add("dashboard", "attention",
            "未能读取本机面板。请启动面板，核对 STACKORION_DASHBOARD_URL 使用 http://127.0.0.1:端口。")
    else:
        enabled = [row for row in connections if row["enabled"]]
        adapters.update(row["adapter"] for row in enabled if isinstance(row.get("adapter"), str))
        add("dashboard", "ok" if enabled else "attention",
            "本机面板可访问。" if enabled else "本机面板可访问，服务接入页暂无已启用的连接。",
            connection_count=len(connections), enabled_connections=len(enabled), managed_in_panel=panel["editable"])

    for name, available, needed, message in (
        ("curl", which("curl") is not None, "spaceship-domains" in adapters, "Spaceship 只读连接需要 PATH 中的 curl。"),
        ("dig", which("/usr/bin/dig") is not None, False, "网站和公共 DNS 探测需要 /usr/bin/dig。"),
        ("ssh", which("ssh") is not None, "linux-ssh-host" in adapters, "使用 SSH 采集服务器指标时需要 PATH 中的 ssh。"),
    ):
        add(name, "ok" if available else "error" if needed else "optional_missing",
            "本机已找到此工具。" if available else message)

    sdk_required = bool(adapters & {"tencent-edgeone", "tencent-management", "tencent-cvm", "tencent-lighthouse"})
    try:
        sdk_version = version("tencentcloud-sdk-python-common")
    except PackageNotFoundError:
        add("tencent_sdk", "error" if sdk_required else "optional_missing",
            "使用腾讯云连接前，请安装带 [tencent] 可选依赖的 stackorion 包。")
    else:
        add("tencent_sdk", "ok", "已安装腾讯云 SDK。", version=sdk_version)

    states = {row["status"] for row in checks}
    status = "error" if "error" in states else "attention" if "attention" in states else "ok"
    return {"status": status, "checks": checks}
