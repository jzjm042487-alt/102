from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.verifier import verify_solution


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _valid_case() -> tuple[dict, dict]:
    fixture = _fixture("valid_case.json")
    return fixture["problem"], fixture["solution"]


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_valid_production_schema_recomputes_every_metric() -> None:
    problem, solution = _valid_case()

    report = verify_solution(problem, solution)

    assert report["passed"] is True
    assert report["issue_count"] == 0
    metrics = report["recomputed_metrics"]
    assert metrics["demand_length"] == 3200
    assert metrics["used_stock_length"] == 3320
    assert metrics["utilization_rate"] == 3200 / 3320
    assert metrics["welding_joint_quantity"] == 4
    assert metrics["welding_pattern_type_quantity"] == 2
    assert metrics["cutting_pattern_type_quantity"] == 2
    assert metrics["kerf_loss"] == 40
    assert metrics["remainder_length"] == 80
    assert metrics["unused_stock_length"] == 1500
    assert metrics["segment_piece_quantity"] == 7


def test_supplied_historical_shape_is_supported() -> None:
    legacy = _fixture("legacy_case.json")

    report = verify_solution(legacy["OriginalProblem"], legacy)

    assert report["passed"] is True
    assert report["issue_count"] == 0
    assert report["recomputed_metrics"]["utilization_rate"] == 1000 / 1010
    assert report["recomputed_metrics"]["welding_joint_quantity"] == 1


def test_pipe_total_and_demand_are_independently_checked() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    broken["groups"][0]["welding_patterns"][0]["parts"] = [401, 600]
    broken["groups"][0]["welding_patterns"][0]["quantity"] = 1

    report = verify_solution(problem, broken)

    assert report["passed"] is False
    assert "PIPE_LENGTH_MISMATCH" in _codes(report)
    assert "PIPE_PATTERN_QUANTITY_MISMATCH" in _codes(report)


def test_forbidden_area_is_closed_at_both_boundaries() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    weld = broken["groups"][0]["welding_patterns"][0]
    weld["parts"] = [100, 900]
    weld["weld_positions"] = [100]
    cut = broken["groups"][0]["cutting_patterns"][0]
    cut["parts"] = [100, 900]

    report = verify_solution(problem, broken)

    assert "WELD_IN_UNWELDABLE_AREA" in _codes(report)


def test_adjacent_weld_distance_uses_internal_segment_length() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    # Same multiset and same total length; only the ordered pipe pattern changes.
    # Welds become 300 and 700, so their distance is 400 (< 500).
    weld = broken["groups"][0]["welding_patterns"][1]
    weld["parts"] = [300, 400, 500]
    weld["weld_positions"] = [300, 700]

    report = verify_solution(problem, broken)

    assert "MIN_WELD_DISTANCE_VIOLATION" in _codes(report)


def test_weld_positions_and_joint_count_cannot_be_faked() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    weld = broken["groups"][0]["welding_patterns"][1]
    weld["weld_positions"] = [301, 801]
    weld["joint_count"] = 1

    report = verify_solution(problem, broken)

    assert "WELD_POSITION_MISMATCH" in _codes(report)
    assert "JOINT_COUNT_MISMATCH" in _codes(report)


def test_blade_margin_and_stock_capacity_are_checked_from_problem() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    cut = broken["groups"][0]["cutting_patterns"][0]
    cut["parts"] = [405, 600]  # parts fit exactly, but 10 mm kerf does not
    cut["kerf_per_cut"] = 5  # solution cannot weaken the input constraint

    report = verify_solution(problem, broken)

    assert "BLADE_MARGIN_MISMATCH" in _codes(report)
    assert "CUT_CAPACITY_EXCEEDED" in _codes(report)


def test_stock_quantity_and_unused_whole_stock_reconcile() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    broken["groups"][0]["cutting_patterns"][0]["quantity"] = 3

    report = verify_solution(problem, broken)

    assert "STOCK_QUANTITY_EXCEEDED" in _codes(report)
    assert "UNUSED_STOCK_MISMATCH" in _codes(report)


def test_cut_and_weld_segment_multisets_must_balance() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    broken["groups"][0]["cutting_patterns"][0]["parts"] = [399, 601]

    report = verify_solution(problem, broken)

    assert "SEGMENT_BALANCE_MISMATCH" in _codes(report)


