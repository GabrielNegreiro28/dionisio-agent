import re
from typing import Any


# Matches $name or $name.path  (name = step1, item, or any bound alias).
# Examples: $step1.items[0].id   $item.date   $item   $date.start
_REF_PATTERN = re.compile(r"^\$([A-Za-z_]\w*)(?:\.(.+))?$")


def _navigate(obj: Any, path: str) -> Any:
    """Navigate a nested structure using dot notation and array indexing.
    Example: 'items[0].id' on {'items': [{'id': 'abc'}]} → 'abc'
    """
    parts = re.split(r"\.(?![^\[]*\])", path)
    current = obj
    for part in parts:
        # Handle array index: e.g. 'items[0]'
        array_match = re.match(r"^(.+)\[(\d+)\]$", part)
        if array_match:
            key, idx = array_match.group(1), int(array_match.group(2))
            lst = current[key]
            if not isinstance(lst, list):
                raise KeyError(f"Expected list at '{key}', got {type(lst).__name__}")
            if idx >= len(lst):
                raise IndexError(
                    f"Index [{idx}] out of range for '{key}' (length={len(lst)}). "
                    f"The list may be empty or have fewer items than expected."
                )
            current = lst[idx]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"Cannot navigate '{part}' on {type(current)}")
    return current


def resolve_params(params: dict, step_results: dict, extra: dict = None) -> dict:
    """
    Replace $name.path references in params with actual values.

    step_results: {'step1': <result>, 'step2': <result>, ...}
    extra: additional named bindings, e.g. {'item': <element>, 'date': <element>}
           used inside fanout sub-nodes. Takes precedence over step_results.
    """
    context = dict(step_results)
    if extra:
        context.update(extra)
    resolved = {}
    for key, value in params.items():
        resolved[key] = _resolve_value(value, context)
    return resolved


def _resolve_value(value: Any, context: dict) -> Any:
    if isinstance(value, str):
        match = _REF_PATTERN.match(value)
        if match:
            name = match.group(1)
            path = match.group(2)
            if name not in context:
                raise ValueError(f"Reference '{value}' points to unknown '{name}'")
            base = context[name]
            return base if path is None else _navigate(base, path)
        return value
    elif isinstance(value, dict):
        return {k: _resolve_value(v, context) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_value(v, context) for v in value]
    return value
