"""CP-SAT feasibility master for ``engine="stable"``.

The model chooses how many copies of each generated weld pattern to use and
how many times to use each cut pattern. Segment supply must meet segment
demand, and each stock length stays inside its inventory.

Patterns are generated from the order's own pipe lengths, stock lengths,
kerf, and forbidden intervals. This module does not branch on a case id,
a material-group id, or a segment length taken from a known order.

This is a restriction of the shop problem: an infeasible model only says the
catalogue and the cut family (identical pieces, pairs, one long piece plus
repeated shorts, and small triples) cannot express a plan. It is not a proof
that the order is impossible. A hit of the time limit stays unknown.
"""

from __future__ import annotations

import contextvars
import time
from collections import Counter, defaultdict

from .domain import MaterialGroup
from .engine_stable import (
    _ENFORCE_MUST,
    _Timeout,
    _acceptable,
    _emit,
    _split_piece,
    _windowed_two_piece,
)
from .solver import _CutCandidate

_NOTE: contextvars.ContextVar[str] = contextvars.ContextVar("stable_cpsat_note", default="")

# Weld patterns kept per pipe after the stock-aligned two-pieces.
_EXTRA_TWOS = 12
_EXTRA_LONG = 24
# Pieces on one bar in the arc-flow cut model, and the arc budget that
# keeps that model small enough to build. Both are catalogue limits, not
# order-specific lengths.
_MAX_PIECES = 6
_MAX_FLOW_ARCS = 80000
_MAX_COLUMNS = 90000


def cpsat_note() -> str:
    return _NOTE.get()


def solve_group_cpsat(group: MaterialGroup, deadline: float) -> dict | None:
    """Return one verifier-sized plan, or None if this restriction fails."""

    _NOTE.set("")
    if time.monotonic() >= deadline:
        raise _Timeout
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        _NOTE.set("CP-SAT 未运行：环境里没有 ortools")
        return None
    if _ENFORCE_MUST.get() and any(stock.must_use_quantity > 0 for stock in group.stocks):
        _NOTE.set("CP-SAT 跳过：本条打开了必用料，目录模型不执行该优先级")
        return None
    # Two-piece catalogue first. If that restriction is infeasible and time
    # remains, try the embed catalogue (remnant in front, middle, or back).
    for prefer in ("two", "long"):
        if time.monotonic() >= deadline - 0.4:
            _NOTE.set("CP-SAT 时限内未知")
            raise _Timeout
        catalogues = _catalogues(group, deadline, prefer=prefer)
        if any(not menu for menu in catalogues):
            _NOTE.set("CP-SAT 缺口：至少一根管子在目录里没有合法拼法")
            if prefer == "two":
                continue
            return None
        built = _build_model(group, catalogues, cp_model)
        if built is None:
            _NOTE.set("CP-SAT 缺口：弧流上限内没有留下每根管子都能用的拼法")
            continue
        model, pack = built
        solver = cp_model.CpSolver()
        remaining = deadline - time.monotonic()
        if remaining < 0.3:
            _NOTE.set("CP-SAT 时限内未知")
            raise _Timeout
        # Leave a slice for the embed pass when the two-piece pass fails fast.
        budget = remaining - 0.05
        if prefer == "two":
            budget = min(budget, max(1.5, remaining * 0.45))
        solver.parameters.max_time_in_seconds = budget
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 1
        status = solver.Solve(model)
        name = solver.StatusName(status)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            emitted = _read_solution(group, pack, solver, time.monotonic())
            if emitted is None:
                _NOTE.set("CP-SAT 找到了分配，但料段供需没有对上，结果没有采用")
                return None
            _NOTE.set("")
            return emitted
        if name == "INFEASIBLE":
            _NOTE.set(
                "CP-SAT 在拼法目录和弧流切法（每根料最多若干段）内不可行，"
                "没有证明订单本身不可行"
            )
            continue
        if time.monotonic() >= deadline - 0.05:
            _NOTE.set("CP-SAT 时限内未知")
            raise _Timeout
        _NOTE.set(f"CP-SAT 结束状态 {name}，时限内未知")
        return None
    return None


