---
name: stackorion-handoff
description: 读取 StackOrigin 最近巡检与待办、为指定资源准备操作预览，或按操作编号核对回执。用于巡检覆盖核对及面板、MCP 之间的操作交接。
---

# StackOrigin 操作交接

- 用 `mcp__stackorion__get_dashboard_inspection` 看最近一次巡检的账号覆盖和命中数。需要新结果时，请用户从面板发起巡检。仅核对演示时设置 `include_demo: true` 并标明结果含模拟数据。
- 用 `mcp__stackorion__get_dashboard_work_item` 读取明确的待办编号。报告证据和账号，使用返回的 `operation_binding.id` 定位同一资源。用户只给出资源名称时，用 `mcp__stackorion__list_dashboard_operation_targets` 按账号、资源和动作查找对应编号。匹配到多个目标时请用户确认。
- 用户明确要求预览某项变更后，调用 `mcp__stackorion__prepare_dashboard_operation`。QVM 普通重启传入 `parameters: {}`，新增单条 A 记录需提供 `name`、`content` 和 `ttl`。从待办交接时带上原 `work_item_id`。待办仅用于定位证据，它的类型不能作为选择操作的依据。
- 将返回的 `record_url` 和对应账号交还用户。合成演练在面板确认，真实模式使用面板查看预览。
- 用 `mcp__stackorion__get_dashboard_operation` 按原 `record_id` 查回执。分别记录预览、已受理、已验证和待核对。遇到待核对时，把原编号交给用户，在面板查看回执和资源状态。沿用同一条记录核对，停止提交新预览或重试执行。
