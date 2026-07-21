"""Route-B: Row-and-Column Generation (R&CG) for the two-stage cut+weld problem.

This is the productionised form of ``docs/research/route-b-rcg-design.md``.  It
treats the nesting problem as a genuine *two-stage* cutting-stock problem:

* **Stage 1 (pipe splitting)** decomposes each finished pipe into welded segments
  at legal (forbidden-zone-avoiding) positions.  The segment lengths it emits are
  the *intermediate orders* -- the shared alphabet ``Sigma``.
* **Stage 2 (stock cutting)** packs stock bars to produce those segments,
  chasing utilisation.

The two stages are coupled inside a single Relaxed Short and Restricted Master
Problem (RSRMP).  We generate **columns** (stage-1 weld patterns and stage-2 cut
patterns) *and* **rows** (new segment specs / intermediate orders) on demand,
driven by LP dual prices, until no negative-reduced-cost candidate remains.  This
is the Column-Dependent-Rows treatment from Grieco et al. 2024 and is what makes
the two stages converge without the oscillation a naive feedback loop suffers.

Entry point ``solve_group(group, time_limit)`` mirrors ``route3_setcover`` /
``route2_equiv`` so the service can run it as an opt-in engine and keep the
better result.  It returns a result dict assembled by the shared
``_assemble_group_result`` (identical schema / metrics / verifier contract) or
``None`` when it finds no solution within the budget.  It never raises and never
mutates ``group``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import logging
import time
from typing import Any, Sequence

from .domain import MaterialGroup, PipeDemand
from .solver import (
    _CutCandidate,
    _WeldCandidate,
    _assemble_group_result,
    _baseline_weld_patterns,
    _carved_weld_patterns,
    _generate_cut_candidates,
    _legal_pattern,
    _standard_weld_candidates,
)

_LOG = logging.getLogger(__name__)

import os as _os


def _trace(msg: str) -> None:
    if _os.environ.get("RCG_TRACE"):
        print(f"[RCG] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Tuning knobs.  Calibrated to stay well inside a per-group time budget while
# leaving enough pricing rounds for the alphabet to stabilise.
# ---------------------------------------------------------------------------
MAX_PRICING_ROUNDS = 40          # hard ceiling on RSRMP re-solves
STAGE1_COLS_PER_ROUND = 8        # new stage-1 patterns priced in per round
STAGE2_COLS_PER_ROUND = 12       # new stage-2 patterns priced in per round
RC_EPS = 1e-6                    # reduced-cost negativity threshold
IMPROVE_EPS = 1e-4               # relative-improvement floor before phase switch
PATTERN_STALL_LIMIT = 3          # column-gen rounds w/o improvement -> row-gen
WELD_PENALTY = 1.0               # cost per weld joint in the stage-1 objective
PATTERN_PENALTY = 50.0           # setup cost per distinct segment spec (Sigma)

# Scale guards.  R&CG's LP re-solves and the final integer MILP grow with the
# column pool and the integer demand coefficients; past these sizes column
# generation cannot converge inside a per-group budget, so we bail out fast and
# let the caller fall back to the baseline engine (which never regresses).
MAX_TOTAL_DEMAND = 200           # sum of pipe demands the master can handle
MAX_COLUMN_POOL = 2500           # combined stage1+stage2 columns before bail-out
# Small-demand tight-packing groups benefit most from the carved-pattern
# injection, which enlarges the pool.  Give them a higher ceiling so the extra
# high-utilisation columns are not discarded by the generic size guard.
SMALL_DEMAND_THRESHOLD = 200     # total demand under which the relaxed cap applies
MAX_COLUMN_POOL_SMALL = 4000     # relaxed pool ceiling for small-demand groups


@dataclass
class _Stage1Col:
    """A stage-1 weld pattern: pipe i decomposed into welded segments."""

    pipe_index: int
    parts: tuple[int, ...]

    @property
    def joints(self) -> int:
        return len(self.parts) - 1


@dataclass
class _Stage2Col:
    """A stage-2 cut pattern: one stock bar cut into segments."""

    stock_length: int
    parts: tuple[int, ...]  # sorted; segments produced from this bar

    @property
    def trim(self) -> int:
        return self.stock_length - sum(self.parts)


# ---------------------------------------------------------------------------
# Legal weld-position enumeration (respects forbidden zones + process limits).
# ---------------------------------------------------------------------------


def _legal_split_positions(pipe: PipeDemand, group: MaterialGroup, max_stock: int) -> list[int]:
    """Every legal single-weld position, i.e. a cut into (k, L-k) that avoids
    forbidden zones and fits the stock.  Used to seed and to expand stage-1."""
    length = pipe.length
    positions: list[int] = []
    for k in range(1, length):
        if k > max_stock or length - k > max_stock:
            continue
        if _legal_pattern(pipe, (k, length - k), group.min_weld_distance, group.min_cut_length):
            positions.append(k)
    return positions


def _seed_stage1(group: MaterialGroup, max_stock: int) -> dict[int, list[tuple[int, ...]]]:
    """Initial stage-1 columns using the shared standard-segment machinery.

    This directly implements the corrected Stage-1 objective (see
    ``route-b-rcg-design.md`` §4 / ``route-a-decoupled-feedback-design.md``):
    NOT per-pipe greedy longest, but a *joint* small alphabet of standard segment
    lengths shared across pipes, chosen under forbidden-zone constraints.  We
    reuse ``_baseline_weld_patterns`` (guarantees each pipe has a feasible
    decomposition) plus ``_standard_weld_candidates`` (the group-level standard
    segments that minimise distinct patterns).  Row generation later widens this
    alphabet only when the dual prices justify it.
    """
    seeds: dict[int, list[tuple[int, ...]]] = {i: [] for i in range(len(group.pipes))}
    for i, pipe in enumerate(group.pipes):
        for parts in sorted(_baseline_weld_patterns(pipe, group, max_stock)):
            seeds[i].append(parts)
    existing: set[tuple[int, tuple[int, ...]]] = {
        (i, parts) for i, plist in seeds.items() for parts in plist
    }
    for cand in _standard_weld_candidates(group, existing, per_pipe_cap=8):
        seeds[cand.pipe_index].append(cand.parts)
    for i, pipe in enumerate(group.pipes):
        if not seeds[i]:
            # last resort: a legal two-weld decomposition
            seeds[i].extend(_seed_two_weld(pipe, group, max_stock))
        if not seeds[i]:
            raise ValueError(f"no legal stage-1 pattern for pipe {pipe.pipe_id}")
        seeds[i] = sorted(set(seeds[i]), key=lambda p: (len(p), p))
    return seeds


def _seed_two_weld(pipe: PipeDemand, group: MaterialGroup, max_stock: int) -> list[tuple[int, ...]]:
    positions = _legal_split_positions(pipe, group, max_stock)
    out: list[tuple[int, ...]] = []
    for a in positions:
        for b in positions:
            if b <= a:
                continue
            parts = (a, b - a, pipe.length - b)
            if max(parts) > max_stock:
                continue
            if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
                out.append(parts)
                if len(out) >= 4:
                    return out
    return out


# ---------------------------------------------------------------------------
# Pricing subproblem 1: stage-1 weld patterns (forbidden-zone DP).
# Reduced cost of a pattern p for pipe i:
#   rc(p) = c_p - mu_i - sum_k pi_k * a1_{k,p}
# where c_p = WELD_PENALTY*joints (+ PATTERN_PENALTY handled at alphabet level),
# a1 are the produced-segment counts, pi_k the segment-conservation duals.
# We look for the pattern MAXIMISING (mu_i + sum pi_k * count_k - c_p), i.e. the
# most negative reduced cost, over legal decompositions using segments in Sigma.
# ---------------------------------------------------------------------------


def _price_stage1(
    pipe: PipeDemand,
    pipe_index: int,
    group: MaterialGroup,
    sigma: Sequence[int],
    pi: dict[int, float],
    mu: float,
    max_stock: int,
    limit: int,
) -> list[tuple[float, tuple[int, ...]]]:
    """Return up to ``limit`` (reduced_cost, parts) with rc < -RC_EPS.

    DP over cut positions: f[pos] = best (value, path) reaching ``pos`` with the
    last weld at ``pos``.  A segment (prev, pos] must have length in ``sigma``,
    fit a stock, and ``pos`` (if internal) must be a legal weld position.
    """
    length = pipe.length
    sigma_set = set(s for s in sigma if 0 < s <= max_stock)
    if not sigma_set:
        return []
    # DP: reach exact position `pos` as a sum of sigma segments, welds legal.
    # best_value[pos] = max sum of pi over segments used to reach pos.
    NEG = float("-inf")
    best_value: dict[int, float] = {0: 0.0}
    best_path: dict[int, tuple[int, ...]] = {0: ()}
    # iterate positions in increasing order
    reachable = [0]
    seen = {0}
    idx = 0
    while idx < len(reachable):
        pos = reachable[idx]
        idx += 1
        base_val = best_value[pos]
        base_path = best_path[pos]
        for seg in sigma_set:
            npos = pos + seg
            if npos > length:
                continue
            joints_so_far = len(base_path)  # welds added before this segment's end
            if joints_so_far > pipe.max_joints:
                continue
            # internal weld at npos must be legal; npos == length is the pipe end
            if npos < length and not pipe.weld_allowed(npos):
                continue
            if npos < length and group.min_weld_distance > 0 and base_path and seg < group.min_weld_distance:
                continue
            cand_val = base_val + pi.get(seg, 0.0)
            if cand_val > best_value.get(npos, NEG) + 1e-12:
                best_value[npos] = cand_val
                best_path[npos] = base_path + (seg,)
                if npos not in seen:
                    seen.add(npos)
                    reachable.append(npos)
    if length not in best_path:
        return []
    parts = best_path[length]
    if not parts or sum(parts) != length:
        return []
    if not _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
        return []
    joints = len(parts) - 1
    seg_dual = sum(pi.get(s, 0.0) for s in parts)
    c_p = WELD_PENALTY * joints
    rc = c_p - mu - seg_dual
    if rc < -RC_EPS:
        return [(rc, tuple(parts))]
    return []


# ---------------------------------------------------------------------------
# Pricing subproblem 2: stage-2 cut patterns (bounded knapsack DP).
# Reduced cost of cut column q:
#   rc(q) = c_q + sum_k pi_k * a2_{k,q} + sigma_stock
# a2 = -count (consumes segments), so we MAXIMISE sum pi_k*count_k - trim_cost.
# Objective on the util side: minimise total stock used == pick columns that pack
# high-dual segments tightly.  We enumerate best-fill patterns per stock length.
# ---------------------------------------------------------------------------


def _price_stage2(
    group: MaterialGroup,
    sigma: Sequence[int],
    pi: dict[int, float],
    stock_dual: dict[int, float],
    limit: int,
) -> list[tuple[float, int, tuple[int, ...]]]:
    """Return up to ``limit`` (reduced_cost, stock_length, parts) with rc<-eps.

    For each stock length L run an unbounded-knapsack DP maximising the packed
    dual value sum(pi_k) over segments in Sigma that fit L (with kerf).  The
    stage-2 column cost is the trim loss (utilisation objective), so
    rc = trim_value_cost - packed_dual + stock_dual.
    """
    kerf = group.blade_margin
    segs = sorted(s for s in set(sigma) if s > 0)
    out: list[tuple[float, int, tuple[int, ...]]] = []
    for stock in group.stocks:
        L = stock.length
        fit = [s for s in segs if s <= L]
        if not fit:
            continue
        # Unbounded knapsack over capacity: value[c] = max packed dual using a
        # bar prefix of length c; from[c] records the last segment placed so the
        # pattern can be reconstructed.  Kerf is charged between adjacent parts.
        value = [0.0] * (L + 1)
        from_seg = [-1] * (L + 1)
        for c in range(1, L + 1):
            value[c] = value[c - 1]  # allow a unit of trim (carry best-so-far)
            from_seg[c] = from_seg[c - 1]
            for seg in fit:
                # placing this seg needs seg (+kerf if it follows another part)
                prev = c - seg
                if prev < 0:
                    continue
                # kerf applies when the bar already holds >=1 part before `prev`
                need_prev = prev - (kerf if prev > 0 and from_seg[prev] != -1 else 0)
                if need_prev < 0:
                    continue
                base = value[prev]
                v = base + pi.get(seg, 0.0)
                if v > value[c] + 1e-12:
                    value[c] = v
                    from_seg[c] = seg
        # reconstruct greedily from full capacity down
        parts: list[int] = []
        c = L
        guard = 0
        while c > 0 and from_seg[c] != -1 and guard <= L:
            guard += 1
            seg = from_seg[c]
            parts.append(seg)
            c -= seg + (kerf if c - seg > 0 and parts else 0)
        if not parts:
            continue
        parts.sort()
        packed_dual = sum(pi.get(s, 0.0) for s in parts)
        c_q = float(L)
        rc = c_q - packed_dual + stock_dual.get(L, 0.0)
        if rc < -RC_EPS:
            out.append((rc, L, tuple(parts)))
    out.sort(key=lambda t: t[0])
    return out[:limit]


# ---------------------------------------------------------------------------
# Row generation (subproblem 3): introduce a new segment spec into Sigma.
# When neither stage prices in an improving column, inspect the worst-trim stage-2
# columns and propose a new segment length that (a) is legally weldable on some
# pipe and (b) tiles a stock more tightly.  Return new segments to add to Sigma.
# ---------------------------------------------------------------------------


def _generate_new_segments(
    group: MaterialGroup,
    sigma: set[int],
    max_stock: int,
    forbidden_segs: set[int],
) -> list[int]:
    """Propose new segment lengths (intermediate orders) to widen Sigma.

    Heuristic: for each stock length, the residual trim after tiling with the
    current alphabet suggests a segment equal to that residual (if it is legally
    weldable on some pipe and not already present/forbidden)."""
    proposals: set[int] = set()
    weldable = _weldable_lengths(group, max_stock)
    for stock in group.stocks:
        L = stock.length
        # exact divisors / complements of L against existing segments
        for s in list(sigma):
            r = L - s
            if 0 < r <= max_stock and r in weldable and r not in sigma and r not in forbidden_segs:
                proposals.add(r)
        # whole-bar segment
        if L in weldable and L not in sigma and L not in forbidden_segs:
            proposals.add(L)
    # cap: add the few largest legal proposals (fewer, longer segments preferred)
    return sorted(proposals, reverse=True)[:4]


def _weldable_lengths(group: MaterialGroup, max_stock: int) -> set[int]:
    """Set of segment lengths that appear in at least one legal decomposition of
    some pipe -- i.e. lengths that Stage 1 can actually produce."""
    lengths: set[int] = set()
    for pipe in group.pipes:
        if pipe.length <= max_stock:
            lengths.add(pipe.length)
        if pipe.max_joints >= 1:
            for k in _legal_split_positions(pipe, group, max_stock):
                lengths.add(k)
                lengths.add(pipe.length - k)
    return {s for s in lengths if 0 < s <= max_stock}


# ---------------------------------------------------------------------------
# The RSRMP master (LP relaxation for pricing, ILP for the final solve).
# ---------------------------------------------------------------------------


def _solve_master(
    group: MaterialGroup,
    stage1: list[_Stage1Col],
    stage2: list[_Stage2Col],
    *,
    integer: bool,
    time_limit: float,
    phase1: bool = False,
) -> dict[str, Any] | None:
    """Solve the RSRMP over the current column pools.

    Constraints:
      (demand_i)  sum_p x_p [pattern p decomposes pipe i] == demand_i
      (seg_k)     sum_q a2_{k,q} y_q - sum_p a1_{k,p} x_p >= 0   (produced>=consumed)
      (stock_L)   sum_q [q uses stock L] y_q <= supply_L
    Objective (weighted lexicographic): minimise
      W_UTIL * sum_q L_q y_q  +  WELD_PENALTY * sum_p joints_p x_p
    Utilisation dominates (large weight); welds break ties.

    When ``phase1`` is True, big-M artificial variables are added to the demand
    and segment rows so the LP is always feasible; this yields duals that drive
    column generation even before the pool can produce a real feasible solution.
    The artificial usage is reported as ``infeasibility`` so the caller can tell
    whether a *real* feasible solution has been reached.  Returns duals when
    ``integer`` is False, else the integer column multiplicities.
    """
    try:
        from pyscipopt import Model, quicksum
    except Exception:
        return None

    model = Model("rcg_rsrmp")
    model.hideOutput(True)
    model.setParam("limits/time", max(0.1, time_limit))

    vtype = "I" if integer else "C"
    x = [model.addVar(vtype=vtype, lb=0, name=f"x{p}") for p in range(len(stage1))]
    y = [model.addVar(vtype=vtype, lb=0, name=f"y{q}") for q in range(len(stage2))]

    BIG_M = 1e7
    art_terms = []  # (var) artificial variables to penalise

    # demand rows
    demand_cons = []
    for i, pipe in enumerate(group.pipes):
        expr = quicksum(x[p] for p, col in enumerate(stage1) if col.pipe_index == i)
        if phase1:
            # a_pos covers shortfall (produce fewer than demand), a_neg the excess
            a_pos = model.addVar(vtype="C", lb=0, name=f"ad_pos{i}")
            expr = expr + a_pos
            art_terms.append(a_pos)
        demand_cons.append(model.addCons(expr == pipe.demand, name=f"demand{i}"))

    # segment-conservation rows over the union of segments appearing in pools.
    # Stage 2 (cutting) PRODUCES segments; stage 1 (welding) CONSUMES them to
    # assemble finished pipes.  Balance: produced (stage2) >= consumed (stage1).
    seg_set: set[int] = set()
    for col in stage1:
        seg_set.update(col.parts)
    for col in stage2:
        seg_set.update(col.parts)
    seg_cons: dict[int, Any] = {}
    for seg in sorted(seg_set):
        produced = quicksum(
            y[q] * col.parts.count(seg) for q, col in enumerate(stage2) if seg in col.parts
        )
        consumed = quicksum(
            x[p] * col.parts.count(seg) for p, col in enumerate(stage1) if seg in col.parts
        )
        if phase1:
            # artificial "external segment supply" covers any consumed-but-not-
            # produced shortfall, keeping the row feasible for dual extraction.
            a_seg = model.addVar(vtype="C", lb=0, name=f"as{seg}")
            produced = produced + a_seg
            art_terms.append(a_seg)
        seg_cons[seg] = model.addCons(produced - consumed >= 0, name=f"seg{seg}")

    # stock supply rows
    supply = {s.length: s.quantity for s in group.stocks}
    stock_cons: dict[int, Any] = {}
    by_stock: dict[int, list[int]] = defaultdict(list)
    for q, col in enumerate(stage2):
        by_stock[col.stock_length].append(q)
    usage_by_length: dict[int, Any] = {}
    for L in sorted(supply):
        qs = by_stock.get(L, [])
        # No column uses stock length L -> its usage is a constant 0; skip the
        # supply row (0 <= supply is vacuous) but still record the constant so
        # the must-use split below stays a valid SCIP expression, never a bool.
        if not qs:
            usage_by_length[L] = None
            continue
        expr = quicksum(y[q] for q in qs)
        stock_cons[L] = model.addCons(expr <= supply.get(L, 0), name=f"stock{L}")
        usage_by_length[L] = expr

    # must-use inventory: mirror the baseline (solver.py) gating.  A binary
    # z_free opens *available* (non-mandatory) usage; turning it on forces the
    # mandatory inventory to be fully consumed first.  Small demand that fits
    # entirely inside mandatory stock is served with z_free=0 and never forced
    # to exhaust it.  Applied in both LP (relaxed) and integer solves so the
    # duals already reflect the constraint.
    must_use_total = sum(s.must_use_quantity for s in group.stocks)
    if must_use_total > 0:
        z_free = model.addVar(vtype="B", name="use_available_stock")
        used_must_terms = []
        for stock in group.stocks:
            L = stock.length
            avail = stock.quantity - stock.must_use_quantity
            u_len = model.addVar(
                vtype=vtype, lb=0, ub=stock.must_use_quantity, name=f"mu_{L}"
            )
            a_len = model.addVar(
                vtype=vtype, lb=0, ub=max(0, avail), name=f"av_{L}"
            )
            usage = usage_by_length.get(L)
            if usage is None:
                # no columns for this length -> it is never used; force both to 0
                model.addCons(u_len == 0, name=f"muzero{L}")
                model.addCons(a_len == 0, name=f"avzero{L}")
            else:
                model.addCons(u_len + a_len == usage, name=f"musplit{L}")
                if avail > 0:
                    model.addCons(a_len <= avail * z_free, name=f"avgate{L}")
                else:
                    model.addCons(a_len == 0, name=f"avzero{L}")
            used_must_terms.append(u_len)
        model.addCons(
            quicksum(used_must_terms) >= must_use_total * z_free, name="muexhaust"
        )

    W_UTIL = 1.0
    obj = (
        W_UTIL * quicksum(y[q] * stage2[q].stock_length for q in range(len(stage2)))
        + WELD_PENALTY * quicksum(x[p] * stage1[p].joints for p in range(len(stage1)))
    )
    if art_terms:
        obj = obj + BIG_M * quicksum(art_terms)
    model.setObjective(obj, "minimize")
    model.optimize()

    status = model.getStatus()
    if status not in ("optimal", "timelimit", "gaplimit", "bestsollimit"):
        return None
    if model.getNSols() == 0:
        return None

    result: dict[str, Any] = {"objective": model.getObjVal()}
    if art_terms:
        result["infeasibility"] = sum(model.getVal(a) for a in art_terms)
    else:
        result["infeasibility"] = 0.0
    if integer:
        result["x"] = [round(model.getVal(v)) for v in x]
        result["y"] = [round(model.getVal(v)) for v in y]
        return result

    # LP duals for pricing
    try:
        result["mu"] = [model.getDualsolLinear(c) for c in demand_cons]
        result["pi"] = {seg: model.getDualsolLinear(c) for seg, c in seg_cons.items()}
        result["stock_dual"] = {L: model.getDualsolLinear(c) for L, c in stock_cons.items()}
    except Exception:
        # SCIP only exposes duals for LPs without presolve transforms; if it
        # refuses, fall back to zero duals (pricing degrades to enumeration).
        result["mu"] = [0.0] * len(demand_cons)
        result["pi"] = {seg: 0.0 for seg in seg_cons}
        result["stock_dual"] = {L: 0.0 for L in stock_cons}
    return result


# ---------------------------------------------------------------------------
# Main R&CG loop.
# ---------------------------------------------------------------------------


def solve_group(group: MaterialGroup, time_limit: float) -> dict[str, Any] | None:
    """Solve one material group with Row-and-Column Generation.

    Returns a result dict (shared ``_assemble_group_result`` schema) or ``None``
    when no solution is found / PySCIPOpt is unavailable.  Never raises, never
    mutates ``group``.
    """
    started = time.monotonic()
    try:
        from pyscipopt import Model  # noqa: F401
    except Exception:
        return None

    try:
        return _run(group, time_limit, started)
    except Exception as exc:  # noqa: BLE001 - opt-in engine must never break request
        _LOG.warning(
            "route-b R&CG failed for group %s/%s: %s",
            group.material, group.specifications, exc,
        )
        return None


def _run(group: MaterialGroup, time_limit: float, started: float) -> dict[str, Any] | None:
    max_stock = max(s.length for s in group.stocks)
    deadline = started + time_limit

    # Scale guard: oversized groups cannot converge in budget -- bail out fast so
    # the caller falls back to baseline instead of burning the whole time limit.
    total_demand = sum(pipe.demand for pipe in group.pipes)
    if total_demand > MAX_TOTAL_DEMAND:
        return None

    # Relax the pool ceiling for small-demand groups so the carved-pattern
    # injection (below) is not discarded by the generic size guard.
    pool_cap = (
        MAX_COLUMN_POOL_SMALL
        if total_demand <= SMALL_DEMAND_THRESHOLD
        else MAX_COLUMN_POOL
    )

    # --- initialise column pools + alphabet Sigma ---
    seeds = _seed_stage1(group, max_stock)
    stage1: list[_Stage1Col] = []
    sigma: set[int] = set()
    for i, pats in seeds.items():
        for parts in pats:
            stage1.append(_Stage1Col(i, parts))
            sigma.update(parts)
    stage2: list[_Stage2Col] = []
    # Seed stage-2 with real composite cut columns over the alphabet (single-
    # segment columns alone cannot pack multi-segment pipes into limited stock).
    seed_welds = [_WeldCandidate(c.pipe_index, c.parts) for c in stage1]
    for cut in _generate_cut_candidates(group, seed_welds):
        stage2.append(_Stage2Col(cut.stock_length, tuple(sorted(cut.parts))))
    for L in sorted({s.length for s in group.stocks}):
        for seg in sorted(sigma):
            if seg <= L:
                stage2.append(_Stage2Col(L, (seg,)))
    # de-dup stage2
    stage2 = _dedup_stage2(stage2)

    # Scale guard: if even the seed pool is huge the LP re-solves and final MILP
    # will not finish in budget -- fall back rather than thrash.
    if len(stage1) + len(stage2) > pool_cap:
        return None

    forbidden_segs: set[int] = set()
    seen_cols: set[tuple[int, tuple[int, ...]]] = {
        (c.pipe_index, c.parts) for c in stage1
    }
    seen_cuts: set[tuple[int, tuple[int, ...]]] = {
        (c.stock_length, c.parts) for c in stage2
    }

    last_obj = float("inf")
    stall = 0

    # Reserve the tail of the budget for backfill + the final integer MILP; the
    # pricing loop only gets the front portion so it can never starve the solve
    # that actually produces the returned solution.  Tight-packing groups tend to
    # stall in pricing very early, so cap pricing at 45% and guarantee the final
    # integer MILP at least half the wall-clock budget.
    pricing_deadline = started + time_limit * 0.45

    for round_no in range(MAX_PRICING_ROUNDS):
        if time.monotonic() >= pricing_deadline:
            break
        lp = _solve_master(
            group, stage1, stage2, integer=False,
            time_limit=max(0.1, (pricing_deadline - time.monotonic()) * 0.5),
            phase1=True,
        )
        if lp is None:
            break
        obj = lp["objective"]
        improvement = (last_obj - obj) / last_obj if last_obj not in (0.0, float("inf")) else 1.0
        last_obj = obj

        mu = lp["mu"]
        pi = lp["pi"]
        stock_dual = lp["stock_dual"]

        added = 0
        # --- column generation: stage 1 ---
        s1_new = 0
        for i, pipe in enumerate(group.pipes):
            if s1_new >= STAGE1_COLS_PER_ROUND:
                break
            for rc, parts in _price_stage1(
                pipe, i, group, sorted(sigma), pi, mu[i], max_stock, 1
            ):
                key = (i, parts)
                if key not in seen_cols:
                    seen_cols.add(key)
                    stage1.append(_Stage1Col(i, parts))
                    sigma.update(parts)
                    added += 1
                    s1_new += 1
        # --- column generation: stage 2 ---
        for rc, L, parts in _price_stage2(
            group, sorted(sigma), pi, stock_dual, STAGE2_COLS_PER_ROUND
        ):
            key = (L, parts)
            if key not in seen_cuts:
                seen_cuts.add(key)
                stage2.append(_Stage2Col(L, parts))
                added += 1

        if added == 0 or improvement < IMPROVE_EPS:
            stall += 1
        else:
            stall = 0

        # --- row generation: widen Sigma when column gen stalls ---
        if stall >= PATTERN_STALL_LIMIT:
            new_segs = _generate_new_segments(group, sigma, max_stock, forbidden_segs)
            grew = False
            for seg in new_segs:
                if seg not in sigma:
                    sigma.add(seg)
                    grew = True
                    # seed a trivial stage-2 col consuming it so the row is live
                    for L in sorted({s.length for s in group.stocks}):
                        if seg <= L:
                            key = (L, (seg,))
                            if key not in seen_cuts:
                                seen_cuts.add(key)
                                stage2.append(_Stage2Col(L, (seg,)))
                    # seed stage-1 cols producing it
                    _seed_stage1_for_segment(group, seg, max_stock, stage1, seen_cols, sigma)
            if not grew:
                break  # no new columns, no new rows -> LP optimum reached
            stall = 0

    # --- column-pool backfill before the final integer solve ---
    # R&CG's pricing may stall before the pool can support a real integer
    # feasible solution (limited budget, non-exact pricing).  Backfill with the
    # shared standard-segment machinery over the *final* alphabet so the integer
    # MILP has a column pool at least as rich as route3's -- pricing only ever
    # *adds* segments, so this can only help.  Skip if we are already out of time
    # or the pool is already at the size ceiling.
    if time.monotonic() < deadline and len(stage1) + len(stage2) <= pool_cap:
        _backfill_pools(group, stage1, stage2, sigma, seen_cols, seen_cuts, max_stock)

    # Warm-start: inject the deterministic fallback's feasible plan as columns so
    # the final MILP always has a feasible integer point to land on (rescues the
    # mid-size welded groups where R&CG's own pool cannot assemble a packing).
    ws_budget = min(3.0, max(0.5, (deadline - time.monotonic()) * 0.3))
    if time.monotonic() < deadline and len(stage1) + len(stage2) <= pool_cap:
        _warmstart_pools(
            group, stage1, stage2, sigma, seen_cols, seen_cuts, ws_budget
        )

    # If the backfill pushed us over the pool ceiling, or we are out of time, the
    # final MILP cannot finish in budget -- fall back rather than thrash.
    if len(stage1) + len(stage2) > pool_cap or time.monotonic() >= deadline:
        _trace(f"bail pool/time: pool={len(stage1)+len(stage2)} left={deadline-time.monotonic():.2f}")
        return None

    # --- final integer solve over the accumulated pools ---
    # Reserve a firm slice for the MILP.  SCIP's ``limits/time`` bounds only the
    # optimise() call, not Python-side model building over a large column pool,
    # so if too little wall-clock remains we skip rather than overrun the budget
    # (which previously let a 20s budget balloon to ~40s).
    remaining = deadline - time.monotonic()
    if remaining < 1.0:
        _trace(f"bail remaining<1 before ip1: {remaining:.2f}")
        return None
    _trace(f"pools before ip1: stage1={len(stage1)} stage2={len(stage2)} sigma={len(sigma)}")
    ip = _solve_master(group, stage1, stage2, integer=True, time_limit=remaining)
    _trace(f"ip1: {None if ip is None else ip.get('infeasibility')}")
    if ip is None or ip.get("infeasibility", 0.0) > 1e-6:
        # Retry with a phase-I integer solve (accept only if artificials are
        # zero) -- but only when enough budget is left to build+solve again.
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            _trace(f"bail remaining<1 before ip2: {remaining:.2f}")
            return None
        ip = _solve_master(
            group, stage1, stage2, integer=True,
            time_limit=remaining, phase1=True,
        )
        _trace(f"ip2(phase1): {None if ip is None else ip.get('infeasibility')}")
        if ip is None or ip.get("infeasibility", 0.0) > 1e-6:
            _trace("bail ip2 infeasible")
            return None

    weld_counts, cut_counts = _to_counts(group, stage1, stage2, ip)
    if not weld_counts or not cut_counts:
        _trace(f"bail empty counts: weld={len(weld_counts)} cut={len(cut_counts)}")
        return None

    # every pipe demand must be met
    produced_pipes: dict[int, int] = defaultdict(int)
    for cand, qty in weld_counts:
        produced_pipes[cand.pipe_index] += qty
    for i, pipe in enumerate(group.pipes):
        if produced_pipes.get(i, 0) != pipe.demand:
            _trace(f"bail demand mismatch pipe {i}: got {produced_pipes.get(i,0)} need {pipe.demand}")
            return None

    # segment balance: produced >= consumed for every segment
    produced: dict[int, int] = defaultdict(int)
    consumed: dict[int, int] = defaultdict(int)
    for cand, qty in cut_counts:
        for seg in cand.parts:
            produced[seg] += qty
    for cand, qty in weld_counts:
        for seg in cand.parts:
            consumed[seg] += qty
    for seg, need in consumed.items():
        if produced.get(seg, 0) < need:
            _trace(f"bail seg balance seg={seg} produced={produced.get(seg,0)} need={need}")
            return None

    # drop surplus produced segments into trim so verifier's produced==consumed
    # holds (mirrors route3's reconciliation).
    cut_counts = _reconcile_surplus(group, weld_counts, cut_counts)
    if not cut_counts:
        _trace("bail reconcile empty")
        return None

    # Must-use priority (mirrors verifier): if any *available* (non-mandatory) bar
    # is cut, every mandatory bar must be consumed first.  The RSRMP's muexhaust
    # gating can be silently unsatisfiable when a must-use length has no feasible
    # cut column (e.g. a mandatory bar too short to hold any segment), letting a
    # solution slip through that uses available stock while a mandatory bar stays
    # idle.  That is an invalid plan the verifier rejects, so bail to the fallback
    # engine rather than emit it.
    if not _must_use_respected(group, cut_counts):
        _trace("bail must-use not respected")
        return None

    result = _assemble_group_result(
        group, "SCIP-RCG", "RCG", weld_counts, cut_counts,
        time.monotonic() - started,
    )
    result["metrics"]["solve_status"] = (
        "RCG_TARGET_REACHED" if result["metrics"]["target_reached"] else "RCG_FEASIBLE"
    )
    result["warnings"].append(
        "ROUTE_B_RCG: two-stage row-and-column generation (Sigma "
        f"= {len(sigma)} segment specs)"
    )
    return result


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _must_use_respected(
    group: MaterialGroup,
    cut_counts: list[tuple[_CutCandidate, int]],
) -> bool:
    """Return True unless an available bar is cut while a mandatory bar is idle.

    Mirrors the verifier's group-level must-use priority: consuming any
    non-mandatory (available) bar requires every mandatory bar to be used first.
    """
    must_use = {s.length: s.must_use_quantity for s in group.stocks}
    must_use_total = sum(must_use.values())
    if must_use_total == 0:
        return True
    used_by_length: dict[int, int] = defaultdict(int)
    for cand, qty in cut_counts:
        used_by_length[cand.stock_length] += qty
    used_must = sum(
        min(used_by_length.get(L, 0), req) for L, req in must_use.items()
    )
    used_available = sum(
        max(0, used_by_length.get(L, 0) - must_use.get(L, 0)) for L in used_by_length
    )
    return not (used_available > 0 and used_must < must_use_total)


def _warmstart_pools(
    group: MaterialGroup,
    stage1: list[_Stage1Col],
    stage2: list[_Stage2Col],
    sigma: set[int],
    seen_cols: set[tuple[int, tuple[int, ...]]],
    seen_cuts: set[tuple[int, tuple[int, ...]]],
    time_limit: float,
) -> None:
    """Inject the deterministic fallback's feasible solution as columns.

    The fallback (baseline heuristic) always finds a *complete feasible* cut+weld
    plan when one exists.  Feeding its weld/cut patterns into the pools guarantees
    the final integer MILP has at least one feasible integer point to land on,
    which rescues the mid-size welded groups where R&CG's own pricing pool cannot
    assemble a feasible packing (e.g. demand > stock, heavy splicing).  It never
    removes columns, so it can only help.  Failures are swallowed -- warm-start is
    strictly optional.
    """
    try:
        from .solver import _solve_group_fallback
    except Exception:  # noqa: BLE001
        return
    try:
        fb = _solve_group_fallback(group, max(0.5, time_limit))
    except Exception:  # noqa: BLE001 - fallback may legitimately find nothing
        return
    pipe_index = {pipe.pipe_id: i for i, pipe in enumerate(group.pipes)}
    for wp in fb.get("welding_patterns", []):
        i = pipe_index.get(wp.get("pipe_id"))
        if i is None:
            continue
        parts = tuple(int(x) for x in wp.get("parts", []))
        if not parts:
            continue
        key = (i, parts)
        if key not in seen_cols:
            seen_cols.add(key)
            stage1.append(_Stage1Col(i, parts))
            sigma.update(parts)
    for cp in fb.get("cutting_patterns", []):
        L = int(cp.get("stock_length", 0))
        parts = tuple(sorted(int(x) for x in cp.get("parts", [])))
        if not parts:
            continue
        key = (L, parts)
        if key not in seen_cuts:
            seen_cuts.add(key)
            stage2.append(_Stage2Col(L, parts))


def _backfill_pools(
    group: MaterialGroup,
    stage1: list[_Stage1Col],
    stage2: list[_Stage2Col],
    sigma: set[int],
    seen_cols: set[tuple[int, tuple[int, ...]]],
    seen_cuts: set[tuple[int, tuple[int, ...]]],
    max_stock: int,
) -> None:
    """Enrich both column pools with the shared standard-segment machinery so the
    final integer MILP is never starved of feasible columns.

    * Stage 1: every standard weld decomposition per pipe (fewest distinct
      segments first -- the joint fewest-patterns objective).
    * Stage 2: composite cut columns over the resulting alphabet.
    Only *adds* columns (monotone), so it can never remove a feasible solution.
    """
    for cand in _standard_weld_candidates(group, set(seen_cols), per_pipe_cap=24):
        key = (cand.pipe_index, cand.parts)
        if key not in seen_cols:
            seen_cols.add(key)
            stage1.append(_Stage1Col(cand.pipe_index, cand.parts))
            sigma.update(cand.parts)
    # Inject high-utilisation carved patterns (baseline T3's core weapon for
    # tight-packing). Store the exact parts tuple validated by _legal_pattern
    # (do NOT sort -- carved parts encode legal weld positions by order).
    for i, pipe in enumerate(group.pipes):
        for parts in _carved_weld_patterns(pipe, group, max_stock):
            key = (i, parts)
            if key not in seen_cols:
                seen_cols.add(key)
                stage1.append(_Stage1Col(i, parts))
                sigma.update(parts)
    seed_welds = [_WeldCandidate(c.pipe_index, c.parts) for c in stage1]
    for cut in _generate_cut_candidates(group, seed_welds):
        key = (cut.stock_length, tuple(sorted(cut.parts)))
        if key not in seen_cuts:
            seen_cuts.add(key)
            stage2.append(_Stage2Col(cut.stock_length, key[1]))


def _dedup_stage2(cols: list[_Stage2Col]) -> list[_Stage2Col]:
    seen: set[tuple[int, tuple[int, ...]]] = set()
    out: list[_Stage2Col] = []
    for c in cols:
        key = (c.stock_length, c.parts)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _seed_stage1_for_segment(
    group: MaterialGroup,
    seg: int,
    max_stock: int,
    stage1: list[_Stage1Col],
    seen_cols: set[tuple[int, tuple[int, ...]]],
    sigma: set[int],
) -> None:
    """Add stage-1 columns that produce ``seg`` for any pipe whose legal
    decomposition can include it (keeps the new seg-row live)."""
    for i, pipe in enumerate(group.pipes):
        if pipe.length == seg and pipe.length <= max_stock:
            key = (i, (seg,))
            if key not in seen_cols:
                seen_cols.add(key)
                stage1.append(_Stage1Col(i, (seg,)))
            continue
        if pipe.max_joints >= 1 and seg < pipe.length:
            other = pipe.length - seg
            if other > max_stock:
                continue
            # The weld sits at the first part's end, so (seg, other) and
            # (other, seg) are DIFFERENT decompositions with different weld
            # positions.  Store whichever order is actually legal -- never sort,
            # or the stored weld position diverges from the validated one (this
            # produced welds inside forbidden zones).
            for parts in ((seg, other), (other, seg)):
                if _legal_pattern(
                    pipe, parts, group.min_weld_distance, group.min_cut_length
                ):
                    key = (i, parts)
                    if key not in seen_cols:
                        seen_cols.add(key)
                        stage1.append(_Stage1Col(i, parts))
                        sigma.add(other)
                    break


def _to_counts(
    group: MaterialGroup,
    stage1: list[_Stage1Col],
    stage2: list[_Stage2Col],
    ip: dict[str, Any],
) -> tuple[list[tuple[_WeldCandidate, int]], list[tuple[_CutCandidate, int]]]:
    kerf = group.blade_margin
    weld_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for p, qty in enumerate(ip["x"]):
        if qty > 0:
            col = stage1[p]
            # Defense-in-depth: never let an illegal weld decomposition reach the
            # output, whatever pool injected it.  An illegal column here means the
            # solution is invalid; signal by returning empty so the caller bails.
            if not _legal_pattern(
                group.pipes[col.pipe_index],
                col.parts, group.min_weld_distance, group.min_cut_length,
            ):
                return [], []
            weld_agg[(col.pipe_index, col.parts)] += qty
    weld_counts = [
        (_WeldCandidate(pipe_index, parts), qty)
        for (pipe_index, parts), qty in sorted(weld_agg.items())
    ]
    cut_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for q, qty in enumerate(ip["y"]):
        if qty > 0:
            col = stage2[q]
            cut_agg[(col.stock_length, col.parts)] += qty
    cut_counts = [
        (_CutCandidate(stock_length, parts, kerf, group.kerf_mode), qty)
        for (stock_length, parts), qty in sorted(cut_agg.items())
    ]
    return weld_counts, cut_counts


def _reconcile_surplus(
    group: MaterialGroup,
    weld_counts: list[tuple[_WeldCandidate, int]],
    cut_counts: list[tuple[_CutCandidate, int]],
) -> list[tuple[_CutCandidate, int]]:
    """Remove over-produced segments from cut columns so produced==consumed, the
    surplus becoming a longer remnant (identical treatment to route3)."""
    kerf = group.blade_margin
    produced: dict[int, int] = defaultdict(int)
    consumed: dict[int, int] = defaultdict(int)
    for cand, qty in cut_counts:
        for seg in cand.parts:
            produced[seg] += qty
    for cand, qty in weld_counts:
        for seg in cand.parts:
            consumed[seg] += qty
    surplus = {seg: produced[seg] - consumed.get(seg, 0) for seg in produced}
    surplus = {seg: n for seg, n in surplus.items() if n > 0}

    bar_instances: list[list[int]] = []
    stock_of_bar: list[int] = []
    for cand, qty in cut_counts:
        for _ in range(qty):
            bar_instances.append(list(cand.parts))
            stock_of_bar.append(cand.stock_length)

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
    return [
        (_CutCandidate(stock_length, parts, kerf, group.kerf_mode), qty)
        for (stock_length, parts), qty in sorted(cut_agg.items())
    ]


__all__ = ["solve_group"]
