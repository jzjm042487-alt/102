"""On-demand feasibility search for ``engine="stable"``.

This stage runs only after the existing strategy chain and the catalogue
CP-SAT stage both return no plan. It does not read those stages' clocks.

Weld positions are not free integers. Each pipe gets a discrete set: a few
interior samples, the forbidden-zone endpoints nudged by at most 1 mm, and
stock-boundary welds. Pricing then adds a few complement lengths that exact-fill
a stock with a length already in the alphabet. Three-piece patterns are
generated only after two-piece pricing adds nothing.

The alphabet, the number of rounds, the patterns per pipe, and the cut-column
list are capped so a pool of hundreds of segment lengths cannot be enumerated.
An infeasible restricted master is a gap in that bounded family, not a proof
that the order is impossible. A hit of the deadline stays unknown.

The module does not branch on a case id, a material-group id, or a segment
length taken from a known order. Must-use stock is not modelled; callers that
turned the priority on skip this stage.
"""

from __future__ import annotations

import contextvars
import time
from collections import Counter

from .domain import MaterialGroup, PipeDemand
from .engine_stable import (
    _ENFORCE_MUST,
    _Timeout,
    _acceptable,
    _emit,
    _min_part,
    _snap_weld,
    _split_piece,
    _uniform_parts,
    _windowed_two_piece,
)
from .solver import _CutCandidate

_NOTE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "stable_ondemand_note", default=""
)

# Size limits chosen before the pilot sample was run. They exist so the
# search cannot expand into an unconstrained fragment pool.
_MAX_ALPHABET = 16
_MAX_ROUNDS = 5
_NEW_LENGTHS = 3
_MAX_PATTERNS = 16
_COARSE_STEPS = 4
_FINE_STEPS = 12
_COARSE_TWOS = 8
_FINE_TWOS = 12
_GRID_RADIUS = 40
_COMPLEMENT_WINDOW = 1
_COMPLEMENT_KEEP = 48
_MAX_ZONES = 32
_MAX_STOCK_ANCHORS = 24
_COLUMN_CAP = 12000
_MASTER_CAP_SECONDS = 8.0
_STOP_REMAINING = 0.4
_MAX_PIECES_ON_BAR = 4


def ondemand_note() -> str:
    return _NOTE.get()


def reset_ondemand_note() -> None:
    _NOTE.set("")


def mark_ondemand_timeout() -> None:
    _NOTE.set("按需生成时限内未知")


def solve_group_ondemand(group: MaterialGroup, deadline: float) -> dict | None:
    """Return one verifier-sized plan, or None if this restriction fails."""

    _NOTE.set("")
    if time.monotonic() >= deadline:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout
    try:
        return _search(group, deadline)
    except _Timeout:
        if not _NOTE.get():
            _NOTE.set("按需生成时限内未知")
        raise
    except Exception as exc:
        _NOTE.set(f"按需生成中断：{type(exc).__name__}: {exc}"[:180])
        return None


def _search(group: MaterialGroup, deadline: float) -> dict | None:
    if _ENFORCE_MUST.get() and any(stock.must_use_quantity > 0 for stock in group.stocks):
        _NOTE.set("按需生成跳过：本条打开了必用料，可行性模型不执行该优先级")
        return None
    if not group.stocks or not group.pipes:
        _NOTE.set("按需生成缺口：没有管子或没有定尺")
        return None
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        _NOTE.set("按需生成未运行：环境里没有 ortools")
        return None

    fine = _small_group(group)
    menus = _seed_menus(group, fine=fine)
    tried_longer = False
    for round_index in range(1, _MAX_ROUNDS + 1):
        _ensure_time(deadline)
        if any(not menu for menu in menus):
            status, plan = "infeasible", None
        else:
            status, plan = _solve_master(group, menus, deadline, cp_model)
            if plan is not None:
                _NOTE.set("")
                return plan
            if status == "unknown":
                menus = _shrink_menus(menus)
                _ensure_time(deadline)
                status, plan = _solve_master(group, menus, deadline, cp_model)
                if plan is not None:
                    _NOTE.set("")
                    return plan
                if status != "infeasible":
                    _NOTE.set("按需生成时限内未知")
                    raise _Timeout
        if status == "broken":
            return None
        added = _price_two_pieces(group, menus, deadline)
        if added:
            continue
        if not tried_longer:
            tried_longer = True
            if _add_longer_patterns(group, menus, deadline):
                continue
        _NOTE.set(_gap_text(round_index, fine))
        return None
    _NOTE.set(_gap_text(_MAX_ROUNDS, fine))
    return None


