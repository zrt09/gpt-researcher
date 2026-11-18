# Changelog

本文件按改造迭代记录；Python 包版本为 `0.17.0+adaptive.1`。

## [1.2.0] - 2026-11-16

### Added
- 原网页报告类型新增 “Adaptive - 自适应研究” 选项。
- WebSocket 适配器：把轮次、子任务、指标、决策以 `logs` 消息推送；报告最后一次性送达。

### Changed
- 开发过程中曾提供的 `/adaptive-ui` 调试页已在最终交付版移除，只保留原网页入口；同步删除其测试。

### Docs
- 新增 16 号（报告结构与网页入口）、17 号（原网页接入自适应）文档。
- 更新已知问题清单。

## [1.1.0] - 2026-11-16

### Changed
- 写作器升级为“大纲 → 分节 → 摘要/结论 → 装配”：单一 H1、章节编号、确定性参考文献、双语附录。
- 新增引用净化：未收集的 URL 改指证据或降级为纯文本。
- 中文任务归属、引用抽取、句子切分修复。
- 写作侧覆盖缺口改为“重写报告”，不再无限补研。
- 调整计划规模与门槛：`ADAPTIVE_MAX_TASKS=6`、`ADAPTIVE_PLAN_TASKS=5`、`ADAPTIVE_MIN_COVERAGE=0.70`。

### Fixed
- 引用 URL 在 `?` 处被截断、清洗时丢失 `_` 等问题。
- 达标轮次被预算检查抢先判为 `max_rounds` 的问题。

## [1.0.0] - 2026-11-14

### Added
- 自适应研究循环：Planner / Executor / Evaluator / Reflection / Budget / Orchestrator。
- `ResearchState` 单一状态：计划、证据、结论、轮次、预算、报告版本。
- 8 项质量指标，`light`/`full` 两种评估模式，质量门。
- 五类预算上限与 9 种停止原因。
- 报告质量自检附录：达标版与 5 段式未达标版。
- CLI `--adaptive`、`--adaptive_rounds`。
- 新增改造测试（最终保留 175 个用例）；对照实验报告与使用说明。

### Fixed
- 真实运行暴露的 5 个缺陷：引用清洗丢失 URL、`state.claims` 未回填、最后一轮达标误判、写作上下文超限、子任务重复 `choose_agent`。
