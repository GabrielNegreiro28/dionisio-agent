from tools.definitions import ALL_TOOLS, ToolDefinition

# Organized by domain
DOMAIN_TOOLS: dict[str, list[ToolDefinition]] = {}
for tool in ALL_TOOLS:
    DOMAIN_TOOLS.setdefault(tool.domain, []).append(tool)

ALL_DOMAINS: list[str] = list(DOMAIN_TOOLS.keys())

# Fast lookup by name
_TOOL_BY_NAME: dict[str, ToolDefinition] = {t.name: t for t in ALL_TOOLS}


def get_tool(name: str) -> ToolDefinition:
    tool = _TOOL_BY_NAME.get(name)
    if not tool:
        raise KeyError(f"Tool '{name}' not found in registry.")
    return tool


def get_all_tools() -> list[ToolDefinition]:
    """The full catalog — used as the planner's fallback so no classifier miss
    can starve it of a tool that actually exists."""
    return list(ALL_TOOLS)


def get_tools_for_domains(domains: list[str]) -> list[ToolDefinition]:
    tools = []
    for domain in domains:
        tools.extend(DOMAIN_TOOLS.get(domain, []))
    return tools


def get_openai_tools_for_domains(domains: list[str]) -> list[dict]:
    return [t.to_openai_tool() for t in get_tools_for_domains(domains)]


def get_all_openai_tools() -> list[dict]:
    return [t.to_openai_tool() for t in ALL_TOOLS]
