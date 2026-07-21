"""Route C -- Branch-and-Price fallback for the *tight welded* B-class.

R&CG (route B) and the baseline MILP both assemble their patterns from a coarse
column pool.  For groups where a single tube must be spliced from several stock
pieces (``pipe length`` close to or above the longest bar) *and* the material
margin is razor thin, that pool cannot express the fine multi-segment mixed
cuts the incumbent software uses, so the integer solve is infeasible in budget.

This module solves exactly that residual.  It is a Gilmore-Gomory two-index
column-generation model driven by a *structured segment alphabet* (母料/管长
边界导出的候选段) with a many-segment legal-sequence weld pricing and diverse
mixed-cut knapsack pricing.  Inside the fallback the per-tube joint ceiling is
relaxed to a geometric limit (the incumbent splices a 5173mm tube with two
joints; the coarse ``length // weld_interval`` process rule that the parser
applies would wrongly cap it at one), while ``_legal_pattern`` still enforces
every forbidden zone and adjacency rule at solve time.

The public entry point :func:`solve_bp` returns ``(weld_counts, cut_counts)`` in
the exact ``_assemble_group_result`` contract, or ``None`` when the model cannot
close the demand gap inside the budget.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import os as _os
import time
from typing import Any

from .domain import MaterialGroup, PipeDemand
from .solver import _CutCandidate, _WeldCandidate, _legal_pattern

# ---------------------------------------------------------------------------
# Tuning knobs.
# ---------------------------------------------------------------------------
MAX_JOINTS_CAP = 13              # hard ceiling on joints per tube in the fallback
MIN_WELD_SEG = 500               # shortest welded segment the shop allows (mm)
ENUM_BUDGET = 40_000             # DFS node budget for legal-sequence enumeration
SEQS_PER_PIPE = 24               # legal sequences injected per gap tube per round
RC_EPS = 1e-6


def _trace(msg: str) -> None:
    if _os.environ.get("BP_TRACE"):
        print(f"[BP] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Structured segment alphabet (母料/管长边界导出候选段).
# ---------------------------------------------------------------------------
def _derive_alphabet(group: MaterialGroup) -> list[int]:
    """Candidate segment lengths derived from stock/pipe boundary arithmetic.

    Segments come from integer combinations of ``stock - k*pipe`` (offcuts left
    after cutting whole tubes) and ``pipe - k*stock`` (tails a spliced tube needs
    beyond whole bars), plus pairwise sums that let two offcuts pack one bar.
    This gives pricing a finite, meaningful alphabet instead of blind mm search.
    """
    stock_lens = sorted({s.length for s in group.stocks})
    pipe_lens = sorted({p.length for p in group.pipes})
    if not stock_lens or not pipe_lens:
        return []
    min_seg = max(group.min_cut_length, MIN_WELD_SEG, 1)
    max_seg = min(max(stock_lens), max(pipe_lens))
    segs: set[int] = set()

    # ① whole tube as a single segment (only if a bar can hold it)
    for pl in pipe_lens:
        if pl <= max(stock_lens):
            segs.add(pl)
    # ①b pipe > stock (splice case): whole-bar segments and splice tails
    for pl in pipe_lens:
        for length in stock_lens:
            if pl > length:
                if min_seg <= length <= max_seg:
                    segs.add(length)
                k = 1
                while k * length < pl:
                    r = pl - k * length
                    if min_seg <= r <= max_seg:
                        segs.add(r)
                    k += 1
    # ② offcut after cutting k whole tubes from a bar (pipe < stock)
    for length in stock_lens:
        for pl in pipe_lens:
            k = 1
            while k * pl <= length:
                r = length - k * pl
                if min_seg <= r <= max_seg:
                    segs.add(r)
                k += 1
    # ③ splice complement: pipe - offcut (so offcuts pair back into a tube)
    base = set(segs)
    for pl in pipe_lens:
        for r in base:
            d = pl - r
            if min_seg <= d <= max_seg:
                segs.add(d)
    # ④ integer multiples of an offcut (several offcuts welded into one segment)
    for r in list(segs):
        for k in (2, 3):
            if min_seg <= r * k <= max_seg:
                segs.add(r * k)
    # ⑤ pairwise sums of small offcuts (extra coverage for tight groups where
    #    a bar packs two dissimilar tails).  This enlarges the segment alphabet
    #    -- and thus the LP row count -- noticeably, so it is only worth enabling
    #    when the base alphabet is still small; on big groups it just slows the
    #    master without unlocking any missing pattern.
    if len(segs) <= 60:
        smalls = sorted(s for s in segs if s <= max_seg // 2)[:40]
        for i, a in enumerate(smalls):
            for b in smalls[i:]:
                t = a + b
                if min_seg <= t <= max_seg:
                    segs.add(t)
    # ⑥ ultra-long tubes (pipe > longest bar): the splice must break at a *legal*
    #    weld position, so sample those positions and add both flanks.  Enabled
    #    only for small-demand groups: the sweep enriches the alphabet enough to
    #    unlock tight splices (57x8/45x12), but on large-demand groups the extra
    #    rows blow up the master LP and starve column generation of solve time.
    total_demand = sum(p.demand for p in group.pipes)
    if total_demand <= 120:
        for pipe in group.pipes:
            pl = pipe.length
            if pl <= max(stock_lens):
                continue
            breaks: set[int] = set()
            for length in stock_lens:
                for cand in (pl - length, length):
                    if 0 < cand < pl:
                        breaks.add(cand)
            step = max(min_seg, (pl - 2 * min_seg) // 24) if pl > 2 * min_seg else min_seg
            pos = min_seg
            while pos < pl - min_seg:
                breaks.add(pos)
                pos += step
            for cut in breaks:
                if not pipe.weld_allowed(cut):
                    continue
                for seg in (cut, pl - cut):
                    if min_seg <= seg <= max_seg and seg <= max(stock_lens):
                        segs.add(seg)
    return sorted(s for s in segs if s >= min_seg)


def _enum_legal_seqs(
    pipe: PipeDemand,
    alphabet: list[int],
    min_wd: int,
    min_cut: int,
    max_stock: int,
    budget: int = ENUM_BUDGET,
) -> list[tuple[int, ...]]:
    """All legal segment sequences (sum == length, joint/zone rules satisfied)."""
    length = pipe.length
    cand = sorted({s for s in alphabet if 0 < s <= min(length, max_stock)}, reverse=True)
    out: set[tuple[int, ...]] = set()
    nodes = [0]

    def dfs(pos: int, seq: list[int]) -> None:
        nodes[0] += 1
        if nodes[0] > budget:
            return
        if pos == length:
            if _legal_pattern(pipe, tuple(seq), min_wd, min_cut):
                out.add(tuple(seq))
            return
        if len(seq) >= pipe.max_joints + 1:
            return
        remain = length - pos
        for s in cand:
            if s <= remain:
                seq.append(s)
                dfs(pos + s, seq)
                seq.pop()

    dfs(0, [])
    if length <= max_stock and _legal_pattern(pipe, (length,), min_wd, min_cut):
        out.add((length,))
    return list(out)


def _cut_knapsack(
    length: int, pi: dict[int, float], kerf: int = 0
) -> tuple[float, dict[int, int]]:
    """Unbounded knapsack: max Σ pi[s]·n_s s.t. Σ n_s·s + (n-1)·kerf <= length.

    Each part after the first pays one blade-margin (``kerf``) for the saw pass
    that separates it, matching ``_CutCandidate.used``.  Modelled by charging
    ``s + kerf`` per part against a capacity of ``length + kerf`` (so the first
    part gets its kerf refunded), keeping the DP one-dimensional.
    """
    cand = sorted(s for s in pi if 0 < s <= length and pi[s] > 1e-9)
    if not cand:
        return 0.0, {}
    cap_max = length + kerf
    dp = [0.0] * (cap_max + 1)
    pick = [-1] * (cap_max + 1)
    for cap in range(1, cap_max + 1):
        bv, bs = dp[cap - 1], -1
        for s in cand:
            cost = s + kerf
            if cost <= cap:
                v = pi[s] + dp[cap - cost]
                if v > bv:
                    bv, bs = v, s
        dp[cap], pick[cap] = bv, bs
    counts: dict[int, int] = defaultdict(int)
    cap = cap_max
    while cap > 0:
        s = pick[cap]
        if s < 0:
            cap -= 1
            continue
        counts[s] += 1
        cap -= s + kerf
    return dp[cap_max], dict(counts)


# ---------------------------------------------------------------------------
# The solver.
# ---------------------------------------------------------------------------
class _BranchAndPrice:
    def __init__(self, group: MaterialGroup, time_limit: float, started: float):
        # Relax per-tube joints to a geometric ceiling for the fallback only.
        pipes = []
        for p in group.pipes:
            geom = max(1, p.length // MIN_WELD_SEG - 1)
            pipes.append(replace(p, max_joints=min(MAX_JOINTS_CAP, max(p.max_joints, geom))))
        self.g = replace(group, pipes=tuple(pipes))
        self.pipes = self.g.pipes
        self.time_limit = time_limit
        self.started = started
        self.deadline = started + time_limit
        self.min_wd = group.min_weld_distance
        self.min_cut = group.min_cut_length
        self.kerf = group.blade_margin
        self.kerf_mode = group.kerf_mode
        self.max_stock = max(s.length for s in group.stocks)
        self.target = group.target_rate
        self.stock_qty: dict[int, int] = defaultdict(int)
        for st in group.stocks:
            self.stock_qty[st.length] += st.quantity
        self.alphabet = _derive_alphabet(self.g)
        self.cut_cols: list[tuple[int, dict[int, int]]] = []
        self.weld_cols: list[tuple[int, tuple[int, ...]]] = []
        self._seed()

    def _time_left(self) -> float:
        return self.deadline - time.monotonic()

    def _seed(self) -> None:
        # Seed each tube with its most compact legal splice, then single-segment
        # cuts for the segments used.
        for i, p in enumerate(self.pipes):
            seqs = _enum_legal_seqs(p, self.alphabet, self.min_wd, self.min_cut,
                                    self.max_stock, budget=8000)
            if not seqs:
                continue
            seqs.sort(key=lambda s: (len(s), -min(s)))
            self.weld_cols.append((i, seqs[0]))
        segs = {s for (_, seq) in self.weld_cols for s in seq}
        for length in self.stock_qty:
            for s in sorted(segs):
                if 0 < s <= length:
                    self.cut_cols.append((length, {s: 1}))

    def _all_segs(self) -> list[int]:
        s: set[int] = set()
        for (_, cc) in self.cut_cols:
            s.update(cc)
        for (_, seq) in self.weld_cols:
            s.update(seq)
        return sorted(s)

    def _solve_lp(self):
        from pyscipopt import Model, quicksum, SCIP_PARAMSETTING

        m = Model("bp_master")
        m.hideOutput()
        m.setParam("limits/time", max(1.0, self._time_left()))
        m.setPresolve(SCIP_PARAMSETTING.OFF)
        m.setHeuristics(SCIP_PARAMSETTING.OFF)
        m.disablePropagation()
        segs = self._all_segs()
        nC, nW, nP = len(self.cut_cols), len(self.weld_cols), len(self.pipes)
        big_m = 10 * sum(st.length * st.quantity for st in self.g.stocks) + 1
        x = {c: m.addVar(vtype="C", lb=0) for c in range(nC)}
        y = {p: m.addVar(vtype="C", lb=0) for p in range(nW)}
        a = {i: m.addVar(vtype="C", lb=0) for i in range(nP)}
        seg_cons = {}
        for s in segs:
            prod = quicksum(self.cut_cols[c][1].get(s, 0) * x[c] for c in range(nC))
            cons = quicksum(self.weld_cols[p][1].count(s) * y[p] for p in range(nW))
            seg_cons[s] = m.addCons(prod - cons >= 0)
        dem_cons = {}
        for i in range(nP):
            terms = [y[pp] for pp in range(nW) if self.weld_cols[pp][0] == i]
            dem_cons[i] = m.addCons(
                (quicksum(terms) if terms else 0) + a[i] >= self.pipes[i].demand
            )
        for length in self.stock_qty:
            cols = [x[c] for c in range(nC) if self.cut_cols[c][0] == length]
            if cols:
                m.addCons(quicksum(cols) <= self.stock_qty[length])
        m.setObjective(
            quicksum(self.cut_cols[c][0] * x[c] for c in range(nC))
            + big_m * quicksum(a[i] for i in range(nP)),
            "minimize",
        )
        m.optimize()
        if m.getNSols() == 0:
            return None
        gap = sum(m.getVal(a[i]) for i in range(nP))
        used_len = sum(self.cut_cols[c][0] * m.getVal(x[c]) for c in range(nC))
        pi = {s: max(0.0, m.getDualsolLinear(seg_cons[s])) for s in segs}
        mu = {i: max(0.0, m.getDualsolLinear(dem_cons[i])) for i in range(nP)}
        return gap, used_len, pi, mu

    def _price(self, pi: dict[int, float], mu: dict[int, float], gap: float) -> int:
        added = 0
        new_segs: set[int] = set()
        # Weld pricing: for every tube inject legal multi-segment sequences the
        # duals value; when a demand gap remains, drive it with mu (feasibility).
        for i, p in enumerate(self.pipes):
            seqs = _enum_legal_seqs(p, self.alphabet, self.min_wd, self.min_cut,
                                    self.max_stock)
            existing = {w[1] for w in self.weld_cols if w[0] == i}

            def score(seq: tuple[int, ...]) -> tuple:
                val = sum(pi.get(s, 0.0) for s in seq)
                return (-val, len(seq), -min(seq))

            seqs.sort(key=score)
            for seq in seqs[:SEQS_PER_PIPE]:
                if seq not in existing:
                    self.weld_cols.append((i, seq))
                    existing.add(seq)
                    new_segs.update(seq)
                    added += 1
        # Cut pricing.  Keep the column count lean: a value-driven knapsack builds
        # mixed multi-segment bars, and each newly consumed segment gets a single
        # supply column plus one complement (segment + its bar remainder).  The
        # per-segment "variant explosion" is avoided -- it makes the LP too slow
        # on big groups without adding solving power the knapsack lacks.
        for length in self.stock_qty:
            weights = {
                s: (pi[s] if pi.get(s, 0.0) > 1e-9 else 1.0)
                for s in self.alphabet
                if 0 < s <= length
            }
            if weights:
                _, counts = _cut_knapsack(length, weights, self.kerf)
                if counts and not any(cc == counts and cl == length for (cl, cc) in self.cut_cols):
                    self.cut_cols.append((length, counts))
                    added += 1
            for s in sorted(new_segs):
                if not (0 < s <= length):
                    continue
                rem = length - s - self.kerf
                variants: list[dict[int, int]] = [{s: 1}]
                if rem >= max(1, self.min_cut) and rem != s:
                    variants.append({s: 1, rem: 1})
                for cc in variants:
                    if not any(c2 == cc and l2 == length for (l2, c2) in self.cut_cols):
                        self.cut_cols.append((length, cc))
                        added += 1
        return added

    def _column_generation(self) -> tuple[float, float] | None:
        for it in range(200):
            if self._time_left() < 2.0:
                _trace(f"cg bail time @it{it}")
                break
            res = self._solve_lp()
            if res is None:
                return None
            gap, used_len, pi, mu = res
            util = self.g.demand_length / used_len if used_len > 0 else 0.0
            _trace(f"cg{it}: gap={gap:.2f} util={util:.4f} "
                   f"cols(cut={len(self.cut_cols)},weld={len(self.weld_cols)})")
            # Once the demand gap is closed and utilisation meets target we have a
            # usable solution.  Keep polishing utilisation while budget is ample,
            # but stop as soon as the remaining time drops to the reserve we need
            # for the final integer solve -- otherwise large groups (196-demand
            # ultra-long tubes) spend all their budget in the LP and never reach
            # the ILP, while small groups keep improving util harmlessly.
            if gap <= RC_EPS and util >= self.target - 1e-6:
                reserve = max(3.0, self.time_limit * 0.4)
                if self._time_left() <= reserve:
                    _trace(f"cg early-stop @it{it}: gap=0 util={util:.4f} (reserve)")
                    return gap, used_len
            added = self._price(pi, mu, gap)
            if added == 0:
                _trace(f"cg converge @it{it}: gap={gap:.2f}")
                return gap, used_len
        res = self._solve_lp()
        if res is None:
            return None
        return res[0], res[1]

    def _final_ilp(self):
        from pyscipopt import Model, quicksum

        remaining = self._time_left()
        if remaining < 1.0:
            return None
        m = Model("bp_final")
        m.hideOutput()
        m.setParam("limits/time", remaining)
        nC, nW, nP = len(self.cut_cols), len(self.weld_cols), len(self.pipes)
        segs = self._all_segs()
        x = {c: m.addVar(vtype="I", lb=0) for c in range(nC)}
        y = {p: m.addVar(vtype="I", lb=0) for p in range(nW)}
        for s in segs:
            prod = quicksum(self.cut_cols[c][1].get(s, 0) * x[c] for c in range(nC))
            cons = quicksum(self.weld_cols[p][1].count(s) * y[p] for p in range(nW))
            # Strict balance: every functional segment cut must be consumed by a
            # weld pattern.  A ``>=`` here lets the solver cut one extra 988mm
            # segment to pad utilisation, which the verifier rejects as an
            # unconsumed segment (SEGMENT_BALANCE_MISMATCH).  Genuine waste must
            # appear as a bar remainder, not a dangling functional segment.
            m.addCons(prod - cons == 0)
        for i in range(nP):
            terms = [y[pp] for pp in range(nW) if self.weld_cols[pp][0] == i]
            if terms:
                m.addCons(quicksum(terms) == self.pipes[i].demand)
            else:
                return None  # a tube has no weld column -> cannot meet demand
        for length in self.stock_qty:
            cols = [x[c] for c in range(nC) if self.cut_cols[c][0] == length]
            if cols:
                m.addCons(quicksum(cols) <= self.stock_qty[length])
        used_len = quicksum(self.cut_cols[c][0] * x[c] for c in range(nC))
        joints = quicksum((len(self.weld_cols[p][1]) - 1) * y[p] for p in range(nW))
        # Lexicographic: min material, then min joints.
        m.setObjective(used_len, "minimize")
        m.optimize()
        if m.getNSols() == 0:
            return None
        u_star = m.getObjVal()
        remaining = self._time_left()
        if remaining >= 1.0:
            m.freeTransform()
            m.setParam("limits/time", remaining)
            m.addCons(used_len <= u_star + 1e-6)
            m.setObjective(joints, "minimize")
            m.optimize()
            if m.getNSols() == 0:
                return None
        weld_counts: list[tuple[_WeldCandidate, int]] = []
        for p in range(nW):
            n = round(m.getVal(y[p]))
            if n > 0:
                idx, seq = self.weld_cols[p]
                weld_counts.append((_WeldCandidate(idx, seq), n))
        cut_counts: list[tuple[_CutCandidate, int]] = []
        for c in range(nC):
            n = round(m.getVal(x[c]))
            if n > 0:
                length, counts = self.cut_cols[c]
                parts = tuple(sorted(s for s, k in counts.items() for _ in range(k)))
                cut_counts.append(
                    (_CutCandidate(length, parts, self.kerf, self.kerf_mode), n)
                )
        return weld_counts, cut_counts

    def solve(self):
        cg = self._column_generation()
        if cg is None:
            _trace("root LP infeasible")
            return None
        gap, _ = cg
        if gap > 1e-6:
            _trace(f"gap not closed: {gap:.2f}")
            return None
        return self._final_ilp()


def solve_bp(
    group: MaterialGroup, time_limit: float, started: float
) -> tuple[list[tuple[_WeldCandidate, int]], list[tuple[_CutCandidate, int]]] | None:
    """Solve a tight welded B-class group; return ``(weld_counts, cut_counts)``.

    Returns ``None`` when the model cannot close the demand gap in budget.
    """
    try:
        return _BranchAndPrice(group, time_limit, started).solve()
    except Exception as exc:  # noqa: BLE001 -- fallback must never crash the caller
        _trace(f"exception: {type(exc).__name__}: {exc}")
        return None