def test_reported_utilization_weld_count_and_pattern_counts_are_recomputed() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    metrics = broken["groups"][0]["metrics"]
    metrics["utilization_rate"] = 0.9999
    metrics["welding_joint_quantity"] = 3
    metrics["welding_pattern_type_quantity"] = 1
    metrics["cutting_pattern_type_quantity"] = 1
    # Keep summary consistent with the bad values: group verification must still fail.
    broken["summary"].update(metrics)

    report = verify_solution(problem, broken)

    codes = _codes(report)
    assert "UTILIZATION_METRIC_MISMATCH" in codes
    assert "WELD_COUNT_METRIC_MISMATCH" in codes
    assert "WELD_PATTERN_COUNT_METRIC_MISMATCH" in codes
    assert "CUT_PATTERN_COUNT_METRIC_MISMATCH" in codes


def test_trim_kerf_remainder_and_generated_remnants_reconcile() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    cut = broken["groups"][0]["cutting_patterns"][1]
    cut["kerf_loss_per_stock"] = 10
    cut["remainder_per_stock"] = 90
    cut["used_length_per_stock"] = 1210
    broken["groups"][0]["generated_remnants"][0]["length"] = 90

    report = verify_solution(problem, broken)

    codes = _codes(report)
    assert "KERF_LOSS_MISMATCH" in codes
    assert "REMAINDER_MISMATCH" in codes
    assert "USED_LENGTH_MISMATCH" in codes
    assert "REMNANT_DETAIL_MISMATCH" in codes


def test_missing_material_group_fails_closed() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    broken["groups"] = []

    report = verify_solution(problem, broken)

    assert report["passed"] is False
    assert "MISSING_SOLUTION_GROUP" in _codes(report)


def test_invalid_numeric_data_returns_issues_instead_of_crashing() -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    broken["groups"][0]["cutting_patterns"][0]["quantity"] = "not-a-number"

    report = verify_solution(problem, broken)

    assert report["passed"] is False
    assert "INVALID_NUMBER" in _codes(report)
    assert all(set(issue) == {"code", "message", "path", "severity"} for issue in report["issues"])


@pytest.mark.parametrize(
    ("mutate", "expected_path"),
    [
        (
            lambda group: group["welding_patterns"][0]["parts"].__setitem__(0, 400.5),
            ".welding_patterns[0].parts[0]",
        ),
        (
            lambda group: group["welding_patterns"][0]["weld_positions"].__setitem__(0, 400.5),
            ".welding_patterns[0].weld_positions[0]",
        ),
        (
            lambda group: group["welding_patterns"][0].__setitem__("pipe_length", 1000.5),
            ".welding_patterns[0].pipe_length",
        ),
        (
            lambda group: group["cutting_patterns"][0].__setitem__("stock_length", 1010.5),
            ".cutting_patterns[0].stock_length",
        ),
        (
            lambda group: group["cutting_patterns"][0]["parts"].__setitem__(0, 400.5),
            ".cutting_patterns[0].parts[0]",
        ),
        (
            lambda group: group["cutting_patterns"][0].__setitem__("kerf_per_cut", 10.5),
            ".cutting_patterns[0].kerf_per_cut",
        ),
        (
            lambda group: group["cutting_patterns"][1].__setitem__("remainder_per_stock", 80.5),
            ".cutting_patterns[1].remainder_per_stock",
        ),
        (
            lambda group: group["unused_materials"][0].__setitem__("stock_length", 1500.5),
            ".unused_materials[0].stock_length",
        ),
        (
            lambda group: group["generated_remnants"][0].__setitem__("length", 80.5),
            ".generated_remnants[0].length",
        ),
        (
            lambda group: group["generated_remnants"][0].__setitem__("source_stock_length", 1300.5),
            ".generated_remnants[0].source_stock_length",
        ),
        (
            lambda group: group["metrics"].__setitem__("demand_length", 3200.5),
            ".metrics.demand_length",
        ),
        (
            lambda group: group["cutting_patterns"][0].__setitem__("cut_positions", [400.5]),
            ".cutting_patterns[0].cut_positions[0]",
        ),
    ],
)
def test_every_length_and_position_output_must_be_integer_millimetres(
    mutate, expected_path: str
) -> None:
    problem, solution = _valid_case()
    broken = deepcopy(solution)
    mutate(broken["groups"][0])

    report = verify_solution(problem, broken)

    matching = [
        issue
        for issue in report["issues"]
        if issue["code"] == "NON_INTEGER_MILLIMETRE_OUTPUT"
        and issue["path"].endswith(expected_path)
    ]
    assert matching, report
    assert report["passed"] is False


