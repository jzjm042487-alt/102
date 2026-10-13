"""Budgeted column generation with lexicographic locks (engine ``lexcg``).

The master keeps weld columns ``x[p]`` and cut columns ``y[q]`` linked by
segment balance.  Pricing is a resource-constrained weld search plus a
bounded cut knapsack.  Every pricing routine stops on a time or node budget.
Hitting that budget yields an incumbent and the status ``unproved``; it never
reports ``INFEASIBLE`` or ``OPTIMAL_LEXICOGRAPHIC``.

Objective locks, in order:

1. total used stock length
2. total weld joints
3. ordered weld-pattern types
4. cut-pattern types (stock length + segment multiset)

``Target_Util_Rate`` is reported, not enforced.  ``_solve_group_fallback`` is
only a warm start; those columns are tagged ``warm-start``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import time
from typing import Any

from .domain import MaterialGroup, PipeDemand
from .solver import (
    _CutCandidate,
    _WeldCandidate,
    _assemble_group_result,
    _solve_group_fallback,
)


# Search depth for the geometric family.  This is not the process joint limit.
SEARCH_JOINT_CAP = 8
MAX_PATTERNS_PER_PIPE = 48
MAX_IDENTICAL_REPEAT = 4
MAX_SEGMENTS_FOR_ALL_PAIRS = 28
SEED_NODE_BUDGET = 80_000
CUT_NODE_BUDGET = 120_000
PRICE_NODE_BUDGET = 80_000
EXACT_LENGTH_LIMIT = 800
RC_EPS = 1e-6
LAYER_NAMES = (
    "used_stock_length",
    "welding_joint_quantity",
    "welding_pattern_type_quantity",
    "cutting_pattern_type_quantity",
)


@dataclass
class _Budget:
    deadline: float
    node_limit: int
    nodes: int = 0
    truncated: bool = False

    def tick(self, count: int = 1) -> bool:
        self.nodes += count
        if self.nodes > self.node_limit or time.monotonic() > self.deadline:
            self.truncated = True
            return False
        return True

    @property
    def expired(self) -> bool:
        return self.truncated or time.monotonic() > self.deadline


@dataclass
class _WeldCol:
    pipe_index: int
    parts: tuple[int, ...]
    sources: set[str] = field(default_factory=set)

    @property
    def joints(self) -> int:
        return max(0, len(self.parts) - 1)


@dataclass
class _CutCol:
    stock_length: int
    parts: tuple[int, ...]
    sources: set[str] = field(default_factory=set)


def _fits(stock: int, parts: tuple[int, ...], kerf: int, kerf_mode: str) -> bool:
    if not parts or any(part <= 0 for part in parts):
        return False
    between = kerf * max(0, len(parts) - 1)
    remainder = stock - sum(parts) - between
    if kerf_mode == "WITH_REMAINDER" and remainder > 0:
        remainder -= kerf
    return remainder >= 0


def _legal_parts(
    pipe: PipeDemand,
    parts: tuple[int, ...],
    *,
    max_stock: int,
    min_weld: int,
    min_cut: int,
    joint_cap: int,
) -> bool:
    if not parts or sum(parts) != pipe.length or any(part <= 0 or part > max_stock for part in parts):
        return False
    if len(parts) - 1 > min(pipe.max_joints, joint_cap):
        return False
    # Min_Cut_Length applies once a pipe is actually spliced.  Min_Welding_Length
    # is not an optimisation constraint, but the verifier still rejects an
    # internal segment shorter than it, so columns have to satisfy that check.
    if len(parts) >= 2 and min_cut > 0 and any(part < min_cut for part in parts):
        return False
    position = 0
    for index, part in enumerate(parts[:-1]):
        position += part
        if not pipe.weld_allowed(position):
            return False
        if index > 0 and min_weld > 0 and part < min_weld:
            return False
    return True


def _select_patterns(
    patterns: set[tuple[int, ...]],
    limit: int,
    *,
    priority: list[tuple[int, ...]] | None = None,
) -> list[tuple[int, ...]]:
    """Keep few-joint patterns and the offcut-repeat patterns that actually pack.

    A pure fewest-joints cut drops the repeated offcut that is the only
    feasible splice on a tight bar, so priority patterns are reserved first.
    """

    ranked = sorted(patterns, key=lambda item: (len(item), item))
    if len(ranked) <= limit and not priority:
        return ranked
    chosen: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()

    def take(seq: list[tuple[int, ...]], cap: int) -> None:
        for item in seq:
            if item not in patterns or item in seen:
                continue
            seen.add(item)
            chosen.append(item)
            if len(chosen) >= cap:
                return

    reserved = list(priority or [])
    take(reserved, min(len(reserved), max(1, limit // 2)))
    take(ranked, limit)
    return chosen


def _seed_weld_patterns(
    pipe: PipeDemand,
    stocks: list[int],
    *,
    kerf: int,
    min_weld: int,
    min_cut: int,
    budget: _Budget,
) -> tuple[list[tuple[int, ...]], bool]:
    """Geometric weld family: stock complements, bar partners, short repeats.

    The family is finite and does not walk every legal copy of a segment.
    ``budget`` stops the loops; a stop is reported to the caller.
    """

    length = pipe.length
    max_stock = max(stocks)
    joint_cap = min(pipe.max_joints, SEARCH_JOINT_CAP)
    found: set[tuple[int, ...]] = set()
    priority: list[tuple[int, ...]] = []

    def consider(parts: tuple[int, ...]) -> None:
        if budget.expired:
            return
        if _legal_parts(
            pipe,
            parts,
            max_stock=max_stock,
            min_weld=min_weld,
            min_cut=min_cut,
            joint_cap=joint_cap,
        ):
            found.add(parts)

    if length <= max_stock:
        consider((length,))
    if pipe.max_joints <= 1 and length <= EXACT_LENGTH_LIMIT:
        # Complete for at most one weld: every legal coordinate, not a grid of
        # stock lengths.  Still node-budgeted.
        for position in range(1, length):
            if not budget.tick():
                break
            consider((position, length - position))
        return _select_patterns(found, MAX_PATTERNS_PER_PIPE, priority=priority), budget.truncated

    complements: list[int] = []
    for stock in stocks:
        if not budget.tick():
            break
        complement = length - stock
        if 0 < complement <= max_stock:
            complements.append(complement)
            consider((stock, complement))
            consider((complement, stock))
    partners: set[int] = set()
    for complement in complements:
        for stock in stocks:
            if not budget.tick():
                break
            partner = stock - kerf - complement
            if 0 < partner <= max_stock:
                partners.add(partner)
    for left in stocks:
        for right in stocks:
            if not budget.tick():
                break
            partner = right - kerf - left
            if 0 < partner <= max_stock:
                partners.add(partner)
    repeat_cap = min(MAX_IDENTICAL_REPEAT + 2, joint_cap + 1)
    for partner in partners:
        for copies in range(1, repeat_cap + 1):
            if not budget.tick():
                break
            residual = length - partner * copies
            if 0 < residual <= max_stock:
                consider((partner,) * copies + (residual,))
                consider((residual,) + (partner,) * copies)
    for stock in stocks:
        for copies in range(1, repeat_cap + 1):
            if not budget.tick():
                break
            residual = length - stock * copies
            if copies >= 1 and 0 < residual <= max_stock:
                consider((stock,) * copies + (residual,))
                consider((residual,) + (stock,) * copies)
    for left in stocks:
        for right in stocks:
            if not budget.tick():
                break
            residual = length - left - right
            if 0 < residual <= max_stock:
                consider((left, right, residual))
                consider((residual, left, right))
                consider((left, residual, right))
    # Offcut of a bar that also holds one whole pipe: c = S - kerf - L.
    # Repeating c leaves a residual (L mod c).  That is the packing pattern
    # on a tight specification such as φ51×9.5, and there is one pattern per
    # stock and copy count — not one column per bar.
    for stock in stocks:
        if not budget.tick():
            break
        if stock <= length:
            continue
        offcut = stock - kerf - length
        if not 0 < offcut <= max_stock:
            continue
        for copies in range(1, repeat_cap + 1):
            if not budget.tick():
                break
            used = offcut * copies
            if used == length:
                pattern = (offcut,) * copies
                consider(pattern)
                priority.append(pattern)
                continue
            residual = length - used
            if 0 < residual <= max_stock:
                forward = (offcut,) * copies + (residual,)
                backward = (residual,) + (offcut,) * copies
                consider(forward)
                consider(backward)
                priority.append(forward)
                priority.append(backward)
    # Largest-legal greedy chain.  One pattern, linear in the joint cap.
    if not budget.expired:
        greedy = _greedy_chain(
            pipe,
            max_stock=max_stock,
            min_weld=min_weld,
            min_cut=min_cut,
            joint_cap=joint_cap,
        )
        if greedy is not None:
            consider(greedy)
    return _select_patterns(found, MAX_PATTERNS_PER_PIPE, priority=priority), budget.truncated


def _greedy_chain(
    pipe: PipeDemand,
    *,
    max_stock: int,
    min_weld: int,
    min_cut: int,
    joint_cap: int,
) -> tuple[int, ...] | None:
    parts: list[int] = []
    position = 0
    while position < pipe.length and len(parts) <= joint_cap:
        remaining = pipe.length - position
        if remaining <= max_stock:
            parts.append(remaining)
            position = pipe.length
            break
        low = position + (min_cut if min_cut > 0 else 1)
        if parts and min_weld > 0:
            low = max(low, position + min_weld)
        high = min(pipe.length - 1, position + max_stock)
        if min_cut > 0:
            high = min(high, pipe.length - min_cut)
        weld = high
        placed = False
        while weld >= low:
            if pipe.weld_allowed(weld):
                parts.append(weld - position)
                position = weld
                placed = True
                break
            weld -= 1
        if not placed:
            return None
    if position != pipe.length:
        return None
    result = tuple(parts)
    if _legal_parts(
        pipe,
        result,
        max_stock=max_stock,
        min_weld=min_weld,
        min_cut=min_cut,
        joint_cap=joint_cap,
    ):
        return result
    return None


def _seed_cut_patterns(
    stocks: list[int],
    quantities: dict[int, int],
    segments: list[int],
    *,
    kerf: int,
    kerf_mode: str,
    budget: _Budget,
    repeated_patterns: list[tuple[int, ...]] | None = None,
) -> list[tuple[int, tuple[int, ...]]]:
    """Singles, a capped identical-repeat, and pairs.

    Identical repeats stop at ``MAX_IDENTICAL_REPEAT``.  The knapsack that
    wants a longer run returns one pattern, not one column per copy.
    """

    found: list[tuple[int, tuple[int, ...]]] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()

    def add(stock: int, parts: tuple[int, ...]) -> None:
        key = (stock, parts)
        if key in seen:
            return
        if _fits(stock, parts, kerf, kerf_mode):
            seen.add(key)
            found.append(key)

    segment_set = set(segments)
    pair_ok = len(segments) <= MAX_SEGMENTS_FOR_ALL_PAIRS
    # Repeated pieces are the ones a splice actually consumes in bulk.
    # Attaching those, not every pair, stays inside the node budget.
    repeated_pieces = sorted(
        {
            part
            for pattern in (repeated_patterns or [])
            for part, count in Counter(pattern).items()
            if count >= 3 and part in segment_set
        }
    )
    for stock in stocks:
        if quantities.get(stock, 0) <= 0:
            continue
        for segment in segments:
            if not budget.tick():
                return found
            if segment <= stock:
                add(stock, (segment,))
            for copies in range(2, MAX_IDENTICAL_REPEAT + 1):
                if not budget.tick():
                    return found
                add(stock, (segment,) * copies)
        if pair_ok:
            for index, left in enumerate(segments):
                for right in segments[index:]:
                    if not budget.tick():
                        return found
                    add(stock, tuple(sorted((left, right))))
        # Exact companion of one segment: one pattern per segment, not per copy.
        for segment in segments:
            if not budget.tick():
                return found
            offcut = stock - kerf - segment
            if offcut in segment_set and offcut > 0:
                add(stock, tuple(sorted((segment, offcut))))
        # k copies of a repeated splice piece beside one larger segment.
        for big in segments:
            for piece in repeated_pieces:
                if not budget.tick():
                    return found
                for copies in range(1, MAX_IDENTICAL_REPEAT + 1):
                    parts = tuple(sorted((big,) + (piece,) * copies))
                    if not _fits(stock, parts, kerf, kerf_mode):
                        break
                    add(stock, parts)
    return found


def knapsack_best_pattern(
    stock: int,
    lengths: list[int],
    values: list[float],
    *,
    kerf: int,
    kerf_mode: str,
    node_budget: int,
    deadline: float,
) -> tuple[tuple[int, ...] | None, bool, int]:
    """One best cut pattern via unbounded knapsack, or None.

    Each capacity update spends one node.  The search never emits a column
    per legal copy; reconstruction returns a single pattern.  A budget stop
    sets ``truncated`` and still returns the best pattern completed so far.
    """

    if stock <= 0 or node_budget <= 0:
        return None, True, 0
    items: list[tuple[int, float]] = []
    seen: set[int] = set()
    for length, value in zip(lengths, values):
        if length in seen or length <= 0 or length > stock:
            continue
        seen.add(length)
        items.append((int(length), float(value)))
    if not items:
        return None, False, 0
    capacity = stock + max(0, kerf)
    negative = -10**18
    best = [negative] * (capacity + 1)
    best[0] = 0.0
    previous = [-1] * (capacity + 1)
    which = [-1] * (capacity + 1)
    nodes = 0
    truncated = False
    for item_index, (length, value) in enumerate(items):
        weight = length + max(0, kerf)
        if weight <= 0 or weight > capacity:
            continue
        for cursor in range(0, capacity - weight + 1):
            nodes += 1
            if nodes > node_budget or time.monotonic() > deadline:
                truncated = True
                break
            if best[cursor] <= negative / 2:
                continue
            nxt = cursor + weight
            score = best[cursor] + value
            if score > best[nxt]:
                best[nxt] = score
                previous[nxt] = cursor
                which[nxt] = item_index
        if truncated:
            break
    chosen = max(range(capacity + 1), key=lambda point: best[point])
    if best[chosen] <= negative / 2 or chosen == 0:
        return None, truncated, nodes
    parts: list[int] = []
    cursor = chosen
    guard = 0
    while cursor > 0 and which[cursor] >= 0 and guard <= stock:
        parts.append(items[which[cursor]][0])
        cursor = previous[cursor]
        guard += 1
    if guard > stock:
        truncated = True
    if not parts:
        return None, truncated, nodes
    ordered = tuple(sorted(parts))
    while ordered and not _fits(stock, ordered, kerf, kerf_mode):
        ordered = ordered[:-1]
    if not ordered:
        return None, truncated, nodes
    return ordered, truncated, nodes


def _merge_welds(columns: list[_WeldCol]) -> list[_WeldCol]:
    merged: dict[tuple[int, tuple[int, ...]], _WeldCol] = {}
    for column in columns:
        key = (column.pipe_index, column.parts)
        if key not in merged:
            merged[key] = _WeldCol(column.pipe_index, column.parts, set(column.sources))
        else:
            merged[key].sources |= column.sources
    return [merged[key] for key in sorted(merged)]


def _merge_cuts(columns: list[_CutCol]) -> list[_CutCol]:
    merged: dict[tuple[int, tuple[int, ...]], _CutCol] = {}
    for column in columns:
        key = (column.stock_length, column.parts)
        if key not in merged:
            merged[key] = _CutCol(column.stock_length, column.parts, set(column.sources))
        else:
            merged[key].sources |= column.sources
    return [merged[key] for key in sorted(merged)]


def _warm_start_columns(
    group: MaterialGroup, time_limit: float
) -> tuple[list[_WeldCol], list[_CutCol]]:
    try:
        result = _solve_group_fallback(group, max(0.05, time_limit))
    except Exception:
        return [], []
    index_of = {pipe.pipe_id: index for index, pipe in enumerate(group.pipes)}
    welds: list[_WeldCol] = []
    cuts: list[_CutCol] = []
    for row in result.get("welding_patterns", []):
        pipe_index = index_of.get(row.get("pipe_id"))
        if pipe_index is None:
            continue
        parts = tuple(int(part) for part in row.get("parts", []))
        if parts:
            welds.append(_WeldCol(pipe_index, parts, {"warm-start"}))
    for row in result.get("cutting_patterns", []):
        parts = tuple(sorted(int(part) for part in row.get("parts", [])))
        stock = int(row.get("stock_length", 0))
        if parts and stock > 0:
            cuts.append(_CutCol(stock, parts, {"warm-start"}))
    return welds, cuts


def _scip_quiet(model: Any, time_limit: float) -> None:
    model.hideOutput()
    model.setRealParam("limits/time", max(0.05, time_limit))
    try:
        model.setIntParam("parallel/maxnthreads", 1)
    except Exception:
        pass


def _lp_duals(
    group: MaterialGroup,
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    time_limit: float,
) -> dict[str, Any] | None:
    """Phase-1 LP duals.  Returns None if the dual vector cannot be read."""

    from pyscipopt import Model, SCIP_PARAMSETTING, quicksum

    if not welds or not cuts:
        return None
    model = Model("lexcg-lp")
    _scip_quiet(model, time_limit)
    model.setPresolve(SCIP_PARAMSETTING.OFF)
    model.setHeuristics(SCIP_PARAMSETTING.OFF)
    model.setSeparating(SCIP_PARAMSETTING.OFF)
    try:
        model.setIntParam("propagating/maxrounds", 0)
        model.setIntParam("propagating/maxroundsroot", 0)
    except Exception:
        pass
    penalty = group.stock_length + 1
    x_vars = [
        model.addVar(vtype="C", lb=0, name=f"x{index}") for index in range(len(welds))
    ]
    y_vars = [
        model.addVar(vtype="C", lb=0, name=f"y{index}") for index in range(len(cuts))
    ]
    slacks = [
        model.addVar(vtype="C", lb=0, obj=penalty, name=f"slack{index}")
        for index in range(len(group.pipes))
    ]
    by_pipe: dict[int, list[int]] = defaultdict(list)
    for index, column in enumerate(welds):
        by_pipe[column.pipe_index].append(index)
    demand_cons = []
    for pipe_index, pipe in enumerate(group.pipes):
        demand_cons.append(
            model.addCons(
                quicksum(x_vars[index] for index in by_pipe[pipe_index])
                + slacks[pipe_index]
                == pipe.demand,
                name=f"dem{pipe_index}",
            )
        )
    stock_cons = {}
    for stock in group.stocks:
        indexes = [
            index
            for index, column in enumerate(cuts)
            if column.stock_length == stock.length
        ]
        stock_cons[stock.length] = model.addCons(
            quicksum(y_vars[index] for index in indexes) <= stock.quantity,
            name=f"st{stock.length}",
        )
    segments = sorted({part for column in welds for part in column.parts})
    seg_cons = {}
    for segment in segments:
        produced = quicksum(
            column.parts.count(segment) * y_vars[index]
            for index, column in enumerate(cuts)
            if segment in column.parts
        )
        consumed = quicksum(
            column.parts.count(segment) * x_vars[index]
            for index, column in enumerate(welds)
            if segment in column.parts
        )
        short = model.addVar(vtype="C", lb=0, obj=penalty, name=f"short{segment}")
        seg_cons[segment] = model.addCons(
            produced - consumed + short == 0, name=f"seg{segment}"
        )
    used = quicksum(
        column.stock_length * y_vars[index] for index, column in enumerate(cuts)
    )
    model.setObjective(used, "minimize")
    model.optimize()
    if model.getNSols() <= 0 or model.getConss() is None:
        return None
    try:
        alpha = [float(model.getDualsolLinear(cons)) for cons in demand_cons]
        beta = {
            length: float(model.getDualsolLinear(cons))
            for length, cons in stock_cons.items()
        }
        pi = {
            segment: float(model.getDualsolLinear(cons))
            for segment, cons in seg_cons.items()
        }
    except Exception:
        return None
    return {
        "alpha": alpha,
        "beta": beta,
        "pi": pi,
        "lp_used": float(model.getObjVal()),
        "slack": sum(float(model.getVal(var)) for var in slacks),
    }


def _weld_reduced_cost(
    column: _WeldCol, duals: dict[str, Any], *, unknown_price: float
) -> float:
    pi: dict[int, float] = duals["pi"]
    alpha = float(duals["alpha"][column.pipe_index])
    cost = 0.0
    for part in column.parts:
        cost += float(pi.get(part, unknown_price))
    # obj coeff is 0 while pricing the material layer.
    return 0.0 - alpha + cost


def _cut_reduced_cost(column: _CutCol, duals: dict[str, Any]) -> float:
    pi: dict[int, float] = duals["pi"]
    beta = float(duals["beta"].get(column.stock_length, 0.0))
    cost = sum(float(pi.get(part, 0.0)) * count for part, count in Counter(column.parts).items())
    return float(column.stock_length) - beta - cost


def _price_welds(
    group: MaterialGroup,
    welds: list[_WeldCol],
    duals: dict[str, Any],
    budget: _Budget,
) -> list[_WeldCol]:
    """Best geometric patterns under the current segment duals.

    Unknown lengths are charged ``max(0, max pi)`` so a brand-new length is
    not treated as free.  At most a few negative-cost columns are returned.
    """

    pi: dict[int, float] = duals["pi"]
    unknown = max([0.0, *[float(value) for value in pi.values()]], default=0.0)
    stocks = [stock.length for stock in group.stocks]
    fresh: list[_WeldCol] = []
    existing = {(column.pipe_index, column.parts) for column in welds}
    for pipe_index, pipe in enumerate(group.pipes):
        if budget.expired:
            break
        patterns, _truncated = _seed_weld_patterns(
            pipe,
            stocks,
            kerf=group.blade_margin,
            min_weld=group.min_weld_distance,
            min_cut=group.min_cut_length,
            budget=budget,
        )
        ranked: list[tuple[float, tuple[int, ...]]] = []
        for parts in patterns:
            column = _WeldCol(pipe_index, parts, {"pricing"})
            score = _weld_reduced_cost(column, duals, unknown_price=unknown)
            if score < -RC_EPS and (pipe_index, parts) not in existing:
                ranked.append((score, parts))
        ranked.sort()
        for _score, parts in ranked[:3]:
            fresh.append(_WeldCol(pipe_index, parts, {"pricing"}))
            existing.add((pipe_index, parts))
    return fresh


def _price_cuts(
    group: MaterialGroup,
    cuts: list[_CutCol],
    welds: list[_WeldCol],
    duals: dict[str, Any],
    budget: _Budget,
) -> list[_CutCol]:
    """One knapsack pattern per stock.  Never one column per copy."""

    pi: dict[int, float] = duals["pi"]
    segments = sorted({part for column in welds for part in column.parts} | set(pi))
    if not segments:
        return []
    values = [float(pi.get(segment, 0.0)) for segment in segments]
    existing = {(column.stock_length, column.parts) for column in cuts}
    fresh: list[_CutCol] = []
    per_stock_nodes = max(1, budget.node_limit // max(1, len(group.stocks)))
    for stock in group.stocks:
        if budget.expired:
            break
        pattern, truncated, nodes = knapsack_best_pattern(
            stock.length,
            segments,
            values,
            kerf=group.blade_margin,
            kerf_mode=group.kerf_mode,
            node_budget=min(per_stock_nodes, max(1, budget.node_limit - budget.nodes)),
            deadline=budget.deadline,
        )
        budget.nodes += nodes
        if truncated:
            budget.truncated = True
        if pattern is None:
            continue
        column = _CutCol(stock.length, pattern, {"pricing"})
        if (column.stock_length, column.parts) in existing:
            continue
        if _cut_reduced_cost(column, duals) < -RC_EPS:
            fresh.append(column)
            existing.add((column.stock_length, column.parts))
    return fresh


def _lex_optimize(
    group: MaterialGroup,
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    deadline: float,
) -> dict[str, Any] | None:
    from pyscipopt import Model, quicksum

    if any(
        not any(column.pipe_index == index for column in welds)
        for index in range(len(group.pipes))
    ):
        return None
    if not cuts:
        return None
    model = Model("lexcg-ip")
    remaining = deadline - time.monotonic()
    if remaining <= 0.05:
        return None
    _scip_quiet(model, remaining)
    x_vars = [
        model.addVar(
            vtype="INTEGER",
            lb=0,
            ub=group.pipes[column.pipe_index].demand,
            name=f"x{index}",
        )
        for index, column in enumerate(welds)
    ]
    y_vars = [
        model.addVar(vtype="INTEGER", lb=0, name=f"y{index}")
        for index in range(len(cuts))
    ]
    by_pipe: dict[int, list[int]] = defaultdict(list)
    for index, column in enumerate(welds):
        by_pipe[column.pipe_index].append(index)
    for pipe_index, pipe in enumerate(group.pipes):
        model.addCons(
            quicksum(x_vars[index] for index in by_pipe[pipe_index]) == pipe.demand
        )
    quantity = {stock.length: stock.quantity for stock in group.stocks}
    usage_by_length: dict[int, Any] = {}
    for stock in group.stocks:
        indexes = [
            index
            for index, column in enumerate(cuts)
            if column.stock_length == stock.length
        ]
        expression = quicksum(y_vars[index] for index in indexes)
        model.addCons(expression <= stock.quantity)
        usage_by_length[stock.length] = expression
    must_use_total = sum(stock.must_use_quantity for stock in group.stocks)
    if must_use_total > 0:
        use_optional = model.addVar(vtype="BINARY", name="use_available_stock")
        used_must_terms = []
        for stock in group.stocks:
            available = stock.quantity - stock.must_use_quantity
            used_must = model.addVar(
                vtype="INTEGER",
                lb=0,
                ub=stock.must_use_quantity,
                name=f"mu{stock.length}",
            )
            used_optional = model.addVar(
                vtype="INTEGER",
                lb=0,
                ub=max(0, available),
                name=f"av{stock.length}",
            )
            model.addCons(used_must + used_optional == usage_by_length[stock.length])
            if available > 0:
                model.addCons(used_optional <= available * use_optional)
            else:
                model.addCons(used_optional == 0)
            used_must_terms.append(used_must)
        model.addCons(quicksum(used_must_terms) >= must_use_total * use_optional)
    weld_of: dict[tuple[int, ...], list[int]] = defaultdict(list)
    for index, column in enumerate(welds):
        weld_of[column.parts].append(index)
    weld_active = []
    for signature, indexes in weld_of.items():
        active = model.addVar(vtype="BINARY", name=f"zw{len(weld_active)}")
        weld_active.append(active)
        usage = quicksum(x_vars[index] for index in indexes)
        pipes = {welds[index].pipe_index for index in indexes}
        maximum = sum(group.pipes[index].demand for index in pipes)
        model.addCons(usage <= maximum * active)
        model.addCons(usage >= active)
        del signature
    cut_of: dict[tuple[int, tuple[int, ...]], list[int]] = defaultdict(list)
    for index, column in enumerate(cuts):
        cut_of[(column.stock_length, column.parts)].append(index)
    cut_active = []
    for signature, indexes in cut_of.items():
        active = model.addVar(vtype="BINARY", name=f"zc{len(cut_active)}")
        cut_active.append(active)
        usage = quicksum(y_vars[index] for index in indexes)
        model.addCons(usage <= quantity[signature[0]] * active)
        model.addCons(usage >= active)
    segments = sorted({part for column in welds for part in column.parts})
    for segment in segments:
        produced = quicksum(
            column.parts.count(segment) * y_vars[index]
            for index, column in enumerate(cuts)
            if segment in column.parts
        )
        consumed = quicksum(
            column.parts.count(segment) * x_vars[index]
            for index, column in enumerate(welds)
            if segment in column.parts
        )
        model.addCons(produced == consumed)
    used = quicksum(
        column.stock_length * y_vars[index] for index, column in enumerate(cuts)
    )
    joints = quicksum(
        column.joints * x_vars[index] for index, column in enumerate(welds)
    )
    objectives = (
        ("used_stock_length", used),
        ("welding_joint_quantity", joints),
        ("welding_pattern_type_quantity", quicksum(weld_active)),
        ("cutting_pattern_type_quantity", quicksum(cut_active)),
    )
    phases: list[dict[str, Any]] = []
    solution_x: list[int] | None = None
    solution_y: list[int] | None = None
    for name, expression in objectives:
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            phases.append(
                {"objective": name, "value": None, "mip_status": "TIME_LIMIT", "mip_optimal": False}
            )
            break
        model.setRealParam("limits/time", max(0.05, remaining))
        model.setObjective(expression, "minimize")
        model.optimize()
        status = str(model.getStatus()).lower()
        if model.getNSols() <= 0:
            phases.append(
                {"objective": name, "value": None, "mip_status": status, "mip_optimal": False}
            )
            break
        solution = model.getBestSol()
        solution_x = [int(round(model.getSolVal(solution, var))) for var in x_vars]
        solution_y = [int(round(model.getSolVal(solution, var))) for var in y_vars]
        value = int(round(model.getSolVal(solution, expression)))
        optimal = status == "optimal"
        phases.append(
            {
                "objective": name,
                "value": value,
                "mip_status": status,
                "mip_optimal": optimal,
            }
        )
        if not optimal:
            break
        model.freeTransform()
        model.addCons(expression <= value)
    if solution_x is None or solution_y is None:
        return None
    return {"x": solution_x, "y": solution_y, "phases": phases}


def _family_is_complete(group: MaterialGroup, seed_truncated: bool) -> bool:
    """True only when every pipe was exhaustively enumerated.

    That is realistic for a single weld on a short tube.  A joint cap below
    the input limit, or a budget stop, leaves the family incomplete.
    """

    if seed_truncated:
        return False
    for pipe in group.pipes:
        if pipe.max_joints > 1 or pipe.length > EXACT_LENGTH_LIMIT:
            return False
    return True


def _layer_rows(
    phases: list[dict[str, Any]],
    *,
    proved_pool: bool,
    pricing_truncated: bool,
) -> list[dict[str, Any]]:
    rows = []
    previous_proved = proved_pool and not pricing_truncated
    by_name = {phase["objective"]: phase for phase in phases}
    for name in LAYER_NAMES:
        phase = by_name.get(name)
        if phase is None or phase.get("value") is None:
            rows.append({"objective": name, "value": None, "status": "unproved"})
            previous_proved = False
            continue
        this_proved = previous_proved and bool(phase.get("mip_optimal"))
        rows.append(
            {
                "objective": name,
                "value": phase["value"],
                "status": "proved" if this_proved else "incumbent",
            }
        )
        previous_proved = this_proved
    return rows


def _empty_result(
    group: MaterialGroup,
    elapsed: float,
    *,
    proved_infeasible: bool,
    pricing_truncated: bool,
    reason: str,
) -> dict[str, Any]:
    result = _assemble_group_result(group, "lexcg", "UNSOLVED", [], [], elapsed)
    result["metrics"]["solve_status"] = "INFEASIBLE" if proved_infeasible else "UNSOLVED"
    result["metrics"]["infeasibility_proved"] = proved_infeasible
    result["metrics"]["pricing_truncated"] = pricing_truncated
    result["metrics"]["lexicographic_optimal"] = False
    result["metrics"]["core_lexicographic_optimal"] = False
    result["metrics"]["lexicographic_layers"] = [
        {"objective": name, "value": None, "status": "unproved"} for name in LAYER_NAMES
    ]
    result["metrics"]["result_source"] = "native"
    result["metrics"]["solver_path"] = "lexcg"
    if pricing_truncated:
        result["warnings"].append("PRICING_TRUNCATED")
    result["warnings"].append(f"LEXCG_UNSOLVED: {reason}")
    return result


def solve_group(
    group: MaterialGroup,
    time_limit: float,
    *,
    use_warm_start: bool = True,
    node_budget: int | None = None,
) -> dict[str, Any]:
    """Solve one material group.  Always returns a verifier-shaped group dict."""

    started = time.monotonic()
    deadline = started + max(time_limit, 0.05)
    seed_nodes = SEED_NODE_BUDGET if node_budget is None else node_budget
    cut_nodes = CUT_NODE_BUDGET if node_budget is None else node_budget
    price_nodes = PRICE_NODE_BUDGET if node_budget is None else node_budget
    seed_budget = _Budget(deadline, seed_nodes)
    stocks = [stock.length for stock in group.stocks]
    quantities = {stock.length: stock.quantity for stock in group.stocks}
    weld_columns: list[_WeldCol] = []
    seed_truncated = False
    for pipe_index, pipe in enumerate(group.pipes):
        patterns, truncated = _seed_weld_patterns(
            pipe,
            stocks,
            kerf=group.blade_margin,
            min_weld=group.min_weld_distance,
            min_cut=group.min_cut_length,
            budget=seed_budget,
        )
        seed_truncated = seed_truncated or truncated
        for parts in patterns:
            weld_columns.append(_WeldCol(pipe_index, parts, {"native-seed"}))
    segments = sorted({part for column in weld_columns for part in column.parts})
    cut_budget = _Budget(deadline, cut_nodes)
    cut_columns = [
        _CutCol(stock, parts, {"native-seed"})
        for stock, parts in _seed_cut_patterns(
            stocks,
            quantities,
            segments,
            kerf=group.blade_margin,
            kerf_mode=group.kerf_mode,
            budget=cut_budget,
            repeated_patterns=[column.parts for column in weld_columns],
        )
    ]
    pricing_truncated = seed_truncated or cut_budget.truncated
    if use_warm_start and time.monotonic() < deadline:
        # The deterministic allocator is a warm start only.  Large orders skip
        # it so a non-aborting heuristic cannot consume the whole budget.
        demand_pieces = sum(pipe.demand for pipe in group.pipes)
        if demand_pieces <= 400:
            warm_welds, warm_cuts = _warm_start_columns(
                group, min(2.0, max(0.05, deadline - time.monotonic()))
            )
            weld_columns.extend(warm_welds)
            cut_columns.extend(warm_cuts)
    weld_columns = _merge_welds(weld_columns)
    cut_columns = _merge_cuts(cut_columns)
    # One knapsack column per stock on the seeded alphabet.  Budgeted.
    if segments and time.monotonic() < deadline:
        pack_budget = _Budget(deadline, cut_nodes)
        for stock in group.stocks:
            if pack_budget.expired:
                pricing_truncated = True
                break
            pattern, truncated, nodes = knapsack_best_pattern(
                stock.length,
                segments,
                [float(segment) for segment in segments],
                kerf=group.blade_margin,
                kerf_mode=group.kerf_mode,
                node_budget=max(1, pack_budget.node_limit // max(1, len(group.stocks))),
                deadline=deadline,
            )
            pack_budget.nodes += nodes
            if truncated:
                pricing_truncated = True
            if pattern is not None:
                cut_columns.append(_CutCol(stock.length, pattern, {"pricing"}))
        cut_columns = _merge_cuts(cut_columns)
    # Dual-driven pricing rounds.  Each round is capped by the shared budget.
    price_budget = _Budget(deadline, price_nodes)
    for _round in range(3):
        if price_budget.expired or time.monotonic() > deadline - 0.4:
            pricing_truncated = True
            break
        duals = _lp_duals(
            group,
            weld_columns,
            cut_columns,
            min(1.5, max(0.05, deadline - time.monotonic())),
        )
        if duals is None:
            pricing_truncated = True
            break
        new_welds = _price_welds(group, weld_columns, duals, price_budget)
        new_cuts = _price_cuts(group, cut_columns, weld_columns, duals, price_budget)
        if price_budget.truncated:
            pricing_truncated = True
        if not new_welds and not new_cuts:
            break
        weld_columns = _merge_welds(weld_columns + new_welds)
        cut_columns = _merge_cuts(cut_columns + new_cuts)
        segments = sorted({part for column in weld_columns for part in column.parts})
    solved = None
    if time.monotonic() < deadline:
        solved = _lex_optimize(group, weld_columns, cut_columns, deadline)
    elapsed = time.monotonic() - started
    if solved is None:
        # Incomplete pricing is not a proof of infeasibility.  A complete
        # one-weld family plus a finished master can be.
        proved = (
            not pricing_truncated
            and _family_is_complete(group, seed_truncated)
            and time.monotonic() <= deadline
        )
        return _empty_result(
            group,
            elapsed,
            proved_infeasible=proved,
            pricing_truncated=pricing_truncated or seed_truncated,
            reason="no integer solution in the priced column pool",
        )
    weld_counts = [
        (
            _WeldCandidate(weld_columns[index].pipe_index, weld_columns[index].parts),
            solved["x"][index],
        )
        for index in range(len(weld_columns))
        if solved["x"][index] > 0
    ]
    cut_counts = [
        (
            _CutCandidate(
                cut_columns[index].stock_length,
                cut_columns[index].parts,
                group.blade_margin,
                group.kerf_mode,
            ),
            solved["y"][index],
        )
        for index in range(len(cut_columns))
        if solved["y"][index] > 0
    ]
    pool_complete = _family_is_complete(group, seed_truncated) and not pricing_truncated
    layers = _layer_rows(
        solved["phases"], proved_pool=pool_complete, pricing_truncated=pricing_truncated
    )
    all_proved = all(layer["status"] == "proved" for layer in layers)
    result = _assemble_group_result(
        group,
        "lexcg",
        "OPTIMAL" if all_proved else "FEASIBLE",
        weld_counts,
        cut_counts,
        elapsed,
    )
    used_warm = False
    for index, column in enumerate(weld_columns):
        if solved["x"][index] > 0 and column.sources == {"warm-start"}:
            used_warm = True
    for index, column in enumerate(cut_columns):
        if solved["y"][index] > 0 and column.sources == {"warm-start"}:
            used_warm = True
    source = "warm-start-assisted" if used_warm else "native"
    result["metrics"].update(
        {
            "solve_status": "OPTIMAL_LEXICOGRAPHIC" if all_proved else (
                "TARGET_REACHED" if result["metrics"]["target_reached"] else "FEASIBLE"
            ),
            "infeasibility_proved": False,
            "pricing_truncated": pricing_truncated,
            "lexicographic_layers": layers,
            "lexicographic_optimal": all_proved,
            "core_lexicographic_optimal": all_proved,
            "optimization_phases_completed": [
                layer["objective"] for layer in layers if layer["status"] == "proved"
            ],
            "optimization_phase_incomplete": next(
                (layer["objective"] for layer in layers if layer["status"] != "proved"),
                None,
            ),
            "result_source": source,
            "solver_path": "lexcg",
            "weld_column_count": len(weld_columns),
            "cut_column_count": len(cut_columns),
        }
    )
    if pricing_truncated:
        result["warnings"].append("PRICING_TRUNCATED")
    if not all_proved:
        result["warnings"].append("LEXICOGRAPHIC_INCUMBENT_NOT_PROVED")
    if used_warm:
        result["warnings"].append("WARM_START_COLUMNS_USED")
    result["column_provenance"] = {
        "weld_sources": sorted(
            {
                source_name
                for column in weld_columns
                for source_name in column.sources
            }
        ),
        "cut_sources": sorted(
            {
                source_name
                for column in cut_columns
                for source_name in column.sources
            }
        ),
        "result_source": source,
    }
    return result
