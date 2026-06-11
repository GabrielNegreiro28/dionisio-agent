"""
In-process LLM usage telemetry — tokens, latency and cost per call.

Every LLMClient call records here. Consumers take a snapshot() mark before a
unit of work and read since(mark) after it, getting the delta: number of LLM
calls, tokens, time spent inside the LLM, and cost in USD (when OpenRouter
returns it). Thread-safe; fanout sub-calls from worker threads land correctly.
"""

import threading
from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMCall:
    model: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Optional[float]  # None when the provider didn't report it
    cached_tokens: int = 0     # prompt tokens served from the provider's cache


class Telemetry:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls: list[LLMCall] = []

    def record(
        self,
        model: str,
        latency_s: float,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: Optional[float] = None,
        cached_tokens: int = 0,
    ) -> None:
        with self._lock:
            self._calls.append(LLMCall(model, latency_s, prompt_tokens,
                                        completion_tokens, cost_usd, cached_tokens))

    def snapshot(self) -> int:
        """Mark the current position; pass to since() to get a delta."""
        with self._lock:
            return len(self._calls)

    def since(self, mark: int) -> dict:
        """Aggregate of all calls recorded after `mark`."""
        with self._lock:
            calls = self._calls[mark:]
        costs = [c.cost_usd for c in calls if c.cost_usd is not None]
        return {
            "llm_calls": len(calls),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "cached_tokens": sum(c.cached_tokens for c in calls),
            "llm_time_s": sum(c.latency_s for c in calls),
            # None (not 0) when the provider reported no cost — don't fake free.
            "cost_usd": sum(costs) if costs else None,
        }

    def total(self) -> dict:
        return self.since(0)


TELEMETRY = Telemetry()


def fmt_cost(cost_usd: Optional[float]) -> str:
    if cost_usd is None:
        return "n/d"
    return f"${cost_usd:.4f}"
