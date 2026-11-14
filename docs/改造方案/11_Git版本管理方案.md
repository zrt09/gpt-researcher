# 11 Git 版本管理方案

> 目标：让"原版"与"改造版"永远可精确对照，任何一个批次都能回滚，且不发生破坏性操作。

---

## 1. 当前仓库事实（已核实）

| 项 | 值 |
| --- | --- |
| 远端 | `origin` = `zrt09/gpt-researcher`（fork）；`upstream` = `assafelovic/gpt-researcher` |
| 基线提交 | `ddd4db36`（本地 `main`，领先 `origin/main` 136 个提交） |
| 基线标签 | **已创建**：`baseline-original-v0.17.0` → `ddd4db36`（annotated tag） |
| 开发分支 | `feature/adaptive-reliable-research`（从 `ddd4db36` 切出） |
| 当前进度 | 2 次文档提交（`40eca22c`、`9b759ae0`） |
| 既有分支 | `feature/competitor-monitor`（上一轮试验残留，不动） |

> 标签验证命令：`git rev-parse --short 'baseline-original-v0.17.0^{commit}'` → `ddd4db36`

---

## 2. 分支模型

```
main ──● ddd4db36 ────────────────────────────────────（冻结，只接受"原版修复"，本次改造不动）
        │  tag: baseline-original-v0.17.0
        └──● feature/adaptive-reliable-research
              ├─ docs: 阶段 0/1
              ├─ docs: 阶段 2
              ├─ docs: 阶段 3
              ├─ feat(adaptive): T01 ... T28（每任务一次提交）
              └─ tag: adaptive-batch1 ... adaptive-batch6
```

规则：

1. **`main` 冻结**：本次改造期间不向 `main` 提交任何东西；`main` 只作为"原版可运行"的证据。
2. **只用一个开发分支**：不切 per-task 分支（单人开发、任务小，PR 流程过重）。
3. **不推送**：`git push` 需要你明确同意（`main` 已领先 origin 136 个提交，推送会改变远端历史关系）。
4. **不做 rebase / force push**：历史保持线性追加，避免改写已存在的提交。

---

## 3. 标签策略

| 标签 | 指向 | 何时打 | 用途 |
| --- | --- | --- | --- |
| `baseline-original-v0.17.0` | `ddd4db36` | ✅ 已打 | 原版基线，对照实验的"前" |
| `adaptive-batch1` | 批次一完成 | T05 通过后 | 状态/预算可用 |
| `adaptive-batch2` | 批次二完成 | T08 通过后 | 规划可用 |
| `adaptive-batch3` | 批次三完成 | T13 通过后 | 评估可用 |
| `adaptive-batch4` | 批次四完成 | T18 通过后 | 评估→补研闭环（假执行器） |
| `adaptive-batch5` | 批次五完成 | T22 通过后 | 全循环可跑（真执行器） |
| `adaptive-v1.0.0` | 全部完成 | T27 通过后 | 交付版本 |

> 打标签的判据统一为："该批次 DoD 全满足 + `pytest tests/ -q` 基线不退化"。

---

## 4. 提交规范

### 4.1 格式

```
<type>(<scope>): <T编号> <一句话说明>

<可选：为什么这么做 / 与设计的偏差 / 已知限制>
```

| type | 用途 |
| --- | --- |
| `feat` | 新模块/新能力 |
| `fix` | 修 bug |
| `refactor` | 不改行为的结构调整 |
| `test` | 只加测试 |
| `docs` | 文档 |
| `chore` | 配置/构建 |

示例：

```
feat(adaptive): T02 新增 ResearchState 与 7 条不变量校验
feat(adaptive): T04 LLM 用量钩子 + BudgetController
refactor(adaptive): T15 研究失败语义改为可判定（保持返回值兼容）
```

### 4.2 一次提交 = 一个任务

- **禁止**把 T01~T05 合在一次提交里（无法回滚单个任务）；
- **禁止**先提交"半成品 + 下次补测试"；
- 提交前必须 `git status` 干净（除临时文件），并附上测试结果。

