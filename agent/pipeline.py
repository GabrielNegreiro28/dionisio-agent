import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from clients.llm import LLMClient
from execution.executor import ExecutionResult, execute_plan
from memory import ConversationMemory
from planning.planner import build_plan
from planning.schemas import Plan
from retrieval.classifier import classify_domains
from retrieval.rewriter import rewrite_query
from retrieval.selector import NoRelevantToolsError, select_tools
from state import SessionState
from tools.registry import get_openai_tools_for_domains, get_all_tools
from knowledge.retriever import knowledge_context

@dataclass
class PipelineResult:
    status: Literal["completed", "needs_confirmation", "needs_info", "safe_alternative", "blocked", "halted", "partial", "failed", "no_tools"]
    response: str
    pending_step_id: Optional[int] = None
    pending_plan: Optional[Plan] = None
    pending_step: Optional[Any] = None  # StepRunResult — carries pre-resolved params
    state: Optional[SessionState] = None


_llm = LLMClient()

_RESPONSE_SYSTEM = """Você é um assistente para operadores de restaurante que usam o CRM Dionísio.
Sua função é DESCREVER resultados já calculados — não calcular nada.

REGRAS DE FIDELIDADE (obrigatórias):
- Descreva SOMENTE o que está explícito nos resultados fornecidos.
- NUNCA conte, filtre, some, ordene ou deduza valores por conta própria. Se um número
  (contagem, total) é necessário, ele já está nos resultados — use exatamente esse valor.
- Se um dado não está nos resultados, diga que não foi apurado. NUNCA invente nomes,
  quantidades, IDs ou status.
- Se houver uma lista, use os itens exatamente como vieram; não acrescente nem remova.

Se foram informadas ações NÃO suportadas (sem ferramenta disponível), diga com clareza o que
não pôde ser feito automaticamente e ofereça ajuda útil (ex: rascunhar a mensagem, listar quem
seria afetado) — sem prometer que executou.

Escreva em português brasileiro, claro e conciso. Não exponha IDs internos a menos que o
operador precise deles.

UNIDADES: valores monetários de pedidos e de analytics estão em CENTAVOS — qualquer campo com
sufixo "Cents" (revenueCents, averageTicketCents, unitPrice, total, subtotal, deliveryFee,
minimumOrderValue, etc.) deve ser dividido por 100 e apresentado em reais (ex: 3000 → R$ 30,00).
Já 'spent' / 'minSpent' (top spenders) já estão em reais.

TRUNCAMENTO: um item "__truncado__" numa lista indica que ela foi cortada para caber no contexto.
Use o TOTAL REAL indicado no marcador ao citar quantidades, mostre só os itens presentes e avise
que a lista exibida é parcial."""

_CONVERSATIONAL_SYSTEM = """Você é um assistente inteligente para operadores de restaurante (CRM Dionísio).
O operador fez uma solicitação que não requer chamadas à API — pode ser para gerar conteúdo,
rascunhar mensagens, responder uma pergunta ou dar continuidade à conversa.

Se for possível atender com base no histórico da conversa e conhecimento geral, faça isso diretamente.
Se não for possível (a ação requer o sistema externo que você não controla), explique brevemente
o que pode ser feito manualmente e ofereça o que você PODE fazer (ex: rascunhar o texto).

Quando forem fornecidos TRECHOS DA DOCUMENTAÇÃO do Dionísio, use-os como base para perguntas sobre
como o sistema/regras/funcionalidades funcionam, e cite a seção (ex: "conforme a seção X"). Não invente
regras que não estejam nos trechos; se eles não cobrirem a pergunta, diga que não encontrou na documentação.

Responda em português brasileiro, de forma útil e direta. Sem listas de bullets a menos que o conteúdo
seja naturalmente enumerável."""


