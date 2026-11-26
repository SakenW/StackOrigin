# StackOrigin 上游插件接入规范

StackOrigin 用 Python entry point 加载上游插件。插件可以单独维护和安装。安装后，用户按账号登记服务并启用，MCP 仍使用已有的能力发现、查询、计划和回执工具。

## 解耦边界

| 层 | 扩展入口 | 职责 |
| --- | --- | --- |
| 服务目录 | 私有配置中的 `service_id`、`account_id`、`provider`、`adapter` | 绑定账号与插件，按服务启用或停用 |
| 查询 | `stackorion.adapters` | 校验配置、绑定上游身份，开放列入白名单的只读查询 |
| 变更 | `stackorion.operations` | 可选。按同一个 adapter ID 注册计划、精确执行和回执 |
| MCP | 固定的发现、查询及操作工具 | 按服务和能力路由，读取启停状态与回执归属 |
| Web 服务器监控 | 查询插件可选的 `host_target`、`host_reading` | 同一面板接入任何实现监控契约的连接器 |
| Web 日常巡检 | 现有面板采集器 | 资产和余额等仍由现有映射采集 |

官方 MCP 是插件可选的上游传输。可按账号和已登记查询选择官方 MCP、SDK、CLI、API 或正式运维入口。上游工具名、允许的参数和结果映射由插件维护。通用 `external-mcp` 配置只探测工具清单和可达性，`client-mcp` 记录客户端直连配置。需要经过 StackOrigin 路由的查询或变更，使用本页的 Python 插件入口。

## 目录与打包

```text
src/stackorion/
  plugin_api.py                 公开的 Python 类型和错误
  catalog.py                    服务与账号登记、查询插件装载
  queries.py                    查询公开契约与白名单
  operations.py                 变更注册、派发和回执归属
  server.py                     固定 MCP 工具
examples/upstream-plugins/
  example-inventory/            可独立构建的示例
```

第三方插件有自己的 `pyproject.toml`、包名、依赖、示例配置和测试，可维护在独立仓库。内置连接器仍随 `stackorion` 包交付。需要整理内置模块时，可逐个厂商迁到 `src/stackorion/providers/<provider>/`，同步其入口、能力索引和测试，沿用已登记的 adapter ID、查询 ID、操作 ID 和服务 ID。第三方包按公开契约接入。插件发布者声明已验过的 StackOrigin 版本范围，用户按经核对的来源和版本安装。

```toml
[project.entry-points."stackorion.adapters"]
example-inventory = "stackorion_example_inventory:InventoryAdapter"
```

entry point 名字就是服务配置的 `adapter`。使用稳定且可区分的 ID，并避免与其他插件重名。同一个 ID 发现多份安装时核心会拒绝装载。公共类型从 `stackorion.plugin_api` 导入，包含 `CatalogError`、`Catalog`、`Service` 和 `OperationDefinition`。

## 查询契约

- 插件类声明字符串 `provider`，与服务和账号登记的 provider 一致。构造函数接收本服务的 `settings`，核对必填项和允许字段。构造、能力列举及路由声明只使用本地配置。
- `query_methods` 是「公开查询 ID → 实现方法名」的字典。方法使用明确可按名称传入的参数，为参数标注类型。签名支持绑定检查，厂商 ID、值的类型与范围、账号和地域权限仍由插件验证。核心只派发允许的查询。
- 可用 `query_upstreams` 字典或属性为查询登记实际来源。键取自 `query_methods`，值为 `official-mcp`、`official-sdk`、`official-cli`、`official-api`、`foundation`、`custom-api` 或 `local`。能力发现返回所选来源。由配置决定的来源用本地属性计算，探测就绪性另走只读查询。
- 查询返回可 JSON 序列化的对象。完整空结果、分页不完整及权限失败分别表达。预期失败抛出只带安全说明的 `CatalogError`。返回值、错误、参数默认值和描述中只放可公开的信息。
- 可选 `discover()`、`inspect(alias)`、`list_targets()` 分别支持 CLI/MCP 的发现、已有目标查询及目标枚举。通过通用 `query_service` 访问的能力仍需登记在 `query_methods`。

`list_service_capabilities(service_id)` 返回查询签名与来源，`query_service(service_id, query_id, parameters)` 执行查询。部署者从未启用的示例登记开始，填好账号和私有凭据引用后再启用。

## 接入服务器监控面板

在同一个已注册的查询插件上实现两个只读方法，公共类型从 `stackorion.plugin_api` 导入。

| 方法 | 返回 | 责任 |
|---|---|---|
| `host_target(alias)` | `HostTarget(resource_id, product, region=None)` | 根据本地登记解析稳定目标。核心逐次比对资源 ID、产品和地域，通过后才采集 |
| `host_reading(alias)` | `HostReading(observed_at, source, values, state=None)` | 在上游核对目标身份，返回带时区的采集时间、短来源 ID 和经过筛选的数值。可附云端实例状态 |

`values` 支持 `cpu_percent`、`memory_percent`、`disk_percent`（根分区）、两种网络指标。`network_rx_kbit` 和 `network_tx_kbit` 表示同一采样窗的 kbit 流量，`network_rx_kbps` 和 `network_tx_kbps` 表示同一观测窗的 kbit/s 速率。CPU、内存和根分区的单位为百分比。无法取得的键可以省略。实例状态可填 `available`、`running`、`stopped`、`starting`、`stopping` 或 `unavailable`。配置容量只用于资产资料，使用率需来自实际观测。

