"""
Deterministic data-op registry.

Pure, side-effect-free operations over data produced by prior plan steps.
No network, no LLM, no global state — every op is a pure function of its params,
which makes the whole layer unit-testable without any external dependency.

This is the layer that takes filtering / selection / counting / derivation OUT of
the response LLM (where it gets hallucinated) and into deterministic code.

Contract:
- Inputs arrive already resolved: $stepN.path references are substituted upstream
  by the resolver, so an op receives concrete lists / dicts / scalars.
- `source` (for collection ops) is the resolved data — typically a list.
- A predicate is DECLARATIVE and RESTRICTED: {"field": <path>, "cmp": <op>, "value": <literal>}.
  `where` accepts a single predicate OR a list of predicates (implicitly AND-ed).
  There is no OR and no arbitrary code — chain steps for anything more complex.
- `field` is a dot-path into each item. It supports:
    "name"                top-level field
    "duration.start"      nested field
    "items[].name"        wildcard: matches ANY element of the `items` list
    "items[0].name"       explicit index
- Every failure raises DataOpError with an operator-safe, descriptive message.
"""

import re
from typing import Any, Callable


class DataOpError(Exception):
    """Raised when a data-op receives invalid input or an unknown operation."""
    pass


# Comparators that need both operands to be ordered/numeric.
_ORDER_CMPS = {"gt", "gte", "lt", "lte"}
_VALID_CMPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"}

_SEGMENT_RE = re.compile(r"^([^\[\]]+)(\[\d*\])?$")


# ---------------------------------------------------------------------------
# Field navigation (soft: missing fields yield [], never raise)
# ---------------------------------------------------------------------------

def _field_values(obj: Any, path: str) -> list:
    """
    Return every value reachable by `path` on `obj`.

    Returns a list because a wildcard ("items[].name") can match many values.
    A plain path returns a single-element list; a missing field returns [].
    """
    current = [obj]
    for raw_part in path.split("."):
        m = _SEGMENT_RE.match(raw_part.strip())
        if not m:
            raise DataOpError(f"Invalid field path segment: '{raw_part}' in '{path}'")
        key, bracket = m.group(1), m.group(2)
        nxt: list = []
        for c in current:
            if not isinstance(c, dict) or key not in c:
                continue
            val = c[key]
            if bracket is None:
                nxt.append(val)
            elif bracket == "[]":
                if isinstance(val, list):
                    nxt.extend(val)
            else:  # explicit index, e.g. [0]
                idx = int(bracket[1:-1])
                if isinstance(val, list) and idx < len(val):
                    nxt.append(val[idx])
        current = nxt
    return current


# ---------------------------------------------------------------------------
# Predicate evaluation
# ---------------------------------------------------------------------------

def _compare(actual: Any, cmp: str, value: Any) -> bool:
    """Apply a single comparator. Ordering on incompatible types yields False."""
    if cmp == "eq":
        return actual == value
    if cmp == "ne":
        return actual != value
    if cmp == "contains":
        # case-insensitive substring for strings; membership for lists
        if isinstance(actual, str) and isinstance(value, str):
            return value.lower() in actual.lower()
        if isinstance(actual, (list, tuple)):
            return value in actual
        return False
    if cmp == "in":
        if not isinstance(value, (list, tuple)):
            raise DataOpError("'in' comparator requires a list as value")
        return actual in value
    if cmp in _ORDER_CMPS:
        try:
            if cmp == "gt":
                return actual > value
            if cmp == "gte":
                return actual >= value
            if cmp == "lt":
                return actual < value
            return actual <= value
        except TypeError:
            return False  # heterogeneous data: treat as non-match, don't crash
    raise DataOpError(f"Unknown comparator '{cmp}'. Allowed: {sorted(_VALID_CMPS)}")


def _validate_predicate(p: Any) -> None:
    if not isinstance(p, dict):
        raise DataOpError(f"Predicate must be an object, got {type(p).__name__}")
    if "field" not in p or "cmp" not in p:
        raise DataOpError("Predicate requires 'field' and 'cmp'")
    if p["cmp"] not in _VALID_CMPS:
        raise DataOpError(f"Unknown comparator '{p['cmp']}'. Allowed: {sorted(_VALID_CMPS)}")
    if p["cmp"] != "exists" and "value" not in p:
        raise DataOpError(f"Comparator '{p['cmp']}' requires a 'value'")


