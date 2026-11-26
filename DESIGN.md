---
name: StackOrigin
description: 本地运维面板的状态、数据和组件规范
colors:
  light-bg: "oklch(97% .009 255)"
  light-paper: "oklch(100% 0 0)"
  light-muted-paper: "oklch(97.8% .007 255)"
  light-ink: "oklch(24% .036 260)"
  light-muted: "oklch(46% .036 257)"
  light-line: "oklch(90% .017 257)"
  light-control-line: "oklch(63% .039 257)"
  light-accent: "oklch(44% .159 264)"
  light-accent-soft: "oklch(95.5% .025 264)"
  light-action: "oklch(50% .181 264)"
  light-action-ink: "oklch(100% 0 0)"
  light-host-trend: "oklch(52% .123 230)"
  light-host-trend-fill: "oklch(66% .119 229 / 26%)"
  light-nav: "oklch(22% .045 264)"
  light-success: "oklch(41% .095 164)"
  light-success-bg: "oklch(96% .028 164)"
  light-success-fill: "oklch(55% .106 164)"
  light-reminder-fill: "oklch(56% .133 264)"
  light-warn: "oklch(43% .095 69)"
  light-warn-bg: "oklch(96% .035 79)"
  light-warn-fill: "oklch(59% .127 70)"
  light-high: "oklch(44% .168 22)"
  light-high-bg: "oklch(96% .026 20)"
  light-high-fill: "oklch(54% .180 22)"
  light-missing: "oklch(44% .096 306)"
  light-missing-bg: "oklch(96% .024 306)"
  light-missing-fill: "oklch(56% .114 306)"
  dark-bg: "oklch(19% .027 260)"
  dark-paper: "oklch(23% .029 260)"
  dark-muted-paper: "oklch(27% .030 260)"
  dark-ink: "oklch(94% .015 255)"
  dark-muted: "oklch(76% .030 255)"
  dark-line: "oklch(34% .032 260)"
  dark-control-line: "oklch(61% .040 260)"
  dark-accent: "oklch(81% .096 264)"
  dark-accent-soft: "oklch(34% .069 264)"
  dark-action: "oklch(80% .104 264)"
  dark-action-ink: "oklch(23% .070 264)"
  dark-host-trend: "oklch(77% .119 228)"
  dark-host-trend-fill: "oklch(69% .120 230 / 31%)"
  dark-nav: "oklch(16% .032 264)"
  dark-success: "oklch(85% .105 164)"
  dark-success-bg: "oklch(32% .051 164)"
  dark-success-fill: "oklch(72% .113 164)"
  dark-reminder-fill: "oklch(75% .100 264)"
  dark-warn: "oklch(88% .115 79)"
  dark-warn-bg: "oklch(33% .073 69)"
  dark-warn-fill: "oklch(75% .130 76)"
  dark-high: "oklch(83% .105 22)"
  dark-high-bg: "oklch(34% .088 22)"
  dark-high-fill: "oklch(72% .148 22)"
  dark-missing: "oklch(85% .105 306)"
  dark-missing-bg: "oklch(35% .076 306)"
  dark-missing-fill: "oklch(74% .115 306)"
typography:
  heading:
    fontSize: "32px"
    lineHeight: 1.22
    letterSpacing: "-0.032em"
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: "14px"
    lineHeight: 1.55
rounded:
  panel: "14px"
  control: "8px"
  badge: "6px"
components:
  button-primary:
    backgroundColor: "{colors.light-action}"
    textColor: "{colors.light-action-ink}"
    rounded: "{rounded.control}"
    height: "42px"
  button-primary-dark:
    backgroundColor: "{colors.dark-action}"
    textColor: "{colors.dark-action-ink}"
    rounded: "{rounded.control}"
    height: "42px"
---

# StackOrigin 面板设计

## Overview

面板服务于日常巡检。用户打开页面先看到需要关注的问题和本轮查到的范围，再进入资产、网站检测和待办详情。页面沿用深色导航、克制的内容底色和紧凑列表，颜色用来表达动作与状态。

## Colors

[dashboard.css](src/stackorion/web/dashboard.css) 是运行时色值的来源。上方列出用于设计工具的 OKLCH 色值。修改主题时同步两处。浅色和深色保持相同语义，跟随系统外观。状态文字、底色和图表填充分别使用对应色值，由 `data-tone` 统一映射。

