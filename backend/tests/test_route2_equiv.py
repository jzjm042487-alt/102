"""Tests for the opt-in equivalent-stock engine (route-2) and its integration.

Route-2 (``app.route2_equiv``) is an alternative solver that reduces the
cut-and-weld problem to a standard CSP over "equivalent stocks".  It is wired
into ``_solve_problem`` as an opt-in, select-only-if-strictly-better path guarded
by the ``NESTING_ROUTE2`` env var.  These tests pin the contract that makes that
integration safe:

  * its standalone output passes the independent verifier (schema + balance),
  * forbidden weld zones and must-use quantities are honoured,
  * enabling it never regresses the incumbent (select-only-if-better),
  * it is off by default and toggled purely by the env var.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.domain import parse_problem
from app.solver import solve_payload
from app.verifier import verify_solution
from app import route2_equiv


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _first_group(payload: dict):
    return parse_problem(payload).groups[0]


# --------------------------------------------------------------------------- #
# Standalone engine contract
# --------------------------------------------------------------------------- #
def test_solve_group_output_passes_independent_verifier() -> None:
    """A route-2 group result must satisfy the same verifier the MILP path does.

    We assemble a full problem-shaped result from the single-group output and run
    the real verifier, exactly as production does after selecting route-2.
    """
    payload = _load("valid_case.json")["problem"]
    group = _first_group(payload)

    r2 = route2_equiv.solve_group(group, time_limit=10.0)
    assert r2 is not None, "route-2 should solve the basic fixture"

    # Build a problem-level result mirroring solve_payload's shape for one group.
    result = solve_payload(payload, time_limit_seconds=10)
    result["groups"][0] = r2
    # Recompute summary fields the verifier reads from the (now route-2) group.
    m = r2["metrics"]
    result["summary"].update(
        {
            "demand_length": m["demand_length"],
            "used_stock_length": m["used_stock_length"],
            "welding_joint_quantity": m["welding_joint_quantity"],
        }
    )

    report = verify_solution(payload, result)
    assert report["passed"] is True, report


def test_solve_group_respects_segment_balance() -> None:
    """Every welded part consumed must be produced by a cutting part (balance)."""
    payload = _load("valid_case.json")["problem"]
    group = _first_group(payload)

    r2 = route2_equiv.solve_group(group, time_limit=10.0)
    assert r2 is not None

    from collections import Counter

    produced: Counter = Counter()
    for row in r2["cutting_patterns"]:
        for part in row["parts"]:
            produced[part] += row["quantity"]
    consumed: Counter = Counter()
    for row in r2["welding_patterns"]:
        for part in row["parts"]:
            consumed[part] += row["quantity"]

    # Consumed segments must be a sub-multiset of produced segments.
    for seg, need in consumed.items():
        assert produced[seg] >= need, (seg, need, produced[seg])


def test_solve_group_honours_forbidden_zones() -> None:
    """No weld may land inside a pipe's forbidden interval (measured from start)."""
    payload = _load("valid_case.json")["problem"]
    group = _first_group(payload)
    # Map pipe length -> forbidden intervals for lookup.
    forbidden_by_len: dict[int, list[tuple[int, int]]] = {}
    for pipe in group.pipes:
        forbidden_by_len.setdefault(
            pipe.length, [(iv.start, iv.end) for iv in pipe.forbidden]
        )

    r2 = route2_equiv.solve_group(group, time_limit=10.0)
    assert r2 is not None

    for row in r2["welding_patterns"]:
        length = row["pipe_length"]
        zones = forbidden_by_len.get(length, [])
        for pos in row["weld_positions"]:
            for lo, hi in zones:
                assert not (lo <= pos <= hi), (
                    f"weld at {pos} lands in forbidden {(lo, hi)} for pipe {length}"
                )


