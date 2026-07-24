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
from pathlib import Path
import sys
import time
from typing import Any

from .domain import MaterialGroup, PipeDemand
from .solver import _CutCandidate, _WeldCandidate, _legal_pattern

# scripts/ hosts the exact lexicographic type-compression MILP (Pass3/4/5) used
# by the arc-flow engine.  It is not a backend package, so put both the project
# root and scripts/ on the path -- same shim route_v4_arcflow.py uses.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Tuning knobs.
# ---------------------------------------------------------------------------
MAX_JOINTS_CAP = 13              # hard ceiling on joints per tube in the fallback
MIN_WELD_SEG = 500               # shortest welded segment the shop allows (mm)
SHORT_TUBE_JOINTS = 2            # joint budget for tubes that fit a single bar
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
    # ③ splice complement: pipe - offcut (so offcuts pair back into a tube).
    #   This complement cascade densifies the alphabet with many closely-spaced
    #   fragments.  It is only worth its cost when a tube must be spliced from
    #   several bars; for a plain 1D cutting group it merely tempts pricing into
    #   shattered mixed cuts (152 produced segments on a 4-pipe group), which
    #   makes the coupled integer MILP infeasible and starves compression.
    needs_splice = any(pl > max(stock_lens) for pl in pipe_lens)
    if needs_splice:
        base = set(segs)
        for pl in pipe_lens:
            for r in base:
                d = pl - r
                if min_seg <= d <= max_seg:
                    segs.add(d)
    else:
        # Pure 1D cutting group.  When demand fits entirely in whole tubes there
        # is nothing to splice and ①② already cover every useful segment.  When
        # demand *exceeds* the whole-tube capacity of the stock (so some tubes
        # must be split to pack the offcuts), add a *regular* grid of split
        # points -- the same structure the incumbent uses (splits on min-weld
        # multiples), which keeps the alphabet small and integer-friendly rather
        # than the fragment cascade.  Cap the grid so the alphabet stays lean.
        whole_cap = 0
        for length, qty in ((s.length, s.quantity) for s in group.stocks):
            best = 0
            for pl in pipe_lens:
                k = 0
                cap = length
                while cap >= pl:
                    cap -= pl + (group.blade_margin if k else 0)
                    k += 1
                best = max(best, k)
            whole_cap += best * qty
        if whole_cap < sum(p.demand for p in group.pipes):
            grid = max(min_seg, group.min_weld_distance or min_seg)
            for pl in pipe_lens:
                pos = grid
                while pos < pl:
                    for seg in (pos, pl - pos):
                        if min_seg <= seg <= max_seg:
                            segs.add(seg)
                    pos += grid
    # ④ integer multiples of an offcut (several offcuts welded into one segment)
    # ⑤ pairwise sums of small offcuts.
    # Both densify the alphabet with many closely-spaced small segments.  They
    # are only needed when a tube must be spliced from several bars (pipe >
    # longest bar); for a plain 1D cutting group (every tube fits one bar) they
    # merely tempt pricing into shattered mixed cuts that explode the produced
    # segment set and the downstream cut-pattern enumerator.  Enable the densifi-
    # cation only when at least one tube genuinely needs splicing.
    if needs_splice:
        for r in list(segs):
            for k in (2, 3):
                if min_seg <= r * k <= max_seg:
                    segs.add(r * k)
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
    #
    #    Guard against alphabet explosion: a group with many *near-identical*
    #    long pipes (e.g. 10 tubes of 11280-11322mm) would otherwise contribute
    #    ten near-duplicate sweeps and hundreds of segments differing by a few
    #    mm, making per-round pricing take seconds and starving the final ILP.
    #    Quantise the swept flanks to a coarse grid so clustered pipes collapse
    #    onto shared segments, keeping the alphabet small and pricing fast.
    total_demand = sum(p.demand for p in group.pipes)
    if total_demand <= 120:
        grid = max(min_seg, group.min_weld_distance or min_seg)
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
    # Cap the alphabet.  Pricing enumerates legal sequences over this set for
    # every pipe each round; an unbounded set (749 on a 10-pipe splice cluster)
    # makes a single round take seconds.  Keep whole-bar segments and real
    # splice tails (the structurally meaningful ones) and thin the rest onto a
    # coarse grid so the set stays lean without losing splice reachability.
    ALPHA_CAP = 160
    if len(segs) > ALPHA_CAP:
        keep: set[int] = set()
        # whole-bar and pipe lengths are always meaningful
        for length in stock_lens:
            if min_seg <= length <= max_seg:
                keep.add(length)
        # real splice tails: pipe - k*bar (the segment a spliced tube needs
        # beyond whole bars) -- these unlock the tight splices
        for pl in pipe_lens:
            for length in stock_lens:
                k = 1
                while k * length < pl:
                    r = pl - k * length
                    if min_seg <= r <= max_seg:
                        keep.add(r)
                    k += 1
        # thin the remainder onto a coarse grid, preferring larger segments
        grid = max(min_seg, group.min_weld_distance or min_seg)
        rest = sorted((s for s in segs if s not in keep), reverse=True)
        seen_cell: set[int] = set()
        for s in rest:
            if len(keep) >= ALPHA_CAP:
                break
            cell = s // grid
            if cell in seen_cell:
                continue
            seen_cell.add(cell)
            keep.add(s)
        segs = keep
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
    length: int, pi: dict[int, float], kerf: int = 0, max_parts: int | None = None
) -> tuple[float, dict[int, int]]:
    """Unbounded knapsack: max Σ pi[s]·n_s s.t. Σ n_s·s + (n-1)·kerf <= length.

    Each part after the first pays one blade-margin (``kerf``) for the saw pass
    that separates it, matching ``_CutCandidate.used``.  Modelled by charging
    ``s + kerf`` per part against a capacity of ``length + kerf`` (so the first
    part gets its kerf refunded), keeping the DP one-dimensional.

    ``max_parts`` caps how many segments a single bar may be cut into.  Without
    it the value-maximising DP happily shatters a 12100mm bar into 15 tiny grid
    segments, which keeps the produced segment set irregular and makes the
    downstream type-compressor explode.  Bounding parts (2D DP over capacity x
    part-count) keeps every generated cut column as regular as the incumbent's.
    """
    cand = sorted(s for s in pi if 0 < s <= length and pi[s] > 1e-9)
    if not cand:
        return 0.0, {}
    cap_max = length + kerf
    if max_parts is None:
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

    # Bounded-parts variant: dp[p][cap] = best value using exactly-<=p parts.
    neg = float("-inf")
    dp2 = [[0.0] * (cap_max + 1) for _ in range(max_parts + 1)]
    pk2 = [[-1] * (cap_max + 1) for _ in range(max_parts + 1)]
    for p in range(1, max_parts + 1):
        for cap in range(1, cap_max + 1):
            bv, bs = dp2[p][cap - 1], -1
            for s in cand:
                cost = s + kerf
                if cost <= cap:
                    prev = dp2[p - 1][cap - cost]
                    if prev > neg:
                        v = pi[s] + prev
                        if v > bv:
                            bv, bs = v, s
            dp2[p][cap], pk2[p][cap] = bv, bs
    counts = defaultdict(int)
    p, cap = max_parts, cap_max
    while cap > 0 and p > 0:
        s = pk2[p][cap]
        if s < 0:
            cap -= 1
            continue
        counts[s] += 1
        cap -= s + kerf
        p -= 1
    return dp2[max_parts][cap_max], dict(counts)


