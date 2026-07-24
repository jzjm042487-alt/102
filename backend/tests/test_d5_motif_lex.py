"""Contract tests for D5 motif's locked lexicographic optimization."""
from __future__ import annotations

from types import SimpleNamespace

from scripts.probe_d5_motif import _remaining_idxs
from scripts.solve_d5_motif import solve_setcover_lex


def test_d5_type_compression_keeps_phase_a_joint_optimum() -> None:
    """Later type-minimization phases must never relax the best joint count."""
    group = SimpleNamespace(
        pipes=[SimpleNamespace(length=10, demand=1)],
        demand_length=10,
    )
    constructed = {
        "ok": True,
        "weld_pool": {0: [(4, 6), (5, 5)]},
        "cut_pool": {10: [(4, 6), (5, 5)]},
        "stock_qty": {10: 1},
        "sigma": [4, 5, 6],
        "n_weld": 2,
        "n_cut": 2,
    }

    result = solve_setcover_lex(group, constructed, tl=15)

    assert result["ok"] is True
    assert result["phase_objectives"]["joints"] == result["joints"]
    assert result["phase_objectives"]["weld_types"] == result["weld_types"]
    assert result["phase_objectives"]["cut_types"] == result["cut_types"]


def test_d5_pareto_budget_allows_explicit_joint_slack() -> None:
    """A Pareto run exposes an explicit cap above the joint optimum."""
    group = SimpleNamespace(
        pipes=[SimpleNamespace(length=10, demand=1)],
        demand_length=10,
    )
    constructed = {
        "ok": True,
        "weld_pool": {0: [(4, 6), (5, 5)]},
        "cut_pool": {10: [(4, 6), (5, 5)]},
        "stock_qty": {10: 1},
        "sigma": [4, 5, 6],
        "n_weld": 2,
        "n_cut": 2,
    }

    result = solve_setcover_lex(group, constructed, tl=15, joint_slack=3)

    assert result["ok"] is True
    assert result["phase_objectives"]["joint_limit"] == result["joints"] + 3


def test_remaining_idxs_skips_completed_case_ids() -> None:
    """An interrupted probe resumes from persisted case rows without duplicates."""
    existing = [{"level": 2452}, {"level": 2605}]

    assert _remaining_idxs([2452, 2605, 1969], existing) == [1969]


def test_d5_recipe_caps_are_reported_in_phase_objectives() -> None:
    """A bounded feasibility probe exposes the requested recipe caps."""
    group = SimpleNamespace(
        pipes=[SimpleNamespace(length=10, demand=1)],
        demand_length=10,
    )
    constructed = {
        "ok": True,
        "weld_pool": {0: [(4, 6), (5, 5)]},
        "cut_pool": {10: [(4, 6), (5, 5)]},
        "stock_qty": {10: 1},
        "sigma": [4, 5, 6],
        "n_weld": 2,
        "n_cut": 2,
    }

    result = solve_setcover_lex(
        group,
        constructed,
        tl=15,
        max_weld_types=1,
        max_cut_types=1,
    )

    assert result["ok"] is True
    assert result["phase_objectives"]["weld_type_limit"] == 1
    assert result["phase_objectives"]["cut_type_limit"] == 1