def _catalogues(
    group: MaterialGroup, deadline: float, *, prefer: str = "two"
) -> list[list[tuple[int, ...]]]:
    stocks = sorted({stock.length for stock in group.stocks})
    stock_set = set(stocks)
    kerf = group.blade_margin
    max_stock = max(stocks)
    seeds = _seeds(group, stocks, kerf, max_stock)
    menus: list[list[tuple[int, ...]]] = []
    for pipe in group.pipes:
        if time.monotonic() >= deadline:
            raise _Timeout
        found: list[tuple[int, ...]] = []
        seen: set[tuple[int, ...]] = set()

        def add(parts: tuple[int, ...] | None) -> None:
            if parts is None or parts in seen:
                return
            if _acceptable(pipe, group, parts):
                seen.add(parts)
                found.append(parts)

        if pipe.length <= max_stock:
            add((pipe.length,))
        for stock in stocks:
            if stock < pipe.length:
                add(_windowed_two_piece(pipe, group, stock, 1))
                add(_windowed_two_piece(pipe, group, pipe.length - stock, 1))
        for zone in pipe.forbidden:
            add(_windowed_two_piece(pipe, group, zone.start, 1))
            add(_windowed_two_piece(pipe, group, zone.end, 1))
        for host in stocks:
            for seed in seeds:
                base = host - kerf - seed
                if 0 < base < pipe.length:
                    add(_windowed_two_piece(pipe, group, base, 1))
                    add(_windowed_two_piece(pipe, group, pipe.length - base, 1))
        if pipe.max_joints >= 2 and pipe.length > max_stock:
            _add_embedded(pipe, group, stocks, seeds, kerf, max_stock, add)
            _add_splits(pipe, group, found, stocks, seeds, kerf, add)
        menus.append(_cap(found, stock_set))
    return _limit_lengths(menus, stock_set, prefer=prefer)


def _seeds(
    group: MaterialGroup, stocks: list[int], kerf: int, max_stock: int
) -> list[int]:
    protected = set(stocks)
    for pipe in group.pipes:
        if pipe.length <= max_stock:
            protected.add(pipe.length)
    remnants: set[int] = set()
    for pipe in group.pipes:
        for stock in stocks:
            remnant = pipe.length - stock
            if 0 < remnant <= max_stock:
                remnants.add(remnant)
    extra: set[int] = set()
    for host in stocks:
        for seed in protected | remnants:
            other = host - kerf - seed
            if 0 < other <= max_stock and other not in protected:
                extra.add(other)
    # Short complements are the pieces that share a bar. Keeping every
    # remnant makes the middle-embed loop too wide, so only the shortest
    # complements join the stock lengths and the whole-pipe lengths.
    chosen = set(protected)
    chosen.update(sorted(extra)[:80])
    return sorted(chosen)


def _add_embedded(pipe, group, stocks, seeds, kerf, max_stock, add) -> None:
    """One or two stock pieces, remnant as one piece or as a complement pair."""

    long_stocks = list(reversed(stocks[-8:])) if len(stocks) > 8 else list(stocks)
    for stock in long_stocks:
        if not 0 < stock < pipe.length:
            continue
        side = pipe.length - stock
        if 0 < side <= max_stock:
            add((stock, side))
            add((side, stock))
        if pipe.max_joints < 2:
            continue
        for host in stocks:
            for seed in seeds:
                left = host - kerf - seed
                for delta in (0, -1, 1):
                    first = left + delta
                    second = side - first
                    if min(first, second) <= 0 or max(first, second) > max_stock:
                        continue
                    pair = (first, second)
                    add(pair + (stock,))
                    add((stock,) + pair)
                    add((first, stock, second))
                    add((second, stock, first))
    if pipe.max_joints < 2:
        return
    for left in long_stocks:
        for right in long_stocks:
            used = left + right
            remnant = pipe.length - used
            if remnant <= 0 or remnant > max_stock:
                continue
            pieces = [left, right]
            for slot in range(3):
                add(tuple(pieces[:slot] + [remnant] + pieces[slot:]))
            if pipe.max_joints < 3 or pipe.length <= 2 * max_stock:
                continue
            for third in long_stocks[:4]:
                used3 = used + third
                remnant3 = pipe.length - used3
                if remnant3 <= 0 or remnant3 > max_stock:
                    continue
                pieces3 = [left, right, third]
                for slot in range(4):
                    add(tuple(pieces3[:slot] + [remnant3] + pieces3[slot:]))


