"""D5 solver candidate driven by transferable teacher construction roles."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.d5_column_factory import construct_d5_columns
from scripts.d5_module_factory import filter_constructed_by_roles, role_limits
from scripts.solve_d5_motif import solve_setcover_lex

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = (
    ROOT / "data" / "acceptance" / "d5_teacher_modules" / "d5_only_support2" / "module_dictionary.json"
)


def load_role_catalog(path: Path = DEFAULT_CATALOG) -> tuple[dict[str, Any], int]:
    """Load only aggregate role support, never concrete historical layouts."""
    document = json.loads(path.read_text(encoding="utf-8"))
    return document.get("recipe_catalog", {}), int(document["n_samples"])


def solve_d5_modules(
    group,
    *,
    tl: float = 120.0,
    min_support_fraction: float = 0.5,
    max_weld_per_pipe: int = 3,
    max_cut_per_stock: int = 12,
    legacy_metrics: dict[str, Any] | None = None,
    **solver_kwargs: Any,
) -> dict[str, Any]:
    """Solve using bounded current-order columns selected by learned roles."""
    catalog, total_training_cases = load_role_catalog()
    limits = role_limits(
        catalog,
        total_training_cases=total_training_cases,
        min_support_fraction=min_support_fraction,
    )
    base = construct_d5_columns(group, max_weld_per_pipe=max_weld_per_pipe * 8)
    constructed = filter_constructed_by_roles(
        base,
        cut_arities=limits["cut_arities"],
        weld_arities=limits["weld_arities"],
        max_weld_per_pipe=max_weld_per_pipe,
        max_cut_per_stock=max_cut_per_stock,
    )
    result = solve_setcover_lex(group, constructed, tl=tl, **solver_kwargs)
    result["solver_path"] = "d5_role_module_v0"
    result["role_limits"] = {
        "cut_arities": sorted(limits["cut_arities"]),
        "weld_arities": sorted(limits["weld_arities"]),
    }
    result["candidate_counts"] = {"weld": constructed["n_weld"], "cut": constructed["n_cut"]}
    if result.get("ok") and legacy_metrics:
        from scripts.solve_d5_motif import auto_tag

        result["auto_tag"] = auto_tag(result, legacy_metrics)
    return result