def run(
    query: str,
    state: Optional[SessionState] = None,
    memory: Optional[ConversationMemory] = None,
) -> PipelineResult:
    """
    Full pipeline: rewrite → classify → select tools → plan → execute → respond.
    memory: ConversationMemory instance carrying short-term + long-term context.
    """
    if state is None:
        state = SessionState()
    state.original_query = query

    history = memory.get_context() if memory and not memory.is_empty() else []

    # 1. Query rewriting — history used only for reference resolution, not merging.
    # With no history there's nothing to resolve: skip the LLM round-trip entirely
    # (saves ~1-3s on every first/single-turn query).
    if history:
        rewritten = rewrite_query(query, history=history)
        state.log("query_rewritten", {"original": query, "rewritten": rewritten})
    else:
        rewritten = query
        state.log("query_rewrite_skipped", {"reason": "no history"})
    state.rewritten_query = rewritten

    # 2. Domain classification — based on current query only, not history
    domains = classify_domains(query, rewritten)
    state.domains = domains
    state.log("domains_classified", {"domains": domains})

    # 3. Tool selection
    try:
        tools = select_tools(domains)
        state.selected_tools = [t.name for t in tools]
    except NoRelevantToolsError:
        # No domain found → try to answer conversationally
        response = _generate_conversational_response(query, history)
        return PipelineResult(status="no_tools", response=response, state=state)

    # 4. Planning — with a full-catalog fallback. The classifier only narrows the
    # tool list to keep the prompt cheap; if planning fails with the narrow set,
    # retry once with ALL tools so a classifier miss can't starve the planner of a
    # tool that actually exists (recall safety net). The validator + self-repair
    # inside build_plan handle malformed plans before we ever get here.
    _clean_fail = "Não consegui montar um plano para esse pedido. Pode reformular ou dar um pouco mais de detalhe?"
    if state.on_progress:
        state.on_progress("montando o plano…")
    try:
        plan = build_plan(query=rewritten, tools=tools, conversation_history=history)
    except Exception as e:
        state.log("plan_build_failed", {"stage": "classified", "error": str(e)})
        full = get_all_tools()
        if len(full) <= len(tools):
            return PipelineResult(status="failed", response=_clean_fail, state=state)
        try:
            plan = build_plan(query=rewritten, tools=full, conversation_history=history)
            state.selected_tools = [t.name for t in full]
            state.log("plan_fallback_full_catalog", {"steps": len(plan.steps)})
        except Exception as e2:
            state.log("plan_build_failed", {"stage": "fallback", "error": str(e2)})
            return PipelineResult(status="failed", response=_clean_fail, state=state)

    state.plan = plan.model_dump()
    state.log("plan_built", {"steps": len(plan.steps)})

    # Empty plan = tools exist but can't satisfy this specific request
    if not plan.steps:
        response = _generate_conversational_response(query, history, cant_do_reason=plan.description)
        return PipelineResult(status="no_tools", response=response, state=state)

    # 5. Execution — guarded: any unexpected exception becomes a clean failure,
    # never a crash of the caller (e.g. the REPL).
    try:
        result = execute_plan(plan, state, tools)
    except Exception as e:
        state.log("execute_crash", {"error": str(e)})
        return PipelineResult(
            status="failed",
            response="Não consegui concluir o pedido por um erro interno. Pode tentar reformular?",
            state=state,
        )
    return _build_pipeline_result(result, plan, state)


def resume(
    state: SessionState,
    pending_plan: Plan,
    confirmed_step_id: int,
    operator_confirmed: bool,
    confirmed_params: dict = None,
) -> PipelineResult:
    """
    Resume execution after operator responds to a confirmation request.
    confirmed_params: pre-resolved params from the first pass (avoids re-resolution failures).
    """
    if not operator_confirmed:
        state.log("confirmation_denied", {"step_id": confirmed_step_id})
        return PipelineResult(
            status="completed",
            response="Operação cancelada conforme solicitado.",
            state=state,
        )

    # Update state.plan so debug output reflects what's actually executing
    state.plan = pending_plan.model_dump()

    tools = select_tools(state.domains)
    try:
        result = execute_plan(
            pending_plan, state, tools,
            confirmed_step_id=confirmed_step_id,
            confirmed_params=confirmed_params,
        )
    except Exception as e:
        state.log("execute_crash", {"error": str(e)})
        return PipelineResult(
            status="failed",
            response="Não consegui concluir a ação por um erro interno.",
            state=state,
        )
    return _build_pipeline_result(result, pending_plan, state)


