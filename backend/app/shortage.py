"""Material-shortage diagnosis for infeasible material groups.

When a material group cannot be nested with the supplied stock, the shop needs
an *actionable* answer, not a bare ``INFEASIBLE``.  This module answers three
questions the workshop actually asks:

1. Is it truly infeasible, or would carving the tubes harder (more splice welds,
   up to the process hard cap) still fit?  The shop has no bargaining power over
   material, so it will cut smaller before it asks for more stock.
2. If still infeasible, what is the *smallest* extra stock (per specification)
   that makes it feasible?  This is the number the shop takes to the material
   department.
3. What does the shortage look like -- a bar-count shortfall (too few pieces of
   stock) or a length shortfall (not enough total metal)?

The oracle here is intentionally independent of the main lexicographic solver:
it only ever asks "does *any* legal packing exist", so it stays fast and its
verdicts are easy to trust.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .domain import MaterialGroup, PipeDemand, from_units
from .solver import (
    _CutCandidate,
    _WeldCandidate,
    _generate_cut_candidates,
    _generate_weld_candidates,
    _legal_pattern,
)

# Absolute ceiling on splice welds when carving harder than the process rule.
# JB/T 6509 practice tops out well below this; 13 is the field-reported maximum
# for very long serpentine tubes.
HARD_JOINT_CAP = 13
# Largest per-spec top-up the search will propose before giving up.
MAX_EXTRA_BARS_PER_SPEC = 40
# Above this many distinct segment lengths the feasibility MILP becomes too large
# to decide quickly (the balance constraints scale with segments x cut columns).
# Such groups are "hard", not necessarily short, so the diagnoser declines rather
# than risk a false shortage verdict or a hang.
MAX_ORACLE_SEGMENT_LENGTHS = 120


def _uniform_split(pipe: PipeDemand, group: MaterialGroup, n: int, max_stock: int) -> tuple[int, ...] | None:
    """Return a legal near-uniform ``n``-segment split, or ``None``.

    Cut positions start at the ideal equal-split coordinates and are nudged onto
    the nearest weldable location that still honours the minimum weld distance.
    """

    if n < 1:
        return None
    if n == 1:
        whole = (pipe.length,)
        if pipe.length <= max_stock and _legal_pattern(
            pipe, whole, group.min_weld_distance, group.min_cut_length
        ):
            return whole
        return None
    ideal = [pipe.length * (i + 1) // n for i in range(n - 1)]
    window = max(group.min_weld_distance, 800)
    positions: list[int] = []
    for target in ideal:
        chosen = None
        for delta in range(0, window + 1):
            for cand in (target - delta, target + delta):
                if positions and cand - positions[-1] < group.min_weld_distance:
                    continue
                if 0 < cand < pipe.length and pipe.weld_allowed(cand):
                    chosen = cand
                    break
            if chosen is not None:
                break
        if chosen is None:
            return None
        positions.append(chosen)
    parts: list[int] = []
    prev = 0
    for pos in positions:
        parts.append(pos - prev)
        prev = pos
    parts.append(pipe.length - prev)
    result = tuple(parts)
    if max(result) > max_stock:
        return None
    if _legal_pattern(pipe, result, group.min_weld_distance, group.min_cut_length):
        return result
    return None


def _carved_weld_pool(group: MaterialGroup, joint_cap: int) -> list[_WeldCandidate]:
    """Rich weld pool: the solver's own high-quality columns *plus* every legal
    near-uniform split up to ``joint_cap`` welds.

    The baseline columns (exact stock-length matches and their complements) are
    essential -- a pool of near-uniform splits alone can miss the only packing
    that balances, wrongly reporting a shortage.  The extra carved splits then
    exercise the *full* carving ability beyond the process joint limit so the
    oracle never blames a shortage on cutting that harder splitting could avoid.
    """

    max_stock = max(stock.length for stock in group.stocks)
    pool: list[_WeldCandidate] = list(_generate_weld_candidates(group))
    seen: set[tuple[int, tuple[int, ...]]] = {
        (candidate.pipe_index, candidate.parts) for candidate in pool
    }
    for pipe_index, pipe in enumerate(group.pipes):
        for n in range(1, joint_cap + 2):
            parts = _uniform_split(pipe, group, n, max_stock)
            if parts is not None and (pipe_index, parts) not in seen:
                seen.add((pipe_index, parts))
                pool.append(_WeldCandidate(pipe_index, parts))
    return pool


def _oracle_cut_pool(
    group: MaterialGroup, weld_pool: list[_WeldCandidate], per_stock_cap: int
) -> list[_CutCandidate]:
    """Cut columns for the feasibility oracle, capped per stock length.

    The oracle only needs to decide feasibility, so an exhaustive cut pool (which
    can reach ~10k columns on large groups and stall the solver) is unnecessary.
    Every single-part column is kept (they are what let a segment be produced at
    all); multi-part columns are ranked by tightest fit and truncated.
    """

    columns = _generate_cut_candidates(group, weld_pool)
    by_stock: dict[int, list[_CutCandidate]] = defaultdict(list)
    for column in columns:
        by_stock[column.stock_length].append(column)
    pruned: list[_CutCandidate] = []
    for items in by_stock.values():
        singles = [c for c in items if len(c.parts) == 1]
        multi = [c for c in items if len(c.parts) > 1]
        multi.sort(key=lambda c: (c.remainder, len(c.parts), c.parts))
        pruned.extend(singles)
        pruned.extend(multi[:per_stock_cap])
    return pruned


def _feasible(
    group: MaterialGroup,
    weld_pool: list[_WeldCandidate],
    stock_quantities: dict[int, int],
    time_limit: float,
    *,
    per_stock_cap: int = 400,
) -> tuple[str, int | None]:
    """Pure feasibility oracle: does any legal packing exist under the given
    stock counts?  Returns ``(verdict, bars_used)`` where verdict is one of
    ``"FEASIBLE"``, ``"INFEASIBLE"`` or ``"INCONCLUSIVE"`` (timed out before a
    verdict).  ``bars_used`` is the first feasible bar count (an upper bound, not
    necessarily minimal) and is ``None`` unless feasible.
    """

    try:
        from pyscipopt import Model, quicksum
    except Exception:  # pragma: no cover - solver backend is a hard dependency here
        raise RuntimeError("pyscipopt is required for shortage diagnosis")

    cut_pool = _oracle_cut_pool(group, weld_pool, per_stock_cap)
    if not weld_pool or not cut_pool:
        return "INFEASIBLE", None

    model = Model("shortage-feasibility")
    model.hideOutput()
    model.setRealParam("limits/time", max(0.1, time_limit))
    # A feasibility verdict does not need the optimal bar count; stopping at the
    # first feasible packing keeps the oracle fast even on large groups.
    model.setIntParam("limits/solutions", 1)

    weld_vars = [model.addVar(vtype="INTEGER", lb=0) for _ in weld_pool]
    cut_vars = [model.addVar(vtype="INTEGER", lb=0) for _ in cut_pool]

    welds_by_pipe: dict[int, list[int]] = defaultdict(list)
    for i, candidate in enumerate(weld_pool):
        welds_by_pipe[candidate.pipe_index].append(i)
    for pipe_index, pipe in enumerate(group.pipes):
        indices = welds_by_pipe.get(pipe_index, [])
        if not indices:
            return "INFEASIBLE", None
        model.addCons(quicksum(weld_vars[i] for i in indices) == pipe.demand)

    cuts_by_stock: dict[int, list[int]] = defaultdict(list)
    for i, candidate in enumerate(cut_pool):
        cuts_by_stock[candidate.stock_length].append(i)
    for stock_length, quantity in stock_quantities.items():
        indices = cuts_by_stock.get(stock_length, [])
        if indices:
            model.addCons(quicksum(cut_vars[i] for i in indices) <= quantity)
    # Cut columns on a stock length with zero supply must stay unused.
    for stock_length, indices in cuts_by_stock.items():
        if stock_quantities.get(stock_length, 0) == 0:
            for i in indices:
                model.addCons(cut_vars[i] == 0)

    segment_lengths = {part for candidate in weld_pool for part in candidate.parts}
    for segment in segment_lengths:
        produced = quicksum(
            candidate.parts.count(segment) * cut_vars[i]
            for i, candidate in enumerate(cut_pool)
            if segment in candidate.parts
        )
        consumed = quicksum(
            candidate.parts.count(segment) * weld_vars[i]
            for i, candidate in enumerate(weld_pool)
            if segment in candidate.parts
        )
        model.addCons(produced == consumed)

    model.setObjective(quicksum(cut_vars), "minimize")
    model.optimize()
    status = str(model.getStatus()).upper()
    if model.getNSols() > 0:
        return "FEASIBLE", int(round(model.getObjVal()))
    if status == "INFEASIBLE":
        return "INFEASIBLE", None
    # Timed out before finding a packing or proving impossibility.
    return "INCONCLUSIVE", None


def _classify_shortage(group: MaterialGroup) -> str:
    """Distinguish a bar-count shortfall from a length shortfall.

    A length shortfall means the total metal is short and no amount of clever
    cutting recovers it.  A bar-count shortfall means there is enough metal but
    too few physical bars to seat every mandatory segment.
    """

    total_length = sum(stock.length * stock.quantity for stock in group.stocks)
    if total_length < group.demand_length:
        return "LENGTH_SHORTFALL"
    return "BAR_COUNT_SHORTFALL"


def diagnose_group_shortage(
    group: MaterialGroup, *, time_limit: float = 20.0
) -> dict[str, Any]:
    """Diagnose why a group is infeasible and quantify the remedy.

    Returns a structured report the workshop can act on:
      - ``carvable``: feasible once tubes are carved to the hard joint cap?
      - ``shortage_type``: bar-count vs length shortfall
      - ``recommendations``: minimal per-spec top-ups that restore feasibility,
        each annotated with the resulting bar usage.
    """

    base_quantities = {stock.length: stock.quantity for stock in group.stocks}
    carved_pool = _carved_weld_pool(group, HARD_JOINT_CAP)

    # The feasibility MILP scales with distinct segment lengths x cut columns.
    # A group with a huge segment variety is computationally hard rather than
    # provably short, so decline instead of risking a hang or a false verdict.
    segment_variety = len({part for c in carved_pool for part in c.parts})
    if segment_variety > MAX_ORACLE_SEGMENT_LENGTHS:
        return {
            "solvable_by_carving": None,
            "hard_joint_cap": HARD_JOINT_CAP,
            "inconclusive": True,
            "segment_length_variety": segment_variety,
            "message": (
                f"该组段长种类过多（{segment_variety} 种），可行性判定过大，"
                "无法在诊断时限内判定；此为求解规模问题而非缺料，"
                "建议交由排料优化处理该组。"
            ),
            "recommendations": [],
        }

    verdict, bars = _feasible(group, carved_pool, base_quantities, time_limit)
    if verdict == "FEASIBLE":
        return {
            "solvable_by_carving": True,
            "hard_joint_cap": HARD_JOINT_CAP,
            "bars_used": bars,
            "message": (
                "可通过加大切分（提高焊口数至工艺硬上限）在现有库存内排出；"
                "无需补料。求解器主流程未能排出，建议交由排料优化处理该组。"
            ),
            "recommendations": [],
        }
    if verdict == "INCONCLUSIVE":
        # Neither a packing nor an impossibility proof was found in time.  Do
        # NOT claim a shortage -- that would send the shop to buy stock it may
        # not need.  Report honestly that the group is hard, not short.
        return {
            "solvable_by_carving": None,
            "hard_joint_cap": HARD_JOINT_CAP,
            "inconclusive": True,
            "message": (
                "在诊断时限内既未排出、也未能证明无解：该组求解困难，"
                "但不能断定缺料。建议提高求解时限或改进排料算法后重试。"
            ),
            "recommendations": [],
        }

    shortage_type = _classify_shortage(group)
    # Search the smallest single-spec top-up that restores feasibility.  Stock
    # lengths are tried longest first: extra long bars usually unlock a packing
    # with the fewest added pieces.
    recommendations: list[dict[str, Any]] = []
    for stock in sorted(group.stocks, key=lambda s: -s.length):
        for extra in range(1, MAX_EXTRA_BARS_PER_SPEC + 1):
            trial = dict(base_quantities)
            trial[stock.length] = trial.get(stock.length, 0) + extra
            trial_verdict, trial_bars = _feasible(
                group, carved_pool, trial, time_limit
            )
            if trial_verdict == "FEASIBLE":
                recommendations.append(
                    {
                        "stock_length": from_units(stock.length),
                        "add_quantity": extra,
                        "resulting_bars_used": trial_bars,
                        "message": (
                            f"补发 {extra} 根 {from_units(stock.length)}mm 定尺后可排出"
                            f"（预计用料 {trial_bars} 根）。"
                        ),
                    }
                )
                break
            if trial_verdict == "INCONCLUSIVE":
                # Stop escalating this spec once verdicts become unreliable.
                break

    return {
        "solvable_by_carving": False,
        "hard_joint_cap": HARD_JOINT_CAP,
        "shortage_type": shortage_type,
        "recommendations": recommendations,
        "message": (
            "现有库存无论如何切分都无法排出。"
            + (
                "以下为使其可行的最小补料建议（任选其一）。"
                if recommendations
                else "在搜索范围内未找到单一规格的补料方案，请人工评估补料。"
            )
        ),
    }


__all__ = ["diagnose_group_shortage", "HARD_JOINT_CAP", "MAX_EXTRA_BARS_PER_SPEC"]