def test_duplicate_rows_do_not_inflate_distinct_counts_and_report_is_deterministic() -> None:
    problem, solution = _valid_case()
    duplicated = deepcopy(solution)
    group = duplicated["groups"][0]

    weld = group["welding_patterns"][0]
    weld["quantity"] = 1
    group["welding_patterns"].insert(1, deepcopy(weld))
    cut = group["cutting_patterns"][0]
    cut["quantity"] = 1
    group["cutting_patterns"].insert(1, deepcopy(cut))

    first = verify_solution(problem, duplicated)
    second = verify_solution(problem, deepcopy(duplicated))

    assert first == second
    assert first["passed"] is True
    assert first["recomputed_metrics"]["welding_pattern_type_quantity"] == 2
    assert first["recomputed_metrics"]["cutting_pattern_type_quantity"] == 2


def test_cutting_pattern_is_unordered_but_splice_pattern_is_ordered() -> None:
    problem, solution = _valid_case()
    isomorphic_cuts = deepcopy(solution)
    cut_group = isomorphic_cuts["groups"][0]
    first_cut = cut_group["cutting_patterns"][0]
    first_cut["quantity"] = 1
    reversed_cut = deepcopy(first_cut)
    reversed_cut["parts"] = list(reversed(reversed_cut["parts"]))
    cut_group["cutting_patterns"].insert(1, reversed_cut)

    cut_report = verify_solution(problem, isomorphic_cuts)

    assert cut_report["passed"] is True
    assert cut_report["recomputed_metrics"]["cutting_pattern_type_quantity"] == 2

    ordered_splices = deepcopy(solution)
    weld_group = ordered_splices["groups"][0]
    first_weld = weld_group["welding_patterns"][0]
    first_weld["quantity"] = 1
    reversed_weld = deepcopy(first_weld)
    reversed_weld["parts"] = list(reversed(reversed_weld["parts"]))
    reversed_weld["weld_positions"] = [reversed_weld["parts"][0]]
    weld_group["welding_patterns"].insert(1, reversed_weld)
    weld_group["metrics"]["welding_pattern_type_quantity"] = 3
    ordered_splices["summary"]["welding_pattern_type_quantity"] = 3

    weld_report = verify_solution(problem, ordered_splices)

    assert weld_report["passed"] is True
    assert weld_report["recomputed_metrics"]["welding_pattern_type_quantity"] == 3


def test_identical_ordered_splice_tuple_is_global_across_pipe_ids() -> None:
    problem, solution = _valid_case()
    problem = deepcopy(problem)
    solution = deepcopy(solution)
    problem_group = problem["input"]["data"][0]
    result_group = solution["groups"][0]

    second_pipe = problem_group["Pipe"][1]
    second_pipe["pipe_length"] = 1000
    second_pipe["Unweldable_Area"] = [[0, 100], [900, 1000]]
    problem_group["Stock"] = [
        {"stock_length": 1010, "stock_demand": 3, "must_use": 0}
    ]

    result_group["welding_patterns"] = [
        {
            "pipe_id": "P1|1|2",
            "pipe_length": 1000,
            "parts": [400, 600],
            "weld_positions": [400],
            "quantity": 2,
            "joint_count": 1,
        },
        {
            "pipe_id": "P2|2|3",
            "pipe_length": 1000,
            "parts": [400, 600],
            "weld_positions": [400],
            "quantity": 1,
            "joint_count": 1,
        },
    ]
    result_group["cutting_patterns"] = [
        {
            "stock_length": 1010,
            "parts": [400, 600],
            "quantity": 3,
            "kerf_per_cut": 10,
            "kerf_loss_per_stock": 10,
            "remainder_per_stock": 0,
            "used_length_per_stock": 1010,
        }
    ]
    result_group["unused_materials"] = []
    result_group["generated_remnants"] = []
    result_group["metrics"].update(
        {
            "demand_length": 3000,
            "available_stock_length": 3030,
            "used_stock_length": 3030,
            "utilization_rate": 3000 / 3030,
            "welding_joint_quantity": 3,
            "welding_pattern_type_quantity": 1,
            "cutting_pattern_type_quantity": 1,
            "kerf_loss": 30,
            "remainder_length": 0,
        }
    )
    solution["summary"].update(
        {
            "demand_length": 3000,
            "used_stock_length": 3030,
            "utilization_rate": 3000 / 3030,
            "welding_joint_quantity": 3,
            "welding_pattern_type_quantity": 1,
            "cutting_pattern_type_quantity": 1,
            "kerf_loss": 30,
            "remainder_length": 0,
        }
    )

    same_report = verify_solution(problem, solution)

    assert same_report["passed"] is True
    assert same_report["recomputed_metrics"]["welding_pattern_type_quantity"] == 1

    reversed_solution = deepcopy(solution)
    reversed_row = reversed_solution["groups"][0]["welding_patterns"][1]
    reversed_row["parts"] = [600, 400]
    reversed_row["weld_positions"] = [600]
    reversed_solution["groups"][0]["metrics"]["welding_pattern_type_quantity"] = 2
    reversed_solution["summary"]["welding_pattern_type_quantity"] = 2

    reversed_report = verify_solution(problem, reversed_solution)

    assert reversed_report["passed"] is True
    assert reversed_report["recomputed_metrics"]["welding_pattern_type_quantity"] == 2


