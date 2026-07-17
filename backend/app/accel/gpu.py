"""GPU-accelerated welding-candidate generator (append-only).

Design (see ``docs/research/GPU可插拔预解器设计方案.md`` §四/§十):

* Written once against a NumPy-compatible array module ``xp``.  ``xp`` is CuPy
  when a CUDA device is present, otherwise NumPy -- so the *identical* code path
  runs (and is unit-tested) on a GPU-less host.  Only the array backend differs.
* The value the GPU adds over the CPU baseline is **differentiated** columns the
  CPU heuristic never enumerates.  The CPU pool is built from proportional
  splits, a coarse grid and a top-N one/two-joint carve; on tight-supply groups
  (supply ratio ~1.0) it rarely lands the *stock-filling* cuts that drive trim
  waste to zero.  So the GPU generates two families:

  1. **Stock-filling greedy** (``_stock_fill_patterns``): starting from the pipe
     head, each cut hugs a real bar length (``stock - k*kerf``) so every part
     but the tail consumes (almost) a whole bar, concentrating the remainder in
     one place.  Many start bars × a small nudge window are evaluated in
     parallel; only the best-waste survivors are snapped.
  2. **Equal-split best-waste** (``_propose_segment_counts``): the original
     vectorised equal-split fallback, kept as a cheap complement.

* Phase 2 (CPU, exact): each surviving proposal is snapped onto legal weld
  positions and re-checked with the module-local ``_legal`` mirror of the
  solver's ``_legal_pattern`` -- the GPU never relaxes a process rule; it only
  *proposes* where to cut and the solver re-validates every returned column.

The output is a list of ``(pipe_index, parts)`` integer-mm columns, already
legal, de-duplicated and deterministically ordered.  The provider degrades to
``[]`` on any failure and never raises for environmental reasons.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

from ..domain import MaterialGroup, PipeDemand
from .base import WeldColumn

log = logging.getLogger(__name__)

# Number of distinct-waste survivors kept per pipe from the vectorised phase
# before the exact legality snap.  GAPG uses K=2 per waste value; we keep a few
# more equal-waste variants because the snap step drops some as illegal.
_BEST_WASTE_K = 3

# Nudge window (mm) explored on each side of an ideal stock-filling cut so a
# forbidden zone or min-distance clash does not kill an otherwise good pattern.
_FILL_NUDGE = 200

# Upper bound on stock-filling carves emitted per pipe.  Multi-step chains
# (first_cut × bar-length step) can multiply quickly; this keeps the appended
# column set bounded and deterministic while still surfacing the useful ones.
_FILL_PER_PIPE_CAP = 12


def _import_backend(force_numpy: bool = False):
    """Return ``(xp, is_gpu)``; CuPy if a device is usable, else NumPy.

    Importing CuPy on a machine without a driver can raise, so every failure
    degrades to NumPy.  NumPy is a hard dependency of the project, so this always
    returns a working array module.  ``force_numpy`` pins the NumPy backend even
    on a CUDA host -- used by the ``gpu-cpu`` mode to exercise the identical
    generation logic on the CPU array backend for tests/benchmarks.
    """

    if not force_numpy:
        try:
            import cupy as xp  # type: ignore

            if int(xp.cuda.runtime.getDeviceCount()) > 0:
                return xp, True
        except Exception as exc:  # noqa: BLE001 - environmental, degrade quietly
            log.debug("CuPy backend unavailable, using NumPy: %s", exc)
    import numpy as xp  # type: ignore

    return xp, False


class GpuCandidateProvider:
    """Vectorised welding-column generator with automatic CPU degradation."""

    def __init__(self, force_numpy: bool = False) -> None:
        self._xp, self.is_gpu = _import_backend(force_numpy=force_numpy)
        self.name = "gpu-cupy" if self.is_gpu else "gpu-numpy"

    def augment_weld_candidates(
        self,
        group: MaterialGroup,
        tier: str,
        existing: Sequence[WeldColumn],
        *,
        deadline: float | None = None,
        cap: int = 0,
    ) -> list[WeldColumn]:
        seen = {tuple(col) for col in existing}
        stock_lengths = sorted({stock.length for stock in group.stocks})
        if not stock_lengths:
            return []
        max_stock = stock_lengths[-1]
        out: list[WeldColumn] = []
        for pipe_index, pipe in enumerate(group.pipes):
            if deadline is not None and time.monotonic() >= deadline:
                break
            # Family 1: stock-filling greedy carves (the differentiated columns
            # the CPU pool rarely lands on tight-supply groups).
            try:
                fills = self._stock_fill_patterns(pipe, group, stock_lengths)
            except Exception as exc:  # noqa: BLE001 - one bad pipe must not abort
                log.debug("gpu fill failed for pipe %s: %s", pipe.pipe_id, exc)
                fills = []
            for parts in fills:
                key = (pipe_index, parts)
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)
            # Family 2: vectorised equal-split best-waste (cheap complement).
            try:
                proposals = self._propose_segment_counts(pipe, group, stock_lengths)
            except Exception as exc:  # noqa: BLE001 - one bad pipe must not abort
                log.debug("gpu proposal failed for pipe %s: %s", pipe.pipe_id, exc)
                proposals = []
            for target in proposals:
                parts = self._snap_split(pipe, group, target, max_stock)
                if parts is None:
                    continue
                key = (pipe_index, parts)
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)
        # Deterministic order irrespective of backend/thread scheduling.
        out.sort(key=lambda item: (item[0], len(item[1]), item[1]))
        if cap and len(out) > cap:
            out = out[:cap]
        return out

    def _stock_fill_patterns(
        self,
        pipe: PipeDemand,
        group: MaterialGroup,
        stock_lengths: list[int],
    ) -> list[tuple[int, ...]]:
        """Stock-filling greedy carves that drive trim waste toward zero.

        Each candidate consumes whole bars from the pipe head: every part but the
        tail equals a real bar's usable span (``stock - k*kerf``), snapped onto
        the nearest legal weld point, so the leftover is concentrated in the last
        segment where it can pair up across pipes.  The GPU evaluates all
        (start-bar × nudge) combinations for the first cut in parallel and keeps
        only the best-waste survivors; the full greedy chain is then finished
        exactly on the CPU.  These are the columns the proportional/grid CPU pool
        does not enumerate on tight-supply groups.
        """

        xp = self._xp
        length = pipe.length
        max_stock = stock_lengths[-1]
        if pipe.max_joints < 1 or length <= max_stock:
            return []
        kerf = group.blade_margin
        # Ideal first-cut positions: one usable bar span for each stock length and
        # a couple of kerf multiples, nudged over a small window -- evaluated as a
        # batch so the GPU scores every trim in parallel.
        bases = xp.asarray(
            sorted({max(1, length_ - k * kerf) for length_ in stock_lengths for k in range(3)})
        )
        nudges = xp.arange(-_FILL_NUDGE, _FILL_NUDGE + 1)
        grid = (bases[:, None] + nudges[None, :]).reshape(-1)
        stocks = xp.asarray(stock_lengths)
        # Trim of placing a first part of size ``grid`` on its best-fitting bar.
        seg = xp.maximum(grid, 1)
        fit = stocks[None, :] // seg[:, None]
        waste = stocks[None, :] - fit * seg[:, None]
        infeasible = seg[:, None] > stocks[None, :]
        big = int(stocks.max()) + 1
        waste = xp.where(infeasible, big, waste)
        best_waste = waste.min(axis=1)
        in_range = (grid > 0) & (grid < length) & (best_waste < big)
        grid_h = _to_host(xp, grid)
        waste_h = _to_host(xp, best_waste)
        keep_h = _to_host(xp, in_range)
        scored: list[tuple[int, int]] = []
        for i in range(len(grid_h)):
            if not bool(keep_h[i]):
                continue
            scored.append((int(waste_h[i]), int(grid_h[i])))
        scored.sort()
        # Best-waste(K) over distinct first-cut trims keeps the batch bounded.
        per_waste: dict[int, int] = {}
        first_cuts: list[int] = []
        for waste_v, cut in scored:
            kept = per_waste.get(waste_v, 0)
            if kept >= _BEST_WASTE_K:
                continue
            per_waste[waste_v] = kept + 1
            first_cuts.append(cut)
        out: list[tuple[int, ...]] = []
        seen_local: set[tuple[int, ...]] = set()
        # Try each first-cut with each distinct bar length as the *step* for the
        # remaining chain.  A uniform ``max_stock`` step only lands one packing;
        # varying the step surfaces "big+big+small" style carves that razor-tight
        # groups (supply ratio ~1.00) actually need.
        steps = sorted({max(1, length_ - kerf) for length_ in stock_lengths}, reverse=True)
        for first in first_cuts:
            for step in steps:
                if len(out) >= _FILL_PER_PIPE_CAP:
                    break
                parts = self._greedy_fill(pipe, group, first, step, max_stock)
                if parts is None or parts in seen_local:
                    continue
                seen_local.add(parts)
                out.append(parts)
            if len(out) >= _FILL_PER_PIPE_CAP:
                break
        return out

    def _greedy_fill(
        self,
        pipe: PipeDemand,
        group: MaterialGroup,
        first_cut: int,
        step: int,
        max_stock: int,
    ) -> tuple[int, ...] | None:
        """Finish a stock-filling carve on the CPU: hug a bar span each step.

        Places the first weld near ``first_cut``, then keeps cutting spans of
        ``step`` mm (snapped to a legal weld) until the remainder fits a single
        bar.  ``step`` is one usable bar length, so each interior part consumes a
        whole bar with (near) zero trim.  Returns a legal part tuple or ``None``.
        """

        length = pipe.length
        positions: list[int] = []
        pos = self._snap_forward(pipe, group, first_cut, prev=0)
        if pos is None:
            return None
        positions.append(pos)
        guard = 0
        while length - positions[-1] > max_stock:
            guard += 1
            if guard > pipe.max_joints + 1:
                return None
            ideal = positions[-1] + step
            nxt = self._snap_forward(pipe, group, ideal, prev=positions[-1])
            if nxt is None or nxt <= positions[-1]:
                return None
            positions.append(nxt)
            if len(positions) > pipe.max_joints:
                return None
        parts: list[int] = []
        prev = 0
        for p in positions:
            parts.append(p - prev)
            prev = p
        parts.append(length - prev)
        result = tuple(parts)
        if max(result) > max_stock:
            return None
        if _legal(pipe, result, group):
            return result
        return None

    def _snap_forward(
        self, pipe: PipeDemand, group: MaterialGroup, target: int, prev: int
    ) -> int | None:
        """Nearest legal weld position to ``target`` after ``prev`` (CPU-exact)."""

        window = max(group.min_weld_distance, group.min_cut_length, _FILL_NUDGE)
        low = prev + max(group.min_weld_distance, group.min_cut_length, 1)
        for delta in range(0, window + 1):
            for cand in (target - delta, target + delta):
                if cand < low:
                    continue
                if group.min_cut_length > 0 and pipe.length - cand < group.min_cut_length:
                    continue
                if 0 < cand < pipe.length and pipe.weld_allowed(cand):
                    return cand
        return None

    def _propose_segment_counts(
        self,
        pipe: PipeDemand,
        group: MaterialGroup,
        stock_lengths: list[int],
    ) -> list[int]:
        """Vectorised: score every feasible segment count, return target lengths.

        For segment counts ``n = 1 .. max_joints+1`` the equal-split segment
        length is ``ceil(length / n)``.  Each candidate is scored by the trim
        waste of placing its segments on the best-fitting stock; ties on waste
        keep the top ``_BEST_WASTE_K``.  Returns *target segment lengths* for the
        exact snap phase, deterministically ordered by (waste, segments).
        """

        xp = self._xp
        max_n = max(1, pipe.max_joints + 1)
        counts = xp.arange(1, max_n + 1)
        seg_len = -(-pipe.length // counts)  # ceil division, integer mm
        stocks = xp.asarray(stock_lengths)
        # For each (count, stock) the pieces of seg_len that fit into a stock bar
        # and the resulting per-bar trim; waste is minimised over stocks.
        seg_col = seg_len[:, None]
        fit = stocks[None, :] // xp.maximum(seg_col, 1)
        waste = stocks[None, :] - fit * seg_col
        # Segments longer than the biggest stock can never be produced.
        infeasible = seg_col > stocks[None, :]
        big = int(stocks.max()) + 1
        waste = xp.where(infeasible, big, waste)
        best_waste = waste.min(axis=1)
        valid = best_waste < big
        counts_h = _to_host(xp, counts)
        seg_h = _to_host(xp, seg_len)
        waste_h = _to_host(xp, best_waste)
        valid_h = _to_host(xp, valid)
        scored: list[tuple[int, int, int]] = []
        for i in range(len(counts_h)):
            if not bool(valid_h[i]):
                continue
            scored.append((int(waste_h[i]), int(counts_h[i]), int(seg_h[i])))
        scored.sort()
        # Best-waste(K): keep at most K target lengths per distinct waste value.
        per_waste: dict[int, int] = {}
        targets: list[int] = []
        for waste_v, _count, target in scored:
            kept = per_waste.get(waste_v, 0)
            if kept >= _BEST_WASTE_K:
                continue
            per_waste[waste_v] = kept + 1
            targets.append(target)
        return targets

    def _snap_split(
        self,
        pipe: PipeDemand,
        group: MaterialGroup,
        target_segment: int,
        max_stock: int,
    ) -> tuple[int, ...] | None:
        """Exact, CPU-side legal split toward ``target_segment`` mm.

        Deliberately mirrors ``solver._snap_split`` semantics so the GPU proposes
        only what the solver considers legal; the solver re-validates anyway.
        """

        if target_segment <= 0:
            return None
        n = max(1, round(pipe.length / target_segment))
        n = min(n, pipe.max_joints + 1)
        if n < 1:
            return None
        if n == 1:
            whole = (pipe.length,)
            if pipe.length <= max_stock and _legal(pipe, whole, group):
                return whole
            return None
        ideal = [pipe.length * (i + 1) // n for i in range(n - 1)]
        window = max(group.min_weld_distance, group.min_cut_length, 1000)
        positions: list[int] = []
        for target in ideal:
            chosen: int | None = None
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
        if _legal(pipe, result, group):
            return result
        return None


def _to_host(xp, array):
    """Bring an ``xp`` array to a host (NumPy) array regardless of backend."""

    getter = getattr(array, "get", None)
    if callable(getter):  # CuPy arrays expose .get(); NumPy arrays do not.
        return getter()
    return array


def _legal(pipe: PipeDemand, parts: Sequence[int], group: MaterialGroup) -> bool:
    """Legality check identical in spirit to ``solver._legal_pattern``.

    Kept local so this module has no import cycle with ``solver``; the solver
    still re-checks every returned column with its own canonical function.
    """

    if not parts or any(part <= 0 for part in parts) or sum(parts) != pipe.length:
        return False
    if len(parts) - 1 > pipe.max_joints:
        return False
    if (
        len(parts) >= 2
        and group.min_cut_length > 0
        and any(part < group.min_cut_length for part in parts)
    ):
        return False
    position = 0
    for index, part in enumerate(parts[:-1]):
        position += part
        if not pipe.weld_allowed(position):
            return False
        if index > 0 and part < group.min_weld_distance:
            return False
    return True
