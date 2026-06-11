from tools.registry import get_tools_for_domains, get_openai_tools_for_domains
from tools.definitions import ToolDefinition


class NoRelevantToolsError(Exception):
    """Raised when no domain matches the query."""
    pass


def select_tools(domains: list[str]) -> list[ToolDefinition]:
    """
    Returns tool definitions for the given domains.
    Raises NoRelevantToolsError if domains is empty.
    """
    if not domains:
        raise NoRelevantToolsError(
            "No relevant tools found for this request. "
            "I can help with: clients, reservations, orders, coupons, promotions, "
            "delivery, iFood, store settings, and analytics."
        )
    return get_tools_for_domains(domains)


def select_openai_tools(domains: list[str]) -> list[dict]:
    """Returns OpenAI-format tool definitions for the given domains."""
    if not domains:
        raise NoRelevantToolsError(
            "No relevant tools found for this request."
        )
    return get_openai_tools_for_domains(domains)