在 [服务器监控配置](../README.md#服务器监控) 用 `service_id` 绑定已启用的目录服务并登记其精确目标。面板复用采集频率、历史、趋势和待办，无需为各厂商新增页面。源协议可选用厂商 API、已授权的官方 MCP 或受管的主机观测入口。

如果插件还提供 `list_targets()`，面板可列出这些采集入口供用户登记。该方法和 `host_target(alias)` 只解析本地登记，返回稳定别名与目标身份。资产详情按账号、地域和资源 ID 匹配。没有地域的自管主机可从服务器监控页直接选择。登记时只保存身份绑定，采样由 `host_reading(alias)` 在检测时执行。

### 使用内置 Linux SSH 采集器

对已登记的云主机，可在私有监控配置对应行设置 `ssh_metrics`。控制面采集通过账号和目标比对后，采集器可补充缺失指标，并保留已有指标及其来源。

```json
{
  "host": "linux.example.net",
  "port": 22,
  "user": "monitor",
  "key_file": "/private/stackorion/monitor-key",
  "known_hosts": "/private/stackorion/known_hosts"
}
```

`known_hosts` 使用独立核验过的主机公钥。走私网入口但复用原入口的公钥登记时，可用 `host_key_alias` 指向原有条目。目标还须匹配 cloud-init 的实例 ID。如果主机没有可用的 cloud-init ID，可在独立确认同一主机后设置 `machine_id_sha256`，取远端 `/etc/machine-id` 去除首尾空白后的 SHA-256。连接资料和身份摘要保存在私有配置中。

主机须已有可用的 SSH 登录和 Python 3。采集器从标准输入执行固定探针，读取内核计数器、内存统计和根分区容量。CPU 与网络基于约一秒的计数器差值，网络默认读 IPv4 默认路由接口，也可以用 `interface` 指定网卡。无法读取的指标显示「未取得」。

自管主机可在服务目录登记 `kind: python-adapter`、`provider: linux`、`adapter: linux-ssh-host`，在 `settings` 中填写 `alias`、`resource_id`、`product` 和上述 `ssh` 对象。监控目标用同一服务、别名、资源 ID 和产品登记。

## 可选变更契约

在 `stackorion.operations` 注册与查询适配器同名的 entry point。操作类接收 `Catalog`，声明 `provider` 和由 `OperationDefinition` 组成的 `definitions` 元组。每项操作使用稳定 ID。通过通用入口接入时，定义中的工具名填写 `plan_service_operation`、`apply_service_operation`、`get_service_operation`。每项操作可另给 `parameter_contract`，让客户端发现该动作的参数。需要公开选中来源时，可实现无网络调用的 `operation_upstream(service_id, operation)`。

| 方法 | 职责 |
| --- | --- |
| `plan(service_id, operation, **parameters)` | 校验精确目标与参数，读取当前状态，返回拟变更差异及确认依据。计划阶段云端变更次数为零 |
| `apply(service_id, operation, **parameters)` | 精确授权由客户端取得。插件重新核对账号、目标、计划摘要和预期状态，先写入持久派发记录再执行。重复请求返回原回执或待核对状态 |
| `receipt(operation_id)` | 读取持久回执，含 `identity.service` 和 `identity.operation`。回执查询无需启用服务，核心会核对它属于请求的服务 |

插件负责记录派发、受理、读回及未知结果。控制面受理与实际生效分别记录。结果未知时读回原状态，换用其他上游或新编号也不得绕过原操作。插件测试须覆盖错误账号、参数或状态漂移、执行前失败、响应丢失、重复请求，以及停用后读回执。先用离线替身验收。

entry point 在 StackOrigin 进程中执行，属于可信代码。操作插件可访问传入的服务目录。凭据按账号和用途分开，由进程以实际执行权限读取。安装前审查实现和依赖，使用最低权限的上游凭据，并将回执和密钥放在仓库外。

## 跑通独立示例

将示例装入按[安装指南](../src/stackorion/INSTALL.md)准备的测试虚拟环境。示例只读本地合成数据，无需凭据或网络请求。把示例配置复制到新建的私有目录，用这份配置启停服务。

```sh
.venv/bin/python -m pip install --no-deps ./examples/upstream-plugins/example-inventory
STACKORION_CONFIG=/absolute/private/example-services.json .venv/bin/stackorion enable example-inventory-local
STACKORION_CONFIG=/absolute/private/example-services.json .venv/bin/stackorion discover example-inventory-local
```

要走 MCP，将客户端的命令设为虚拟环境中的 `stackorion-mcp`，环境变量 `STACKORION_CONFIG` 指向同一份示例文件。调用顺序如下。

1. 启用前调用 `list_service_capabilities`，确认状态为 `unavailable`。
2. 启用后再次调用，确认 `inventory` 和 `target` 可发现，来源为 `local`，变更列表为空。
3. 用 `query_service` 请求 `target`，参数填写 `{"alias":"demo-site"}`。核对返回 `source: synthetic`。改用未登记的查询或目标时，应明确拒绝。
4. 停用后确认查询受限，服务仍列在目录里。

[独立包](../examples/upstream-plugins/example-inventory/pyproject.toml) · [示例配置](../examples/upstream-plugins/example-inventory/services.example.json) · [示例实现](../examples/upstream-plugins/example-inventory/src/stackorion_example_inventory/__init__.py)
