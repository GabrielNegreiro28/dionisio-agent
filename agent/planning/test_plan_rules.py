"""
Unit tests for the pure plan-validation rules. No pydantic, no I/O.
Run: python -m pytest planning/test_plan_rules.py -q
"""

import pytest
from planning.plan_rules import validate_node, validate_plan, infer_kind, PlanValidationError


# --- kind inference ---------------------------------------------------------

def test_infer_api():
    assert infer_kind({"id": 1, "tool": "reservations_list", "params": {}}) == "api"

def test_infer_compute():
    assert infer_kind({"id": 2, "operation": "filter"}) == "compute"

def test_infer_fanout():
    assert infer_kind({"id": 3, "over": [1, 2], "node": {}}) == "fanout"

def test_infer_verify():
    assert infer_kind({"id": 4, "check": "non_empty"}) == "verify"


# --- normalization ----------------------------------------------------------

def test_compute_default_output_key():
    n = validate_node({"id": 2, "operation": "filter", "params": {"source": "$step1.items"}})
    assert n["output_key"] == "result"

def test_legacy_transform_bridged_to_compute():
    t = validate_node({"id": 5, "tool": "__transform__",
                       "params": {"operation": "add", "a": 1, "b": 2, "output_key": "new_start"}})
    assert t["kind"] == "compute" and t["operation"] == "add" and t["output_key"] == "new_start"

def test_fanout_default_output_key():
    fo = validate_node({"id": 8, "over": ["2026-06-03"], "as": "date",
                        "node": {"id": 81, "tool": "orders_list", "params": {"date": "$item.date"}}})
    assert fo["kind"] == "fanout" and fo["output_key"] == "items"


# --- valid verify forms -----------------------------------------------------

def test_verify_keyword():
    assert validate_node({"id": 6, "check": "non_empty", "params": {"source": "$s1.items"}})["kind"] == "verify"

def test_verify_cmp():
    assert validate_node({"id": 7, "check": {"cmp": "gt", "value": 0}, "params": {"value": "$s4.seats"}})["kind"] == "verify"


# --- error cases ------------------------------------------------------------

@pytest.mark.parametrize("node", [
    {"id": 9, "kind": "api", "params": {}},                                  # api missing tool
    {"id": 10, "operation": "frobnicate"},                                   # bad operation
    {"id": 11, "check": "whatever"},                                         # bad check keyword
    {"id": 12, "check": {"cmp": "approx", "value": 1}},                      # bad comparator
    {"id": 13, "check": {"cmp": "gt"}},                                      # cmp missing value
    {"id": 14, "over": [1], "node": {"id": 141, "tool": "x"}},               # fanout missing 'as'
    {"id": 15, "over": [1], "as": "d", "node": {"id": 151, "check": "non_empty"}},  # fanout body verify
    {"id": 16, "tool": "__transform__"},                                     # transform without operation
])
def test_invalid_nodes_raise(node):
    with pytest.raises(PlanValidationError):
        validate_node(node)


# --- plan-level -------------------------------------------------------------

def test_plan_normalizes_kinds():
    plan = {"description": "x", "steps": [
        {"id": 1, "tool": "reservations_list", "params": {"date": "2026-06-12"}},
        {"id": 2, "operation": "find", "output_key": "joao", "depends_on": [1],
         "params": {"source": "$step1.items", "where": {"field": "clientName", "cmp": "contains", "value": "João"}}},
        {"id": 3, "check": {"cmp": "gt", "value": 0}, "on_fail": "sem reserva", "depends_on": [2],
         "params": {"value": "$step2.joao"}},
    ]}
    vp = validate_plan(plan)
    assert [s["kind"] for s in vp["steps"]] == ["api", "compute", "verify"]

def test_duplicate_ids_raise():
    with pytest.raises(PlanValidationError):
        validate_plan({"steps": [{"id": 1, "tool": "a"}, {"id": 1, "tool": "b"}]})

def test_dependency_must_be_prior():
    with pytest.raises(PlanValidationError):
        validate_plan({"steps": [{"id": 1, "tool": "a", "depends_on": [2]}]})


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
