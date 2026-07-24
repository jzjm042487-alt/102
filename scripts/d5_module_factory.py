"""Input-reconstructed D5 role-module selection.

Teacher mining supplies only role frequencies. All concrete pattern selection uses
this order's generated candidates and its current inventory lengths.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def role_limits(
    catalog: Mapping[str, Any], *, total_training_cases: int, min_support_fraction: float = 0.5
) -> dict[str, set[int]]:
    """Return arities whose role support clears the training-only threshold."""
    if total_training_cases <= 0:
        raise ValueError("total_training_cases must be positive")
    if not 0 < min_support_fraction <= 1:
        raise ValueError("min_support_fraction must be within (0, 1]")
    required = total_training_cases * min_support_fraction
    return {
        "cut_arities": {
            int(row["arity"])
            for row in catalog.get("cut_roles", [])
            if int(row.get("support_cases", 0)) >= required
        },
        "weld_arities": {
            int(row["arity"])
            for row in catalog.get("weld_roles", [])
            if int(row.get("support_cases", 0)) >= required
        },
    }


def filter_constructed_by_roles(
    constructed: Mapping[str, Any],
    *,
    cut_arities: set[int],
    weld_arities: set[int],
    max_weld_per_pipe: int,
    max_cut_per_stock: int,
) -> dict[str, Any]:
    """Bound an existing current-order candidate pool by learned role arities."""
    stock_lengths = {int(length) for length in constructed["stock_qty"]}
    weld_pool = {
        int(pipe_idx): select_role_patterns(
            patterns,
            stock_lengths=stock_lengths,
            allowed_arities=weld_arities,
            max_patterns=max_weld_per_pipe,
        )
        for pipe_idx, patterns in constructed["weld_pool"].items()
    }
    cut_pool = {
        int(stock_len): select_role_patterns(
            patterns,
            stock_lengths=set(),
            allowed_arities=cut_arities,
            max_patterns=max_cut_per_stock,
        )
        for stock_len, patterns in constructed["cut_pool"].items()
    }
    required_segments = {segment for patterns in weld_pool.values() for pattern in patterns for segment in pattern}
    produced_segments = {segment for patterns in cut_pool.values() for pattern in patterns for segment in pattern}
    missing_segments = required_segments - produced_segments
    if missing_segments:
        for stock_len, patterns in constructed["cut_pool"].items():
            candidates = select_role_patterns(
                patterns,
                stock_lengths=set(),
                allowed_arities=cut_arities,
                max_patterns=len(patterns),
            )
            for pattern in candidates:
                if not (set(pattern) & missing_segments) or pattern in cut_pool[int(stock_len)]:
                    continue
                cut_pool[int(stock_len)].append(pattern)
                missing_segments -= set(pattern)
                if not missing_segments:
                    break
            if not missing_segments:
                break
    result = dict(constructed)
    result.update(
        {
            "ok": bool(constructed.get("ok")) and all(weld_pool.values()),
            "weld_pool": weld_pool,
            "cut_pool": cut_pool,
            "n_weld": sum(len(patterns) for patterns in weld_pool.values()),
            "n_cut": sum(len(patterns) for patterns in cut_pool.values()),
            "factory": "d5_role_module_v0",
        }
    )
    return result


def select_role_patterns(
    patterns: Iterable[tuple[int, ...]],
    *,
    stock_lengths: set[int],
    allowed_arities: set[int],
    max_patterns: int,
) -> list[tuple[int, ...]]:
    """Choose a bounded set of current-order patterns by transferable roles."""
    if max_patterns < 1:
        return []
    unique = {tuple(int(value) for value in pattern) for pattern in patterns}
    eligible = [pattern for pattern in unique if len(pattern) in allowed_arities]
    eligible.sort(
        key=lambda pattern: (
            -int(any(segment in stock_lengths for segment in pattern)),
            -max(pattern),
            pattern,
        )
    )
    return eligible[:max_patterns]
