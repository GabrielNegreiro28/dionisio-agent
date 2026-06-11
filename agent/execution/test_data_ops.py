"""
Unit tests for the data-op registry. Pure functions → no mocks, no LLM, no network.
Run: python -m pytest execution/test_data_ops.py -q   (or plain `python execution/test_data_ops.py`)
"""

import pytest
from execution.data_ops import run_data_op, describe_ops, DataOpError


# --- fixtures mirroring the shapes seen in the debug trace -------------------

CLIENTS = [
    {"id": "c1", "name": "Vinícius Carvalho", "totalSpent": 1000, "couponsUsed": 0},
    {"id": "c2", "name": "Patrícia Oliveira", "totalSpent": 900, "couponsUsed": 2},
    {"id": "c3", "name": "Caio Costa", "totalSpent": 500, "couponsUsed": 0},
    {"id": "c4", "name": "Ana Lima", "totalSpent": 300, "couponsUsed": 0},
]

RESERVATIONS = [
    {"id": "res_a", "clientName": "Maria Souza", "duration": {"start": 1000}},
    {"id": "res_g0mbg6b3", "clientName": "João Silva", "duration": {"start": 1781296200000}},
]

ORDERS = [
    {"id": "o1", "clientId": "c1", "items": [{"name": "Risoto de Funghi"}, {"name": "Água"}]},
    {"id": "o2", "clientId": "c2", "items": [{"name": "Pizza"}]},
    {"id": "o3", "clientId": "c1", "items": [{"name": "Risoto de Funghi"}]},
]


# --- filter -----------------------------------------------------------------

def test_filter_single_predicate():
    out = run_data_op("filter", {"source": CLIENTS, "where": {"field": "totalSpent", "cmp": "gte", "value": 500}})
    assert [c["id"] for c in out] == ["c1", "c2", "c3"]


def test_filter_and_list_query4():
    # ">=500 AND never used a coupon" — the query-4 case, now deterministic
    out = run_data_op("filter", {"source": CLIENTS, "where": [
        {"field": "totalSpent", "cmp": "gte", "value": 500},
        {"field": "couponsUsed", "cmp": "eq", "value": 0},
    ]})
    assert [c["id"] for c in out] == ["c1", "c3"]


def test_filter_nested_wildcard_query3():
    # orders whose ANY line item is "Risoto de Funghi" — the query-3 case
    out = run_data_op("filter", {"source": ORDERS, "where": {"field": "items[].name", "cmp": "eq", "value": "Risoto de Funghi"}})
    assert [o["id"] for o in out] == ["o1", "o3"]


def test_filter_contains_case_insensitive():
    out = run_data_op("filter", {"source": RESERVATIONS, "where": {"field": "clientName", "cmp": "contains", "value": "joão"}})
    assert len(out) == 1 and out[0]["id"] == "res_g0mbg6b3"


# --- find -------------------------------------------------------------------

def test_find_by_name_query2():
    # "a reserva do João" — selection by name, NOT items[0] guessing
    got = run_data_op("find", {"source": RESERVATIONS, "where": {"field": "clientName", "cmp": "contains", "value": "João"}})
    assert got["id"] == "res_g0mbg6b3"


def test_find_no_match_returns_none():
    assert run_data_op("find", {"source": RESERVATIONS, "where": {"field": "clientName", "cmp": "eq", "value": "Ninguém"}}) is None


# --- count / sort / slice / project / dedup ---------------------------------

def test_count_with_predicate():
    assert run_data_op("count", {"source": CLIENTS, "where": {"field": "couponsUsed", "cmp": "eq", "value": 0}}) == 3


def test_count_all():
    assert run_data_op("count", {"source": CLIENTS}) == 4


def test_sort_desc_missing_last():
    data = [{"v": 3}, {"v": 1}, {"nope": 0}, {"v": 2}]
    out = run_data_op("sort", {"source": data, "by": "v", "order": "desc"})
    assert [d.get("v") for d in out] == [3, 2, 1, None]


def test_slice_limit():
    assert run_data_op("slice", {"source": [1, 2, 3, 4], "start": 1, "limit": 2}) == [2, 3]


def test_project_dot_path():
    out = run_data_op("project", {"source": RESERVATIONS, "fields": ["id", "duration.start"]})
    assert out[0] == {"id": "res_a", "duration.start": 1000}


def test_dedup_by_field():
    # distinct clientIds among Risoto orders → the "who ordered it" answer
    out = run_data_op("dedup", {"source": ORDERS, "by": "clientId"})
    assert [o["clientId"] for o in out] == ["c1", "c2"]


# --- scalar derivation ------------------------------------------------------

def test_add_reschedule_two_days():
    # +2 days in ms, the query-2 reschedule math
    assert run_data_op("add", {"a": 1781296200000, "b": 172800000}) == 1781469000000


def test_multiply_discount():
    assert run_data_op("multiply", {"a": 100, "b": 0.85}) == 85.0


# --- error handling ---------------------------------------------------------

def test_source_not_list_is_clean_error():
    # the query-5 failure shape: step1 returned a dict, not a list
    with pytest.raises(DataOpError) as e:
        run_data_op("filter", {"source": {"id": "x"}, "where": {"field": "id", "cmp": "eq", "value": "x"}})
    assert "expects 'source' to be a list" in str(e.value)


def test_unknown_operation():
    with pytest.raises(DataOpError):
        run_data_op("frobnicate", {"source": []})


def test_unknown_comparator():
    with pytest.raises(DataOpError):
        run_data_op("filter", {"source": CLIENTS, "where": {"field": "id", "cmp": "approx", "value": 1}})


def test_add_rejects_non_numeric():
    with pytest.raises(DataOpError):
        run_data_op("add", {"a": "abc", "b": 1})


def test_add_rejects_bool():
    with pytest.raises(DataOpError):
        run_data_op("add", {"a": True, "b": 1})


def test_divide_by_zero():
    with pytest.raises(DataOpError):
        run_data_op("divide", {"a": 1, "b": 0})


def test_describe_ops_lists_all():
    text = describe_ops()
    for name in ("filter", "find", "count", "sort", "project", "dedup", "add"):
        assert name in text


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