| 语义 | 标记 | 用途 |
| --- | --- | --- |
| 青绿 · 正常 | `success` | 检测符合预期，账号查完，待办已解决 |
| 蓝色 · 提醒 | `reminder` | 即将到期、下一次检测等需要留意的信息 |
| 琥珀 · 警告 | `warn` | 余额低于阈值、部分检查无法完成 |
| 玫红 · 高风险 | `high` | 安全组允许公网 SSH、实例已到期、网站检测得到明确异常 |
| 灰紫 · 缺少结果 | `missing` | 账号未查到、漏测、探测结果不完整 |
| 灰色 · 中性 | `neutral` | 停用、暂停、尚无结果 |

普通文字与底色的对比度至少为 4.5:1。关键图形、状态边界和焦点标记至少为 3:1。标签用文字说明状态。紧凑的检测序列提供可辨识图标、文字图例与每项的可访问名称。

## Typography

使用系统字体栈，中文优先采用系统中文字体。正文 14 px，桌面页标题 32 px，面板标题 17 px，辅助文字通常为 11–13 px。统计值、耗时和时间用等宽数字。日期保留明确时区，耗时使用毫秒。

## Layout

桌面首屏先显示待办、网站、服务器和资产四张总览卡，每张都直达详情。下一层是每日巡检摘要和账号覆盖，完整发现、待办和趋势接在下方。监控卡按各目标最近一次记录统计，未知、未采集和读取失败各自有明确状态。宽度不超过 860 px 时，导航移到顶端，双栏合为一列。不超过 530 px 时，摘要卡和网站分段指标为两列。环境、账号和 URL 可以换行。

## Elevation & Depth

主界面以背景层、面板和边线区分区域。详情使用原生对话框与遮罩，关闭后焦点回到触发项。

## Shapes

面板圆角 14 px。按钮和输入使用 8 px 圆角，状态标签使用 6 px 圆角。交互项保留可见的键盘焦点。系统高对比模式下用边框和轮廓表达选中与状态。

## Components

- **导航与图标**使用 [dashboard.js](src/stackorion/web/dashboard.js) 中随包分发的 Lucide 1.21.0 SVG。StackOrigin 品牌符号单独保留。装饰图标隐藏于辅助技术。有操作的图标按钮使用可访问名称。[Lucide 许可](src/stackorion/web/lucide-license.txt)
- **摘要与列表**把数量、状态、账号和观测依据放在一起。筛选项使用按下状态。模拟记录随行标明，停用账户单列。
- **账号覆盖图**使用分段横条。图例列出查完、部分完成、未查到的数量。停用账号不参加本轮应查数。
- **网站阶段耗时**在同一探测内按最长阶段对齐长度，并列出 DNS、TCP、TLS、HTTP 的原始毫秒值。累计总耗时单列。未执行的阶段显示文字。
- **最近检测序列**显示最近最多 12 次检测，从早到晚排列。每个点可打开对应详情，完整记录从检测历史查看。点间距表示顺序。
- **调度与可用率**同时展示最近一次、下一次和有效结果分母。漏测单列。统计范围和暂停行为按 [README](README.md#网站监控) 的口径显示。
- **服务器趋势**用四张状态卡切换一张全宽时间序列。百分比默认使用 0–100%，选择「放大波动」后显示局部刻度并标明。网络按数据源的单位绘制，发送使用虚线。趋势线使用浅面积填充，悬停读值，拖选缩放，近 1、6、24 小时可用键盘切换。样本稀疏时显示采样点。缺采留断点，无有效值时显示空态。精确记录可从「查看采集明细」读取。图表使用随包分发的 uPlot 1.6.32。[uPlot 许可](src/stackorion/web/uplot-license.txt)
- **磁盘预估**随根分区趋势显示，使用文字说明阈值、样本数和覆盖时长。可预估时补充时点与增长速度，数据不足时说明原因。曲线仍对应实际采集值。


## Do's and Don'ts

- 保留状态文字、图标或轮廓，高对比模式下仍能操作。
- 缺失数据用「未查到」「未执行」或 `—`，零只表示确认为零。
- 新的状态表现复用已有语义色。新增图表沿用原始观测口径和单位。
