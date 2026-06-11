"""
Two-layer conversation memory.

- short_term: last N turns verbatim (immediate context for the LLM)
- long_term_summary: rolling compressed summary of older turns

When short_term fills up, the oldest turns are summarized and merged
into long_term_summary via a cheap LLM call. The summary stays bounded
at ~200 tokens regardless of session length.
"""

from dataclasses import dataclass, field
from clients.llm import LLMClient
from config import LLM_MODEL_FAST

# Summary compression is a trivial task — use the fast model.
_llm = LLMClient(model=LLM_MODEL_FAST)

_SUMMARY_SYSTEM = """Você mantém um resumo conciso e contínuo de uma conversa entre um operador de restaurante
e um assistente de IA. Você receberá o resumo atual e novos turnos para incorporar.
Atualize o resumo para incluir as novas informações. Seja conciso — máximo 200 tokens.
Foque em: o que foi solicitado, o que foi encontrado/feito, quaisquer pendências ou esclarecimentos.
Responda APENAS com o resumo atualizado em português brasileiro, sem preâmbulo."""

MAX_SHORT_TERM_TURNS = 8  # number of individual messages (4 exchanges)


@dataclass
class ConversationMemory:
    short_term: list[dict] = field(default_factory=list)
    long_term_summary: str = ""

    def add_turn(self, user_msg: str, agent_msg: str) -> None:
        """Add a user+agent exchange and compress if short_term is full."""
        self.short_term.append({"role": "user", "content": user_msg})
        self.short_term.append({"role": "assistant", "content": agent_msg})

        if len(self.short_term) > MAX_SHORT_TERM_TURNS:
            self._compress()

    def _compress(self) -> None:
        """Move the oldest 2 messages (1 exchange) into the long-term summary."""
        to_compress = self.short_term[:2]
        self.short_term = self.short_term[2:]

        turns_text = "\n".join(
            f"{'Operator' if m['role'] == 'user' else 'Agent'}: {m['content']}"
            for m in to_compress
        )

        messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Current summary:\n{self.long_term_summary or '(none yet)'}\n\n"
                    f"New turns to incorporate:\n{turns_text}"
                ),
            },
        ]
        self.long_term_summary = _llm.text(messages, temperature=0.0)

    def get_context(self) -> list[dict]:
        """
        Returns a list of messages to prepend to the planner context:
        [summary_as_system_note] + short_term_turns
        """
        messages = []
        if self.long_term_summary:
            messages.append({
                "role": "user",
                "content": f"[Conversation summary so far: {self.long_term_summary}]",
            })
            messages.append({
                "role": "assistant",
                "content": "Understood, I have context from the earlier conversation.",
            })
        messages.extend(self.short_term)
        return messages

    def is_empty(self) -> bool:
        return not self.short_term and not self.long_term_summary