def _build_pipeline_result(
    result: ExecutionResult,
    plan: Plan,
    state: SessionState,
) -> PipelineResult:
    if result.status == "needs_confirmation":
        # Prefer the plan from the executor (may be a replanned plan with different step IDs)
        effective_plan = result.pending_plan or plan
        return PipelineResult(
            status="needs_confirmation",
            response=result.message,
            pending_step_id=result.pending_step.step_id if result.pending_step else None,
            pending_plan=effective_plan,
            pending_step=result.pending_step,
            state=state,
        )

    if result.status in ("needs_info", "safe_alternative"):
        return PipelineResult(
            status=result.status,
            response=result.message,
            pending_plan=plan,
            state=state,
        )

    if result.status == "blocked":
        return PipelineResult(
            status="blocked",
            response=f"Operação bloqueada: {result.message}",
            state=state,
        )

    # A verify precondition failed — surface the operator-facing message verbatim.
    if result.status == "halted":
        return PipelineResult(status="halted", response=result.message, state=state)

    # Some writes committed but a later step failed — report verbatim (already
    # lists what was done + compensation offer).
    if result.status == "partial":
        return PipelineResult(status="partial", response=result.message, state=state)

    if result.status == "failed":
        return PipelineResult(
            status="failed",
            response=f"Não consegui concluir o pedido: {result.message}",
            state=state,
        )

    # Completed — describe the FINAL (leaf) results only, so the response is grounded
    # in what was actually computed and never re-derives intermediate data.
    visible_results = _visible_results(result.step_results or [], plan)
    unsupported = list(getattr(plan, "unsupported", []) or [])
    if state.on_progress:
        state.on_progress("redigindo a resposta…")
    response = _generate_response(state.original_query, visible_results, unsupported)
    return PipelineResult(status="completed", response=response, state=state)


# Compute ops that produce a NEW collection/object from a source — i.e. they
# "refine away" their input, so the input is intermediate and should be hidden.
# Aggregates/derivations (count, add, ...) summarize the input but do NOT replace
# it as the displayable answer, so their inputs stay visible.
_LIST_REFINING_OPS = {"filter", "find", "sort", "slice", "project", "dedup"}


def _refined_step_ids(plan: Plan) -> set:
    """Step ids whose result is superseded by a later list-refining compute step."""
    refined: set = set()

    def scan(obj: Any) -> None:
        if isinstance(obj, str):
            refined.update(int(n) for n in re.findall(r"\$step(\d+)", obj))
        elif isinstance(obj, dict):
            for v in obj.values():
                scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)

    for s in plan.steps:
        if getattr(s, "kind", None) == "compute" and getattr(s, "operation", None) in _LIST_REFINING_OPS:
            scan(s.params)
    return refined


def _visible_results(step_results: list[dict], plan: Plan) -> list[dict]:
    """
    Ground the response on the answer-bearing results: drop verify gates and
    any step refined away by a later list op (e.g. the raw list a filter narrowed).
    A list summarized only by a count stays visible — so both the list and its
    count reach the response. Falls back to all non-verify results if empty.
    """
    refined = _refined_step_ids(plan)
    desc_by_id = {s.id: s.description for s in plan.steps}

    def enrich(r: dict) -> dict:
        return {
            "step_id": r.get("step_id"),
            "description": desc_by_id.get(r.get("step_id"), ""),
            "result": r.get("result"),
        }

    leaves = [
        enrich(r) for r in step_results
        if r.get("kind") != "verify" and r.get("step_id") not in refined
    ]
    if leaves:
        return leaves
    return [enrich(r) for r in step_results if r.get("kind") != "verify"]


