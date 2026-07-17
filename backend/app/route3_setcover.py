"""Route-3: global set-covering ILP that jointly optimises cutting and welding.

This is the productionised form of the ``_poc_setcover_ilp_v2`` research script.
Unlike the baseline (per-pipe fixed weld templates) it lets *which weld pattern
each pipe uses* be a decision variable, with segment lengths taking any legal
value at legal weld positions.  The cut side and weld side are coupled by
segment balance (produced >= consumed), and a two-phase lexicographic objective
first locks utilisation (min total stock length) then minimises distinct cut
pattern types (then weld pattern types) -- directly targeting the shop's real
pain point of "too many cutting/welding patterns".

Design and validation: docs/research/GPU可插拔预解器设计方案.md §11.26-§11.29.

Entry point ``solve_group(group, time_limit)`` mirrors ``route2_equiv`` so the
service can run it as an opt-in engine and keep the better result.  It returns a
result dict assembled by the shared ``_assemble_group_result`` (so metrics /
schema / verifier are identical to every other engine), or ``None`` when it
finds no solution within the budget.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .domain import MaterialGroup, PipeDemand
from .solver import (
    _CutCandidate,
    _WeldCandidate,
    _assemble_group_result,
    _legal_pattern,
)

# Candidate-generation knobs, calibrated on the four hardest real samples
# (§11.27): max_pieces=4 is the feasibility key for very tight groups with many
# short pipes; splits/max_trim/col_cap keep the column pool bounded.
DEFAULT_SPLITS = 4
DEFAULT_MAX_PIECES = 4
DEFAULT_COL_CAP = 30_000
DEFAULT_MAX_TRIM_UNITS = 120


# ---------------------------------------------------------------------------
# Step 1: candidate weld patterns per pipe (the DECISION set) + shared alphabet.
# ---------------------------------------------------------------------------


def _weld_positions(pipe: PipeDemand, group: MaterialGroup, max_stock: int, want: int) -> list[int]:
    length = pipe.length
    stock_lengths = sorted({s.length for s in group.stocks})
    raw: set[int] = set()
    for sl in stock_lengths:
        raw.add(sl)
        raw.add(length - sl)
        raw.add(sl - group.blade_margin)
        raw.add(length - sl + group.blade_margin)
    for t in range(1, want + 1):
        raw.add(length * t // (want + 1))
    legal: list[int] = []
    for k in sorted(raw):
        if not (0 < k < length):
            continue
        parts = (k, length - k)
        if max(parts) > max_stock:
            continue
        if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
            legal.append(k)
    return legal


def _build_weld_candidates(
    group: MaterialGroup, splits: int
) -> tuple[dict[int, list[tuple[int, ...]]], set[int]]:
    max_stock = max(s.length for s in group.stocks)
    cands: dict[int, list[tuple[int, ...]]] = {}
    alphabet: set[int] = set()
    for i, pipe in enumerate(group.pipes):
        pats: set[tuple[int, ...]] = set()
        if pipe.length <= max_stock:
            pats.add((pipe.length,))
        if pipe.max_joints >= 1:
            positions = _weld_positions(pipe, group, max_stock, splits)
            if len(positions) > splits:
                step = len(positions) / splits
                positions = [positions[int(j * step)] for j in range(splits)]
            for k in positions:
                pats.add((k, pipe.length - k))
        if pipe.max_joints >= 2:
            positions = _weld_positions(pipe, group, max_stock, splits)
            picks = positions[: min(4, len(positions))]
            for a in picks:
                for b in picks:
                    if b <= a:
                        continue
                    parts = (a, b - a, pipe.length - b)
                    if max(parts) > max_stock:
                        continue
                    if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
                        pats.add(parts)
        if not pats:
            raise ValueError(f"no legal weld pattern for pipe {pipe.pipe_id} (L={pipe.length})")
        cands[i] = sorted(pats, key=lambda p: (len(p), p))
        for p in cands[i]:
            alphabet.update(p)
    return cands, alphabet


# ---------------------------------------------------------------------------
# Step 2: enumerate cutting-pattern columns over the shared alphabet.
# ---------------------------------------------------------------------------


def _enumerate_cut_columns(
    seg_lengths: list[int],
    stock_lengths: list[int],
    kerf: int,
    col_cap: int,
    max_trim: int,
    max_pieces: int,
) -> list[dict[str, Any]]:
    seg_lengths = sorted(set(seg_lengths), reverse=True)
    smallest = seg_lengths[-1] if seg_lengths else 0
    columns: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[tuple[int, int], ...]]] = set()

    for L in sorted(set(stock_lengths)):
        counts: dict[int, int] = {}
        budget = [col_cap]

        def dfs(idx: int, used: int, n_pieces: int) -> None:
            if budget[0] <= 0:
                return
            remnant = L - used
            if n_pieces >= 1 and remnant <= max_trim:
                key = (L, tuple(sorted(counts.items())))
                if key not in seen:
                    seen.add(key)
                    columns.append(
                        {"stock": L, "counts": dict(counts), "used": used, "remnant": remnant}
                    )
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return
            if remnant < smallest or n_pieces >= max_pieces:
                return
            for j in range(idx, len(seg_lengths)):
                seg = seg_lengths[j]
                extra = seg + (kerf if n_pieces >= 1 else 0)
                if used + extra > L:
                    continue
                counts[seg] = counts.get(seg, 0) + 1
                dfs(j, used + extra, n_pieces + 1)
                counts[seg] -= 1
                if counts[seg] == 0:
                    del counts[seg]
                if budget[0] <= 0:
                    return

        dfs(0, 0, 0)
    return columns


def _ensure_coverage(
    columns: list[dict[str, Any]],
    seg_lengths: list[int],
    stock_lengths: list[int],
    kerf: int,
    max_pieces: int,
) -> list[dict[str, Any]]:
    produced = {seg for col in columns for seg in col["counts"]}
    missing = [s for s in seg_lengths if s not in produced]
    if not missing:
        return columns
    segs_desc = sorted(set(seg_lengths), reverse=True)
    max_stock = max(stock_lengths)
    existing = {(c["stock"], tuple(sorted(c["counts"].items()))) for c in columns}
    for seg in missing:
        L = min((s for s in sorted(set(stock_lengths)) if s >= seg), default=max_stock)
        counts: dict[int, int] = {seg: 1}
        used = seg
        n_pieces = 1
        for s in segs_desc:
            while n_pieces < max_pieces and used + s + kerf <= L:
                counts[s] = counts.get(s, 0) + 1
                used += s + kerf
                n_pieces += 1
        key = (L, tuple(sorted(counts.items())))
        if key in existing:
            continue
        existing.add(key)
        columns.append(
            {"stock": L, "counts": dict(counts), "used": used, "remnant": L - used}
        )
    return columns


# ---------------------------------------------------------------------------
# Step 3: joint cut+weld set-covering ILP (two-phase lexicographic).
# ---------------------------------------------------------------------------


def _solve_joint(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
    time_limit: float,
    target_len: int | None = None,
    minimise: str = "length",
) -> dict[str, Any] | None:
    from pyscipopt import Model, quicksum

    m = Model("route3_setcover")
    m.setParam("limits/time", max(0.05, time_limit))
    m.hideOutput()

    n = len(cut_cols)
    bars_by_len: dict[int, int] = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity

    u: dict[tuple[int, int], Any] = {}
    for i, pats in weld_cands.items():
        for wi in range(len(pats)):
            u[(i, wi)] = m.addVar(vtype="I", lb=0, name=f"u_{i}_{wi}")
        m.addCons(quicksum(u[(i, wi)] for wi in range(len(pats))) == group.pipes[i].demand)

    x = {p: m.addVar(vtype="I", lb=0, name=f"x{p}") for p in range(n)}

    cols_by_len: dict[int, list[int]] = defaultdict(list)
    for p, col in enumerate(cut_cols):
        cols_by_len[col["stock"]].append(p)
    for L, plist in cols_by_len.items():
        m.addCons(quicksum(x[p] for p in plist) <= bars_by_len[L])

    produced: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for p, col in enumerate(cut_cols):
        for seg, c in col["counts"].items():
            produced[seg].append((c, x[p]))
    consumed: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for i, pats in weld_cands.items():
        for wi, parts in enumerate(pats):
            cnt: dict[int, int] = defaultdict(int)
            for seg in parts:
                cnt[seg] += 1
            for seg, c in cnt.items():
                consumed[seg].append((c, u[(i, wi)]))

    for seg in set(produced) | set(consumed):
        lhs = quicksum(c * var for c, var in produced.get(seg, []))
        rhs = quicksum(c * var for c, var in consumed.get(seg, []))
        m.addCons(lhs >= rhs)

    length_term = quicksum(cut_cols[p]["stock"] * x[p] for p in range(n))

    if minimise == "cut_types":
        if target_len is None:
            raise ValueError("cut_types objective requires target_len")
        m.addCons(length_term <= target_len)
        y = {p: m.addVar(vtype="B", name=f"y{p}") for p in range(n)}
        for p in range(n):
            m.addCons(x[p] <= bars_by_len[cut_cols[p]["stock"]] * y[p])
        wtype_vars: dict[tuple[int, ...], list[Any]] = defaultdict(list)
        for i, pats in weld_cands.items():
            for wi, parts in enumerate(pats):
                wtype_vars[tuple(parts)].append(u[(i, wi)])
        z = {t: m.addVar(vtype="B", name=f"z{k}") for k, t in enumerate(wtype_vars)}
        bigM = sum(group.pipes[i].demand for i in range(len(group.pipes)))
        for t, vars_ in wtype_vars.items():
            m.addCons(quicksum(vars_) <= bigM * z[t])
        lex_cut_w = len(wtype_vars) + 1
        m.setObjective(
            lex_cut_w * quicksum(y[p] for p in range(n))
            + quicksum(z[t] for t in wtype_vars),
            "minimize",
        )
    else:
        if target_len is not None:
            m.addCons(length_term <= target_len)
        m.setObjective(length_term, "minimize")

    m.optimize()
    if m.getNSols() == 0:
        return None

    used_cuts: list[dict[str, Any]] = []
    active_cut_idx: list[int] = []
    used_len = 0
    for p in range(n):
        xv = round(m.getVal(x[p]))
        if xv > 0:
            used_cuts.append({**cut_cols[p], "count": xv})
            active_cut_idx.append(p)
            used_len += cut_cols[p]["stock"] * xv
    used_welds: list[dict[str, Any]] = []
    for (i, wi), var in u.items():
        uv = round(m.getVal(var))
        if uv > 0:
            used_welds.append({"pipe": i, "parts": weld_cands[i][wi], "count": uv})
    return {
        "status": str(m.getStatus()).upper(),
        "used_len": used_len,
        "cuts": used_cuts,
        "welds": used_welds,
        "active_cut_idx": active_cut_idx,
    }


def _solve_two_phase(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
    time_p1: float,
    time_p2: float,
) -> dict[str, Any] | None:
    p1 = _solve_joint(group, weld_cands, cut_cols, time_p1, minimise="length")
    if p1 is None:
        return None
    # Phase 2 pool = phase-1 active columns only: keeps the big-M type-count MIP
    # small enough to solve to optimality (§11.29).  A wider pool times out and
    # the fallback is worse.
    active = [cut_cols[i] for i in p1["active_cut_idx"]]
    p2 = _solve_joint(
        group, weld_cands, active, time_p2, target_len=p1["used_len"], minimise="cut_types"
    )
    return p2 if p2 is not None else p1


# ---------------------------------------------------------------------------
# Step 4: convert the aggregated ILP solution into weld/cut counts and assemble.
# ---------------------------------------------------------------------------


def _reconcile_to_counts(
    group: MaterialGroup, res: dict[str, Any]
) -> tuple[list[tuple[_WeldCandidate, int]], list[tuple[_CutCandidate, int]]]:
    """Turn the aggregated ILP solution into (_WeldCandidate, qty) and
    (_CutCandidate, qty) lists, reconciling produced>=consumed surplus into trim.

    The ILP allows a segment to be cut but welded into nothing (produced >
    consumed).  Physically that means "cut one fewer piece, leave a longer
    remnant".  We drop surplus segments from cut columns so every remaining cut
    segment is welded => verifier segment balance (produced == consumed) holds.
    (Carrying the remnant to a later batch is a future enhancement, §11.28.)
    """
    kerf = group.blade_margin

    weld_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for w in res["welds"]:
        weld_agg[(w["pipe"], tuple(w["parts"]))] += w["count"]
    weld_counts = [
        (_WeldCandidate(pipe_index, parts), qty)
        for (pipe_index, parts), qty in sorted(weld_agg.items())
    ]

    produced_cnt: dict[int, int] = defaultdict(int)
    for c in res["cuts"]:
        for seg, nseg in c["counts"].items():
            produced_cnt[seg] += nseg * c["count"]
    consumed_cnt: dict[int, int] = defaultdict(int)
    for w in res["welds"]:
        for seg in w["parts"]:
            consumed_cnt[seg] += w["count"]
    surplus: dict[int, int] = {
        seg: produced_cnt[seg] - consumed_cnt.get(seg, 0)
        for seg in produced_cnt
        if produced_cnt[seg] - consumed_cnt.get(seg, 0) > 0
    }

    bar_instances: list[list[int]] = []
    stock_of_bar: list[int] = []
    for c in res["cuts"]:
        segs: list[int] = []
        for seg, nseg in c["counts"].items():
            segs.extend([seg] * nseg)
        for _ in range(c["count"]):
            bar_instances.append(list(segs))
            stock_of_bar.append(c["stock"])

    if surplus:
        by_seg_bars: dict[int, list[int]] = defaultdict(list)
        for bi, segs in enumerate(bar_instances):
            for seg in set(segs):
                if seg in surplus:
                    by_seg_bars[seg].append(bi)
        for seg, need in surplus.items():
            removed = 0
            for bi in by_seg_bars.get(seg, []):
                while removed < need and seg in bar_instances[bi]:
                    bar_instances[bi].remove(seg)
                    removed += 1
                if removed >= need:
                    break

    cut_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for bi, segs in enumerate(bar_instances):
        if not segs:
            continue
        cut_agg[(stock_of_bar[bi], tuple(sorted(segs)))] += 1
    cut_counts = [
        (_CutCandidate(stock_length, parts, kerf, group.kerf_mode), qty)
        for (stock_length, parts), qty in sorted(cut_agg.items())
    ]
    return weld_counts, cut_counts


def solve_group(group: MaterialGroup, time_limit: float) -> dict[str, Any] | None:
    """Solve one material group with the global set-covering ILP.

    Returns a result dict (same schema as the baseline via
    ``_assemble_group_result``) or ``None`` if no solution is found or PySCIPOpt
    is unavailable.  Never raises, never mutates ``group``.
    """
    import time as _time

    started = _time.monotonic()
    try:
        from pyscipopt import Model  # noqa: F401
    except Exception:
        return None

    try:
        weld_cands, alphabet = _build_weld_candidates(group, DEFAULT_SPLITS)
        stock_lengths = [s.length for s in group.stocks]
        kerf = group.blade_margin
        cols = _enumerate_cut_columns(
            sorted(alphabet), stock_lengths, kerf,
            DEFAULT_COL_CAP, DEFAULT_MAX_TRIM_UNITS, DEFAULT_MAX_PIECES,
        )
        cols = _ensure_coverage(cols, sorted(alphabet), stock_lengths, kerf, DEFAULT_MAX_PIECES)
        if not cols:
            return None
        res = _solve_two_phase(
            group, weld_cands, cols, time_limit / 2, time_limit / 2
        )
    except Exception:
        return None
    if res is None:
        return None

    weld_counts, cut_counts = _reconcile_to_counts(group, res)
    if not weld_counts or not cut_counts:
        return None

    # Reject partial solutions: every pipe's demand must be met by the weld side.
    produced_pipes: dict[int, int] = defaultdict(int)
    for cand, qty in weld_counts:
        produced_pipes[cand.pipe_index] += qty
    for i, pipe in enumerate(group.pipes):
        if produced_pipes.get(i, 0) != pipe.demand:
            return None

    result = _assemble_group_result(
        group, "SCIP-SETCOVER-V2", "SETCOVER_V2", weld_counts, cut_counts,
        _time.monotonic() - started,
    )
    result["metrics"]["solve_status"] = (
        "SETCOVER_TARGET_REACHED"
        if result["metrics"]["target_reached"]
        else "SETCOVER_FEASIBLE"
    )
    result["warnings"].append("ROUTE3_SETCOVER: global cut+weld ILP (two-phase lexicographic)")
    return result


__all__ = ["solve_group"]
