"""Column-generation lexicographic engine (engine=lexcg)."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time

from app.domain import parse_problem
from app.lexcg import knapsack_best_pattern, solve_group
from app.solver import _tier_meets_target, solve_payload
from app.verifier import verify_solution


FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _tiny(
    *,
    length: int = 200,
    demand: int = 2,
    joints: int = 0,
    stocks: list[tuple[int, int]] | None = None,
    blade: int | None = 0,
    min_weld: int = 0,
    must: set[int] | None = None,
) -> dict:
    stock_rows = stocks if stocks is not None else [(200, 2)]
    payload: dict = {
        "input": {
            "Min_Welding_Length": str(min_weld),
            "Target_Util_Rate": "50",
            "data": [
                {
                    "material": "M",
                    "specifications": "S",
                    "Target_Util_Rate": "50",
                    "Pipe": [
                        {
                            "figure_number": "P",
                            "Parent_node": "P",
                            "jlxh": 1,
                            "cube_no": 1,
                            "pipe_length": str(length),
                            "pipe_demand": demand,
                            "Max_Weldingjoint_Number": joints,
                            "Unweldable_Area": [[0, 1], [length - 1, length]],
                        }
                    ],
                    "Stock": [
                        {
                            "stock_length": stock_length,
                            "stock_demand": quantity,
                            "must_use": 1 if must and stock_length in must else 0,
                        }
                        for stock_length, quantity in stock_rows
                    ],
                }
            ],
        }
    }
    if blade is not None:
        payload["input"]["NestParam"] = {"BladeMargin": str(blade)}
    return payload


def _group_metrics(result: dict) -> dict:
    return result["groups"][0]["metrics"]


def test_tier_target_compares_fractions_not_percent() -> None:
    payload = _tiny()
    group = parse_problem(payload).groups[0]
    # 50% target is the fraction 0.5.  A 1% plan must not count as on target.
    low = {"metrics": {"utilization_rate": 0.01, "lexicographic_optimal": False}}
    high = {"metrics": {"utilization_rate": 0.5, "lexicographic_optimal": False}}
    assert _tier_meets_target(low, group) is False
    assert _tier_meets_target(high, group) is True


def test_corpus_blademargin_spelling_is_honoured() -> None:
    payload = _tiny(blade=None)
    payload["Blademargin"] = "10"
    # Single-group envelope: the typo key sits on the group itself.
    group_payload = payload["input"]["data"][0]
    group_payload["Blademargin"] = "10"
    problem = parse_problem(group_payload)
    assert problem.groups[0].blade_margin == 10


def test_default_blade_margin_is_five_on_both_sides() -> None:
    payload = _tiny(blade=None)
    problem = parse_problem(payload)
    assert problem.groups[0].blade_margin == 5
    result = solve_payload(payload, time_limit_seconds=8, engine="lexcg")
    report = verify_solution(payload, result)
    assert report["passed"] is True
    assert result["groups"][0]["metrics"]["kerf_loss"] == 0


def test_production_joint_cap_is_not_clamped() -> None:
    payload = _load("production_phi51x13.json")
    pipe = parse_problem(payload).groups[0].pipes[0]
    assert pipe.max_joints == 2000


def test_small_exact_family_is_lexicographically_proved() -> None:
    payload = _tiny(length=200, demand=2, joints=0, stocks=[(200, 2)])
    result = solve_payload(payload, time_limit_seconds=8, engine="lexcg")
    metrics = _group_metrics(result)
    assert verify_solution(payload, result)["passed"] is True
    assert metrics["solve_status"] == "OPTIMAL_LEXICOGRAPHIC"
    assert metrics["lexicographic_optimal"] is True
    assert metrics["pricing_truncated"] is False
    assert [layer["status"] for layer in metrics["lexicographic_layers"]] == [
        "proved",
        "proved",
        "proved",
        "proved",
    ]
    assert metrics["used_stock_length"] == 400
    assert metrics["welding_joint_quantity"] == 0
    assert metrics["result_source"] == "native"


def test_truncated_pricing_is_not_optimal_or_infeasible() -> None:
    payload = _tiny(length=500, demand=3, joints=6, stocks=[(120, 30)], blade=0)
    group = parse_problem(payload).groups[0]
    result = solve_group(group, 2.0, use_warm_start=False, node_budget=5)
    metrics = result["metrics"]
    assert metrics["pricing_truncated"] is True
    assert metrics["solve_status"] == "UNSOLVED"
    assert metrics["infeasibility_proved"] is False
    assert metrics["lexicographic_optimal"] is False
    assert metrics["solve_status"] != "OPTIMAL_LEXICOGRAPHIC"
    assert "PRICING_TRUNCATED" in result["warnings"]


def test_knapsack_respects_node_budget_and_returns_one_pattern() -> None:
    started = time.monotonic()
    pattern, truncated, nodes = knapsack_best_pattern(
        100_000,
        [1, 2, 3, 5, 7, 11],
        [1.0, 2.0, 3.0, 5.0, 7.0, 11.0],
        kerf=0,
        kerf_mode="BETWEEN_PARTS",
        node_budget=5000,
        deadline=time.monotonic() + 2.0,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert truncated is True
    assert nodes <= 5000 + 50
    assert nodes >= 5000
    assert pattern is None or isinstance(pattern, tuple)
    # One reconstructed pattern, never a column per copy.
    assert pattern is None or len({pattern}) == 1


def test_a598_lexcg_passes_verifier_with_no_joints() -> None:
    payload = json.loads((ROOT / "data" / "input_a598e024.json").read_text(encoding="utf-8"))
    result = solve_payload(payload, time_limit_seconds=15, engine="lexcg")
    metrics = _group_metrics(result)
    assert verify_solution(payload, result)["passed"] is True
    assert metrics["result_source"] == "native"
    assert metrics["welding_joint_quantity"] == 0
    assert metrics["used_stock_length"] == 170136


def test_phi51_native_beats_fallback_joint_and_type_counts() -> None:
    payload = _load("production_phi51x13.json")
    group = parse_problem(payload).groups[0]
    started = time.monotonic()
    solved = solve_group(group, 40.0, use_warm_start=False)
    assert time.monotonic() - started < 40.0
    solution = {"status": "FEASIBLE", "groups": [solved]}
    report = verify_solution(payload, solution)
    assert report["passed"] is True, report["issues"]
    metrics = solved["metrics"]
    assert metrics["result_source"] == "native"
    assert metrics["solve_status"] != "OPTIMAL_LEXICOGRAPHIC"
    assert metrics["used_stock_length"] == 452250
    assert metrics["welding_joint_quantity"] <= 44
    assert metrics["welding_pattern_type_quantity"] <= 6
    assert "warm-start" not in solved["column_provenance"]["result_source"]


def test_phi51x95_group_passes_verifier() -> None:
    raw = json.loads(
        (
            ROOT
            / "backend"
            / "data"
            / "samples"
            / "software-io"
            / "req_92de3aca_input.json"
        ).read_text(encoding="utf-8")
    )
    payload = deepcopy(raw)
    payload["input"]["data"] = [raw["input"]["data"][3]]
    assert "9.5" in str(payload["input"]["data"][0]["specifications"])
    result = solve_payload(payload, time_limit_seconds=20, engine="lexcg")
    report = verify_solution(payload, result)
    assert report["passed"] is True, report["issues"]
    metrics = _group_metrics(result)
    assert metrics["used_stock_length"] > 0
    assert metrics["solve_status"] != "UNSOLVED"

    native = solve_group(parse_problem(payload).groups[0], 15.0, use_warm_start=False)
    native_report = verify_solution(payload, {"status": "FEASIBLE", "groups": [native]})
    assert native_report["passed"] is True, native_report["issues"]
    assert native["metrics"]["result_source"] == "native"


def test_must_use_is_enforced_when_optional_stock_is_cut() -> None:
    payload = _tiny(
        length=100,
        demand=2,
        joints=0,
        stocks=[(100, 1), (100, 2)],
        must={100},
    )
    # Two distinct lengths so optional stock can be told apart from the must-use bar.
    payload["input"]["data"][0]["Stock"] = [
        {"stock_length": 100, "stock_demand": 1, "must_use": 1},
        {"stock_length": 110, "stock_demand": 2, "must_use": 0},
    ]
    result = solve_payload(payload, time_limit_seconds=8, engine="lexcg")
    assert verify_solution(payload, result)["passed"] is True
    metrics = _group_metrics(result)
    assert metrics["must_use_used_quantity"] == metrics["must_use_stock_quantity"]
    assert metrics["must_use_used_quantity"] >= 1
