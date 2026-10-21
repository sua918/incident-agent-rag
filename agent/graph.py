"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | incident 파이프라인)
incident: plan(MCP Prompt 절차 반영) → gate(권위 게이트) → research(가드+병렬+재시도)
          → replan_check(부족 시 gap task 후 재조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import json
import time
from functools import partial
from pathlib import Path
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .retry import with_retry


def _load_known_systems() -> set[str]:
    """ollama_llm.py/openai_llm.py와 동일한 소스(data/monitoring.json)에서
    시스템 목록을 읽는다 — plan_node가 MCP Prompt에 넘길 system_name을
    쿼리에서 멤버십 매칭으로 뽑아낼 때 쓴다."""
    path = Path(__file__).resolve().parent.parent / "data" / "monitoring.json"
    try:
        return set(json.loads(path.read_text()).keys())
    except (OSError, json.JSONDecodeError):
        return {"auth-service", "web-01"}


KNOWN_SYSTEMS = _load_known_systems()


def _extract_system_name(query: str) -> str:
    """정규식 대신 실제 KNOWN_SYSTEMS 멤버십 매칭 — MCP Prompt(incident_analysis_
    prompt)의 system_name은 "시스템: X" 한 줄짜리 컨텍스트라 항상 정해진 이름
    중 하나로만 고정한다(여러 개 언급돼도 join하지 않음 — 실제 조사 대상 목록은
    이후 plan()의 tasks가 별도로 결정). 쿼리에서 가장 먼저 등장하는 시스템을
    고르고, 하나도 안 걸리면 가장 흔한 사례인 auth-service로 폴백."""
    matches = [(query.index(s), s) for s in KNOWN_SYSTEMS if s in query]
    return min(matches)[1] if matches else "auth-service"


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    failed_sources: list[dict]
    validation_passed: bool
    status: str
    validation_feedback: str
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    result, usage = deps.fast_llm.route(state["query"])
    event = {"type": "RouteDecided", "route": result["route"]}
    return {"route": result["route"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage),
            "events": state.get("events", []) + [event]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": _extract_system_name(state["query"]), "symptom": state["query"]},
        server=prompt_server,
    )
    result, usage = deps.fast_llm.plan(state["query"], procedure)
    targets = [t["target"] for t in result["tasks"]
               if t["agent"] == "research" and t.get("target")]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    tasks = state["tasks"]
    indices = [i for i, task in enumerate(tasks)
               if task["agent"] == "research" and task["status"] == "pending"]
    if not indices:
        return {"events": state["events"] + [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    async def run_one(index):
        task = tasks[index]
        if task["kind"] == "monitoring":
            data, retries = await with_retry(deps.mcp.call_tool, "get_monitoring_status", {"system_name": task["target"]})
            if not isinstance(data, dict) or "status" not in data:
                raise ValueError("모니터링 응답 형식 오류")
            if data["status"] not in {"healthy", "degraded", "down"}:
                raise LookupError("등록된 시스템의 모니터링 상태를 확인할 수 없습니다.")
            return {"source": "monitoring", "data": data, "retries": retries}
        server, template = await deps.mcp.find_resource_template("get_incident_runbook")
        data = await deps.mcp.read_resource(template.format(incident_type=task["target"]), server=server)
        if not data.strip() or data.lstrip().startswith("# Runbook Not Found"):
            raise LookupError("대응 매뉴얼 없음")
        return {"source": "runbook", "data": data, "retries": 0}

    started = time.perf_counter()
    # 한 조회가 실패해도 다른 작업에서 받은 자료는 남긴다.
    results = await asyncio.gather(*(run_one(i) for i in indices), return_exceptions=True)
    elapsed = round(time.perf_counter() - started, 2)
    new_tasks = [dict(task) for task in tasks]
    collected = list(state.get("collected", []))
    failures = list(state.get("failed_sources", []))
    events = list(state["events"])
    retry_total = state.get("retry_count", 0)
    for index, result in zip(indices, results):
        task = tasks[index]
        source = "monitoring" if task["kind"] == "monitoring" else "runbook"
        if isinstance(result, BaseException):
            new_tasks[index]["status"] = "failed"
            failure = {"task_id": task["id"], "source": source, "target": task.get("target", ""), "error": type(result).__name__}
            failures.append(failure)
            events.append({"type": "TaskFailed", **failure})
            continue
        new_tasks[index]["status"] = "done"
        collected.append({"source": result["source"], "data": result["data"]})
        retry_total += result["retries"]
        events.append({"type": "TaskCompleted", "id": task["id"], "source": result["source"], "target": task["target"], "retries": result["retries"]})
    return {"tasks": new_tasks, "collected": collected, "failed_sources": failures,
            "retry_count": retry_total, "parallel_elapsed": elapsed, "events": events}


# 수집된 모니터링 결과(상태·알림 문구)를 보고 어떤 runbook이 맞는지 고른다. LLM에게
# 다시 묻는 대신 코드 레벨 규칙으로 판단한다 — gate(Wave 6)·replan(Wave 7)과 같은 설계
# 원칙: 모델의 판단을 그대로 믿지 않고, 실측 데이터로 검증 가능한 결정은 코드가 내린다.
_INCIDENT_TYPE_RULES = [
    ("latency", "high-latency"),
    ("pool", "high-latency"),
    ("cpu", "cpu-spike"),
    ("memory", "memory-leak"),
    ("heap", "memory-leak"),
    ("timeout", "network-timeout"),
    ("network", "network-timeout"),
    ("disk", "disk-full"),
    ("down", "db-down"),
]


def _classify_incident_type(collected: list[dict]) -> str:
    text = " ".join(
        f"{c['data'].get('status', '')} " + " ".join(c["data"].get("alerts", []))
        for c in collected if c.get("source") == "monitoring"
    ).lower()
    for keyword, incident_type in _INCIDENT_TYPE_RULES:
        if keyword in text:
            return incident_type
    return "db-down"   # 매칭되는 증상이 없으면 가장 일반적인 장애 대응 절차로 폴백


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 7 — 1차 수집 결과 평가. runbook이 없으면 gap task만 추가해 재조사."""
    if not state.get("collected") and state.get("failed_sources"):
        return {"events": state["events"] + [{"type": "ReplanCheck", "round": state.get("replan_rounds", 0), "is_sufficient": True, "reason": "추가 조사 기준이 되는 자료 없음"}]}
    if state.get("failed_sources") and not any(
            item.get("source") == "monitoring" and item["data"].get("status") in {"degraded", "down"}
            for item in state.get("collected", [])):
        return {"events": state["events"] + [{"type": "ReplanCheck", "round": state.get("replan_rounds", 0), "is_sufficient": True, "reason": "조회하지 못한 대상의 장애 유형을 추정하지 않음"}]}
    has_runbook = any(c["source"] == "runbook" for c in state.get("collected", []))
    rounds = state.get("replan_rounds", 0)
    if has_runbook or rounds >= 1:
        verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": True}
        return {"events": state["events"] + [verdict]}
    incident_type = _classify_incident_type(state.get("collected", []))
    gap_task = {"id": "task-gap-runbook", "agent": "research", "kind": "runbook",
                "target": incident_type, "status": "pending"}
    verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
               "gap_task": gap_task["id"], "target": incident_type}
    return {"tasks": state["tasks"] + [gap_task], "replan_rounds": rounds + 1,
            "events": state["events"] + [verdict]}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = deps.fast_llm.analyze(state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    if not state.get("collected"):
        return {"draft": "", "events": state["events"] + [{"type": "DraftSkipped", "reason": "조회 자료 없음"}]}
    attempt = state.get("validation_attempts", 0) + 1
    # 작성 중인 초안은 숨기고, 검증이 끝난 답변만 화면에 보낸다.
    draft, usage = await deps.quality_llm.write(
        state["query"], state["collected"], attempt, state.get("validation_feedback"))
    return {"draft": draft, "last_prompt": usage.get("prompt"), "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt, "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    draft = state["draft"]
    attempts = state.get("validation_attempts", 0) + 1
    issues = []
    if not state.get("collected"):
        issues.append("진단에 필요한 조회 자료가 없습니다.")
    if len(draft) < 30:
        issues.append("답변을 30자 이상으로 구체적으로 설명하세요.")
    if "1)" not in draft:
        issues.append("대응 절차를 1) 2) 3) 번호로 설명하세요.")
    passed = not issues
    exhausted = not passed and (attempts >= 3 or not state.get("collected"))
    verdict = "합격" if passed else ("확인 필요" if exhausted else "불합격")
    return {"validation_attempts": attempts, "validation_passed": passed,
            "validation_feedback": " ".join(issues) if issues else None,
            "events": state["events"] + [{"type": "Validation", "attempt": attempts, "verdict": verdict}]}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    failures = state.get("failed_sources", [])
    passed = state.get("validation_passed", False)
    review = bool(failures) or not passed
    if not state.get("collected"):
        reply = "진단 자료를 조회하지 못해 결과를 확정할 수 없습니다. 모니터링 연결을 확인한 뒤 다시 요청해 주세요."
    elif not passed:
        reply = "답변을 3회 확인했지만 검증 기준을 충족하지 못했습니다. 담당자가 조회 자료와 대응 절차를 직접 확인해 주세요."
    else:
        reply = state["draft"]
        if failures:
            reply = "일부 자료를 조회하지 못했습니다. 아래 내용은 확인된 자료에 한하며 담당자 확인이 필요합니다.\n\n" + reply
    if failures:
        targets = ", ".join(dict.fromkeys(item["target"] for item in failures))
        reply += "\n\n조회하지 못한 대상: " + targets
    status = "needs_review" if review else "completed"
    return {"final": reply, "status": status,
            "events": state["events"] + [{"type": "Finalize", "pipeline": "incident-v2", "status": status}]}


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "plan"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("is_sufficient") else "research"


def _after_validate(state: AgentState) -> str:
    if state.get("validation_passed") or state.get("validation_attempts", 0) >= 3 or not state.get("collected"):
        return "finalize"
    return "write"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan"})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
