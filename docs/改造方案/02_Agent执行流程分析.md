# 02 Agent 执行流程分析（真实调用链）

> 所有行号基于本地 `main`（v0.17.0）代码实测；`file:line` 可直接定位
> 目的：说明"用户输入一个研究问题后，系统到底经过哪些函数，最终产出报告"，并标出状态在哪里产生、更新、读取

---

## 1. 四条入口链路总览

| 模式 | 入口 | 编排方式 | 报告类型 |
| --- | --- | --- | --- |
| A 标准 Web 研究 | `main.py` → FastAPI `/ws` 或 `cli.py` | **纯 async 流水线**（无图、无循环） | `research_report` / `resource_report` / `outline_report` / `custom_report` |
| B 详细报告 | 同上，`report_type=detailed_report` | 主研究 + **每子主题一个独立 Researcher 并发** | `detailed_report` |
| C 深度研究 | 同上，`report_type=deep` | **递归下降**（breadth × depth） | `deep` |
| D 多 Agent | `/api/multi_agents` 或 `multi_agents/main.py` | **LangGraph 状态图**（条件边 + 回退） | `multi_agents` |

---

## 2. 链路 A：标准 Web 研究（核心，必须吃透）

### 2.1 从 HTTP/WS 到研究启动

```
main.py（uvicorn 启动）
  └─ backend/server/app.py:410  @app.websocket("/ws") websocket_endpoint
       └─ backend/server/server_utils.py:342 handle_websocket_communication
            └─ server_utils.py:134 handle_start_command        # 解析 "start" 指令
                 └─ backend/server/websocket_manager.py:101 WebSocketManager.start_streaming
                      └─ websocket_manager.py:116 run_agent     # 按 report_type 分派
                           └─ backend/report_type/basic_report/basic_report.py:60  BasicReport(...)
                                └─ basic_report.py:72 run()
                                     ├─ GPTResearcher.conduct_research()   gpt_researcher/agent.py:353
                                     └─ GPTResearcher.write_report()       gpt_researcher/agent.py:473
```

CLI 入口等价：`cli.py:265 main()` →（`report_type` 分派）→ `BasicReport` / `DetailedReport` / `DeepResearch`。

### 2.2 研究阶段 `conduct_research()`（`agent.py:353`）

```
agent.py:353  conduct_research()
  ├─ 事件: research/start                                   (agent.py:365)
  ├─ if report_type == "deep": → _handle_deep_research()     (agent.py:373-375)   # 见链路 C
  ├─ if not (agent and role): choose_agent(...)              (agent.py:377-391)
  │      └─ actions/agent_creator.py:18 choose_agent
  │           └─ utils/llm.py:41 create_chat_completion     # 1 次 LLM，产出角色+系统提示
  ├─ self.context = ResearchConductor.conduct_research()     (agent.py:402)
  ├─ 可选：ImageGenerator.plan_and_generate_images()         (agent.py:410-421)
  └─ return self.context
```

### 2.3 研究主流程 `ResearchConductor.conduct_research()`（`skills/researcher.py:121`）

```
researcher.py:121
  ├─ 选 Agent（若上层没给）                                  (researcher.py:153-161)
  ├─ 按 report_source 分支：
  │   ├─ source_urls 指定 → _get_context_by_urls()            (researcher.py:171, 269)
  │   ├─ web（默认）    → _get_context_by_web_search()        (researcher.py:189, 322)
  │   ├─ local          → DocumentLoader + web search         (researcher.py:190-197)
  │   ├─ hybrid         → asyncio.gather(本地文档, web)       (researcher.py:199-212)
  │   ├─ azure / langchain_documents / langchain_vectorstore  (researcher.py:213-233)
  ├─ self.researcher.context = research_data                 (researcher.py:236)
  ├─ if cfg.curate_sources: SourceCurator.curate_sources()    (researcher.py:237-253)
  └─ return context
```

### 2.4 Web 检索主循环 `_get_context_by_web_search()`（`researcher.py:322`）★ 最关键

