"""
Dionísio Agent — CLI entry point.

Usage:
    python main.py           # normal mode
    python main.py --debug   # shows retrieval + plan details
"""

import os
import sys
from clients.telemetry import TELEMETRY, fmt_cost
from memory import ConversationMemory
from pipeline import run, resume, PipelineResult
from planning.schemas import Plan
from state import SessionState

DEBUG = "--debug" in sys.argv
if DEBUG:
    os.environ["AGENT_FLAGS"] = "--debug"


def _print_agent(message: str) -> None:
    print(f"\n🤖  {message}\n")


def _print_separator() -> None:
    print("─" * 60)


def _print_progress(message: str) -> None:
    print(f"   ⏳ {message}", flush=True)


def _print_telemetry(mark: int) -> None:
    t = TELEMETRY.since(mark)
    print(
        f"\033[90m[telemetria] {t['llm_calls']} chamadas LLM · "
        f"{t['prompt_tokens']}+{t['completion_tokens']} tokens "
        f"({t['cached_tokens']} em cache) · "
        f"{t['llm_time_s']:.1f}s em LLM · custo {fmt_cost(t['cost_usd'])}\033[0m"
    )


def _print_debug(state: SessionState) -> None:
    if not DEBUG or state is None:
        return
    print(f"\n\033[90m[debug] domains   : {state.domains}")
    print(f"[debug] tools     : {len(state.selected_tools)} → {state.selected_tools}")
    print(f"[debug] rewritten : {state.rewritten_query}")
    if state.plan:
        steps = state.plan.get("steps", [])
        for s in steps:
            print(f"[debug] step {s['id']}    : {_describe_step(s)}")
        unsupported = state.plan.get("unsupported") or []
        if unsupported:
            print(f"[debug] unsupported: {unsupported}")
    print("\033[0m", end="")


def _describe_step(s: dict) -> str:
    kind = s.get("kind") or "api"
    if kind == "compute":
        return f"[compute] {s.get('operation')} → {s.get('output_key')} {s.get('params', {})}"
    if kind == "verify":
        return f"[verify] check {s.get('check')} on {s.get('params', {})}"
    if kind == "fanout":
        over = s.get("over")
        n = len(over) if isinstance(over, list) else over
        return f"[fanout] over {n}x → {s.get('output_key')}"
    return f"[api] {s.get('tool')} {s.get('params', {})}"


def main() -> None:
    mode = " [DEBUG]" if DEBUG else ""
    w = 42  # largura interna do box
    print("\n╔" + "═" * w + "╗")
    print("║   " + f"Dionísio — Agente Assistente{mode}".ljust(w - 3) + "║")
    print("║   " + "Digite 'sair' para encerrar".ljust(w - 3) + "║")
    print("╚" + "═" * w + "╝\n")

    memory = ConversationMemory()
    pending_plan: Plan | None = None
    pending_step_id: int | None = None
    pending_confirmed_params: dict | None = None
    awaiting_confirmation: bool = False
    last_state: SessionState | None = None

    while True:
        try:
            user_input = input("Operador: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nEncerrando...")
            sys.exit(0)

        if not user_input:
            continue

        if user_input.lower() in ("sair", "exit", "quit"):
            print("Até logo!")
            sys.exit(0)

        _print_separator()

        # Track whether this input is a confirmation response (not a new operator request)
        is_confirmation_response = (
            awaiting_confirmation
            and pending_plan is not None
            and pending_step_id is not None
        )

        telemetry_mark = TELEMETRY.snapshot()

        if is_confirmation_response:
            confirmed = user_input.strip().lower() == "confirmo"
            result = resume(
                state=last_state,
                pending_plan=pending_plan,
                confirmed_step_id=pending_step_id,
                confirmed_params=pending_confirmed_params,
                operator_confirmed=confirmed,
            )
            awaiting_confirmation = False
            pending_plan = None
            pending_step_id = None
            pending_confirmed_params = None
        else:
            new_state = SessionState()
            new_state.on_progress = _print_progress
            result = run(user_input, state=new_state, memory=memory)
            last_state = result.state

        _print_debug(result.state)
        if DEBUG:
            _print_telemetry(telemetry_mark)
        _handle_result(result)

        # Only add genuine operator requests to memory, not confirmation responses.
        # "confirmo" / cancel are internal gates — adding them to history confuses
        # the rewriter and long-term summary.
        if not is_confirmation_response:
            memory.add_turn(user_input, result.response)

        if result.status == "needs_confirmation":
            awaiting_confirmation = True
            pending_plan = result.pending_plan
            pending_step_id = result.pending_step_id
            # Save pre-resolved params so second pass doesn't need to re-resolve $step refs
            ps = result.pending_step
            pending_confirmed_params = (
                ps.result.get("params") if ps and isinstance(ps.result, dict) else None
            )
        else:
            awaiting_confirmation = False
            pending_plan = None
            pending_step_id = None
            pending_confirmed_params = None

        _print_separator()


def _handle_result(result: PipelineResult) -> None:
    icons = {
        "completed": "✅",
        "needs_confirmation": "⚠️",
        "needs_info": "❓",
        "safe_alternative": "💡",
        "blocked": "🚫",
        "halted": "🛑",
        "partial": "⚠️",
        "failed": "❌",
        "no_tools": "🤷",
    }
    icon = icons.get(result.status, "•")
    _print_agent(f"{icon}  {result.response}")


if __name__ == "__main__":
    main()
