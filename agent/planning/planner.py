import json
from datetime import datetime, timezone, timedelta
from clients.llm import LLMClient
from planning.schemas import Plan
from tools.definitions import ToolDefinition, DATA_MODEL, CONVENTIONS
from execution.data_ops import describe_ops, COMPARATORS

_llm = LLMClient()

_SYSTEM_TEMPLATE = """You are a planning agent for a restaurant CRM assistant (Dionísio).
Produce a JSON execution plan as a graph of typed NODES. There are four node kinds.

== NODE KINDS ==

1) api — call a tool (the ONLY way to read or change data; goes through a risk gate).
   {{ "id":1, "kind":"api", "description":"...", "tool":"tool_name",
      "params":{{ "p":"value or $stepN.path" }}, "depends_on":[] }}

2) compute — a DETERMINISTIC data operation over prior results. NO tool, NO LLM.
   {{ "id":2, "kind":"compute", "description":"...", "operation":"filter",
      "params":{{ "source":"$step1.items", "where":{{...}} }},
      "output_key":"matches", "depends_on":[1] }}
   Result is read later as $step2.matches.
   Available operations:
{data_ops}
   PREDICATES (for filter/find/count) are declarative and restricted:
     {{ "field":"<path>", "cmp":"<comparator>", "value":<literal> }}
     "where" may be ONE predicate or a LIST of predicates (AND-ed).
     field paths support dot, index and wildcard: "name", "duration.start",
     "items[].name" (ANY element), "items[0].name".
     comparators: {comparators}
     ("contains" on strings is case-insensitive substring.)

3) verify — assert a precondition over a prior result; HALTS the plan if false.
   {{ "id":3, "kind":"verify", "description":"...",
      "params":{{ "value":"$step4.availableSeats" }},   // or "source":"$stepN.items"
      "check":{{ "cmp":"gt", "value":0 }},               // or "non_empty"/"empty"/"truthy"/"falsy"
      "on_fail":"Não há mesa disponível nesse horário.", "depends_on":[4] }}
   Put a verify BEFORE a destructive/important step whenever the request has a condition
   ("... e confirma se tem mesa", "se houver ...", "caso exista ...").

4) fanout — run an api|compute sub-node ONCE PER element of a list, collect results.
   {{ "id":1, "kind":"fanout", "description":"pedidos de cada dia dos últimos 7 dias",
      "over":["2026-06-03","2026-06-04","2026-06-05","2026-06-06","2026-06-07","2026-06-08","2026-06-09"],
      "as":"date",
      "node":{{ "id":11, "kind":"api", "tool":"orders_list", "params":{{ "date":"$date" }} }},
      "output_key":"items", "collect":"merge_items", "depends_on":[] }}
   Inside the sub-node, reference the current element as $<as> (e.g. $date) or $item.
   "over" may be a literal list or a $stepN ref. "collect":"merge_items" concatenates each
   result's items into one list; "list" (default) keeps a list of results.
   The sub-node MUST be read-only (an api GET tool) or compute — NEVER a write. A write per
   element bypasses confirmation; to write per item, that's not supported here.
   Para TOTAIS/agregados ao longo de muitos dias (ex: "faturamento/pedidos de cada dia do
   ano"), use uma tool analytics_* com periodStart/periodEnd (UMA chamada) — NÃO faça fanout
   dia a dia. Fanout só para poucos elementos (no máximo ~1 mês).

== HARD RULES ==
- Use ONLY tools from the provided list, with their EXACT parameter names. Never invent either.
- ALL filtering, selection, counting, sorting and derivation MUST be compute nodes.
  The response layer only DESCRIBES results — it never filters or counts. If the operator
  asks "quantos / quais / que nunca / que mais", produce a compute (filter/find/count) for it.
- To pick a specific entity by name/attribute, use compute "find" with a predicate —
  NEVER guess items[0].
- Reference ONLY fields documented in the DATA MODEL / each tool's "Returns". NEVER invent
  field names (e.g. a Reservation has clientId, NOT client.name) or enum values (use the exact
  vocabularies given). When unsure how to read a result, re-read its Returns shape.
- Respect the CONVENTIONS (units, timestamps, "lugares = slots[].free"). Money in orders is in
  centavos; do not confuse counts of items with sums of values.
- $stepN.path references are pass-through only. For DERIVED values use compute arithmetic
  (e.g. add($stepM.duration.start, 172800000) → +2 days; multiply($price, 0.85) → 15% off).
- Timestamps (type "integer ms") are concrete integers; date fields (type "string YYYY-MM-DD")
  are strings like "2026-06-09".
- depends_on must list the ids this node reads from, and they must be earlier nodes.

== CAPABILITIES & PARTIAL FULFILLMENT ==
- The system CANNOT send messages/notifications to customers, and CANNOT manage menu items
  (add/remove dishes). There are no tools for these.
- coupons_assign_group atribui um cupom a TODOS os clientes de um GRUPO EXISTENTE
  (groupId = "grp_..."). NÃO existe ferramenta para atribuir cupom a clientes individuais/avulsos
  (ex: a uma lista de inativos), nem para criar grupos. Se o pedido for atribuir a clientes
  específicos, NÃO use coupons_assign_group com um clientId — marque a atribuição em "unsupported"
  e faça só o que é possível (ex: criar o cupom, listar os clientes-alvo).
- If the request asks for something with no tool, DO NOT give up on the rest. Plan every part
  you CAN do (e.g. "quem pediu o prato X nos últimos 7 dias" via fanout+filter), and list the
  impossible parts in the top-level "unsupported" array (short PT-BR phrases).
- Only return steps:[] when EVERY part is impossible; then explain why in "description".

- Write every "description" in Brazilian Portuguese.
- Output ONLY valid JSON (no markdown, no preamble) matching:
{{
  "description": "...",
  "steps": [ {{ "id":1, "kind":"api", "description":"...", "tool":"...", "params":{{}}, "depends_on":[] }} ],
  "unsupported": []
}}"""

