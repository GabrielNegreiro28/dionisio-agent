"""
Benchtest final — exercita o agente Dionísio ponta a ponta e gera um relatório
(benchtest_report.md). Precisa do ambiente vivo (.env com as chaves), como o main.py.

Cobertura:
- LEITURA em todos os domínios (com ORÁCULOS contra a API quando há verdade-base)
- FLUXO DESTRUTIVO: gate → "confirmo" (executa) e negação (cancela)
- BORDERLINE: ambíguo (sem id), fora de escopo, cap de fanout, verdadeiro-negativo,
  ação impossível (sem ferramenta), entrada estranha
- MULTI-TURNO (resolução de referência)

Segurança: cenários que ALTERAM dados ("mutating": True) só rodam com --mutating.
Por padrão o benchtest é seguro e repetível (leituras + gate negado). Exceção
controlada: reactivation_campaign cria um cupom (write de risco médio, sem gate)
— o teardown desativa o que foi criado, então nada vaza entre rodadas.

Uso:
  python benchtest.py                 # seguro: leituras + edge + gate-negado
  python benchtest.py --mutating      # inclui os que confirmam ação destrutiva
  python benchtest.py --only spend    # subconjunto por id
  python benchtest.py --md report.md
"""

import argparse
import sys
import time

from clients.telemetry import TELEMETRY, fmt_cost
from memory import ConversationMemory
from pipeline import run as run_pipeline, resume as resume_pipeline
from state import SessionState

# Nunca deve aparecer numa resposta ao operador.
_LEAK_PATTERNS = [
    "traceback", "validation error", "pydantic", "errors.pydantic", "context keys",
    "$step", "nonetype", "keyerror", "indexerror", "apistatuserror", "}\n  params",
]

# Frases que indicariam que uma ação destrutiva foi (falsamente) executada.
_FALSE_SUCCESS = ["remarcada com sucesso", "reagendada com sucesso", "remarquei", "reagendei",
                  "cancelada com sucesso", "cancelei a reserva", "desativado com sucesso"]


# ---------------------------------------------------------------------------
# Oráculos e setups — falam direto com a API (verdade-base / ids reais)
# ---------------------------------------------------------------------------

def _api():
    from clients.dionisio import DionisioClient
    return DionisioClient()


def _tool(name, params):
    from tools.registry import get_tool
    return _api().execute_tool(get_tool(name), params)


def oracle_spend_no_coupon():
    data = _tool("clients_top_spenders", {"period": "month", "minSpent": 500, "limit": 500})
    return str(sum(1 for it in data.get("items", []) if it.get("couponsUsed") == 0))


def oracle_inactive_60():
    data = _tool("clients_inactive", {"days": 60})
    return str(data.get("total", len(data.get("items", []))))


def oracle_active_coupons():
    data = _tool("coupons_list", {"status": "active"})
    items = data.get("items", data if isinstance(data, list) else [])
    n = sum(1 for c in items if c.get("status") == "active") or len(items)
    return str(n)