# Response-context budget. Unbounded results (e.g. a fanout over months of
# orders) blow the LLM's output budget — reasoning models then spend every
# output token thinking and return EMPTY text. Truncating with explicit
# markers keeps the response grounded AND guarantees it exists.
_MAX_LIST_ITEMS = 50
_MAX_CONTEXT_CHARS = 60_000

_WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira",
                "quinta-feira", "sexta-feira", "sábado", "domingo"]


def _now_line() -> str:
    now = datetime.now().astimezone()
    return (f"Data/hora atual: {_WEEKDAYS_PT[now.weekday()]}, "
            f"{now.strftime('%d/%m/%Y %H:%M')}")


def _shrink(obj: Any, max_items: int) -> Any:
    """Trim lists to max_items, appending a marker with the real total so the
    response can still state correct counts."""
    if isinstance(obj, list):
        if len(obj) > max_items:
            head = [_shrink(v, max_items) for v in obj[:max_items]]
            head.append({"__truncado__": f"mostrando {max_items} de {len(obj)} itens (total real: {len(obj)})"})
            return head
        return [_shrink(v, max_items) for v in obj]
    if isinstance(obj, dict):
        return {k: _shrink(v, max_items) for k, v in obj.items()}
    return obj


def _bounded_context(step_results: list[dict]) -> str:
    """JSON context for the response LLM, guaranteed under _MAX_CONTEXT_CHARS."""
    trimmed = _shrink(step_results, _MAX_LIST_ITEMS)
    context = json.dumps(trimmed, ensure_ascii=False, indent=2)
    if len(context) > _MAX_CONTEXT_CHARS:
        trimmed = _shrink(step_results, 10)
        context = json.dumps(trimmed, ensure_ascii=False, indent=2)
    return context[:_MAX_CONTEXT_CHARS]


def _generate_response(
    original_query: str,
    step_results: list[dict],
    unsupported: list[str] = None,
) -> str:
    """Render a grounded natural-language summary of the final results."""
    context = _bounded_context(step_results)
    user_content = (
        f"{_now_line()}\n\n"
        f"Pedido do operador: {original_query}\n\nResultados (já calculados):\n{context}"
    )
    if unsupported:
        items = "\n".join(f"- {u}" for u in unsupported)
        user_content += (
            f"\n\nAções solicitadas SEM ferramenta disponível (não executadas):\n{items}"
        )
    messages = [
        {"role": "system", "content": _RESPONSE_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    resp = _llm.text(messages)
    if not (resp or "").strip():
        # Ex: resultado grande demais estourou o orçamento de saída → texto vazio.
        return ("Apurei os dados, mas não consegui montar o resumo (resultado muito grande). "
                "Tente um recorte menor — um período mais curto ou um filtro mais específico.")
    return resp


def _generate_conversational_response(
    original_query: str,
    history: list[dict],
    cant_do_reason: str = "",
) -> str:
    """
    For requests that don't map to API tools, generate a helpful response directly.
    If there's conversation context, the LLM can generate content (e.g., WhatsApp messages)
    or explain what can be done manually — whichever is more useful.
    """
    messages = [{"role": "system", "content": _CONVERSATIONAL_SYSTEM}]

    if history:
        messages.extend(history[-6:])

    user_content = f"{_now_line()}\n\nPedido: {original_query}"
    if cant_do_reason:
        user_content += f"\n\n(Contexto interno: {cant_do_reason})"

    # Grounding pela documentação real (GitBook) para perguntas de regra/negócio.
    doc = knowledge_context(original_query)
    if doc:
        user_content += (
            "\n\nTrechos da documentação do Dionísio (use se responderem a pergunta; cite a seção):\n"
            + doc
        )

    messages.append({"role": "user", "content": user_content})
    return _llm.text(messages)