_EXAMPLES = '''Exemplo 1 — "clientes que gastaram mais de R$500 no mês e nunca usaram cupom":
{
  "description": "Clientes >R$500 no mês sem cupom",
  "steps": [
    {"id": 1, "kind": "api", "tool": "clients_top_spenders", "params": {"period": "month", "minSpent": 500, "limit": 100}},
    {"id": 2, "kind": "compute", "operation": "filter", "params": {"source": "$step1.items", "where": {"field": "couponsUsed", "cmp": "eq", "value": 0}}, "output_key": "semCupom", "depends_on": [1]},
    {"id": 3, "kind": "compute", "operation": "count", "params": {"source": "$step2.semCupom"}, "output_key": "total", "depends_on": [2]}
  ],
  "unsupported": []
}
(em TopSpenderItem, couponsUsed e spent são campos de TOPO; nome/grupo ficam em client.* — ex: client.clientGroupIds)

Exemplo 2 — "qual a taxa de no-show deste mês?" (tool analytics_* = UMA chamada, sem compute):
{
  "description": "Taxa de no-show do mês",
  "steps": [
    {"id": 1, "kind": "api", "tool": "analytics_reservations", "params": {"periodStart": <início do mês ms>, "periodEnd": <agora ms>}}
  ],
  "unsupported": []
}
(use as faixas de data prontas; a resposta lê noShowRate do objeto — NUNCA aplique filter/count numa analytics_*)

Exemplo 3 — "a reserva do João" (resolve por nome → clientId; verify antes de agir):
{
  "description": "Reserva do cliente João",
  "steps": [
    {"id": 1, "kind": "api", "tool": "clients_search", "params": {"name": "João"}},
    {"id": 2, "kind": "verify", "params": {"source": "$step1.items"}, "check": "non_empty", "on_fail": "Cliente João não encontrado.", "depends_on": [1]},
    {"id": 3, "kind": "compute", "operation": "find", "params": {"source": "$step1.items", "where": {"field": "name", "cmp": "contains", "value": "João"}}, "output_key": "cliente", "depends_on": [1]},
    {"id": 4, "kind": "api", "tool": "clients_reservations", "params": {"clientId": "$step3.cliente.id"}, "depends_on": [3]}
  ],
  "unsupported": []
}

Exemplo 4 — "cria uma campanha de reativação para inativos há 60 dias com cupom de 15%" (CRIAÇÃO + fulfillment parcial — sem verify; o que não tem ferramenta vai em "unsupported", NÃO vira passo):
{
  "description": "Campanha de reativação: cria o cupom e lista os inativos",
  "steps": [
    {"id": 1, "kind": "api", "tool": "clients_inactive", "params": {"days": 60}},
    {"id": 2, "kind": "api", "tool": "coupons_create", "params": {"name": "Reativação 15%", "type": "uniqueCode", "benefitText": "15% de desconto"}}
  ],
  "unsupported": ["Atribuir o cupom a clientes individuais (coupons_assign_group só atribui a um grupo grp_*, não a clientes avulsos)", "Notificar os clientes (sem ferramenta de mensagens)"]
}
(criar é direto: poucas chamadas, SEM verify desnecessário; partes sem ferramenta vão em unsupported)'''

