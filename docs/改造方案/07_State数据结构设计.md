# 07 State 数据结构设计

> 目标：给出一个能落地、可序列化、可回放、不膨胀的 State Schema
> 原则：**State 是唯一真相**；`GPTResearcher` 的实例属性只作为执行期缓存，每轮结束同步回 State

---

## 1. 设计约束

| 约束 | 说明 |
| --- | --- |
| 单任务 | 一个 State 只服务一次研究任务，不跨任务共享 |
| 可序列化 | 必须能 `to_dict()/from_dict()` 无损往返（JSON） |
| 有界 | 长任务下 `evidence`、`returns` 会膨胀，必须有条数/字数上限与裁剪策略 |
| 不引入依赖 | 只用标准库 `dataclasses` + `enum` + `json` |
| 与现有代码解耦 | 不继承 `ResearchState(TypedDict)`（那是多 Agent 的），也不放进 `gpt_researcher/memory/` |

---

## 2. 顶层结构 `ResearchState`

```python
@dataclass
class ResearchState:
    # —— 任务标识 ——
    run_id: str                      # "20261114_1715_ab12cd"
    query: str                       # 用户原始研究问题（永不改写）
    report_type: str = "research_report"
    started_at: str = ""             # ISO8601

    # —— 计划 ——
    plan: list[SubTask] = field(default_factory=list)
    plan_version: int = 1
    plan_history: list[PlanSnapshot] = field(default_factory=list)

    # —— 研究与证据 ——
    evidence: list[Evidence] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    visited_urls: set[str] = field(default_factory=set)

    # —— 轮次与评估 ——
    rounds: list[RoundRecord] = field(default_factory=list)
    latest_evaluation: EvaluationResult | None = None

    # —— 报告 ——
    report: str | None = None
    report_versions: list[ReportVersion] = field(default_factory=list)

    # —— 预算与停止 ——
    budget: BudgetState = field(default_factory=BudgetState)
    status: RunStatus = RunStatus.RUNNING
    stop_reason: StopReason | None = None
    open_questions: list[str] = field(default_factory=list)

    updated_at: str = ""
```

### 为什么这些字段是"必须的"

| 字段 | 对应任务书要求 |
| --- | --- |
| `query` | "用户原始研究问题" |
| `plan / plan_version / plan_history` | "当前研究计划""子任务列表""历史计划版本" |
| `evidence` | "已收集的研究资料" |
| `claims` | "关键结论及对应证据" |
| `latest_evaluation / rounds` | "质量评估结果""历史研究与修改记录" |
| `budget` | "Token 使用情况""停止原因" |
| `open_questions` | "明确报告未解决的问题" |
| `report_versions` | "保留每轮研究和修改记录" |

---

## 3. 子结构

### 3.1 `SubTask`（子任务）

```python
class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"      # 被去重或预算裁掉

class TaskOrigin(str, Enum):
    INITIAL = "initial"      # 首轮拆解
    GAP = "gap"              # 评估缺口补充
    REPLAN = "replan"        # 重规划

@dataclass
class SubTask:
    id: str                  # "T1".."T9"，稳定、不复用
    query: str               # 实际检索式
    goal: str                # 可验收的研究目标（一句话，含"判断标准"）
    origin: TaskOrigin = TaskOrigin.INITIAL
    parent_id: str | None = None      # gap 任务的来源父任务
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    created_round: int = 1
    closed_round: int | None = None
    failure_reason: str | None = None
```

**关键点**
- `goal` 不是装饰：评估时用它判断"该任务是否被覆盖"，所以要求可判定（例："列出 3 类以上被学界公认的 RAG 局限，并注明出处"）。
- `id` 单调递增，不复用；去重时把 `goal` 合并到已有任务，不产生新 id。

### 3.2 `Evidence`（证据）

```python
@dataclass
class Evidence:
    id: str                  # "E7"
    task_id: str
    url: str
    title: str
    snippet: str             # 压缩后的片段，≤ 400 词
    query: str               # 产生它的检索式
    round: int
    fetched_at: str
    published_at: str | None = None   # 页面发布时间（可缺失）
    source_kind: str = "web"          # web / pdf / arxiv / prefetched
```

**裁剪策略**：单任务 ≤ 8 条；全局 ≤ 120 条；超出时按 `(round 新 > 旧, 与任务的词面相关性)` 保留，被裁的只保留 `id/url/title` 的"瘦身记录"，避免评估时引用不到。

### 3.3 `Claim`（结论）

```python
class SupportLevel(str, Enum):
    SUPPORTED = "supported"
    WEAK = "weak"
    UNSUPPORTED = "unsupported"
    UNCHECKED = "unchecked"

@dataclass
class Claim:
    id: str                  # "C12"
    text: str                # 结论句（原文摘录，不改写）
    section: str             # 所属章节标题
    cited_urls: list[str]    # 原文中的引用链接
    task_ids: list[str]      # 覆盖了哪些子任务（可为空）
    evidence_ids: list[str]  # 评估判定的证据（由 evaluator 回填）
    support: SupportLevel = SupportLevel.UNCHECKED
    judge_reason: str | None = None
```

