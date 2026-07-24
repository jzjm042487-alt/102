"""Contract tests for the role-module D5 solver entry point."""
from __future__ import annotations

from types import SimpleNamespace

from scripts.solve_d5_modules import solve_d5_modules


def test_solve_d5_modules_passes_role_filtered_columns_to_existing_solver(monkeypatch) -> None:
    group = SimpleNamespace()
    seen = {}

    monkeypatch.setattr(
        "scripts.solve_d5_modules.construct_d5_columns",
        lambda _group, max_weld_per_pipe: {
            "ok": True,
            "weld_pool": {0: [(40, 60), (20, 30, 50)]},
            "cut_pool": {105: [(40, 60), (20, 30, 50)]},
            "stock_qty": {105: 2},
            "sigma": [20, 30, 40, 50, 60],
        },
    )
    monkeypatch.setattr(
        "scripts.solve_d5_modules.load_role_catalog",
        lambda: ({"cut_roles": [{"arity": 2, "support_cases": 10}], "weld_roles": [{"arity": 2, "support_cases": 10}]}, 10),
    )

    def fake_solve(_group, constructed, **_kwargs):
        seen["constructed"] = constructed
        return {"ok": True, "joints": 1}

    monkeypatch.setattr("scripts.solve_d5_modules.solve_setcover_lex", fake_solve)

    result = solve_d5_modules(group, tl=5)

    assert result["ok"] is True
    assert result["solver_path"] == "d5_role_module_v0"
    assert result["role_limits"] == {"cut_arities": [2], "weld_arities": [2]}
    assert seen["constructed"]["weld_pool"] == {0: [(40, 60)]}
    assert seen["constructed"]["cut_pool"] == {105: [(40, 60)]}
