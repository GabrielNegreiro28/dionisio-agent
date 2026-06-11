import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, Optional

from config import MAX_FANOUT_SIZE
from clients.dionisio import DionisioClient, DionisioAPIError
from execution.resolver import resolve_params
from execution.data_ops import run_data_op, evaluate_check, DataOpError
from planning.schemas import PlanStep
from safety.risk_gate import RiskGate
from safety.validators import validate_params
from safety.policies import capability_problem
from state import SessionState, StepResult
from tools.registry import get_tool

_DEBUG = "--debug" in os.environ.get("AGENT_FLAGS", "") or os.path.exists("/tmp/.agent_debug")


def _dbg(msg: str) -> None:
    if _DEBUG:
        print(f"\033[90m[exec] {msg}\033[0m")


@dataclass
class StepRunResult:
    status: Literal["success", "needs_confirmation", "needs_info", "safe_alternative", "blocked", "halted", "error"]
    step_id: int
    result: Any = None
    message: str = ""
    confirmation_prompt: str = ""
    error: Optional[str] = None
    # Whether the error is transient (network/API) and worth retrying.
    # False for parameter/schema/logic errors — retrying won't help.
    retryable: bool = True


_dionisio = DionisioClient()
_risk_gate = RiskGate()


# ---------------------------------------------------------------------------
# Response normalization (shared by api + fanout sub-nodes)
# ---------------------------------------------------------------------------

def _normalize_response(result: Any) -> Any:
    """
    Normalize API responses to a consistent shape so $stepN.items[0].field
    references work. None→{}, list→{items:list}, single-key wrappers hoisted.
    """
    if result is None:
        return {}
    if isinstance(result, list):
        return {"items": result}
    if isinstance(result, dict):
        if len(result) == 1:
            (key, value) = next(iter(result.items()))
            if isinstance(value, list):
                return {"items": value}
            if isinstance(value, dict):
                return value
    return result


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _resolve_error(e: Exception, context: dict) -> str:
    """Enrich a $step resolution failure with the fields actually available at
    each prior step — fed to the replan so a weaker model can fix the reference."""
    shapes = {}
    for k, v in context.items():
        if isinstance(v, dict):
            shapes[k] = list(v.keys())
        elif isinstance(v, list):
            shapes[k] = f"list[{len(v)}]"
        else:
            shapes[k] = type(v).__name__
    return f"{e} | campos disponíveis por passo: {shapes}"


def precheck_write(step: PlanStep, state: SessionState) -> Optional[str]:
    """
    Validate a write step WITHOUT executing it (pre-commit): resolve params,
    schema-validate, and check capability constraints. Returns an operator-safe
    problem string, or None if the write is feasible. No side effects.
    """
    context = state.get_results_as_context()
    try:
        params = resolve_params(step.params, context)
    except (ValueError, KeyError, IndexError, TypeError):
        return f"Não consegui preparar os parâmetros do passo {step.id} ({step.tool})."
    try:
        tool_def = get_tool(step.tool)
    except KeyError:
        return f"Ferramenta '{step.tool}' não encontrada."
    clean = {k: v for k, v in params.items() if not k.startswith("__")}
    valid, error = validate_params(tool_def, clean)
    if not valid:
        return f"Passo {step.id} ({step.tool}): {error}"
    return capability_problem(step.tool, clean)


def run_step(step: PlanStep, state: SessionState) -> StepRunResult:
    """Execute a single plan node, dispatching on its kind."""
    kind = getattr(step, "kind", None) or "api"
    state.log("step_start", {"step_id": step.id, "kind": kind, "tool": step.tool})

    if state.on_progress:
        label = step.description or step.tool or step.operation or kind
        state.on_progress(label)

    if kind == "compute":
        return _run_compute(step, state)
    if kind == "verify":
        return _run_verify(step, state)
    if kind == "fanout":
        return _run_fanout(step, state)
    return _run_api(step, state)


# ---------------------------------------------------------------------------
# api node
# ---------------------------------------------------------------------------

