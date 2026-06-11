from clients.llm import LLMClient
from config import LLM_MODEL_FAST

_SYSTEM = """Você é um reescritor de consultas para um assistente de CRM de restaurante (Dionísio).

Sua tarefa: reescrever APENAS a mensagem atual do operador em termos claros de vocabulário do domínio, em português.
Use o histórico da conversa APENAS para resolver referências ambíguas (ex: "a reserva dele" → "reserva do cliente João Silva", "faça tudo" → "atribuir cupom de reativação para clientes inativos").
NÃO inclua nem repita pedidos anteriores na saída — reescreva apenas o que o operador está pedindo AGORA.

Foque em entidades: cliente, reserva, pedido, cupom, promoção, delivery, ifood, loja, analytics
e ações: buscar, listar, criar, atualizar, cancelar, remarcar, confirmar, pausar, gerar, atribuir.

Saída: apenas a consulta reescrita em português — sem explicação, sem preâmbulo, sem blocos de código."""

# Rewriting is a trivial task — the fast model halves perceived latency.
_llm = LLMClient(model=LLM_MODEL_FAST)


def rewrite_query(query: str, history: list[dict] = None) -> str:
    """Rewrite the current operator query into domain vocabulary (Portuguese).
    History is used only to resolve references, not to merge previous requests.
    """
    messages = [{"role": "system", "content": _SYSTEM}]
    if history:
        messages.extend(history[-6:])  # last 3 exchanges for reference resolution only
    messages.append({"role": "user", "content": f"Reescreva apenas esta mensagem: {query}"})

    rewritten = _llm.text(messages)
    return rewritten.strip() or query
