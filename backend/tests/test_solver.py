from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain import parse_problem
from app.main import app
from app.solver import solve_payload
from app.verifier import verify_solution


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _production_signature(result: dict) -> list[tuple]:
    return [
        (
            group["metrics"]["used_stock_length"],
            group["metrics"]["welding_joint_quantity"],
            group["metrics"]["welding_pattern_type_quantity"],
            [
                (row["pipe_id"], tuple(row["parts"]), row["quantity"])
                for row in group["welding_patterns"]
            ],
            [
                (row["stock_length"], tuple(row["parts"]), row["quantity"])
                for row in group["cutting_patterns"]
            ],
        )
        for group in result["groups"]
    ]


def test_solver_result_passes_independent_verifier() -> None:
    payload = _load("valid_case.json")["problem"]
    result = solve_payload(payload, time_limit_seconds=10)
    report = verify_solution(payload, result)

    assert result["status"] == "TARGET_REACHED"
    assert report["passed"] is True
    assert report["issue_count"] == 0


def _integerised_production_payload() -> dict:
    """Explicit integer-mm derivative; the original decimal fixture is retained."""

    payload = deepcopy(_load("production_phi51x13.json"))
    payload["input"]["data"][0]["Pipe"][0]["pipe_length"] = "17310"
    return payload


def test_integer_production_regression_reaches_target_and_is_deterministic() -> None:
    payload = _integerised_production_payload()
    first = solve_payload(payload, time_limit_seconds=20)
    second = solve_payload(payload, time_limit_seconds=20)

    assert verify_solution(payload, first)["passed"] is True
    assert first["groups"][0]["metrics"]["target_reached"] is True
    assert first["groups"][0]["metrics"]["used_stock_length"] == 452250
    assert first["groups"][0]["metrics"]["utilization_rate"] >= 0.9925
    assert _production_signature(first) == _production_signature(second)

    group = first["groups"][0]
    distinct_splices = {tuple(row["parts"]) for row in group["welding_patterns"]}
    distinct_cuts = {
        (row["stock_length"], tuple(sorted(row["parts"])))
        for row in group["cutting_patterns"]
    }
    assert group["metrics"]["welding_pattern_type_quantity"] == len(distinct_splices)
    assert group["metrics"]["cutting_pattern_type_quantity"] == len(distinct_cuts)


def _normalizations(result: dict) -> dict[str, dict]:
    return {
        record["path"]: record
        for record in result["groups"][0]["input_normalizations"]
    }


def test_original_decimal_production_fixture_is_accepted_and_audited() -> None:
    payload = _load("production_phi51x13.json")

    result = solve_payload(payload, time_limit_seconds=20)
    report = verify_solution(payload, result)

    path = "input.data[0].Pipe[0].pipe_length"
    assert _normalizations(result)[path] == {
        "path": path,
        "original": "17310.2",
        "normalized": 17311,
        "rule": "CEILING_TO_INTEGER_MM",
    }
    assert result["summary"]["normalized_length_field_quantity"] == 1
    assert all(row["pipe_length"] == 17311 for row in result["groups"][0]["welding_patterns"])
    assert report["passed"] is True


def _set_pipe_length(payload: dict) -> None:
    payload["input"]["data"][0]["Pipe"][0]["pipe_length"] = 1000.5


def _set_stock_length(payload: dict) -> None:
    payload["input"]["data"][0]["Stock"][0]["stock_length"] = 1010.5


def _set_forbidden_coordinate(payload: dict) -> None:
    payload["input"]["data"][0]["Pipe"][0]["Unweldable_Area"][0][0] = 0.5


def _set_blade_margin(payload: dict) -> None:
    payload["input"]["NestParam"]["BladeMargin"] = 10.5


def _set_min_weld_distance(payload: dict) -> None:
    payload["input"]["Min_Welding_Length"] = 500.5


def _set_min_remnant(payload: dict) -> None:
    payload["input"]["Min_Reusable_Remnant_Length"] = 100.5