```
researcher.py:322
  1) MCP 策略（可选，默认无 MCP 检索器时整段跳过）             (researcher.py:336-386)
       fast: 只对原始 query 跑一次并缓存
       deep: 每个子查询都跑
       disabled: 不跑
  2) initial_results = _get_initial_search_results(query)      (researcher.py:389 → 59)
       └─ actions/query_processing.py:44 get_search_results
            └─ retriever.search(...)  （asyncio.to_thread 包装，避免阻塞事件循环）
  3) sub_queries = plan_research(query, search_results)        (researcher.py:390 → 86)
       └─ actions/query_processing.py:158 plan_research_outline
            └─ query_processing.py:83 generate_sub_queries
                 └─ create_chat_completion(strategic_llm)      # 1 次 LLM 生成 N 条查询
                 └─ query_processing.py:12 _normalize_sub_queries  # 容错 JSON
  4) if report_type != "subtopic_report": sub_queries.append(query)   (researcher.py:394-395)
  5) 规划来源补充：initial_context = _get_context_from_initial_results(...)  (researcher.py:411-415 → 868)
  6) context = await asyncio.gather(*[_process_sub_query(q) for q in sub_queries])   (researcher.py:416-421)
  7) " ".join(非空结果) → 返回字符串；异常时 log + 返回 []      (researcher.py:424-432)
```

**要点**：并发是"对子查询 gather"，**不是**"计划→执行→评估→再计划"的循环。每条子查询彼此独立、无反馈。

### 2.5 单条子查询 `_process_sub_query()`（`researcher.py:544`）

```
researcher.py:544
  ├─ MCP 去重/复用（可选）                                    (researcher.py:561-627)
  ├─ scraped_data = _scrape_data_by_urls(sub_query)           (researcher.py:631 → 1019)
  │    └─ _search_relevant_source_urls()                      (researcher.py:883)
  │         ├─ 每条检索器 retriever.search()                  (researcher.py:905)
  │         ├─ 区分"需要抓取的 URL" vs "检索器已带全文"        (researcher.py:924-987)
  │         ├─ _get_new_urls()：以 visited_urls 去重并登记     (researcher.py:845, 999)
  │         ├─ random.shuffle()                               (researcher.py:1015)
  │         └─ emit: candidate_discovered / selected_for_fetch / candidate_deduplicated
  │    └─ BrowserManager.browse_urls(urls)                    (researcher.py:1048 → skills/browser.py:37)
  │         └─ scraper/scraper.py:116 Scraper（按 SCRAPER 配置选实现，并发受 MAX_SCRAPER_WORKERS 限制）
  │    └─ 可选 vector_store.load(scraped_content)              (researcher.py:1053-1054)
  ├─ web_context = ContextManager.get_similar_content_by_query(sub_query, scraped_data)  (researcher.py:636)
  │    └─ context/select.py:53 select_context
  │         └─ context/compression.py:148 ContextCompressor.async_get_context
  │              ├─ 小文档快路径（< COMPRESSION_THRESHOLD=8000 字符）直接返回
  │              └─ 否则 切块 → EmbeddingsFilter(阈值 SIMILARITY_THRESHOLD=0.42) → pretty_print
  ├─ combined = _combine_mcp_and_web_context(...)             (researcher.py:640 → 771)
  └─ return combined   （异常 → log + 返回 ""）               (researcher.py:678-687)
```

### 2.6 报告阶段 `write_report()`（`agent.py:473`）

```
agent.py:473 write_report(existing_headers, relevant_written_contents, ext_context, custom_prompt)
  └─ ReportGenerator.write_report()                            (skills/writer.py:49)
       └─ actions/report_generation.py:209 generate_report
            ├─ get_prompt_by_report_type(report_type)          (actions/report_generation.py:250)
            ├─ 拼 prompt（context 全文塞入）                    (:253-258)
            └─ create_chat_completion(smart_llm, stream=True)  (:292-306)   # 1 次 LLM 出整篇
  └─ （CLI/后端）add_references(report, visited_urls)          (actions/markdown_processing.py:104)
```

### 2.7 全链路数据形态

| 阶段 | 产出 | 类型 | 去向 |
| --- | --- | --- | --- |
| 搜索 | `search_results` | `list[dict]`（`href/url/body/raw_content/title`） | 规划 prompt、抓取候选 |
| 规划 | `sub_queries` | `list[str]` | 并发执行，**不落库** |
| 抓取 | `scraped_content` | `list[dict]`（`url/raw_content/title`） | 压缩输入、vector store |
| 压缩 | `web_context` | `str` | 拼接 |
| 汇总 | `self.context` | `str`（web 模式）/ `list`（vectorstore 模式） | 报告 prompt、日志 |
| 报告 | `report` | `str`（markdown） | 落盘 + References |

> 这个"字符串一路传到底"的形态是后续质量评估的**主要障碍**：评估"结论是否有证据支撑"需要能定位到来源片段，而 `self.context` 已经把来源结构压扁成一段文本。