def _add_splits(pipe, group, found, stocks, seeds, kerf, add) -> None:
    bases = [parts for parts in found if len(parts) == 2][:8]
    for base in bases:
        for index, piece in enumerate(base):
            if piece in stocks:
                continue
            for host in stocks:
                for seed in seeds:
                    guess = host - kerf - seed
                    if guess <= 0 or guess >= piece:
                        continue
                    add(_split_piece(pipe, group, base, index, guess))


def _cap(found: list[tuple[int, ...]], stock_set: set[int]) -> list[tuple[int, ...]]:
    wholes = [parts for parts in found if len(parts) == 1]
    aligned = [
        parts
        for parts in found
        if len(parts) == 2 and any(part in stock_set for part in parts)
    ]
    other_twos = [
        parts
        for parts in found
        if len(parts) == 2 and parts not in set(aligned)
    ][:_EXTRA_TWOS]
    longer = [parts for parts in found if len(parts) > 2][:_EXTRA_LONG]
    chosen: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()
    for parts in wholes + aligned + other_twos + longer:
        if parts not in seen:
            seen.add(parts)
            chosen.append(parts)
    return chosen


def _limit_lengths(
    menus: list[list[tuple[int, ...]]],
    stock_set: set[int],
    *,
    hard_cap: int = 72,
    prefer: str = "two",
) -> list[list[tuple[int, ...]]]:
    """Drop patterns that introduce rare lengths once the cut alphabet is wide.

    Stock-aligned two-pieces stay until ``hard_cap``. Longer embeds are only
    kept while the alphabet is still small, because each new length adds a
    row of pair-cut columns.
    """

    ranked: list[tuple[tuple[int, int, int], int, tuple[int, ...]]] = []
    for pipe_index, menu in enumerate(menus):
        for parts in menu:
            stock_pieces = sum(part in stock_set for part in parts)
            long_pattern = len(parts) >= 3 and stock_pieces > 0
            if prefer == "long" and long_pattern:
                rank = (0, -stock_pieces, len(parts))
            elif len(parts) == 1:
                rank = (0, 0, 0) if prefer == "two" else (1, 0, 0)
            elif len(parts) == 2 and stock_pieces:
                rank = (1, -stock_pieces, -min(parts))
            elif len(parts) == 2:
                rank = (2, -stock_pieces, len(parts))
            else:
                rank = (3, -stock_pieces, len(parts))
            ranked.append((rank, pipe_index, parts))
    ranked.sort(key=lambda item: item[0])
    chosen: list[list[tuple[int, ...]]] = [[] for _ in menus]
    seen: list[set[tuple[int, ...]]] = [set() for _ in menus]
    used: set[int] = set()
    for rank, pipe_index, parts in ranked:
        if parts in seen[pipe_index]:
            continue
        extra = {part for part in parts if part not in used}
        cap = hard_cap if rank[0] <= 1 else 48
        if extra and len(used) + len(extra) > cap:
            continue
        chosen[pipe_index].append(parts)
        seen[pipe_index].add(parts)
        used.update(parts)
    for pipe_index, menu in enumerate(menus):
        if not chosen[pipe_index] and menu:
            chosen[pipe_index].append(menu[0])
    return chosen