def _run_api(step: PlanStep, state: SessionState) -> StepRunResult:
    try:
        tool_def = get_tool(step.tool)
    except KeyError as e:
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Ferramenta '{step.tool}' não encontrada.", retryable=False)

    context = state.get_results_as_context()
    try:
        resolved_params = resolve_params(step.params, context)
        _dbg(f"step {step.id} ({step.tool}) resolved={resolved_params}")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        # Technical detail goes to the log; the operator sees a clean message.
        state.log("resolve_error", {"step_id": step.id, "error": str(e),
                                    "params": step.params,
                                    "context_keys": list(context.keys())})
        return StepRunResult(
            status="error", step_id=step.id, error=_resolve_error(e, context),
            message=f"Não consegui usar o resultado de um passo anterior no passo {step.id}.",
            retryable=False,
        )

    # Risk gate
    decision = _risk_gate.evaluate(tool_def, resolved_params, state)
    state.log("risk_gate_decision", {"step_id": step.id, "tool": step.tool,
                                     "decision": decision.action, "risk_level": decision.risk_level,
                                     "reason": decision.reason})

    if decision.action == "block":
        return StepRunResult(status="blocked", step_id=step.id, message=decision.reason, retryable=False)
    if decision.action == "needs_confirmation":
        return StepRunResult(status="needs_confirmation", step_id=step.id,
                             confirmation_prompt=decision.confirmation_prompt, message=decision.reason,
                             result={"tool": step.tool, "params": resolved_params}, retryable=False)
    if decision.action == "needs_info":
        return StepRunResult(status="needs_info", step_id=step.id, message=decision.reason, retryable=False)
    if decision.action == "safe_alternative":
        return StepRunResult(status="safe_alternative", step_id=step.id, message=decision.reason, retryable=False)

    # Execute
    try:
        raw = _dionisio.execute_tool(tool_def, resolved_params)
        result = _normalize_response(raw)
        state.add_step_result(StepResult(step_id=step.id, tool=step.tool, params=resolved_params,
                                         result=result, success=True, kind="api"))
        state.log("step_success", {"step_id": step.id, "tool": step.tool})
        return StepRunResult(status="success", step_id=step.id, result=result)
    except DionisioAPIError as e:
        state.add_step_result(StepResult(step_id=step.id, tool=step.tool, params=resolved_params,
                                         result=None, success=False, error=str(e), kind="api"))
        state.log("step_error", {"step_id": step.id, "tool": step.tool, "error": str(e)})
        retryable = e.status_code >= 500 if e.status_code else True
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Erro na API ({step.tool}): {e.message}", retryable=retryable)
    except Exception as e:
        state.log("step_error", {"step_id": step.id, "tool": step.tool, "error": str(e)})
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Erro inesperado no passo {step.id}.", retryable=True)


# ---------------------------------------------------------------------------
# compute node (pure data-op, no risk gate, no network)
# ---------------------------------------------------------------------------

def _entity_label(item: Any) -> str:
    """Short human-readable label for a disambiguation option."""
    if not isinstance(item, dict):
        return str(item)
    name = (item.get("name") or item.get("clientName")
            or item.get("title") or item.get("label") or "")
    ident = item.get("id", "")
    extra = item.get("phone") or item.get("email") or item.get("date") or ""
    parts = [p for p in (name, extra) if p]
    label = " — ".join(str(p) for p in parts) or str(ident)
    return f"{label} (id={ident})" if ident and name else label


def _check_find_ambiguity(step: PlanStep, state: SessionState, resolved: dict) -> Optional[StepRunResult]:
    """
    For a 'find' whose result feeds a destructive write: if more than one record
    matches the predicate, return needs_info listing the candidates instead of
    silently acting on the first match (e.g. two clients named "João").
    """
    if step.id not in state.ambiguity_sensitive_ids:
        return None
    try:
        matches = run_data_op("filter", resolved)  # same params: source + where
    except DataOpError:
        return None  # let the normal 'find' path surface the real error
    if not isinstance(matches, list) or len(matches) <= 1:
        return None

    options = "\n".join(f"  {i + 1}. {_entity_label(m)}" for i, m in enumerate(matches[:5]))
    more = f"\n  … e mais {len(matches) - 5}." if len(matches) > 5 else ""
    state.log("ambiguous_find", {"step_id": step.id, "matches": len(matches)})
    return StepRunResult(
        status="needs_info", step_id=step.id, retryable=False,
        message=(
            f"Encontrei {len(matches)} registros compatíveis e a ação é irreversível — "
            f"não vou escolher por conta própria:\n{options}{more}\n"
            f"Refaça o pedido indicando qual deles (nome completo ou id)."
        ),
    )


def _run_compute(step: PlanStep, state: SessionState) -> StepRunResult:
    context = state.get_results_as_context()
    output_key = getattr(step, "output_key", None) or "result"
    try:
        resolved = resolve_params(step.params, context)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        state.log("resolve_error", {"step_id": step.id, "error": str(e)})
        return StepRunResult(status="error", step_id=step.id, error=_resolve_error(e, context),
                             message=f"Não consegui usar o resultado de um passo anterior no passo {step.id}.",
                             retryable=False)

    if (getattr(step, "operation", None) or "").lower() == "find":
        ambiguous = _check_find_ambiguity(step, state, resolved)
        if ambiguous is not None:
            return ambiguous

    try:
        value = run_data_op(step.operation, resolved)
    except DataOpError as e:
        state.log("compute_error", {"step_id": step.id, "operation": step.operation, "error": str(e)})
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Não consegui processar os dados no passo {step.id}: {e}",
                             retryable=False)

    result = {output_key: value}
    state.add_step_result(StepResult(step_id=step.id, tool=step.operation, params=resolved,
                                     result=result, success=True, kind="compute"))
    _dbg(f"compute step {step.id}: {step.operation} → {output_key}")
    return StepRunResult(status="success", step_id=step.id, result=result)


# ---------------------------------------------------------------------------
# verify node (gate a precondition; halt if it fails)
# ---------------------------------------------------------------------------