def test_verifier_independently_replays_fractional_input_normalization() -> None:
    problem, solution = _valid_case()
    problem = deepcopy(problem)
    solution = deepcopy(solution)
    root = problem["input"]
    group = root["data"][0]
    root["NestParam"]["BladeMargin"] = "9.01"
    root["Min_Welding_Length"] = "499.01"
    root["Min_Reusable_Remnant_Length"] = "99.01"
    group["Pipe"][0]["pipe_length"] = "999.01"
    group["Pipe"][0]["Unweldable_Area"] = [
        ["0.01", "99.01"],
        ["900.99", "999.01"],
    ]
    group["Stock"][0]["stock_length"] = "1009.01"

    records = [
        {
            "path": "input.Min_Reusable_Remnant_Length",
            "original": "99.01",
            "normalized": 100,
            "rule": "CEILING_TO_INTEGER_MM",
        },
        {
            "path": "input.Min_Welding_Length",
            "original": "499.01",
            "normalized": 500,
            "rule": "CEILING_TO_INTEGER_MM",
        },
        {
            "path": "input.NestParam.BladeMargin",
            "original": "9.01",
            "normalized": 10,
            "rule": "CEILING_TO_INTEGER_MM",
        },
        {
            "path": "input.data[0].Pipe[0].Unweldable_Area[0][0]",
            "original": "0.01",
            "normalized": 0,
            "rule": "FLOOR_TO_INTEGER_MM",
        },
        {
            "path": "input.data[0].Pipe[0].Unweldable_Area[0][1]",
            "original": "99.01",
            "normalized": 100,
            "rule": "CEILING_TO_INTEGER_MM",
        },
        {
            "path": "input.data[0].Pipe[0].Unweldable_Area[1][0]",
            "original": "900.99",
            "normalized": 900,
            "rule": "FLOOR_TO_INTEGER_MM",
        },
        {
            "path": "input.data[0].Pipe[0].Unweldable_Area[1][1]",
            "original": "999.01",
            "normalized": 1000,
            "rule": "CEILING_TO_INTEGER_MM",
        },
        {
            "path": "input.data[0].Pipe[0].pipe_length",
            "original": "999.01",
            "normalized": 1000,
            "rule": "CEILING_TO_INTEGER_MM",
        },
        {
            "path": "input.data[0].Stock[0].stock_length",
            "original": "1009.01",
            "normalized": 1010,
            "rule": "CEILING_TO_INTEGER_MM",
        },
    ]
    records.sort(key=lambda row: (row["path"], row["rule"]))
    solution["groups"][0]["input_normalizations"] = records
    solution["summary"]["normalized_length_field_quantity"] = len(records)

    report = verify_solution(problem, solution)

    assert report["passed"] is True
    assert report["recomputed_metrics"]["normalized_length_field_quantity"] == 9

    corrupted = deepcopy(solution)
    corrupted["groups"][0]["input_normalizations"][0]["normalized"] += 1
    corrupted_report = verify_solution(problem, corrupted)
    assert "INPUT_NORMALIZATION_VALUE_MISMATCH" in _codes(corrupted_report)


def test_is_must_chinese_yes_is_a_hard_inventory_requirement() -> None:
    problem, solution = _valid_case()
    stock = problem["input"]["data"][0]["Stock"][2]
    stock.pop("must_use", None)
    stock["Is_Must"] = "是"

    required_report = verify_solution(problem, solution)

    assert "MUST_USE_STOCK_NOT_USED" in _codes(required_report)

    stock["Is_Must"] = "否"
    optional_report = verify_solution(problem, solution)
    assert optional_report["passed"] is True