**为什么 `text` 是原文摘录**：改写会让"引用是否支撑结论"变得不可判定；保持原文可让评估结果可人工复核。

### 3.4 `EvaluationResult` 与 `MetricResult`

```python
@dataclass
class MetricResult:
    name: str
    value: float | None      # None = 无法计算/未评估
    numerator: float | None = None
    denominator: float | None = None
    method: str = "deterministic"   # deterministic / http / llm
    sampled: bool = False
    notes: str = ""
    error: str | None = None

@dataclass
class EvaluationResult:
    round: int
    mode: str                          # light / full
    metrics: dict[str, MetricResult]
    claims: list[Claim]
    deficits: list[Deficit]
    passed: bool
    evaluated_at: str
    llm_calls: int = 0

@dataclass
class Deficit:
    kind: str        # missing_coverage / weak_evidence / broken_citation / conflict / staleness
    task_id: str | None
    description: str
    suggested_query: str | None
```

### 3.5 `RoundRecord`（轮次记录）

```python
@dataclass
class RoundRecord:
    round: int
    plan_version: int
    tasks_started: list[str]
    tasks_done: list[str]
    tasks_failed: list[str]
    new_evidence: int
    new_claims: int
    evaluation: EvaluationResult | None
    decision: str | None              # supplement / replan / rewrite / finish / abort
    decision_reason: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    elapsed_s: float
```

> 这是"过程记录（验收项 8）"的载体：每轮的输入输出、决策、耗时一条不漏，可直接渲染成表格。

### 3.6 `PlanSnapshot`（计划版本）

```python
@dataclass
class PlanSnapshot:
    version: int
    round: int
    task_ids: list[str]
    added: list[str]
    removed: list[str]
    reason: str
```

> 只存"差异"不存全量，避免历史计划把状态撑大。

### 3.7 `BudgetState`

```python
@dataclass
class BudgetState:
    max_rounds: int = 3
    max_llm_calls: int = 120
    max_tokens: int = 300_000
    max_seconds: int = 900
    max_cost_usd: float = 1.0
    # 累计
    rounds: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    # 首个触顶原因（单调）
    first_exhausted: str | None = None
```

### 3.8 `ReportVersion`

```python
@dataclass
class ReportVersion:
    round: int
    path: str           # outputs/adaptive/<run_id>/report_v{n}.md
    chars: int
    sha256: str
    decision: str | None
```

> 报告正文不存进 `state.json`（太大），只存路径 + 摘要；`report` 字段保留最新全文供返回。

---

## 4. 状态生命周期

| 时点 | 变化 | 责任模块 |
| --- | --- | --- |
| run 开始 | 建 State，`status=RUNNING`，预算初始化 | orchestrator |
| 首轮规划 | 写 `plan`、`plan_version=1`、`plan_history[0]` | planner |
| 每任务执行 | 任务 `status=RUNNING→DONE/FAILED`；`evidence` 追加；`visited_urls` 合并 | executor |
| 每轮报告 | `report`、`report_versions` 追加 | orchestrator/writer |
| 每轮评估 | `latest_evaluation`、`claims`、本轮 `RoundRecord.evaluation` | evaluator |
| 每轮决策 | `RoundRecord.decision`、可能新增任务/`plan_version+1` | reflection/planner |
| 每轮结束 | `budget.rounds+1`、计时、`updated_at`、落盘 | orchestrator |
| 终止 | `status`、`stop_reason`、`open_questions` | orchestrator |
| 结束 | 最终落盘 `state.json` | orchestrator |

---

## 5. 持久化策略（哪些落盘、哪些只在内存）

| 数据 | 内存 | 落盘 | 理由 |
| --- | --- | --- | --- |
| `query / plan / tasks` | ✅ | ✅ | 回放与断点的基础 |
| `evidence.snippet` | ✅ | ✅（可裁剪） | 评估证据；体积可控 |
| `visited_urls` | ✅ | ✅（仅 run 内） | 去重；跨 run 无意义 |
| `claims` | ✅ | ✅ | 评估结果可复核 |
| `report` 全文 | ✅ | ❌（存文件） | 避免 state.json 巨大 |
| `report_versions` | ✅ | ✅（路径+hash） | 过程记录 |
| `rounds` | ✅ | ✅ | 过程记录 |
| `budget` 计数 | ✅ | ✅ | 审计与对照实验 |
| LLM 原始响应 | ❌ | ❌ | 体积/隐私，不建议 |

**落盘位置**：`outputs/adaptive/<run_id>/`（已被 `.gitignore` 的 `outputs/` 覆盖）