def _match(item: Any, where: Any) -> bool:
    """True if `item` satisfies `where` (a predicate or AND-list of predicates)."""
    if where is None:
        return True
    if isinstance(where, list):
        return all(_match(item, p) for p in where)

    _validate_predicate(where)
    field, cmp = where["field"], where["cmp"]

    if cmp == "exists":
        present = len(_field_values(item, field)) > 0
        return present == bool(where.get("value", True))

    value = where.get("value")
    actuals = _field_values(item, field)
    if not actuals:
        return False
    # Wildcard paths may yield several values — ANY match satisfies the predicate.
    return any(_compare(a, cmp, value) for a in actuals)


# ---------------------------------------------------------------------------
# Verify-node check evaluation (used to gate destructive ops on a precondition)
# ---------------------------------------------------------------------------

def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return True


def evaluate_check(value: Any, check: Any) -> bool:
    """
    Evaluate a verify-node check against a resolved value. Pure.

    check is either a keyword ('non_empty'|'empty'|'truthy'|'falsy') or a
    {"cmp": ..., "value": ...} object (same comparators as predicates).
    'exists' tests presence (value is not None).
    """
    if isinstance(check, str):
        if check == "non_empty":
            return _is_non_empty(value)
        if check == "empty":
            return not _is_non_empty(value)
        if check == "truthy":
            return bool(value)
        if check == "falsy":
            return not bool(value)
        raise DataOpError(f"Unknown check keyword '{check}'")
    if isinstance(check, dict):
        cmp = check.get("cmp")
        if cmp == "exists":
            return (value is not None) == bool(check.get("value", True))
        return _compare(value, cmp, check.get("value"))
    raise DataOpError("check must be a keyword string or a {cmp, value} object")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_list(params: dict, op: str) -> list:
    source = params.get("source")
    if not isinstance(source, list):
        raise DataOpError(
            f"'{op}' expects 'source' to be a list, got {type(source).__name__}. "
            f"A prior step likely returned an object instead of a collection."
        )
    return source


def _single_value(item: Any, path: str) -> Any:
    """First value at `path`, or None. Used by sort/project/dedup (non-predicate)."""
    vals = _field_values(item, path)
    return vals[0] if vals else None


# ---------------------------------------------------------------------------
# Operations (each: params -> value, pure)
# ---------------------------------------------------------------------------

def _op_filter(params: dict) -> list:
    source = _require_list(params, "filter")
    where = params.get("where")
    return [item for item in source if _match(item, where)]


def _op_find(params: dict) -> Any:
    source = _require_list(params, "find")
    where = params.get("where")
    for item in source:
        if _match(item, where):
            return item
    return None


def _op_count(params: dict) -> int:
    source = _require_list(params, "count")
    where = params.get("where")
    if where is None:
        return len(source)
    return sum(1 for item in source if _match(item, where))


def _op_sort(params: dict) -> list:
    source = _require_list(params, "sort")
    by = params.get("by")
    if not by:
        raise DataOpError("'sort' requires 'by' (a field path)")
    order = params.get("order", "asc")
    if order not in ("asc", "desc"):
        raise DataOpError("'sort' order must be 'asc' or 'desc'")
    # Missing/None values sort last regardless of direction: partition first so
    # the reverse flag never flips them to the front.
    present = [it for it in source if _single_value(it, by) is not None]
    missing = [it for it in source if _single_value(it, by) is None]
    try:
        present.sort(key=lambda it: _single_value(it, by), reverse=(order == "desc"))
    except TypeError:
        raise DataOpError(f"'sort' cannot order field '{by}': mixed/incomparable types")
    return present + missing


def _op_slice(params: dict) -> list:
    source = _require_list(params, "slice")
    start = params.get("start", 0)
    if "limit" in params:
        return source[start:start + int(params["limit"])]
    end = params.get("end")
    return source[start:end]


def _op_project(params: dict) -> list:
    source = _require_list(params, "project")
    fields = params.get("fields")
    if not isinstance(fields, list) or not fields:
        raise DataOpError("'project' requires a non-empty 'fields' list")
    out = []
    for item in source:
        out.append({f: _single_value(item, f) for f in fields})
    return out