def test_must_use_is_group_priority_not_hard_quota() -> None:
    """Must-use is a group-level priority: available bars may be used only after
    every must-use bar is consumed.  Demand small enough to be served from a
    subset of must-use bars does NOT force exhausting the must-use inventory.

    In valid_case the 1500 bar is left in ``unused_materials``.  Marking every
    offered length must-use means the solution uses only must-use bars (no
    available bar is touched), so the surplus 1500 must-use bar left uncut must
    NOT raise MUST_USE_STOCK_NOT_USED under the corrected priority semantics.
    """
    problem, solution = _valid_case()
    problem = deepcopy(problem)
    for stock in problem["input"]["data"][0]["Stock"]:
        stock.pop("Is_Must", None)
        stock["must_use"] = 1

    report = verify_solution(problem, solution)
    assert "MUST_USE_STOCK_NOT_USED" not in _codes(report)
    assert report["passed"] is True


def test_must_use_violation_when_available_used_before_must_use_exhausted() -> None:
    """Using an available bar while a must-use bar is still unused is a
    violation.  valid_case uses the 1010 and 1300 bars but leaves 1500 unused;
    if 1500 is must-use while 1010/1300 stay available, the plan touches
    available stock before exhausting must-use -> MUST_USE_STOCK_NOT_USED.
    """
    problem, solution = _valid_case()
    problem = deepcopy(problem)
    for stock in problem["input"]["data"][0]["Stock"]:
        stock.pop("Is_Must", None)
        stock["must_use"] = 1 if stock["stock_length"] == 1500 else 0

    report = verify_solution(problem, solution)
    assert "MUST_USE_STOCK_NOT_USED" in _codes(report)


def test_string_encoded_unweldable_area_is_parsed_and_audited() -> None:
    # Real MOM exports flatten Unweldable_Area into a one-element string list
    # such as ["[0, 100],[900, 1000]"].  The verifier must recognise that shape
    # so a valid solution still passes (previously the string tripped a false
    # INVALID_UNWELDABLE_INTERVAL) and so fractional endpoints are still audited.
    problem, solution = _valid_case()
    problem = deepcopy(problem)
    pipe = problem["input"]["data"][0]["Pipe"][0]
    pipe["Unweldable_Area"] = ["[0, 100],[900, 1000]"]

    report = verify_solution(problem, solution)

    assert "INVALID_UNWELDABLE_INTERVAL" not in _codes(report)
    assert report["passed"] is True


def test_string_encoded_unweldable_area_audits_fractional_endpoint() -> None:
    problem, solution = _valid_case()
    problem = deepcopy(problem)
    solution = deepcopy(solution)
    pipe = problem["input"]["data"][0]["Pipe"][0]
    # A forbidden zone clear of the pipe[0] weld at 400, with a fractional end.
    pipe["Unweldable_Area"] = ["[0, 100],[850.62, 999]"]

    records = list(solution["groups"][0]["input_normalizations"])
    records.append(
        {
            "path": "input.data[0].Pipe[0].Unweldable_Area[1][0]",
            "original": 850.62,
            "normalized": 850,
            "rule": "FLOOR_TO_INTEGER_MM",
        }
    )
    records.sort(key=lambda row: (row["path"], row["rule"]))
    solution["groups"][0]["input_normalizations"] = records
    solution["summary"]["normalized_length_field_quantity"] = len(
        {row["path"] for row in records}
    )

    report = verify_solution(problem, solution)

    assert report["passed"] is True
    audited_paths = report["recomputed_metrics"]["groups"][0][
        "input_normalization_paths"
    ]
    assert any(path.endswith("Unweldable_Area[1][0]") for path in audited_paths)


def test_unsolved_group_reports_input_derived_must_use_and_stock() -> None:
    # An unsolved group has no cutting plan, but must-use bars and total
    # available stock are input facts the solver still reports in its summary.
    # The verifier must recompute them from the problem rather than zeroing
    # them, or the summary cross-check trips on a group that never solved.
    problem, _ = _valid_case()
    problem = deepcopy(problem)
    group = problem["input"]["data"][0]
    group["Stock"][0]["must_use"] = 1

    unsolved_solution = {
        "status": "INFEASIBLE",
        "groups": [
            {
                "material": group.get("material") or group.get("Material"),
                "specifications": group.get("specifications")
                or group.get("Specification"),
                "metrics": {"solve_status": "UNSOLVED"},
                "shortage_diagnosis": {"solvable_by_carving": False},
                "input_normalizations": [],
            }
        ],
    }

    report = verify_solution(problem, unsolved_solution)

    metrics = report["recomputed_metrics"]["groups"][0]
    assert metrics["unsolved"] is True
    assert metrics["must_use_stock_quantity"] >= 1
    assert metrics["must_use_stock_length"] > 0
    assert metrics["available_stock_length"] > 0
    assert metrics["used_stock_length"] == 0
    assert metrics["welding_joint_quantity"] == 0