@pytest.mark.parametrize(
    ("mutate", "field_path", "normalized", "rule"),
    [
        (_set_pipe_length, "input.data[0].Pipe[0].pipe_length", 1001, "CEILING_TO_INTEGER_MM"),
        (_set_stock_length, "input.data[0].Stock[0].stock_length", 1011, "CEILING_TO_INTEGER_MM"),
        (
            _set_forbidden_coordinate,
            "input.data[0].Pipe[0].Unweldable_Area[0][0]",
            0,
            "FLOOR_TO_INTEGER_MM",
        ),
        (_set_blade_margin, "input.NestParam.BladeMargin", 11, "CEILING_TO_INTEGER_MM"),
        (_set_min_weld_distance, "input.Min_Welding_Length", 501, "CEILING_TO_INTEGER_MM"),
        (
            _set_min_remnant,
            "input.Min_Reusable_Remnant_Length",
            101,
            "CEILING_TO_INTEGER_MM",
        ),
    ],
)
def test_standardisation_accepts_and_audits_fractional_millimetre_input(
    mutate, field_path: str, normalized: int, rule: str
) -> None:
    payload = _load("valid_case.json")["problem"]
    mutate(payload)

    result = solve_payload(payload, time_limit_seconds=10)
    report = verify_solution(payload, result)

    record = _normalizations(result)[field_path]
    assert record["normalized"] == normalized
    assert record["rule"] == rule
    assert result["summary"]["normalized_length_field_quantity"] == 1
    assert report["passed"] is True


def test_sync_api_accepts_non_integer_input_and_returns_audit_record() -> None:
    payload = _load("valid_case.json")["problem"]
    _set_pipe_length(payload)

    response = TestClient(app).post("/api/v1/solve", json=payload)

    assert response.status_code == 200
    result = response.json()
    path = "input.data[0].Pipe[0].pipe_length"
    assert _normalizations(result)[path]["normalized"] == 1001
    assert result["verification"]["passed"] is True


def test_point_zero_one_boundaries_integer_text_and_stock_merge() -> None:
    payload = _load("valid_case.json")["problem"]
    root = payload["input"]
    group = root["data"][0]
    root["NestParam"]["BladeMargin"] = "10.01"
    root["Min_Welding_Length"] = "500.01"
    root["Min_Reusable_Remnant_Length"] = "0.01"
    pipe = deepcopy(group["Pipe"][0])
    pipe.update(
        {
            "pipe_length": "1000.01",
            "pipe_demand": 1,
            "Unweldable_Area": [
                ["0.01", "100.01"],
                ["900.01", "1000.01"],
            ],
        }
    )
    group["Pipe"] = [pipe]
    group["Target_Util_Rate"] = 90
    group["Stock"] = [
        {"stock_length": "1012.01", "stock_demand": 1, "must_use": 0},
        {"stock_length": "1013", "stock_demand": 2, "must_use": 0},
        {"stock_length": "2000.0", "stock_demand": 1, "must_use": 0},
    ]

    normalized_problem = parse_problem(payload)
    result = solve_payload(payload, time_limit_seconds=10)
    report = verify_solution(payload, result)

    assert [(stock.length, stock.quantity) for stock in normalized_problem.groups[0].stocks] == [
        (1013, 3),
        (2000, 1),
    ]
    records = result["groups"][0]["input_normalizations"]
    assert [(row["path"], row["rule"]) for row in records] == sorted(
        (row["path"], row["rule"]) for row in records
    )
    assert result["summary"]["normalized_length_field_quantity"] == 9
    record_map = _normalizations(result)
    expected = {
        "input.NestParam.BladeMargin": ("10.01", 11, "CEILING_TO_INTEGER_MM"),
        "input.Min_Welding_Length": ("500.01", 501, "CEILING_TO_INTEGER_MM"),
        "input.Min_Reusable_Remnant_Length": ("0.01", 1, "CEILING_TO_INTEGER_MM"),
        "input.data[0].Pipe[0].pipe_length": ("1000.01", 1001, "CEILING_TO_INTEGER_MM"),
        "input.data[0].Pipe[0].Unweldable_Area[0][0]": ("0.01", 0, "FLOOR_TO_INTEGER_MM"),
        "input.data[0].Pipe[0].Unweldable_Area[0][1]": ("100.01", 101, "CEILING_TO_INTEGER_MM"),
        "input.data[0].Pipe[0].Unweldable_Area[1][0]": ("900.01", 900, "FLOOR_TO_INTEGER_MM"),
        "input.data[0].Pipe[0].Unweldable_Area[1][1]": ("1000.01", 1001, "CEILING_TO_INTEGER_MM"),
        "input.data[0].Stock[0].stock_length": ("1012.01", 1013, "CEILING_TO_INTEGER_MM"),
    }
    assert set(record_map) == set(expected)
    for path, (original, normalized, rule) in expected.items():
        assert record_map[path] == {
            "path": path,
            "original": original,
            "normalized": normalized,
            "rule": rule,
        }
    assert "input.data[0].Stock[1].stock_length" not in record_map
    assert "input.data[0].Stock[2].stock_length" not in record_map
    assert report["passed"] is True


