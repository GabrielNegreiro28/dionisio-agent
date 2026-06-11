from __future__ import annotations

from pydantic import BaseModel, Field, model_validator
from typing import Any, Optional

from planning.plan_rules import validate_node, PlanValidationError


class PlanNode(BaseModel):
    """
    A single plan node. Four kinds (api | compute | verify | fanout); see
    planning/plan_rules.py for the field contract. `kind` is inferred from the
    fields present when omitted, and legacy `__transform__` steps are bridged to
    compute. Validation is delegated to the pure rules module so it stays the
    single source of truth.
    """
    id: int = Field(description="Unique step number, starting at 1")
    description: str = Field(default="", description="What this step does (PT-BR)")
    kind: Optional[str] = Field(default=None, description="api|compute|verify|fanout (inferred if omitted)")
    depends_on: list[int] = Field(default_factory=list, description="Ids that must run first")

    # api
    tool: Optional[str] = Field(default=None, description="Tool name (api nodes)")
    params: dict[str, Any] = Field(default_factory=dict, description="Params; '$stepN.path' references prior results")

    # compute
    operation: Optional[str] = Field(default=None, description="Data-op name (compute nodes)")
    output_key: Optional[str] = Field(default=None, description="Key the result is stored under")

    # verify
    check: Optional[Any] = Field(default=None, description="'non_empty'|'empty'|'truthy'|'falsy' or {cmp, value}")
    on_fail: Optional[str] = Field(default=None, description="Operator-facing message if the check fails")

    # fanout
    over: Optional[Any] = Field(default=None, description="List or $stepN ref to iterate")
    as_param: Optional[str] = Field(default=None, alias="as", description="Per-element param name")
    node: Optional["PlanNode"] = Field(default=None, description="Sub-node run per element (api|compute)")
    collect: Optional[str] = Field(default=None, description="'list' (default) or 'merge_items'")

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _normalize_and_validate(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        try:
            return validate_node(data)  # returns normalized node, raises on problems
        except PlanValidationError as e:
            raise ValueError(str(e)) from e


class Plan(BaseModel):
    description: str = Field(description="Natural language summary of the full plan")
    steps: list[PlanNode] = Field(description="Ordered list of nodes to execute")
    # Capabilities the request asked for but no tool can satisfy (partial fulfillment).
    unsupported: list[str] = Field(default_factory=list, description="Requested actions with no available tool")

    @model_validator(mode="after")
    def _plan_level(self) -> "Plan":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Duplicate step ids: {dupes}")
        seen: set = set()
        for s in self.steps:
            for dep in s.depends_on:
                if dep not in seen:
                    raise ValueError(f"step {s.id}: depends_on {dep} is not a prior step")
            seen.add(s.id)
        return self


PlanNode.model_rebuild()

# Backwards-compatible alias: executor/step_runner import PlanStep.
PlanStep = PlanNode