### 4.3 提交信息里必须写的两件事

1. 该任务是否偏离设计（偏离则写原因）；
2. 若引入已知限制（如"仅支持 light 评估"），写清楚。

---

## 5. 每次改动前的检查清单（强制）

```bash
git status --short          # 必须干净，或有明确的未跟踪文件说明
git branch --show-current   # 必须是 feature/adaptive-reliable-research
git log --oneline -3        # 确认上一个提交是预期任务
pytest tests/ -q            # 改动前先知道基线（可选，但 T15 前必做）
```

任何一项不符合 → 先停下来报告，不要"顺手修"。

---

## 6. 禁止操作清单（硬约束）

| 操作 | 原因 |
| --- | --- |
| `git push --force` / `-f` | 改写历史，不可恢复 |
| `git reset --hard` 到非当前提交 | 丢工作 |
| `git checkout .` / `git restore .` 批量丢弃 | 可能丢用户的本地改动 |
| 删除 `main` / 删除标签 | 破坏对照基线 |
| 修改 `.env`（除已授权的 `RETRIEVER`） | 含密钥，属用户私有配置 |
| 提交 `outputs/`、`logs/`、`env/` | 产物/虚拟环境，已在 `.gitignore` |
| 提交 `.env*` | 密钥泄漏风险（`.env.bak` 已移出仓库） |
| 修改 `tests/` 里既有用例来"让测试通过" | 任务书明确禁止 |

---

## 7. 回滚策略

| 场景 | 做法 |
| --- | --- |
| 单个任务写坏 | `git revert <sha>`（保留历史），或继续提交修复 |
| 某批次不可用 | 回退到上个标签：`git checkout adaptive-batchN-1 -- .` 后重新提交（**不** reset） |
| 灾难性错误 | `git checkout feature/adaptive-reliable-research` + `git reset --soft <batch tag>`，先看 diff 再决定（需你确认后才执行） |
| 需要对照原版 | `git checkout baseline-original-v0.17.0`（只读），或 `git worktree add ../gpt-researcher-orig baseline-original-v0.17.0` |

**推荐用 worktree 做对照**，避免反复切分支污染工作区：

```bash
git worktree add ../gpt-researcher-orig baseline-original-v0.17.0
# 原版在 ../gpt-researcher-orig 跑，改造版在当前目录跑
```

---

## 8. 与 `outputs/`、`docs/` 的关系

| 目录 | 状态 | 说明 |
| --- | --- | --- |
| `docs/改造方案/` | **纳入版本控制** | 本次交付文档（00~09 + 后续 10~14） |
| `outputs/` | 忽略 | 报告与状态落盘（`ADAPTIVE_STATE_DIR` 默认也在其下） |
| `logs/` | 忽略 | 运行日志 |
| `config/`、`scripts/` 空壳 | 未跟踪 | 上一轮试验残留，不在本次范围 |

> `docs/` 同时是 Docusaurus 站点根目录。`改造方案/` 子目录未加入导航配置，不影响站点构建；如需发布，另开任务。

---

## 9. 与真实远端的关系（需要你决策时再动）

当前 `main` 领先 `origin/main` 136 个提交，说明本地历史与远端已分叉。本次改造**不处理**这个分叉：

- 不 push；
- 不 pull/rebase；
- 不合并 upstream。

如果后续要开源/备份，建议：

```bash
git push origin feature/adaptive-reliable-research   # 只推特性分支，不动 main
```

---

## 10. 提交历史（预计 31 次）

| # | 提交 | 备注 |
| --- | --- | --- |
| 1-3 | `docs: 阶段0/1`、`docs: 阶段2`、`docs: 阶段3` | 已完成 2 次 |
| 4-8 | T01~T05 | 批次一 |
| 9-11 | T06~T08 | 批次二 |
| 12-16 | T09~T13 | 批次三 |
| 17-21 | T14~T18 | 批次四 |
| 22-25 | T19~T22 | 批次五 |
| 26-29 | T23~T26 | 批次六 |
| 30-31 | T27 + 最终交付 | 收尾 |