def _stock_graph(lengths: list[int], stock: int, kerf: int, mode: str):
    """Reachable arc-flow for one stock length.

    A node is ``(pieces, used)`` where ``used`` is the sum of piece lengths
    plus the kerf between them. Ending a bar is legal only when the same
    rule as ``_CutCandidate.remainder`` accepts that prefix.
    """

    use: list[tuple[int, int, int, int, int]] = []
    seen = {(0, 0)}
    layer = [(0, 0)]
    for _piece in range(_MAX_PIECES):
        nxt: list[tuple[int, int]] = []
        fresh: set[tuple[int, int]] = set()
        for count, used in layer:
            pay = kerf if count else 0
            for length in lengths:
                nxt_used = used + pay + length
                if nxt_used > stock:
                    continue
                state = (count + 1, nxt_used)
                use.append((count, used, length, state[0], state[1]))
                if state not in seen:
                    seen.add(state)
                    if state not in fresh:
                        fresh.add(state)
                        nxt.append(state)
        layer = nxt
        if not layer:
            break
    ends: list[tuple[int, int]] = []
    for count, used in seen:
        if count == 0:
            continue
        if mode == "WITH_REMAINDER" and used < stock and used + kerf > stock:
            continue
        ends.append((count, used))
    end_set = set(ends)
    outgoing = {(count, used) for count, used, _length, _ncount, _nused in use}
    use = [
        arc
        for arc in use
        if (arc[3], arc[4]) in end_set or (arc[3], arc[4]) in outgoing
    ]
    return use, ends


def _flow_arcs(lengths: list[int], stocks: list[int], kerf: int, mode: str) -> int:
    total = 0
    for stock in stocks:
        use, ends = _stock_graph(lengths, stock, kerf, mode)
        total += len(use) + len(ends)
        if total > _MAX_FLOW_ARCS:
            return total
    return total


def _trim_menus(menus, stocks: list[int], kerf: int, mode: str):
    """Keep the most common piece lengths whose arc-flow still fits."""

    freq: Counter[int] = Counter()
    for menu in menus:
        for parts in menu:
            freq.update(set(parts))
    ranked = sorted(freq, key=lambda length: (-freq[length], length))
    best_n = 0
    low, high = 1, len(ranked)
    while ranked and low <= high:
        mid = (low + high) // 2
        if _flow_arcs(ranked[:mid], stocks, kerf, mode) <= _MAX_FLOW_ARCS:
            best_n = mid
            low = mid + 1
        else:
            high = mid - 1
    allowed = set(ranked[:best_n])
    return [[parts for parts in menu if set(parts) <= allowed] for menu in menus]


def _cut_patterns(lengths: list[int], stocks: list[int], kerf: int, mode: str):
    """Explicit bars: identical pieces, pairs, and one piece plus repeats.

    This list is a restriction. Callers that would exceed ``_MAX_COLUMNS``
    use the arc-flow model instead of treating the overflow as infeasible.
    """

    patterns: list[tuple[int, Counter[int]]] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()

    def add(stock: int, parts: tuple[int, ...]) -> None:
        if any(part <= 0 for part in parts):
            return
        key = (stock, tuple(sorted(parts)))
        if key in seen:
            return
        if _CutCandidate(stock, key[1], kerf, mode).remainder < 0:
            return
        seen.add(key)
        patterns.append((stock, Counter(key[1])))
        if len(patterns) > _MAX_COLUMNS:
            raise _ColumnOverflow

    try:
        for index, left in enumerate(lengths):
            for copies in range(1, 7):
                placed = False
                blob = (left,) * copies
                for stock in stocks:
                    before = len(patterns)
                    add(stock, blob)
                    placed = placed or len(patterns) > before
                if not placed:
                    break
            for right in lengths[index + 1 :]:
                for stock in stocks:
                    add(stock, (left, right))
                for copies in range(2, 5):
                    placed = False
                    for stock in stocks:
                        before = len(patterns)
                        add(stock, (left,) + (right,) * copies)
                        add(stock, (right,) + (left,) * copies)
                        placed = placed or len(patterns) > before
                    if not placed:
                        break
        if len(lengths) <= 18 and len(patterns) < 70000:
            for i, first in enumerate(lengths):
                for j in range(i + 1, len(lengths)):
                    second = lengths[j]
                    for k in range(j + 1, len(lengths)):
                        third = lengths[k]
                        for stock in stocks:
                            add(stock, (first, second, third))
    except _ColumnOverflow:
        return None
    return patterns


class _ColumnOverflow(Exception):
    """The explicit cut list passed the fixed column budget."""


