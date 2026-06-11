import re
from dataclasses import dataclass
from typing import Literal, Optional

from config import MAX_REPLANS
from execution.step_runner import run_step, precheck_write, StepRunResult
from planning.planner import build_plan
from planning.schemas import Plan, PlanStep
from safety.policies import get_compensating_tool, ALWAYS_CONFIRM
from state import SessionState
from tools.definitions import ToolDefinition
from tools.registry import get_all_tools, get_tool

_STEP_REF = re.compile(r"\$step(\d+)")


@dataclass
class ExecutionResult:
    status: Literal["completed", "needs_confirmation", "needs_info", "safe_alternative",
                    "blocked", "halted", "partial", "failed"]
    message: str
    step_results: list[dict] = None
    pending_step: Optional[StepRunResult] = None
    pending_plan: Optional[Plan] = None


# ---------------------------------------------------------------------------
# Effect analysis — reversible (read/compute/verify) vs irreversible (write).
# ---------------------------------------------------------------------------

def _is_write(step: PlanStep) -> bool:
    """An api node whose tool mutates state (non-GET)."""
    if getattr(step, "kind", "api") != "api" or not step.tool:
        return False
    try:
        return get_tool(step.tool).method.upper() != "GET"
    except KeyError:
        return False


def _referenced_ids(plan: Plan) -> set:
    """Step ids that any other step consumes (via $stepN or depends_on)."""
    refs: set = set()

    def scan(o):
        if isinstance(o, str):
            refs.update(int(n) for n in _STEP_REF.findall(o))
        elif isinstance(o, dict):
            for v in o.values():
                scan(v)
        elif isinstance(o, list):
            for v in o:
                scan(v)

    for s in plan.steps:
        scan(s.params)
        scan(getattr(s, "over", None))
        sub = getattr(s, "node", None)
        if sub is not None:
            scan(sub.params)
        for d in (s.depends_on or []):
            refs.add(d)
    return refs


def _deferred_write_ids(plan: Plan) -> set:
    """Leaf writes (nothing depends on them) → defer to the act phase, last.
    A write that feeds a later step stays inline (can't be deferred)."""
    referenced = _referenced_ids(plan)
    return {s.id for s in plan.steps if _is_write(s) and s.id not in referenced}


def _is_destructive(step: PlanStep) -> bool:
    if getattr(step, "kind", "api") != "api" or not step.tool:
        return False
    if step.tool in ALWAYS_CONFIRM:
        return True
    try:
        return bool(get_tool(step.tool).destructive)
    except KeyError:
        return False


def _ambiguity_sensitive_find_ids(plan: Plan) -> set:
    """
    Ids of compute-'find' steps whose output is consumed (via $stepN) by a
    destructive api step. 'find' silently returns the FIRST match — acceptable
    for reads, but acting destructively on a guessed entity is not. These steps
    must ask the operator when more than one record matches. Only direct refs
    are traced; a find piped through another compute is not detected.
    """
    find_ids = {
        s.id for s in plan.steps
        if getattr(s, "kind", None) == "compute" and getattr(s, "operation", None) == "find"
    }
    if not find_ids:
        return set()

    sensitive: set = set()
    for s in plan.steps:
        if not _is_destructive(s):
            continue
        refs: set = set()

        def scan(o):
            if isinstance(o, str):
                refs.update(int(n) for n in _STEP_REF.findall(o))
            elif isinstance(o, dict):
                for v in o.values():
                    scan(v)
            elif isinstance(o, list):
                for v in o:
                    scan(v)

        scan(s.params)
        sensitive |= refs & find_ids
    return sensitive


def _successful_writes(state: SessionState) -> list:
    """Writes that already committed a side effect this run."""
    out = []
    for r in state.step_results:
        if not r.success or r.kind != "api":
            continue
        try:
            if get_tool(r.tool).method.upper() != "GET":
                out.append(r)
        except KeyError:
            pass
    return out


# ---------------------------------------------------------------------------
# Execution — two phases: validate everything reversible, then write last.
# ---------------------------------------------------------------------------