def _run_verify(step: PlanStep, state: SessionState) -> StepRunResult:
    context = state.get_results_as_context()
    try:
        resolved = resolve_params(step.params, context)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        state.log("resolve_error", {"step_id": step.id, "error": str(e)})
        return StepRunResult(status="error", step_id=step.id, error=_resolve_error(e, context),
                             message=f"Não consegui avaliar a verificação do passo {step.id}.",
                             retryable=False)

    # The value under test: explicit 'value', else 'source'.
    value = resolved.get("value", resolved.get("source"))
    try:
        passed = evaluate_check(value, step.check)
    except DataOpError as e:
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Verificação inválida no passo {step.id}.", retryable=False)

    state.add_step_result(StepResult(step_id=step.id, tool="__verify__", params=resolved,
                                     result={"passed": passed}, success=True, kind="verify"))
    _dbg(f"verify step {step.id}: check={step.check} value={value!r} → {passed}")

    if passed:
        return StepRunResult(status="success", step_id=step.id, result={"passed": True})

    msg = step.on_fail or "A condição necessária para concluir a ação não foi atendida."
    return StepRunResult(status="halted", step_id=step.id, message=msg, retryable=False)


# ---------------------------------------------------------------------------
# fanout node (run an api|compute sub-node per element, collect)
# ---------------------------------------------------------------------------

def _as_param_of(step: PlanStep):
    return getattr(step, "as_param", None)


def _run_fanout(step: PlanStep, state: SessionState) -> StepRunResult:
    context = state.get_results_as_context()
    output_key = getattr(step, "output_key", None) or "items"
    collect = getattr(step, "collect", None) or "list"

    try:
        elements = resolve_params({"over": step.over}, context)["over"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        state.log("resolve_error", {"step_id": step.id, "error": str(e)})
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Não consegui montar a lista do passo {step.id}.", retryable=False)

    if not isinstance(elements, list):
        return StepRunResult(status="error", step_id=step.id,
                             error=f"'over' resolved to {type(elements).__name__}",
                             message=f"O passo {step.id} esperava uma lista para iterar.", retryable=False)

    if len(elements) > MAX_FANOUT_SIZE:
        return StepRunResult(
            status="error", step_id=step.id,
            error=f"fanout size {len(elements)} > {MAX_FANOUT_SIZE}",
            message=(f"O passo {step.id} tentaria iterar {len(elements)} vezes "
                     f"(limite {MAX_FANOUT_SIZE}). Reduza o período ou a lista."),
            retryable=False,
        )

    as_name = _as_param_of(step)
    sub = step.node

    def _one(el):
        extra = {"item": el}
        if as_name:
            extra[as_name] = el
        return _run_subnode(sub, context, extra)

    # Sub-nodes are read-only and independent → run them concurrently.
    # httpx.Client is thread-safe; executor.map preserves element order.
    # 31 sequential day-by-day calls (~90s) drop to a few seconds.
    try:
        if len(elements) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(elements))) as pool:
                collected = list(pool.map(_one, elements))
        else:
            collected = [_one(el) for el in elements]
    except DionisioAPIError as e:
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Erro na API durante a iteração do passo {step.id}: {e.message}",
                             retryable=(e.status_code >= 500 if e.status_code else True))
    except DataOpError as e:
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Erro ao processar a iteração do passo {step.id}: {e}", retryable=False)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        # Bad $ref / missing tool inside the sub-node — would otherwise crash the
        # whole session (no try/except upstream). Turn into a clean failure.
        state.log("fanout_error", {"step_id": step.id, "error": str(e)})
        return StepRunResult(status="error", step_id=step.id, error=str(e),
                             message=f"Não consegui executar a iteração do passo {step.id}.",
                             retryable=False)

    if collect == "merge_items":
        merged: list = []
        for r in collected:
            if isinstance(r, dict) and isinstance(r.get("items"), list):
                merged.extend(r["items"])
            elif isinstance(r, list):
                merged.extend(r)
            elif r is not None:
                merged.append(r)
        value: Any = merged
    else:
        value = collected

    result = {output_key: value}
    state.add_step_result(StepResult(step_id=step.id, tool="__fanout__", params={"over_count": len(elements)},
                                     result=result, success=True, kind="fanout"))
    _dbg(f"fanout step {step.id}: {len(elements)} iterations → {output_key} ({collect})")
    return StepRunResult(status="success", step_id=step.id, result=result)


def _run_subnode(sub: PlanStep, context: dict, extra: dict) -> Any:
    """Execute one fanout body node (api|compute) and return its value.
    Read-oriented: api sub-nodes skip the risk gate (fanout is for fetching)."""
    sub_kind = getattr(sub, "kind", None) or "api"
    resolved = resolve_params(sub.params, context, extra=extra)
    if sub_kind == "compute":
        return run_data_op(sub.operation, resolved)
    tool_def = get_tool(sub.tool)
    # Fanout bodies are read-only (they skip the risk gate). A write per element
    # would bypass confirmation + the two-phase discipline — block it.
    if tool_def.method.upper() != "GET":
        raise DataOpError(
            f"fanout só itera leituras; '{sub.tool}' é uma escrita (não-GET) e não pode rodar por elemento."
        )
    raw = _dionisio.execute_tool(tool_def, resolved)
    return _normalize_response(raw)
