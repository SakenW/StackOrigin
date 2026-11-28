# 安装 StackOrigin（源栈）

准备 macOS 或 Linux 和 Python 3.11+。在自己的工作目录中创建虚拟环境，把示例里的文件名替换为拿到的 wheel 或源码压缩包。

```sh
python3 -m venv ./stackorion-env
./stackorion-env/bin/python -m pip install /absolute/path/to/stackorion.whl
./stackorion-env/bin/stackorion-dashboard --state-dir "$PWD/stackorion-data"
```

打开 [本地面板](http://127.0.0.1:8765/)。从「设置 → 服务接入」登记 Cloudflare 或 Spaceship 只读连接并测试权限，随后回到工作台开始巡检。Spaceship 接入需要系统 `curl`。网站探测需要 `/usr/bin/dig`。遇到端口占用时，给启动命令加 `--port 8766`，再访问相应端口。

面板打不开或连接缺少依赖时，运行 `./stackorion-env/bin/stackorion doctor`。如果改过端口或状态目录，执行前将 `STACKORION_DASHBOARD_URL` 和 `STACKORION_STATE_DIR` 设为面板实际使用的值。命令只读取本机状态，厂商凭据和权限可在「服务接入」另行核对。

面板运行后，可用 `./stackorion-env/bin/stackorion inspection` 读取最近巡检，`inspection run` 显式发起一轮只读巡检，`work-items` 列出待办编号，`work-item <编号>` 读取详情。结果为 JSON，可重定向到本地文件。

只想离线试用时，运行下面的命令。保持 `STACKORION_CONFIG` 未设置，工作台可用合成数据独立演练。

```sh
./stackorion-env/bin/stackorion-dashboard --development --port 8766 --state-dir "$PWD/stackorion-demo"
```

## 可选连接器与升级

- 使用腾讯云 SDK 时，在安装路径后加 `[tencent]`，并给整个参数加引号。
- 可选厂商工具默认使用 `~/.local/share/stackorion`。通过 `STACKORION_RUNTIME_ROOT` 指定绝对路径。已有工具或 Spaceship OAuth 会话可以继续使用原运行根目录。账密和账号配置中的绝对路径按原值读取。
- 源码包带有 `config/services.example.json`，供 CLI 或 MCP 使用。复制到私有目录，替换所需凭据与目标，再将该文件的绝对路径设为 `STACKORION_CONFIG`。希望通过面板登记连接时，启动环境无需设置这个变量。
- 使用 MCP 时，把客户端的启动命令指向虚拟环境内的 `stackorion-mcp`。只需要面板交接时，用 `stackorion-dashboard-mcp` 并将 `STACKORION_DASHBOARD_URL` 指向已启动的本机面板，例如 `http://127.0.0.1:8765`。
- 升级时在原虚拟环境安装新的包，沿用服务 ID、配置和 `--state-dir`。从虚拟环境卸载包不会删除单独保存的历史或凭据。

### 七牛 QVM 只读查询

在服务目录登记 `qiniu-qvm` 服务。将私有文件的绝对路径填入 `settings.credential_file`，凭据文件写入只读账号的 `QINIU_AK` 和 `QINIU_SK`，每行一项。文件和其所在目录分别使用 `0600`、`0700` 权限。`settings.targets` 填写 `{"alias":"我的主机","instance_id":"i-…"}` 形式的条目。仅做发现时可以填空数组。

省略 `script_path` 即可从安装包查询。CLI 使用 `stackorion discover <服务ID>` 和 `stackorion inspect <服务ID> <别名>`。MCP 使用 `discover_service`、`inspect_target`，或通过 `query_service` 调用 `inventory`、`instance`。Web 巡检通过相同配置读取实例和已登记目标的安全组。发现查询每类最多返回 1000 条，分页截断或关联资源读取失败时查看结果中的覆盖状态。

已有 `script_path` 配置继续走原脚本。使用安全组变更计划与执行入口或旧 QVM 指标采集时，保留其显式脚本配置。