def _try_column_model(group, catalogues, cp_model, avail, stocks, kerf, mode):
    lengths = sorted({part for menu in catalogues for parts in menu for part in parts})
    patterns = _cut_patterns(lengths, stocks, kerf, mode)
    if not patterns:
        return None
    model = cp_model.CpModel()
    choice: list[list] = []
    for pipe_index, pipe in enumerate(group.pipes):
        row = []
        for pattern_index, _parts in enumerate(catalogues[pipe_index]):
            row.append(model.NewIntVar(0, pipe.demand, f"x{pipe_index}_{pattern_index}"))
        model.Add(sum(row) == pipe.demand)
        choice.append(row)
    demand_terms: dict[int, list] = {length: [] for length in lengths}
    for pipe_index, menu in enumerate(catalogues):
        for pattern_index, parts in enumerate(menu):
            for length, copies in Counter(parts).items():
                var = choice[pipe_index][pattern_index]
                demand_terms[length].append(var if copies == 1 else copies * var)
    usage = [
        model.NewIntVar(0, avail[stock], f"y{index}")
        for index, (stock, _recipe) in enumerate(patterns)
    ]
    for length in lengths:
        produced = []
        for var, (_stock, recipe) in zip(usage, patterns):
            copies = recipe.get(length, 0)
            if copies == 1:
                produced.append(var)
            elif copies > 1:
                produced.append(copies * var)
        if not produced:
            if demand_terms[length]:
                model.Add(sum(demand_terms[length]) == 0)
            continue
        model.Add(sum(demand_terms[length]) == sum(produced))
    by_stock: dict[int, list] = defaultdict(list)
    for var, (stock, _recipe) in zip(usage, patterns):
        by_stock[stock].append(var)
    for stock, terms in by_stock.items():
        model.Add(sum(terms) <= avail[stock])
    return model, ("columns", catalogues, patterns, usage, choice, kerf, mode)


def _build_model(group: MaterialGroup, catalogues, cp_model):
    kerf = group.blade_margin
    mode = group.kerf_mode
    avail: Counter[int] = Counter()
    for stock in group.stocks:
        avail[stock.length] += stock.quantity
    stocks = [length for length, qty in sorted(avail.items()) if qty > 0]
    columns = _try_column_model(group, catalogues, cp_model, avail, stocks, kerf, mode)
    if columns is not None:
        return columns
    trimmed = _trim_menus(catalogues, stocks, kerf, mode)
    if not trimmed or any(not menu for menu in trimmed):
        return None
    model = cp_model.CpModel()
    choice: list[list] = []
    for pipe_index, pipe in enumerate(group.pipes):
        row = []
        for pattern_index, _parts in enumerate(trimmed[pipe_index]):
            row.append(model.NewIntVar(0, pipe.demand, f"x{pipe_index}_{pattern_index}"))
        model.Add(sum(row) == pipe.demand)
        choice.append(row)
    lengths = sorted({part for menu in trimmed for parts in menu for part in parts})
    demand_terms: dict[int, list] = {length: [] for length in lengths}
    for pipe_index, menu in enumerate(trimmed):
        for pattern_index, parts in enumerate(menu):
            for length, copies in Counter(parts).items():
                var = choice[pipe_index][pattern_index]
                demand_terms[length].append(var if copies == 1 else copies * var)
    graphs = []
    produced: dict[int, list] = {length: [] for length in lengths}
    serial = 0
    for stock in stocks:
        use, ends = _stock_graph(lengths, stock, kerf, mode)
        use_vars = []
        inflow: dict[tuple[int, int], list] = defaultdict(list)
        outflow: dict[tuple[int, int], list] = defaultdict(list)
        for count, used, length, ncount, nused in use:
            var = model.NewIntVar(0, avail[stock], f"a{serial}")
            serial += 1
            use_vars.append(var)
            outflow[(count, used)].append(var)
            inflow[(ncount, nused)].append(var)
            produced[length].append(var)
        end_vars = {}
        for count, used in ends:
            var = model.NewIntVar(0, avail[stock], f"e{serial}")
            serial += 1
            end_vars[(count, used)] = var
        nodes = set(outflow) | set(inflow) | set(end_vars)
        for node in nodes:
            if node == (0, 0):
                continue
            incoming = inflow.get(node, [])
            outgoing = list(outflow.get(node, []))
            end_var = end_vars.get(node)
            if end_var is not None:
                outgoing.append(end_var)
            if incoming and outgoing:
                model.Add(sum(incoming) == sum(outgoing))
            elif incoming:
                model.Add(sum(incoming) == 0)
        started = outflow.get((0, 0), [])
        if not started:
            continue
        model.Add(sum(started) <= avail[stock])
        graphs.append((stock, use, use_vars, end_vars))
    if not graphs:
        return None
    for length, terms in demand_terms.items():
        made = produced[length]
        if not made:
            if terms:
                model.Add(sum(terms) == 0)
            continue
        model.Add(sum(terms) == sum(made))
    return model, ("arc", trimmed, graphs, choice, kerf, mode)


