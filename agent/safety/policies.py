import re
from typing import Literal

RiskLevel = Literal["low", "medium", "high"]

# Compensating (inverse) tool for a reversible write — used by the saga net to
# offer/undo a side effect when a later step fails. Writes absent here are
# treated as irreversible (no clean undo).
COMPENSATIONS: dict[str, str] = {
    "coupons_create":        "coupons_deactivate",
    "reservations_create":   "reservations_cancel",
    "delivery_create_pause": "delivery_end_pause",
}

# Plan-time capability constraints on resolved write params — caught BEFORE the
# write executes (pre-commit), so an infeasible action never hits the API.
# pattern is a regex the param value must match.
CAPABILITY_RULES: dict[str, dict[str, str]] = {
    # assign-group attributes a coupon to an EXISTING group, not to a client.
    "coupons_assign_group": {"groupId": r"^grp_"},
}


def get_compensating_tool(tool_name: str) -> str | None:
    return COMPENSATIONS.get(tool_name)


def is_reversible(tool_name: str) -> bool:
    return tool_name in COMPENSATIONS


def capability_problem(tool_name: str, params: dict) -> str | None:
    """Operator-safe message if a write violates a known capability constraint."""
    for param, pattern in CAPABILITY_RULES.get(tool_name, {}).items():
        value = params.get(param)
        if value is not None and not re.match(pattern, str(value)):
            return (
                f"'{tool_name}' não pode ser usada assim: '{param}'={value} não é um id de "
                f"grupo (grp_*). Não há ferramenta para atribuir a clientes individuais."
            )
    return None

# Tools that are destructive but have safe alternatives to suggest
SAFE_ALTERNATIVES: dict[str, str] = {
    "delivery_create_pause": "If the intent is temporary, consider ending an existing pause instead (delivery_end_pause).",
    "promotions_delete": "Consider updating validUntil to expire the promotion instead of deleting it.",
}

# Tools that require explicit confirmation regardless of other factors
ALWAYS_CONFIRM: set[str] = {
    "reservations_cancel",
    "reservations_reschedule",
    "orders_cancel",
    "coupons_deactivate",
    "promotions_delete",
    "delivery_create_pause",
    "ifood_cancel",
}


def classify_risk(tool_name: str, destructive: bool, params: dict) -> RiskLevel:
    """
    Classify the risk level of a tool call.
    - high: always-confirm destructive operations
    - medium: write operations that affect existing records
    - low: read-only or simple creates
    """
    if tool_name in ALWAYS_CONFIRM:
        return "high"

    if destructive:
        return "high"

    # Write operations that modify existing data
    write_ops = {
        "clients_update", "clients_create",
        "reservations_update", "reservations_confirm", "reservations_create",
        "orders_create", "orders_update_status",
        "coupons_create", "coupons_update", "coupons_assign_group",
        "promotions_create", "promotions_update",
        "delivery_update_config", "delivery_end_pause",
        "store_update", "store_update_hours",
        "ifood_confirm", "ifood_dispatch",
    }
    if tool_name in write_ops:
        return "medium"

    return "low"


def get_safe_alternative(tool_name: str) -> str | None:
    return SAFE_ALTERNATIVES.get(tool_name)
