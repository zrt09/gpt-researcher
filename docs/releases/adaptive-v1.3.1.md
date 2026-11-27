# Adaptive v1.3.1 — 网页控件与文档同步

> 上一版：`adaptive-v1.2.0`（原网页支持 Adaptive 报告类型）
> 本版：`adaptive-v1.3.1`（网页表单控件 + README 截图/说明 + 发布文档）
> 说明：功能代码在 `adaptive-v1.3.0` 已合入；本标签补齐文档、截图与发布说明。

## 亮点

### 1. 选中 Adaptive 后，表单自动切换

- 隐藏：`Tone`（语气）、`Report Source`（来源）——自适应写作固定客观语气，且只做 Web 调研；
- 保留并真正生效：`每查询抓取站点数`、`Query Domains`；
- 新增：`最多轮次`（1~5）、`评估模式`（light / full）；
- 切回其它报告类型立即恢复原版表单，无需刷新页面。

### 2. 参数真正落到检索层

- `每查询抓取站点数` → 每个子研究的 `max_search_results_per_query`；
- `Query Domains` → 每个子研究的 `query_domains`；
- 后端对浏览器传入值做边界校验（轮次 1~5、站点数 1~20、模式白名单），非法值回退配置默认。

### 3. 退出条件与报告

- 每轮评估后按“质量达标 / 预算触顶 / 连续无进展”停止；
- 达标时报告附紧凑质量自检表；未达标时附 5 段式缺口说明；
- 报告结构：单一 H1、章节编号、固定参考文献；附录语言跟随正文。

## 变更清单

- `feat(web)`：Adaptive 模式下的表单切换（轮次/评估模式），站点数与域名生效
  （`frontend/index.html`、`frontend/scripts.js`、`server_utils.py`、`websocket_manager.py`、
  `adaptive_report.py`、`orchestrator.py`、`executor.py`）
- `test`：新增 5 条用例（参数透传、边界夹取、执行器生效、前端控件、协议字段）
- `docs`：README_ADAPTIVE 截图替换与说明更新；新增本发布说明

## 验证

| 项 | 结果 |
|---|---|
| 单元测试（adaptive 相关） | 172 个用例收集 |
| 全量测试 | 1063 passed / 2 failed（原项目既有日志测试）/ 17 skipped |
| 真实网页链路 | 1 轮、每查询 2 站点、19 条证据、$0.0374、md/docx/json 下载正常 |

真实链路日志片段：

```text
🚀 自适应模式：最多 1 轮、120 次模型调用；每查询抓取 2 个站点；域名 不限；评估模式 light
🔄 第 1 轮研究开始
📄 Scraped 2 pages of content
🏁 研究结束：已完成（质量达标）（1 轮，$0.0374）
```

## 升级说明

- 无需修改配置；新增的两个请求字段只对 `report_type=adaptive` 生效，原版路径完全不变。
- 若从 `adaptive-v1.2.0` 升级：只需拉取 `main`，网页表单会自动出现新控件。
- 已有 `outputs/adaptive/*/state.json` 不受影响。

## 已知限制

- 自适应模式只支持 Web 检索；`Tone` / `Sources` 在该模式下隐藏。
- MCP 配置暂不传入自适应流程。
- PDF 导出在 Windows 缺少 GTK 依赖时为空（md/docx/json 正常）。
- 来源时效性通常为 `None`（抓取阶段未回填发布日期）。
- 模型调用/token 计数可能偏低；成本估算基于 OpenAI 价目表，DeepSeek 场景下金额为上界。

## 相关文档

- [README_ADAPTIVE.md](../../README_ADAPTIVE.md)
- [原网页接入自适应](../改造方案/17_原网页接入自适应.md)
- [使用说明与已知问题](../改造方案/15_使用说明与已知问题.md)
- [对照实验报告](../改造方案/14_对照实验报告.md)
