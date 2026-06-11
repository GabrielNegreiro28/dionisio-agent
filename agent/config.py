from dotenv import load_dotenv
import os

load_dotenv()


def _require(name: str) -> str:
    """Load a required env var, raising a clear error if absent."""
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            f"Add it to your .env file."
        )
    return val


DIONISIO_API_KEY: str = _require("DIONISIO_API_KEY")
DIONISIO_BASE_URL: str = "https://dionisio-crm.web.app"

# LLM via OpenRouter (API compatível com OpenAI).
LLM_API_KEY: str = _require("OPENROUTER_API_KEY")
LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
LLM_MODEL: str = os.getenv("LLM_MODEL", "anthropic/claude-fable-5")

# Modelo barato/rápido para tarefas triviais (rewriter, classifier fallback,
# compressão de memória). Pagar latência e custo do modelo principal nessas
# chamadas não melhora qualidade — só piora o tempo de resposta ao operador.
LLM_MODEL_FAST: str = os.getenv("LLM_MODEL_FAST", "anthropic/claude-haiku-4.5")

# Max API retries per step on transient errors (1 retry = 2 total attempts)
MAX_REFLECTIONS: int = 2
MAX_REPLANS: int = 1

# Max iterations a single fanout node may run (each is a sequential API call) —
# guards against a plan like "pedidos de cada dia do último ano" (365 calls).
# ~1 month of days; longer ranges should use an analytics_* tool (one call).
MAX_FANOUT_SIZE: int = 31
