"""A self-contained web UI for the adaptive research loop.

The repository ships a full Next.js frontend, but running it needs Node plus an
npm install. This module serves a single-page UI straight from the existing
FastAPI server, so `python main.py` (or uvicorn) is enough:

    http://localhost:8000/adaptive-ui

Endpoints:

* ``GET  /adaptive-ui``              - the page
* ``POST /adaptive-ui/api/run``      - start a run, returns run_id
* ``GET  /adaptive-ui/api/status/{}`` - progress, metrics, log tail
* ``GET  /adaptive-ui/api/report/{}`` - rendered report (HTML) or raw markdown
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

router = APIRouter(prefix="/adaptive-ui", tags=["adaptive-ui"])

MAX_LOG_ENTRIES = 500
LOG_TAIL = 60
_tasks: dict[str, asyncio.Task] = {}
_runs: dict[str, dict[str, Any]] = {}


class RunRequest(BaseModel):
    query: str
    rounds: int | None = None
    eval_mode: str = "light"
    persist: bool = True


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(entry: dict[str, Any], event: str, fields: dict[str, Any] | None = None) -> None:
    entry["log"].append({"at": _now(), "event": event, "fields": fields or {}})
    if len(entry["log"]) > MAX_LOG_ENTRIES:
        del entry["log"][: len(entry["log"]) - MAX_LOG_ENTRIES]


def _metrics(entry: dict[str, Any], evaluation) -> None:
    if evaluation is None:
        return
    entry["metrics"] = {
        name: {
            "value": metric.value,
            "notes": metric.notes,
            "numerator": metric.numerator,
            "denominator": metric.denominator,
        }
        for name, metric in evaluation.metrics.items()
    }
    entry["passed"] = evaluation.passed
    entry["claims"] = len(evaluation.claims)
    entry["deficits"] = [d.kind for d in evaluation.deficits]


async def _execute(run_id: str, request: RunRequest) -> None:
    from gpt_researcher.adaptive import AdaptiveResearcher

    entry = _runs[run_id]
    try:
        researcher = AdaptiveResearcher(
            request.query,
            max_rounds=request.rounds,
            eval_mode=request.eval_mode,
            observer=lambda event, **fields: _log(entry, event, fields),
            persist=request.persist,
        )
        entry["max_rounds"] = researcher.state.budget.max_rounds

        def on_round(record) -> None:
            entry["round"] = record.round
            entry["tasks_done"] = sum(
                1 for task in researcher.state.plan if task.status.value == "done"
            )
            entry["tasks_total"] = len(researcher.state.plan)
            entry["evidence"] = len(researcher.state.evidence)
            if record.evaluation is not None:
                _metrics(entry, record.evaluation)
            _log(
                entry,
                "round_completed",
                {
                    "round": record.round,
                    "decision": record.decision,
                    "new_evidence": record.new_evidence,
                },
            )

        result = await researcher.run(on_round=on_round)
        _metrics(entry, result.evaluation)
        entry.update(
            status=result.status.value,
            stop_reason=result.stop_reason.value if result.stop_reason else None,
            report=result.report,
            directory=result.directory,
            finished_at=_now(),
        )
        _log(entry, "finished", {"status": result.status.value})
    except Exception as exc:  # noqa: BLE001 - surface the error in the UI
        entry.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=_now())
        _log(entry, "error", {"message": str(exc)})


@router.get("", response_class=HTMLResponse)
async def adaptive_page() -> str:
    return _PAGE


@router.post("/api/run")
async def start_run(request: RunRequest) -> dict[str, Any]:
    query = (request.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    run_id = uuid.uuid4().hex[:10]
    _runs[run_id] = {
        "id": run_id,
        "query": query,
        "status": "running",
        "started_at": _now(),
        "round": 0,
        "max_rounds": request.rounds or 0,
        "tasks_total": 0,
        "tasks_done": 0,
        "evidence": 0,
        "metrics": {},
        "passed": None,
        "report": None,
        "log": [],
        "error": None,
    }
    _tasks[run_id] = asyncio.create_task(_execute(run_id, request))
    return {"run_id": run_id}


@router.get("/api/status/{run_id}")
async def run_status(run_id: str) -> dict[str, Any]:
    entry = _runs.get(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return {
        "id": entry["id"],
        "query": entry["query"],
        "status": entry["status"],
        "started_at": entry.get("started_at"),
        "finished_at": entry.get("finished_at"),
        "round": entry.get("round", 0),
        "max_rounds": entry.get("max_rounds", 0),
        "tasks_total": entry.get("tasks_total", 0),
        "tasks_done": entry.get("tasks_done", 0),
        "evidence": entry.get("evidence", 0),
        "claims": entry.get("claims", 0),
        "metrics": entry.get("metrics", {}),
        "passed": entry.get("passed"),
        "stop_reason": entry.get("stop_reason"),
        "report_ready": bool(entry.get("report")),
        "error": entry.get("error"),
        "log": entry["log"][-LOG_TAIL:],
    }


@router.get("/api/report/{run_id}")
async def run_report(run_id: str, format: str = "html"):
    entry = _runs.get(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown run")
    if entry["status"] == "running":
        raise HTTPException(status_code=409, detail="run still in progress")
    report = entry.get("report") or entry.get("error") or "（没有生成报告）"
    if format == "md":
        return PlainTextResponse(report, media_type="text/markdown; charset=utf-8")

    import markdown as markdown_lib

    body = markdown_lib.markdown(
        report, extensions=["tables", "fenced_code", "toc", "sane_lists"]
    )
    return {"markdown": report, "html": body}


_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>Adaptive Research</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; background: #0f1115; color: #e6e6e6; }
  header { padding: 18px 24px; border-bottom: 1px solid #262b36; display: flex; gap: 12px; align-items: baseline; }
  header h1 { font-size: 18px; margin: 0; font-weight: 600; }
  header span { color: #8b93a7; font-size: 13px; }
  main { max-width: 1080px; margin: 0 auto; padding: 24px; display: grid; gap: 20px; }
  .card { background: #161a22; border: 1px solid #262b36; border-radius: 12px; padding: 18px; }
  textarea { width: 100%; min-height: 90px; background: #0f1115; color: inherit;
             border: 1px solid #2c3340; border-radius: 8px; padding: 12px; font-size: 15px; resize: vertical; }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
  input[type=number] { width: 80px; background: #0f1115; color: inherit; border: 1px solid #2c3340;
                       border-radius: 8px; padding: 8px; }
  select { background: #0f1115; color: inherit; border: 1px solid #2c3340; border-radius: 8px; padding: 8px; }
  button { background: #3b82f6; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px;
           font-size: 15px; cursor: pointer; }
  button:disabled { background: #2a3342; color: #7f8a9d; cursor: not-allowed; }
  .muted { color: #8b93a7; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #232936; }
  .ok { color: #4ade80; } .warn { color: #fbbf24; } .bad { color: #f87171; }
  #log { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: #9aa4b8;
         max-height: 220px; overflow: auto; white-space: pre-wrap; }
  #report { line-height: 1.75; font-size: 16px; }
  #report h1 { font-size: 26px; } #report h2 { font-size: 20px; margin-top: 28px; }
  #report h3 { font-size: 17px; } #report a { color: #7ab7ff; }
  #report pre { background: #0f1115; padding: 12px; border-radius: 8px; overflow: auto; }
  #report table { margin: 12px 0; }
  .hidden { display: none; }
</style>
</head>
<body>
<header>
  <h1>Adaptive Research Agent</h1>
  <span>规划 → 研究 → 评估 → 反思 → 补研（本地运行，不经过第三方前端）</span>
</header>
<main>
  <section class="card">
    <textarea id="query" placeholder="输入研究问题，例如：2026年诺贝尔物理学奖得主及其主要贡献"></textarea>
    <div class="row">
      <label class="muted">最多轮次 <input id="rounds" type="number" min="1" max="6" value="3" /></label>
      <label class="muted">评估模式
        <select id="evalMode">
          <option value="light">light（零额外模型调用）</option>
          <option value="full">full（加证据/冲突判定）</option>
        </select>
      </label>
      <button id="run">开始研究</button>
      <span id="hint" class="muted"></span>
    </div>
  </section>

  <section class="card hidden" id="progressCard">
    <div class="row" style="justify-content: space-between;">
      <strong id="statusLine">运行中…</strong>
      <a id="mdLink" class="muted" href="#" target="_blank">查看 Markdown</a>
    </div>
    <div id="metricsWrap" class="hidden" style="margin-top:12px;">
      <table id="metrics"><thead><tr><th>指标</th><th>结果</th><th>说明</th></tr></thead><tbody></tbody></table>
    </div>
    <pre id="log"></pre>
  </section>

  <section class="card hidden" id="reportCard">
    <div id="report"></div>
  </section>
</main>
<script>
let runId = null, timer = null;
const $ = (id) => document.getElementById(id);

$("run").onclick = async () => {
  const query = $("query").value.trim();
  if (!query) { $("hint").textContent = "请先输入问题"; return; }
  $("run").disabled = true;
  $("hint").textContent = "启动中…";
  try {
    const resp = await fetch("adaptive-ui/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        query,
        rounds: parseInt($("rounds").value || "3", 10),
        eval_mode: $("evalMode").value,
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    runId = data.run_id;
    $("hint").textContent = "运行 ID：" + runId;
    $("progressCard").classList.remove("hidden");
    $("reportCard").classList.add("hidden");
    $("mdLink").href = "adaptive-ui/api/report/" + runId + "?format=md";
    poll();
    timer = setInterval(poll, 2000);
  } catch (err) {
    $("hint").textContent = "启动失败：" + err.message;
    $("run").disabled = false;
  }
};

async function poll() {
  if (!runId) return;
  const resp = await fetch("adaptive-ui/api/status/" + runId);
  if (!resp.ok) return;
  const s = await resp.json();
  renderStatus(s);
  if (s.status !== "running") {
    clearInterval(timer);
    $("run").disabled = false;
    loadReport();
  }
}

function renderStatus(s) {
  const cls = s.status === "completed" ? "ok" : (s.status === "failed" ? "bad" : "warn");
  $("statusLine").innerHTML = `<span class="${cls}">${s.status}</span> · 第 ${s.round}/${s.max_rounds} 轮 · 任务 ${s.tasks_done}/${s.tasks_total} · 证据 ${s.evidence} · 结论 ${s.claims}`;
  const body = $("metrics").querySelector("tbody");
  const names = Object.keys(s.metrics || {});
  if (names.length) {
    $("metricsWrap").classList.remove("hidden");
    body.innerHTML = names.map((n) => {
      const m = s.metrics[n];
      const v = (m.value === null || m.value === undefined) ? "—" : Number(m.value).toFixed(2);
      return `<tr><td>${n}</td><td>${v}</td><td>${m.notes || ""}</td></tr>`;
    }).join("");
  }
  $("log").textContent = (s.log || []).map((e) => `${e.at}  ${e.event}  ${JSON.stringify(e.fields)}`).join("\\n");
  if (s.error) $("log").textContent += "\\nERROR: " + s.error;
}

async function loadReport() {
  const resp = await fetch("adaptive-ui/api/report/" + runId);
  if (!resp.ok) { $("report").textContent = "报告读取失败：" + (await resp.text()); }
  else { const data = await resp.json(); $("report").innerHTML = data.html; }
  $("reportCard").classList.remove("hidden");
  $("report").scrollIntoView({behavior: "smooth"});
}
</script>
</body>
</html>
"""
