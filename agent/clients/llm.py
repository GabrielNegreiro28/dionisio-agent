import time

from openai import OpenAI
from openai.types.chat import ChatCompletion
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from clients.telemetry import TELEMETRY

# Cap on OUTPUT tokens per call. Without this, OpenRouter assumes the model's
# full max (e.g. 64k for Opus) and reserves the whole cost up front — which
# trips a 402 "insufficient credits" even though a plan/response needs far less.
# Plans (JSON) and replies are small; 8k is generous and keeps cost predictable.
DEFAULT_MAX_TOKENS = 8000


class LLMClient:
    def __init__(self, model: str = None):
        """model: optional override (e.g. LLM_MODEL_FAST for trivial tasks)."""
        self.client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
        )
        self.model = model or LLM_MODEL

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] = None,
        temperature: float = 0.0,
        response_format: dict = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ChatCompletion:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            # OpenRouter: include per-call cost (USD) in the usage object.
            "extra_body": {"usage": {"include": True}},
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format

        t0 = time.perf_counter()
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            # Reasoning models (e.g. Claude Fable/Mythos, OpenAI o-series) reject
            # 'temperature'. Retry once without it so the client stays portable.
            if "temperature" in kwargs and "temperature" in str(e).lower():
                kwargs.pop("temperature")
                response = self.client.chat.completions.create(**kwargs)
            else:
                raise
        self._record(response, time.perf_counter() - t0)
        return response

    def _record(self, response: ChatCompletion, latency_s: float) -> None:
        usage = getattr(response, "usage", None)
        cost = None
        cached = 0
        if usage is not None:
            extra = getattr(usage, "model_extra", None) or {}
            cost = extra.get("cost")
            # Prompt-cache hits (OpenRouter/OpenAI shape): usage.prompt_tokens_details.cached_tokens
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) or 0
        TELEMETRY.record(
            model=self.model,
            latency_s=latency_s,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cost_usd=cost,
            cached_tokens=cached,
        )

    def text(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Convenience: returns just the text content of the first choice."""
        response = self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        return response.choices[0].message.content or ""
