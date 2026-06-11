import json
import re
from clients.llm import LLMClient
from config import LLM_MODEL_FAST

# ---------------------------------------------------------------------------
# Rules-based classifier
# ---------------------------------------------------------------------------

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "clients": [
        "cliente", "clientes", "pessoa", "contato", "nome", "telefone", "cpf",
        "email", "grupo", "aniversário", "inativo", "inativos", "top spender",
        "quem mais gasta", "reativação", "cadastro", "perfil", "quem é",
        "banco de dados", "base de clientes",
    ],
    "reservations": [
        "reserva", "reservas", "remarcar", "remarca", "cancelar reserva",
        "confirmar reserva", "disponibilidade", "mesa", "horário", "área",
        "salão", "varanda", "mezanino", "no-show", "no show", "agendar",
        "lugar", "lugares", "capacidade",
    ],
    "orders": [
        "pedido", "pedidos", "cancelar pedido", "status do pedido",
        "compra", "ticket", "venda", "itens do pedido", "prato", "item",
        "cardápio", "produto", "quem pediu", "gastou", "gastaram",
    ],
    "coupons": [
        "cupom", "cupons", "desconto", "código", "instância", "gerar cupom",
        "atribuir cupom", "desativar cupom", "campanha", "reativação",
        "benefício", "distribuir cupom", "cupom de",
    ],
    "promotions": [
        "promoção", "promoções", "promocao", "oferta", "desconto fixo",
        "remover promoção", "criar promoção", "percentual", "valor fixo",
    ],
    "delivery": [
        "delivery", "entrega", "pausar delivery", "pausa delivery",
        "bairro", "taxa de entrega", "mínimo de entrega", "retomar delivery",
        "área de entrega", "frete",
    ],
    "ifood": [
        "ifood", "iFood", "i food", "pedido ifood",
    ],
    "store": [
        "loja", "horário de funcionamento", "equipe", "membro", "membros",
        "configuração da loja", "dados da loja", "features", "integrações",
        "funcionamento", "abre", "fecha",
    ],
    "analytics": [
        "receita", "faturamento", "relatório", "analytics", "estatísticas",
        "métricas", "top items", "vendas", "mais vendido", "conversas",
        "no-show rate", "no-show", "no show", "retorno de cupom", "performance",
        "resultado", "quanto", "total", "média", "media",
        "taxa", "percentual", "ticket médio", "ticket medio", "rate",
    ],
}

# Domain classification fallback is a trivial task — use the fast model.
_llm = LLMClient(model=LLM_MODEL_FAST)


def _score_domains(text: str) -> dict[str, int]:
    """Returns raw hit counts per domain."""
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in text_lower)
        if hits > 0:
            scores[domain] = hits
    return scores


def _llm_classify(query: str) -> list[str]:
    """Fallback: ask the LLM which domains are relevant."""
    domain_list = ", ".join(DOMAIN_KEYWORDS.keys())
    messages = [
        {
            "role": "system",
            "content": (
                f"Classify restaurant CRM queries into domains: {domain_list}.\n"
                "Return a JSON array of matching domain names. Example: [\"clients\", \"reservations\"]\n"
                "Return [] if no domain matches or if the message is purely conversational.\n"
                "Return only the JSON array, no explanation."
            ),
        },
        {"role": "user", "content": query},
    ]
    raw = _llm.text(messages)
    # Strip markdown fences if present
    raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [d for d in result if d in DOMAIN_KEYWORDS]
    except Exception:
        pass
    return []


def _classify_raw(query: str, rewritten: str = "") -> list[str]:
    """
    Returns a list of relevant domain names for the query.

    Strategy:
    - Strong signal (max_hits >= 2): rules-based, threshold = max(1, max_hits * 0.5)
    - Weak signal  (max_hits == 1): rules give a candidate list; LLM validates/supplements.
      Uses intersection to avoid false positives (falls back to rules if LLM returns empty).
    - No signal   (max_hits == 0): pure LLM fallback.
    """
    combined = f"{query} {rewritten}".strip()
    scores = _score_domains(combined)

    if not scores:
        # No keyword hits at all — full LLM fallback
        return _llm_classify(combined)

    max_hits = max(scores.values())

    if max_hits >= 2:
        # Strong signal: trust the rules
        threshold = max(1, max_hits * 0.5)
        return [d for d, s in scores.items() if s >= threshold]

    # Weak signal (max_hits == 1): every matching domain has exactly 1 hit.
    # Supplement with LLM to avoid expanding on irrelevant domains.
    rules_candidates = [d for d, s in scores.items() if s >= 1]
    llm_candidates = _llm_classify(combined)

    if llm_candidates:
        # Prefer the intersection (domains confirmed by both)
        intersection = [d for d in rules_candidates if d in llm_candidates]
        # If intersection is empty, trust LLM (it may have found something rules missed)
        return intersection or llm_candidates

    # LLM returned nothing — fall back to rules
    return rules_candidates


# Companion domains: included automatically when a trigger domain is present,
# because they're commonly needed together but rarely co-mentioned in the query.
_COMPANIONS: dict[str, list[str]] = {
    # Reservations carry only clientId (no client name), so resolving
    # "a reserva do Fulano" needs clients_search / clients_reservations.
    "reservations": ["clients"],
}


def _with_companions(domains: list[str]) -> list[str]:
    out = list(domains)
    for trigger, companions in _COMPANIONS.items():
        if trigger in out:
            for c in companions:
                if c not in out:
                    out.append(c)
    return out


def classify_domains(query: str, rewritten: str = "") -> list[str]:
    """Classify into domains and pull in companion domains (e.g. clients with
    reservations) so name-resolution tools are available when needed."""
    return _with_companions(_classify_raw(query, rewritten))
