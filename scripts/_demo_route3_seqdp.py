"""Route-3 demo: sequential per-stock heuristic for large-scale (types-heavy /
1000+ bar) divisible-items cutting-stock-with-welding.

Motivation (see docs/research §11.17): baseline two-layer MILP and route2
equivalent-stock CSP both time out on groups with 1000+ real bars and hundreds
of distinct segment lengths, because they enumerate/solve a *global* model.  The
MKA steel-industry solution (arXiv 1606.01419) instead builds the plan
*sequentially* -- one stock/pipe at a time via a small DP -- so cost grows
LINEARLY in the number of bars.  This demo implements that idea for our schema.

Problem shape in our data: pipes are long (~22000 mm) and stock bars short
(6500-12400 mm), so every pipe is a WELD CHAIN of 2-3 bar pieces.  The natural
sequential construction is therefore *per-pipe assembly*:

  1. Order pipes by a fixed strategy (default LPT: longest first -- hardest to
     place gets first pick of the best material).
  2. To build one pipe of length L, weld bar pieces end-to-end until L is
     reached, cutting the last bar to fit.  A running pool of "usable leftovers"
     (offcuts >= min_reusable_remnant) is consumed first before opening a new
     whole bar -- this is the usable-leftover heuristic that lifts utilisation.
  3. Every weld position is checked against min_weld_distance, max_joints and
     the pipe's forbidden zones; every cut piece against min_cut_length.
  4. Leftover from the final bar re-enters the pool for the next pipe.

This is a FIRST VERSION with a fixed order (no tuning yet).  The goal is only to
answer: does sequential assembly SOLVE the groups the global solvers cannot,
and at what utilisation vs the legacy software?  Order tuning / local search is
a deliberate follow-up (docs §11.17 route3 second stage).

Usage:
    python scripts/_demo_route3_seqdp.py --sample-file d:/07-codeing/12-plrj/_sample_55S2212MX.json
    python scripts/_demo_route3_seqdp.py --sample-id <uuid>   # pull from samples.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.domain import MaterialGroup, from_units, parse_problem  # noqa: E402


# ---------------------------------------------------------------------------
# Stock pool: a multiset of available bar lengths plus a growing pool of usable
# leftovers.  ``take`` consumes the shortest piece that is still >= a needed
# length (best-fit), or the longest whole bar when nothing fits, so short pieces
# are burned down first.
# ---------------------------------------------------------------------------


class _BarInstance:
    """One physical stock bar that has been opened.  Tracks the segments already
    cut from it (in order) so the per-bar cutting pattern can be reconstructed
    for the verifier; ``remain`` is the current usable tail length."""

    __slots__ = ("bar_id", "stock_length", "remain", "cuts")

    def __init__(self, bar_id: int, stock_length: int) -> None:
        self.bar_id = bar_id
        self.stock_length = stock_length
        self.remain = stock_length
        self.cuts: list[int] = []


class StockPool:
    def __init__(self, group: MaterialGroup) -> None:
        # whole[length] = remaining count of untouched bars of that length.
        self.whole: dict[int, int] = defaultdict(int)
        self.must_use: dict[int, int] = {}
        for s in group.stocks:
            self.whole[s.length] += s.quantity
            if s.must_use_quantity:
                self.must_use[s.length] = s.must_use_quantity
        # leftovers: list of reusable offcut lengths (>= min_reusable_remnant).
        self.leftovers: list[int] = []
        # A leftover is only worth pooling if it can serve as a legal welded
        # stub later; anything shorter than min_seg is trim loss, not stock.
        self.min_seg = max(group.min_cut_length, group.min_weld_distance, 1)
        self.min_remnant = max(self.min_seg, group.min_reusable_remnant)
        self.blade = group.blade_margin
        # bookkeeping
        self.bars_used: dict[int, int] = defaultdict(int)
        self.total_whole = sum(self.whole.values())
        # --- physical-bar tracking (for verifier-grade cutting patterns) ---
        # Every opened bar gets a _BarInstance; a pooled leftover carries the id
        # of the bar it was cut from, so a segment cut off a leftover is charged
        # to the original bar.  ``leftover_src[i]`` is the bar id for
        # ``self.leftovers[i]``.
        self._next_bar_id = 0
        self.bars: dict[int, _BarInstance] = {}
        self.leftover_src: list[int] = []

    def total_available_length(self) -> int:
        return (
            sum(length * n for length, n in self.whole.items())
            + sum(self.leftovers)
        )

    def _remaining_must_use(self) -> int:
        used_must = sum(
            min(self.bars_used.get(length, 0), req)
            for length, req in self.must_use.items()
        )
        return sum(self.must_use.values()) - used_must

    def _open_whole(self, length: int) -> _BarInstance:
        self.whole[length] -= 1
        self.bars_used[length] += 1
        bar = _BarInstance(self._next_bar_id, length)
        self._next_bar_id += 1
        self.bars[bar.bar_id] = bar
        return bar

    def _charge_cut(self, bar: _BarInstance, part: int) -> None:
        """Record one physical cut of length ``part`` on ``bar`` and deduct it
        from ``remain``.  Kerf is NOT deducted here; it is reserved when the
        remainder is re-pooled (see ``_pool_remainder``) so that consuming a
        leftover whole does not double-charge a kerf."""
        bar.cuts.append(part)
        bar.remain -= part

    def _pool_remainder(self, bar: _BarInstance, min_remnant: int) -> None:
        """Re-pool a bar's remainder as a reusable leftover, RESERVING one kerf for
        the next cut (verifier charges kerf per additional part).  The reserved
        kerf is deducted from ``remain`` too, so ``remain`` and the pooled leftover
        length stay in lock-step and a later cut cannot overrun the bar."""
        bar.remain -= self.blade  # reserve the next cut's kerf physically
        if bar.remain >= min_remnant:
            self.leftovers.append(bar.remain)
            self.leftover_src.append(bar.bar_id)

    def open_exact(self, part: int, kerf_extra: int = 0) -> tuple[str, int, int] | None:
        """Weld out a segment of usable length ``part``.  A pooled leftover whose
        length exactly equals ``part`` is welded whole (it becomes the parent
        bar's next recorded cut); otherwise a whole bar of that exact length is
        opened and cut flush.  Every welded-out segment is charged as one cut on
        its parent bar via ``_charge_cut`` so the cutting side balances the
        welding side.  ``kerf_extra`` ignored (kerf is a per-cut cost)."""
        for i, piece in enumerate(self.leftovers):
            if piece == part:
                self.leftovers.pop(i)
                bar_id = self.leftover_src.pop(i)
                self._charge_cut(self.bars[bar_id], part)
                return ("leftover", part, bar_id)
        if self.whole.get(part, 0) > 0:
            bar = self._open_whole(part)
            self._charge_cut(bar, part)
            return ("whole", part, bar.bar_id)
        return None

    def open_finishing(
        self, part: int, min_remnant: int, kerf_extra: int = 0
    ) -> tuple[str, int, int] | None:
        """Weld out a finishing segment of usable length ``part``.  Prefer the
        smallest pooled leftover that can yield it: if the leftover equals
        ``part`` it is welded whole; if larger it is cut, charging one kerf on the
        parent bar and re-pooling the new remainder.  Else open a whole bar.
        Every welded-out segment is charged as one cut.  ``kerf_extra`` ignored."""
        exact_i = None
        best_i = None
        best_len = None
        for i, piece in enumerate(self.leftovers):
            if piece == part:
                exact_i = i
                break
            # a further cut off this leftover reserves one more kerf.
            if piece >= part + self.blade and (best_len is None or piece < best_len):
                best_len, best_i = piece, i
        if exact_i is not None:
            self.leftovers.pop(exact_i)
            bar_id = self.leftover_src.pop(exact_i)
            self._charge_cut(self.bars[bar_id], part)
            return ("leftover", part, bar_id)
        if best_i is not None:
            self.leftovers.pop(best_i)
            bar_id = self.leftover_src.pop(best_i)
            bar = self.bars[bar_id]
            self._charge_cut(bar, part)
            self._pool_remainder(bar, min_remnant)
            return ("leftover", part, bar_id)
        # whole bar: first cut on it; remainder re-pools with a reserved kerf.
        covering = [length for length, n in self.whole.items() if n > 0 and length >= part]
        if covering:
            length = min(covering, key=lambda L: self._tail_dead(L - part, min_remnant))
            bar = self._open_whole(length)
            self._charge_cut(bar, part)
            self._pool_remainder(bar, min_remnant)
            return ("whole", part, bar.bar_id)
        return None

    def _tail_dead(self, tail: int, min_remnant: int) -> int:
        if tail <= 0:
            return 0
        return 0 if tail >= min_remnant else tail

    def snapshot(self) -> dict[str, Any]:
        """Capture enough state to exactly restore the pool after a failed pipe
        assembly.  Cheaper and provably-correct vs surgical undo, since a pipe
        touches only a handful of bars but the reversal interactions (kerf, tail
        re-pooling, exact-match welds) are error-prone to invert in place."""
        return {
            "whole": dict(self.whole),
            "leftovers": list(self.leftovers),
            "leftover_src": list(self.leftover_src),
            "bars_used": dict(self.bars_used),
            "bars": {bid: (b.stock_length, b.remain, list(b.cuts)) for bid, b in self.bars.items()},
            "next_bar_id": self._next_bar_id,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.whole = defaultdict(int, snap["whole"])
        self.leftovers = list(snap["leftovers"])
        self.leftover_src = list(snap["leftover_src"])
        self.bars_used = defaultdict(int, snap["bars_used"])
        self.bars = {}
        for bid, (sl, rem, cuts) in snap["bars"].items():
            b = _BarInstance(bid, sl)
            b.remain = rem
            b.cuts = list(cuts)
            self.bars[bid] = b
        self._next_bar_id = snap["next_bar_id"]

    def return_leftover(self, length: int, bar_id: int) -> None:
        if length >= self.min_remnant:
            self.leftovers.append(length)
            self.leftover_src.append(bar_id)


def _assemble_pipe(
    pool: StockPool,
    pipe_length: int,
    max_joints: int,
    forbidden: tuple[tuple[int, int], ...],
    kerf: int,
    min_seg: int,
    min_cut: int,
    prefer_few_pieces: bool = False,
) -> dict[str, Any] | None:
    """Weld pieces end-to-end into one pipe of ``pipe_length`` via a bounded DP.

    Instead of an incremental greedy (which strands sub-min_seg gaps on
    ultra-tight groups), we search over combinations of available piece lengths
    (distinct whole-bar lengths + pooled leftovers, respecting counts) up to
    ``max_joints + 1`` pieces, and keep the assembly with the least DEAD trim
    loss (tail neither flush nor reusable), tie-broken by fewest welds then least
    long-stock consumed.  The pipe/bar ratio is small (2-4 pieces), so this stays
    cheap.  Returns {segments, welds} on success or None if no legal assembly
    exists with the current pool.

    ``prefer_few_pieces`` flips the tie-break to favour FEWER welds before less
    waste.  This is used for tightly joint-capped pipes (small ``max_joints``):
    they must be built from a few long pieces, so consuming whole/long bars now
    and leaving short offcuts for loosely-capped pipes later is globally better
    than a locally-minimal-waste build that shreds the long stock.
    """

    # A pipe's welded segments sum EXACTLY to its length: welding fuses pieces
    # with no material loss.  Kerf is a CUTTING cost only (saw blade), charged by
    # the pool when a bar is physically cut into 2+ pieces (see _charge_cut).  The
    # DFS below therefore plans segments with NO inter-weld kerf.
    # A pipe needs at most ceil(pipe_length / smallest_usable_piece) pieces; cap
    # the search depth to that (and the joint budget), so deep max_joints values
    # (e.g. 7) do not blow up the DFS when only 2-4 pieces can physically fit.
    smallest = min(
        [length for length, n in pool.whole.items() if n > 0] + pool.leftovers,
        default=pipe_length,
    )
    phys_pieces = pipe_length // max(smallest, 1) + 2
    max_pieces = min(max_joints + 1, phys_pieces)
    min_remnant = pool.min_remnant

    # Candidate supply: distinct lengths available right now, each with a count
    # and a "kind" tag (must-use bars are forced first, so surface them too).
    supply: dict[int, int] = defaultdict(int)
    for length, n in pool.whole.items():
        if n > 0:
            supply[length] += n
    for piece in pool.leftovers:
        supply[piece] += 1
    if not supply:
        return None
    distinct = sorted(supply.keys(), reverse=True)

    # DFS over piece choices in NON-INCREASING length order (so we never explore
    # the same multiset twice) summing (with kerf between welds) to pipe_length.
    best: dict[str, Any] | None = None
    max_len = distinct[0]
    # Hard node budget: a construction heuristic must stay cheap per pipe even as
    # the leftover pool grows the candidate set; we keep the best found so far.
    budget = [1200]

    def _consider(segments: list[int], welds: int, waste: int) -> None:
        nonlocal best
        acc = 0
        weld_positions = []
        for seg in segments[:-1]:
            acc += seg
            weld_positions.append(acc)
        for pos in weld_positions:
            if any(s <= pos <= e for s, e in forbidden):
                return
        if prefer_few_pieces:
            # joint-capped pipe: minimise welds first (use few long pieces),
            # then waste, then prefer keeping large segments.
            key = (welds, waste, -min(segments))
        else:
            key = (waste, welds, -min(segments))
        if best is None or key < best["key"]:
            best = {"segments": list(segments), "welds": welds, "waste": waste, "key": key}

    def _done() -> bool:
        # A flush/reusable, single-piece (no weld) solution is unbeatable.
        return best is not None and best["waste"] == 0 and best["welds"] == 0

    def _dfs(si: int, remaining: int, segments: list[int], taken: dict[int, int]) -> None:
        if budget[0] <= 0 or _done():
            return
        budget[0] -= 1
        # Finish here with a covering piece (the last segment).  Try the smallest
        # covering length first: it minimises the stranded tail, so a zero-dead
        # finisher (flush or reusable) is found fast and cuts the search.
        if not segments or remaining >= min_seg:
            if remaining >= min_cut:
                # smallest covering length first -> smallest tail -> zero-dead fast
                for length in reversed(distinct):
                    if length < remaining:
                        continue
                    if supply[length] - taken.get(length, 0) <= 0:
                        continue
                    tail = length - remaining
                    dead = tail if 0 < tail < min_remnant else 0
                    _consider(segments + [remaining], len(segments), dead)
                    if dead == 0:
                        break  # cannot do better than a zero-dead finisher here
        if len(segments) + 1 >= max_pieces:
            return
        # Weld a whole piece in; only consider lengths <= the last welded piece
        # (non-increasing) to avoid exploring permutations of the same multiset.
        for j in range(si, len(distinct)):
            if budget[0] <= 0 or _done():
                return
            length = distinct[j]
            if supply[length] - taken.get(length, 0) <= 0:
                continue
            usable = length  # welding is loss-free; kerf is a cutting cost only
            if usable < min_seg:
                continue
            if usable >= remaining:
                continue  # a finisher, handled above
            # prune: remaining after this piece must still be coverable by <= the
            # remaining piece slots * max piece length.
            new_remaining = remaining - usable
            slots = max_pieces - (len(segments) + 1)
            if new_remaining > slots * max_len:
                continue
            taken[length] = taken.get(length, 0) + 1
            _dfs(j, new_remaining, segments + [usable], taken)
            taken[length] -= 1

    _dfs(0, pipe_length, [], {})
    if best is None:
        return None

    # Commit: pull the exact pieces the winning assembly used from the pool.  A
    # pipe touches only a few bars; snapshot-and-restore the pool on any failure
    # so inventory + kerf bookkeeping stay exactly consistent (surgical undo of
    # the kerf/tail-repool interactions is error-prone).
    segments = best["segments"]
    welds = best["welds"]
    snap = pool.snapshot()
    consumed: list[tuple[str, int, int]] = []

    for i, seg in enumerate(segments):
        finishing = i == len(segments) - 1
        if finishing:
            piece = pool.open_finishing(seg, min_remnant)
        else:
            piece = pool.open_exact(seg)
        if piece is None:
            pool.restore(snap)
            return None
        consumed.append(piece)
    return {"segments": segments, "welds": welds, "consumed": consumed}


def _expand_instances(group: MaterialGroup) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for p in group.pipes:
        fb = tuple((iv.start, iv.end) for iv in p.forbidden)
        for _ in range(p.demand):
            instances.append(
                {
                    "pipe_id": p.pipe_id,
                    "length": p.length,
                    "max_joints": p.max_joints,
                    "forbidden": fb,
                }
            )
    return instances


def _order_instances(instances: list[dict[str, Any]], order: str, rng: Any = None) -> list[dict[str, Any]]:
    seq = list(instances)
    if order == "lpt":  # longest processing time first
        seq.sort(key=lambda it: it["length"], reverse=True)
    elif order == "spt":  # shortest first
        seq.sort(key=lambda it: it["length"])
    elif order == "tight":  # most-constrained first: fewest joints, then longest
        seq.sort(key=lambda it: (it["max_joints"], -it["length"]))
    elif order == "loose":  # most joints allowed first, then longest
        seq.sort(key=lambda it: (-it["max_joints"], -it["length"]))
    elif order == "shuffle":  # randomised restart for local search
        if rng is not None:
            rng.shuffle(seq)
    # "given": keep as-is
    return seq


def _run_single_order(
    group: MaterialGroup,
    instances: list[dict[str, Any]],
    kerf: int,
    min_seg: int,
    min_cut: int,
    few_piece_cap: int | None = None,
) -> dict[str, Any]:
    """One sequential construction pass over ``instances`` (already ordered).

    Pipes whose ``max_joints`` <= ``few_piece_cap`` are assembled in
    "prefer few pieces" mode so they grab long/whole stock before it is shredded;
    loosely-capped pipes then mop up the resulting short offcuts.

    A second RECOVERY pass then retries every failed pipe against the pool as it
    stands after the first pass (leftovers have grown), because a pipe that could
    not be built early may now be buildable from accumulated offcuts."""

    pool = StockPool(group)
    orders: list[dict[str, Any]] = []
    made = 0
    total_welds = 0
    pending_failures: list[dict[str, Any]] = []

    def _few(inst: dict[str, Any]) -> bool:
        return few_piece_cap is not None and inst["max_joints"] <= few_piece_cap

    for inst in instances:
        res = _assemble_pipe(
            pool, inst["length"], inst["max_joints"], inst["forbidden"], kerf, min_seg, min_cut,
            prefer_few_pieces=_few(inst),
        )
        if res is None:
            pending_failures.append(inst)
            continue
        made += 1
        total_welds += res["welds"]
        orders.append({"pipe_id": inst["pipe_id"], "length": inst["length"], **res})

    # Recovery pass: retry failures until no further progress (leftovers keep
    # changing as recovered pipes consume/return fragments).
    progress = True
    while pending_failures and progress:
        progress = False
        still: list[dict[str, Any]] = []
        for inst in pending_failures:
            res = _assemble_pipe(
                pool, inst["length"], inst["max_joints"], inst["forbidden"], kerf, min_seg, min_cut,
                prefer_few_pieces=_few(inst),
            )
            if res is None:
                still.append(inst)
                continue
            made += 1
            total_welds += res["welds"]
            orders.append({"pipe_id": inst["pipe_id"], "length": inst["length"], **res})
            progress = True
        pending_failures = still

    failed = len(pending_failures)
    used_len = sum(length * n for length, n in pool.bars_used.items())
    produced_len = sum(o["length"] for o in orders)
    util = produced_len / used_len if used_len else 0.0
    return {
        "made": made,
        "failed": failed,
        "welds": total_welds,
        "bars_used": dict(pool.bars_used),
        "n_bars_used": sum(pool.bars_used.values()),
        "used_len": used_len,
        "produced_len": produced_len,
        "util": util,
        "orders": orders,
        "pool": pool,
    }


_ALL_ORDERS = ["lpt", "spt", "tight", "loose", "given"]


def solve_route3(group: MaterialGroup, order: str = "auto", restarts: int = 0, seed: int = 12345) -> dict[str, Any]:
    """Solve one group by sequential DP assembly.

    ``order='auto'`` tries every deterministic ordering strategy and, when
    ``restarts > 0``, additionally runs that many RANDOMISED restarts (shuffle
    the pipe order) as a lightweight local-search layer (docs §11.17 route3
    stage-2).  The best result by the production lexicographic objective is kept:
    (1) fewest failed pipes, (2) highest utilisation, (3) fewest welds.  A single
    named order runs just that one (restarts still apply if > 0)."""

    import random

    started = time.monotonic()
    kerf = group.blade_margin
    min_seg = max(group.min_cut_length, group.min_weld_distance, 1)
    min_cut = group.min_cut_length

    base = _expand_instances(group)
    demand_total = len(base)
    total_stock_len = group.stock_length
    demand_len = group.demand_length

    orders_to_try = _ALL_ORDERS if order == "auto" else [order]

    best: dict[str, Any] | None = None
    best_order = None
    attempts: list[dict[str, Any]] = []

    def _try(od: str, seq: list[dict[str, Any]], few_cap: int | None = None) -> None:
        nonlocal best, best_order
        r = _run_single_order(group, seq, kerf, min_seg, min_cut, few_piece_cap=few_cap)
        tag = od if few_cap is None else f"{od}+fp{few_cap}"
        attempts.append({"order": tag, "made": r["made"], "failed": r["failed"], "util": round(r["util"], 4)})
        key = (r["failed"], -round(r["util"], 6), r["welds"])
        if best is None or key < best["key"]:
            best = {**r, "key": key}
            best_order = tag

    # Joint caps present in this group -> candidate few-piece thresholds so that
    # the tightest-capped pipes are built from long stock before it is shredded.
    joint_caps = sorted({p.max_joints for p in group.pipes})
    few_caps: list[int | None] = [None]
    if len(joint_caps) > 1:
        # protect the tightest tier, and the tightest two tiers
        few_caps.append(joint_caps[0])
        if len(joint_caps) > 2:
            few_caps.append(joint_caps[1])

    for od in orders_to_try:
        seq = _order_instances(base, od)
        for fc in few_caps:
            _try(od, seq, few_cap=fc)
            if best is not None and best["failed"] == 0:
                break
        if best is not None and best["failed"] == 0:
            break

    rng = random.Random(seed)
    if not (best is not None and best["failed"] == 0):
        for k in range(restarts):
            _try(f"shuffle#{k}", _order_instances(base, "shuffle", rng), few_cap=joint_caps[0] if len(joint_caps) > 1 else None)
            # early exit once fully solved -- no point burning more restarts
            if best is not None and best["failed"] == 0:
                break

    assert best is not None
    status = "SOLVED" if best["failed"] == 0 else "PARTIAL"
    return {
        "status": status,
        "order": best_order,
        "attempts": attempts,
        "made": best["made"],
        "failed": best["failed"],
        "demand_total": demand_total,
        "welds": best["welds"],
        "bars_used": best["bars_used"],
        "n_bars_used": best["n_bars_used"],
        "used_len": best["used_len"],
        "produced_len": best["produced_len"],
        "util": round(best["util"], 4),
        "demand_len": demand_len,
        "stock_len": total_stock_len,
        "tightness": round(demand_len / total_stock_len, 4) if total_stock_len else None,
        "elapsed": round(time.monotonic() - started, 3),
        "orders": best["orders"],
        "pool": best["pool"],
    }


def _rebuild_from_instances(
    group: MaterialGroup,
    kept: list[dict[str, Any]],
    retry: list[dict[str, Any]],
    kerf: int,
    min_seg: int,
    min_cut: int,
    few_piece_cap: int | None,
) -> dict[str, Any]:
    """Replay ``kept`` pipe instances (in order) into a fresh pool, then assemble
    ``retry`` pipes against the resulting (larger) free pool.  Assembly is
    deterministic given pool state + pipe, so replaying kept pipes reproduces
    their exact allocations; the freed material is then available to retry.  A
    final recovery pass mops up remaining failures.  Returns the same result
    dict shape as ``_run_single_order``."""
    pool = StockPool(group)
    orders: list[dict[str, Any]] = []
    made = 0
    total_welds = 0

    def _few(inst: dict[str, Any]) -> bool:
        return few_piece_cap is not None and inst["max_joints"] <= few_piece_cap

    pending: list[dict[str, Any]] = []
    for inst in kept + retry:
        res = _assemble_pipe(
            pool, inst["length"], inst["max_joints"], inst["forbidden"],
            kerf, min_seg, min_cut, prefer_few_pieces=_few(inst),
        )
        if res is None:
            pending.append(inst)
            continue
        made += 1
        total_welds += res["welds"]
        orders.append({"pipe_id": inst["pipe_id"], "length": inst["length"], **res})

    progress = True
    while pending and progress:
        progress = False
        still: list[dict[str, Any]] = []
        for inst in pending:
            res = _assemble_pipe(
                pool, inst["length"], inst["max_joints"], inst["forbidden"],
                kerf, min_seg, min_cut, prefer_few_pieces=_few(inst),
            )
            if res is None:
                still.append(inst)
                continue
            made += 1
            total_welds += res["welds"]
            orders.append({"pipe_id": inst["pipe_id"], "length": inst["length"], **res})
            progress = True
        pending = still

    failed = len(pending)
    used_len = sum(length * n for length, n in pool.bars_used.items())
    produced_len = sum(o["length"] for o in orders)
    return {
        "made": made,
        "failed": failed,
        "welds": total_welds,
        "bars_used": dict(pool.bars_used),
        "n_bars_used": sum(pool.bars_used.values()),
        "used_len": used_len,
        "produced_len": produced_len,
        "util": produced_len / used_len if used_len else 0.0,
        "orders": orders,
        "pool": pool,
    }


def _lns_key(r: dict[str, Any], cut_types: int, weld_types: int) -> tuple:
    """Production lexicographic objective for LNS acceptance (docs §11.20 四):
    (1) fewest failed pipes  -> rescue is paramount,
    (2) fewest pattern types -> user's #1 pain (cut+weld types),
    (3) highest utilisation,
    (4) fewest welds."""
    return (r["failed"], cut_types + weld_types, -round(r["util"], 6), r["welds"])


def _pattern_type_counts(res: dict[str, Any]) -> tuple[int, int]:
    """Count distinct cutting patterns (stock_length, sorted cuts) and welding
    patterns (ordered segments) in a route3 result -- matches the verifier's
    type identities and thus the legacy-baseline comparison."""
    cut_ids = {
        (bar.stock_length, tuple(sorted(bar.cuts)))
        for bar in res["pool"].bars.values()
        if bar.cuts
    }
    weld_ids = {tuple(o["segments"]) for o in res["orders"]}
    return len(cut_ids), len(weld_ids)


def lns_improve(
    group: MaterialGroup,
    res: dict[str, Any],
    iterations: int = 40,
    destroy_k: int = 8,
    seed: int = 20260716,
    few_piece_cap: int | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Naive Large-Neighbourhood-Search outer loop over a route3 initial solution
    (docs §11.20 前置③; §11.23).

    Each iteration DESTROYS the ``destroy_k`` most wasteful bars (largest trim
    loss) -- freeing their material and re-queuing every pipe that consumed a
    piece from them -- then REPAIRS by replaying the kept pipes and re-assembling
    the freed+failed pipes against the enlarged pool.  A strictly lexicographic
    improvement (fewest failed, fewest pattern types, highest util, fewest welds)
    is accepted; otherwise the incumbent is kept.  Pure destroy-and-rebuild keeps
    every intermediate solution verifier-valid by construction."""
    import random

    rng = random.Random(seed)
    kerf = group.blade_margin
    min_seg = max(group.min_cut_length, group.min_weld_distance, 1)
    min_cut = group.min_cut_length

    inst_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for inst in _expand_instances(group):
        inst_by_id[inst["pipe_id"]].append(inst)

    def order_to_instance(o: dict[str, Any]) -> dict[str, Any]:
        # any instance for this pipe_id is interchangeable (same length/joints/fb)
        return inst_by_id[o["pipe_id"]][0]

    best = res
    best_ct, best_wt = _pattern_type_counts(best)
    best_key = _lns_key(best, best_ct, best_wt)
    accepted = 0

    for it in range(iterations):
        pool = best["pool"]
        # rank bars by trim loss (stock_length - consumed), most wasteful first;
        # only bars that actually hold cuts are candidates.
        bars = [b for b in pool.bars.values() if b.cuts]
        if not bars:
            break
        def trim(b: _BarInstance) -> int:
            return b.stock_length - (sum(b.cuts) + kerf * (len(b.cuts) - 1))
        bars.sort(key=trim, reverse=True)
        # destroy: the K worst + a couple of random bars for diversification.
        victims = set(id(b) for b in bars[:destroy_k])
        extra = rng.sample(bars, min(2, len(bars)))
        for b in extra:
            victims.add(id(b))

        # any order (pipe) that consumed material from a victim bar is freed.
        kept_orders: list[dict[str, Any]] = []
        retry_orders: list[dict[str, Any]] = []
        for o in best["orders"]:
            touched = any(id(pool.bars.get(bar_id)) in victims for _, _, bar_id in o["consumed"])
            (retry_orders if touched else kept_orders).append(o)

        # also carry the currently-failed pipes as retry targets.
        made_ids: list[str] = [o["pipe_id"] for o in best["orders"]]
        from collections import Counter
        need = Counter(i["pipe_id"] for lst in inst_by_id.values() for i in lst)
        have = Counter(made_ids)
        failed_insts: list[dict[str, Any]] = []
        for pid, n in need.items():
            miss = n - have.get(pid, 0)
            for _ in range(miss):
                failed_insts.append(inst_by_id[pid][0])

        kept_insts = [order_to_instance(o) for o in kept_orders]
        retry_insts = [order_to_instance(o) for o in retry_orders] + failed_insts

        cand = _rebuild_from_instances(
            group, kept_insts, retry_insts, kerf, min_seg, min_cut, few_piece_cap
        )
        ct, wt = _pattern_type_counts(cand)
        key = _lns_key(cand, ct, wt)
        if key < best_key:
            best, best_key, best_ct, best_wt = cand, key, ct, wt
            accepted += 1
            if verbose:
                print(
                    f"    iter {it:>3}: ACCEPT failed={cand['failed']} "
                    f"cut={ct} weld={wt} util={cand['util']:.5f} welds={cand['welds']}"
                )

    demand_total = sum(len(v) for v in inst_by_id.values())
    total_stock_len = group.stock_length
    demand_len = group.demand_length
    return {
        "status": "SOLVED" if best["failed"] == 0 else "PARTIAL",
        "order": "lns",
        "attempts": [],
        "made": best["made"],
        "failed": best["failed"],
        "demand_total": demand_total,
        "welds": best["welds"],
        "bars_used": best["bars_used"],
        "n_bars_used": best["n_bars_used"],
        "used_len": best["used_len"],
        "produced_len": best["produced_len"],
        "util": round(best["util"], 4),
        "demand_len": demand_len,
        "stock_len": total_stock_len,
        "tightness": round(demand_len / total_stock_len, 4) if total_stock_len else None,
        "elapsed": 0.0,
        "orders": best["orders"],
        "pool": best["pool"],
        "lns_accepted": accepted,
        "lns_iterations": iterations,
    }


def route3_to_solution(
    group: MaterialGroup, res: dict[str, Any], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Convert a route3 result into the baseline solution schema so it can be
    fed to app.verifier.verify_solution.  Welding patterns are aggregated by
    (pipe identity, ordered parts); cutting patterns by (stock_length, sorted
    parts).  Per-bar cut records come from the tracked pool, which guarantees
    segment balance (every welded segment was physically cut from a bar)."""
    from app.domain import from_units

    pipe_by_id = {p.pipe_id: p for p in group.pipes}

    # --- welding patterns: group by (pipe_id, parts tuple) ---
    weld_agg: dict[tuple[str, tuple[int, ...]], int] = defaultdict(int)
    for o in res["orders"]:
        parts = tuple(o["segments"])
        weld_agg[(o["pipe_id"], parts)] += 1

    welding_patterns: list[dict[str, Any]] = []
    for idx, ((pipe_id, parts), qty) in enumerate(sorted(weld_agg.items())):
        p = pipe_by_id[pipe_id]
        positions: list[Any] = []
        acc = 0
        for part in parts[:-1]:
            acc += part
            positions.append(from_units(acc))
        welding_patterns.append(
            {
                "pattern_id": f"W{idx}",
                "pipe_id": pipe_id,
                "figure_number": p.figure_number,
                "parent_node": p.parent_node,
                "jlxh": p.jlxh,
                "cube_no": p.cube_no,
                "pipe_length": from_units(p.length),
                "parts": [from_units(x) for x in parts],
                "weld_positions": positions,
                "quantity": qty,
                "joint_count": len(parts) - 1,
            }
        )

    # --- cutting patterns: group by (stock_length, sorted cut parts) ---
    pool = res["pool"]
    kerf = group.blade_margin
    cut_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for bar in pool.bars.values():
        if not bar.cuts:
            continue
        cut_agg[(bar.stock_length, tuple(sorted(bar.cuts)))] += 1

    cutting_patterns: list[dict[str, Any]] = []
    for idx, ((stock_length, parts), qty) in enumerate(sorted(cut_agg.items())):
        cuts = len(parts)
        kerf_loss = kerf * max(0, cuts - 1)
        used = sum(parts) + kerf_loss
        remainder = stock_length - used
        cut_positions: list[Any] = []
        cursor = 0
        for i, part in enumerate(parts):
            cursor += part
            if i < cuts - 1 or remainder > 0:
                cut_positions.append(from_units(cursor))
            if i < cuts - 1:
                cursor += kerf
        cutting_patterns.append(
            {
                "pattern_id": f"C{idx}",
                "stock_length": from_units(stock_length),
                "parts": [from_units(x) for x in parts],
                "cut_positions": cut_positions,
                "quantity": qty,
                "kerf_per_cut": from_units(kerf),
                "kerf_loss_per_stock": from_units(kerf_loss),
                "remainder_per_stock": from_units(max(0, remainder)),
                "used_length_per_stock": from_units(used),
            }
        )

    # Metric identities MUST match the verifier's:
    #  * welding type = ordered parts tuple only (pool-global, ignores pipe_id)
    #  * cutting type = (stock_length, sorted parts)
    # (see verifier._verify_group welding_types / cutting_types).
    weld_type_ids = {tuple(o["segments"]) for o in res["orders"]}
    cut_type_ids = {
        (bar.stock_length, tuple(sorted(bar.cuts)))
        for bar in pool.bars.values()
        if bar.cuts
    }
    # The verifier defines utilization = demand_length / used_stock_length
    # (total DEMANDED pipe length over consumed stock), NOT produced/used.  For a
    # fully-solved group these coincide; for a partial one they differ, and we
    # must report the verifier's definition to pass the metric-consistency check.
    demand_len = sum(p.length * p.demand for p in group.pipes)
    metrics = {
        "utilization_rate": demand_len / res["used_len"] if res["used_len"] else 0.0,
        "welding_joint_quantity": res["welds"],
        "welding_pattern_type_quantity": len(weld_type_ids),
        "cutting_pattern_type_quantity": len(cut_type_ids),
        "solve_status": f"ROUTE3_{res['status']}",
    }
    # Input normalizations: the verifier expects one record per input value that
    # was NOT already integral (rounding rule per field).  Replicate its logic
    # from the raw payload so the record set matches exactly (empty if all
    # inputs are integers).  pipe_length / stock_length -> CEILING_TO_INTEGER_MM.
    import math

    input_norms: list[dict[str, Any]] = []
    if payload is not None:
        def _add(container: dict, key: str, path: str, rule: str) -> None:
            if key not in container:
                return
            raw = container[key]
            try:
                val = float(raw)
            except (TypeError, ValueError):
                return
            norm = math.floor(val) if rule == "FLOOR_TO_INTEGER_MM" else math.ceil(val)
            if val != norm:
                input_norms.append(
                    {"path": path, "original": raw, "normalized": int(norm), "rule": rule}
                )

        for i, pipe in enumerate(payload.get("Pipe", []) or []):
            if isinstance(pipe, dict):
                _add(pipe, "pipe_length", f"Pipe[{i}].pipe_length", "CEILING_TO_INTEGER_MM")
                for j, iv in enumerate(pipe.get("Unweldable_Area", []) or []):
                    if isinstance(iv, (list, tuple)) and len(iv) >= 2:
                        syn = {"start": iv[0], "end": iv[1]}
                        _add(syn, "start", f"Pipe[{i}].Unweldable_Area[{j}][0]", "FLOOR_TO_INTEGER_MM")
                        _add(syn, "end", f"Pipe[{i}].Unweldable_Area[{j}][1]", "CEILING_TO_INTEGER_MM")
        for i, stock in enumerate(payload.get("Stock", []) or []):
            if isinstance(stock, dict):
                _add(stock, "stock_length", f"Stock[{i}].stock_length", "CEILING_TO_INTEGER_MM")
        input_norms.sort(key=lambda r: (r["path"], r["rule"]))

    group_result = {
        "material": group.material,
        "specifications": group.specifications,
        "metrics": metrics,
        "welding_patterns": welding_patterns,
        "cutting_patterns": cutting_patterns,
        "input_normalizations": input_norms,
    }
    return {"status": res["status"], "groups": [group_result]}


def _load_group(args) -> MaterialGroup:
    if args.sample_file:
        payload = json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
    elif args.sample_id:
        samples = json.loads(
            (REPO_ROOT / "frontend-next" / "public" / "samples.json").read_text(
                encoding="utf-8"
            )
        )
        rec = next(s for s in samples["samples"] if s["id"] == args.sample_id)
        payload = rec["problem"]
    else:
        raise SystemExit("provide --sample-file or --sample-id")
    if args.blade_margin is not None:
        payload = dict(payload)
        payload.setdefault("NestParam", {})
        payload["NestParam"] = {**payload.get("NestParam", {}), "BladeMargin": args.blade_margin}
    problem = parse_problem(payload)
    if not problem.groups:
        raise SystemExit("no group parsed")
    return problem.groups[0]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sample-file")
    p.add_argument("--sample-id")
    p.add_argument("--blade-margin", type=float, default=None)
    p.add_argument("--order", default="auto", choices=["auto", "lpt", "spt", "tight", "loose", "given"])
    p.add_argument("--restarts", type=int, default=0, help="randomised restart count for local search")
    p.add_argument("--seed", type=int, default=12345)
    p.add_argument("--verify", action="store_true", help="convert to solution schema and run app.verifier")
    p.add_argument("--lns", type=int, default=0, metavar="N", help="run N LNS destroy-repair iterations over the route3 initial solution")
    p.add_argument("--destroy-k", type=int, default=8, help="bars destroyed per LNS iteration")
    return p


def _load_payload(args) -> dict[str, Any]:
    if args.sample_file:
        payload = json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
    elif args.sample_id:
        samples = json.loads(
            (REPO_ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
        )
        rec = next(s for s in samples["samples"] if s["id"] == args.sample_id)
        payload = rec["problem"]
    else:
        raise SystemExit("provide --sample-file or --sample-id")
    if args.blade_margin is not None:
        payload = dict(payload)
        payload["NestParam"] = {**payload.get("NestParam", {}), "BladeMargin": args.blade_margin}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    group = _load_group(args)
    print(
        f"Group {group.material}/{group.specifications}: "
        f"{len(group.pipes)} pipe types, demand {sum(p.demand for p in group.pipes)} pipes, "
        f"{sum(s.quantity for s in group.stocks)} bars, "
        f"kerf={group.blade_margin}, min_weld={group.min_weld_distance}, "
        f"min_cut={group.min_cut_length}\n"
    )
    res = solve_route3(group, order=args.order, restarts=args.restarts, seed=args.seed)
    if res.get("attempts"):
        print("  order attempts (made/failed/util):")
        for a in res["attempts"]:
            print(f"    {a['order']:10s} made={a['made']}/{res['demand_total']} failed={a['failed']} util={a['util']}")
        print()
    print(
        f"route3(best={res['order']}): status={res['status']} "
        f"made={res['made']}/{res['demand_total']} failed={res['failed']}\n"
        f"  welds={res['welds']} bars_used={res['n_bars_used']} "
        f"used_len={res['used_len']} produced_len={res['produced_len']}\n"
        f"  util={res['util']} tightness={res['tightness']} elapsed={res['elapsed']}s"
    )
    if args.lns > 0:
        ct0, wt0 = _pattern_type_counts(res)
        print(
            f"\n  --- LNS ({args.lns} iters, destroy_k={args.destroy_k}) ---\n"
            f"    init : failed={res['failed']} cut_types={ct0} weld_types={wt0} "
            f"util={res['util']} welds={res['welds']}"
        )
        t0 = time.monotonic()
        res = lns_improve(
            group, res, iterations=args.lns, destroy_k=args.destroy_k,
            seed=args.seed, verbose=True,
        )
        ct1, wt1 = _pattern_type_counts(res)
        print(
            f"    final: failed={res['failed']} cut_types={ct1} weld_types={wt1} "
            f"util={res['util']} welds={res['welds']} "
            f"(accepted {res['lns_accepted']}/{res['lns_iterations']}, {time.monotonic()-t0:.1f}s)"
        )
    if args.verify:
        from app.verifier import verify_solution

        payload = _load_payload(args)
        # route3 builds with group.blade_margin; the verifier defaults an absent
        # BladeMargin to 10, so pin the payload to the SAME kerf route3 used to
        # avoid a spurious kerf-mismatch (production input always carries it).
        payload = dict(payload)
        payload["BladeMargin"] = int(group.blade_margin)
        solution = route3_to_solution(group, res, payload)
        report = verify_solution(payload, solution)
        m = report["recomputed_metrics"]
        print(
            f"\n  VERIFY passed={report['passed']} issues={report['issue_count']}\n"
            f"    recomputed util={m.get('utilization_rate')} "
            f"joints={m.get('welding_joint_quantity')} "
            f"cut_types={m.get('cutting_pattern_type_quantity')} "
            f"weld_types={m.get('welding_pattern_type_quantity')}"
        )
        if not report["passed"]:
            from collections import Counter

            codes = Counter(i["code"] for i in report["issues"] if i["severity"] == "error")
            for code, n in codes.most_common(10):
                sample = next(i for i in report["issues"] if i["code"] == code)
                print(f"    [{code}] x{n}: {sample['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