def _gap_text(rounds: int, fine: bool) -> str:
    grid = "更密" if fine else "粗"
    return (
        "按需生成缺口：定价在字母表、轮数和切法列上限内没有再生成能让主问题可行的拼法。"
        f"限制为段长种类不超过{_MAX_ALPHABET}、最多{_MAX_ROUNDS}轮、"
        f"每轮最多{_NEW_LENGTHS}种新段长、每管最多{_MAX_PATTERNS}个拼法、"
        f"切法列不超过{_COLUMN_CAP}，焊口用{grid}离散网格、禁焊区端点外移 1mm "
        "和定尺互补，不是连续整数位置。"
        f"已完成{rounds}轮。这不是订单不可行的证明。"
    )


def _ensure_time(deadline: float) -> None:
    if deadline - time.monotonic() < _STOP_REMAINING:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout


def _small_group(group: MaterialGroup) -> bool:
    """Finer weld grid only when the master stays small.

    A free integer weld variable is not used on this path either: the extra
    candidates are still the discrete grid, just with more sample points.
    """

    demand = sum(pipe.demand for pipe in group.pipes)
    stocks = len({stock.length for stock in group.stocks})
    joints = max((pipe.max_joints for pipe in group.pipes), default=0)
    return len(group.pipes) <= 3 and demand <= 6 and stocks <= 4 and joints <= 3


def _avail(group: MaterialGroup) -> list[int]:
    counts: Counter[int] = Counter()
    for stock in group.stocks:
        if stock.quantity > 0 and stock.length > 0:
            counts[stock.length] += stock.quantity
    return [length for length, _qty in sorted(counts.items())]


def _seed_menus(group: MaterialGroup, *, fine: bool) -> list[list[tuple[int, ...]]]:
    steps = _FINE_STEPS if fine else _COARSE_STEPS
    two_cap = _FINE_TWOS if fine else _COARSE_TWOS
    stocks = _avail(group)
    menus: list[list[tuple[int, ...]]] = []
    for pipe in group.pipes:
        found: list[tuple[int, ...]] = []
        seen: set[tuple[int, ...]] = set()

        def add(parts: tuple[int, ...] | None) -> None:
            if parts is None or parts in seen:
                return
            if _acceptable(pipe, group, parts):
                seen.add(parts)
                found.append(parts)

        max_stock = max(stocks) if stocks else 0
        whole = (pipe.length,) if pipe.length <= max_stock else None
        add(whole)
        for weld in _grid_welds(pipe, group, stocks, steps):
            add(_two_from_weld(pipe, group, weld))
        for zone in pipe.forbidden[:_MAX_ZONES]:
            add(_windowed_two_piece(pipe, group, zone.start - 1, _COMPLEMENT_WINDOW))
            add(_windowed_two_piece(pipe, group, zone.end + 1, _COMPLEMENT_WINDOW))
        for stock in _anchor_stocks(stocks, pipe.length):
            add(_windowed_two_piece(pipe, group, stock, _COMPLEMENT_WINDOW))
            add(_windowed_two_piece(pipe, group, pipe.length - stock, _COMPLEMENT_WINDOW))
        menus.append(_keep(found, whole, two_cap, set(stocks)))
    return _limit_alphabet(menus)


def _keep(
    found: list[tuple[int, ...]],
    whole: tuple[int, ...] | None,
    cap: int,
    stock_lengths: set[int],
) -> list[tuple[int, ...]]:
    """Keep wholes and non-stock welds ahead of stock-boundary welds.

    Stock-boundary two-pieces are already the catalogue stage's preference.
    Filling this stage's pattern cap with them would repeat that gap.
    """

    def sort_key(parts: tuple[int, ...]) -> tuple:
        aligned = sum(1 for part in parts if part in stock_lengths)
        return (aligned, len(parts), parts)

    ranked = sorted(found, key=sort_key)
    kept: list[tuple[int, ...]] = []
    if whole is not None and whole in found:
        kept.append(whole)
    for parts in ranked:
        if parts in kept:
            continue
        kept.append(parts)
        if len(kept) >= cap:
            break
    return kept[:_MAX_PATTERNS]