# ---------------------------------------------------------------------------
# The solver.
# ---------------------------------------------------------------------------
class _BranchAndPrice:
    def __init__(self, group: MaterialGroup, time_limit: float, started: float):
        # Relax per-tube joints to a geometric ceiling -- but keep the relaxation
        # proportional to what the tube actually needs.  A tube that must be
        # spliced (``length`` exceeds the longest bar) gets the full geometric
        # budget; a tube that fits a single bar only gets a *small* budget
        # (``SHORT_TUBE_JOINTS``).  The incumbent splices a 5173mm tube into 2-3
        # segments, never 9 -- giving short tubes the full geometric ceiling only
        # tempts pricing into shattering them, exploding the produced segment set
        # and the joint count while never improving material.
        max_stock = max(s.length for s in group.stocks)
        pipes = []
        for p in group.pipes:
            if p.length > max_stock:
                geom = max(1, p.length // MIN_WELD_SEG - 1)
                cap = min(MAX_JOINTS_CAP, max(p.max_joints, geom))
            else:
                cap = min(MAX_JOINTS_CAP, max(p.max_joints, SHORT_TUBE_JOINTS))
            pipes.append(replace(p, max_joints=cap))
        self.g = replace(group, pipes=tuple(pipes))
        self.pipes = self.g.pipes
        self.time_limit = time_limit
        self.started = started
        self.deadline = started + time_limit
        # The lexicographic type compressor is the single biggest quality lever
        # (joints/cut/weld variety all drop to incumbent parity), so reserve half
        # the budget for it.  Column generation and the feasibility ILP run
        # against the earlier ``solve_deadline``; compression uses the full one.
        self.compress_reserve = time_limit * 0.5
        self.solve_deadline = self.deadline - self.compress_reserve
        self.min_wd = group.min_weld_distance
        self.min_cut = group.min_cut_length
        self.kerf = group.blade_margin
        self.kerf_mode = group.kerf_mode
        self.max_stock = max(s.length for s in group.stocks)
        self.target = group.target_rate
        self.stock_qty: dict[int, int] = defaultdict(int)
        for st in group.stocks:
            self.stock_qty[st.length] += st.quantity
        self.stock_qty_total = sum(self.stock_qty.values())
        self.alphabet = _derive_alphabet(self.g)
        self.cut_cols: list[tuple[int, dict[int, int]]] = []
        self.weld_cols: list[tuple[int, tuple[int, ...]]] = []
        self._seed()

    def _time_left(self) -> float:
        return self.deadline - time.monotonic()

    def _time_left_solve(self) -> float:
        """Budget left before the compression reserve kicks in."""
        return self.solve_deadline - time.monotonic()

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
        self._seed_whole()

    def _seed_whole(self) -> None:
        """Inject the incumbent's Precut skeleton: whole tubes cut whole.

        The dominant reason our plans fragment is that the LP optimum at a razor
        margin mixes offcut splices even for tubes that fit a bar whole.  The
        incumbent instead keeps every tube it can whole (a 5295mm tube stays one
        segment, packed 2-per-bar) and only splices the residual demand that no
        whole-tube packing can absorb.  We hand the model exactly that structure:
        a zero-joint weld column ``(length,)`` per fitting tube, plus cut columns
        that pack as many *whole* tubes (of one or mixed types) into each bar as
        the kerf allows.  The joint-minimising stage then naturally prefers these
        over splices, collapsing joints and the produced segment set.
        """
        fit = [(i, p) for i, p in enumerate(self.pipes) if p.length <= self.max_stock]
        for i, p in fit:
            col = (i, (p.length,))
            if col not in self.weld_cols and _legal_pattern(
                p, (p.length,), self.min_wd, self.min_cut
            ):
                self.weld_cols.append(col)
        tube_lens = sorted({p.length for _, p in fit}, reverse=True)
        for length in self.stock_qty:
            # Greedy whole-tube packings of this bar: repeatedly place the
            # longest tube that still fits (accounting for kerf between parts),
            # then also every single-type packing (k copies of one tube length).
            for start in tube_lens:
                counts: dict[int, int] = {}
                cap = length
                first = True
                order = [start] + [t for t in tube_lens if t != start]
                for t in order:
                    while (t + (0 if first else self.kerf)) <= cap:
                        counts[t] = counts.get(t, 0) + 1
                        cap -= t + (0 if first else self.kerf)
                        first = False
                if counts and not any(
                    c2 == counts and l2 == length for (l2, c2) in self.cut_cols
                ):
                    self.cut_cols.append((length, counts))

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

            # Balance injection between dual value and segment count.  Pure
            # value-first fills the pool with many-segment shatter patterns
            # (343+ joints); pure count-first collapses joints but explodes the
            # produced segment set (every tube picks a *different* 2-segment
            # split).  The incumbent does neither: it reuses a handful of splits
            # and keeps whole tubes whole.  Rounding the dual value into coarse
            # buckets lets segment count break ties within a value tier, so
            # whole/short sequences win among near-equal columns without chasing
            # a unique split per tube.
            def score(seq: tuple[int, ...]) -> tuple:
                val = sum(pi.get(s, 0.0) for s in seq)
                bucket = -round(val / max(1.0, self.max_stock * 0.05))
                return (bucket, len(seq), -val, -min(seq))

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
            # Reserve a firm slice for the final integer solve.  Pricing on a
            # splice group's dense alphabet can take several seconds per round,
            # so a 2.0s guard is not enough -- the loop could enter one more
            # expensive ``_price`` and blow the whole budget, leaving the final
            # ILP with negative time (BP then returns None and the caller falls
            # back to the far worse baseline plan).  Bail while enough remains
            # for ``_final_ilp`` to build and solve.
            ilp_reserve = max(3.0, self.time_limit * 0.15)
            if self._time_left() < ilp_reserve:
                _trace(f"cg bail time @it{it} (reserve {ilp_reserve:.1f})")
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
            # but stop as soon as the remaining solve time drops to the reserve we
            # need for the final integer solve -- the compression reserve is
            # already carved out of ``_time_left_solve``.
            # Once the demand gap is closed and utilisation meets target we have
            # a usable LP relaxation -- stop immediately.  Polishing utilisation
            # past the target only pumps more shatter columns into the pool
            # (weld count creeps up round after round) and burns the budget the
            # integer solve and the type compressor need.  A single confirming
            # round guards against a fluke gap read; then we bail with the pool
            # as lean as it will ever be.  But if the next pricing round would
            # eat the reserve, take the target hit immediately -- a solved LP in
            # hand beats a confirmation round we cannot afford.
            if gap <= RC_EPS and util >= self.target - 1e-6:
                self._target_hits = getattr(self, "_target_hits", 0) + 1
                tight = self._time_left_solve() < 2 * ilp_reserve
                if self._target_hits >= 2 or tight:
                    _trace(f"cg stop @it{it}: gap=0 util={util:.4f} "
                           f"(target met, tight={tight})")
                    return gap, used_len
            else:
                self._target_hits = 0
            added = self._price(pi, mu, gap)
            if added == 0:
                _trace(f"cg converge @it{it}: gap={gap:.2f}")
                return gap, used_len
        res = self._solve_lp()
        if res is None:
            return None
        return res[0], res[1]

    def _final_ilp(self):
        from pyscipopt import Model, quicksum, SCIP_PARAMEMPHASIS

        remaining = self._time_left()
        if remaining < 1.0:
            return None
        m = Model("bp_final")
        m.hideOutput()
        m.setParam("limits/time", remaining)
        # The pool is large (hundreds of shatter columns) and strict segment
        # balance + exact demand make a *feasible* integer point hard to hit.
        # Bias SCIP toward finding feasibility fast rather than proving the LP
        # bound; without this the min-material objective can time out with zero
        # solutions on the messy razor-margin groups (200-demand 38.1x6).
        m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
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
        # Stage 1: minimise material.
        m.setObjective(used_len, "minimize")
        m.optimize()
        if m.getNSols() == 0:
            return None
        u_star = m.getObjVal()
        _trace(f"final ilp1 solved: used={u_star:.0f}")
        # Stage 2: lock material, minimise joints.  Pattern-type variety is *not*
        # optimised here -- the coarse column pool is full of shatter patterns and
        # adding 0/1 indicators here just blows the model up.  Instead we hand the
        # feasible plan to the arc-flow lexicographic compressor below, which
        # rebuilds a tiny model on the produced segment set.
        remaining = self._time_left_solve()
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
        compressed = self._compress(weld_counts, cut_counts)
        return compressed if compressed is not None else (weld_counts, cut_counts)

    def _compress(
        self,
        weld_counts: list[tuple[_WeldCandidate, int]],
        cut_counts: list[tuple[_CutCandidate, int]],
    ) -> tuple[list, list] | None:
        """Shrink cut/weld pattern variety without hurting material or joints.

        The B&P column pool is full of shatter patterns, so the feasible plan
        typically uses far more distinct cut/weld types than the incumbent.  We
        reuse the arc-flow lexicographic compressor: it rebuilds a tiny全列式
        MILP on the *produced segment set* (usually <=16 lengths) and minimises
        weld-types -> cut-types -> seg-types while pinning ``joints`` and
        ``used_len`` to the incumbent plan (so quality never regresses).
        """
        return compress_plan(
            self.g,
            self.pipes,
            self.stock_qty,
            self.kerf,
            self.kerf_mode,
            weld_counts,
            cut_counts,
            self._time_left(),
        )

    def solve(self):
        cg = self._column_generation()
        if cg is None:
            _trace("root LP infeasible")
            return None
        gap, _ = cg
        if gap > 1e-6:
            _trace(f"gap not closed: {gap:.2f}")
            return None
        _trace(f"entering final_ilp, time_left={self._time_left():.1f}")
        return self._final_ilp()


def compress_plan(
    group: MaterialGroup,
    pipes: list,
    stock_qty: dict[int, int],
    kerf: float,
    kerf_mode: Any,
    weld_counts: list[tuple[_WeldCandidate, int]],
    cut_counts: list[tuple[_CutCandidate, int]],
    time_budget: float,
) -> tuple[list, list] | None:
    """Shrink cut/weld pattern variety without hurting material or joints.

    Both the B&P fallback and the R&CG main engine assemble patterns from a
    coarse column pool, so their feasible plans use far more distinct cut/weld
    types (and joints) than the incumbent.  Rebuild a tiny全列式 MILP on the
    *produced segment set* (usually <=28 lengths) and minimise weld-types ->
    cut-types -> seg-types -> joints while pinning ``joints`` and ``used_len``
    to the input plan, so quality can only improve.  Returns the compressed
    ``(weld_counts, cut_counts)`` or ``None`` when compression is skipped.
    """
    if time_budget < 3.0:
        return None
    try:
        from _arcflow_v3 import _compress_types
    except Exception as exc:  # noqa: BLE001
        _trace(f"compress import failed: {exc}")
        return None

    seg_set: set[int] = set()
    j_cap = 0
    u_cap = 0
    max_parts = 1
    for cand, n in cut_counts:
        for s in cand.parts:
            seg_set.add(s)
        u_cap += cand.stock_length * n
    for cand, n in weld_counts:
        for s in cand.parts:
            seg_set.add(s)
        j_cap += (len(cand.parts) - 1) * n
        max_parts = max(max_parts, len(cand.parts))
    if not seg_set:
        return None
    # The cut-pattern enumerator is combinatorial in the segment count: a
    # 12-length set yields ~5k cut columns, a 15-length set explodes past
    # 500k on unbounded parts.  Bounding parts to what the plan uses keeps a
    # 20-length set inside the column guard; only give up when the set is so
    # rich even bounded enumeration would be pointless.
    if len(seg_set) > 28:
        _trace(f"compress skip: seg_set={len(seg_set)} too rich")
        return None
    # Cap the compressor's cut-pattern parts to a small constant regardless of
    # how fragmented the plan's cut columns are: the incumbent never packs more
    # than ~4-5 functional segments per bar, so a small fixed cap lets the
    # enumerator rebuild *regular* cut patterns inside the MILP's column guard.
    cut_cap = min(5, len(seg_set))
    _trace(f"compress: seg_set={len(seg_set)} cut_cap={cut_cap}")

    # Tighten each tube's joint ceiling to what the plan actually uses (+head
    # room via max_parts) so the enumerator cannot spawn shatter patterns.
    comp_pipes = [replace(p, max_joints=min(p.max_joints, max_parts)) for p in pipes]
    # Split the reserve across the four lexicographic passes (Pass3 types,
    # Pass4 cut-types, Pass5 seg-types, Pass6 min-joints).  Give Pass4 the
    # largest slice; retry once if the compressor bails while budget remains
    # (SCIP's search is non-deterministic so a fresh solve usually clears it).
    budget = time_budget * 0.85
    pass_tl = max(4.0, budget * 0.22)
    cut_pass_tl = max(4.0, budget * 0.34)
    verbose = bool(_os.environ.get("BP_TRACE"))
    res = None
    for attempt in range(2):
        try:
            res = _compress_types(
                group,
                comp_pipes,
                seg_set,
                dict(stock_qty),
                j_cap=j_cap,
                u_cap=u_cap,
                tl=pass_tl,
                verbose=verbose,
                cut_tl=cut_pass_tl,
                cut_max_parts=cut_cap,
            )
        except Exception as exc:  # noqa: BLE001
            _trace(f"compress raised: {exc}")
            res = None
        if res and res.get("weld_patterns") and res.get("cut_patterns"):
            break
        if attempt == 0 and budget - (pass_tl + cut_pass_tl) < pass_tl + cut_pass_tl:
            break
        _trace(f"compress attempt {attempt + 1} empty -> retry")
    if not res or not res.get("weld_patterns") or not res.get("cut_patterns"):
        return None

    new_weld: list[tuple[_WeldCandidate, int]] = []
    for (idx, seq), n in res["weld_patterns"].items():
        new_weld.append((_WeldCandidate(idx, tuple(seq)), n))
    new_cut: list[tuple[_CutCandidate, int]] = []
    for (length, segs), n in res["cut_patterns"].items():
        new_cut.append((_CutCandidate(length, tuple(segs), kerf, kerf_mode), n))
    _trace(
        f"compress: weld_types {len(weld_counts)}->{res.get('weld_types')} "
        f"cut_types {len(cut_counts)}->{res.get('cut_types')} "
        f"joints {j_cap}->{res.get('joints')}"
    )
    return new_weld, new_cut


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