def _op_dedup(params: dict) -> list:
    source = _require_list(params, "dedup")
    by = params.get("by")
    seen = set()
    out = []
    for item in source:
        marker = _single_value(item, by) if by else _hashable(item)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def _hashable(item: Any):
    """Best-effort stable marker for whole-item dedup."""
    if isinstance(item, (dict, list)):
        import json
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    return item


# --- Scalar derivation (migrated from step_runner._SAFE_OPS, with type guards) ---

def _num(label: str, op: str, v: Any) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DataOpError(f"'{op}' operand '{label}' = {v!r} is not numeric")
    return v


def _op_add(p: dict):
    return _num("a", "add", p.get("a")) + _num("b", "add", p.get("b", 0))


def _op_subtract(p: dict):
    return _num("a", "subtract", p.get("a")) - _num("b", "subtract", p.get("b", 0))


def _op_multiply(p: dict):
    return _num("a", "multiply", p.get("a")) * _num("b", "multiply", p.get("b", 1))


def _op_divide(p: dict):
    b = _num("b", "divide", p.get("b"))
    if b == 0:
        raise DataOpError("'divide' by zero")
    return _num("a", "divide", p.get("a")) / b


def _op_concat(p: dict):
    return str(p.get("a", "")) + str(p.get("b", ""))


def _op_pick(p: dict):
    if "a" not in p:
        raise DataOpError("'pick' requires 'a'")
    return p["a"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable[[dict], Any]] = {
    "filter":   _op_filter,
    "find":     _op_find,
    "count":    _op_count,
    "sort":     _op_sort,
    "slice":    _op_slice,
    "project":  _op_project,
    "dedup":    _op_dedup,
    "add":      _op_add,
    "subtract": _op_subtract,
    "multiply": _op_multiply,
    "divide":   _op_divide,
    "concat":   _op_concat,
    "pick":     _op_pick,
}

# Param metadata — single source of truth for the planner catalog (used later).
SPECS: dict[str, dict] = {
    "filter":   {"params": "source, where", "returns": "list", "desc": "Keep items matching predicate(s)."},
    "find":     {"params": "source, where", "returns": "object|null", "desc": "First item matching predicate(s)."},
    "count":    {"params": "source, where?", "returns": "int", "desc": "Count items (optionally matching predicate)."},
    "sort":     {"params": "source, by, order?", "returns": "list", "desc": "Sort by field, asc|desc."},
    "slice":    {"params": "source, start?, end?|limit?", "returns": "list", "desc": "Sublist by range or limit."},
    "project":  {"params": "source, fields", "returns": "list", "desc": "Keep only the given fields per item."},
    "dedup":    {"params": "source, by?", "returns": "list", "desc": "Remove duplicates (by field or whole item)."},
    "add":      {"params": "a, b", "returns": "number", "desc": "a + b (numbers)."},
    "subtract": {"params": "a, b", "returns": "number", "desc": "a - b (numbers)."},
    "multiply": {"params": "a, b", "returns": "number", "desc": "a * b (numbers)."},
    "divide":   {"params": "a, b", "returns": "number", "desc": "a / b (numbers)."},
    "concat":   {"params": "a, b", "returns": "string", "desc": "String concatenation."},
    "pick":     {"params": "a", "returns": "any", "desc": "Alias a value under a new key."},
}


# Public constants — single source of truth for plan-time validation.
OPERATIONS: frozenset = frozenset(_REGISTRY)
COMPARATORS: frozenset = frozenset(_VALID_CMPS)


def run_data_op(operation: str, params: dict) -> Any:
    """
    Execute one data-op and return its value. Pure: no I/O, no state mutation.
    Raises DataOpError on unknown op or invalid input.
    """
    if not isinstance(operation, str):
        raise DataOpError(f"operation must be a string, got {type(operation).__name__}")
    op = operation.lower()
    fn = _REGISTRY.get(op)
    if fn is None:
        raise DataOpError(f"Unknown operation '{operation}'. Allowed: {sorted(_REGISTRY)}")
    return fn(params)


def describe_ops() -> str:
    """Render the op catalog as text (for the planner prompt, later)."""
    lines = []
    for name, spec in SPECS.items():
        lines.append(f"- {name}({spec['params']}) -> {spec['returns']}: {spec['desc']}")
    return "\n".join(lines)
