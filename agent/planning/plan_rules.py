"""
Pure plan validation rules — no pydantic, no I/O, no LLM.

This is the single source of truth for what a well-formed plan node looks like,
factored out of the pydantic models so it can be unit-tested standalone (the
sandbox has no pydantic) and reused by the executor at dispatch time.

Node kinds
----------
api      — calls a tool (goes through the risk gate). Fields: tool, params.
compute  — runs a deterministic data-op (no risk gate). Fields: operation,
           params (source/where/by/...), output_key.
verify   — asserts a boolean over a resolved value; halts the plan if false.
           Fields: params (value/source), check, on_fail.
fanout   — runs an api|compute sub-node once per element of a list, collects
           results. Fields: over, as_param ("as"), node, output_key, collect.

`kind` may be omitted by the planner; it is inferred from the fields present.
Legacy `tool: "__transform__"` steps are bridged to compute automatically.
"""

# Single source of truth for valid op/comparator names lives in the data-op
# registry. Fall back to a flat import when tested in isolation.
import re

try:  # production layout
    from execution.data_ops import OPERATIONS, COMPARATORS
except ImportError:  # isolated test
    from data_ops import OPERATIONS, COMPARATORS

_STEP_REF = re.compile(r"\$step(\d+)")


class PlanValidationError(Exception):
    """Raised when a plan node or plan structure is malformed."""
    pass


NODE_KINDS = frozenset({"api", "compute", "verify", "fanout"})
_TRANSFORM_TOOL = "__transform__"
_CHECK_KEYWORDS = frozenset({"non_empty", "empty", "truthy", "falsy"})
_FANOUT_BODY_KINDS = frozenset({"api", "compute"})


def infer_kind(node: dict) -> str:
    """Determine a node's kind from explicit `kind` or the fields present."""
    explicit = node.get("kind")
    if explicit:
        return explicit
    if node.get("operation") is not None or node.get("tool") == _TRANSFORM_TOOL:
        return "compute"
    if node.get("over") is not None or node.get("node") is not None:
        return "fanout"
    if node.get("check") is not None:
        return "verify"
    return "api"


def _as_param_of(node: dict):
    """Accept both 'as_param' (python) and 'as' (planner JSON alias)."""
    return node.get("as_param", node.get("as"))


def normalize_node(node: dict) -> dict:
    """
    Return a shallow copy with `kind` resolved and legacy transform steps
    bridged to compute (operation/output_key lifted out of params).
    Does not mutate the input.
    """
    n = dict(node)
    n["kind"] = infer_kind(n)

    if n["kind"] == "compute":
        if n.get("operation") is None:
            params = n.get("params") or {}
            n["operation"] = params.get("operation")  # bridge __transform__
            if n.get("output_key") is None:
                n["output_key"] = params.get("output_key", "result")
        if n.get("output_key") is None:
            n["output_key"] = "result"

    if n["kind"] == "fanout" and n.get("output_key") is None:
        n["output_key"] = "items"

    return n


def _validate_check(check, nid) -> None:
    if isinstance(check, str):
        if check not in _CHECK_KEYWORDS:
            raise PlanValidationError(
                f"verify node {nid}: unknown check '{check}'. "
                f"Allowed keywords: {sorted(_CHECK_KEYWORDS)} or a {{cmp, value}} object."
            )
        return
    if isinstance(check, dict):
        cmp = check.get("cmp")
        if cmp not in COMPARATORS:
            raise PlanValidationError(
                f"verify node {nid}: check has unknown comparator '{cmp}'. "
                f"Allowed: {sorted(COMPARATORS)}"
            )
        if cmp != "exists" and "value" not in check:
            raise PlanValidationError(f"verify node {nid}: check '{cmp}' requires a 'value'")
        return
    raise PlanValidationError(
        f"verify node {nid}: 'check' must be a keyword string or a {{cmp, value}} object"
    )


def validate_node(node: dict) -> dict:
    """
    Validate a single node. Returns the normalized node.
    Raises PlanValidationError with an operator-safe message on any problem.
    """
    if not isinstance(node, dict):
        raise PlanValidationError(f"Node must be an object, got {type(node).__name__}")

    n = normalize_node(node)
    kind = n["kind"]
    nid = n.get("id", "?")

    if kind not in NODE_KINDS:
        raise PlanValidationError(f"Node {nid}: unknown kind '{kind}'. Allowed: {sorted(NODE_KINDS)}")
    if "id" not in node:
        raise PlanValidationError(f"Node missing 'id' (kind={kind})")

    if kind == "api":
        if not n.get("tool"):
            raise PlanValidationError(f"api node {nid}: missing 'tool'")
        if n.get("tool") == _TRANSFORM_TOOL:
            raise PlanValidationError(f"node {nid}: '__transform__' must be a compute node")

    elif kind == "compute":
        op = n.get("operation")
        if not op:
            raise PlanValidationError(f"compute node {nid}: missing 'operation'")
        if op not in OPERATIONS:
            raise PlanValidationError(
                f"compute node {nid}: unknown operation '{op}'. Allowed: {sorted(OPERATIONS)}"
            )

    elif kind == "verify":
        if n.get("check") is None:
            raise PlanValidationError(f"verify node {nid}: missing 'check'")
        _validate_check(n["check"], nid)

    elif kind == "fanout":
        if n.get("over") is None:
            raise PlanValidationError(f"fanout node {nid}: missing 'over' (list or $stepN ref)")
        if not _as_param_of(n):
            raise PlanValidationError(f"fanout node {nid}: missing 'as' (per-element param name)")
        sub = n.get("node")
        if not isinstance(sub, dict):
            raise PlanValidationError(f"fanout node {nid}: 'node' must be a sub-node object")
        sub_kind = infer_kind(sub)
        if sub_kind not in _FANOUT_BODY_KINDS:
            raise PlanValidationError(
                f"fanout node {nid}: body must be api|compute, got '{sub_kind}'"
            )
        validate_node(sub)  # recurse

    return n


def _step_refs(node: dict) -> set:
    """All $stepN ids referenced anywhere in a node's params/over/check/sub-node."""
    refs: set = set()

    def scan(o):
        if isinstance(o, str):
            refs.update(int(n) for n in _STEP_REF.findall(o))
        elif isinstance(o, dict):
            for v in o.values():
                scan(v)
        elif isinstance(o, list):
            for v in o:
                scan(v)

    scan(node.get("params"))
    scan(node.get("over"))
    scan(node.get("check"))
    sub = node.get("node")
    if isinstance(sub, dict):
        scan(sub.get("params"))
    return refs


def validate_plan(plan: dict) -> dict:
    """
    Validate full plan structure: each node, unique ids, and that every
    depends_on references an earlier-defined node id. Returns the plan with
    normalized nodes. Raises PlanValidationError on any problem.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise PlanValidationError("Plan must be an object with a 'steps' list")

    steps = plan["steps"]
    normalized = [validate_node(s) for s in steps]

    ids = [n["id"] for n in normalized]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise PlanValidationError(f"Duplicate step ids: {dupes}")

    seen: set = set()
    for n in normalized:
        for dep in n.get("depends_on", []) or []:
            if dep not in seen:
                raise PlanValidationError(
                    f"step {n['id']}: depends_on {dep} is not a prior step"
                )
        # Every $stepK reference must point to an EARLIER step (catches self- and
        # forward-references like a step whose source is its own output).
        for ref in _step_refs(n):
            if ref not in seen:
                raise PlanValidationError(
                    f"step {n['id']}: references $step{ref} which is not a prior step"
                )
        seen.add(n["id"])

    out = dict(plan)
    out["steps"] = normalized
    return out