---

## 3. 链路 B：`detailed_report`

```
backend/report_type/detailed_report/detailed_report.py:84 run()
  ├─ _initial_research()                         → GPTResearcher.conduct_research()（与链路 A 相同）
  ├─ _get_all_subtopics()                        → ReportGenerator.get_subtopics()（writer.py:214，1 次 LLM）
  ├─ write_introduction()                        → writer.py:183
  ├─ _generate_subtopic_reports(subtopics)       (detailed_report.py:110)
  │    └─ asyncio.create_task(_research_subtopic(st))  ← 每个子主题一个独立 GPTResearcher（并发）
  │         └─ _research_subtopic → _write_subtopic_report（writer.py，带 existing_headers 去重）
  └─ _construct_detailed_report(intro, body)
```

特点：**并发 + 去重**（`existing_headers`、`relevant_written_contents`、共享 `visited_urls`），但每个子报告仍是一次成型，无质量门禁。

---

## 4. 链路 C：`deep`（递归研究，原项目最接近"自适应"的部分）

```
agent.py:373 → _handle_deep_research()                       (agent.py:425)
  └─ DeepResearchSkill.run()                                 (skills/deep_research.py:597)
       ├─ generate_research_plan(query)                      (deep_research.py:306)  # 生成追问问题
       ├─ 拼 combined_query（初始问题 + 追问/自答）
       ├─ deep_research(query, breadth, depth)               (deep_research.py:396)  # ★递归
       │    ├─ generate_search_queries(query, breadth)        (deep_research.py:272)  # N 条 {query, researchGoal}
       │    ├─ 每条 query 并发（Semaphore=concurrency_limit）：
       │    │    ├─ new GPTResearcher(...).conduct_research()  (deep_research.py:452)  # 完整链路 A
       │    │    ├─ process_research_results(context)          (deep_research.py:361)  # 抽取 learnings + followUpQuestions + citations
       │    │    └─ return {learnings, followUpQuestions, researchGoal, citations, context, sources}
       │    ├─ if depth > 1: next_query = 研究目标 + 追问 → 递归 deep_research(breadth//2, depth-1)   (deep_research.py:551-571)
       │    └─ trim_context_to_word_limit(25000)               (deep_research.py:226, 586)
       └─ 把 learnings 按引用拼成 context 交回主流程            (deep_research.py:630-658)
```

**已有的"自适应"要素**（重要，避免重复造）：

1. 每轮从结果里抽 `learnings` + `followUpQuestions`，据此构造下一轮查询 —— 这是**基于内容的动态下钻**；
2. `visited_urls` 跨轮共享，避免重复抓取；
3. `depth/breadth` 提供粗粒度预算；
4. 每层失败（0 结果）会提前停止，避免空转（`deep_research.py:513-531`）。

**缺口**：判断"继续/停止"只依赖"是否还有追问"，**没有质量评估、没有 token/时间预算、没有计划版本、没有明确停止原因**；且 depth/breadth 是固定衰减，不是按信息缺口调整。

---

## 5. 链路 D：`multi_agents`（LangGraph）

```
multi_agents/main.py:69 run_research_task → ChiefEditorAgent.run_research_task
  └─ agents/orchestrator.py:149 run_research_task()
       ├─ init_research_team() → _create_workflow()                 (orchestrator.py:65)
       │    StateGraph(ResearchState)，节点：
       │      browser   : ResearchAgent.run_initial_research        (agents/researcher.py:41)
       │      planner   : EditorAgent.plan_research                 (agents/editor.py:27)
       │      human     : HumanAgent.review_plan                    （可配置是否真正等人）
       │      researcher: EditorAgent.run_parallel_research         (agents/editor.py:57)
       │      writer    : WriterAgent.run
       │      fact_checker: FactCheckerAgent.run                    (agents/fact_checker.py:34)
       │      visualizer: VisualizerAgent.run
       │      publisher : PublisherAgent.run
       ├─ 条件边：human --accept/revise--> researcher/planner       (orchestrator.py:94-98)
       ├─ 条件边：fact_checker --accept/revise--> visualizer/writer (orchestrator.py:101-105)
       └─ chain.ainvoke({"task": task}, config={thread_id})          (orchestrator.py:171)
```

两条回退边都**有上限保护**（`max_plan_revisions` / `max_fact_check_revisions`，超限强制 accept），避免 LangGraph 递归爆炸——这是很好的"有界循环"范式，值得在功能 D 中借鉴。