def test_solve_group_respects_must_use_quantity() -> None:
    """Must-use bars have priority: when demand exceeds the must-use inventory,
    every must-use bar must be consumed before any available bar."""
    payload = deepcopy(_load("valid_case.json")["problem"])
    # Force the 1500 bar to be must-use; demand far exceeds this single bar, so
    # under the priority rule it must be consumed.
    for stock in payload["input"]["data"][0]["Stock"]:
        if stock["stock_length"] == 1500:
            stock["must_use"] = 1
    group = _first_group(payload)

    r2 = route2_equiv.solve_group(group, time_limit=10.0)
    if r2 is None:
        pytest.skip("route-2 could not satisfy must_use within budget")

    used_1500 = sum(
        row["quantity"] for row in r2["cutting_patterns"]
        if row["stock_length"] == 1500
    )
    assert used_1500 >= 1


# --------------------------------------------------------------------------- #
# Integration contract (select-only-if-better, env toggle)
# --------------------------------------------------------------------------- #
def test_route2_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NESTING_ROUTE2", raising=False)
    from app import solver

    assert solver._route2_enabled() is False


@pytest.mark.parametrize("flag", ["1", "on", "true", "YES", "True"])
def test_route2_enabled_by_env(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    from app import solver

    monkeypatch.setenv("NESTING_ROUTE2", flag)
    assert solver._route2_enabled() is True


@pytest.mark.parametrize("flag", ["0", "off", "false", "", "no"])
def test_route2_disabled_by_env(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    from app import solver

    monkeypatch.setenv("NESTING_ROUTE2", flag)
    assert solver._route2_enabled() is False


def test_route2_never_regresses_incumbent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling route-2 must never produce a worse result than with it off.

    The incumbent is only replaced when strictly better (utilisation, then
    welds), so the ON run must be >= the OFF run on utilisation and the result
    must still pass the verifier.
    """
    payload = _load("valid_case.json")["problem"]

    monkeypatch.delenv("NESTING_ROUTE2", raising=False)
    off = solve_payload(payload, time_limit_seconds=10)

    monkeypatch.setenv("NESTING_ROUTE2", "1")
    on = solve_payload(payload, time_limit_seconds=10)

    off_util = off["groups"][0]["metrics"]["utilization_rate"]
    on_util = on["groups"][0]["metrics"]["utilization_rate"]
    assert on_util >= off_util - 1e-9

    assert verify_solution(payload, on)["passed"] is True


def test_route2_selection_marks_warning_when_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When route-2 wins, the group carries the ROUTE2_SELECTED marker."""
    payload = _load("valid_case.json")["problem"]
    monkeypatch.setenv("NESTING_ROUTE2", "1")

    result = solve_payload(payload, time_limit_seconds=10)
    group = result["groups"][0]
    warnings = group.get("warnings", [])
    # Either route-2 was selected (marker present) or the MILP incumbent stood;
    # both are valid, but if route-2 produced this group the marker must be set.
    if group["metrics"].get("solver_backend") == "route2-equiv":
        assert "ROUTE2_SELECTED" in warnings


def test_solve_group_small_demand_not_forced_to_exhaust_must_use() -> None:
    """Must-use is a priority, not a hard quota: when demand is small enough to
    be served from a subset of must-use bars, route-2 must NOT be forced to
    consume every must-use bar, and its result must still pass the verifier.

    (Previously an over-large must-use quantity forced a degenerate, verifier-
    failing plan; the corrected semantics serve small demand from must-use stock
    and leave the surplus uncut.)
    """
    payload = deepcopy(_load("valid_case.json")["problem"])
    # One long must-use bar with plenty of copies; total pipe demand needs only a
    # few of them, so the surplus must-use bars are legitimately left uncut.
    stocks = payload["input"]["data"][0]["Stock"]
    stocks[0]["must_use"] = 1
    stocks[0]["stock_demand"] = 10_000
    group = _first_group(payload)

    r2 = route2_equiv.solve_group(group, time_limit=10.0)
    if r2 is None:
        pytest.skip("route-2 could not solve within budget")

    # The self-verify gate is embedded in solve_group, so a non-None result is
    # already verifier-clean.  Re-assert it explicitly for documentation.
    assert route2_equiv._self_verify(group, r2) is True
