"""Feasibility-first nesting engine (``engine="stable"``).

This phase looks for one verifier-valid plan and stops. It does not run the
lexicographic optimality contest and never labels a result optimal.

Business rules applied only on this path (the default engine is unchanged):

* omitted ``BladeMargin`` becomes 5 mm; an explicit kerf is kept;
* ``Min_Welding_Length`` is ignored (forced to 0 for both solver and verifier);
* the ``length // 3000`` weld cap is not applied; the input joint limit is kept.

Several constructive strategies share one stock pool. The first legal plan wins.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .domain import (
    MaterialGroup,
    NestingProblem,
    PipeDemand,
    _normalize_unweldable_area,
    from_units,
)
from .solver import (
    _Bar,
    _CutCandidate,
    _WeldCandidate,
    _assemble_group_result,
    _largest_allowed,
    _legal_pattern,
    _required_path,
    _solve_group_fallback,
    _solve_group_scip,
)

STABLE_DEFAULT_TIME_LIMIT_SECONDS = 3600.0
STABLE_PREVIEW_TIME_LIMIT_SECONDS = 60.0
STABLE_DEFAULT_BLADE_MARGIN_MM = 5
_NO_INTERVAL_CAP = 1
_JOINT_CAP = 1_000_000

Strategy = Callable[[MaterialGroup, float], dict[str, Any] | None]


class _Timeout(Exception):
    """The formal time limit expired before a feasible plan was stored."""


def _has_blade(node: Mapping[str, Any]) -> bool:
    nest = node.get("NestParam")
    if isinstance(nest, Mapping) and "BladeMargin" in nest:
        return True
    return "BladeMargin" in node


def _set_blade(node: dict[str, Any], margin: int) -> None:
    nest = node.get("NestParam")
    if not isinstance(nest, dict):
        nest = {}
        node["NestParam"] = nest
    nest["BladeMargin"] = margin


def _apply_process_rules(node: dict[str, Any]) -> None:
    """Ignore min weld distance and the L/3000 joint cap on this node."""

    node["Min_Welding_Length"] = 0
    node["Weld_Interval_mm"] = _NO_INTERVAL_CAP
    node["Max_Joints_Cap"] = _JOINT_CAP


def _order_forbidden_zones(pipe: dict[str, Any]) -> None:
    """Swap reversed forbidden-zone endpoints before solve and verify.

    The domain parser already treats end-before-start as an ordering slip.
    The verifier records that slip as an error, so the stable payload fixes
    it once and both sides see the same intervals.
    """

    raw = pipe.get("Unweldable_Area", pipe.get("unweldable_area"))
    areas = _normalize_unweldable_area(raw)
    if not areas:
        return
    ordered: list[Any] = []
    changed = False
    for area in areas:
        if isinstance(area, (list, tuple)) and len(area) >= 2:
            try:
                start = float(area[0])
                end = float(area[1])
            except (TypeError, ValueError):
                ordered.append(list(area))
                continue
            if start > end:
                ordered.append([area[1], area[0]])
                changed = True
            else:
                ordered.append(list(area[:2]))
        else:
            ordered.append(area)
    if changed or isinstance(raw, str):
        pipe["Unweldable_Area"] = ordered


def _clean_pipes(group: dict[str, Any]) -> None:
    """Drop non-positive demand and repeated pipe identities.

    Historical MOM rows repeat ``Parent_node|jlxh|cube_no`` or leave a
    zero-demand placeholder. The strict parser rejects both. Legacy output
    matches the first row's demand, so later duplicates are dropped.
    """

    pipes = group.get("Pipe")
    if not isinstance(pipes, list):
        return
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for pipe in pipes:
        if not isinstance(pipe, dict):
            continue
        try:
            demand = int(float(pipe.get("pipe_demand", 0) or 0))
        except (TypeError, ValueError):
            demand = 0
        if demand <= 0:
            continue
        key = (
            str(pipe.get("Parent_node", pipe.get("figure_number", ""))).strip(),
            str(pipe.get("jlxh", "")).strip(),
            str(pipe.get("cube_no", "")).strip(),
        )
        if key in merged:
            # Repeated identities in the MOM export duplicate the same demand.
            # Legacy output matches the first row, not the sum.
            continue
        copy = dict(pipe)
        copy["pipe_demand"] = demand
        _order_forbidden_zones(copy)
        merged[key] = copy
        order.append(key)
    group["Pipe"] = [merged[key] for key in order]


def prepare_stable_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy with stable-phase business parameters applied."""

    prepared = deepcopy(dict(payload))
    root, groups = _stable_nodes(prepared)
    if not _has_blade(root):
        _set_blade(root, STABLE_DEFAULT_BLADE_MARGIN_MM)
    _apply_process_rules(root)
    for group in groups:
        if group is not root:
            _apply_process_rules(group)
        _clean_pipes(group)
    return prepared