```
outputs/adaptive/20261114_1715_ab12cd/
├── state.json           # 每轮覆盖写（原子写：临时文件 + rename）
├── report_v1.md
├── report_v2.md
└── final_report.md      # 含评估附录
```

**断点续研**：v1 只做"崩溃后可读"，**不实现**自动 resume（避免复杂度）；`state.json` 里字段足够，后续版本可加。

---

## 6. 大小控制

估算：单任务 8 条证据 × 400 词 ≈ 3200 词；6 任务 ≈ 2 万词 ≈ 3 万 token 字符级 ≈ 120KB。加上 claims/rounds，单 run `state.json` 预计 **150~300KB**，可接受。

硬限制（超出即裁剪并记录）：

| 项 | 上限 |
| --- | --- |
| `evidence` 总条数 | 120 |
| 单条 `snippet` | 400 词 |
| `claims` 总条数 | 200 |
| `rounds` | 等于 `max_rounds`（≤ 5） |
| `plan_history` | 20 条 |

---

## 7. 不变量与校验（`__post_init__` / `validate()`）

1. `status == COMPLETED` ⇒ `stop_reason == QUALITY_MET` 且 `latest_evaluation.passed` 为真；
2. `status in (PARTIAL, FAILED)` ⇒ `open_questions` 或 `stop_reason` 非空；
3. `plan` 中 `id` 唯一；`evidence.id` 唯一；
4. `Evidence.task_id` ∈ `plan` 的 id 集合（允许已 closed）；
5. `Claim.evidence_ids` ⊆ `evidence` 的 id 集合；
6. `budget.rounds == len(rounds)`；
7. `plan_version == len(plan_history) + (1 if plan else 0)`。

> 这些不变量是单测的断言点，也是"不得伪造完成"的机器可检形式。

---

## 8. JSON 示例（截断）

```json
{
  "run_id": "20261114_1715_ab12cd",
  "query": "What are the documented limitations of RAG systems?",
  "plan": [
    {"id": "T1", "query": "RAG limitations survey", "goal": "列出≥3类公认局限并注明出处",
     "origin": "initial", "status": "done", "attempts": 1,
     "evidence_ids": ["E1","E2"], "created_round": 1, "closed_round": 1}
  ],
  "plan_version": 2,
  "evidence": [
    {"id": "E1", "task_id": "T1", "url": "https://arxiv.org/abs/2506.00054",
     "title": "RAG: A comprehensive survey", "snippet": "...", "round": 1,
     "fetched_at": "2026-11-14T17:10:02Z", "published_at": "2025-06-01"}
  ],
  "claims": [
    {"id": "C1", "text": "RAG 系统在检索召回不足时会放大幻觉。",
     "section": "## 2. 检索质量依赖", "cited_urls": ["https://arxiv.org/abs/2506.00054"],
     "task_ids": ["T1"], "evidence_ids": ["E1"], "support": "supported"}
  ],
  "rounds": [
    {"round": 1, "plan_version": 1, "tasks_started": ["T1"], "tasks_done": ["T1"],
     "tasks_failed": [], "new_evidence": 2, "new_claims": 1,
     "decision": "supplement", "tokens_in": 12000, "tokens_out": 3000,
     "cost_usd": 0.004, "elapsed_s": 42.1}
  ],
  "budget": {"max_rounds": 3, "rounds": 1, "llm_calls": 9,
             "input_tokens": 12000, "output_tokens": 3000, "cost_usd": 0.004},
  "status": "running",
  "stop_reason": null,
  "open_questions": []
}
```

---

## 9. 与现有数据结构的映射

| State 字段 | 现有来源 | 转换方式 |
| --- | --- | --- |
| `visited_urls` | `GPTResearcher.visited_urls` | 直接复用同一 `set` 对象（跨任务共享去重） |
| `evidence` | `ResearchConductor` 的 `scraped_content` + 压缩 `web_context` | 通过 `artifact_sink` 回调捕获，切分为 `Evidence` |
| `budget.cost_usd` | `GPTResearcher.research_costs` | 保留原值，**另记**（两套账并列，互不覆盖） |
| `budget.input_tokens/output_tokens` | provider `last_usage_metadata` | 由 `usage_sink` 上报 |
| `report` | `ReportGenerator.write_report()` 返回 | 直接赋值 |
| `claims` | 新产物 | `extractor` 解析报告（light）或 LLM 抽取（full） |

---

## 10. 单测清单（`tests/test_adaptive_state.py`）

| 用例 | 断言 |
| --- | --- |
| `to_dict → from_dict` 往返 | 深度相等（含 set 转 list） |
| 重复 id | `validate()` 抛 `ValueError` |
| `status=COMPLETED` 但 `stop_reason=None` | 抛 `ValueError` |
| 超出 evidence 上限 | 裁剪后数量正确、瘦身记录保留 |
| 空 plan 的 `plan_version` | 不变量成立 |
| `Claim.evidence_ids` 指向不存在证据 | 抛 `ValueError` |