def _grid_welds(
    pipe: PipeDemand, group: MaterialGroup, stocks: list[int], steps: int
) -> list[int]:
    if pipe.max_joints < 1 or steps < 1 or not stocks:
        return []
    margin = _min_part(group, multi=True)
    max_stock = max(stocks)
    low = max(margin, pipe.length - max_stock)
    high = min(pipe.length - margin, max_stock)
    if low > high:
        return []
    span = high - low
    welds: list[int] = []
    seen: set[int] = set()
    for index in range(steps):
        center = low if span == 0 else low + (span * (index + 1)) // (steps + 1)
        lo = max(low, center - _GRID_RADIUS)
        hi = min(high, center + _GRID_RADIUS)
        weld = _snap_weld(pipe, center, lo, hi)
        if weld is not None and weld not in seen:
            seen.add(weld)
            welds.append(weld)
    return welds


def _two_from_weld(
    pipe: PipeDemand, group: MaterialGroup, weld: int
) -> tuple[int, ...] | None:
    return _windowed_two_piece(pipe, group, weld, 0)


def _anchor_stocks(stocks: list[int], pipe_length: int) -> list[int]:
    usable = [stock for stock in stocks if 0 < stock < pipe_length]
    if len(usable) <= _MAX_STOCK_ANCHORS:
        return usable
    step = len(usable) / _MAX_STOCK_ANCHORS
    return [usable[int(index * step)] for index in range(_MAX_STOCK_ANCHORS)]


def _limit_alphabet(
    menus: list[list[tuple[int, ...]]],
) -> list[list[tuple[int, ...]]]:
    """Keep at most ``_MAX_ALPHABET`` distinct piece lengths.

    Patterns are admitted one pipe at a time so the first pipe cannot fill
    the alphabet alone. A pattern that would add an extra length after the
    cap is skipped; the pipe keeps any earlier pattern that already fit.
    """

    chosen: list[list[tuple[int, ...]]] = [[] for _ in menus]
    used: set[int] = set()
    progress = True
    while progress:
        progress = False
        for pipe_index, menu in enumerate(menus):
            if len(chosen[pipe_index]) >= _MAX_PATTERNS:
                continue
            for parts in menu:
                if parts in chosen[pipe_index]:
                    continue
                extra = {part for part in parts if part not in used}
                if extra and len(used) + len(extra) > _MAX_ALPHABET:
                    continue
                chosen[pipe_index].append(parts)
                used.update(parts)
                progress = True
                break
    return chosen


def _alphabet(menus: list[list[tuple[int, ...]]]) -> set[int]:
    return {part for menu in menus for parts in menu for part in parts}


def _price_two_pieces(
    group: MaterialGroup,
    menus: list[list[tuple[int, ...]]],
    deadline: float,
) -> bool:
    """Add complement welds after the restricted master is infeasible.

    The witness is that infeasibility. Each candidate length is the exact
    partner of a length already used, on one of this order's own stocks.
    """

    _ensure_time(deadline)
    alphabet = _alphabet(menus)
    stocks = _avail(group)
    kerf = group.blade_margin
    partners = alphabet or set(stocks)
    scores: Counter[int] = Counter()
    for stock in stocks:
        for partner in partners:
            piece = stock - kerf - partner
            if piece > 0:
                scores[piece] += 1
    if not scores:
        return False
    shortlist = [piece for piece, _score in scores.most_common(_COMPLEMENT_KEEP)]
    proposals: list[tuple[int, int, int, tuple[int, ...]]] = []
    for pipe_index, pipe in enumerate(group.pipes):
        _ensure_time(deadline)
        existing = set(menus[pipe_index])
        empty_bonus = 1 if not existing else 0
        for piece in shortlist:
            for target in (piece, pipe.length - piece):
                parts = _windowed_two_piece(pipe, group, target, _COMPLEMENT_WINDOW)
                if parts is None or parts in existing:
                    continue
                new = {part for part in parts if part not in alphabet}
                if len(alphabet) + len(new) > _MAX_ALPHABET:
                    continue
                proposals.append((scores[piece] + empty_bonus, -len(new), pipe_index, parts))
    proposals.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    added_lengths: set[int] = set()
    added = False
    seen: set[tuple[int, tuple[int, ...]]] = set()
    for _score, _prefer, pipe_index, parts in proposals:
        if (pipe_index, parts) in seen:
            continue
        if len(menus[pipe_index]) >= _MAX_PATTERNS:
            continue
        new = {part for part in parts if part not in alphabet}
        if len(added_lengths | new) > _NEW_LENGTHS and new:
            continue
        if len(alphabet) + len(added_lengths | new) > _MAX_ALPHABET:
            continue
        menus[pipe_index].append(parts)
        seen.add((pipe_index, parts))
        added_lengths |= new
        added = True
    return added


