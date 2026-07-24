"""Contract tests for input-reconstructed D5 role module columns."""
from __future__ import annotations

from scripts.d5_module_factory import filter_constructed_by_roles, role_limits, select_role_patterns


def test_role_limits_keep_only_frequent_teacher_construction_roles() -> None:
    catalog = {
        "cut_roles": [
            {"arity": 1, "role": "near_fill", "support_cases": 9},
            {"arity": 2, "role": "near_fill", "support_cases": 8},
            {"arity": 5, "role": "near_fill", "support_cases": 2},
        ],
        "weld_roles": [
            {"arity": 1, "role": "one_segment", "support_cases": 7},
            {"arity": 2, "role": "two_segment", "support_cases": 6},
            {"arity": 4, "role": "multi_segment", "support_cases": 1},
        ],
    }

    assert role_limits(catalog, total_training_cases=10, min_support_fraction=0.5) == {
        "cut_arities": {1, 2},
        "weld_arities": {1, 2},
    }


def test_select_role_patterns_prefers_stock_anchored_welds_and_limits_variety() -> None:
    patterns = [(350, 650), (400, 600), (250, 750), (200, 300, 500)]

    selected = select_role_patterns(
        patterns,
        stock_lengths={600, 750},
        allowed_arities={2},
        max_patterns=2,
    )

    assert selected == [(250, 750), (400, 600)]
    assert all(len(pattern) == 2 for pattern in selected)


def test_select_role_patterns_uses_current_stock_not_teacher_dimensions() -> None:
    selected = select_role_patterns(
        [(250, 750), (400, 600)],
        stock_lengths={650},
        allowed_arities={2},
        max_patterns=1,
    )

    assert selected == [(250, 750)]


def test_filter_constructed_by_roles_keeps_segment_coverage_after_limiting_patterns() -> None:
    constructed = {
        "ok": True,
        "weld_pool": {0: [(40, 60)]},
        "cut_pool": {105: [(40, 60), (40,), (60,)]},
        "stock_qty": {105: 2},
        "sigma": [40, 60],
    }

    filtered = filter_constructed_by_roles(
        constructed,
        cut_arities={1, 2},
        weld_arities={2},
        max_weld_per_pipe=1,
        max_cut_per_stock=1,
    )

    produced = {segment for pattern in filtered["cut_pool"][105] for segment in pattern}
    assert {40, 60} <= produced


def test_filter_constructed_by_roles_removes_unfrequent_arities_and_reports_counts() -> None:
    constructed = {
        "ok": True,
        "weld_pool": {0: [(40, 60), (20, 30, 50)]},
        "cut_pool": {105: [(40, 60), (20, 30, 50)]},
        "stock_qty": {105: 2},
        "sigma": [20, 30, 40, 50, 60],
    }

    filtered = filter_constructed_by_roles(
        constructed,
        cut_arities={2},
        weld_arities={2},
        max_weld_per_pipe=1,
        max_cut_per_stock=1,
    )

    assert filtered["weld_pool"] == {0: [(40, 60)]}
    assert filtered["cut_pool"] == {105: [(40, 60)]}
    assert filtered["n_weld"] == 1
    assert filtered["n_cut"] == 1