# NOTE: built once at import. It's a large prompt (~3-5k tokens: node spec +
# data-op catalog + DATA MODEL + conventions + examples) paid on EVERY planner
# call, even simple ones. Acceptable for this case; if cost matters, trim the
# examples/data-model per detected intent or cache by domain set.
_SYSTEM = (
    _SYSTEM_TEMPLATE.format(
        data_ops="\n".join("     " + line for line in describe_ops().splitlines()),
        comparators=", ".join(sorted(COMPARATORS)),
    )
    + "\n\n== DATA MODEL ==\n" + DATA_MODEL
    + "\n\n== " + CONVENTIONS
    + "\n\n== EXEMPLOS ==\n" + _EXAMPLES
)


def _format_tools(tools: list[ToolDefinition]) -> str:
    """
    Render a full, precise tool catalog for the planner.
    Each tool shows: name, destructive flag, description, and every parameter
    with its exact name, type, required/optional status, and constraints.
    """
    lines = []
    for t in tools:
        header = f"### {t.name}" + (" ⚠️ DESTRUCTIVE" if t.destructive else "")
        lines.append(header)
        lines.append(t.description.strip())
        lines.append("Params:")

        schema = t.parameters_schema
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        if not properties:
            lines.append("  (no parameters)")
        else:
            for param_name, param_schema in properties.items():
                req_flag = "REQUIRED" if param_name in required else "optional"
                type_str = _describe_type(param_schema)
                constraints = _describe_constraints(param_schema)
                constraint_str = f" — {constraints}" if constraints else ""
                lines.append(f"  {param_name} ({type_str}, {req_flag}){constraint_str}")

        if t.path_params:
            lines.append(f"  [path params: {', '.join(t.path_params)}]")

        if t.returns:
            lines.append(f"  Returns: {t.returns}")

        lines.append("")  # blank line between tools

    return "\n".join(lines)


def _describe_type(schema: dict) -> str:
    """Derive a human-readable type label, including semantic subtypes."""
    t = schema.get("type", "any")
    fmt = schema.get("format", "")

    # Explicit JSON Schema format annotations
    if fmt in ("int64", "timestamp"):
        return "integer ms"
    if fmt == "date":
        return "string YYYY-MM-DD"

    # Heuristic fallback: description starts with date pattern
    desc = schema.get("description", "")
    if t == "string" and desc.upper().strip().startswith("YYYY"):
        return "string YYYY-MM-DD"

    return t


def _describe_constraints(schema: dict) -> str:
    parts = []
    if "enum" in schema:
        parts.append(f"one of: {schema['enum']}")
    if "default" in schema:
        parts.append(f"default: {schema['default']}")
    if "minimum" in schema:
        parts.append(f"min: {schema['minimum']}")
    desc = schema.get("description", "")
    if desc and not desc.upper().strip().startswith("YYYY"):
        parts.append(desc)
    return "; ".join(parts)


def _current_datetime_str() -> str:
    """Return a datetime string with local timezone for unambiguous timestamp math."""
    now_local = datetime.now().astimezone()
    tz_offset = now_local.strftime("%z")   # e.g. "-0300"

    if tz_offset:
        sign = tz_offset[0]
        hh = int(tz_offset[1:3])
        mm = int(tz_offset[3:5])
        tz_display = f"UTC{sign}{hh}" if mm == 0 else f"UTC{sign}{hh}:{mm:02d}"
    else:
        tz_display = "UTC"

    ts_ms = int(now_local.timestamp() * 1000)
    return (
        f"Current datetime: {now_local.strftime('%Y-%m-%d %H:%M')} "
        f"({tz_display}, timestamp ms: {ts_ms})"
    )