def _decompose(use, use_vars, end_vars, solver) -> list[tuple[tuple[int, ...], int]] | None:
    flows = [solver.Value(var) for var in use_vars]
    end_flow = {node: solver.Value(var) for node, var in end_vars.items()}
    adj: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (count, used, _length, _ncount, _nused) in enumerate(use):
        if flows[index] > 0:
            adj[(count, used)].append(index)

    def search(node: tuple[int, int]) -> tuple[list[int], tuple[int, int]] | None:
        if node != (0, 0) and end_flow.get(node, 0) > 0:
            return [], node
        for index in adj.get(node, []):
            if flows[index] <= 0:
                continue
            _count, _used, _length, ncount, nused = use[index]
            found = search((ncount, nused))
            if found is not None:
                arc_ids, end = found
                return [index, *arc_ids], end
        return None

    patterns: list[tuple[tuple[int, ...], int]] = []
    while True:
        found = search((0, 0))
        if found is None:
            break
        arc_ids, end = found
        qty = end_flow.get(end, 0)
        for index in arc_ids:
            qty = min(qty, flows[index])
        if qty <= 0:
            return None
        for index in arc_ids:
            flows[index] -= qty
        end_flow[end] = end_flow.get(end, 0) - qty
        parts = tuple(sorted(use[index][2] for index in arc_ids))
        patterns.append((parts, qty))
    if any(qty > 0 for qty in flows) or any(qty > 0 for qty in end_flow.values()):
        return None
    return patterns


def _read_solution(group, pack, solver, started: float):
    if pack[0] == "columns":
        _kind, catalogues, patterns, usage, choice, kerf, mode = pack
        weld: Counter[tuple[int, tuple[int, ...]]] = Counter()
        for pipe_index, menu in enumerate(catalogues):
            for pattern_index, parts in enumerate(menu):
                qty = solver.Value(choice[pipe_index][pattern_index])
                if qty > 0:
                    weld[(pipe_index, parts)] += qty
        merged: Counter[tuple[int, tuple[int, ...]]] = Counter()
        for var, (stock, recipe) in zip(usage, patterns):
            qty = solver.Value(var)
            if qty > 0:
                merged[(stock, tuple(sorted(recipe.elements())))] += qty
        cuts = [
            (_CutCandidate(stock, parts, kerf, mode), qty)
            for (stock, parts), qty in merged.items()
            if qty > 0
        ]
        return _emit(group, weld, cuts, started)
    _kind, trimmed, graphs, choice, kerf, mode = pack
    weld: Counter[tuple[int, tuple[int, ...]]] = Counter()
    for pipe_index, menu in enumerate(trimmed):
        for pattern_index, parts in enumerate(menu):
            qty = solver.Value(choice[pipe_index][pattern_index])
            if qty > 0:
                weld[(pipe_index, parts)] += qty
    merged: Counter[tuple[int, tuple[int, ...]]] = Counter()
    for stock, use, use_vars, end_vars in graphs:
        decomposed = _decompose(use, use_vars, end_vars, solver)
        if decomposed is None:
            return None
        for parts, qty in decomposed:
            if qty > 0:
                merged[(stock, parts)] += qty
    cuts = [
        (_CutCandidate(stock, parts, kerf, mode), qty)
        for (stock, parts), qty in merged.items()
        if qty > 0
    ]
    return _emit(group, weld, cuts, started)