def _stable_nodes(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if "input" in payload and isinstance(payload["input"], dict):
        root = payload["input"]
        data = root.get("data")
        if isinstance(data, list) and data:
            return root, [item for item in data if isinstance(item, dict)]
        return root, [root]
    if "OriginalProblem" in payload and isinstance(payload["OriginalProblem"], dict):
        root = payload["OriginalProblem"]
        return root, [root]
    data = payload.get("data")
    if isinstance(data, list) and data:
        return payload, [item for item in data if isinstance(item, dict)]
    return payload, [payload]


def stable_problem_status(groups: Sequence[Mapping[str, Any]]) -> str:
    """Honest problem status. Optimality is never reported.

    ``INFEASIBLE`` is used only when every group has a proved stock gap.
    A timeout is never relabelled as infeasible.
    """

    if not groups:
        return "INPUT_ERROR"
    failure_classes: list[str] = []
    feasible = 0
    for group in groups:
        if group.get("metrics", {}).get("solve_status") == "FEASIBLE":
            feasible += 1
            continue
        failure_classes.append(str(group.get("failure_class") or "UNPROVED_NO_SOLUTION"))
    if feasible == len(groups):
        return "FEASIBLE"
    if feasible:
        return "PARTIAL"
    if failure_classes and all(item == "STOCK_GAP" for item in failure_classes):
        return "INFEASIBLE"
    if any(item == "TIMEOUT_NO_SOLUTION" for item in failure_classes):
        return "TIMEOUT"
    if failure_classes and all(item == "INPUT_ERROR" for item in failure_classes):
        return "INPUT_ERROR"
    return "UNSOLVED"


class _TrackedBar:
    __slots__ = ("bar", "from_must", "cap")

    def __init__(self, stock: int, kerf: int, mode: str, from_must: bool) -> None:
        self.bar = _Bar(stock, [], mode)
        self.from_must = from_must
        self.cap = self.bar.next_capacity(kerf)

    def add(self, part: int, kerf: int) -> None:
        self.bar.append(part, kerf)
        self.cap = self.bar.next_capacity(kerf)


class _Pool:
    """Finite heterogeneous stock with best-fit placement."""

    def __init__(self, group: MaterialGroup) -> None:
        self.group = group
        self.kerf = group.blade_margin
        self.mode = group.kerf_mode
        self.must_left: Counter[int] = Counter()
        self.opt_left: Counter[int] = Counter()
        for stock in group.stocks:
            self.must_left[stock.length] += stock.must_use_quantity
            self.opt_left[stock.length] += stock.quantity - stock.must_use_quantity
        self.open_bars: list[_TrackedBar] = []
        self.closed: list[_TrackedBar] = []
        self._empty_cap = {
            stock.length: _Bar(stock.length, [], self.mode).next_capacity(self.kerf)
            for stock in group.stocks
        }

    def clone_counts(self) -> tuple[Counter[int], Counter[int]]:
        return self.must_left.copy(), self.opt_left.copy()

    def restore_counts(self, must: Counter[int], opt: Counter[int]) -> None:
        self.must_left = must
        self.opt_left = opt
        self.open_bars.clear()
        self.closed.clear()

    def _tightest(self, need: int, *, allow_optional: bool) -> tuple[int, bool] | None:
        best: tuple[int, int, bool] | None = None
        for length, qty in self.must_left.items():
            cap = self._empty_cap.get(length, length)
            if qty > 0 and cap >= need:
                waste = cap - need
                if best is None or waste < best[0]:
                    best = (waste, length, True)
        if best is None and allow_optional:
            for length, qty in self.opt_left.items():
                cap = self._empty_cap.get(length, length)
                if qty > 0 and cap >= need:
                    waste = cap - need
                    if best is None or waste < best[0]:
                        best = (waste, length, False)
        if best is None:
            return None
        return best[1], best[2]

    def _open(self, need: int, *, allow_optional: bool) -> _TrackedBar | None:
        chosen = self._tightest(need, allow_optional=allow_optional)
        if chosen is None:
            return None
        length, from_must = chosen
        if from_must:
            self.must_left[length] -= 1
        else:
            self.opt_left[length] -= 1
        bar = _TrackedBar(length, self.kerf, self.mode, from_must)
        bar.add(need, self.kerf)
        if bar.cap > 0:
            self.open_bars.append(bar)
        else:
            self.closed.append(bar)
        return bar

    def place_one(self, length: int, *, allow_optional: bool = True) -> bool:
        fitting = [bar for bar in self.open_bars if bar.cap >= length]
        if fitting:
            # Prefer residual must-use bars, then the tightest residual.
            bar = min(fitting, key=lambda item: (not item.from_must, item.cap))
            bar.add(length, self.kerf)
            if bar.cap <= 0:
                self.open_bars.remove(bar)
                self.closed.append(bar)
            return True
        opened = self._open(length, allow_optional=allow_optional)
        return opened is not None

    def can_place(self, length: int, *, allow_optional: bool = True) -> bool:
        if any(bar.cap >= length for bar in self.open_bars):
            return True
        return self._tightest(length, allow_optional=allow_optional) is not None

    def usable_for(self, min_len: int) -> int:
        total = 0
        for length, qty in self.must_left.items():
            if length >= min_len and qty > 0:
                total += length * qty
        for length, qty in self.opt_left.items():
            if length >= min_len and qty > 0:
                total += length * qty
        total += sum(bar.cap for bar in self.open_bars if bar.cap >= min_len)
        return total

    def has_shorter_unopened(self, limit: int) -> bool:
        for length, qty in list(self.must_left.items()) + list(self.opt_left.items()):
            if qty > 0 and self._empty_cap.get(length, length) < limit:
                return True
        return False

    def pack_counter(self, segments: Counter[int], *, allow_optional: bool, seed_must: bool) -> bool:
        base_counts = Counter({length: qty for length, qty in segments.items() if qty > 0})
        saved_must = self.must_left.copy()
        saved_opt = self.opt_left.copy()
        # Exact complement fills first, then the older tightest-fit packer.
        # A failed exact attempt rolls back so it cannot block a greedy plan.
        for use_exact in (True, False):
            self.must_left = saved_must.copy()
            self.opt_left = saved_opt.copy()
            self.open_bars.clear()
            self.closed.clear()
            counts = base_counts.copy()
            if use_exact:
                self._apply_exact(counts, allow_optional=allow_optional)
            if seed_must and not self._seed_must(counts):
                continue
            if (
                self._greedy_fill(counts, allow_optional=allow_optional)
                and all(qty <= 0 for qty in counts.values())
                and self._must_respected(allow_optional)
            ):
                return True
        return False

    def _apply_exact(self, counts: Counter[int], *, allow_optional: bool) -> None:
        lengths = [length for length, qty in counts.items() if qty > 0]
        if len(lengths) < 2:
            return
        stock_lengths = [stock.length for stock in self.group.stocks]
        stock_set = set(stock_lengths)
        if len(lengths) > 24:
            ordered = sorted(set(lengths))
            lengths = ordered[:6] + ordered[-18:]
        ranked: list[tuple[tuple[int, int, int], int, Counter[int]]] = []
        for stock in sorted(set(stock_lengths), reverse=True):
            available = self.must_left[stock] + (self.opt_left[stock] if allow_optional else 0)
            if available <= 0:
                continue
            for recipe in _exact_recipes(stock, lengths, self.kerf, self.mode):
                awkward = sum(qty for length, qty in recipe.items() if length not in stock_set)
                if awkward <= 0:
                    continue
                score = (awkward, sum(recipe.values()), stock)
                ranked.append((score, stock, recipe))
        ranked.sort(reverse=True)
        for _score, stock, recipe in ranked:
            available = self.must_left[stock] + (self.opt_left[stock] if allow_optional else 0)
            times = available
            for length, qty in recipe.items():
                if qty <= 0 or counts[length] < qty:
                    times = 0
                    break
                times = min(times, counts[length] // qty)
            if times <= 0:
                continue
            parts = tuple(sorted(recipe.elements()))
            placed = 0
            for _ in range(times):
                from_must = self.must_left[stock] > 0
                if from_must:
                    self.must_left[stock] -= 1
                elif allow_optional and self.opt_left[stock] > 0:
                    self.opt_left[stock] -= 1
                else:
                    break
                bar = _TrackedBar(stock, self.kerf, self.mode, from_must)
                # Parts are assigned directly: sequential placement reserves a
                # trailing kerf and would reject a flush exact fill.
                bar.bar.parts = list(parts)
                bar.cap = 0
                self.closed.append(bar)
                placed += 1
            if placed <= 0:
                continue
            for length, qty in recipe.items():
                counts[length] -= qty * placed

    def _greedy_fill(self, counts: Counter[int], *, allow_optional: bool) -> bool:
        for length in sorted(counts, reverse=True):
            qty = counts[length]
            if qty <= 0:
                continue
            self.open_bars.sort(key=lambda bar: bar.cap)
            for bar in list(self.open_bars):
                if qty <= 0:
                    break
                if bar.cap < length:
                    continue
                room = _extra_pieces(bar, length, self.kerf)
                take = min(room, qty)
                if take <= 0:
                    continue
                for _ in range(take):
                    bar.add(length, self.kerf)
                qty -= take
                if bar.cap <= 0:
                    self.open_bars.remove(bar)
                    self.closed.append(bar)
            while qty > 0:
                stock = self._tightest(length, allow_optional=allow_optional)
                if stock is None:
                    counts[length] = qty
                    return False
                length_stock, from_must = stock
                room = _max_pieces(length_stock, length, self.kerf, self.mode)
                if room <= 0:
                    counts[length] = qty
                    return False
                take = min(room, qty)
                if from_must:
                    self.must_left[length_stock] -= 1
                else:
                    self.opt_left[length_stock] -= 1
                bar = _TrackedBar(length_stock, self.kerf, self.mode, from_must)
                for _ in range(take):
                    bar.add(length, self.kerf)
                qty -= take
                if bar.cap > 0:
                    self.open_bars.append(bar)
                else:
                    self.closed.append(bar)
            counts[length] = 0
        return True

    def _seed_must(self, counts: Counter[int]) -> bool:
        seeds: list[int] = []
        for length, qty in self.must_left.items():
            seeds.extend([length] * qty)
        for stock in sorted(seeds):
            cap = self._empty_cap.get(stock, stock)
            options = [part for part, qty in counts.items() if qty > 0 and part <= cap]
            if not options or self.must_left[stock] <= 0:
                return False
            part = max(options)
            counts[part] -= 1
            self.must_left[stock] -= 1
            bar = _TrackedBar(stock, self.kerf, self.mode, True)
            bar.add(part, self.kerf)
            if bar.cap > 0:
                self.open_bars.append(bar)
            else:
                self.closed.append(bar)
        return True

    def _must_respected(self, allow_optional: bool) -> bool:
        unused_must = sum(self.must_left.values())
        used_opt = 0
        for stock in self.group.stocks:
            original_opt = stock.quantity - stock.must_use_quantity
            used_opt += original_opt - self.opt_left[stock.length]
        if used_opt > 0 and unused_must > 0:
            return False
        if not allow_optional and used_opt > 0:
            return False
        return True

    def cut_counts(self) -> list[tuple[_CutCandidate, int]]:
        counter: Counter[tuple[int, tuple[int, ...]]] = Counter()
        for bar in self.closed + self.open_bars:
            if not bar.bar.parts:
                continue
            counter[(bar.bar.stock_length, tuple(sorted(bar.bar.parts)))] += 1
        return [
            (
                _CutCandidate(stock, parts, self.kerf, self.mode),
                qty,
            )
            for (stock, parts), qty in sorted(counter.items())
            if qty > 0
        ]


def _exact_recipes(
    stock: int, lengths: Sequence[int], kerf: int, mode: str
) -> list[Counter[int]]:
    """Multisets of segment lengths that fill ``stock`` with remainder 0."""

    recipes: list[Counter[int]] = []
    seen: set[tuple[int, ...]] = set()
    unique = [length for length in sorted(set(lengths)) if 0 < length <= stock]

    def add(parts: list[int]) -> None:
        if len(parts) < 2 or len(parts) > 6:
            return
        if any(part <= 0 or part > stock for part in parts):
            return
        key = tuple(sorted(parts))
        if key in seen:
            return
        seen.add(key)
        if _CutCandidate(stock, key, kerf, mode).remainder == 0:
            recipes.append(Counter(key))

    for index, left in enumerate(unique):
        for right in unique[index:]:
            add([left, right])
    for left in unique:
        for copies in range(3, 7):
            add([left] * copies)
            for filler in unique:
                if filler == left:
                    continue
                add([left] * copies + [filler])
    for index, left in enumerate(unique):
        for right in unique[index + 1 :]:
            add([left, left, right])
            add([left, right, right])
            add([left, left, left, right])
            add([left, right, right, right])
            add([left, left, right, right])
            for third in unique:
                if third <= right:
                    continue
                add([left, right, third])
                add([left, left, right, third])
    return recipes


def _extra_pieces(bar: _TrackedBar, part: int, kerf: int) -> int:
    probe = _Bar(bar.bar.stock_length, list(bar.bar.parts), bar.bar.kerf_mode)
    count = 0
    while probe.next_capacity(kerf) >= part and count < 10_000:
        probe.append(part, kerf)
        count += 1
    return count


def _max_pieces(stock: int, part: int, kerf: int, mode: str) -> int:
    if part <= 0 or part > stock:
        return 0
    if kerf == 0:
        return stock // part
    upper = (stock + kerf) // (part + kerf)
    for count in range(upper, 0, -1):
        candidate = _CutCandidate(stock, (part,) * count, kerf, mode)
        if candidate.remainder >= 0:
            return count
    return 0


def _acceptable(pipe: PipeDemand, group: MaterialGroup, parts: tuple[int, ...]) -> bool:
    max_stock = max(stock.length for stock in group.stocks)
    if not parts or any(part <= 0 or part > max_stock for part in parts):
        return False
    return _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length)


def _min_part(group: MaterialGroup, *, multi: bool) -> int:
    if multi and group.min_cut_length > 0:
        return group.min_cut_length
    return 1


def _uniform_parts(
    pipe: PipeDemand, group: MaterialGroup, pieces: int
) -> tuple[int, ...] | None:
    max_stock = max(stock.length for stock in group.stocks)
    if pieces < 1 or pieces > pipe.max_joints + 1:
        return None
    if pieces == 1:
        parts = (pipe.length,)
        return parts if _acceptable(pipe, group, parts) else None
    targets = [pipe.length * (index + 1) // pieces for index in range(pieces - 1)]
    positions: list[int] = []
    window = max(group.min_weld_distance, group.min_cut_length, 1)
    for target in targets:
        chosen: int | None = None
        previous = positions[-1] if positions else 0
        low = previous + _min_part(group, multi=True)
        if positions and group.min_weld_distance > 0:
            low = max(low, previous + group.min_weld_distance)
        for delta in range(0, max(window, pipe.length) + 1):
            for candidate in (target - delta, target + delta):
                if candidate < low or candidate >= pipe.length:
                    continue
                if candidate - previous > max_stock:
                    continue
                if not pipe.weld_allowed(candidate):
                    continue
                chosen = candidate
                break
            if chosen is not None:
                break
        if chosen is None:
            return None
        positions.append(chosen)
    parts_list: list[int] = []
    previous = 0
    for position in positions:
        parts_list.append(position - previous)
        previous = position
    parts_list.append(pipe.length - previous)
    parts = tuple(parts_list)
    return parts if _acceptable(pipe, group, parts) else None


def _peel_pattern(
    pipe: PipeDemand,
    group: MaterialGroup,
    *,
    longest_first: bool,
    finish_early: bool,
) -> tuple[int, ...] | None:
    """Cut stock-sized pieces, longest or shortest stock first."""

    max_stock = max(stock.length for stock in group.stocks)
    stocks = sorted({stock.length for stock in group.stocks}, reverse=longest_first)
    pos = 0
    parts: list[int] = []
    guard = 0
    while pos < pipe.length:
        guard += 1
        if guard > pipe.max_joints + 2:
            return None
        remaining = pipe.length - pos
        can_finish = remaining <= max_stock and (
            not parts or _acceptable(pipe, group, tuple(parts + [remaining]))
        )
        shorter = [stock for stock in stocks if stock < remaining]
        if can_finish and (finish_early or not shorter or len(parts) >= pipe.max_joints):
            parts.append(remaining)
            break
        if len(parts) >= pipe.max_joints:
            return None
        placed = False
        for stock in shorter:
            low = pos + _min_part(group, multi=True)
            if parts and group.min_weld_distance > 0:
                # The piece we are cutting becomes an internal segment once another
                # cut follows it. Keep the first piece free of that gap rule.
                if len(parts) >= 1:
                    low = max(low, pos + group.min_weld_distance) if len(parts) > 0 else low
            high = min(pipe.length - 1, pos + min(stock, max_stock))
            slots_after = pipe.max_joints - len(parts)
            if slots_after < 1:
                break
            max_rest = max_stock * slots_after
            high = min(high, pipe.length - _min_part(group, multi=True))
            weld = _largest_allowed(pipe, low, high)
            if weld is None:
                continue
            part = weld - pos
            rest = pipe.length - weld
            if part <= 0 or part > max_stock or rest <= 0 or rest > max_rest:
                continue
            parts.append(part)
            pos = weld
            placed = True
            break
        if not placed:
            if can_finish:
                parts.append(remaining)
                break
            return None
    result = tuple(parts)
    return result if _acceptable(pipe, group, result) else None


def _pattern_waste(parts: tuple[int, ...], group: MaterialGroup) -> int:
    stocks = [stock.length for stock in group.stocks]
    waste = 0
    for part in parts:
        fits = [stock - part for stock in stocks if stock >= part]
        if not fits:
            return 10**12
        waste += min(fits)
    # Exact bar fills are the pieces that keep a tight order feasible.
    waste -= 1000 * sum(1 for part in parts if part in stocks)
    return waste


def _snap_weld(pipe: PipeDemand, target: int, low: int, high: int) -> int | None:
    if low > high:
        return None
    window = max(0, high - low)
    for delta in range(0, window + 1):
        for candidate in (target - delta, target + delta):
            if low <= candidate <= high and pipe.weld_allowed(candidate):
                return candidate
    return None


def _two_piece(pipe: PipeDemand, group: MaterialGroup, weld_target: int) -> tuple[int, ...] | None:
    max_stock = max(stock.length for stock in group.stocks)
    low = _min_part(group, multi=True)
    high = pipe.length - _min_part(group, multi=True)
    if pipe.max_joints < 1 or low > high:
        return None
    weld = _snap_weld(pipe, weld_target, low, high)
    if weld is None:
        return None
    parts = (weld, pipe.length - weld)
    if max(parts) > max_stock:
        return None
    return parts if _acceptable(pipe, group, parts) else None


def _split_piece(
    pipe: PipeDemand,
    group: MaterialGroup,
    parts: tuple[int, ...],
    index: int,
    first_len: int,
) -> tuple[int, ...] | None:
    piece = parts[index]
    if first_len <= 0 or first_len >= piece:
        return None
    if len(parts) >= pipe.max_joints + 1:
        return None
    rest = piece - first_len
    for order in ((first_len, rest), (rest, first_len)):
        rebuilt = parts[:index] + order + parts[index + 1 :]
        if _acceptable(pipe, group, rebuilt):
            return rebuilt
    return None


def _complement_patterns(
    pipe: PipeDemand, group: MaterialGroup, base: tuple[int, ...]
) -> list[tuple[int, ...]]:
    """Split an awkward piece so several copies of it exact-fill a bar.

    One partner (``m == 1``) is the classic offcut pair. Larger ``m`` covers
    patterns such as three offcuts plus a short filler on the same bar.
    """

    stocks = [stock.length for stock in group.stocks]
    found: list[tuple[int, ...]] = []
    kerf = group.blade_margin
    for index, piece in enumerate(base):
        if piece in stocks:
            continue
        for host in stocks:
            for copies in range(1, 6):
                filler = host - copies * piece - copies * kerf
                if filler <= 0:
                    break
                if filler >= piece:
                    continue
                placed = False
                for delta in (0, 1, 2, 3, 4, 5, 8, 10, 15, 20, 40, 80):
                    guesses = (filler,) if delta == 0 else (filler - delta, filler + delta)
                    for guess in guesses:
                        if guess <= 0 or guess >= piece:
                            continue
                        rebuilt = _split_piece(pipe, group, base, index, guess)
                        if rebuilt is not None and rebuilt not in found:
                            found.append(rebuilt)
                            placed = True
                            break
                    if placed:
                        break
    return found


def _patterns_for(pipe: PipeDemand, group: MaterialGroup) -> list[tuple[int, ...]]:
    found: list[tuple[int, ...]] = []

    def add(parts: tuple[int, ...] | None) -> None:
        if parts is not None and parts not in found and _acceptable(pipe, group, parts):
            found.append(parts)

    max_stock = max(stock.length for stock in group.stocks)
    if pipe.length <= max_stock:
        add((pipe.length,))
    add(_required_path(pipe, group, max_stock))
    add(_peel_pattern(pipe, group, longest_first=True, finish_early=True))
    add(_peel_pattern(pipe, group, longest_first=True, finish_early=False))
    add(_peel_pattern(pipe, group, longest_first=False, finish_early=False))
    for stock in group.stocks:
        if stock.length < pipe.length:
            add(_two_piece(pipe, group, stock.length))
            add(_two_piece(pipe, group, pipe.length - stock.length))
    limit = min(pipe.max_joints + 1, 6)
    for pieces in range(2, limit + 1):
        add(_uniform_parts(pipe, group, pieces))
    bases = list(found)
    for base in bases:
        if len(base) > 3:
            continue
        for extra in _complement_patterns(pipe, group, base):
            add(extra)
            if len(found) >= 36:
                break
        if len(found) >= 36:
            break
    found.sort(key=lambda parts: (_pattern_waste(parts, group), len(parts), parts))
    return found[:18]


def _geometry_gap(group: MaterialGroup) -> str | None:
    if group.stock_length < group.demand_length:
        deficit = group.demand_length - group.stock_length
        return (
            f"总供料 {group.stock_length}mm 短于总需求 {group.demand_length}mm，"
            f"缺口 {deficit}mm"
        )
    max_stock = max(stock.length for stock in group.stocks)
    for pipe in group.pipes:
        need = (pipe.length + max_stock - 1) // max_stock
        if need > pipe.max_joints + 1:
            return (
                f"管 {pipe.pipe_id} 长 {pipe.length}mm，最长定尺 {max_stock}mm，"
                f"至少需要 {need} 段，超过输入焊口上限 {pipe.max_joints}"
            )
    return None


def _emit(
    group: MaterialGroup,
    weld: Counter[tuple[int, tuple[int, ...]]],
    cuts: list[tuple[_CutCandidate, int]],
    started: float,
) -> dict[str, Any] | None:
    for (pipe_index, parts), qty in weld.items():
        if qty <= 0:
            continue
        if not _acceptable(group.pipes[pipe_index], group, parts):
            return None
    weld_counts = [
        (_WeldCandidate(pipe_index, parts), qty)
        for (pipe_index, parts), qty in sorted(weld.items())
        if qty > 0
    ]
    if not weld_counts or not cuts:
        return None
    produced: Counter[int] = Counter()
    consumed: Counter[int] = Counter()
    for (_index, parts), qty in weld.items():
        for part in parts:
            consumed[part] += qty
    for candidate, qty in cuts:
        for part in candidate.parts:
            produced[part] += qty
    if produced != consumed:
        return None
    return _assemble_group_result(
        group,
        "stable",
        "FEASIBLE",
        weld_counts,
        cuts,
        time.monotonic() - started,
    )


def _pack_assignment(
    group: MaterialGroup,
    assignment: Sequence[tuple[int, tuple[int, ...], int]],
    started: float,
) -> dict[str, Any] | None:
    segments: Counter[int] = Counter()
    weld: Counter[tuple[int, tuple[int, ...]]] = Counter()
    for pipe_index, parts, qty in assignment:
        if qty <= 0:
            continue
        if not _acceptable(group.pipes[pipe_index], group, parts):
            return None
        for part in parts:
            segments[part] += qty
        weld[(pipe_index, parts)] += qty
    if not segments:
        return None
    must_qty = sum(stock.must_use_quantity for stock in group.stocks)
    attempts = [(False, False)]
    if must_qty:
        attempts.append((True, True))
    else:
        attempts = [(True, False)]
    for allow_optional, seed_must in attempts:
        pool = _Pool(group)
        if pool.pack_counter(segments, allow_optional=allow_optional, seed_must=seed_must):
            return _emit(group, weld, pool.cut_counts(), started)
    return None


def _assign_one(
    group: MaterialGroup, chooser: Callable[[PipeDemand], tuple[int, ...] | None], started: float
) -> dict[str, Any] | None:
    assignment: list[tuple[int, tuple[int, ...], int]] = []
    for index, pipe in enumerate(group.pipes):
        parts = chooser(pipe)
        if parts is None:
            return None
        assignment.append((index, parts, pipe.demand))
    return _pack_assignment(group, assignment, started)


def _strategy_whole(group: MaterialGroup, deadline: float) -> dict[str, Any] | None:
    _check(deadline)
    max_stock = max(stock.length for stock in group.stocks)

    def choose(pipe: PipeDemand) -> tuple[int, ...] | None:
        parts = (pipe.length,)
        if pipe.length <= max_stock and _acceptable(pipe, group, parts):
            return parts
        return None

    return _assign_one(group, choose, time.monotonic())


def _strategy_peel(
    group: MaterialGroup, deadline: float, *, longest_first: bool, finish_early: bool
) -> dict[str, Any] | None:
    _check(deadline)

    def choose(pipe: PipeDemand) -> tuple[int, ...] | None:
        if finish_early and pipe.length <= max(stock.length for stock in group.stocks):
            whole = (pipe.length,)
            if _acceptable(pipe, group, whole):
                return whole
        return _peel_pattern(
            pipe, group, longest_first=longest_first, finish_early=finish_early
        )

    return _assign_one(group, choose, time.monotonic())


def _strategy_uniform(group: MaterialGroup, deadline: float, pieces: int) -> dict[str, Any] | None:
    _check(deadline)

    def choose(pipe: PipeDemand) -> tuple[int, ...] | None:
        if pipe.length <= max(stock.length for stock in group.stocks) and pieces == 2:
            whole = (pipe.length,)
            if _acceptable(pipe, group, whole):
                return whole
        return _uniform_parts(pipe, group, min(pieces, pipe.max_joints + 1))

    return _assign_one(group, choose, time.monotonic())


def _anchor_patterns(pipe: PipeDemand, group: MaterialGroup) -> list[tuple[int, ...]]:
    found: list[tuple[int, ...]] = []

    def add(parts: tuple[int, ...] | None) -> None:
        if parts is not None and parts not in found and _acceptable(pipe, group, parts):
            found.append(parts)

    max_stock = max(stock.length for stock in group.stocks)
    if pipe.length <= max_stock:
        add((pipe.length,))
    add(_peel_pattern(pipe, group, longest_first=True, finish_early=False))
    add(_peel_pattern(pipe, group, longest_first=False, finish_early=False))
    add(_peel_pattern(pipe, group, longest_first=True, finish_early=True))
    for stock in group.stocks:
        if stock.length < pipe.length:
            add(_two_piece(pipe, group, stock.length))
            add(_two_piece(pipe, group, pipe.length - stock.length))
    return found


def _peel_rest(
    pipe: PipeDemand,
    group: MaterialGroup,
    pos: int,
    parts: list[int],
    *,
    longest_first: bool,
) -> tuple[int, ...] | None:
    max_stock = max(stock.length for stock in group.stocks)
    stocks = sorted({stock.length for stock in group.stocks}, reverse=longest_first)
    built = list(parts)
    guard = 0
    while pos < pipe.length:
        guard += 1
        if guard > pipe.max_joints + 2:
            return None
        remaining = pipe.length - pos
        if remaining <= max_stock and len(built) <= pipe.max_joints:
            candidate = tuple(built + [remaining])
            if _acceptable(pipe, group, candidate):
                return candidate
        if len(built) >= pipe.max_joints:
            return None
        placed = False
        for stock in stocks:
            if stock >= remaining:
                continue
            low = pos + _min_part(group, multi=True)
            high = min(pipe.length - _min_part(group, multi=True), pos + min(stock, max_stock))
            if low > high:
                continue
            weld = _largest_allowed(pipe, low, high)
            if weld is None:
                continue
            part = weld - pos
            if part <= 0 or part > max_stock:
                continue
            built.append(part)
            pos = weld
            placed = True
            break
        if not placed:
            return None
    return tuple(built) if _acceptable(pipe, group, tuple(built)) else None


def _parts_summing_to(
    pipe: PipeDemand, group: MaterialGroup, target: int, *, longest_first: bool
) -> tuple[int, ...] | None:
    if target <= 0 or target > pipe.length:
        return None
    if target != pipe.length and not pipe.weld_allowed(target):
        return None
    max_stock = max(stock.length for stock in group.stocks)
    if target <= max_stock:
        return (target,)
    stocks = sorted({stock.length for stock in group.stocks}, reverse=longest_first)
    pos = 0
    parts: list[int] = []
    guard = 0
    while pos < target:
        guard += 1
        if guard > pipe.max_joints + 2:
            return None
        remaining = target - pos
        if remaining <= max_stock:
            parts.append(remaining)
            break
        if len(parts) >= pipe.max_joints:
            return None
        placed = False
        for stock in stocks:
            if stock >= remaining:
                continue
            low = pos + _min_part(group, multi=True)
            high = min(target - _min_part(group, multi=True), pos + min(stock, max_stock))
            if low > high:
                continue
            weld = _largest_allowed(pipe, low, high)
            if weld is None:
                continue
            part = weld - pos
            if part <= 0 or part > max_stock:
                continue
            parts.append(part)
            pos = weld
            placed = True
            break
        if not placed:
            return None
    return tuple(parts)


def _patterns_containing(
    pipe: PipeDemand, group: MaterialGroup, segment: int
) -> list[tuple[int, ...]]:
    """Weld patterns that include one segment of exactly ``segment`` mm."""

    found: list[tuple[int, ...]] = []
    if segment <= 0 or segment > pipe.length:
        return found

    def add(parts: tuple[int, ...] | None) -> None:
        if parts is not None and segment in parts and parts not in found and _acceptable(pipe, group, parts):
            found.append(parts)

    if segment == pipe.length:
        add((segment,))
        return found
    if pipe.weld_allowed(segment):
        for longest in (True, False):
            add(_peel_rest(pipe, group, segment, [segment], longest_first=longest))
            if len(found) >= 3:
                return found
    prefix_len = pipe.length - segment
    if pipe.weld_allowed(prefix_len):
        for longest in (True, False):
            prefix = _parts_summing_to(pipe, group, prefix_len, longest_first=longest)
            if prefix is not None:
                add(prefix + (segment,))
            if len(found) >= 3:
                return found
    max_stock = max(stock.length for stock in group.stocks)
    for stock in sorted({item.length for item in group.stocks}, reverse=True):
        if stock >= pipe.length or stock + segment > pipe.length:
            continue
        if not pipe.weld_allowed(stock):
            continue
        if stock + segment == pipe.length:
            add((stock, segment))
            continue
        if not pipe.weld_allowed(stock + segment):
            continue
        if pipe.length - stock - segment <= max_stock:
            add((stock, segment, pipe.length - stock - segment))
        else:
            add(_peel_rest(pipe, group, stock + segment, [stock, segment], longest_first=True))
        if len(found) >= 4:
            break
    return found[:4]


def _same_pipe_counts(demand: int, parent: tuple[int, ...], child: tuple[int, ...], group: MaterialGroup) -> list[int]:
    parent_counts = Counter(parent)
    child_counts = Counter(child)
    stocks = [stock.length for stock in group.stocks]
    found: list[int] = []
    for donor, donor_qty in parent_counts.items():
        per_parent = donor_qty - child_counts.get(donor, 0)
        if per_parent <= 0:
            continue
        for host in stocks:
            for copies in range(1, 6):
                filler = host - copies * donor - copies * group.blade_margin
                if filler <= 0:
                    break
                per_child = child_counts.get(filler, 0) - parent_counts.get(filler, 0)
                if per_child <= 0:
                    continue
                denom = per_parent + copies * per_child
                if denom > 0 and (demand * per_parent) % denom == 0:
                    count = demand * per_parent // denom
                    if 0 < count < demand and count not in found:
                        found.append(count)
    for divisor in (2, 3, 4, 5):
        if demand % divisor == 0:
            count = demand // divisor
            if count not in found:
                found.append(count)
    return found[:8]


def _strategy_stock_tail(group: MaterialGroup, deadline: float) -> dict[str, Any] | None:
    """Give each pipe a stock-length segment, longest bars first, then pack the tails.

    Many legacy plans are exactly this shape: one piece equals a stock length and
    the offcut is short enough to share the remaining bars.
    """

    _check(deadline)
    started = time.monotonic()
    stocks = sorted({stock.length for stock in group.stocks}, reverse=True)
    left: Counter[int] = Counter()
    for stock in group.stocks:
        left[stock.length] += stock.quantity
    rows: list[tuple[int, tuple[int, ...], int]] = []
    for index, pipe in enumerate(group.pipes):
        _check(deadline)
        need = pipe.demand
        options: list[tuple[int, tuple[int, ...]]] = []
        seen: set[tuple[int, tuple[int, ...]]] = set()
        for stock in stocks:
            candidates: list[tuple[int, ...]] = []
            if pipe.length <= stock:
                whole = (pipe.length,)
                if _acceptable(pipe, group, whole):
                    candidates.append(whole)
            if pipe.length > stock:
                for target in (pipe.length - stock, stock):
                    if target <= 0:
                        continue
                    parts = _two_piece(pipe, group, target)
                    if parts is not None and stock in parts:
                        candidates.append(parts)
            for parts in candidates:
                key = (stock, parts)
                if key in seen:
                    continue
                seen.add(key)
                options.append((stock, parts))
        for stock, parts in options:
            if need <= 0:
                break
            uses = parts.count(stock)
            if uses <= 0:
                continue
            take = min(need, left[stock] // uses)
            if take <= 0:
                continue
            rows.append((index, parts, take))
            left[stock] -= take * uses
            need -= take
        if need > 0:
            return None
    return _pack_assignment(group, rows, started)


def _strategy_complement(group: MaterialGroup, deadline: float) -> dict[str, Any] | None:
    """Mix a stock-sized pattern with the pattern that uses its exact offcut."""

    _check(deadline)
    started = time.monotonic()
    # Tight one- and two-length orders are the mixes this strategy is for.
    # Wide orders only get a short look so later strategies still run.
    wide = len(group.pipes) > 4 or sum(pipe.demand for pipe in group.pipes) > 250
    budget = 1.0 if wide else 6.0
    local_deadline = time.monotonic() + min(
        budget, max(0.25, 0.45 * (deadline - time.monotonic()))
    )
    attempt_limit = 60 if wide else 2000
    anchors = [_anchor_patterns(pipe, group) for pipe in group.pipes]
    if any(not catalogue for catalogue in anchors):
        return None
    attempts = 0
    stocks = sorted({stock.length for stock in group.stocks}, reverse=True)
    kerf = group.blade_margin
    slack = group.stock_length - group.demand_length
    cross = len(group.pipes) <= 8 and slack <= max(20_000, group.demand_length // 40)

    def pack(rows: list[tuple[int, tuple[int, ...], int]] | None) -> dict[str, Any] | None:
        nonlocal attempts
        if rows is None:
            return None
        if time.monotonic() >= local_deadline:
            raise _Timeout
        _check(deadline)
        attempts += 1
        if attempts > attempt_limit:
            return None
        kept = [row for row in rows if row[2] > 0]
        if not kept:
            return None
        return _pack_assignment(group, kept, started)

    def rows_for(overrides: dict[int, list[tuple[tuple[int, ...], int]]]) -> list[tuple[int, tuple[int, ...], int]] | None:
        rows: list[tuple[int, tuple[int, ...], int]] = []
        for index, pipe in enumerate(group.pipes):
            if index in overrides:
                rows.extend((index, parts, qty) for parts, qty in overrides[index] if qty > 0)
                continue
            rows.append((index, anchors[index][0], pipe.demand))
        return rows

    try:
        for index, pipe in enumerate(group.pipes):
            for base in anchors[index][:8]:
                for child in _complement_patterns(pipe, group, base)[:10]:
                    for count in _same_pipe_counts(pipe.demand, base, child, group):
                        found = pack(
                            rows_for(
                                {
                                    index: [
                                        (base, pipe.demand - count),
                                        (child, count),
                                    ]
                                }
                            )
                        )
                        if found is not None:
                            return found
                        if attempts > attempt_limit:
                            return None
        if not cross:
            return None
        for index, pipe in enumerate(group.pipes):
            for base in anchors[index][:5]:
                base_counts = Counter(base)
                for piece, piece_qty in base_counts.items():
                    if piece in stocks:
                        continue
                    for host in stocks:
                        if host <= piece + kerf:
                            continue
                        for copies in range(1, 5):
                            filler = host - copies * piece - copies * kerf
                            if filler <= 0:
                                break
                            if filler > max(stocks):
                                continue
                            for other_index, other in enumerate(group.pipes):
                                if other_index == index:
                                    continue
                                for pattern in _patterns_containing(other, group, filler)[:2]:
                                    filler_qty = Counter(pattern)[filler]
                                    if filler_qty <= 0 or piece_qty <= 0:
                                        continue
                                    produced = pipe.demand * piece_qty
                                    consumed = copies * other.demand * filler_qty
                                    overrides: dict[int, list[tuple[tuple[int, ...], int]]]
                                    if produced == consumed:
                                        overrides = {
                                            index: [(base, pipe.demand)],
                                            other_index: [(pattern, other.demand)],
                                        }
                                    elif produced > consumed and consumed % piece_qty == 0:
                                        donor_qty = consumed // piece_qty
                                        rest = _rest_pattern(anchors[index], piece)
                                        if donor_qty <= 0 or donor_qty >= pipe.demand or rest is None:
                                            continue
                                        overrides = {
                                            index: [
                                                (base, donor_qty),
                                                (rest, pipe.demand - donor_qty),
                                            ],
                                            other_index: [(pattern, other.demand)],
                                        }
                                    elif consumed > produced and produced % (copies * filler_qty) == 0:
                                        child_qty = produced // (copies * filler_qty)
                                        rest = _rest_pattern(anchors[other_index], filler)
                                        if child_qty <= 0 or child_qty >= other.demand or rest is None:
                                            continue
                                        overrides = {
                                            index: [(base, pipe.demand)],
                                            other_index: [
                                                (pattern, child_qty),
                                                (rest, other.demand - child_qty),
                                            ],
                                        }
                                    else:
                                        continue
                                    found = pack(rows_for(overrides))
                                    if found is not None:
                                        return found
                                    if attempts > attempt_limit or time.monotonic() >= local_deadline:
                                        return None
    except _Timeout:
        if time.monotonic() >= deadline:
            raise
        return None
    return None


def _rest_pattern(patterns: Sequence[tuple[int, ...]], avoided: int) -> tuple[int, ...] | None:
    for parts in patterns:
        if avoided not in parts:
            return parts
    return None


def _strategy_pattern_scan(group: MaterialGroup, deadline: float) -> dict[str, Any] | None:
    """Try each pipe's legal patterns, then a long/short mix if one pattern is short of metal."""

    _check(deadline)
    # Keep a slice of the formal limit for the strategies that follow.
    local_deadline = time.monotonic() + min(2.0, max(0.2, 0.3 * (deadline - time.monotonic())))
    catalogues = [_patterns_for(pipe, group) for pipe in group.pipes]
    if any(not catalogue for catalogue in catalogues):
        return None
    started = time.monotonic()
    width = max(len(catalogue) for catalogue in catalogues)
    for slot in range(width):
        if time.monotonic() >= local_deadline:
            return None
        _check(deadline)
        assignment = [
            (index, catalogues[index][min(slot, len(catalogues[index]) - 1)], pipe.demand)
            for index, pipe in enumerate(group.pipes)
        ]
        packed = _pack_assignment(group, assignment, started)
        if packed is not None:
            return packed
    # Mix: move copies of the longest pipe types onto a later pattern.
    if width < 2:
        return None
    base = [(index, catalogues[index][0], pipe.demand) for index, pipe in enumerate(group.pipes)]
    order = sorted(range(len(group.pipes)), key=lambda index: -group.pipes[index].length)
    for pipe_index in order:
        catalogue = catalogues[pipe_index]
        if len(catalogue) < 2:
            continue
        demand = group.pipes[pipe_index].demand
        steps = []
        step = 1
        while step < demand:
            steps.append(step)
            step *= 2
        for divisor in (2, 3, 4, 5):
            if demand % divisor == 0:
                steps.append(demand // divisor)
        steps.append(demand)
        for short_count in steps:
            if time.monotonic() >= local_deadline:
                return None
            _check(deadline)
            mixed = list(base)
            mixed[pipe_index] = (pipe_index, catalogue[0], demand - short_count)
            # Represent the short copies as an extra assignment row.
            trial = [row for row in mixed if row[2] > 0]
            trial.append((pipe_index, catalogue[1], short_count))
            packed = _pack_assignment(group, trial, started)
            if packed is not None:
                return packed
    return None


def _strategy_adaptive(group: MaterialGroup, deadline: float, *, longest_first: bool) -> dict[str, Any] | None:
    """Place one pipe at a time, peeling a short stock while long metal cannot cover the rest."""

    _check(deadline)
    soft = time.monotonic() + min(1.5, max(0.15, 0.25 * (deadline - time.monotonic())))
    try:
        return _strategy_adaptive_body(group, min(deadline, soft), longest_first=longest_first)
    except _Timeout:
        if time.monotonic() >= deadline:
            raise
        return None


def _strategy_adaptive_body(
    group: MaterialGroup, deadline: float, *, longest_first: bool
) -> dict[str, Any] | None:
    _check(deadline)
    started = time.monotonic()
    pool = _Pool(group)
    weld: Counter[tuple[int, tuple[int, ...]]] = Counter()
    demand_left = group.demand_length
    jobs: list[int] = []
    order = sorted(
        range(len(group.pipes)),
        key=lambda index: (-group.pipes[index].length, group.pipes[index].pipe_id),
    )
    if group.pipes:
        for copy in range(max(pipe.demand for pipe in group.pipes)):
            for index in order:
                if copy < group.pipes[index].demand:
                    jobs.append(index)
    max_stock = max(stock.length for stock in group.stocks)
    stocks = sorted({stock.length for stock in group.stocks}, reverse=longest_first)
    for pipe_index in jobs:
        _check(deadline)
        pipe = group.pipes[pipe_index]
        parts = _carve_adaptive(
            pipe, group, pool, demand_left, stocks, max_stock, deadline
        )
        if parts is None:
            return None
        weld[(pipe_index, parts)] += 1
        demand_left -= pipe.length
    if not pool._must_respected(True):
        return None
    return _emit(group, weld, pool.cut_counts(), started)


def _carve_adaptive(
    pipe: PipeDemand,
    group: MaterialGroup,
    pool: _Pool,
    demand_left: int,
    stocks: Sequence[int],
    max_stock: int,
    deadline: float,
) -> tuple[int, ...] | None:
    pos = 0
    parts: list[int] = []
    guard = 0
    while pos < pipe.length:
        guard += 1
        if guard > min(pipe.max_joints + 2, 64):
            return None
        _check(deadline)
        remaining = pipe.length - pos
        can_finish = remaining <= max_stock and pool.can_place(remaining)
        shortfall = pool.usable_for(remaining) < demand_left + pool.kerf
        if can_finish and (not shortfall or len(parts) >= pipe.max_joints or not pool.has_shorter_unopened(remaining)):
            if not pool.place_one(remaining):
                return None
            parts.append(remaining)
            demand_left -= remaining
            break
        if len(parts) >= pipe.max_joints:
            return None
        placed = False
        for stock in stocks:
            if stock >= remaining:
                continue
            low = pos + _min_part(group, multi=True)
            high = min(pipe.length - 1, pos + min(stock, max_stock))
            slots_after = pipe.max_joints - len(parts)
            if slots_after < 1:
                break
            weld = _largest_allowed(pipe, low, high)
            if weld is None:
                continue
            part = weld - pos
            rest = pipe.length - weld
            if part <= 0 or rest <= 0 or rest > max_stock * slots_after:
                continue
            if not pool.can_place(part):
                continue
            if not pool.place_one(part):
                continue
            parts.append(part)
            pos = weld
            demand_left -= part
            placed = True
            break
        if not placed:
            if can_finish and pool.place_one(remaining):
                parts.append(remaining)
                break
            return None
    result = tuple(parts)
    if not _acceptable(pipe, group, result):
        return None
    return result


def _strategy_fallback(group: MaterialGroup, deadline: float) -> dict[str, Any] | None:
    remaining = deadline - time.monotonic()
    if remaining < 0.2:
        return None
    # Leave a slice for the constructive scans that follow this strategy.
    budget = remaining if remaining < 2.5 else max(1.0, remaining * 0.65)
    try:
        return _solve_group_fallback(group, budget, abort_on_time=True)
    except (ValueError, TimeoutError, _Timeout):
        return None


def _strategy_baseline_master(group: MaterialGroup, deadline: float) -> dict[str, Any] | None:
    """Restricted master used only as a feasibility probe, not an optimality proof."""

    remaining = deadline - time.monotonic()
    pieces = sum(pipe.demand for pipe in group.pipes)
    # Model build on large pools overruns the formal limit before SCIP starts.
    if remaining < 0.5 or pieces > 80:
        return None
    try:
        import pyscipopt  # noqa: F401
    except Exception:
        return None
    try:
        return _solve_group_scip(group, remaining)
    except (ValueError, _Timeout):
        return None


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _Timeout


def _mark_feasible(result: dict[str, Any], strategy: str, source: str) -> dict[str, Any]:
    metrics = result["metrics"]
    metrics["solve_status"] = "FEASIBLE"
    metrics["core_lexicographic_optimal"] = False
    metrics["lexicographic_optimal"] = False
    metrics["optimization_phases_completed"] = []
    metrics["optimization_phase_incomplete"] = "not_attempted_feasibility_only"
    metrics["stable_strategy"] = strategy
    metrics["failure_class"] = ""
    metrics["result_source"] = source
    result["solver_path"] = "stable"
    result["failure_class"] = ""
    result["result_source"] = source
    warnings = result.setdefault("warnings", [])
    warnings.append(f"STABLE_STRATEGY: {strategy}")
    warnings.append("FEASIBILITY_ONLY_NO_OPTIMALITY_CLAIM")
    return result


def _fail_group(
    group: MaterialGroup,
    failure_class: str,
    message: str,
    elapsed: float,
) -> dict[str, Any]:
    result = _assemble_group_result(group, "stable", "UNSOLVED", [], [], elapsed)
    result["metrics"]["core_lexicographic_optimal"] = False
    result["metrics"]["lexicographic_optimal"] = False
    result["metrics"]["optimization_phases_completed"] = []
    result["metrics"]["optimization_phase_incomplete"] = "not_attempted_feasibility_only"
    result["metrics"]["stable_strategy"] = ""
    result["metrics"]["failure_class"] = failure_class
    result["metrics"]["result_source"] = "none"
    result["solver_path"] = "stable"
    result["failure_class"] = failure_class
    result["failure_message"] = message
    result["result_source"] = "none"
    result["warnings"].append(f"STABLE_FAILURE: {failure_class}: {message}")
    result["shortage_diagnosis"] = {
        "failure_class": failure_class,
        "message": message,
        "proved_infeasible": failure_class == "STOCK_GAP",
    }
    return result


def _strategies() -> list[tuple[str, str, Strategy]]:
    """(name, result_source, function). Order is the feasibility search order."""

    return [
        ("whole-first", "native", _strategy_whole),
        (
            "stock-peel-long",
            "native",
            lambda group, deadline: _strategy_peel(
                group, deadline, longest_first=True, finish_early=False
            ),
        ),
        (
            "stock-peel-short",
            "native",
            lambda group, deadline: _strategy_peel(
                group, deadline, longest_first=False, finish_early=False
            ),
        ),
        (
            "largest-early-finish",
            "native",
            lambda group, deadline: _strategy_peel(
                group, deadline, longest_first=True, finish_early=True
            ),
        ),
        ("uniform-2", "native", lambda group, deadline: _strategy_uniform(group, deadline, 2)),
        ("uniform-3", "native", lambda group, deadline: _strategy_uniform(group, deadline, 3)),
        ("uniform-4", "native", lambda group, deadline: _strategy_uniform(group, deadline, 4)),
        ("uniform-6", "native", lambda group, deadline: _strategy_uniform(group, deadline, 6)),
        ("stock-tail", "native", _strategy_stock_tail),
        ("complement-mix", "native", _strategy_complement),
        ("group-fallback", "fallback", _strategy_fallback),
        ("pattern-scan", "native", _strategy_pattern_scan),
        (
            "adaptive-peel-long",
            "native",
            lambda group, deadline: _strategy_adaptive(group, deadline, longest_first=True),
        ),
        (
            "adaptive-peel-short",
            "native",
            lambda group, deadline: _strategy_adaptive(group, deadline, longest_first=False),
        ),
        ("baseline-restricted-master", "baseline-restricted-master", _strategy_baseline_master),
    ]


def _checkpoint_load(path: str | None, task_id: str) -> dict[str, Any] | None:
    if not path:
        return None
    file = Path(path)
    if not file.exists():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("task_id") != task_id:
        return None
    return data


def _checkpoint_store(path: str | None, payload: Mapping[str, Any]) -> None:
    if not path:
        return
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, file)


def solve_group_stable(
    group: MaterialGroup,
    time_limit: float,
    *,
    checkpoint_path: str | None = None,
    checkpoint_state: dict[str, Any] | None = None,
    group_index: int = 0,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + max(time_limit, 0.0)
    gap = _geometry_gap(group)
    if gap and group.stock_length < group.demand_length:
        return _fail_group(group, "STOCK_GAP", gap, time.monotonic() - started)
    if gap and "至少需要" in gap:
        return _fail_group(group, "STOCK_GAP", gap, time.monotonic() - started)

    skipped = set()
    if checkpoint_state is not None:
        skipped = set(checkpoint_state.get("strategies_done") or [])
    tried: list[str] = []
    errors: list[str] = []
    for name, source, function in _strategies():
        if name in skipped:
            tried.append(name)
            continue
        if time.monotonic() >= deadline:
            return _fail_group(
                group,
                "TIMEOUT_NO_SOLUTION",
                "时限内策略未找到可行解：" + ",".join(tried),
                time.monotonic() - started,
            )
        tried.append(name)
        if checkpoint_state is not None:
            checkpoint_state["strategies_done"] = list(tried)
            checkpoint_state["group_index"] = group_index
            checkpoint_state["status"] = "running"
            _checkpoint_store(checkpoint_path, checkpoint_state)
        try:
            found = function(group, deadline)
        except _Timeout:
            return _fail_group(
                group,
                "TIMEOUT_NO_SOLUTION",
                f"策略 {name} 超时，已尝试：" + ",".join(tried),
                time.monotonic() - started,
            )
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            found = None
        if found is not None:
            found = _mark_feasible(found, name, source)
            found["metrics"]["elapsed_seconds"] = round(time.monotonic() - started, 6)
            return found
    detail = ",".join(tried)
    if errors:
        detail += "；错误 " + " | ".join(errors[-3:])
    if time.monotonic() >= deadline:
        failure_class = "TIMEOUT_NO_SOLUTION"
        message = "时限内没有可行解：" + detail
    else:
        failure_class = "UNPROVED_NO_SOLUTION"
        message = "全部构造策略结束仍无可行解（未证明不可行）：" + detail
        if gap:
            message = gap + "；" + message
    return _fail_group(group, failure_class, message, time.monotonic() - started)


def solve_stable_problem(
    problem: NestingProblem,
    time_limit_seconds: float,
    *,
    checkpoint_path: str | None = None,
) -> list[dict[str, Any]]:
    state = _checkpoint_load(checkpoint_path, problem.task_id) or {
        "task_id": problem.task_id,
        "groups": [],
        "strategies_done": [],
        "status": "running",
        "done": False,
    }
    groups: list[dict[str, Any]] = list(state.get("groups") or [])
    if state.get("done") and len(groups) == len(problem.groups):
        return groups
    started = time.monotonic()
    deadline = started + max(time_limit_seconds, 0.0)
    for index in range(len(groups), len(problem.groups)):
        remaining_groups = len(problem.groups) - index
        budget = max(0.05, (deadline - time.monotonic()) / max(1, remaining_groups))
        state["strategies_done"] = []
        result = solve_group_stable(
            problem.groups[index],
            budget,
            checkpoint_path=checkpoint_path,
            checkpoint_state=state,
            group_index=index,
        )
        groups.append(result)
        state["groups"] = groups
        state["strategies_done"] = []
        state["status"] = "running"
        _checkpoint_store(checkpoint_path, state)
    state["groups"] = groups
    state["done"] = True
    state["status"] = "finished"
    state["strategies_done"] = []
    _checkpoint_store(checkpoint_path, state)
    return groups