@pytest.mark.parametrize(("raw", "expected"), [("是", 2), ("否", 0), (True, 2), (False, 0)])
def test_is_must_chinese_and_boolean_forms(raw, expected: int) -> None:
    payload = _load("valid_case.json")["problem"]
    stock = payload["input"]["data"][0]["Stock"][0]
    stock.pop("must_use", None)
    stock["Is_Must"] = raw

    parsed = parse_problem(payload)

    assert parsed.groups[0].stocks[0].must_use_quantity == expected


def test_solver_public_schema_uses_json_integers_for_all_mm_outputs() -> None:
    result = solve_payload(_integerised_production_payload(), time_limit_seconds=20)
    group = result["groups"][0]

    values: list[object] = []
    for row in group["welding_patterns"]:
        values.extend([row["pipe_length"], *row["parts"], *row["weld_positions"]])
    for row in group["cutting_patterns"]:
        values.extend(
            [
                row["stock_length"],
                *row["parts"],
                row["kerf_per_cut"],
                row["kerf_loss_per_stock"],
                row["remainder_per_stock"],
                row["used_length_per_stock"],
            ]
        )
    values.extend(row["stock_length"] for row in group["unused_materials"])
    for row in group["generated_remnants"]:
        values.extend([row["length"], row["source_stock_length"]])
    for key in (
        "demand_length",
        "available_stock_length",
        "used_stock_length",
        "kerf_loss",
        "remainder_length",
    ):
        values.append(group["metrics"][key])
    for key in ("demand_length", "used_stock_length", "kerf_loss", "remainder_length"):
        if key in result["summary"]:
            values.append(result["summary"][key])

    assert values
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in values)


def test_length_shortfall_group_is_rejected_without_solver() -> None:
    """A group whose total stock length is below total demand is provably
    infeasible; the arithmetic prefilter must reject it (no solver, no crash)."""

    payload = _load("valid_case.json")["problem"]
    # Demand length is 2*1000 + 1*1200 = 3200 mm.  Leave only a single short bar
    # so total supply (1010 mm) is far below demand.
    payload["input"]["data"][0]["Stock"] = [
        {"stock_length": 1010, "stock_demand": 1, "must_use": 0}
    ]

    result = solve_payload(payload, time_limit_seconds=10)
    group = result["groups"][0]
    diagnosis = group["shortage_diagnosis"]

    assert result["status"] == "INFEASIBLE"
    assert result["summary"]["unsolved_group_count"] == 1
    assert group["metrics"]["solve_status"] == "UNSOLVED"
    assert diagnosis["shortage_type"] == "LENGTH_SHORTFALL"
    assert diagnosis["supply_ratio"] is not None and diagnosis["supply_ratio"] < 1.0
    assert diagnosis["length_deficit"] > 0
    assert diagnosis["recommendations"][0]["add_quantity"] >= 1
    # The result must still pass the independent verifier (unsolved group path).
    assert verify_solution(payload, result)["passed"] is True

