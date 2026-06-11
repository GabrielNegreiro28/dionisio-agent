"""
Contract sync test: the hand-written tool catalog vs the OpenAPI spec.

Guarantees the catalog never drifts from the real API contract:
- every spec operation has a matching tool (and vice-versa)
- HTTP method, path and the destructive flag match the contract
- required params declared by the spec are present on the tool

It does NOT assert response shapes (the mock spec returns bare 200s for several
domains) — those live in the DATA_MODEL, fed from live dumps.

Run:  python -m pytest tools/test_openapi_sync.py -q
      python tools/test_openapi_sync.py            # also prints the enrichment gaps
"""

import json
import os
import re

try:
    import pytest
except ImportError:  # allow running without pytest
    pytest = None

from tools.definitions import ALL_TOOLS

_SPEC_PATH = os.path.join(os.path.dirname(__file__), "openapi.json")


def _load_spec() -> dict:
    with open(_SPEC_PATH, encoding="utf-8") as f:
        return json.load(f)


def _camel_to_snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def op_to_tool_name(operation_id: str) -> str:
    """clients.topSpenders -> clients_top_spenders ; orders.updateStatus -> orders_update_status"""
    domain, _, op = operation_id.partition(".")
    return f"{domain}_{_camel_to_snake(op)}"


def _spec_operations(spec: dict):
    """Yield (tool_name, http_method, path, operation_dict) for every operation."""
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            yield op_to_tool_name(op["operationId"]), method.upper(), path, op


# Build lookups once.
_SPEC = _load_spec()
_SPEC_OPS = {name: (m, p, op) for name, m, p, op in _spec_operations(_SPEC)}
_CATALOG = {t.name: t for t in ALL_TOOLS}


# --- coverage ---------------------------------------------------------------

def test_every_spec_operation_has_a_tool():
    missing = sorted(set(_SPEC_OPS) - set(_CATALOG))
    assert not missing, f"Spec operations without a tool: {missing}"


def test_no_extra_tools_outside_spec():
    extra = sorted(set(_CATALOG) - set(_SPEC_OPS))
    assert not extra, f"Tools not present in the contract: {extra}"


# --- per-operation contract -------------------------------------------------

def _matched():
    return sorted(set(_SPEC_OPS) & set(_CATALOG))


def test_methods_match():
    bad = {n: (_CATALOG[n].method.upper(), _SPEC_OPS[n][0])
           for n in _matched() if _CATALOG[n].method.upper() != _SPEC_OPS[n][0]}
    assert not bad, f"Method mismatches (tool vs spec): {bad}"


def test_paths_match():
    bad = {n: (_CATALOG[n].path_template, _SPEC_OPS[n][1])
           for n in _matched() if _CATALOG[n].path_template != _SPEC_OPS[n][1]}
    assert not bad, f"Path mismatches (tool vs spec): {bad}"


def test_destructive_flag_matches():
    bad = {}
    for n in _matched():
        spec_destructive = bool(_SPEC_OPS[n][2].get("x-destructive", False))
        if _CATALOG[n].destructive != spec_destructive:
            bad[n] = (_CATALOG[n].destructive, spec_destructive)
    assert not bad, f"Destructive flag mismatches (tool vs spec): {bad}"


def test_required_params_present():
    """Every param the spec marks required must exist on the tool (query/body/path)."""
    problems = {}
    for n in _matched():
        _, _, op = _SPEC_OPS[n]
        tool = _CATALOG[n]
        declared = set(tool.parameters_schema.get("properties", {})) | set(tool.path_params)
        required = set()
        for p in op.get("parameters", []):
            if p.get("required"):
                required.add(p["name"])
        body = (op.get("requestBody", {}).get("content", {})
                  .get("application/json", {}).get("schema", {}))
        required |= set(body.get("required", []))
        missing = required - declared
        if missing:
            problems[n] = sorted(missing)
    assert not problems, f"Required params missing on tools: {problems}"


# --- enrichment gaps (informational, not asserted) --------------------------

def enrichment_gaps() -> dict:
    """Spec params that carry an enum the tool doesn't declare yet — the rollout TODO."""
    gaps = {}
    for n in _matched():
        _, _, op = _SPEC_OPS[n]
        tool = _CATALOG[n]
        props = tool.parameters_schema.get("properties", {})
        body = (op.get("requestBody", {}).get("content", {})
                  .get("application/json", {}).get("schema", {}).get("properties", {}))
        spec_params = {p["name"]: p.get("schema", {}) for p in op.get("parameters", [])}
        spec_params.update(body)
        for pname, pschema in spec_params.items():
            if "enum" in pschema and "enum" not in props.get(pname, {}):
                gaps.setdefault(n, []).append({pname: pschema["enum"]})
    return gaps


if __name__ == "__main__":
    spec_ops, tools = set(_SPEC_OPS), set(_CATALOG)
    print(f"spec operations: {len(spec_ops)} | catalog tools: {len(tools)}")
    print(f"missing tools : {sorted(spec_ops - tools) or 'none'}")
    print(f"extra tools   : {sorted(tools - spec_ops) or 'none'}")
    gaps = enrichment_gaps()
    print(f"\nenum enrichment gaps ({len(gaps)} tools):")
    for tool, items in sorted(gaps.items()):
        print(f"  {tool}: {items}")
    if pytest:
        print()
        raise SystemExit(pytest.main([__file__, "-q"]))
