"""Feasibility-only checks for engine=\"stable\".

The default engine is covered by test_solver.py and must stay on its own path.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from app.engine_stable import prepare_stable_payload, stable_problem_status
from app.solver import solve_payload
from app.verifier import verify_solution

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
RECORDS = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _verified_stable(payload: dict, limit: float = 30.0) -> dict:
    result = solve_payload(payload, time_limit_seconds=limit, engine="stable")
    prepared = result.pop("_stable_prepared_payload")
    report = verify_solution(prepared, result)
    assert report["passed"] is True
    assert result["status"] == "FEASIBLE"
    assert result["summary"]["engine"] == "stable"
    assert result["summary"]["optimality_claimed"] is False
    for group in result["groups"]:
        metrics = group["metrics"]
        assert metrics["solve_status"] == "FEASIBLE"
        assert metrics["lexicographic_optimal"] is False
        assert metrics["core_lexicographic_optimal"] is False
        assert metrics["stable_strategy"]
    return result


def test_default_engine_is_unchanged() -> None:
    payload = _load(FIXTURES / "valid_case.json")["problem"]
    result = solve_payload(payload, time_limit_seconds=10)
    assert result["status"] == "TARGET_REACHED"
    assert result["summary"].get("engine") != "stable"
    assert "optimality_claimed" not in result["summary"]
    assert not result["groups"][0]["metrics"].get("stable_strategy")


def test_a598_phi51x13_and_phi51x9_5_are_feasible() -> None:
    a598 = _load(ROOT / "data/input_a598e024.json")
    _verified_stable(a598, 20)

    phi13 = _load(FIXTURES / "production_phi51x13.json")
    _verified_stable(phi13, 20)

    record = _load(RECORDS)["RECORDS"][0]
    raw = record["MOMPROBLEMJSON"]
    phi95 = json.loads(raw) if isinstance(raw, str) else raw
    result = _verified_stable(phi95, 20)
    assert result["groups"][0]["metrics"]["welding_joint_quantity"] >= 0


def test_prepare_keeps_explicit_blade_and_defaults_missing_blade() -> None:
    explicit = _load(FIXTURES / "valid_case.json")["problem"]
    prepared = prepare_stable_payload(explicit)
    assert prepared["input"]["NestParam"]["BladeMargin"] == "10"
    assert prepared["input"]["Min_Welding_Length"] == 0

    missing = deepcopy(explicit)
    missing["input"]["NestParam"].pop("BladeMargin")
    prepared_missing = prepare_stable_payload(missing)
    assert prepared_missing["input"]["NestParam"]["BladeMargin"] == 5


def test_reversed_forbidden_zone_still_verifies() -> None:
    payload = deepcopy(_load(FIXTURES / "valid_case.json")["problem"])
    pipe = payload["input"]["data"][0]["Pipe"][0]
    pipe["Unweldable_Area"] = [[0, 100], [1000, 900]]
    _verified_stable(payload, 15)


def test_length_shortfall_is_stock_gap_not_optimal() -> None:
    payload = _load(FIXTURES / "valid_case.json")["problem"]
    payload = deepcopy(payload)
    payload["input"]["data"][0]["Stock"] = [
        {"stock_length": 100, "stock_demand": 1, "must_use": 0}
    ]
    result = solve_payload(payload, time_limit_seconds=5, engine="stable")
    assert result["status"] == "INFEASIBLE"
    assert result["summary"]["optimality_claimed"] is False
    assert result["groups"][0]["failure_class"] == "STOCK_GAP"
    assert result["groups"][0]["metrics"]["solve_status"] != "FEASIBLE"


def test_stable_status_does_not_relabel_timeout_as_infeasible() -> None:
    assert stable_problem_status([]) == "INPUT_ERROR"
    assert stable_problem_status([{"metrics": {"solve_status": "FEASIBLE"}}]) == "FEASIBLE"
    assert (
        stable_problem_status(
            [
                {"metrics": {"solve_status": "FEASIBLE"}},
                {"metrics": {"solve_status": "UNSOLVED"}, "failure_class": "TIMEOUT_NO_SOLUTION"},
            ]
        )
        == "PARTIAL"
    )
    assert (
        stable_problem_status(
            [{"metrics": {"solve_status": "UNSOLVED"}, "failure_class": "TIMEOUT_NO_SOLUTION"}]
        )
        == "TIMEOUT"
    )
    assert (
        stable_problem_status(
            [{"metrics": {"solve_status": "UNSOLVED"}, "failure_class": "STOCK_GAP"}]
        )
        == "INFEASIBLE"
    )
    assert (
        stable_problem_status(
            [{"metrics": {"solve_status": "UNSOLVED"}, "failure_class": "UNPROVED_NO_SOLUTION"}]
        )
        == "UNSOLVED"
    )


def _flat_group(**extra: object) -> dict:
    payload = {
        "Pipe": [
            {
                "figure_number": "P",
                "Parent_node": "P",
                "jlxh": "1",
                "cube_no": "1",
                "pipe_length": 100,
                "pipe_demand": 1,
                "Max_Weldingjoint_Number": 0,
            }
        ],
        "Stock": [
            {"stock_length": 50, "stock_demand": 1, "must_use": 1},
            {"stock_length": 100, "stock_demand": 1, "must_use": 0},
        ],
        "NestParam": {"BladeMargin": 0},
        "Min_Welding_Length": 0,
        "Weld_Interval_mm": 1,
        "Max_Joints_Cap": 10,
    }
    payload.update(extra)
    return payload


def test_prepare_defaults_enforce_must_use_off_and_keeps_explicit_on() -> None:
    explicit = _load(FIXTURES / "valid_case.json")["problem"]
    prepared = prepare_stable_payload(explicit)
    assert prepared["input"]["NestParam"]["EnforceMustUse"] == 0

    forced = deepcopy(explicit)
    forced["input"]["NestParam"]["EnforceMustUse"] = 1
    assert prepare_stable_payload(forced)["input"]["NestParam"]["EnforceMustUse"] == 1


def test_stable_must_use_is_off_by_default_and_honoured_when_set() -> None:
    payload = _flat_group()
    result = solve_payload(payload, time_limit_seconds=5, engine="stable")
    prepared = result.pop("_stable_prepared_payload")
    assert result["status"] == "FEASIBLE"
    assert verify_solution(prepared, result)["passed"] is True
    assert prepared["NestParam"]["EnforceMustUse"] == 0

    rejected = verify_solution(prepared, result, enforce_must_use=True)
    assert any(item["code"] == "MUST_USE_STOCK_NOT_USED" for item in rejected["issues"])

    forced = deepcopy(payload)
    forced["NestParam"]["EnforceMustUse"] = 1
    blocked = solve_payload(forced, time_limit_seconds=5, engine="stable")
    assert blocked["status"] != "FEASIBLE"


def test_flexible_packer_opens_the_bar_longest_first_misses() -> None:
    from collections import Counter

    from app.domain import parse_problem
    from app.engine_stable import _Pool

    payload = _flat_group()
    payload["Pipe"][0]["pipe_length"] = 33
    payload["Pipe"][0]["Max_Weldingjoint_Number"] = 6
    payload["NestParam"]["BladeMargin"] = 0
    payload["Stock"] = [
        {"stock_length": 10, "stock_demand": 2, "must_use": 0},
        {"stock_length": 14, "stock_demand": 1, "must_use": 0},
    ]
    group = parse_problem(payload).groups[0]
    # Longest-first opens both 10s with a 7 and strands a 4. Finishing the 14
    # with 7+6 first leaves 5+4 and 7+4 on the 10s.
    segments = Counter({7: 2, 6: 1, 5: 1, 4: 2})
    missed = _Pool(group)
    assert missed.pack_counter(segments, allow_optional=True, seed_must=False) is False
    packed = _Pool(group)
    assert packed.pack_flexible(segments) is True


def test_multi_stock_embed_rejects_closed_zone_endpoints() -> None:
    from app.domain import parse_problem
    from app.engine_stable import _multi_stock_patterns

    payload = _flat_group()
    payload["Pipe"][0]["pipe_length"] = 300
    payload["Pipe"][0]["Max_Weldingjoint_Number"] = 4
    payload["Pipe"][0]["Unweldable_Area"] = [[80, 90]]
    payload["Stock"] = [
        {"stock_length": 80, "stock_demand": 10, "must_use": 0},
        {"stock_length": 140, "stock_demand": 10, "must_use": 0},
    ]
    group = parse_problem(payload).groups[0]
    patterns = _multi_stock_patterns(group.pipes[0], group)
    assert (140, 80, 80) in patterns
    for parts in patterns:
        position = 0
        for part in parts[:-1]:
            position += part
            assert not 80 <= position <= 90


def test_patterns_for_keeps_stock_aligned_two_pieces() -> None:
    from app.domain import parse_problem
    from app.engine_stable import _patterns_for

    payload = _flat_group()
    payload["Pipe"][0]["pipe_length"] = 100
    payload["Pipe"][0]["Max_Weldingjoint_Number"] = 4
    payload["Stock"] = [
        {"stock_length": 60, "stock_demand": 4, "must_use": 0},
        {"stock_length": 40, "stock_demand": 4, "must_use": 0},
    ]
    group = parse_problem(payload).groups[0]
    patterns = _patterns_for(group.pipes[0], group)
    assert any(60 in parts and 40 in parts for parts in patterns)


def test_existing_strategy_order_matches_the_previous_round() -> None:
    from app.engine_stable import _strategies

    names = [name for name, _source, _function in _strategies()]
    assert names == [
        "whole-first",
        "stock-peel-long",
        "stock-peel-short",
        "largest-early-finish",
        "uniform-2",
        "uniform-3",
        "uniform-4",
        "uniform-6",
        "stock-tail",
        "complement-mix",
        "cross-complement",
        "multi-stock-embed",
        "shared-tail",
        "short-stock-split",
        "group-fallback",
        "pattern-scan",
        "adaptive-peel-long",
        "adaptive-peel-short",
        "baseline-restricted-master",
    ]
    assert "cpsat-feasibility" not in names


def test_cpsat_runs_only_after_existing_strategies_and_on_its_own_clock(monkeypatch) -> None:
    import time

    from app.domain import parse_problem
    from app.engine_stable import _CPSAT_LIMIT, solve_group_stable

    calls: list[float] = []

    def fake(group, deadline):
        calls.append(deadline - time.monotonic())
        return None

    monkeypatch.setattr("app.engine_stable_cpsat.solve_group_cpsat", fake)
    monkeypatch.setattr(
        "app.engine_stable._strategies",
        lambda: [("noop", "native", lambda group, deadline: None)],
    )
    payload = _flat_group()
    group = parse_problem(payload).groups[0]
    token = _CPSAT_LIMIT.set(1.25)
    try:
        result = solve_group_stable(group, 0.05)
    finally:
        _CPSAT_LIMIT.reset(token)
    assert len(calls) == 1
    assert 1.0 <= calls[0] <= 1.25
    assert result["failure_class"] == "UNPROVED_NO_SOLUTION"
    assert result["metrics"]["cpsat_elapsed_seconds"] < 0.5


def test_cpsat_does_not_run_when_an_existing_strategy_succeeds(monkeypatch) -> None:
    from app.engine_stable import _CPSAT_LIMIT

    calls: list[int] = []

    def fake(group, deadline):
        calls.append(1)
        return None

    monkeypatch.setattr("app.engine_stable_cpsat.solve_group_cpsat", fake)
    token = _CPSAT_LIMIT.set(5.0)
    try:
        result = _verified_stable(_flat_group(), 5)
    finally:
        _CPSAT_LIMIT.reset(token)
    assert result["groups"][0]["metrics"]["stable_strategy"] != "cpsat-feasibility"
    assert calls == []


def test_cpsat_does_not_start_when_the_chain_deadline_stops_the_list(monkeypatch) -> None:
    from app.domain import parse_problem
    from app.engine_stable import _CPSAT_LIMIT, _Timeout, solve_group_stable

    calls: list[int] = []

    def fake(group, deadline):
        calls.append(1)
        return None

    def boom(group, deadline):
        raise _Timeout

    monkeypatch.setattr("app.engine_stable_cpsat.solve_group_cpsat", fake)
    monkeypatch.setattr(
        "app.engine_stable._strategies",
        lambda: [("boom", "native", boom), ("later", "native", lambda group, deadline: None)],
    )
    group = parse_problem(_flat_group()).groups[0]
    token = _CPSAT_LIMIT.set(3.0)
    try:
        result = solve_group_stable(group, 5)
    finally:
        _CPSAT_LIMIT.reset(token)
    assert calls == []
    assert result["failure_class"] == "TIMEOUT_NO_SOLUTION"


def test_cpsat_module_has_no_case_specific_branch() -> None:
    import re

    source = (Path(__file__).resolve().parents[1] / "app" / "engine_stable_cpsat.py").read_text(
        encoding="utf-8"
    )
    assert "case_id" not in source
    assert "record_index" not in source
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-", source)
    for token in ("2649", "2830", "2502", "11000", "1550", "12550"):
        assert token not in source


def test_cpsat_plan_verifies_and_does_not_claim_optimality() -> None:
    from app.domain import parse_problem
    from app.engine_stable import prepare_stable_payload
    from app.engine_stable_cpsat import solve_group_cpsat

    payload = _flat_group()
    payload["Pipe"] = [
        {
            "figure_number": "A",
            "Parent_node": "A",
            "jlxh": "1",
            "cube_no": "1",
            "pipe_length": 100,
            "pipe_demand": 3,
            "Max_Weldingjoint_Number": 2,
            "Unweldable_Area": [[48, 52]],
        },
        {
            "figure_number": "B",
            "Parent_node": "B",
            "jlxh": "1",
            "cube_no": "1",
            "pipe_length": 70,
            "pipe_demand": 3,
            "Max_Weldingjoint_Number": 2,
        },
    ]
    payload["Stock"] = [{"stock_length": 90, "stock_demand": 8, "must_use": 0}]
    payload["NestParam"]["BladeMargin"] = 5
    prepared = prepare_stable_payload(payload)
    group = parse_problem(prepared).groups[0]
    found = solve_group_cpsat(group, __import__("time").monotonic() + 8)
    assert found is not None
    assert found["metrics"]["solve_status"] == "FEASIBLE"
    for row in found["welding_patterns"]:
        position = 0
        parts = [int(part) for part in row["parts"]]
        for part in parts[:-1]:
            position += part
            assert not 48 <= position <= 52
