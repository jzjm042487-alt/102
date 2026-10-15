"""Lexicographic post-pass: locks, non-regression, and the frozen pilot list."""

from __future__ import annotations

import json
from pathlib import Path

from app.engine_stable_opt import accept_candidate
from app.solver import solve_payload
from app.verifier import verify_solution

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable(limit: float = 5.0) -> tuple[dict, dict]:
    payload = _load(FIXTURES / "valid_case.json")["problem"]
    result = solve_payload(
        payload,
        time_limit_seconds=limit,
        engine="stable",
        cpsat_time_limit=0,
        ondemand_time_limit=0,
    )
    prepared = result.pop("_stable_prepared_payload")
    report = verify_solution(prepared, result)
    assert report["passed"] is True
    assert result["status"] == "FEASIBLE"
    return prepared, result


def _key(result: dict) -> tuple[int, int, int, int]:
    summary = result["summary"]
    return (
        int(summary["used_stock_length"]),
        int(summary["welding_joint_quantity"]),
        int(summary["welding_pattern_type_quantity"]),
        int(summary["cutting_pattern_type_quantity"]),
    )


def test_accept_candidate_honours_locks_and_lex_order() -> None:
    incumbent = (100, 10, 5, 3)
    used_only = {"used_stock_length": 100}
    both = {"used_stock_length": 100, "welding_joint_quantity": 10}
    less_material_more_joints = (90, 12, 1, 1)
    assert accept_candidate(less_material_more_joints, incumbent, used_only) is True
    assert accept_candidate(less_material_more_joints, incumbent, both) is False
    assert accept_candidate((110, 0, 0, 0), incumbent, used_only) is False
    assert accept_candidate(incumbent, incumbent, both) is True
    assert accept_candidate((100, 10, 5, 2), incumbent, both) is True
    assert accept_candidate((100, 10, 5, 4), incumbent, both) is False
    assert accept_candidate((100, 9, 8, 9), incumbent, both) is True
    assert accept_candidate((100, 10, 6, 1), incumbent, both) is False


def test_zero_budget_keeps_the_verified_plan() -> None:
    from app.engine_stable_opt import apply_optimization

    prepared, result = _stable()
    updated, logs = apply_optimization(prepared, result, (0.0, 0.0, 0.0))
    assert updated is result
    assert _key(updated) == _key(result)
    assert logs
    assert all(log["kept"] == "stable" for log in logs)
    assert result["summary"]["optimality_claimed"] is False
    assert result["summary"]["lexicographic_optimal"] is False


def test_infeasible_candidate_does_not_replace_the_incumbent(monkeypatch) -> None:
    from app.engine_stable_opt import apply_optimization, optimize_group

    def _extend(group, welds, cuts, deadline):
        del group, deadline
        return welds, cuts

    def _worse(group, welds, cuts, objective, seconds, locks, start_x, start_y):
        del group, welds, cuts, objective, seconds, locks
        x_values = list(start_x or [])
        y_values = list(start_y or [])
        if x_values:
            x_values[0] += 50
        return {"x": x_values, "y": y_values, "key": (0, 0, 0, 0), "status": "optimal"}

    monkeypatch.setattr("app.engine_stable_opt._extend_pool", _extend)
    monkeypatch.setattr("app.engine_stable_opt._optimize_objective", _worse)
    prepared, result = _stable()
    updated, logs = apply_optimization(prepared, result, (1.0, 0.0, 0.0))
    assert updated is result
    assert logs[0]["kept"] == "stable"
    assert logs[0]["layers"][0]["accepted"] is False
    assert str(logs[0]["layers"][0]["status"]).startswith("rejected:infeasible")
    # The same predicate protects optimize_group when the caller does not verify.
    kept, log = optimize_group(
        __import__("app.domain", fromlist=["parse_problem"]).parse_problem(prepared).groups[0],
        result["groups"][0],
        (1.0, 0.0, 0.0),
    )
    assert log["kept"] == "stable"
    assert kept["metrics"]["used_stock_length"] == result["groups"][0]["metrics"]["used_stock_length"]


def test_short_post_pass_stays_feasible_and_lex_no_worse() -> None:
    from app.engine_stable_opt import apply_optimization

    prepared, result = _stable()
    updated, logs = apply_optimization(prepared, result, (1.5, 0.8, 0.8))
    report = verify_solution(prepared, updated)
    assert report["passed"] is True
    assert _key(updated) <= _key(result)
    assert updated["summary"]["optimality_claimed"] is False
    assert updated["summary"]["lexicographic_optimal"] is False
    assert updated["summary"]["core_lexicographic_optimal"] is False
    assert updated["status"] == "FEASIBLE"
    for group in updated["groups"]:
        metrics = group["metrics"]
        assert metrics["lexicographic_optimal"] is False
        assert metrics["core_lexicographic_optimal"] is False
    assert logs and logs[0]["layers"]


def test_post_pass_is_not_on_the_feasibility_path() -> None:
    source = (ROOT / "backend/app/solver.py").read_text(encoding="utf-8")
    assert "apply_optimization" not in source
    assert "engine_stable_opt" not in source
    opt = (ROOT / "backend/app/engine_stable_opt.py").read_text(encoding="utf-8")
    assert "record_index" not in opt
    assert "case_id" not in opt


def test_pilot_split_is_the_frozen_non_blind_losses() -> None:
    import csv

    split = json.loads((ROOT / "docs/opt-pilot-split.json").read_text(encoding="utf-8"))
    blind = {
        int(index)
        for index in json.loads((ROOT / "docs/stable-round3-split.json").read_text(encoding="utf-8"))[
            "blind_record_indexes"
        ]
    }
    chosen = [int(index) for index in split["record_indexes"]]
    assert split["count"] == 50
    assert len(chosen) == 50 and len(set(chosen)) == 50
    assert not (set(chosen) & blind)
    legacy_path = ROOT / "docs/stable-round5-vs-legacy.csv"
    with legacy_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    eligible = []
    for row in rows:
        index = int(row["record_index"])
        if index in blind or row["老软件合规"] != "是" or row["词典序对照"] != "负":
            continue
        eligible.append(
            (
                int(row["用料差"]),
                int(row["焊口差"]),
                int(row["拼法差"]),
                int(row["切法差"]),
                -index,
                index,
            )
        )
    eligible.sort(reverse=True)
    expected = [item[-1] for item in eligible[:50]]
    assert chosen == expected