def execute_plan(
    plan: Plan,
    state: SessionState,
    tools: list[ToolDefinition],
    confirmed_step_id: Optional[int] = None,
    confirmed_params: Optional[dict] = None,
) -> ExecutionResult:
    """
    Run a plan with a transactional discipline:
      Phase SENSE  — all reversible nodes (reads/computes/verifies + any write that
                     feeds a later step) run first. A verify halt aborts here, with
                     nothing irreversible done.
      PRE-COMMIT   — validate every deferred (leaf) write WITHOUT executing it; if
                     any is infeasible, abort before the first mutation.
      Phase ACT    — execute the deferred writes last. If one fails after another
                     already committed, report partial success + compensation.
    """
    is_resume = confirmed_step_id is not None
    deferred = _deferred_write_ids(plan)
    state.ambiguity_sensitive_ids = _ambiguity_sensitive_find_ids(plan)

    def _handle(step: PlanStep) -> Optional[ExecutionResult]:
        """Run one step; return an ExecutionResult to bubble up, or None to continue."""
        if state.has_successful_result(step.id):
            return None
        if confirmed_step_id == step.id:
            if confirmed_params:
                step.params = {**confirmed_params, "__confirmed__": True}
            else:
                step.params["__confirmed__"] = True

        result = _run_with_retry(step, state)

        if result.status == "success":
            return None
        if result.status in ("needs_confirmation", "needs_info", "safe_alternative"):
            return ExecutionResult(
                status=result.status,
                message=result.confirmation_prompt or result.message,
                pending_step=result,
                pending_plan=plan,
            )
        if result.status == "blocked":
            return ExecutionResult(status="blocked", message=result.message)
        if result.status == "halted":
            return ExecutionResult(status="halted", message=result.message)

        # error — replan only if nothing irreversible happened yet (avoid double writes)
        if not is_resume and not _successful_writes(state) and state.replan_count < MAX_REPLANS:
            return _replan(plan, state, step, result)
        return _partial_or_failed(state, result.message)

    # PHASE SENSE — everything except deferred leaf-writes, in order
    for step in plan.steps:
        if step.id in deferred:
            continue
        outcome = _handle(step)
        if outcome is not None:
            return outcome

    # PRE-COMMIT — validate the write batch before any of it runs
    pending = [s for s in plan.steps if s.id in deferred and not state.has_successful_result(s.id)]
    problems = [p for s in pending for p in [precheck_write(s, state)] if p]
    if problems:
        state.log("precommit_blocked", {"problems": problems})
        return _partial_or_failed(state, problems[0])

    # PHASE ACT — execute the writes last
    for step in pending:
        outcome = _handle(step)
        if outcome is not None:
            return outcome

    return _completed(state)


def _replan(plan: Plan, state: SessionState, step: PlanStep, result: StepRunResult) -> ExecutionResult:
    """Recovery replan with the FULL catalog (a classifier miss may be the cause)."""
    state.replan_count += 1
    state.log("replan", {"attempt": state.replan_count, "failed_step": step.id})
    full_tools = get_all_tools()
    try:
        new_plan = build_plan(
            query=state.rewritten_query,
            tools=full_tools,
            prior_results=state.get_results_as_context(),
            failed_step={"step_id": step.id, "tool": step.tool, "error": result.error},
        )
    except Exception as e:
        state.log("replan_failed", {"error": str(e)})
        return ExecutionResult(status="failed", message=result.message)
    if not new_plan.steps:
        return ExecutionResult(status="failed", message=f"Could not recover from error: {result.message}")
    state.plan = new_plan.model_dump()
    return execute_plan(new_plan, state, full_tools)


def _partial_or_failed(state: SessionState, fail_msg: str) -> ExecutionResult:
    """If a side effect already committed, report partial success + compensation;
    otherwise a clean failure (nothing was done)."""
    writes = _successful_writes(state)
    if not writes:
        return ExecutionResult(status="failed", message=fail_msg)

    done_lines, compensable = [], []
    for r in writes:
        done_lines.append(f"✅ {r.tool} — concluído.")
        inverse = get_compensating_tool(r.tool)
        if inverse:
            compensable.append(f"{r.tool} (desfazer com {inverse})")

    msg = (
        "Concluí parte do pedido:\n" + "\n".join(done_lines)
        + f"\n❌ Não concluí o restante: {fail_msg}"
    )
    if compensable:
        msg += "\n↩️ Posso desfazer, se você quiser: " + "; ".join(compensable)
    return ExecutionResult(status="partial", message=msg)


def _completed(state: SessionState) -> ExecutionResult:
    return ExecutionResult(
        status="completed",
        message="All steps completed successfully.",
        step_results=[
            {"step_id": r.step_id, "tool": r.tool, "kind": r.kind, "result": r.result}
            for r in state.step_results
            if r.success
        ],
    )


def _run_with_retry(step: PlanStep, state: SessionState) -> StepRunResult:
    """Run a step with one retry on transient (retryable) errors.

    Writes are NEVER retried: a 5xx/timeout can arrive AFTER the server already
    committed the mutation, and the mock API has no idempotency keys — a blind
    retry could double-create a coupon or double-cancel. Reads and computes are
    side-effect-free and safe to retry."""
    result = run_step(step, state)
    if result.status == "error" and result.retryable and not _is_write(step):
        state.log("retry", {"step_id": step.id, "reason": result.error})
        result = run_step(step, state)
    return result