---

## 6. 状态生命周期（标准模式）

| 时点 | 状态变化 | 代码位置 |
| --- | --- | --- |
| 构造 | 初始化 `context=[]`、`visited_urls=set()`、`research_costs=0.0`、`step_costs={}` | `agent.py:156-174` |
| 规划 | `sub_queries` 仅存活于局部变量 | `researcher.py:390` |
| 搜索/抓取 | `visited_urls`、`research_sources` 增长 | `researcher.py:853-955` |
| 压缩 | 无状态（纯函数） | `context/compression.py:148` |
| 汇总 | `context` 被整体赋值 | `researcher.py:236` |
| LLM 调用 | `research_costs`、`step_costs[_current_step]` 累加 | `agent.py:795-816` |
| 报告 | 读 `context`，不写状态 | `writer.py:49` |
| 结束 | 实例被丢弃，**状态不可回放** | — |

> 唯一"持久化"的是 `backend/server/server_utils.py CustomLogsHandler` 写的 `outputs/*.json`（事件流 + context + costs）和报告文件。它**不是**结构化状态，无法用于"断点续研"。

---

## 7. 现有"动态规划 / 审核 / 反思 / 循环"能力盘点

| 能力 | 现状 | 位置 | 性质 |
| --- | --- | --- | --- |
| 任务拆解 | 一次性生成 3 条子查询 | `query_processing.py:83` | 静态，不可修订 |
| 计划调整 | 无（deep 模式的追问下钻最接近） | `deep_research.py:551` | 隐式、不可见 |
| 子任务状态追踪 | 无 | — | — |
| 来源审核 | 可选 LLM 打分筛选 | `skills/curator.py:38` | 有，但只作用于"来源"，不作用于"结论" |
| 事实核查 | 多 Agent 一次 LLM 复核，二值 accept/revise | `agents/fact_checker.py:10` | 有，非量化，不参与标准模式 |
| 反思修正 | 多 Agent 的 writer 回退（有界） | `orchestrator.py:101-105` | 有，仅报告文本层 |
| 循环执行 | 标准模式**无**；deep 有递归；multi_agents 有图 | — | 三种形态互不统一 |
| 终止条件 | 流水线自然结束；deep 用 depth；multi 用 revision 上限 | — | 无"质量达标"判定 |
| 成本记录 | 美元成本 + 步骤分桶 | `agent.py:795` / `utils/costs.py` | 有，无上限 |
| 过程事件 | `research_observer` 钩子 + JSON 日志 | `observability.py:22` | 有，可直接复用 |

---

## 8. 失败与降级路径（可靠性现状）

| 位置 | 行为 | 风险 |
| --- | --- | --- |
| `researcher.py:430-432` | web 检索整体异常 → `log.error` 后**返回 `[]`** | 上层拿到空 context 仍继续写报告，可能产出"无依据报告" |
| `researcher.py:678-687` | 单个子查询异常 → 返回 `""` | 静默丢一条研究线 |
| `utils/llm.py:120-160` | 非流式最多重试 10 次，指数退避 | 单次调用可能耗时数分钟，无全局超时 |
| `skills/curator.py:105-114` | 审核失败 → 回退到未筛选来源 | 合理降级 |
| `skills/context_manager.py:147-153` | embedding 不可用 → 关键词排序 | 合理降级 |
| `deep_research.py:513-531` | 某层全失败 → 停止下钻 | 合理，但原因只进日志 |

**这些是功能 D（预算与终止）和"防止伪造完成"要求的主要攻击面**：现在系统在"研究失败"时会**继续**生成报告，且不标注"未解决的问题"。

---

## 9. 对改造的直接结论

1. 标准模式的**唯一插入点**是 `GPTResearcher.conduct_research()` 与 `write_report()` 之间——要加"评估→反思→补充研究"的循环，必须在这两者之外新增一层编排，而不是改 `ResearchConductor` 内部。
2. `create_chat_completion()` 是全项目唯一 LLM 收口，**预算/计数必须挂在这里**，否则必然漏算。
3. `research_observer` 事件已经覆盖了"候选来源、抓取、预取、失败"等关键节点，**过程记录不需要另造轮子**。
4. `self.context` 是纯字符串，**评估需要新增"结论—证据"结构化中间层**，否则引用支撑率无法计算。
5. deep 模式已经证明"递归 + 追问"在本代码库可行，**功能 A/C 应优先复用它的模式，而不是引入 LangGraph 重写**。