def _date_ranges_str() -> str:
    """Ready-made epoch-ms ranges so the planner never does timestamp arithmetic
    (a common failure for weaker models). Analytics params take these directly."""
    now = datetime.now().astimezone()
    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)
    return (
        "Faixas de data prontas (epoch ms) — use direto em periodStart/periodEnd, "
        "NÃO calcule timestamps:\n"
        f"- hoje: {ms(today_start)}–{ms(now)} (data de hoje: {today_start.strftime('%Y-%m-%d')})\n"
        f"- este mês: {ms(month_start)}–{ms(now)}\n"
        f"- últimos 7 dias: {ms(d7)}–{ms(now)}\n"
        f"- últimos 30 dias: {ms(d30)}–{ms(now)}"
    )


def _parse_plan(raw: str) -> Plan:
    """Strip markdown fences, parse JSON, and validate into a Plan (raises on any problem)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    data = json.loads(raw)
    return Plan.model_validate(data)


def build_plan(
    query: str,
    tools: list[ToolDefinition],
    prior_results: dict = None,
    failed_step: dict = None,
    conversation_history: list[dict] = None,
) -> Plan:
    """
    Ask the LLM to produce a Plan for the given query and available tools.
    prior_results and failed_step are provided during replanning.
    """
    current_time = _current_datetime_str() + "\n" + _date_ranges_str()
    tool_catalog = _format_tools(tools)

    if conversation_history:
        history_text = "\n".join(
            f"{'Operator' if m['role'] == 'user' else 'Agent'}: {m['content']}"
            for m in conversation_history[-6:]
        )
        user_content = (
            f"{current_time}\n\n"
            f"Conversation so far:\n{history_text}\n\n"
            f"Available tools:\n{tool_catalog}\n\n"
            f"Latest operator message: {query}"
        )
    else:
        user_content = (
            f"{current_time}\n\n"
            f"Available tools:\n{tool_catalog}\n\n"
            f"Operator request: {query}"
        )

    if prior_results:
        context = json.dumps(prior_results, ensure_ascii=False, indent=2)
        user_content += f"\n\nPrior step results (for replanning):\n{context}"

    if failed_step:
        user_content += f"\n\nFailed step: {json.dumps(failed_step, ensure_ascii=False)}"

    messages = [
        # Prompt caching (Anthropic via OpenRouter): o bloco de sistema é estático
        # (montado uma vez no import), então marcá-lo com cache_control o torna um
        # prefixo cacheado — leituras custam ~0.1x do preço de input (TTL 5 min,
        # renovado a cada hit; escrita 1.25x, paga-se em 2 chamadas). Todo o
        # conteúdo VOLÁTIL (datas, histórico, query) fica na mensagem de usuário,
        # DEPOIS do breakpoint — não invalida o cache. Modelos não-Anthropic
        # ignoram o campo sem erro.
        {"role": "system", "content": [
            {"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}
        ]},
        {"role": "user", "content": user_content},
    ]

    raw = _llm.text(messages)
    try:
        return _parse_plan(raw)
    except Exception as first_error:
        # One self-repair attempt: show the model its own output and the exact
        # error, and ask for corrected JSON. LLMs occasionally slip on node
        # format; a single targeted retry fixes the vast majority of cases.
        repair_messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": (
                "O JSON acima é inválido. Erro de validação:\n"
                f"{first_error}\n\n"
                "Corrija e devolva APENAS o JSON do plano válido (sem markdown, sem comentários), "
                "seguindo exatamente o schema dos nós. Lembre: em um nó 'verify', 'check' deve ser "
                "uma palavra-chave ('non_empty'|'empty'|'truthy'|'falsy') OU um objeto {cmp, value}."
            )},
        ]
        repaired = _llm.text(repair_messages)
        return _parse_plan(repaired)
