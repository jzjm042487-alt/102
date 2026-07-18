"""Production baseline solver for serpentine-pipe one-dimensional nesting.

The public entry point is :func:`solve_payload`.  When PySCIPOpt is installed,
the module builds a coupled integer model whose welding-pattern demand and
cutting-pattern production are balanced by exact segment length.  A pure
Python deterministic allocator remains available for degraded deployments and
for recovery when the restricted master has no feasible incumbent.

Objectives are lexicographic: used stock length, added welding joints, distinct
ordered welding signatures, distinct cutting signatures, then reuse and a
stable content-ID tie-break.  Reversing a part tuple therefore creates a
different welding pattern, as required by production.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import os
import time
from typing import Any, Iterable, Sequence

from .domain import (
    LENGTH_SCALE,
    MaterialGroup,
    NestingProblem,
    PipeDemand,
    from_units,
    parse_problem,
)
from .accel import select_provider
from . import route2_equiv


def _route2_enabled() -> bool:
    """Whether the equivalent-stock CSP engine may run alongside the MILP.

    Off by default; enabled by NESTING_ROUTE2 in {1,on,true,yes} (case-
    insensitive).  Keeping it opt-in means the production path is unchanged
    unless explicitly switched on.
    """
    return os.environ.get("NESTING_ROUTE2", "").strip().lower() in {
        "1", "on", "true", "yes",
    }


# --- Tiered candidate generation (graded relaxation) --------------------------
# The MILP restricted master is solved against a *tier* of welding candidates
# that widens from few standard segments to the full pool.  Starting narrow
# keeps the segment-conservation model small (fast, few patterns) and only
# expands when a tier cannot reach the group's target.  T3 == the historical
# full pool, so the strategy is never worse than the previous behaviour.
#
# Per-pipe standard-candidate caps are calibrated from the CSP-S / PMP
# literature (Mobasher & Ekici 2013 use a max of 4 selected patterns; follow-up
# work relaxes to 5); see docs/段长收敛方案-分级放松.md section 8.
TIER_STANDARD_ONLY = "standard"  # T1: baseline + few standard segments
TIER_STANDARD_WIDE = "standard_wide"  # T2: baseline + more standard + snap splits
TIER_FULL = "full"  # T3: historical full candidate pool
TIER_ORDER = (TIER_STANDARD_ONLY, TIER_STANDARD_WIDE, TIER_FULL)

T1_PER_PIPE_STANDARD_CAP = 4
T2_PER_PIPE_STANDARD_CAP = 6

# Composite (multi-part) cut columns per stock length.  This is the true scale
# driver of the MILP (each is an integer variable tied into the segment-balance
# constraints), so low tiers keep it small and the full tier restores history.
# Single-part columns are ALWAYS kept regardless of tier, which guarantees the
# segment-conservation constraints are satisfiable (the paper's greedy-coverage
# feasibility guarantee, doi:10.3390/a15110394).
T1_CUT_COMPOSITE_CAP = 60
T2_CUT_COMPOSITE_CAP = 150
FULL_CUT_COMPOSITE_CAP = 900

# A hard ceiling on the TOTAL cut columns across all stocks.  ``per_stock`` caps
# alone do not bound model size when a group carries many distinct stock lengths
# (25 stocks * 900 -> ~32k integer vars), and pyscipopt model construction for
# that many columns costs tens of seconds -- time that SCIP's own ``limits/time``
# never covers.  Singles are always retained first (feasibility), then the
# best-ranked composites fill the remaining budget.
TIER_TOTAL_CUT_CAP = {
    TIER_STANDARD_ONLY: 1_500,
    TIER_STANDARD_WIDE: 3_000,
    TIER_FULL: 6_000,
}

TIER_CUT_COMPOSITE_CAP = {
    TIER_STANDARD_ONLY: T1_CUT_COMPOSITE_CAP,
    TIER_STANDARD_WIDE: T2_CUT_COMPOSITE_CAP,
    TIER_FULL: FULL_CUT_COMPOSITE_CAP,
}

# Fraction of the group's remaining time budget granted to each non-final tier;
# whatever is left rolls into the full-pool fallback so it can always finish.
TIER_BUDGET_FRACTION = {
    TIER_STANDARD_ONLY: 0.4,
    TIER_STANDARD_WIDE: 0.3,
}

# Upper bound on the number of *extra* welding columns an acceleration provider
# (e.g. GPU) may append per tier.  The pool is append-only, so this only caps how
# much the accelerator widens it; the CPU baseline is always retained beneath it.
TIER_ACCEL_WELD_CAP = {
    TIER_STANDARD_ONLY: 200,
    TIER_STANDARD_WIDE: 600,
    TIER_FULL: 1_500,
}

# Wall-clock budget (seconds) for the acceleration provider's candidate
# generation.  Generation is a *pre-solve* step, so it must never eat into (let
# alone dwarf) the solve time limit -- a provider that runs long simply yields
# fewer extra columns.  Overridable via ``NESTING_ACCEL_BUDGET_S``.
def _accel_budget_seconds() -> float:
    import os

    try:
        value = float(os.getenv("NESTING_ACCEL_BUDGET_S", "3.0"))
    except (TypeError, ValueError):
        return 3.0
    return value if value > 0 else 3.0


@dataclass(frozen=True, slots=True)
class _WeldCandidate:
    pipe_index: int
    parts: tuple[int, ...]

    @property
    def joints(self) -> int:
        return len(self.parts) - 1


@dataclass(frozen=True, slots=True)
class _CutCandidate:
    stock_length: int
    parts: tuple[int, ...]  # canonical/sorted; cut order has no process meaning.
    kerf: int
    kerf_mode: str = "BETWEEN_PARTS"

    @property
    def _trailing_kerf(self) -> int:
        # In WITH_REMAINDER mode a bar that is not cut flush to its end pays for
        # one extra blade pass separating the final part from the leftover stub.
        if self.kerf_mode != "WITH_REMAINDER" or self.kerf <= 0:
            return 0
        between = self.stock_length - sum(self.parts) - self.kerf * max(
            0, len(self.parts) - 1
        )
        return self.kerf if between > 0 else 0

    @property
    def kerf_loss(self) -> int:
        return self.kerf * max(0, len(self.parts) - 1) + self._trailing_kerf

    @property
    def used(self) -> int:
        return sum(self.parts) + self.kerf_loss

    @property
    def remainder(self) -> int:
        return self.stock_length - self.used


@dataclass(slots=True)
class _Bar:
    stock_length: int
    parts: list[int]
    kerf_mode: str = "BETWEEN_PARTS"

    @property
    def occupied_without_kerf(self) -> int:
        return sum(self.parts)

    def remaining(self, kerf: int) -> int:
        return self.stock_length - self.occupied_without_kerf - kerf * max(0, len(self.parts) - 1)

    def next_capacity(self, kerf: int) -> int:
        remainder = self.remaining(kerf)
        if self.parts:
            remainder = max(0, remainder - kerf)
        # Reserve a trailing kerf so a WITH_REMAINDER bar can always separate the
        # part being placed from any leftover stub without overrunning the stock.
        if kerf > 0 and self.kerf_mode == "WITH_REMAINDER":
            remainder = max(0, remainder - kerf)
        return remainder

    def append(self, part: int, kerf: int) -> None:
        if part <= 0 or part > self.next_capacity(kerf):
            raise RuntimeError("internal allocator attempted an over-capacity cut")
        self.parts.append(part)


@dataclass(frozen=True, slots=True)
class _Source:
    kind: str  # "open" or "new"
    token: int  # open-bar index or stock length
    capacity: int
    new_stock_length: int

    @property
    def stable_key(self) -> tuple[int, int, int]:
        return (0 if self.kind == "open" else 1, self.token, self.capacity)


def _legal_pattern(
    pipe: PipeDemand,
    parts: Sequence[int],
    min_distance: int,
    min_cut_length: int = 0,
) -> bool:
    if not parts or any(part <= 0 for part in parts) or sum(parts) != pipe.length:
        return False
    if len(parts) - 1 > pipe.max_joints:
        return False
    # A single unwelded whole pipe is not a spliced segment, so the minimum
    # spliceable-segment rule only applies once the pipe is actually cut into
    # two or more welded parts.  Every such part, including the two end parts,
    # must be at least the shortest weldable segment the shop can handle.
    if len(parts) >= 2 and min_cut_length > 0 and any(
        part < min_cut_length for part in parts
    ):
        return False
    position = 0
    for index, part in enumerate(parts[:-1]):
        position += part
        if not pipe.weld_allowed(position):
            return False
        # Only distances between two internal welds are constrained.  The first
        # and last parts terminate at a design endpoint, not another added weld.
        if index > 0 and part < min_distance:
            return False
    return True


def _largest_allowed(pipe: PipeDemand, low: int, high: int) -> int | None:
    low = max(1, low)
    high = min(pipe.length - 1, high)
    if low > high:
        return None
    candidate = high
    for interval in reversed(pipe.forbidden):
        if interval.start > candidate:
            continue
        if interval.end < low:
            break
        if interval.contains(candidate):
            candidate = interval.start - 1
    return candidate if candidate >= low and pipe.weld_allowed(candidate) else None


def _positions_between(
    pipe: PipeDemand, low: int, high: int, preferred: Iterable[int] = ()
) -> tuple[int, ...]:
    low = max(1, low)
    high = min(pipe.length - 1, high)
    if low > high:
        return ()
    raw = {low, high, (low + high) // 2}
    raw.update(max(low, min(high, item)) for item in preferred)
    for interval in pipe.forbidden:
        raw.add(interval.start - 1)
        raw.add(interval.end + 1)
    legal = sorted({item for item in raw if low <= item <= high and pipe.weld_allowed(item)})
    return tuple(legal)


def _segment_fit(part: int, stock_lengths: Sequence[int]) -> int:
    fitting = [length - part for length in stock_lengths if length >= part]
    return min(fitting) if fitting else 10**18


def _candidate_positions(pipe: PipeDemand, group: MaterialGroup) -> list[int]:
    raw: set[int] = set()
    length = pipe.length
    # Exact stock lengths and their complements are the most valuable welds.
    for stock in group.stocks:
        for kerf_count in range(4):
            adjusted = stock.length - kerf_count * group.blade_margin
            raw.add(adjusted)
            raw.add(length - adjusted)
    for denominator in (2, 3, 4, 5, 8, 12, 16, 24, 32):
        for numerator in range(1, denominator):
            raw.add(round(length * numerator / denominator))
    for interval in pipe.forbidden:
        raw.update((interval.start - 1, interval.end + 1))
    # A modest deterministic grid lets the restricted master escape stock-only
    # coordinates without enumerating every 0.1 mm point.
    grid = max(100 * LENGTH_SCALE, length // 32)
    raw.update(range(grid, length, grid))
    return sorted(position for position in raw if pipe.weld_allowed(position))


def _required_path(pipe: PipeDemand, group: MaterialGroup, max_stock: int) -> tuple[int, ...] | None:
    parts: list[int] = []
    position = 0
    safety = 0
    while pipe.length - position > max_stock:
        safety += 1
        if safety > min(pipe.max_joints, 10_000):
            return None
        low = position + (group.min_weld_distance if position else 1)
        high = position + max_stock
        if group.min_cut_length > 0:
            low = max(low, position + group.min_cut_length)
            high = min(high, pipe.length - group.min_cut_length)
        weld = _largest_allowed(pipe, low, high)
        if weld is None:
            return None
        parts.append(weld - position)
        position = weld
    parts.append(pipe.length - position)
    result = tuple(parts)
    return (
        result
        if _legal_pattern(pipe, result, group.min_weld_distance, group.min_cut_length)
        else None
    )


def _snap_split(
    pipe: PipeDemand, group: MaterialGroup, target_segment: int, max_stock: int
) -> tuple[int, ...] | None:
    """Split ``pipe`` into segments of roughly ``target_segment`` mm, snapping each
    cut onto the nearest legal weld position.

    The number of segments is chosen so the average part is close to the target,
    then cuts are placed at ideal equal-split coordinates and nudged onto a legal,
    minimum-distance-respecting weld point.  Returns a legal part tuple or
    ``None`` when no such split exists (e.g. the target is larger than any stock).
    """

    if target_segment <= 0:
        return None
    n = max(1, round(pipe.length / target_segment))
    n = min(n, pipe.max_joints + 1)
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
    if _legal_pattern(pipe, result, group.min_weld_distance, group.min_cut_length):
        return result
    return None


def _standard_segment_lengths(group: MaterialGroup) -> list[int]:
    """Pick a small set of group-level *standard* segment lengths.

    Reusing a handful of lengths across every pipe is what the literature calls
    pattern reduction: shared segments pair up cleanly, share stock bars, and
    collapse the number of distinct cut/weld patterns.  Candidates are drawn from
    the two most valuable sources:

    * **Stock-driven**: each stock length and its kerf-adjusted self (a bar cut
      into equal pieces) — these pack a bar with (near) zero remainder.
    * **Demand-driven**: near-equal splits of the actual pipe lengths, so a
      standard length is one that pipes genuinely decompose into.

    The set is deliberately small and deterministic; it augments, never replaces,
    the baseline columns so a feasible baseline is never carved away.
    """

    max_stock = max(stock.length for stock in group.stocks)
    lengths: set[int] = set()
    for stock in group.stocks:
        lengths.add(stock.length)
        for pieces in (2, 3, 4):
            piece = (stock.length - group.blade_margin * (pieces - 1)) // pieces
            if piece > 0:
                lengths.add(piece)
    for pipe in group.pipes:
        for pieces in range(1, pipe.max_joints + 2):
            piece = pipe.length // pieces
            if 0 < piece <= max_stock:
                lengths.add(piece)
    return sorted(length for length in lengths if length > 0)


def _standard_weld_candidates(
    group: MaterialGroup, existing: set[tuple[int, tuple[int, ...]]], per_pipe_cap: int = 6
) -> list[_WeldCandidate]:
    """Extra weld columns that split each pipe into group-level standard segments.

    Each pipe is split toward every standard length; the resulting legal splits
    are ranked so the ones reusing the fewest distinct segment lengths (best for
    pattern reduction) come first, then capped per pipe to keep the master
    problem bounded.  Columns already present in ``existing`` are skipped.
    """

    max_stock = max(stock.length for stock in group.stocks)
    standards = _standard_segment_lengths(group)
    additions: list[_WeldCandidate] = []
    for pipe_index, pipe in enumerate(group.pipes):
        found: dict[tuple[int, ...], tuple[int, int]] = {}
        for target in standards:
            parts = _snap_split(pipe, group, target, max_stock)
            if parts is None:
                continue
            key = (pipe_index, parts)
            if key in existing or parts in found:
                continue
            # Rank: fewer distinct segment lengths first (better reuse), then
            # fewer joints, then deterministic tie-break by the parts themselves.
            found[parts] = (len(set(parts)), len(parts))
        ranked = sorted(found.items(), key=lambda item: (item[1], item[0]))
        for parts, _score in ranked[:per_pipe_cap]:
            existing.add((pipe_index, parts))
            additions.append(_WeldCandidate(pipe_index, parts))
    return additions


def _baseline_weld_patterns(
    pipe: PipeDemand, group: MaterialGroup, max_stock: int
) -> set[tuple[int, ...]]:
    """The minimal always-feasible columns for one pipe (Tier T0).

    Every tier contains at least these, so a standard-segment tier can never
    carve away a pipe's only legal decomposition.  It holds the whole pipe (when
    a bar is long enough) and the forced weld path that respects stock length,
    min-cut length and forbidden zones.
    """

    patterns: set[tuple[int, ...]] = set()
    if pipe.length <= max_stock:
        patterns.add((pipe.length,))
    required = _required_path(pipe, group, max_stock)
    if required is not None:
        patterns.add(required)
    return patterns


def _carved_weld_patterns(
    pipe: PipeDemand, group: MaterialGroup, max_stock: int
) -> set[tuple[int, ...]]:
    """Fine-grained one/two-joint carving columns for one pipe (Tier T3 only).

    These maximise utilisation on tightly packed bars but multiply the distinct
    segment lengths, so they are reserved for the full-pool fallback.
    """

    stock_lengths = tuple(stock.length for stock in group.stocks)
    patterns: set[tuple[int, ...]] = set()
    positions = _candidate_positions(pipe, group)
    one_joint: list[tuple[tuple[int, int, int], tuple[int, ...]]] = []
    if pipe.max_joints >= 1:
        for position in positions:
            parts = (position, pipe.length - position)
            if max(parts) > max_stock or not _legal_pattern(
                pipe, parts, group.min_weld_distance, group.min_cut_length
            ):
                continue
            score = (
                _segment_fit(parts[0], stock_lengths)
                + _segment_fit(parts[1], stock_lengths),
                max(_segment_fit(part, stock_lengths) for part in parts),
                position,
            )
            one_joint.append((score, parts))
        for _, parts in sorted(one_joint)[:24]:
            patterns.add(parts)

    # A small high-quality two-joint pool materially improves utilisation
    # while keeping the restricted master bounded and deterministic.
    if pipe.max_joints >= 2:
        key_positions = [item[1][0] for item in sorted(one_joint)[:16]]
        key_positions.extend(positions[:8])
        key_positions = sorted(set(key_positions))
        two_joint: list[tuple[tuple[int, int, int, tuple[int, ...]], tuple[int, ...]]] = []
        for first in key_positions:
            for second in key_positions:
                if second <= first or second - first < group.min_weld_distance:
                    continue
                parts = (first, second - first, pipe.length - second)
                if max(parts) > max_stock or not _legal_pattern(
                    pipe, parts, group.min_weld_distance, group.min_cut_length
                ):
                    continue
                wastes = tuple(_segment_fit(part, stock_lengths) for part in parts)
                two_joint.append(((sum(wastes), max(wastes), second, parts), parts))
        for _, parts in sorted(two_joint)[:12]:
            patterns.add(parts)
    return patterns


def _tiered_weld_candidates(group: MaterialGroup, tier: str) -> list[_WeldCandidate]:
    """Welding columns for one ``tier`` of the graded-relaxation ladder.

    * ``TIER_STANDARD_ONLY`` (T1): baseline + a few group-level standard
      segments per pipe (cap ``T1_PER_PIPE_STANDARD_CAP``) — small, few
      patterns, fast.
    * ``TIER_STANDARD_WIDE`` (T2): baseline + more standard segments (cap
      ``T2_PER_PIPE_STANDARD_CAP``).
    * ``TIER_FULL`` (T3): baseline + full one/two-joint carving + all standard
      segments — identical to the historical pool.

    Every tier is a superset of the baseline (T0), and the tiers are monotone
    (``T1 ⊆ T2 ⊆ T3``), so escalation can only widen the feasible region.
    """

    max_stock = max(stock.length for stock in group.stocks)
    result: list[_WeldCandidate] = []
    for pipe_index, pipe in enumerate(group.pipes):
        patterns = _baseline_weld_patterns(pipe, group, max_stock)
        if tier == TIER_FULL:
            patterns |= _carved_weld_patterns(pipe, group, max_stock)
        if not patterns:
            raise ValueError(
                f"no legal welding pattern can be generated for pipe {pipe.pipe_id}"
            )
        result.extend(
            _WeldCandidate(pipe_index, parts)
            for parts in sorted(patterns, key=lambda item: (len(item), item))
        )
    # Group-level standard-segment columns (pattern reduction).  The per-pipe cap
    # widens with the tier; the full tier lifts the cap entirely to match history.
    if tier == TIER_STANDARD_ONLY:
        per_pipe_cap = T1_PER_PIPE_STANDARD_CAP
    elif tier == TIER_STANDARD_WIDE:
        per_pipe_cap = T2_PER_PIPE_STANDARD_CAP
    else:
        per_pipe_cap = 6  # historical default in _standard_weld_candidates
    seen = {(candidate.pipe_index, candidate.parts) for candidate in result}
    result.extend(_standard_weld_candidates(group, seen, per_pipe_cap=per_pipe_cap))
    result.extend(_accel_weld_candidates(group, tier, result))
    return result


def _accel_weld_candidates(
    group: MaterialGroup, tier: str, existing: list[_WeldCandidate]
) -> list[_WeldCandidate]:
    """Append-only welding columns from the acceleration provider (e.g. GPU).

    The provider yields ``(pipe_index, parts)`` tuples; the solver re-validates
    each one (defense-in-depth -- a provider must produce legal columns but the
    solver never trusts that), drops duplicates, and orders the result
    deterministically so an identical input always feeds SCIP an identical pool.
    With the default CPU no-op provider this returns ``[]`` and the pool is
    exactly the historical baseline.
    """

    provider = select_provider()
    if provider.name == "cpu-noop":
        return []
    seen = {(candidate.pipe_index, candidate.parts) for candidate in existing}
    max_stock = max(stock.length for stock in group.stocks)
    import time as _time

    deadline = _time.monotonic() + _accel_budget_seconds()
    try:
        columns = provider.augment_weld_candidates(
            group,
            tier,
            tuple(seen),
            deadline=deadline,
            cap=TIER_ACCEL_WELD_CAP.get(tier, 0),
        )
    except Exception as exc:  # noqa: BLE001 - accelerator must never break a solve
        _log_accel_failure(provider.name, exc)
        return []
    validated: list[_WeldCandidate] = []
    for column in columns:
        try:
            pipe_index, parts = column
            pipe_index = int(pipe_index)
            parts = tuple(int(part) for part in parts)
        except (TypeError, ValueError):
            continue
        if not (0 <= pipe_index < len(group.pipes)):
            continue
        key = (pipe_index, parts)
        if key in seen:
            continue
        pipe = group.pipes[pipe_index]
        if not parts or max(parts) > max_stock:
            continue
        if not _legal_pattern(
            pipe, parts, group.min_weld_distance, group.min_cut_length
        ):
            continue
        seen.add(key)
        validated.append(_WeldCandidate(pipe_index, parts))
    validated.sort(key=lambda item: (item.pipe_index, len(item.parts), item.parts))
    return validated


def _log_accel_failure(name: str, exc: Exception) -> None:
    import logging

    logging.getLogger(__name__).warning(
        "acceleration provider %s failed, using baseline pool: %s", name, exc
    )


def _generate_weld_candidates(group: MaterialGroup) -> list[_WeldCandidate]:
    """Full historical welding-candidate pool (Tier T3).

    Kept as a thin wrapper so callers that want the widest pool (and existing
    tests/tools) keep working unchanged.
    """

    return _tiered_weld_candidates(group, TIER_FULL)


def _generate_cut_candidates(
    group: MaterialGroup,
    weld_candidates: Sequence[_WeldCandidate],
    per_stock_composite_cap: int = FULL_CUT_COMPOSITE_CAP,
    deadline: float | None = None,
    total_cut_cap: int | None = None,
) -> list[_CutCandidate]:
    part_lengths = sorted({part for pattern in weld_candidates for part in pattern.parts})
    single_cuts: list[_CutCandidate] = []
    # (rank_key, candidate) so the global cap can keep the tightest-fitting
    # composites across every stock, not just the first stocks enumerated.
    composite_cuts: list[tuple[tuple[int, ...], _CutCandidate]] = []
    for stock in group.stocks:
        # A hard deadline guards against pathological groups where the sheer
        # number of stocks makes composite enumeration itself the bottleneck.
        # Singles emitted so far still guarantee a feasible restricted master.
        if deadline is not None and time.monotonic() >= deadline:
            break
        fitting = [part for part in part_lengths if part <= stock.length]
        singles = {(part,) for part in fitting}
        composites: set[tuple[int, ...]] = set()
        # Complement-directed pairs.
        for first in fitting:
            capacity = stock.length - first - group.blade_margin
            index = bisect_right(fitting, capacity) - 1
            for offset in (0, -1, -2):
                at = index + offset
                if at >= 0:
                    pair = tuple(sorted((first, fitting[at])))
                    if sum(pair) + group.blade_margin <= stock.length:
                        composites.add(pair)
            count = max(1, (stock.length + group.blade_margin) // (first + group.blade_margin))
            if count >= 2:
                repeated = (first,) * min(count, 1000)
                if sum(repeated) + group.blade_margin * (len(repeated) - 1) <= stock.length:
                    composites.add(repeated)

        # Seeded best-fill patterns cover combinations of three or more pieces.
        for seed in fitting:
            parts = [seed]
            used = seed
            while len(parts) < 64:
                capacity = stock.length - used - group.blade_margin
                index = bisect_right(fitting, capacity) - 1
                if index < 0:
                    break
                part = fitting[index]
                parts.append(part)
                used += group.blade_margin + part
            if len(parts) >= 2:
                composites.add(tuple(sorted(parts)))

        ranked = sorted(
            composites,
            key=lambda parts: (
                stock.length
                - sum(parts)
                - group.blade_margin * (len(parts) - 1),
                len(parts),
                parts,
            ),
        )[:per_stock_composite_cap]
        for parts in sorted(singles):
            candidate = _CutCandidate(
                stock.length, parts, group.blade_margin, group.kerf_mode
            )
            if candidate.remainder >= 0:
                single_cuts.append(candidate)
        for parts in ranked:
            candidate = _CutCandidate(
                stock.length, parts, group.blade_margin, group.kerf_mode
            )
            if candidate.remainder >= 0:
                waste = (
                    stock.length - sum(parts) - group.blade_margin * (len(parts) - 1)
                )
                composite_cuts.append(((waste, len(parts), parts), candidate))

    # Singles are always kept (feasibility of the segment-conservation rows).  A
    # global ceiling then bounds total model size: with many distinct stocks the
    # per-stock cap alone lets columns explode into the tens of thousands, and
    # pyscipopt model construction for that many vars costs tens of seconds that
    # SCIP's own time limit never covers.
    if total_cut_cap is not None:
        room = max(0, total_cut_cap - len(single_cuts))
        composite_cuts.sort(key=lambda item: item[0])
        kept_composites = [candidate for _, candidate in composite_cuts[:room]]
    else:
        kept_composites = [candidate for _, candidate in composite_cuts]
    # Stable de-duplication (multiple generation routes can produce one column).
    return list(dict.fromkeys(single_cuts + kept_composites))


def _source_options(
    bars: Sequence[_Bar], remaining_stock: dict[int, int], kerf: int
) -> list[_Source]:
    open_sources = [
        # A partially cut bar is already charged to used stock; an empty
        # preselected bar is not.  This distinction makes reuse of remnants
        # outrank opening another full bar in the lexicographic objective.
        _Source(
            "open",
            index,
            bar.next_capacity(kerf),
            0 if bar.parts else bar.stock_length,
        )
        for index, bar in enumerate(bars)
        if bar.next_capacity(kerf) > 0
    ]
    # Bound pair-search time but retain both small remnants and large capacities.
    by_capacity = sorted(open_sources, key=lambda source: (source.capacity, source.token))
    if len(by_capacity) > 32:
        by_capacity = by_capacity[:16] + by_capacity[-16:]
    new_sources = [
        _Source("new", length, length, length)
        for length, quantity in sorted(remaining_stock.items())
        if quantity > 0
    ]
    return by_capacity + new_sources


def _source_pair_available(
    first: _Source, second: _Source, remaining_stock: dict[int, int]
) -> bool:
    if first.kind == second.kind == "open":
        return first.token != second.token
    if first.kind == second.kind == "new" and first.token == second.token:
        return remaining_stock.get(first.token, 0) >= 2
    return True


def _plan_new_length(plan: Sequence[tuple[_Source, int]]) -> int:
    return sum(source.new_stock_length for source, _ in plan)


def _plan_score(
    plan: Sequence[tuple[_Source, int]], known_patterns: set[tuple[int, ...]]
) -> tuple[Any, ...]:
    parts = tuple(part for _, part in plan)
    return (
        _plan_new_length(plan),
        len(parts) - 1,
        0 if parts in known_patterns else 1,
        sum(source.capacity - part for source, part in plan),
        tuple(source.stable_key for source, _ in plan),
        parts,
    )


def _best_plan(
    pipe: PipeDemand,
    group: MaterialGroup,
    bars: Sequence[_Bar],
    remaining_stock: dict[int, int],
    known_patterns: set[tuple[int, ...]],
) -> list[tuple[_Source, int]] | None:
    sources = _source_options(bars, remaining_stock, group.blade_margin)
    candidates: list[tuple[tuple[Any, ...], list[tuple[_Source, int]]]] = []
    for source in sources:
        if source.capacity >= pipe.length:
            parts = (pipe.length,)
            score = (
                source.new_stock_length,
                0,
                0 if parts in known_patterns else 1,
                source.capacity - pipe.length,
                source.stable_key,
            )
            candidates.append((score, [(source, pipe.length)]))

    if pipe.max_joints >= 1:
        for first in sources:
            for second in sources:
                if not _source_pair_available(first, second, remaining_stock):
                    continue
                low = max(1, pipe.length - second.capacity)
                high = min(pipe.length - 1, first.capacity)
                if group.min_cut_length > 0:
                    # Both the welded part and its complement must stay above
                    # the shortest spliceable segment the shop can weld.
                    low = max(low, group.min_cut_length)
                    high = min(high, pipe.length - group.min_cut_length)
                weld_candidates: set[int] = set()
                largest = _largest_allowed(pipe, low, high)
                if largest is not None:
                    weld_candidates.add(largest)
                # Reusing an already active ordered splice is the fifth-level
                # preference and avoids enumerating every forbidden boundary.
                weld_candidates.update(
                    parts[0]
                    for parts in known_patterns
                    if len(parts) == 2
                    and low <= parts[0] <= high
                    and pipe.weld_allowed(parts[0])
                )
                for weld in sorted(weld_candidates):
                    parts = (weld, pipe.length - weld)
                    if not _legal_pattern(
                        pipe, parts, group.min_weld_distance, group.min_cut_length
                    ):
                        continue
                    score = (
                        first.new_stock_length + second.new_stock_length,
                        1,
                        0 if parts in known_patterns else 1,
                        (first.capacity - parts[0]) + (second.capacity - parts[1]),
                        first.stable_key,
                        second.stable_key,
                        parts,
                    )
                    candidates.append((score, [(first, parts[0]), (second, parts[1])]))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _generic_plan(
    pipe: PipeDemand,
    group: MaterialGroup,
    bars: Sequence[_Bar],
    remaining_stock: dict[int, int],
) -> list[tuple[_Source, int]] | None:
    sources = _source_options(bars, remaining_stock, group.blade_margin)
    available_new = dict(remaining_stock)
    used_open: set[int] = set()
    plan: list[tuple[_Source, int]] = []
    position = 0
    max_parts = min(pipe.max_joints + 1, 256)
    while position < pipe.length and len(plan) < max_parts:
        current_sources = [
            source
            for source in sources
            if (
                (source.kind == "open" and source.token not in used_open)
                or (source.kind == "new" and available_new.get(source.token, 0) > 0)
            )
        ]
        remaining = pipe.length - position
        fitting = [source for source in current_sources if source.capacity >= remaining]
        if fitting:
            source = min(
                fitting,
                key=lambda item: (
                    item.new_stock_length,
                    item.capacity - remaining,
                    item.stable_key,
                ),
            )
            plan.append((source, remaining))
            position = pipe.length
        else:
            choices: list[tuple[tuple[Any, ...], _Source, int]] = []
            for source in current_sources:
                minimum = position + (group.min_weld_distance if position else 1)
                if group.min_cut_length > 0:
                    # Leave room for a final part no shorter than the minimum
                    # spliceable segment, and make this part itself long enough.
                    minimum = max(minimum, position + group.min_cut_length)
                    ceiling = min(
                        position + source.capacity,
                        pipe.length - group.min_cut_length,
                    )
                else:
                    ceiling = position + source.capacity
                weld = _largest_allowed(pipe, minimum, ceiling)
                if weld is None:
                    continue
                part = weld - position
                choices.append(
                    (
                        (
                            source.new_stock_length,
                            source.capacity - part,
                            -part,
                            source.stable_key,
                        ),
                        source,
                        part,
                    )
                )
            if not choices:
                return None
            _, source, part = min(choices, key=lambda item: item[0])
            plan.append((source, part))
            position += part
        if source.kind == "open":
            used_open.add(source.token)
        else:
            available_new[source.token] -= 1
    parts = tuple(part for _, part in plan)
    return plan if position == pipe.length and _legal_pattern(
        pipe, parts, group.min_weld_distance, group.min_cut_length
    ) else None


def _commit_plan(
    plan: Sequence[tuple[_Source, int]],
    bars: list[_Bar],
    remaining_stock: dict[int, int],
    kerf: int,
    kerf_mode: str = "BETWEEN_PARTS",
) -> None:
    for source, part in plan:
        if source.kind == "new":
            if remaining_stock.get(source.token, 0) <= 0:
                raise RuntimeError("internal allocator exhausted a stock length")
            remaining_stock[source.token] -= 1
            bars.append(_Bar(source.token, [], kerf_mode))
            bars[-1].append(part, kerf)
        else:
            bars[source.token].append(part, kerf)


def _pipe_output(
    pipe: PipeDemand, parts: tuple[int, ...], quantity: int, pattern_id: str
) -> dict[str, Any]:
    positions: list[int | float] = []
    position = 0
    for part in parts[:-1]:
        position += part
        positions.append(from_units(position))
    return {
        # Welding type identity is global within a material/specification pool:
        # ordered parts only.  pipe_id remains on the row for traceability.
        "pattern_id": pattern_id,
        "pipe_id": pipe.pipe_id,
        "figure_number": pipe.figure_number,
        "parent_node": pipe.parent_node,
        "jlxh": pipe.jlxh,
        "cube_no": pipe.cube_no,
        "pipe_length": from_units(pipe.length),
        "parts": [from_units(part) for part in parts],
        "weld_positions": positions,
        "quantity": quantity,
        "joint_count": len(parts) - 1,
    }


def _cut_output(
    candidate: _CutCandidate, quantity: int, pattern_id: str
) -> dict[str, Any]:
    parts = tuple(sorted(candidate.parts))
    cut_positions: list[int] = []
    cursor = 0
    for index, part in enumerate(parts):
        cursor += part
        if index < len(parts) - 1 or candidate.remainder > 0:
            cut_positions.append(cursor)
        if index < len(parts) - 1:
            cursor += candidate.kerf
    return {
        "pattern_id": pattern_id,
        "stock_length": from_units(candidate.stock_length),
        "parts": [from_units(part) for part in parts],
        "cut_positions": [from_units(position) for position in cut_positions],
        "quantity": quantity,
        "kerf_per_cut": from_units(candidate.kerf),
        "kerf_loss_per_stock": from_units(candidate.kerf_loss),
        "remainder_per_stock": from_units(candidate.remainder),
        "used_length_per_stock": from_units(candidate.used),
    }


def _minimal_stock_quota(group: MaterialGroup) -> dict[int, int]:
    """Select a minimum-length bounded inventory subset before allocation.

    A Python-integer bitset keeps this exact and fast even for the largest
    supplied production pool.  Binary decomposition preserves bounded counts;
    retained prefix bitsets provide deterministic reconstruction.
    """

    quota = {stock.length: stock.must_use_quantity for stock in group.stocks}
    mandatory_length = sum(
        stock.length * stock.must_use_quantity for stock in group.stocks
    )
    # Three kerfs per demanded pipe cover the baseline's 0--2 joint candidate
    # depth without materially weakening the material-utilisation bound.
    # If inventory is tight the target is clamped and the allocator can expand
    # beyond the quota only when the selected subset proves geometrically poor.
    reserve = 3 * group.blade_margin * sum(pipe.demand for pipe in group.pipes)
    desired = max(0, group.demand_length + reserve - mandatory_length)
    supplies = [
        (stock.length, stock.quantity - stock.must_use_quantity)
        for stock in reversed(group.stocks)
        if stock.quantity > stock.must_use_quantity
    ]
    available = sum(length * quantity for length, quantity in supplies)
    if desired <= 0:
        return quota
    if desired >= available:
        for length, quantity in supplies:
            quota[length] = quota.get(length, 0) + quantity
        return quota

    divisor = 0
    for length, _ in supplies:
        divisor = math.gcd(divisor, length)
    divisor = max(1, divisor)
    target = (desired + divisor - 1) // divisor
    weighted = [(length // divisor, quantity, length) for length, quantity in supplies]
    limit = target + max(weight for weight, _, _ in weighted)

    # item = (scaled weight, original stock length, represented count)
    items: list[tuple[int, int, int]] = []
    for weight, quantity, original_length in weighted:
        chunk = 1
        left = quantity
        while left:
            take = min(chunk, left)
            items.append((weight * take, original_length, take))
            left -= take
            chunk <<= 1
    mask = (1 << (limit + 1)) - 1
    prefixes = [1]
    bits = 1
    for weight, _, _ in items:
        bits = (bits | (bits << weight)) & mask
        prefixes.append(bits)
    selected_sum = next(
        (candidate for candidate in range(target, limit + 1) if (bits >> candidate) & 1),
        None,
    )
    if selected_sum is None:
        for length, quantity in supplies:
            quota[length] = quota.get(length, 0) + quantity
        return quota
    cursor = selected_sum
    for item_index in range(len(items) - 1, -1, -1):
        weight, original_length, represented_count = items[item_index]
        previous = prefixes[item_index]
        if (previous >> cursor) & 1:
            continue
        if cursor >= weight and (previous >> (cursor - weight)) & 1:
            quota[original_length] = quota.get(original_length, 0) + represented_count
            cursor -= weight
    return quota


def _assemble_group_result(
    group: MaterialGroup,
    backend: str,
    solve_status: str,
    weld_counts: Sequence[tuple[_WeldCandidate, int]],
    cut_counts: Sequence[tuple[_CutCandidate, int]],
    elapsed: float,
) -> dict[str, Any]:
    welding_signatures = sorted(
        {candidate.parts for candidate, quantity in weld_counts if quantity > 0}
    )
    welding_pattern_ids = {
        signature: f"WP-{index:03d}"
        for index, signature in enumerate(welding_signatures, start=1)
    }
    cutting_signatures = sorted(
        {
            (candidate.stock_length, tuple(sorted(candidate.parts)))
            for candidate, quantity in cut_counts
            if quantity > 0
        }
    )
    cutting_pattern_ids = {
        signature: f"CP-{index:03d}"
        for index, signature in enumerate(cutting_signatures, start=1)
    }
    welding_patterns = [
        _pipe_output(
            group.pipes[candidate.pipe_index],
            candidate.parts,
            quantity,
            welding_pattern_ids[candidate.parts],
        )
        for candidate, quantity in weld_counts
        if quantity > 0
    ]
    welding_patterns.sort(
        key=lambda row: (tuple(row["parts"]), row["pipe_id"], row["pattern_id"])
    )
    cutting_patterns = [
        _cut_output(
            candidate,
            quantity,
            cutting_pattern_ids[
                (candidate.stock_length, tuple(sorted(candidate.parts)))
            ],
        )
        for candidate, quantity in cut_counts
        if quantity > 0
    ]
    cutting_patterns.sort(
        key=lambda row: (row["stock_length"], tuple(row["parts"]), row["pattern_id"])
    )

    used_by_length: Counter[int] = Counter()
    remnant_counter: Counter[tuple[int, int, bool]] = Counter()
    kerf_loss = 0
    remainder_length = 0
    for candidate, quantity in cut_counts:
        if quantity <= 0:
            continue
        used_by_length[candidate.stock_length] += quantity
        kerf_loss += candidate.kerf_loss * quantity
        remainder_length += candidate.remainder * quantity
        if candidate.remainder > 0:
            reusable = candidate.remainder >= group.min_reusable_remnant
            remnant_counter[(candidate.remainder, candidate.stock_length, reusable)] += quantity

    unused_materials: list[dict[str, Any]] = []
    for stock in group.stocks:
        quantity = stock.quantity - used_by_length[stock.length]
        if quantity > 0:
            unused_materials.append(
                {"stock_length": from_units(stock.length), "quantity": quantity}
            )
    generated_remnants = [
        {
            "length": from_units(length),
            "quantity": quantity,
            "source_stock_length": from_units(source),
            "reusable": reusable,
        }
        for (length, source, reusable), quantity in sorted(remnant_counter.items())
    ]
    used_stock_length = sum(
        candidate.stock_length * quantity for candidate, quantity in cut_counts
    )
    weld_quantity = sum(
        candidate.joints * quantity for candidate, quantity in weld_counts
    )
    welding_type_usage: Counter[tuple[int, ...]] = Counter()
    for candidate, quantity in weld_counts:
        if quantity > 0:
            welding_type_usage[candidate.parts] += quantity
    cutting_type_usage: Counter[tuple[int, tuple[int, ...]]] = Counter()
    for candidate, quantity in cut_counts:
        if quantity > 0:
            cutting_type_usage[
                (candidate.stock_length, tuple(sorted(candidate.parts)))
            ] += quantity
    utilisation = group.demand_length / used_stock_length if used_stock_length else 0.0
    target_reached = utilisation + 1e-12 >= group.target_rate
    must_use_stock_quantity = sum(stock.must_use_quantity for stock in group.stocks)
    must_use_used_quantity = sum(
        min(used_by_length[stock.length], stock.must_use_quantity)
        for stock in group.stocks
    )
    must_use_stock_length = sum(
        stock.length * stock.must_use_quantity for stock in group.stocks
    )
    metrics = {
        "demand_length": from_units(group.demand_length),
        "available_stock_length": from_units(group.stock_length),
        "used_stock_length": from_units(used_stock_length),
        "utilization_rate": utilisation,
        "target_utilization_rate": group.target_rate,
        "target_reached": target_reached,
        "welding_joint_quantity": weld_quantity,
        "welding_pattern_type_quantity": len(welding_type_usage),
        "cutting_pattern_type_quantity": len(cutting_type_usage),
        "reused_welding_pattern_type_quantity": sum(
            quantity >= 2 for quantity in welding_type_usage.values()
        ),
        "reused_cutting_pattern_type_quantity": sum(
            quantity >= 2 for quantity in cutting_type_usage.values()
        ),
        "kerf_loss": from_units(kerf_loss),
        "remainder_length": from_units(remainder_length),
        "must_use_stock_quantity": must_use_stock_quantity,
        "must_use_used_quantity": must_use_used_quantity,
        "must_use_stock_length": from_units(must_use_stock_length),
        "solver_backend": backend,
        "solve_status": solve_status,
        "elapsed_seconds": round(elapsed, 6),
    }
    return {
        "material": group.material,
        "specifications": group.specifications,
        "length_scale": LENGTH_SCALE,
        "metrics": metrics,
        "welding_patterns": welding_patterns,
        "cutting_patterns": cutting_patterns,
        "unused_materials": unused_materials,
        "generated_remnants": generated_remnants,
        "input_normalizations": [
            normalization.as_dict()
            for normalization in group.input_normalizations
        ],
        "warnings": list(group.warnings),
    }


def _solve_group_fallback_attempt(
    group: MaterialGroup,
    time_limit: float,
    *,
    use_quota: bool,
    round_robin: bool,
    restrictive_first: bool,
    longest_first: bool,
    strategy_name: str,
) -> dict[str, Any]:
    started = time.monotonic()
    remaining_stock = {stock.length: stock.quantity for stock in group.stocks}
    bars: list[_Bar] = []
    # Treat the minimum bounded subset as the material budget.  All selected
    # bars are visible to the allocator from the start, avoiding the myopic
    # "open every stock then discover fragmented remnants" failure mode.
    quota = (
        _minimal_stock_quota(group)
        if use_quota
        else {stock.length: stock.must_use_quantity for stock in group.stocks}
    )
    for stock in group.stocks:
        for _ in range(quota.get(stock.length, 0)):
            bars.append(_Bar(stock.length, [], group.kerf_mode))
            remaining_stock[stock.length] -= 1

    allocations: Counter[tuple[int, tuple[int, ...]]] = Counter()
    known_patterns: set[tuple[int, ...]] = set()
    # Restrictive/long pipe types first; stable identity closes all tie-breaks.
    order = sorted(
        range(len(group.pipes)),
        key=lambda index: (
            (-1 if longest_first else 1) * group.pipes[index].length,
            (-1 if restrictive_first else 1)
            * sum(
                interval.end - interval.start + 1
                for interval in group.pipes[index].forbidden
            ),
            group.pipes[index].pipe_id,
        ),
    )
    # Round-robin prevents one pipe type from consuming all useful stock
    # lengths before another type with different forbidden zones is considered.
    if round_robin:
        jobs = [
            pipe_index
            for repetition in range(max(pipe.demand for pipe in group.pipes))
            for pipe_index in order
            if repetition < group.pipes[pipe_index].demand
        ]
    else:
        jobs = [
            pipe_index
            for pipe_index in order
            for _ in range(group.pipes[pipe_index].demand)
        ]
    for pipe_index in jobs:
        pipe = group.pipes[pipe_index]
        direct_plan = _best_plan(
            pipe,
            group,
            bars,
            remaining_stock,
            known_patterns,
        )
        generic_plan = _generic_plan(pipe, group, bars, remaining_stock)
        feasible_plans = [
            candidate
            for candidate in (direct_plan, generic_plan)
            if candidate is not None
        ]
        plan = (
            min(
                feasible_plans,
                key=lambda candidate: _plan_score(
                    candidate, known_patterns
                ),
            )
            if feasible_plans
            else None
        )
        if plan is None:
            raise ValueError(
                f"finite stock cannot produce pipe {pipe.pipe_id} with legal weld positions"
            )
        parts = tuple(part for _, part in plan)
        _commit_plan(plan, bars, remaining_stock, group.blade_margin, group.kerf_mode)
        allocations[(pipe_index, parts)] += 1
        known_patterns.add(parts)
        if time.monotonic() - started > max(time_limit, 0.001):
            # A feasible result is more valuable than aborting midway.  The
            # deterministic allocator completes the current material pool;
            # this warning is added after assembly.
            pass

    mandatory_by_length = {
        stock.length: stock.must_use_quantity for stock in group.stocks
    }
    must_use_total = sum(mandatory_by_length.values())
    used_mandatory: Counter[int] = Counter()
    used_available = 0
    kept_bars: list[_Bar] = []
    for bar in bars:
        if bar.parts:
            kept_bars.append(bar)
            if used_mandatory[bar.stock_length] < mandatory_by_length[bar.stock_length]:
                used_mandatory[bar.stock_length] += 1
            else:
                used_available += 1
        else:
            remaining_stock[bar.stock_length] += 1
    # Group-level priority: an available bar may be used only after every
    # must-use bar is consumed.  Small demand served entirely from must-use stock
    # (used_available == 0) is fine even if must-use is not fully exhausted.
    if used_available > 0 and sum(used_mandatory.values()) < must_use_total:
        raise ValueError("must_use inventory not exhausted before using available stock")
    bars = kept_bars
    cut_counter: Counter[tuple[int, tuple[int, ...]]] = Counter()
    for bar in bars:
        cut_counter[(bar.stock_length, tuple(sorted(bar.parts)))] += 1
    weld_counts = [
        (_WeldCandidate(pipe_index, parts), quantity)
        for (pipe_index, parts), quantity in sorted(
            allocations.items(), key=lambda item: (item[0][0], item[0][1])
        )
    ]
    cut_counts = [
        (_CutCandidate(stock_length, parts, group.blade_margin, group.kerf_mode), quantity)
        for (stock_length, parts), quantity in sorted(cut_counter.items())
    ]
    elapsed = time.monotonic() - started
    result = _assemble_group_result(
        group, "deterministic-fallback", "FEASIBLE", weld_counts, cut_counts, elapsed
    )
    result["warnings"].append(f"FALLBACK_STRATEGY: {strategy_name}")
    if elapsed > time_limit:
        result["warnings"].append("TIME_LIMIT_REACHED_AFTER_FEASIBLE_COMPLETION")
    result["metrics"]["solve_status"] = (
        "TARGET_REACHED" if result["metrics"]["target_reached"] else "FEASIBLE"
    )
    return result


def _solve_group_fallback(group: MaterialGroup, time_limit: float) -> dict[str, Any]:
    """Run complementary deterministic policies and choose lexicographically."""

    started = time.monotonic()
    if all(stock.must_use_quantity == stock.quantity for stock in group.stocks):
        # Quota/no-quota are identical when every bar is mandatory.
        strategies = (
            (False, False, True, True, "longest-sequential"),
            (False, True, False, True, "longest-round-robin"),
            (False, True, True, False, "shortest-round-robin"),
        )
    else:
        strategies = (
            (False, False, True, True, "sequential"),
            (True, False, True, True, "quota-sequential"),
            (True, True, False, True, "quota-round-robin"),
            (False, True, False, True, "round-robin"),
        )
    feasible: list[dict[str, Any]] = []
    failures: list[str] = []
    for use_quota, round_robin, restrictive_first, longest_first, name in strategies:
        remaining = time_limit - (time.monotonic() - started)
        try:
            feasible.append(
                _solve_group_fallback_attempt(
                    group,
                    max(0.05, remaining),
                    use_quota=use_quota,
                    round_robin=round_robin,
                    restrictive_first=restrictive_first,
                    longest_first=longest_first,
                    strategy_name=name,
                )
            )
        except ValueError as exc:
            failures.append(f"{name}: {exc}")
    if not feasible:
        raise ValueError("; ".join(failures) or "deterministic fallback found no solution")

    def rank(result: dict[str, Any]) -> tuple[Any, ...]:
        metrics = result["metrics"]
        repeat_score = int(metrics["reused_welding_pattern_type_quantity"]) + int(
            metrics["reused_cutting_pattern_type_quantity"]
        )
        stable_signature = tuple(
            (tuple(row["parts"]), row["pipe_id"], row["quantity"])
            for row in result["welding_patterns"]
        ) + tuple(
            (row["stock_length"], tuple(row["parts"]), row["quantity"])
            for row in result["cutting_patterns"]
        )
        return (
            float(metrics["used_stock_length"]),
            int(metrics["welding_joint_quantity"]),
            int(metrics["welding_pattern_type_quantity"]),
            int(metrics["cutting_pattern_type_quantity"]),
            -repeat_score,
            stable_signature,
        )

    best = min(feasible, key=rank)
    if failures:
        best["warnings"].append("FALLBACK_ATTEMPT_FAILURES: " + " | ".join(failures))
    best["warnings"].append("HEURISTIC_NO_GLOBAL_OPTIMALITY_PROOF")
    best["metrics"]["elapsed_seconds"] = round(time.monotonic() - started, 6)
    best["metrics"].update(
        {
            "solve_status": (
                "FEASIBLE_TARGET_REACHED"
                if best["metrics"]["target_reached"]
                else "FEASIBLE"
            ),
            "optimization_phases_completed": [],
            "optimization_phase_incomplete": "used_stock_length_global_proof",
            "core_lexicographic_optimal": False,
            "lexicographic_optimal": False,
        }
    )
    return best


def _tier_meets_target(result: dict[str, Any], group: MaterialGroup) -> bool:
    """Decide whether a tier's solution is good enough to stop escalating.

    Stop when the lexicographic solve was fully optimal, or when the achieved
    utilisation already reaches the group's target rate.  Otherwise the next,
    wider tier gets a chance to improve feasibility/utilisation.
    """

    metrics = result.get("metrics", {})
    if metrics.get("lexicographic_optimal"):
        return True
    achieved = metrics.get("utilization_rate")
    if achieved is None:
        return False
    return achieved * 100.0 >= group.target_rate - 1e-9


def _solve_group_scip(group: MaterialGroup, time_limit: float) -> dict[str, Any]:
    """Solve one material group by graded relaxation over candidate tiers.

    Each tier feeds a widening welding-candidate pool into the restricted MILP.
    The first tier that reaches the group's target (or proves lexicographic
    optimality) wins; otherwise we keep the best feasible result seen and
    escalate.  The full tier equals the historical pool, so the outcome is never
    worse than the previous single-shot behaviour.
    """

    started = time.monotonic()
    deadline = started + max(time_limit, 0.05)
    best_result: dict[str, Any] | None = None
    last_error: Exception | None = None
    for tier in TIER_ORDER:
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            break
        if tier == TIER_FULL:
            budget = remaining  # fallback tier always gets whatever is left
        else:
            budget = max(0.05, remaining * TIER_BUDGET_FRACTION[tier])
        weld_candidates = _tiered_weld_candidates(group, tier)
        try:
            result = _solve_group_scip_with_candidates(
                group,
                weld_candidates,
                budget,
                cut_composite_cap=TIER_CUT_COMPOSITE_CAP[tier],
                total_cut_cap=TIER_TOTAL_CUT_CAP[tier],
                deadline=deadline,
            )
        except ValueError as exc:
            last_error = exc
            continue
        result["metrics"]["candidate_tier"] = tier
        if best_result is None or _better_group_result(result, best_result):
            best_result = result
        if _tier_meets_target(result, group):
            break
    if best_result is None:
        if last_error is not None:
            raise last_error
        raise ValueError("SCIP did not produce a feasible solution")
    return best_result


def _better_group_result(candidate: dict[str, Any], incumbent: dict[str, Any]) -> bool:
    """Prefer the higher-utilisation, then fewer-joint group result.

    Used to keep the best feasible tier result when no tier reaches the target,
    mirroring the lexicographic spirit (material first, then welds).
    """

    cm = candidate.get("metrics", {})
    im = incumbent.get("metrics", {})
    cu = cm.get("utilization_rate") or 0.0
    iu = im.get("utilization_rate") or 0.0
    if abs(cu - iu) > 1e-9:
        return cu > iu
    cj = cm.get("welding_joint_quantity")
    ij = im.get("welding_joint_quantity")
    if cj is None or ij is None:
        return False
    return cj < ij


def _solve_group_scip_with_candidates(
    group: MaterialGroup,
    weld_candidates: Sequence[_WeldCandidate],
    time_limit: float,
    cut_composite_cap: int = FULL_CUT_COMPOSITE_CAP,
    total_cut_cap: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    from pyscipopt import Model, quicksum  # type: ignore[import-not-found]

    started = time.monotonic()
    cut_candidates = _generate_cut_candidates(
        group,
        weld_candidates,
        per_stock_composite_cap=cut_composite_cap,
        deadline=deadline,
        total_cut_cap=total_cut_cap,
    )
    model = Model(f"nest-{group.material}-{group.specifications}")
    model.hideOutput()
    # Candidate generation and model building are not covered by SCIP's own
    # limit, so charge their elapsed time against the budget: the solve gets only
    # the wall-clock left before the group deadline, never the full nominal
    # limit.  This is what keeps a pathological group from blocking for minutes.
    if deadline is not None:
        solve_budget = deadline - time.monotonic()
    else:
        solve_budget = time_limit - (time.monotonic() - started)
    if solve_budget <= 0.05:
        raise ValueError("candidate generation exhausted the time budget")
    model.setRealParam("limits/time", max(0.1, solve_budget))
    y = [
        model.addVar(vtype="INTEGER", lb=0, name=f"w_{index}")
        for index in range(len(weld_candidates))
    ]
    x = [
        model.addVar(vtype="INTEGER", lb=0, name=f"c_{index}")
        for index in range(len(cut_candidates))
    ]
    by_pipe: dict[int, list[int]] = defaultdict(list)
    for index, candidate in enumerate(weld_candidates):
        by_pipe[candidate.pipe_index].append(index)
    for pipe_index, pipe in enumerate(group.pipes):
        indexes = by_pipe[pipe_index]
        model.addCons(quicksum(y[index] for index in indexes) == pipe.demand)

    # A welding type is the ordered parts tuple globally inside this material
    # pool.  Different pipe IDs sharing that tuple activate one type variable.
    weld_by_signature: dict[tuple[int, ...], list[int]] = defaultdict(list)
    for index, candidate in enumerate(weld_candidates):
        weld_by_signature[candidate.parts].append(index)
    weld_active: dict[tuple[int, ...], Any] = {}
    weld_repeated: dict[tuple[int, ...], Any] = {}
    for signature_index, signature in enumerate(sorted(weld_by_signature)):
        indexes = weld_by_signature[signature]
        active = model.addVar(vtype="BINARY", name=f"use_wp_{signature_index}")
        weld_active[signature] = active
        usage = quicksum(y[index] for index in indexes)
        pipe_indexes = {weld_candidates[index].pipe_index for index in indexes}
        maximum = sum(group.pipes[index].demand for index in pipe_indexes)
        model.addCons(usage <= maximum * active)
        model.addCons(usage >= active)
        if maximum >= 2:
            repeated = model.addVar(
                vtype="BINARY", name=f"repeat_wp_{signature_index}"
            )
            weld_repeated[signature] = repeated
            model.addCons(usage >= 2 * repeated)
            model.addCons(usage <= 1 + (maximum - 1) * repeated)

    cuts_by_stock: dict[int, list[int]] = defaultdict(list)
    for index, candidate in enumerate(cut_candidates):
        cuts_by_stock[candidate.stock_length].append(index)
    # Must-use is a group-level priority (see verifier): every must-use bar must
    # be consumed before any available (non-must-use) bar may be used.  Encode it
    # per length with a split n = u + a (u<=must, a<=avail) and a single group
    # binary z_free that gates *all* available usage: if any available bar is used
    # (z_free=1) then the total must-use consumption U must reach MU_total; if
    # z_free=0 no available bar may be used, so each length is capped at its
    # must-use quantity.  Small demand that fits inside must-use stock is served
    # with z_free=0 and never forced to exhaust the must-use inventory.
    must_use_total = sum(stock.must_use_quantity for stock in group.stocks)
    usage_by_length: dict[int, Any] = {}
    for stock in group.stocks:
        expression = quicksum(x[index] for index in cuts_by_stock[stock.length])
        model.addCons(expression <= stock.quantity)
        usage_by_length[stock.length] = expression
    if must_use_total > 0:
        z_free = model.addVar(vtype="BINARY", name="use_available_stock")
        used_must_terms = []
        for stock in group.stocks:
            n_len = usage_by_length[stock.length]
            avail = stock.quantity - stock.must_use_quantity
            u_len = model.addVar(
                vtype="INTEGER", lb=0, ub=stock.must_use_quantity,
                name=f"mu_used_{stock.length}",
            )
            a_len = model.addVar(
                vtype="INTEGER", lb=0, ub=max(0, avail),
                name=f"av_used_{stock.length}",
            )
            model.addCons(u_len + a_len == n_len)
            # Available slots for this length are only openable when z_free is on.
            if avail > 0:
                model.addCons(a_len <= avail * z_free)
            else:
                model.addCons(a_len == 0)
            used_must_terms.append(u_len)
        # Turning on any available usage forces the must-use inventory exhausted.
        model.addCons(quicksum(used_must_terms) >= must_use_total * z_free)

    cut_by_signature: dict[tuple[int, tuple[int, ...]], list[int]] = defaultdict(list)
    for index, candidate in enumerate(cut_candidates):
        signature = (candidate.stock_length, tuple(sorted(candidate.parts)))
        cut_by_signature[signature].append(index)
    stock_quantity = {stock.length: stock.quantity for stock in group.stocks}
    cut_active: dict[tuple[int, tuple[int, ...]], Any] = {}
    cut_repeated: dict[tuple[int, tuple[int, ...]], Any] = {}
    for signature_index, signature in enumerate(sorted(cut_by_signature)):
        indexes = cut_by_signature[signature]
        active = model.addVar(vtype="BINARY", name=f"use_cp_{signature_index}")
        cut_active[signature] = active
        usage = quicksum(x[index] for index in indexes)
        maximum = stock_quantity[signature[0]]
        model.addCons(usage <= maximum * active)
        model.addCons(usage >= active)
        if maximum >= 2:
            repeated = model.addVar(
                vtype="BINARY", name=f"repeat_cp_{signature_index}"
            )
            cut_repeated[signature] = repeated
            model.addCons(usage >= 2 * repeated)
            model.addCons(usage <= 1 + (maximum - 1) * repeated)

    segment_lengths = sorted(
        {part for candidate in weld_candidates for part in candidate.parts}
    )
    for segment in segment_lengths:
        produced = quicksum(
            candidate.parts.count(segment) * x[index]
            for index, candidate in enumerate(cut_candidates)
            if segment in candidate.parts
        )
        consumed = quicksum(
            candidate.parts.count(segment) * y[index]
            for index, candidate in enumerate(weld_candidates)
            if segment in candidate.parts
        )
        model.addCons(produced == consumed)

    used_expr = quicksum(
        candidate.stock_length * x[index]
        for index, candidate in enumerate(cut_candidates)
    )
    weld_expr = quicksum(
        candidate.joints * y[index]
        for index, candidate in enumerate(weld_candidates)
    )
    weld_type_expr = quicksum(weld_active.values())
    cut_type_expr = quicksum(cut_active.values())
    repeat_expr = quicksum(weld_repeated.values()) + quicksum(cut_repeated.values())
    stable_expr = quicksum(
        (rank + 1) * weld_active[signature]
        for rank, signature in enumerate(sorted(weld_active))
    ) + quicksum(
        (rank + 1) * cut_active[signature]
        for rank, signature in enumerate(sorted(cut_active))
    )
    objectives = (
        ("used_stock_length", used_expr, "minimize"),
        ("welding_joint_quantity", weld_expr, "minimize"),
        ("welding_pattern_type_quantity", weld_type_expr, "minimize"),
        ("cutting_pattern_type_quantity", cut_type_expr, "minimize"),
        ("repeated_pattern_preference", repeat_expr, "maximize"),
        ("stable_pattern_tiebreak", stable_expr, "minimize"),
    )
    best_weld_values: list[int] | None = None
    best_cut_values: list[int] | None = None
    completed_phases: list[str] = []
    incomplete_phase: str | None = None
    status = "UNKNOWN"
    for phase, (phase_name, objective, sense) in enumerate(objectives):
        if deadline is not None:
            remaining = deadline - time.monotonic()
        else:
            remaining = time_limit - (time.monotonic() - started)
        if remaining <= 0.05 and best_weld_values is not None:
            incomplete_phase = phase_name
            status = "TIME_LIMIT"
            break
        model.setRealParam("limits/time", max(0.05, remaining))
        model.setObjective(objective, sense)
        model.optimize()
        status = str(model.getStatus()).upper()
        if model.getNSols() <= 0:
            if best_weld_values is None:
                raise ValueError(f"SCIP restricted master returned {status}")
            incomplete_phase = phase_name
            break
        solution = model.getBestSol()
        best_weld_values = [
            int(round(model.getSolVal(solution, variable))) for variable in y
        ]
        best_cut_values = [
            int(round(model.getSolVal(solution, variable))) for variable in x
        ]
        incumbent = int(round(model.getSolVal(solution, objective)))
        if status != "OPTIMAL":
            incomplete_phase = phase_name
            break
        completed_phases.append(phase_name)
        if phase < len(objectives) - 1:
            model.freeTransform()
            if sense == "minimize":
                model.addCons(objective <= incumbent)
            else:
                model.addCons(objective >= incumbent)
    if best_weld_values is None or best_cut_values is None:
        raise ValueError("SCIP did not produce a feasible solution")
    weld_counts = [
        (candidate, best_weld_values[index])
        for index, candidate in enumerate(weld_candidates)
        if best_weld_values[index] > 0
    ]
    cut_counts = [
        (candidate, best_cut_values[index])
        for index, candidate in enumerate(cut_candidates)
        if best_cut_values[index] > 0
    ]
    result = _assemble_group_result(
        group, "SCIP-MILP", status, weld_counts, cut_counts, time.monotonic() - started
    )
    core_phases = [name for name, _, _ in objectives[:4]]
    core_optimal = all(name in completed_phases for name in core_phases)
    fully_optimal = len(completed_phases) == len(objectives)
    result["metrics"].update(
        {
            "optimization_phases_completed": completed_phases,
            "optimization_phase_incomplete": incomplete_phase,
            "core_lexicographic_optimal": core_optimal,
            "lexicographic_optimal": fully_optimal,
        }
    )
    if fully_optimal:
        result["metrics"]["solve_status"] = "OPTIMAL_LEXICOGRAPHIC"
    else:
        phase_label = (incomplete_phase or "unknown").upper()
        result["metrics"]["solve_status"] = f"{status}_INCOMPLETE_{phase_label}"
        result["warnings"].append(
            "LEXICOGRAPHIC_PHASE_INCOMPLETE: "
            f"{incomplete_phase or 'unknown'}; completed={','.join(completed_phases) or 'none'}"
        )
    return result


def _unsolved_group_result(
    group: MaterialGroup, reason: str, elapsed: float
) -> dict[str, Any]:
    """Assemble a zero-valued, summary-safe result for a group that could not be
    nested, carrying a structured material-shortage diagnosis for the workshop.

    Empty pattern lists make every quantitative metric fall through to zero, so
    the payload-level aggregation stays valid while the group is clearly flagged
    as unsolved via ``solve_status`` and ``shortage_diagnosis``.
    """

    result = _assemble_group_result(group, "none", "UNSOLVED", [], [], elapsed)
    try:
        from .shortage import diagnose_group_shortage

        diagnosis = diagnose_group_shortage(group)
    except Exception as exc:  # noqa: BLE001 - diagnosis must never mask the result
        diagnosis = {
            "solvable_by_carving": None,
            "inconclusive": True,
            "message": f"缺料诊断失败：{type(exc).__name__}: {exc}",
            "recommendations": [],
        }
    result["shortage_diagnosis"] = diagnosis
    result["warnings"].append(f"GROUP_UNSOLVED: {reason}")
    return result


def _length_shortfall_result(group: MaterialGroup, elapsed: float) -> dict[str, Any]:
    """Fast, solver-free verdict for a group whose total stock length is provably
    below total demand length.

    When ``sum(stock) < sum(demand)`` no cutting or welding can conjure the
    missing metal, so the group is INFEASIBLE by arithmetic alone.  Skipping both
    the MILP and the shortage oracle avoids wasting the whole time budget on an
    outcome a single subtraction already decides.  The attached diagnosis states
    the exact length gap and the minimum extra stock that closes it.
    """

    result = _assemble_group_result(group, "none", "UNSOLVED", [], [], elapsed)
    deficit = group.demand_length - group.stock_length
    longest = max(group.stocks, key=lambda stock: stock.length)
    extra_bars = -(-deficit // longest.length)  # ceil division
    result["shortage_diagnosis"] = {
        "solvable_by_carving": False,
        "shortage_type": "LENGTH_SHORTFALL",
        "supply_ratio": (
            group.stock_length / group.demand_length if group.demand_length else None
        ),
        "length_deficit": from_units(deficit),
        "recommendations": [
            {
                "stock_length": from_units(longest.length),
                "add_quantity": extra_bars,
                "message": (
                    f"总供料长度比总需求短 {from_units(deficit)}mm，"
                    f"无论如何切分都不可能排出；至少需补 {extra_bars} 根 "
                    f"{from_units(longest.length)}mm 定尺才可能可行（仅长度必要条件，"
                    "根数/段配平仍需再判）。"
                ),
            }
        ],
        "message": (
            f"供料比 {group.stock_length / group.demand_length:.4f} < 1，"
            "总料长不足，物理无解（已跳过求解器直接判定）。"
        ),
    }
    result["warnings"].append(
        f"GROUP_UNSOLVED: LENGTH_SHORTFALL deficit={from_units(deficit)}mm"
    )
    return result


def _solve_problem(
    problem: NestingProblem, time_limit_seconds: float, *, engine: str = "baseline"
) -> list[dict[str, Any]]:
    try:
        import pyscipopt  # noqa: F401

        has_scip = True
    except Exception:
        has_scip = False
    started = time.monotonic()
    groups: list[dict[str, Any]] = []
    for index, group in enumerate(problem.groups):
        elapsed = time.monotonic() - started
        remaining_groups = len(problem.groups) - index
        budget = max(0.1, (time_limit_seconds - elapsed) / max(1, remaining_groups))
        group_started = time.monotonic()
        # Zero-risk arithmetic prefilter: if total stock length is below total
        # demand length the group is provably infeasible, so skip the solver and
        # the shortage oracle entirely.  This never misjudges a solvable group --
        # a supply ratio >= 1 is a *necessary* (not sufficient) condition, so
        # ratio-based rejection is deliberately limited to ratio < 1.
        if group.stock_length < group.demand_length:
            groups.append(
                _length_shortfall_result(group, time.monotonic() - group_started)
            )
            continue
        result: dict[str, Any] | None = None
        # Engine "route3": global set-covering ILP as the primary path.  It self-
        # contains and returns None (never raises) when it finds no solution in
        # the budget; we then fall through to the baseline path below so route3
        # can only help, never regress solve rate.
        if engine == "route3" and has_scip:
            try:
                from . import route3_setcover

                r3 = route3_setcover.solve_group(group, budget)
            except Exception as r3_exc:  # noqa: BLE001 - never break the request
                r3 = None
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "route3 failed for group %s/%s: %s",
                    group.material, group.specifications, r3_exc,
                )
            if r3 is not None:
                groups.append(r3)
                continue
        # Engine "v4": V3 arc-flow global integer model + classifier routing.
        # Same contract as route3 — returns None (never raises) on infeasible/no
        # solution, then falls through to baseline so it can only help.
        if engine == "v4" and has_scip:
            try:
                from . import route_v4_arcflow

                r4 = route_v4_arcflow.solve_group(group, budget)
            except Exception as r4_exc:  # noqa: BLE001 - never break the request
                r4 = None
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "v4 failed for group %s/%s: %s",
                    group.material, group.specifications, r4_exc,
                )
            if r4 is not None:
                groups.append(r4)
                continue
        if has_scip:
            try:
                result = _solve_group_scip(group, budget)
            except Exception as scip_exc:  # restricted columns may still be incomplete
                try:
                    result = _solve_group_fallback(group, budget)
                    result["warnings"].append(
                        f"SCIP_FALLBACK: {type(scip_exc).__name__}: {scip_exc}"
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    # Both engines failed: the group is infeasible or too hard.
                    # Diagnose the shortage instead of aborting the whole request.
                    result = _unsolved_group_result(
                        group,
                        f"{type(fallback_exc).__name__}: {fallback_exc}",
                        time.monotonic() - group_started,
                    )
        else:
            try:
                result = _solve_group_fallback(group, budget)
                result["warnings"].append("PYSCIPOPT_UNAVAILABLE")
            except Exception as fallback_exc:  # noqa: BLE001
                result = _unsolved_group_result(
                    group,
                    f"PYSCIPOPT_UNAVAILABLE; {type(fallback_exc).__name__}: {fallback_exc}",
                    time.monotonic() - group_started,
                )
        # Optional alternative engine: the equivalent-stock CSP (route-2).  It is
        # opt-in (env NESTING_ROUTE2) and append-only in spirit -- it can only
        # REPLACE the incumbent when its result is strictly better by the same
        # lexicographic rule (utilisation, then welds).  It never raises, never
        # mutates the group, and self-verifies against the production verifier
        # before returning, so it is safe to run alongside the primary path.
        if _route2_enabled():
            # Give route-2 its own slice, but never let the two engines together
            # exceed the total time limit: cap by the wall-clock still left for
            # the whole problem.  (Opt-in, so a modest per-group overhead beyond
            # the MILP's slice is acceptable in exchange for higher solve rate.)
            total_left = time_limit_seconds - (time.monotonic() - started)
            r2_budget = max(0.1, min(budget, total_left))
            try:
                r2 = route2_equiv.solve_group(group, r2_budget)
            except Exception as r2_exc:  # engine unavailable / model error
                r2 = None
                result["warnings"].append(
                    f"ROUTE2_SKIPPED: {type(r2_exc).__name__}: {r2_exc}"
                )
            if r2 is not None and _better_group_result(r2, result):
                r2["warnings"] = list(result.get("warnings", [])) + ["ROUTE2_SELECTED"]
                result = r2
        groups.append(result)
    return groups


def solve_payload(
    payload: dict[str, Any], *, time_limit_seconds: float = 30.0,
    engine: str = "baseline",
) -> dict[str, Any]:
    """Solve all material/specification pools in a MOM payload.

    The function is deterministic for identical input and backend.  It never
    mutates ``payload`` and returns JSON-serialisable Python primitives.

    ``engine`` selects the primary solver: ``baseline`` (graded-relaxation MILP,
    the default) or ``route3`` (global set-covering ILP; falls back to baseline
    per group when it finds no solution in the budget).
    """

    if time_limit_seconds <= 0 or not math.isfinite(time_limit_seconds):
        raise ValueError("time_limit_seconds must be a positive finite number")
    started = time.monotonic()
    problem = parse_problem(payload)
    groups = _solve_problem(problem, float(time_limit_seconds), engine=engine)
    demand_units = sum(group.demand_length for group in problem.groups)
    used_units = sum(
        int(round(float(group["metrics"]["used_stock_length"]) * LENGTH_SCALE))
        for group in groups
    )
    total_welds = sum(group["metrics"]["welding_joint_quantity"] for group in groups)
    total_types = sum(
        group["metrics"]["welding_pattern_type_quantity"] for group in groups
    )
    total_cut_types = sum(
        group["metrics"]["cutting_pattern_type_quantity"] for group in groups
    )
    total_reused_weld_types = sum(
        group["metrics"]["reused_welding_pattern_type_quantity"] for group in groups
    )
    total_reused_cut_types = sum(
        group["metrics"]["reused_cutting_pattern_type_quantity"] for group in groups
    )
    total_kerf_units = sum(
        int(group["metrics"]["kerf_loss"]) for group in groups
    )
    total_remainder_units = sum(
        int(group["metrics"]["remainder_length"]) for group in groups
    )
    total_must_use_quantity = sum(
        int(group["metrics"]["must_use_stock_quantity"]) for group in groups
    )
    total_must_use_used_quantity = sum(
        int(group["metrics"]["must_use_used_quantity"]) for group in groups
    )
    total_must_use_length = sum(
        int(group["metrics"]["must_use_stock_length"]) for group in groups
    )
    normalized_paths = {
        record["path"]
        for group in groups
        for record in group.get("input_normalizations", [])
    }
    all_target = all(group["metrics"]["target_reached"] for group in groups)
    unsolved_group_count = sum(
        1 for group in groups if group["metrics"].get("solve_status") == "UNSOLVED"
    )
    all_core_optimal = all(
        group["metrics"].get("core_lexicographic_optimal", False) for group in groups
    )
    all_lexicographic_optimal = all(
        group["metrics"].get("lexicographic_optimal", False) for group in groups
    )
    if unsolved_group_count:
        status = "PARTIAL" if unsolved_group_count < len(groups) else "INFEASIBLE"
    else:
        status = "TARGET_REACHED" if all_target else "FEASIBLE"
    return {
        "status": status,
        "task_id": problem.task_id,
        "groups": groups,
        "summary": {
            "group_count": len(groups),
            "unsolved_group_count": unsolved_group_count,
            "demand_length": from_units(demand_units),
            "used_stock_length": from_units(used_units),
            "utilization_rate": demand_units / used_units if used_units else 0.0,
            "welding_joint_quantity": total_welds,
            "welding_pattern_type_quantity": total_types,
            "cutting_pattern_type_quantity": total_cut_types,
            "reused_welding_pattern_type_quantity": total_reused_weld_types,
            "reused_cutting_pattern_type_quantity": total_reused_cut_types,
            "kerf_loss": from_units(total_kerf_units),
            "remainder_length": from_units(total_remainder_units),
            "must_use_stock_quantity": total_must_use_quantity,
            "must_use_used_quantity": total_must_use_used_quantity,
            "must_use_stock_length": from_units(total_must_use_length),
            "normalized_length_field_quantity": len(normalized_paths),
            "target_reached": all_target,
            "core_lexicographic_optimal": all_core_optimal,
            "lexicographic_optimal": all_lexicographic_optimal,
            "elapsed_seconds": round(time.monotonic() - started, 6),
        },
        # service.solve_and_verify replaces this with the independent verifier.
        "verification": {"passed": None, "issues": [], "source": "pending"},
    }


__all__ = ["solve_payload"]