def oracle_revenue_orders():
    import datetime as _dt
    now = _dt.datetime.now()
    start = int(_dt.datetime(now.year, now.month, 1).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    data = _tool("analytics_revenue", {"periodStart": start, "periodEnd": end})
    return str(data.get("orderCount", ""))


def oracle_top_item():
    import datetime as _dt
    now = _dt.datetime.now()
    start = int((now - _dt.timedelta(days=30)).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    data = _tool("analytics_top_items", {"periodStart": start, "periodEnd": end, "limit": 10})
    items = data.get("items", [])
    return items[0]["name"] if items else ""


def setup_active_reservation():
    """Pega o id de uma reserva ATIVA real, para exercitar o gate destrutivo."""
    data = _tool("reservations_list", {})
    items = data.get("items", [])
    active = next((i for i in items
                   if i.get("status") in ("pending", "confirmed", "seated", "awaiting_payment")), None)
    chosen = active or (items[0] if items else None)
    return {"reservation_id": chosen["id"] if chosen else "res_inexistente"}


def _coupon_ids():
    data = _tool("coupons_list", {})
    items = data.get("items", data if isinstance(data, list) else [])
    return {c["id"] for c in items if isinstance(c, dict) and "id" in c}


def setup_coupon_snapshot():
    """Snapshot dos cupons existentes — o teardown desativa só o que o cenário criar."""
    return {"_coupons_before": _coupon_ids()}


def teardown_deactivate_new_coupons(subs):
    """Desativa cupons criados pelo cenário, mantendo a bateria repetível
    (sem isso, cada rodada deixaria um cupom de reativação novo para trás)."""
    before = subs.get("_coupons_before", set())
    for cid in _coupon_ids() - before:
        _tool("coupons_deactivate", {"id": cid})


# ---------------------------------------------------------------------------
# Cenários
# ---------------------------------------------------------------------------

SCENARIOS = [
    # ---- LEITURA / agregação (com oráculo onde dá) ----
    {"id": "avail_evening", "category": "leitura/agregação", "needs_plan": True, "must_contain": ["lugar"],
     "query": "Quantas reservas temos pra hoje à noite e quantos lugares ainda sobram?"},
    {"id": "spend_no_coupon", "category": "filtro multi-condição + lista", "needs_plan": True,
     "query": "Lista os clientes que gastaram mais de R$500 no último mês e nunca usaram cupom.",
     "oracle": oracle_spend_no_coupon},
    {"id": "vip_high_spend", "category": "filtro por grupo + gasto", "needs_plan": True,
     "query": "Quais clientes do grupo VIP gastaram mais de R$800 este mês?"},
    {"id": "inactive_list", "category": "leitura simples", "needs_plan": True,
     "query": "Quais clientes estão inativos há 60 dias?", "oracle": oracle_inactive_60},
    {"id": "revenue_month", "category": "analytics", "needs_plan": True,
     "query": "Quanto faturamos neste mês e quantos pedidos?", "oracle": oracle_revenue_orders},
    {"id": "top_items", "category": "analytics", "needs_plan": True,
     "query": "Quais foram os itens mais vendidos recentemente?", "oracle": oracle_top_item},
    {"id": "noshow_rate", "category": "analytics", "needs_plan": True,
     "query": "Qual a taxa de no-show das reservas neste mês?"},
    {"id": "conversations", "category": "analytics", "needs_plan": True,
     "query": "Quantas conversas a IA resolveu sozinha este mês?"},
    {"id": "coupon_returns", "category": "analytics", "needs_plan": True,
     "query": "Qual foi o retorno dos cupons este mês (gerados x usados)?"},
    {"id": "active_coupons", "category": "leitura cupons", "needs_plan": True,
     "query": "Quais cupons estão ativos no momento?", "oracle": oracle_active_coupons},
    {"id": "delivery_min", "category": "leitura config", "needs_plan": True,
     "query": "Qual é o valor mínimo de pedido no delivery?"},
    {"id": "delivery_hoods", "category": "leitura config", "needs_plan": True,
     "query": "Quais bairros atendemos no delivery e quais as taxas?"},
    {"id": "store_hours", "category": "leitura loja", "needs_plan": True,
     "query": "Que horas a loja abre amanhã?"},
    {"id": "promotions_list", "category": "leitura promoções", "needs_plan": True,
     "query": "Quais promoções estão ativas agora?"},
    {"id": "client_insights", "category": "leitura cliente", "needs_plan": True,
     "query": "Me dá um resumo (insights) do cliente João."},
    {"id": "reservations_on_date", "category": "leitura reservas", "needs_plan": True,
     "query": "Quais reservas temos para amanhã?"},

    # ---- ENTIDADE por nome / multi-turno ----
    {"id": "reservation_by_name", "category": "resolução por nome",
     "query": "Qual dia está a reserva do João?",
     "status_in": ["completed", "halted", "needs_info"]},
    {"id": "multiturn_phone", "category": "multi-turno (referência)",
     "turns": ["Busca o cliente João.", "Qual o telefone dele?"],
     "status_in": ["completed", "halted", "needs_info"]},

    # ---- BORDERLINE / edge ----
    {"id": "cancel_ambiguous", "category": "ambíguo (falta id)",
     "query": "Cancela a reserva.",
     "status_in": ["needs_info", "needs_confirmation", "halted", "completed", "no_tools"],
     "must_not_contain": _FALSE_SUCCESS},
    {"id": "menu_remove_notify", "category": "fulfillment parcial / unsupported",
     "query": "O prato 'Risoto de Funghi' saiu do cardápio — remove ele e avisa quem pediu nos últimos 7 dias.",
     "status_in": ["completed", "no_tools", "partial"], "must_contain": ["cardápio"]},
    {"id": "impossible_sms", "category": "ação sem ferramenta",
     "query": "Manda um SMS para todos os clientes avisando que abrimos no feriado.",
     "status_in": ["completed", "no_tools", "partial"]},
    {"id": "fanout_cap", "category": "cap de fanout",
     "query": "Me lista os pedidos de cada dia do último ano, dia a dia.",
     "status_in": ["completed", "no_tools", "failed", "partial"]},
    {"id": "weather_out_of_scope", "category": "fora de escopo",
     "query": "Qual a previsão do tempo para amanhã?",
     "status_in": ["completed", "no_tools"], "needs_plan": False,
     "must_not_contain": ["graus", "°c", "chuva forte prevista"]},
    {"id": "gibberish", "category": "entrada estranha",
     "query": "asdkjh ;; reserva?? cupom mesa 9999 ???",
     "status_in": ["completed", "no_tools", "needs_info", "halted", "failed"]},
    {"id": "thanks", "category": "conversa",
     "query": "Show, obrigado pela ajuda!", "status_in": ["completed", "no_tools"], "needs_plan": False},
    {"id": "knowledge_noshow", "category": "conhecimento (doc)",
     "query": "Como funciona o no-show de reservas no Dionísio? Quero entender a regra.",
     "status_in": ["completed", "no_tools"], "needs_plan": False,
     "must_not_contain": ["não tenho acesso", "não posso ajudar com isso"]},
    {"id": "knowledge_feature", "category": "conhecimento (doc)",
     "query": "O que é a fila de espera e como ela funciona?",
     "status_in": ["completed", "no_tools"], "needs_plan": False},
    {"id": "capabilities", "category": "meta",
     "query": "O que você consegue fazer por mim?", "status_in": ["completed", "no_tools"], "needs_plan": False},

    # ---- CRIAÇÃO + unsupported ----
    # Cria um cupom de verdade (coupons_create é write de risco médio, sem gate);
    # o teardown desativa o que foi criado para a bateria continuar repetível.
    {"id": "reactivation_campaign", "category": "criação + unsupported",
     "setup": setup_coupon_snapshot, "teardown": teardown_deactivate_new_coupons,
     "query": "Cria uma campanha de reativação para clientes inativos há 60 dias, com um cupom de 15%.",
     "status_in": ["completed", "needs_confirmation", "partial"]},

    # ---- DESTRUTIVO: gate + negação (SEGURO, não muta) ----
    {"id": "reschedule_gate", "category": "pré-condição + destrutivo",
     "query": "Remarca a reserva do João de quinta pra sábado no mesmo horário e confirma se tem mesa.",
     "status_in": ["needs_confirmation", "halted", "safe_alternative", "completed"],
     "must_not_contain": _FALSE_SUCCESS},
    {"id": "cancel_deny", "category": "destrutivo + negação", "setup": setup_active_reservation,
     "query": "Cancela a reserva de id {reservation_id}.",
     "confirm": False,
     "status_in": ["completed", "halted", "needs_info", "no_tools"],
     "must_not_contain": _FALSE_SUCCESS},

    # ---- DESTRUTIVO: confirmação real (MUTA — só com --mutating) ----
    {"id": "cancel_confirm", "category": "destrutivo + confirmo", "mutating": True,
     "setup": setup_active_reservation,
     "query": "Cancela a reserva de id {reservation_id}.",
     "confirm": True,
     "status_in": ["completed", "blocked", "failed", "partial"]},
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _plan_steps(state):
    return len((state.plan or {}).get("steps", [])) if state and state.plan else 0


def _leak(response):
    low = (response or "").lower()
    return [p for p in _LEAK_PATTERNS if p in low]


def _confirm_params(res):
    ps = res.pending_step
    return ps.result.get("params") if ps and isinstance(ps.result, dict) else None


def _run_scenario(sc):
    memory = ConversationMemory()
    t0 = time.time()
    tmark = TELEMETRY.snapshot()  # delta de LLM calls/tokens/custo deste cenário

    # setup (ids reais via API)
    subs = {}
    if sc.get("setup"):
        try:
            subs = sc["setup"]() or {}
        except Exception as e:
            return _exc(sc, f"setup falhou: {type(e).__name__}: {e}", time.time() - t0, tmark)

    # teardown roda SEMPRE (mesmo com exceção) — limpa efeitos colaterais do
    # cenário para manter a bateria repetível.
    try:
        return _run_scenario_turns(sc, subs, memory, t0, tmark)
    finally:
        if sc.get("teardown"):
            try:
                sc["teardown"](subs)
            except Exception as e:
                print(f"    (teardown de {sc['id']} falhou: {type(e).__name__}: {e})")


def _run_scenario_turns(sc, subs, memory, t0, tmark):
    turns = sc.get("turns") or [sc["query"]]
    try:
        turns = [t.format(**subs) for t in turns]
    except Exception as e:
        return _exc(sc, f"template falhou: {e}", time.time() - t0, tmark)

    res, state = None, None
    try:
        for q in turns:
            state = SessionState()
            res = run_pipeline(q, state=state, memory=memory)
            # fluxo de confirmação (confirmo / nega)
            if res.status == "needs_confirmation" and sc.get("confirm") is not None:
                res = resume_pipeline(
                    state=res.state, pending_plan=res.pending_plan,
                    confirmed_step_id=res.pending_step_id,
                    confirmed_params=_confirm_params(res),
                    operator_confirmed=bool(sc["confirm"]),
                )
            memory.add_turn(q, res.response)
    except Exception as e:
        return _exc(sc, f"{type(e).__name__}: {e}", time.time() - t0, tmark)

    return _evaluate(sc, res, _plan_steps(res.state), time.time() - t0, tmark)


def _exc(sc, msg, secs, tmark=None):
    return {"id": sc["id"], "category": sc["category"], "status": "EXCEPTION",
            "response": msg, "steps": 0, "secs": secs,
            "telemetry": TELEMETRY.since(tmark) if tmark is not None else {},
            "checks": [("sem exceção", False, msg)]}


def _evaluate(sc, res, steps, secs, tmark=None):
    response = res.response or ""
    checks = []

    allowed = sc.get("status_in", ["completed", "no_tools", "needs_confirmation",
                                    "needs_info", "safe_alternative", "halted", "partial"])
    checks.append(("status sano", res.status in allowed, f"{res.status} (esperado {allowed})"))

    leaks = _leak(response)
    checks.append(("sem vazamento", not leaks, "ok" if not leaks else f"vazou: {leaks}"))

    if sc.get("needs_plan"):
        checks.append(("plano com passos", steps > 0, f"{steps} passos"))

    read_like = any(k in sc["category"] for k in ("leitura", "analytics", "filtro"))
    if read_like and "status_in" not in sc:
        checks.append(("leitura → completed", res.status == "completed", res.status))

    for sub in sc.get("must_contain", []):
        checks.append((f"contém '{sub}'", sub.lower() in response.lower(), ""))
    for sub in sc.get("must_not_contain", []):
        checks.append((f"NÃO contém '{sub}'", sub.lower() not in response.lower(), ""))

    if "oracle" in sc:
        try:
            expected = sc["oracle"]()
            hit = bool(expected) and expected in response
            checks.append((f"oráculo == {expected}", hit, f"esperava conter '{expected}'"))
        except Exception as e:
            checks.append(("oráculo", False, f"falha ao computar: {e}"))

    return {"id": sc["id"], "category": sc["category"], "status": res.status,
            "response": response, "steps": steps, "checks": checks, "secs": secs,
            "telemetry": TELEMETRY.since(tmark) if tmark is not None else {}}


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def _telemetry_summary(results):
    """Unit-economics section: latency, LLM calls, tokens and cost per request."""
    tels = [r.get("telemetry") or {} for r in results]
    n = len(results) or 1
    total_calls = sum(t.get("llm_calls", 0) for t in tels)
    total_in = sum(t.get("prompt_tokens", 0) for t in tels)
    total_out = sum(t.get("completion_tokens", 0) for t in tels)
    total_cached = sum(t.get("cached_tokens", 0) for t in tels)
    costs = [t["cost_usd"] for t in tels if t.get("cost_usd") is not None]
    total_cost = sum(costs) if costs else None
    avg_secs = sum(r["secs"] for r in results) / n
    p_max = max(results, key=lambda r: r["secs"]) if results else None

    lines = ["## Telemetria — custo & latência por pedido", "",
             "| Métrica | Valor |", "|---|---|",
             f"| Latência média | {avg_secs:.1f}s |"]
    if p_max:
        lines.append(f"| Latência máxima | {p_max['secs']:.1f}s ({p_max['id']}) |")
    cache_rate = (total_cached / total_in * 100) if total_in else 0
    lines += [
        f"| Chamadas LLM (média por pedido) | {total_calls / n:.1f} |",
        f"| Tokens (entrada / saída, total) | {total_in:,} / {total_out:,} |",
        f"| Tokens de entrada servidos do cache | {total_cached:,} ({cache_rate:.0f}%) |",
        f"| Custo total da bateria | {fmt_cost(total_cost)} |",
        f"| **Custo médio por pedido** | **{fmt_cost(total_cost / n if total_cost is not None else None)}** |",
        "",
    ]
    return lines


def _render_md(results):
    total = sum(len(r["checks"]) for r in results)
    passed = sum(1 for r in results for _, ok, _ in r["checks"] if ok)
    lines = ["# Benchtest final — Dionísio Agent", "",
             f"Cenários: {len(results)} | Checagens: {passed}/{total} ok", ""]
    lines += _telemetry_summary(results)
    lines += ["| Cenário | Categoria | Status | Passos | Checagens | Tempo | LLM | Custo |",
              "|---|---|---|---|---|---|---|---|"]
    for r in results:
        ok = sum(1 for _, k, _ in r["checks"] if k)
        mark = "✅" if ok == len(r["checks"]) else "⚠️"
        t = r.get("telemetry") or {}
        lines.append(
            f"| {r['id']} | {r['category']} | {r['status']} | {r['steps']} "
            f"| {mark} {ok}/{len(r['checks'])} | {r['secs']:.1f}s "
            f"| {t.get('llm_calls', '—')} | {fmt_cost(t.get('cost_usd'))} |")
    lines.append("\n---\n")
    for r in results:
        lines.append(f"## {r['id']}  ({r['category']})  —  {r['status']}  ·  {r['secs']:.1f}s")
        for name, ok, detail in r["checks"]:
            lines.append(f"- {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
        snippet = (r["response"] or "").strip().replace("\n", " ")
        lines.append(f"\n> {snippet[:400]}{'…' if len(snippet) > 400 else ''}\n")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="benchtest_report.md")
    ap.add_argument("--only", help="roda só cenários cujo id contém esta substring")
    ap.add_argument("--mutating", action="store_true", help="inclui cenários que ALTERAM dados")
    args = ap.parse_args()

    scenarios = SCENARIOS
    if not args.mutating:
        scenarios = [s for s in scenarios if not s.get("mutating")]
    if args.only:
        scenarios = [s for s in scenarios if args.only in s["id"]]

    if not args.mutating:
        print("(modo seguro — cenários que alteram dados omitidos; use --mutating para incluí-los)\n")

    results = []
    for i, sc in enumerate(scenarios, 1):
        print(f"[{i}/{len(scenarios)}] {sc['id']} …", flush=True)
        r = _run_scenario(sc)
        ok = sum(1 for _, k, _ in r["checks"] if k)
        print(f"    {r['status']:18} {ok}/{len(r['checks'])} checks  ({r['secs']:.1f}s)")
        results.append(r)

    with open(args.md, "w", encoding="utf-8") as f:
        f.write(_render_md(results))

    passed = sum(1 for r in results for _, ok, _ in r["checks"] if ok)
    total = sum(len(r["checks"]) for r in results)
    print(f"\n{passed}/{total} checagens ok — relatório: {args.md}")
    # Exit != 0 se houve leak ou exceção (útil pra CI).
    hard = any(r["status"] == "EXCEPTION" for r in results) or \
        any(not ok for r in results for name, ok, _ in r["checks"] if name == "sem vazamento")
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
