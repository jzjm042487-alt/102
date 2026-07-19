"""Tests for the global joint cut+weld engine (engine=global)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain import parse_problem
from app.global_candidates import build_cut_pool, build_weld_pool
from app.solver import solve_payload
from app.verifier import verify_solution
from app import solver_global

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _first_group(payload: dict):
    return parse_problem(payload).groups[0]


def test_time_limit_is_required_positive_input() -> None:
    payload = _load("valid_case.json")["problem"]
    group = _first_group(payload)
    assert solver_global.solve_group(group, time_limit=0) is None
    assert solver_global.solve_group(group, time_limit=-1) is None


def test_candidate_pools_are_nonempty_and_legal() -> None:
    payload = _load("valid_case.json")["problem"]
    group = _first_group(payload)
    welds, alphabet = build_weld_pool(group)
    assert welds
    assert alphabet
    cuts = build_cut_pool(group, alphabet)
    assert cuts
    for column in cuts:
        assert column["stock"] >= column["used"]
        assert sum(column["counts"].values()) >= 1


def test_global_solve_group_passes_verifier() -> None:
    payload = _load("valid_case.json")["problem"]
    group = _first_group(payload)
    result = solver_global.solve_group(group, time_limit=20.0)
    assert result is not None, "global engine should solve the basic fixture"
    assert result["metrics"]["utilization_rate"] + 1e-12 >= solver_global.UTIL_FLOOR

    full = solve_payload(payload, time_limit_seconds=20, engine="baseline")
    full["groups"][0] = result
    metrics = result["metrics"]
    full["summary"].update(
        {
            "demand_length": metrics["demand_length"],
            "used_stock_length": metrics["used_stock_length"],
            "welding_joint_quantity": metrics["welding_joint_quantity"],
        }
    )
    report = verify_solution(payload, full)
    assert report["passed"] is True, report


def test_solve_payload_engine_global() -> None:
    payload = _load("valid_case.json")["problem"]
    result = solve_payload(payload, time_limit_seconds=20, engine="global")
    assert result["groups"]
    assert result["groups"][0]["metrics"]["solver_backend"] == "SCIP-GLOBAL-V1"
    report = verify_solution(payload, result)
    assert report["passed"] is True, report
    assert (
        float(result["groups"][0]["metrics"]["utilization_rate"]) + 1e-12
        >= solver_global.UTIL_FLOOR
    )


def test_util_floor_helper() -> None:
    assert solver_global._max_used_for_util_floor(950, 0.95) == 1000
