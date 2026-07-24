"""Contract tests for leakage-safe D5 teacher module extraction."""
from __future__ import annotations

from pathlib import Path

from scripts.teacher_module_miner import (
    build_module_dictionary,
    build_recipe_catalog,
    classify_recipe_rows,
    filter_paths_for_case_ids,
    normalize_teacher_case,
    split_case_ids,
)


def test_split_case_ids_is_stable_disjoint_and_exhaustive() -> None:
    case_ids = list(range(100, 120))

    first = split_case_ids(case_ids)
    second = split_case_ids(list(reversed(case_ids)))

    assert first == second
    assigned = [idx for ids in first.values() for idx in ids]
    assert sorted(assigned) == case_ids
    assert not (set(first["train"]) & set(first["validation"]))
    assert not (set(first["train"]) & set(first["holdout"]))
    assert not (set(first["validation"]) & set(first["holdout"]))


def test_filter_paths_for_case_ids_keeps_only_requested_cases() -> None:
    paths = [
        Path("cases/2747_alpha/teacher_columns.json"),
        Path("cases/390_beta/teacher_columns.json"),
    ]

    assert filter_paths_for_case_ids(paths, {390}) == [paths[1]]


def test_normalize_teacher_case_creates_closed_loop_module_without_case_identity() -> None:
    sample = {
        "cuts": [{"stock_len": 100, "segs": [40, 60], "number": 2}],
        "welds": [{"pipe_len": 100, "segs": [40, 60], "number": 2}],
    }

    normalized = normalize_teacher_case(sample)

    assert normalized["cut_signatures"] == [{"signature": [100, [40, 60]], "number": 2}]
    assert normalized["weld_signatures"] == [{"signature": [100, [40, 60]], "number": 2}]
    assert normalized["segment_flow"] == {"40": 0, "60": 0}
    assert normalized["closed_loop"] is True
    assert "case_id" not in normalized


def test_normalize_teacher_case_keeps_cut_surplus_as_module_flow() -> None:
    sample = {
        "cuts": [{"stock_len": 105, "segs": [40, 60], "number": 2}],
        "welds": [{"pipe_len": 100, "segs": [40, 60], "number": 1}],
    }

    normalized = normalize_teacher_case(sample)

    assert normalized is not None
    assert normalized["segment_flow"] == {"40": 1, "60": 1}


def test_normalize_teacher_case_rejects_uncovered_weld_segment() -> None:
    sample = {
        "cuts": [{"stock_len": 100, "segs": [40, 60], "number": 1}],
        "welds": [{"pipe_len": 110, "segs": [40, 70], "number": 1}],
    }

    assert normalize_teacher_case(sample) is None


def test_normalize_teacher_case_preserves_weld_order_but_sorts_cut_signature() -> None:
    sample = {
        "cuts": [{"stock_len": 100, "segs": [60, 40], "number": 1}],
        "welds": [{"pipe_len": 100, "segs": [60, 40], "number": 1}],
    }

    normalized = normalize_teacher_case(sample)

    assert normalized is not None
    assert normalized["cut_signatures"][0]["signature"] == [100, [40, 60]]
    assert normalized["weld_signatures"][0]["signature"] == [100, [60, 40]]


def test_classify_recipe_rows_marks_concentrated_pattern_as_main() -> None:
    rows = [
        {"signature": [100, [40, 60]], "number": 9},
        {"signature": [100, [30, 70]], "number": 1},
    ]

    classified = classify_recipe_rows(rows, main_coverage=0.8)

    assert classified[0]["role"] == "main"
    assert classified[1]["role"] == "tail"


def test_build_module_dictionary_uses_only_training_samples_for_prototypes() -> None:
    sample = {
        "cuts": [{"stock_len": 100, "segs": [40, 60], "number": 2}],
        "welds": [{"pipe_len": 100, "segs": [40, 60], "number": 2}],
    }
    normalized = normalize_teacher_case(sample)

    dictionary = build_module_dictionary([normalized], min_support=1)

    assert dictionary["n_samples"] == 1
    assert dictionary["prototypes"][0]["support_cases"] == 1
    assert "case_id" not in str(dictionary["prototypes"])


def test_build_recipe_catalog_groups_geometry_roles_not_absolute_lengths() -> None:
    first = normalize_teacher_case(
        {
            "cuts": [{"stock_len": 105, "segs": [40, 60], "number": 1}],
            "welds": [{"pipe_len": 100, "segs": [40, 60], "number": 1}],
        }
    )
    second = normalize_teacher_case(
        {
            "cuts": [{"stock_len": 125, "segs": [50, 70], "number": 1}],
            "welds": [{"pipe_len": 120, "segs": [50, 70], "number": 1}],
        }
    )

    catalog = build_recipe_catalog([first, second])

    assert catalog["cut_roles"] == [{"arity": 2, "role": "near_fill", "support_cases": 2}]
    assert catalog["weld_roles"] == [{"arity": 2, "role": "two_segment", "support_cases": 2}]
