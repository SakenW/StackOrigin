# StackOrigin（源栈）

StackOrigin 是本地运维工作台。用 Web 面板巡检账单和资产、跟踪网站与服务器状态、查看异常证据。CLI 和 MCP 提供账号、资源和操作查询。需要变更时，先核对预览，再按对应操作的确认流程执行。

## 本地试用

在 macOS 或 Linux 上安装 Python 3.11+，从本目录运行以下命令。

```sh
python3 -m venv .venv
bash tools/pip-cache.sh .venv/bin/python install .
.venv/bin/stackorion-dashboard --development --port 8766 --state-dir "$PWD/.stackorion/demo"
```

在浏览器访问 [本地工作台](http://127.0.0.1:8766/)，确认勾选「显示模拟数据」，点击「开始巡检」。要使用真实的只读连接，按[安装指南](src/stackorion/INSTALL.md)另选状态目录并启动，再在「设置 → 服务接入」登记账号。

在本机 macOS 上，安装入口将 pip 下载缓存放在工作盘的 `/Volumes/工作盘/.缓存/依赖/darwin-<架构>/pip`。虚拟环境留在各工作树。可在联网时用相同平台和 Python 版本预备离线安装所需的 wheels。

```sh
# wheelhouse 指向工作盘上专用于这次安装的目录
bash tools/pip-cache.sh .venv/bin/python wheel --wheel-dir "$wheelhouse" .
bash tools/pip-cache.sh .venv/bin/python install --no-index --find-links "$wheelhouse" "$wheelhouse"/stackorion-*.whl
```

## 网站监控

在域名详情登记 HTTPS URL 后，可立即检测并查看 DNS、TLS、HTTP 分段、证书和历史。设置检测间隔或暂停计划。面板区分未采到的数据和已确认的异常。

## 服务器监控

在服务器详情登记已有采集入口，或从服务器监控页选择自管主机。面板按目标记录 CPU、内存、磁盘和网络的趋势。采集器按 [上游插件规范](docs/upstream-plugins.md) 提供目标身份和指标，主机连接使用私有配置。

## CLI、MCP 与插件

源码带有[服务目录模板](config/services.example.json)和[离线插件样例](docs/upstream-plugins.md#跑通独立示例)。CLI 与 MCP 使用 `STACKORION_CONFIG` 加载已登记的服务。部分厂商能力依赖外部工具、用户提供的脚本或 Foundation 环境，登记前根据模板确认所需入口。

在终端也可以检查面板并运行合成巡检。

```sh
export STACKORION_DASHBOARD_URL=http://127.0.0.1:8766
.venv/bin/stackorion doctor
.venv/bin/stackorion inspection run --include-demo
.venv/bin/stackorion work-items --include-demo
```

DeepSeek Harness 交接插件源文件位于 [integrations/deepseek-harness](integrations/deepseek-harness)。运行 `npm pack ./integrations/deepseek-harness` 生成安装包，在 Harness 的插件页安装本地包并启用。启动 Harness 的进程需能从 `PATH` 找到 `stackorion-dashboard-mcp`。

要加载配套的 [交接 Skill](integrations/deepseek-harness/skills/stackorion-handoff/SKILL.md)，将 `stackorion-handoff` 目录复制到 Harness 的 `$DSH_HOME/skills/`，默认是 `~/.dsh/skills/`，再重启该 profile。

## 本地验证

完整运行随源码附带的离线测试前，安装腾讯云可选 SDK。加密备份用例使用系统已安装的 `age`。

```sh
bash tools/pip-cache.sh .venv/bin/python install '.[tencent]'
.venv/bin/python -m unittest discover -s tests
```

使用合成数据或独立测试账号。密钥、真实账号配置和回执放在项目目录外。面板界面的更新遵循[设计约定](DESIGN.md)。

## 许可证

项目采用 [MIT 许可证](LICENSE)。随项目分发的 Lucide 和 uPlot 各自保留 [ISC 许可证](src/stackorion/web/lucide-license.txt)和 [MIT 许可证](src/stackorion/web/uplot-license.txt)。
