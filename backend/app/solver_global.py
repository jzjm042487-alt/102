"""Global joint cut+weld nesting engine (engine=global).

Implements ``docs/research/global-joint-model-spec-v1.md``:

* Phase A: minimise used stock length subject to util >= 95%
* Phase B: minimise cut types under used-length slack
* Phase C: minimise weld types then joints under Phase B caps

``time_limit`` is a required caller-provided budget (API/CLI input), never a
hard-coded wall-clock policy inside the model.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from typing import Any

from .domain import MaterialGroup
from .global_candidates import build_cut_pool, build_weld_pool, focus_cut_alphabet
from .solver import (
    _CutCandidate,
    _WeldCandidate,
    _assemble_group_result,
)

logger = logging.getLogger(__name__)

UTIL_FLOOR = 0.95
PHASE_B_SLACK = 0.002
PHASE_A_SHARE = 0.50
PHASE_B_SHARE = 0.30
PHASE_C_SHARE = 0.20


def _precheck(group: MaterialGroup) -> str | None:
    if group.stock_length < group.demand_length:
        return "A_LENGTH_SHORTFALL"
    if not group.stocks:
        return "A_STOCK_STRUCTURE"
    if not group.pipes:
        return "A_PROCESS_INFEASIBLE"
    return None


def _max_used_for_util_floor(demand_length: int, floor: float = UTIL_FLOOR) -> int:
    """Largest used length that still satisfies demand/used >= floor."""
    if floor <= 0:
        raise ValueError("util floor must be positive")
    # used <= demand / floor
    return int(math.floor(demand_length / floor + 1e-12))


def _solve_joint(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
    time_limit: float,
    *,
    minimise: str,
    target_len: int | None = None,
    max_used_len: int | None = None,
    max_cut_types: int | None = None,
) -> dict[str, Any] | None:
    from pyscipopt import Model, quicksum

    if time_limit <= 0 or not math.isfinite(time_limit):
        raise ValueError("time_limit must be a positive finite number")

    model = Model(f"global_{minimise}")
    model.setParam("limits/time", max(0.05, float(time_limit)))
    model.hideOutput()

    n = len(cut_cols)
    bars_by_len: dict[int, int] = defaultdict(int)
    for stock in group.stocks:
        bars_by_len[stock.length] += stock.quantity

    u: dict[tuple[int, int], Any] = {}
    for pipe_index, patterns in weld_cands.items():
        for weld_index in range(len(patterns)):
            u[(pipe_index, weld_index)] = model.addVar(
                vtype="I", lb=0, name=f"u_{pipe_index}_{weld_index}"
            )
        model.addCons(
            quicksum(u[(pipe_index, weld_index)] for weld_index in range(len(patterns)))
            == group.pipes[pipe_index].demand
        )

    x = {index: model.addVar(vtype="I", lb=0, name=f"x{index}") for index in range(n)}
    cols_by_len: dict[int, list[int]] = defaultdict(list)
    for index, column in enumerate(cut_cols):
        cols_by_len[column["stock"]].append(index)
    for stock_len, indexes in cols_by_len.items():
        model.addCons(quicksum(x[index] for index in indexes) <= bars_by_len[stock_len])

    # must_use is a shop preference, not a hard lower bound in V1: forcing
    # must-use bars can violate the util floor when demand is small.  Preferential
    # consumption is deferred to a later objective term.

    produced: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for index, column in enumerate(cut_cols):
        for seg, count in column["counts"].items():
            produced[seg].append((count, x[index]))
    consumed: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for pipe_index, patterns in weld_cands.items():
        for weld_index, parts in enumerate(patterns):
            counts: dict[int, int] = defaultdict(int)
            for seg in parts:
                counts[seg] += 1
            for seg, count in counts.items():
                consumed[seg].append((count, u[(pipe_index, weld_index)]))
    for seg in set(produced) | set(consumed):
        lhs = quicksum(count * var for count, var in produced.get(seg, []))
        rhs = quicksum(count * var for count, var in consumed.get(seg, []))
        model.addCons(lhs >= rhs)

    length_term = quicksum(cut_cols[index]["stock"] * x[index] for index in range(n))
    if max_used_len is not None:
        model.addCons(length_term <= max_used_len)
    if target_len is not None:
        model.addCons(length_term <= target_len)

    if minimise == "length":
        model.setObjective(length_term, "minimize")
        model.optimize()
        if model.getNSols() == 0:
            return None
        return _extract_solution(model, x, u, weld_cands, cut_cols)

    y = {index: model.addVar(vtype="B", name=f"y{index}") for index in range(n)}
    for index in range(n):
        model.addCons(x[index] <= bars_by_len[cut_cols[index]["stock"]] * y[index])
    if max_cut_types is not None:
        model.addCons(quicksum(y[index] for index in range(n)) <= max_cut_types)

    wtype_vars: dict[tuple[int, ...], list[Any]] = defaultdict(list)
    for pipe_index, patterns in weld_cands.items():
        for weld_index, parts in enumerate(patterns):
            wtype_vars[tuple(parts)].append(u[(pipe_index, weld_index)])
    z = {
        parts: model.addVar(vtype="B", name=f"z{index}")
        for index, parts in enumerate(wtype_vars)
    }
    big_m = sum(pipe.demand for pipe in group.pipes)
    for parts, vars_ in wtype_vars.items():
        model.addCons(quicksum(vars_) <= big_m * z[parts])

    joints_term = quicksum(
        (len(parts) - 1) * u[(pipe_index, weld_index)]
        for pipe_index, patterns in weld_cands.items()
        for weld_index, parts in enumerate(patterns)
    )

    if minimise == "cut_types":
        model.setObjective(
            (len(wtype_vars) + 1) * quicksum(y[index] for index in range(n))
            + quicksum(z[parts] for parts in wtype_vars),
            "minimize",
        )
    elif minimise == "weld_joints":
        alpha = max(1, big_m)
        model.setObjective(
            alpha * quicksum(z[parts] for parts in wtype_vars) + joints_term,
            "minimize",
        )
    else:
        raise ValueError(f"unknown minimise mode {minimise!r}")

    model.optimize()
    if model.getNSols() == 0:
        return None
    return _extract_solution(model, x, u, weld_cands, cut_cols)


def _extract_solution(
    model: Any,
    x: dict[int, Any],
    u: dict[tuple[int, int], Any],
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
) -> dict[str, Any]:
    used_cuts: list[dict[str, Any]] = []
    active_cut_idx: list[int] = []
    used_len = 0
    for index in range(len(cut_cols)):
        value = round(model.getVal(x[index]))
        if value > 0:
            used_cuts.append({**cut_cols[index], "count": value})
            active_cut_idx.append(index)
            used_len += cut_cols[index]["stock"] * value

    used_welds: list[dict[str, Any]] = []
    for (pipe_index, weld_index), var in u.items():
        value = round(model.getVal(var))
        if value > 0:
            used_welds.append(
                {
                    "pipe": pipe_index,
                    "parts": weld_cands[pipe_index][weld_index],
                    "count": value,
                }
            )
    return {
        "status": str(model.getStatus()).upper(),
        "used_len": used_len,
        "cuts": used_cuts,
        "welds": used_welds,
        "active_cut_idx": active_cut_idx,
        "cut_types": len(used_cuts),
        "weld_types": len({tuple(row["parts"]) for row in used_welds}),
        "joints": sum((len(row["parts"]) - 1) * row["count"] for row in used_welds),
    }


def _lp_feasible(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
    time_limit: float,
    max_used_len: int | None = None,
) -> bool:
    """Quick continuous feasibility probe before integer phases."""
    from pyscipopt import Model, quicksum

    model = Model("global_lp_probe")
    model.setParam("limits/time", max(0.05, float(time_limit)))
    model.hideOutput()
    bars_by_len: dict[int, int] = defaultdict(int)
    for stock in group.stocks:
        bars_by_len[stock.length] += stock.quantity

    u: dict[tuple[int, int], Any] = {}
    for pipe_index, patterns in weld_cands.items():
        for weld_index in range(len(patterns)):
            u[(pipe_index, weld_index)] = model.addVar(vtype="C", lb=0)
        model.addCons(
            quicksum(u[(pipe_index, weld_index)] for weld_index in range(len(patterns)))
            == float(group.pipes[pipe_index].demand)
        )
    x = {index: model.addVar(vtype="C", lb=0) for index in range(len(cut_cols))}
    cols_by_len: dict[int, list[int]] = defaultdict(list)
    for index, column in enumerate(cut_cols):
        cols_by_len[column["stock"]].append(index)
    for stock_len, indexes in cols_by_len.items():
        model.addCons(quicksum(x[index] for index in indexes) <= float(bars_by_len[stock_len]))

    produced: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for index, column in enumerate(cut_cols):
        for seg, count in column["counts"].items():
            produced[seg].append((count, x[index]))
    consumed: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for pipe_index, patterns in weld_cands.items():
        for weld_index, parts in enumerate(patterns):
            counts: dict[int, int] = defaultdict(int)
            for seg in parts:
                counts[seg] += 1
            for seg, count in counts.items():
                consumed[seg].append((count, u[(pipe_index, weld_index)]))
    for seg in set(produced) | set(consumed):
        model.addCons(
            quicksum(count * var for count, var in produced.get(seg, []))
            >= quicksum(count * var for count, var in consumed.get(seg, []))
        )
    length_term = quicksum(cut_cols[index]["stock"] * x[index] for index in range(len(cut_cols)))
    if max_used_len is not None:
        model.addCons(length_term <= float(max_used_len))
    model.setObjective(length_term, "minimize")
    model.optimize()
    return model.getNSols() > 0


def _solve_phases(
    group: MaterialGroup,
    time_limit: float,
) -> tuple[dict[str, Any] | None, str | None]:
    max_used = _max_used_for_util_floor(group.demand_length, UTIL_FLOOR)
    t_a = time_limit * PHASE_A_SHARE
    t_b = time_limit * PHASE_B_SHARE
    t_c = time_limit * PHASE_C_SHARE

    last_fail = "C_CANDIDATE_INSUFFICIENT"
    for tier in (1, 2, 3):
        # Spend a slice of Phase-A budget building/probing each tier.
        tier_build_budget = max(0.5, t_a * 0.15)
        started_tier = time.monotonic()
        try:
            weld_cands, alphabet = build_weld_pool(group, tier=tier)
            cut_alphabet = focus_cut_alphabet(group, weld_cands, alphabet)
            cut_cols = build_cut_pool(group, cut_alphabet, tier=tier)
        except ValueError as exc:
            if str(exc).startswith("A_PROCESS_INFEASIBLE"):
                return None, "A_PROCESS_INFEASIBLE"
            last_fail = "C_CANDIDATE_INSUFFICIENT"
            continue
        if not cut_cols:
            last_fail = "C_CANDIDATE_INSUFFICIENT"
            continue

        probe_budget = max(0.5, min(3.0, t_a * 0.2))
        if not _lp_feasible(group, weld_cands, cut_cols, probe_budget, max_used_len=None):
            last_fail = "C_CANDIDATE_INSUFFICIENT"
            continue

        spent = time.monotonic() - started_tier
        phase_a_budget = max(1.0, t_a - spent - tier_build_budget)
        phase_a = _solve_joint(
            group,
            weld_cands,
            cut_cols,
            phase_a_budget,
            minimise="length",
            max_used_len=max_used,
        )
        if phase_a is None:
            # Try without util floor to classify.
            probe = _solve_joint(
                group,
                weld_cands,
                cut_cols,
                min(phase_a_budget, 5.0),
                minimise="length",
            )
            if probe is None:
                last_fail = "C_CANDIDATE_INSUFFICIENT"
                continue
            if probe["used_len"] > max_used:
                last_fail = "A_STOCK_STRUCTURE"
                continue
            last_fail = "C_INTEGER_FAIL"
            continue

        util = group.demand_length / phase_a["used_len"] if phase_a["used_len"] else 0.0
        if util + 1e-12 < UTIL_FLOOR:
            last_fail = "A_STOCK_STRUCTURE"
            continue

        cap_b = int(phase_a["used_len"] * (1.0 + PHASE_B_SLACK))
        active = [cut_cols[index] for index in phase_a["active_cut_idx"]] or cut_cols
        phase_b = _solve_joint(
            group,
            weld_cands,
            active,
            t_b,
            minimise="cut_types",
            target_len=cap_b,
            max_used_len=max_used,
        )
        incumbent = phase_b if phase_b is not None else phase_a
        phase_c = _solve_joint(
            group,
            weld_cands,
            [cut_cols[index] for index in incumbent["active_cut_idx"]] or active,
            t_c,
            minimise="weld_joints",
            target_len=incumbent["used_len"],
            max_used_len=max_used,
            max_cut_types=incumbent["cut_types"],
        )
        return (phase_c if phase_c is not None else incumbent), None

    return None, last_fail


def _reconcile_to_counts(
    group: MaterialGroup, result: dict[str, Any]
) -> tuple[list[tuple[_WeldCandidate, int]], list[tuple[_CutCandidate, int]]]:
    """Drop surplus cut segments so verifier segment balance holds."""
    kerf = group.blade_margin
    weld_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for row in result["welds"]:
        weld_agg[(row["pipe"], tuple(row["parts"]))] += row["count"]
    weld_counts = [
        (_WeldCandidate(pipe_index, parts), quantity)
        for (pipe_index, parts), quantity in sorted(weld_agg.items())
    ]

    produced_cnt: dict[int, int] = defaultdict(int)
    for row in result["cuts"]:
        for seg, count in row["counts"].items():
            produced_cnt[seg] += count * row["count"]
    consumed_cnt: dict[int, int] = defaultdict(int)
    for row in result["welds"]:
        for seg in row["parts"]:
            consumed_cnt[seg] += row["count"]
    surplus = {
        seg: produced_cnt[seg] - consumed_cnt.get(seg, 0)
        for seg in produced_cnt
        if produced_cnt[seg] - consumed_cnt.get(seg, 0) > 0
    }

    bar_instances: list[list[int]] = []
    stock_of_bar: list[int] = []
    for row in result["cuts"]:
        segs: list[int] = []
        for seg, count in row["counts"].items():
            segs.extend([seg] * count)
        for _ in range(row["count"]):
            bar_instances.append(list(segs))
            stock_of_bar.append(row["stock"])

    if surplus:
        by_seg_bars: dict[int, list[int]] = defaultdict(list)
        for bar_index, segs in enumerate(bar_instances):
            for seg in set(segs):
                if seg in surplus:
                    by_seg_bars[seg].append(bar_index)
        for seg, need in surplus.items():
            removed = 0
            for bar_index in by_seg_bars.get(seg, []):
                while removed < need and seg in bar_instances[bar_index]:
                    bar_instances[bar_index].remove(seg)
                    removed += 1
                if removed >= need:
                    break

    cut_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for bar_index, segs in enumerate(bar_instances):
        if not segs:
            continue
        cut_agg[(stock_of_bar[bar_index], tuple(sorted(segs)))] += 1
    cut_counts = [
        (_CutCandidate(stock_length, parts, kerf, group.kerf_mode), quantity)
        for (stock_length, parts), quantity in sorted(cut_agg.items())
    ]
    return weld_counts, cut_counts


def solve_group(group: MaterialGroup, time_limit: float) -> dict[str, Any] | None:
    """Solve one material group.

    ``time_limit`` is an input condition from the caller. Returns a production
    schema result or ``None`` when unsolved. Never raises, never mutates group.
    """
    started = time.monotonic()
    if time_limit <= 0 or not math.isfinite(time_limit):
        logger.warning("global engine rejected non-positive time_limit=%r", time_limit)
        return None

    try:
        from pyscipopt import Model  # noqa: F401
    except Exception:
        return None

    diagnosis = _precheck(group)
    if diagnosis is not None:
        logger.info("global precheck %s for %s/%s", diagnosis, group.material, group.specifications)
        return None

    try:
        result, fail_code = _solve_phases(group, float(time_limit))
    except ValueError as exc:
        message = str(exc)
        if message.startswith("A_PROCESS_INFEASIBLE"):
            logger.info("%s", message)
            return None
        logger.warning("global candidate failure: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "global engine failed for %s/%s: %s",
            group.material,
            group.specifications,
            exc,
        )
        return None

    if result is None:
        logger.info(
            "global unsolved (%s) for %s/%s",
            fail_code or "C_TIMEOUT",
            group.material,
            group.specifications,
        )
        return None

    weld_counts, cut_counts = _reconcile_to_counts(group, result)
    if not weld_counts or not cut_counts:
        return None

    produced_pipes: dict[int, int] = defaultdict(int)
    for candidate, quantity in weld_counts:
        produced_pipes[candidate.pipe_index] += quantity
    for pipe_index, pipe in enumerate(group.pipes):
        if produced_pipes.get(pipe_index, 0) != pipe.demand:
            return None

    assembled = _assemble_group_result(
        group,
        "SCIP-GLOBAL-V1",
        "GLOBAL_V1",
        weld_counts,
        cut_counts,
        time.monotonic() - started,
    )
    util = float(assembled["metrics"]["utilization_rate"])
    assembled["metrics"]["solve_status"] = (
        "GLOBAL_TARGET_REACHED"
        if util + 1e-12 >= UTIL_FLOOR and assembled["metrics"]["target_reached"]
        else "GLOBAL_FEASIBLE"
        if util + 1e-12 >= UTIL_FLOOR
        else "GLOBAL_BELOW_UTIL_FLOOR"
    )
    assembled["metrics"]["util_floor"] = UTIL_FLOOR
    assembled["metrics"]["time_limit_seconds"] = float(time_limit)
    assembled["warnings"].append(
        "GLOBAL_JOINT: phased cut+weld ILP (util floor + pattern compression)"
    )
    if util + 1e-12 < UTIL_FLOOR:
        # Spec: util floor is a delivery gate; do not emit sub-floor solutions.
        return None
    return assembled


__all__ = ["solve_group", "UTIL_FLOOR", "PHASE_B_SLACK"]
