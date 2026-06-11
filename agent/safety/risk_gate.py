from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from safety.policies import classify_risk, get_safe_alternative, ALWAYS_CONFIRM, RiskLevel
from safety.validators import validate_params
from state import SessionState
from tools.definitions import ToolDefinition


@dataclass
class RiskDecision:
    action: Literal["execute", "needs_confirmation", "needs_info", "safe_alternative", "block"]
    risk_level: RiskLevel
    reason: str
    confirmation_prompt: str = ""


def _humanize_value(v: Any) -> str:
    """Render a param value for the operator. Epoch-ms ints become readable dates."""
    # 1e12 ms ≈ year 2001; anything above is plausibly a millisecond timestamp.
    if isinstance(v, int) and not isinstance(v, bool) and v > 1_000_000_000_000:
        try:
            dt = datetime.fromtimestamp(v / 1000).astimezone()
            return f"{dt.strftime('%d/%m/%Y %H:%M')} ({v})"
        except (OSError, OverflowError, ValueError):
            return str(v)
    return str(v)


class RiskGate:
    """
    Evaluates a tool call through a sequential chain of checks.
    Returns a RiskDecision that the executor acts on.

    Pipeline:
    1. Schema validation
    2. Permission check (stub — always passes in mock)
    3. State/context check
    4. Risk classification
    5. Safe alternative suggestion (for high-risk ops with alternatives)
    6. Decision
    """

    def evaluate(
        self,
        tool: ToolDefinition,
        params: dict,
        state: SessionState,
    ) -> RiskDecision:
        # Strip internal flags before validation
        clean_params = {k: v for k, v in params.items() if not k.startswith("__")}
        confirmed = params.get("__confirmed__", False)

        # 1. Schema validation
        valid, error_msg = validate_params(tool, clean_params)
        if not valid:
            return RiskDecision(
                action="block",
                risk_level="high",
                reason=f"Invalid parameters: {error_msg}",
            )

        # 2. Permission check (stub — extend for multi-user environments)
        # In production: check if the current user has permission to run this tool.
        # For the mock, all authenticated requests are permitted.

        # 3. State/context check
        context_issue = self._check_context(tool, clean_params, state)
        if context_issue:
            return RiskDecision(
                action="block",
                risk_level="medium",
                reason=context_issue,
            )

        # 4. Risk classification
        risk_level = classify_risk(tool.name, tool.destructive, clean_params)
        # Non-destructive but sensitive writes can still require confirmation
        # (decouples "should confirm" from "irreversible"). Dormant until a tool
        # sets requires_confirmation=True.
        if getattr(tool, "requires_confirmation", False) and risk_level != "high":
            risk_level = "high"

        # 5. Safe alternative suggestion (before confirmation)
        if risk_level == "high" and not confirmed:
            alt = get_safe_alternative(tool.name)
            if alt:
                return RiskDecision(
                    action="safe_alternative",
                    risk_level=risk_level,
                    reason=f"⚠️ {tool.name} is destructive. {alt}",
                )

        # 6. Decision
        if risk_level == "high" and not confirmed:
            prompt = self._build_confirmation_prompt(tool, clean_params, state)
            return RiskDecision(
                action="needs_confirmation",
                risk_level=risk_level,
                reason=f"⚠️ '{tool.name}' is a destructive, irreversible operation.",
                confirmation_prompt=prompt,
            )

        return RiskDecision(
            action="execute",
            risk_level=risk_level,
            reason="All checks passed.",
        )

    def _check_context(
        self,
        tool: ToolDefinition,
        params: dict,
        state: SessionState,
    ) -> Optional[str]:
        """
        Programmatic sanity checks against current state.
        Returns an error string if something is wrong, None if clear.
        """
        # Prevent double-cancellation of the same resource in the same session
        if tool.name in ("reservations_cancel", "orders_cancel", "ifood_cancel"):
            resource_id = params.get("id")
            if resource_id:
                for step_result in state.step_results:
                    if (
                        step_result.success
                        and step_result.tool == tool.name
                        and step_result.params.get("id") == resource_id
                    ):
                        return f"Resource '{resource_id}' was already cancelled in this session."

        return None

    def _build_confirmation_prompt(
        self,
        tool: ToolDefinition,
        params: dict,
        state: Optional[SessionState] = None,
    ) -> str:
        """
        Build a human-readable confirmation prompt for the operator.
        Enriches raw IDs with entity names from prior step results when available.
        """
        param_str = ", ".join(f"{k}={_humanize_value(v)}" for k, v in params.items())

        # Try to resolve human-readable entity context from state step results
        entity_hints = self._resolve_entity_hints(params, state)
        entity_line = f"\nEntidade: {entity_hints}" if entity_hints else ""

        return (
            f"⚠️  Ação destrutiva — não pode ser desfeita.\n"
            f"Operação: {tool.name}{entity_line}\n"
            f"Parâmetros: {param_str}\n"
            f'Para confirmar, digite "confirmo". Para cancelar, qualquer outra resposta.'
        )

    def _resolve_entity_hints(
        self,
        params: dict,
        state: Optional[SessionState],
    ) -> str:
        """
        Look through prior step results for entities whose ID appears in params.
        Returns a human-readable label like "João Silva (id=res_g0mbg6b3)" or "".
        """
        if not state:
            return ""

        param_values = {str(v) for v in params.values() if v is not None}
        hints = []

        for step_result in state.step_results:
            if not step_result.success or not isinstance(step_result.result, dict):
                continue

            # Unwrap items list or treat the dict itself as a single entity
            data = step_result.result
            candidates = data.get("items", [data])
            if not isinstance(candidates, list):
                candidates = [candidates]

            for item in candidates:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id", ""))
                if not item_id or item_id not in param_values:
                    continue
                # Find the most descriptive name field available
                name = (
                    item.get("name")
                    or item.get("clientName")
                    or item.get("title")
                    or item.get("label")
                    or ""
                )
                if name:
                    hints.append(f"{name} (id={item_id})")

        return ", ".join(hints)
