---
name: stackorion-handoff
description: 读取 StackOrigin 巡检与待办，排查七牛 QVM 主机的 CPU 或网络曲线缺失，为指定资源准备操作预览，或按操作编号核对回执。
---

# StackOrigin 操作交接

- 用 `mcp__stackorion__get_dashboard_inspection` 看最近一次巡检的账号覆盖和命中数。需要新结果时，请用户从面板发起巡检。仅核对演示时设置 `include_demo: true` 并标明结果含模拟数据。
- 用 `mcp__stackorion__get_dashboard_work_item` 读取明确的待办编号。报告证据和账号，使用返回的 `operation_binding.id` 定位同一资源。用户只给出资源名称时，用 `mcp__stackorion__list_dashboard_operation_targets` 按账号、资源和动作查找对应编号。匹配到多个目标时请用户确认。
- 用户明确要求预览某项变更后，调用 `mcp__stackorion__prepare_dashboard_operation`。QVM 普通重启传入 `parameters: {}`，新增单条 A 记录需提供 `name`、`content` 和 `ttl`。从待办交接时带上原 `work_item_id`。待办仅用于定位证据，它的类型不能作为选择操作的依据。
- 将返回的 `record_url` 和对应账号交还用户。合成演练在面板确认，真实模式使用面板查看预览。
- 用 `mcp__stackorion__get_dashboard_operation` 按原 `record_id` 查回执。分别记录预览、已受理、已验证和待核对。遇到待核对时，把原编号交给用户，在面板查看回执和资源状态。沿用同一条记录核对，停止提交新预览或重试执行。

## 七牛主机没有曲线

1. 用已登记的 QVM 服务 ID 和主机别名定位目标。重名时先确认所属账号和地域。
2. 用服务目录 MCP（`stackorion-mcp`）的 `list_service_capabilities` 确认该服务提供 `metrics`，然后调用 `query_service`，传入 `{"service_id":"<服务ID>","query_id":"metrics","parameters":{"alias":"<主机别名>"}}`。随交接插件启动的 `stackorion-dashboard-mcp` 只提供面板交接工具。若当前只有面板 MCP，而本地 CLI 已配置相同的服务目录，可用 `stackorion metrics <服务ID> <主机别名>` 读取。两种入口均不可用时，说明需要哪个入口。
3. 按结果报告采样时间和覆盖范围。数值 `0` 是有效采样，`partial` 表示只拿到部分指标，`no_data` 表示近期没有样本，`unavailable` 时回报原因，例如 `permission_denied` 或 `region_mismatch`。内网收发是采样时段内的流量（kbit），内存和根分区用量由主机侧采集。
4. 如果七牛已有样本，但面板仍没有曲线，检查同一账号、地域和实例是否已登记到服务器监控，并查看采集计划和历史。汇报到这里查到的证据，结束本次只读排查。