def _add_longer_patterns(
    group: MaterialGroup,
    menus: list[list[tuple[int, ...]]],
    deadline: float,
) -> bool:
    """Three- and four-piece patterns once two-piece pricing has stalled."""

    added = False
    alphabet = _alphabet(menus)
    stocks = _avail(group)
    kerf = group.blade_margin
    for pipe_index, pipe in enumerate(group.pipes):
        _ensure_time(deadline)
        if pipe.max_joints < 2 or len(menus[pipe_index]) >= _MAX_PATTERNS:
            continue
        existing = set(menus[pipe_index])
        for pieces in (3, 4):
            if pieces - 1 > pipe.max_joints:
                continue
            uniform = _uniform_parts(pipe, group, pieces)
            if uniform is not None and uniform not in existing and _fits_alphabet(uniform, alphabet):
                menus[pipe_index].append(uniform)
                existing.add(uniform)
                alphabet.update(uniform)
                added = True
                if len(menus[pipe_index]) >= _MAX_PATTERNS:
                    break
        bases = [parts for parts in menus[pipe_index] if len(parts) == 2][:4]
        for base in bases:
            if len(menus[pipe_index]) >= _MAX_PATTERNS:
                break
            longer = 0 if base[0] >= base[1] else 1
            piece = base[longer]
            targets = [piece // 2]
            for partner in list(alphabet)[:_MAX_ALPHABET]:
                for stock in stocks[:_MAX_STOCK_ANCHORS]:
                    complement = stock - kerf - partner
                    if 0 < complement < piece:
                        targets.append(complement)
                    if len(targets) >= 6:
                        break
                if len(targets) >= 6:
                    break
            for target in targets[:6]:
                rebuilt = _split_piece(pipe, group, base, longer, target)
                if rebuilt is None or rebuilt in existing:
                    continue
                if not _fits_alphabet(rebuilt, alphabet):
                    continue
                menus[pipe_index].append(rebuilt)
                existing.add(rebuilt)
                alphabet.update(rebuilt)
                added = True
                if len(menus[pipe_index]) >= _MAX_PATTERNS:
                    break
    return added


def _fits_alphabet(parts: tuple[int, ...], alphabet: set[int]) -> bool:
    extra = {part for part in parts if part not in alphabet}
    return len(alphabet) + len(extra) <= _MAX_ALPHABET


def _shrink_menus(menus: list[list[tuple[int, ...]]]) -> list[list[tuple[int, ...]]]:
    freq: Counter[int] = Counter()
    for menu in menus:
        for parts in menu:
            freq.update(parts)
    if not freq:
        return menus
    ranked = sorted(freq, key=lambda length: (-freq[length], length))
    allowed = set(ranked[: max(1, len(ranked) // 2)])
    shrunk: list[list[tuple[int, ...]]] = []
    for menu in menus:
        kept = [parts for parts in menu if set(parts) <= allowed]
        if not kept and menu:
            kept = [max(menu, key=lambda parts: sum(freq[part] for part in parts))]
        shrunk.append(kept[:_MAX_PATTERNS])
    return shrunk


def _solve_master(group, menus, deadline: float, cp_model):
    remaining = deadline - time.monotonic()
    if remaining < _STOP_REMAINING:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout
    slice_seconds = min(_MASTER_CAP_SECONDS, max(0.2, remaining * 0.45), remaining - 0.05)
    if slice_seconds < 0.2:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout
    lengths = sorted(_alphabet(menus))
    if not lengths or any(not menu for menu in menus):
        return "infeasible", None
    stocks = _avail(group)
    kerf = group.blade_margin
    mode = group.kerf_mode
    patterns = _cut_columns(lengths, stocks, kerf, mode, deadline)
    if not patterns:
        return "infeasible", None
    model = cp_model.CpModel()
    choice: list[list] = []
    for pipe_index, pipe in enumerate(group.pipes):
        row = []
        for pattern_index, _parts in enumerate(menus[pipe_index]):
            row.append(model.NewIntVar(0, pipe.demand, f"w{pipe_index}_{pattern_index}"))
        model.Add(sum(row) == pipe.demand)
        choice.append(row)
    demand_terms: dict[int, list] = {length: [] for length in lengths}
    for pipe_index, menu in enumerate(menus):
        for pattern_index, parts in enumerate(menu):
            variable = choice[pipe_index][pattern_index]
            for length, copies in Counter(parts).items():
                demand_terms[length].append(variable if copies == 1 else copies * variable)
    avail: Counter[int] = Counter()
    for stock in group.stocks:
        avail[stock.length] += max(stock.quantity, 0)
    usage = []
    for index, (stock, _parts) in enumerate(patterns):
        usage.append(model.NewIntVar(0, avail[stock], f"c{index}"))
    for length in lengths:
        produced = []
        for variable, (_stock, parts) in zip(usage, patterns):
            copies = parts.count(length)
            if copies == 1:
                produced.append(variable)
            elif copies > 1:
                produced.append(copies * variable)
        if not produced:
            if demand_terms[length]:
                model.Add(sum(demand_terms[length]) == 0)
            continue
        consumed = demand_terms[length]
        if not consumed:
            model.Add(sum(produced) == 0)
            continue
        model.Add(sum(consumed) == sum(produced))
    by_stock: dict[int, list] = {}
    for variable, (stock, _parts) in zip(usage, patterns):
        by_stock.setdefault(stock, []).append(variable)
    for stock, terms in by_stock.items():
        model.Add(sum(terms) <= avail[stock])
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = slice_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 1
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        emitted = _read(group, menus, patterns, usage, choice, solver, kerf, mode)
        if emitted is None:
            _NOTE.set("按需生成找到了分配，但料段供需没有对上，结果没有采用")
            return "broken", None
        return "feasible", emitted
    if status == cp_model.INFEASIBLE:
        return "infeasible", None
    return "unknown", None


def _cut_columns(lengths: list[int], stocks: list[int], kerf: int, mode: str, deadline: float):
    patterns: list[tuple[int, tuple[int, ...]]] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()

    def add(stock: int, parts: tuple[int, ...], *, exact_only: bool) -> bool:
        if len(patterns) >= _COLUMN_CAP:
            return False
        canonical = tuple(sorted(parts))
        key = (stock, canonical)
        if key in seen:
            return True
        if sum(canonical) + kerf * max(0, len(canonical) - 1) > stock:
            return True
        candidate = _CutCandidate(stock, canonical, kerf, mode)
        if candidate.remainder < 0:
            return True
        if exact_only and candidate.remainder != 0:
            return True
        seen.add(key)
        patterns.append((stock, canonical))
        return len(patterns) < _COLUMN_CAP

    for exact_only in (True, False):
        for stock in stocks:
            if time.monotonic() >= deadline:
                _NOTE.set("按需生成时限内未知")
                raise _Timeout
            for length in lengths:
                for copies in range(1, _MAX_PIECES_ON_BAR + 1):
                    if not add(stock, (length,) * copies, exact_only=exact_only):
                        return patterns
            for index, left in enumerate(lengths):
                for right in lengths[index + 1 :]:
                    if not add(stock, (left, right), exact_only=exact_only):
                        return patterns
                    for copies in (2, 3):
                        if copies + 1 > _MAX_PIECES_ON_BAR:
                            continue
                        if not add(stock, (left,) + (right,) * copies, exact_only=exact_only):
                            return patterns
                        if not add(stock, (right,) + (left,) * copies, exact_only=exact_only):
                            return patterns
            if len(lengths) <= 10:
                for i, first in enumerate(lengths):
                    for j in range(i + 1, len(lengths)):
                        second = lengths[j]
                        for k in range(j + 1, len(lengths)):
                            if not add(
                                stock,
                                (first, second, lengths[k]),
                                exact_only=exact_only,
                            ):
                                return patterns
    return patterns


def _read(group, menus, patterns, usage, choice, solver, kerf: int, mode: str):
    weld: Counter[tuple[int, tuple[int, ...]]] = Counter()
    for pipe_index, menu in enumerate(menus):
        for pattern_index, parts in enumerate(menu):
            quantity = solver.Value(choice[pipe_index][pattern_index])
            if quantity > 0:
                weld[(pipe_index, parts)] += quantity
    cuts = []
    for variable, (stock, parts) in zip(usage, patterns):
        quantity = solver.Value(variable)
        if quantity > 0:
            cuts.append((_CutCandidate(stock, parts, kerf, mode), quantity))
    return _emit(group, weld, cuts, time.monotonic())
