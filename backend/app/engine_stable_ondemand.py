"""On-demand feasibility search for ``engine="stable"``.

This stage runs only after the existing strategy chain and the catalogue
CP-SAT stage both return no plan. It does not read those stages' clocks.

Weld positions are not free integers. Each pipe gets a discrete set: a few
interior samples, the forbidden-zone endpoints nudged by at most 1 mm, and
stock-boundary welds. Pricing then adds a few complement lengths that
exact-fill a stock with a length already in the alphabet. Three-piece
patterns are generated only after two-piece pricing adds nothing.

When that ordinary restricted master is infeasible and a pipe is longer than
every stock, a second master sees splits of that pipe into pieces taken from
the stock lengths and those lengths minus a few kerfs, on welds the forbidden
zones still allow. Those columns are not written back, so later ordinary
tiers keep the pattern family that already solved earlier cases.

The alphabet, the number of new lengths per round, the patterns per pipe, and
the cut-column list start small and double when the restricted master is
infeasible. An infeasible restricted master is a reason to enlarge that
family, not a proof that the order is impossible. The search returns when it
finds a plan or the deadline is too close for another tier. A hit of the
deadline stays unknown.

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
    _peel_pattern,
    _snap_weld,
    _split_piece,
    _uniform_parts,
    _windowed_two_piece,
)
from .solver import _CutCandidate

_NOTE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "stable_ondemand_note", default=""
)
_LOG: contextvars.ContextVar[str] = contextvars.ContextVar(
    "stable_ondemand_tier_log", default=""
)
_TIER: contextvars.ContextVar[int] = contextvars.ContextVar(
    "stable_ondemand_tier", default=0
)

# First tier. Later tiers double the alphabet. The reservoir stays finite so
# a group with hundreds of fragment lengths is still not enumerated whole.
_MAX_ALPHABET = 16
_NEW_LENGTHS = 4
_MAX_PATTERNS = 16
_COARSE_STEPS = 4
_FINE_STEPS = 12
_GRID_RADIUS = 40
_COMPLEMENT_WINDOW = 1
_WINDOWS = (1, 8, 40)
_COMPLEMENT_KEEP = 48
_MAX_ZONES = 32
_MAX_STOCK_ANCHORS = 24
_COLUMN_CAP = 12000
_MASTER_CAP_SECONDS = 8.0
_STOP_REMAINING = 0.4
_MAX_PIECES_ON_BAR = 4
_POOL_LIMIT = 512
_LOG_KEEP = 24
_NEAR_STOCK_KEEP = 8
_NEAR_STOCK_BRANCH = 4
_NEAR_STOCK_NODES = 400


def _tier() -> int:
    return max(0, _TIER.get())


def _alphabet_cap() -> int:
    return _MAX_ALPHABET << _tier()


def _new_lengths() -> int:
    table = (_NEW_LENGTHS, 8, 16, 24, 32)
    tier = _tier()
    if tier < len(table):
        return table[tier]
    return 32 + 8 * (tier - len(table) + 1)


def _pattern_cap() -> int:
    return min(64, _MAX_PATTERNS + 8 * _tier())


def _column_cap() -> int:
    table = (_COLUMN_CAP, 24000, 48000, 80000, 100000)
    tier = _tier()
    if tier < len(table):
        return table[tier]
    return table[-1]


def _pieces_cap() -> int:
    return min(6, _MAX_PIECES_ON_BAR + _tier() // 2)


def _pool_cap() -> int:
    return min(_POOL_LIMIT, max(64, _alphabet_cap()))


def _master_cap() -> float:
    return min(12.0, _MASTER_CAP_SECONDS + 2.0 * _tier())


def _sample_steps() -> int:
    return 4 + 2 * _tier()


def ondemand_note() -> str:
    return _NOTE.get()


def ondemand_tier_log() -> str:
    return _LOG.get()


def reset_ondemand_note() -> None:
    _NOTE.set("")
    _LOG.set("")


def mark_ondemand_timeout() -> None:
    _NOTE.set("按需生成时限内未知")


def solve_group_ondemand(group: MaterialGroup, deadline: float) -> dict | None:
    """Return one verifier-sized plan, or None if this restriction fails."""

    _NOTE.set("")
    _LOG.set("")
    _TIER.set(0)
    try:
        if time.monotonic() >= deadline:
            _NOTE.set("按需生成时限内未知")
            _LOG.set("timeout")
            raise _Timeout
        return _search(group, deadline)
    except _Timeout:
        if not _NOTE.get():
            _NOTE.set("按需生成时限内未知")
        log = _LOG.get()
        if log and not log.endswith(":timeout"):
            _LOG.set(log + ":timeout")
        elif not log:
            _LOG.set("timeout")
        raise
    except Exception as exc:
        _NOTE.set(f"按需生成中断：{type(exc).__name__}: {exc}"[:180])
        return None
    finally:
        _TIER.set(0)


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

    origin = time.monotonic()
    log: list[str] = []
    fine = _small_group(group)
    pools = _seed_menus(group, fine=fine)
    stock_lengths = set(_avail(group))
    tier = 0
    idle = 0
    prev_admitted = 0
    unknown_streak = 0
    last_missing = 0
    try:
        while True:
            _TIER.set(tier)
            _ensure_time(deadline)
            menus = _limit_alphabet([list(item) for item in pools], stock_lengths)
            dropped: set[int] = set()
            window = _WINDOWS[min(tier, len(_WINDOWS) - 1)]
            before = sum(len(menu) for menu in pools)
            status, plan, missing = _expand_once(
                group,
                menus,
                pools,
                deadline,
                cp_model,
                window,
                dropped,
                stock_lengths,
            )
            last_missing = len(missing)
            if plan is not None:
                _stamp(log, origin, solved=True)
                _NOTE.set("")
                return plan
            if status == "broken":
                _stamp(log, origin, solved=False)
                return None
            admitted = len(_alphabet(menus))
            after = sum(len(menu) for menu in pools)
            if after == before and admitted <= prev_admitted:
                idle += 1
            else:
                idle = 0
            prev_admitted = admitted
            if status == "unknown":
                unknown_streak += 1
            else:
                unknown_streak = 0
            _stamp(log, origin, solved=False)
            remaining = deadline - time.monotonic()
            reservoir = len(_alphabet(pools))
            cap_binding = reservoir >= _alphabet_cap()
            empty = any(not menu for menu in menus)
            fine_enough = _sample_steps() >= (80 if empty else 48)
            if (
                status == "infeasible"
                and idle >= 2
                and not cap_binding
                and fine_enough
            ):
                _NOTE.set(_gap_text(fine, empty, last_missing, reservoir))
                return None
            if status == "unknown" and (unknown_streak >= 2 or remaining < 2.0):
                if remaining >= _STOP_REMAINING:
                    status, plan, missing, _weak = _solve_master(
                        group,
                        menus,
                        deadline,
                        cp_model,
                        slice_cap=max(0.2, remaining - 0.05),
                    )
                    last_missing = len(missing)
                    if plan is not None:
                        _stamp(log, origin, solved=True)
                        _NOTE.set("")
                        return plan
                if status != "infeasible":
                    _NOTE.set("按需生成时限内未知")
                    raise _Timeout
            if remaining < _STOP_REMAINING + 0.35:
                if status == "unknown":
                    _NOTE.set("按需生成时限内未知")
                    raise _Timeout
                _NOTE.set(_gap_text(fine, empty, last_missing, reservoir))
                return None
            tier += 1
    except _Timeout:
        if not log or not log[-1].startswith(f"{_alphabet_cap()}@"):
            _stamp(log, origin, solved=False)
        raise


def _augmented_near_stock_plan(
    group: MaterialGroup,
    pools: list[list[tuple[int, ...]]],
    deadline: float,
    cp_model,
    stock_lengths: set[int],
) -> dict | None:
    """Re-select patterns after the ordinary master is infeasible.

    Long pipes prefer pieces on the stock menu, and the near-stock splits are
    visible to that selection. Nothing is written back to ``pools``, so the
    ordinary tiers keep the pattern family that already solved earlier cases.
    """

    if not stock_lengths or deadline - time.monotonic() < 0.8:
        return None
    max_stock = max(stock_lengths)
    long = {
        index
        for index, pipe in enumerate(group.pipes)
        if pipe.length > max_stock and pipe.max_joints >= 1
    }
    if not long:
        return None
    copied = [list(menu) for menu in pools]
    for index in long:
        have = set(copied[index])
        for parts in _near_stock_patterns(group.pipes[index], group):
            if parts not in have:
                copied[index].append(parts)
                have.add(parts)
    limited = _limit_alphabet(
        copied,
        stock_lengths,
        long,
        set(_near_stock_menu(group)),
    )
    if any(not menu for menu in limited):
        return None
    slice_cap = 14.0 if _alphabet_cap() >= 64 else 4.0
    try:
        _status, plan, _missing, _weak = _solve_master(
            group, limited, deadline, cp_model, slice_cap=slice_cap
        )
    except _Timeout:
        return None
    return plan


def _expand_once(
    group: MaterialGroup,
    menus: list[list[tuple[int, ...]]],
    pools: list[list[tuple[int, ...]]],
    deadline: float,
    cp_model,
    window: int,
    dropped: set[int],
    stock_lengths: set[int],
) -> tuple[str, dict | None, list[int]]:
    """Solve once, price from the witness, then solve the enlarged master."""

    introduced: set[int] = set()
    _cover_empty_pipes(
        group, menus, pools, deadline, introduced, dropped, stock_lengths
    )
    _reuse_patterns(group, menus, deadline)
    _sync_pools(pools, menus, stock_lengths)
    status, plan, missing, weak = "infeasible", None, [], []
    if all(menus):
        status, plan, missing, weak = _finish_master(
            group, menus, deadline, cp_model
        )
        if plan is not None or status in ("broken", "unknown"):
            return status, plan, missing
        extra = _augmented_near_stock_plan(
            group, pools, deadline, cp_model, stock_lengths
        )
        if extra is not None:
            return "feasible", extra, []
    if missing:
        _drop_unsourcable(menus, set(missing))
        dropped.update(missing)
    introduced = set()
    focus = _focus_lengths(weak or missing, group)
    added = _price_two_pieces(
        group,
        menus,
        deadline,
        window=window,
        introduced=introduced,
        dropped=dropped,
        focus=focus,
        pools=pools,
        stock_lengths=stock_lengths,
    )
    longer = _add_longer_patterns(
        group,
        menus,
        deadline,
        introduced=introduced,
        dropped=dropped,
        pools=pools,
        stock_lengths=stock_lengths,
    )
    covered = _cover_empty_pipes(
        group, menus, pools, deadline, introduced, dropped, stock_lengths
    )
    _sync_pools(pools, menus, stock_lengths)
    if (added or longer or covered) and all(menus):
        status, plan, missing, _weak = _solve_master(
            group, menus, deadline, cp_model
        )
    elif not all(menus):
        status = "infeasible"
    return status, plan, missing


def _finish_master(group, menus, deadline: float, cp_model):
    status, plan, missing, weak = _solve_master(group, menus, deadline, cp_model)
    if plan is not None or status in ("broken", "infeasible"):
        return status, plan, missing, weak
    shrunk = _shrink_menus(menus)
    _ensure_time(deadline)
    status, plan, missing, weak = _solve_master(group, shrunk, deadline, cp_model)
    if plan is not None or status == "broken":
        return status, plan, missing, weak
    if deadline - time.monotonic() < 2.0:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout
    # The unshrunk model did not finish. One longer slice of that same
    # restriction; a failure of the shrunk model is not a proof.
    return _solve_master(
        group,
        menus,
        deadline,
        cp_model,
        slice_cap=min(20.0, max(0.2, deadline - time.monotonic() - 0.05)),
    )


def _stamp(log: list[str], origin: float, *, solved: bool) -> None:
    elapsed = max(0.0, time.monotonic() - origin)
    item = f"{_alphabet_cap()}@{elapsed:.2f}"
    if solved:
        item += ":solved"
    if len(log) < _LOG_KEEP:
        log.append(item)
    elif log[-1].startswith("+") and not solved:
        log[-1] = "+" + item
    else:
        log.append("+" + item)
    _LOG.set(",".join(log))


def _gap_text(fine: bool, empty: bool, missing: int, reservoir: int) -> str:
    grid = "更密" if fine else "粗"
    if empty:
        cover = "仍有管子没有拼法。"
    else:
        cover = "每根管子都有拼法，受限主问题仍不可行。"
    lack = f"最后一档有{missing}种段长没有任何切法。" if missing else ""
    record = ondemand_tier_log()
    if len(record) > 160:
        record = record[:160] + "…"
    return (
        f"按需生成缺口：字母表从{_MAX_ALPHABET}起逐档翻倍，停在{_alphabet_cap()}，"
        f"池内不同段长{reservoir}。"
        f"本档每轮新段长不超过{_new_lengths()}，每管拼法不超过{_pattern_cap()}，"
        f"切法列不超过{_column_cap()}。{cover}{lack}"
        f"焊口用{grid}离散网格并随档加密，不是连续整数。"
        "更低档不可行后已自动放大。这不是订单不可行的证明。"
        f"档位：{record}。"
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


def _near_stock_menu(group: MaterialGroup) -> list[int]:
    """Piece lengths that fill a stock, or fill it after a few kerfs.

    The menu is derived from this order's stocks and blade width. It is the
    same for every pipe in the group.
    """

    stocks = _avail(group)
    if not stocks:
        return []
    margin = _min_part(group, multi=True)
    max_stock = max(stocks)
    kerf = max(0, group.blade_margin)
    found: set[int] = set()
    for stock in stocks:
        steps = 1 if kerf == 0 else 4
        for count in range(steps):
            piece = stock - count * kerf
            if margin <= piece <= max_stock:
                found.add(piece)
    return sorted(found, reverse=True)


def _piece_waste(length: int, stocks: list[int], kerf: int, mode: str) -> int:
    best: int | None = None
    for stock in stocks:
        if length > stock:
            continue
        candidate = _CutCandidate(stock, (length,), kerf, mode)
        if candidate.remainder < 0:
            continue
        if best is None or candidate.remainder < best:
            best = candidate.remainder
    return 10**9 if best is None else best


def _near_stock_patterns(
    pipe: PipeDemand, group: MaterialGroup
) -> list[tuple[int, ...]]:
    """Split a pipe that is longer than every stock into near-stock pieces.

    Each internal weld sits on a stock length or a stock length minus a few
    kerfs, and only where welding is allowed. A forbidden zone drops that
    candidate; the leftover piece may be shorter than the menu. Existing
    stock peels are kept as extra seeds when the menu itself has no legal
    weld. Pipes that fit on one stock are left untouched.
    """

    stocks = _avail(group)
    if not stocks or pipe.length <= max(stocks):
        return []
    max_stock = max(stocks)
    margin = _min_part(group, multi=True)
    need = (pipe.length + max_stock - 1) // max_stock
    if need < 2 or need - 1 > pipe.max_joints:
        return []
    upper = min(pipe.max_joints + 1, need + 2, 6)
    menu = _near_stock_menu(group)
    menu_set = set(menu)
    kerf = group.blade_margin
    mode = group.kerf_mode
    found: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()
    nodes = 0

    def emit(parts: tuple[int, ...] | None) -> None:
        if parts is None or parts in seen or len(found) >= 24:
            return
        if _acceptable(pipe, group, parts):
            seen.add(parts)
            found.append(parts)

    def search(cursor: int, left: int, parts: list[int]) -> None:
        nonlocal nodes
        if nodes >= _NEAR_STOCK_NODES or len(found) >= 24:
            return
        nodes += 1
        remain = pipe.length - cursor
        if left == 1:
            if margin <= remain <= max_stock:
                emit(tuple([*parts, remain]))
            return
        min_rest = margin * (left - 1)
        max_rest = max_stock * (left - 1)
        options: list[tuple[int, int, int]] = []
        for length in menu:
            if length < margin or length > max_stock:
                continue
            rest = remain - length
            if rest < min_rest or rest > max_rest:
                continue
            if not pipe.weld_allowed(cursor + length):
                continue
            # A piece between two welds must clear the minimum weld distance.
            if parts and left > 1 and length < group.min_weld_distance:
                continue
            options.append((_piece_waste(length, stocks, kerf, mode), -length, length))
        options.sort()
        picked: list[int] = []
        for _waste, _neg, length in options:
            if length in picked:
                continue
            picked.append(length)
            if len(picked) >= _NEAR_STOCK_BRANCH:
                break
        for length in picked:
            parts.append(length)
            search(cursor + length, left - 1, parts)
            parts.pop()

    for pieces in range(need, upper + 1):
        search(0, pieces, [])
    for longest in (True, False):
        for early in (True, False):
            emit(_peel_pattern(pipe, group, longest_first=longest, finish_early=early))

    def rank(parts: tuple[int, ...]) -> tuple:
        off_menu = sum(1 for part in parts if part not in menu_set)
        total = sum(_piece_waste(part, stocks, kerf, mode) for part in parts)
        return (off_menu, len(parts), total, parts)

    found.sort(key=rank)
    return found[:_NEAR_STOCK_KEEP]


def _seed_menus(group: MaterialGroup, *, fine: bool) -> list[list[tuple[int, ...]]]:
    steps = _FINE_STEPS if fine else _COARSE_STEPS
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
        if max_stock and pipe.length > max_stock:
            need = (pipe.length + max_stock - 1) // max_stock
            upper = min(pipe.max_joints + 1, max(need, 2) + 2, 5)
            for pieces in range(max(need, 3), upper + 1):
                add(_uniform_parts(pipe, group, pieces))
                for stock in _anchor_stocks(stocks, pipe.length):
                    add(_stock_peel(pipe, group, stock, pieces))
            for parts in _sample_multi(pipe, group, _sample_steps(), time.monotonic() + 0.05):
                add(parts)
        menus.append(_keep(found, whole, set(stocks), _pool_cap()))
    return menus


def _keep(
    found: list[tuple[int, ...]],
    whole: tuple[int, ...] | None,
    stock_lengths: set[int],
    limit: int,
) -> list[tuple[int, ...]]:
    """Keep wholes and non-stock welds ahead of stock-boundary welds.

    Stock-boundary two-pieces are already the catalogue stage's preference.
    Filling this stage's pattern cap with them would repeat that gap.
    """

    def sort_key(parts: tuple[int, ...]) -> tuple:
        aligned = sum(1 for part in parts if part in stock_lengths)
        return (aligned, len(parts), parts)

    multis = [parts for parts in found if len(parts) >= 3][: max(4, limit // 4)]
    ranked = sorted((parts for parts in found if len(parts) < 3), key=sort_key)
    kept: list[tuple[int, ...]] = []
    if whole is not None and whole in found:
        kept.append(whole)
    for parts in multis:
        if parts not in kept:
            kept.append(parts)
    for parts in ranked:
        if parts in kept:
            continue
        kept.append(parts)
        if len(kept) >= limit:
            break
    return kept[:limit]


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
    stock_lengths: set[int],
    long_pipes: set[int] | None = None,
    menu_lengths: set[int] | None = None,
) -> list[list[tuple[int, ...]]]:
    """Keep at most the current tier's alphabet of distinct piece lengths.

    A pipe with no pattern is served before a pipe that already has one, and
    a pattern that reuses lengths already chosen is preferred. Otherwise the
    first pipes fill the alphabet and later pipes never reach the master.
    Pipes longer than every stock prefer pieces that sit on the near-stock
    menu; shorter pipes keep the previous ranking.
    """

    long = long_pipes or set()
    menu = menu_lengths or stock_lengths
    chosen: list[list[tuple[int, ...]]] = [[] for _ in menus]
    used: set[int] = set()
    while True:
        best: tuple | None = None
        for pipe_index, menu_row in enumerate(menus):
            if len(chosen[pipe_index]) >= _pattern_cap():
                continue
            for parts in menu_row:
                if parts in chosen[pipe_index]:
                    continue
                extra = {part for part in parts if part not in used}
                if len(used) + len(extra) > _alphabet_cap():
                    continue
                aligned = sum(1 for part in parts if part in stock_lengths)
                if pipe_index in long and len(parts) >= 2:
                    align_key = -sum(1 for part in parts if part in menu)
                else:
                    align_key = aligned
                cand = (
                    0 if not chosen[pipe_index] else 1,
                    len(extra),
                    align_key,
                    len(parts),
                    pipe_index,
                    parts,
                )
                if best is None or cand < best:
                    best = cand
        if best is None:
            break
        pipe_index = best[-2]
        parts = best[-1]
        chosen[pipe_index].append(parts)
        used.update(parts)
    return chosen


def _stock_peel(
    pipe: PipeDemand, group: MaterialGroup, stock: int, pieces: int
) -> tuple[int, ...] | None:
    """Place ``pieces - 1`` welds near multiples of one stock length."""

    if pieces < 2 or pieces - 1 > pipe.max_joints or stock <= 0:
        return None
    margin = _min_part(group, multi=True)
    max_stock = max(item.length for item in group.stocks)
    positions: list[int] = []
    cursor = 0
    for index in range(pieces - 1):
        target = cursor + stock
        if target <= cursor or target >= pipe.length:
            return None
        later = pieces - index - 1
        hi = min(pipe.length - margin * later, target + _GRID_RADIUS, max_stock + cursor)
        lo = max(cursor + margin, target - _GRID_RADIUS)
        if lo > hi:
            return None
        weld = _snap_weld(pipe, target, lo, hi)
        if weld is None or weld <= cursor:
            return None
        positions.append(weld)
        cursor = weld
    parts: list[int] = []
    previous = 0
    for position in positions:
        parts.append(position - previous)
        previous = position
    parts.append(pipe.length - previous)
    result = tuple(parts)
    if max(result) > max_stock:
        return None
    return result if _acceptable(pipe, group, result) else None


def _alphabet(menus: list[list[tuple[int, ...]]]) -> set[int]:
    return {part for menu in menus for parts in menu for part in parts}


def _reuse_patterns(
    group: MaterialGroup,
    menus: list[list[tuple[int, ...]]],
    deadline: float,
) -> bool:
    """Add welds whose every piece is already in the alphabet."""

    alphabet = _alphabet(menus)
    if not alphabet:
        return False
    added = False
    ordered = tuple(sorted(alphabet))
    if len(ordered) > 48:
        stride = max(1, len(ordered) // 48)
        middles = ordered[::stride]
    else:
        middles = ordered
    checks = 0
    for pipe_index, pipe in enumerate(group.pipes):
        _ensure_time(deadline)
        existing = set(menus[pipe_index])
        if pipe.max_joints >= 1:
            for left in ordered:
                if len(menus[pipe_index]) >= _pattern_cap():
                    break
                right = pipe.length - left
                if right not in alphabet or right <= 0:
                    continue
                parts = _windowed_two_piece(pipe, group, left, _COMPLEMENT_WINDOW)
                if parts is None or parts in existing or not set(parts) <= alphabet:
                    continue
                menus[pipe_index].append(parts)
                existing.add(parts)
                added = True
        if pipe.max_joints < 2:
            continue
        for left in middles:
            if len(menus[pipe_index]) >= _pattern_cap():
                break
            for middle in middles:
                checks += 1
                if checks % 32 == 0:
                    _ensure_time(deadline)
                tail = pipe.length - left - middle
                if tail not in alphabet or tail <= 0:
                    continue
                parts = _chain_parts(pipe, group, (left, middle, tail))
                if parts is None or parts in existing:
                    continue
                menus[pipe_index].append(parts)
                existing.add(parts)
                added = True
                if len(menus[pipe_index]) >= _pattern_cap():
                    break
    return added


def _chain_parts(
    pipe: PipeDemand, group: MaterialGroup, parts: tuple[int, ...]
) -> tuple[int, ...] | None:
    if sum(parts) != pipe.length:
        return None
    for order in (parts, tuple(reversed(parts))):
        if _acceptable(pipe, group, order):
            return order
    return None


def _plan_drops(
    menus: list[list[tuple[int, ...]]],
    needed: int,
    banned: set[int],
    allow_empty: set[int],
) -> list[int] | None:
    """Lengths that can be removed without emptying a protected pipe."""

    if needed <= 0:
        return []
    freq: Counter[int] = Counter()
    for menu in menus:
        for parts in menu:
            freq.update(set(parts))
    order = sorted((length for length in freq if length not in banned), key=lambda n: (freq[n], n))
    chosen: list[int] = []
    simulated = [list(menu) for menu in menus]
    for length in order:
        if len(chosen) >= needed:
            break
        nxt: list[list[tuple[int, ...]]] = []
        ok = True
        for index, menu in enumerate(simulated):
            kept = [parts for parts in menu if length not in parts]
            if menu and not kept and index not in allow_empty:
                ok = False
                break
            nxt.append(kept)
        if not ok:
            continue
        simulated = nxt
        chosen.append(length)
    if len(chosen) < needed:
        return None
    return chosen


def _apply_drops(menus: list[list[tuple[int, ...]]], victims: list[int]) -> None:
    banned = set(victims)
    for index, menu in enumerate(menus):
        menus[index] = [parts for parts in menu if banned.isdisjoint(parts)]


def _admit(
    menus: list[list[tuple[int, ...]]],
    pipe_index: int,
    parts: tuple[int, ...],
    introduced: set[int],
    dropped: set[int],
) -> bool:
    """Add one pattern, dropping rare lengths when the alphabet is full."""

    if parts in menus[pipe_index] or len(menus[pipe_index]) >= _pattern_cap():
        return False
    alphabet = _alphabet(menus)
    fresh = {part for part in set(parts) if part not in alphabet}
    if fresh & dropped:
        return False
    new_slots = fresh - introduced
    pipe_empty = not menus[pipe_index]
    if new_slots and not (pipe_empty and not introduced and len(fresh) <= 4):
        if len(introduced) + len(new_slots) > _new_lengths():
            return False
    if len(alphabet) + len(fresh) > _alphabet_cap():
        need = len(alphabet) + len(fresh) - _alphabet_cap()
        victims = _plan_drops(
            menus,
            need,
            banned=introduced | fresh,
            allow_empty={pipe_index} if pipe_empty else set(),
        )
        if victims is None:
            return False
        _apply_drops(menus, victims)
        dropped.update(victims)
    if parts in menus[pipe_index] or len(menus[pipe_index]) >= _pattern_cap():
        return False
    menus[pipe_index].append(parts)
    introduced.update(fresh)
    return True


def _remember(
    pools: list[list[tuple[int, ...]]],
    index: int,
    parts: tuple[int, ...],
    stock_lengths: set[int],
) -> bool:
    """Keep a rejected pattern so a later, larger tier can admit it."""

    bucket = pools[index]
    if parts in bucket:
        return False
    if len(bucket) < _pool_cap():
        bucket.append(parts)
        return True
    have = {part for menu in pools for cand in menu for part in cand}
    fresh = [part for part in parts if part not in have]
    if not fresh:
        return False
    for pos, cand in enumerate(bucket):
        if not set(cand).isdisjoint(fresh):
            continue
        if all(part in stock_lengths for part in cand) or set(cand) < have:
            bucket.pop(pos)
            bucket.append(parts)
            return True
    return False


def _sync_pools(
    pools: list[list[tuple[int, ...]]],
    menus: list[list[tuple[int, ...]]],
    stock_lengths: set[int],
) -> None:
    for index, menu in enumerate(menus):
        for parts in menu:
            _remember(pools, index, parts, stock_lengths)


def _drop_unsourcable(
    menus: list[list[tuple[int, ...]]], missing: set[int]
) -> bool:
    """Drop welds whose segment length has no cut on any stock."""

    if not missing:
        return False
    changed = False
    for index, menu in enumerate(menus):
        kept = [parts for parts in menu if missing.isdisjoint(parts)]
        if len(kept) != len(menu):
            menus[index] = kept
            changed = True
    return changed


def _focus_lengths(weak: list[int], group: MaterialGroup) -> set[int]:
    """Complements of lengths that still have no exact-fill cut."""

    stocks = _avail(group)
    kerf = group.blade_margin
    found: set[int] = set()
    for length in weak:
        for stock in stocks:
            piece = stock - kerf - length
            if piece > 0:
                found.add(piece)
            if len(found) >= 64:
                return found
    return found


def _cover_empty_pipes(
    group: MaterialGroup,
    menus: list[list[tuple[int, ...]]],
    pools: list[list[tuple[int, ...]]],
    deadline: float,
    introduced: set[int],
    dropped: set[int],
    stock_lengths: set[int],
) -> bool:
    """Patterns for pipes the current alphabet still does not cover."""

    added = False
    stocks = _avail(group)
    max_stock = max(stocks) if stocks else 0
    steps = _sample_steps()
    for pipe_index, pipe in enumerate(group.pipes):
        _ensure_time(deadline)
        if menus[pipe_index]:
            continue
        existing = set(menus[pipe_index])
        candidates: list[tuple[int, ...]] = []
        if max_stock and pipe.length > max_stock and pipe.max_joints >= 1:
            need = (pipe.length + max_stock - 1) // max_stock
            upper = min(pipe.max_joints + 1, need + 1 + _tier(), 8)
            for pieces in range(max(need, 2), upper + 1):
                uniform = _uniform_parts(pipe, group, pieces)
                if uniform is not None:
                    candidates.append(uniform)
                for stock in _anchor_stocks(stocks, pipe.length):
                    peeled = _stock_peel(pipe, group, stock, pieces)
                    if peeled is not None:
                        candidates.append(peeled)
        for parts in _sample_multi(pipe, group, steps, deadline):
            candidates.append(parts)
        for parts in candidates:
            _remember(pools, pipe_index, parts, stock_lengths)
            if parts in existing:
                continue
            if _admit(menus, pipe_index, parts, introduced, dropped):
                existing.add(parts)
                added = True
            if len(menus[pipe_index]) >= _pattern_cap():
                break
    return added


def _snap_chain(
    pipe: PipeDemand, group: MaterialGroup, parts: tuple[int, ...]
) -> tuple[int, ...] | None:
    if len(parts) < 2:
        return None
    max_stock = max(stock.length for stock in group.stocks)
    margin = _min_part(group, multi=True)
    positions: list[int] = []
    cursor = 0
    for part in parts[:-1]:
        target = cursor + part
        lo = max(cursor + margin, target - _GRID_RADIUS)
        hi = min(pipe.length - margin, cursor + max_stock, target + _GRID_RADIUS)
        if lo > hi:
            return None
        weld = _snap_weld(pipe, target, lo, hi)
        if weld is None or weld <= cursor:
            return None
        positions.append(weld)
        cursor = weld
    built: list[int] = []
    previous = 0
    for position in positions:
        built.append(position - previous)
        previous = position
    built.append(pipe.length - previous)
    result = tuple(built)
    if min(result) <= 0 or max(result) > max_stock:
        return None
    return result if _acceptable(pipe, group, result) else None


def _sample_multi(
    pipe: PipeDemand, group: MaterialGroup, steps: int, deadline: float
) -> list[tuple[int, ...]]:
    """Discrete multi-piece welds. Each piece stays within one stock.

    The grid gets finer as the tier index grows. Nominal positions are snapped
    onto the same allowed weld set used everywhere else; this is not a free
    integer weld.
    """

    if pipe.max_joints < 2 or steps < 2 or time.monotonic() >= deadline:
        return []
    stocks = [stock.length for stock in group.stocks if stock.length > 0 and stock.quantity > 0]
    if not stocks:
        return []
    max_stock = max(stocks)
    margin = _min_part(group, multi=True)
    found: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()

    def push(nominal: tuple[int, ...]) -> bool:
        if time.monotonic() >= deadline or len(found) >= 48:
            return False
        snapped = _snap_chain(pipe, group, nominal)
        if snapped is None or snapped in seen:
            return True
        seen.add(snapped)
        found.append(snapped)
        return len(found) < 48

    if pipe.max_joints >= 2:
        low = max(margin, pipe.length - 2 * max_stock)
        high = min(max_stock, pipe.length - 2 * margin)
        if low <= high:
            denom = max(steps - 1, 1)
            cells = min(16, steps)
            stride = 1 if steps <= cells else max(1, steps // cells)
            for i in range(0, steps, stride):
                if time.monotonic() >= deadline:
                    return found
                first = low + (high - low) * i // denom
                remain = pipe.length - first
                low2 = max(margin, remain - max_stock)
                high2 = min(max_stock, remain - margin)
                if low2 > high2:
                    continue
                for j in range(0, steps, stride):
                    second = low2 + (high2 - low2) * j // denom
                    third = remain - second
                    if min(first, second, third) < margin or max(first, second, third) > max_stock:
                        continue
                    if not push((first, second, third)):
                        return found
    upper = min(pipe.max_joints + 1, 8)
    need = max(2, (pipe.length + max_stock - 1) // max_stock)
    for pieces in range(max(need, 4), upper + 1):
        if time.monotonic() >= deadline or len(found) >= 48:
            break
        uniform = _uniform_parts(pipe, group, pieces)
        if uniform is not None and uniform not in seen:
            seen.add(uniform)
            found.append(uniform)
        low = margin
        high = min(max_stock, pipe.length - margin * (pieces - 1))
        if low > high:
            continue
        denom = max(min(steps, 12) - 1, 1)
        for i in range(min(steps, 12)):
            first = low + (high - low) * i // denom
            tail = pipe.length - first
            others = pieces - 1
            base, extra = divmod(tail, others)
            if base <= 0:
                continue
            nominal_list = [first]
            for index in range(others):
                nominal_list.append(base + (1 if index < extra else 0))
            nominal = tuple(nominal_list)
            if min(nominal) < margin or max(nominal) > max_stock:
                continue
            if not push(nominal):
                return found
    return found


def _price_two_pieces(
    group: MaterialGroup,
    menus: list[list[tuple[int, ...]]],
    deadline: float,
    *,
    window: int,
    introduced: set[int],
    dropped: set[int],
    focus: set[int] | None = None,
    pools: list[list[tuple[int, ...]]] | None = None,
    stock_lengths: set[int] | None = None,
) -> bool:
    """Add complement welds after the restricted master is infeasible.

    The witness is that infeasibility, plus any length that had no exact cut.
    A full alphabet is not a reason to discard the new column: rare lengths
    are replaced, up to the current tier's cap.
    """

    _ensure_time(deadline)
    alphabet = _alphabet(menus)
    stocks = _avail(group)
    kerf = group.blade_margin
    partners = alphabet or set(stocks)
    wanted = focus or set()
    scores: Counter[int] = Counter()
    for stock in stocks:
        for partner in partners:
            piece = stock - kerf - partner
            if piece > 0:
                scores[piece] += 1
        if len(scores) > 4000:
            break
    for piece in wanted:
        scores[piece] += 5
    shortlist = [piece for piece, _score in scores.most_common(_COMPLEMENT_KEEP)]
    steps = min(36, _sample_steps() + (0 if window <= 1 else 4 if window <= 8 else 8))
    proposals: list[tuple[int, int, tuple[int, ...]]] = []
    for pipe_index, pipe in enumerate(group.pipes):
        _ensure_time(deadline)
        existing = set(menus[pipe_index])
        empty_bonus = 1000 if not existing else 0
        for piece in shortlist:
            weight = scores[piece] + empty_bonus
            for target in (piece, pipe.length - piece):
                parts = _windowed_two_piece(pipe, group, target, window)
                if parts is None or parts in existing:
                    continue
                bonus = 50 if wanted and set(parts) & wanted else 0
                proposals.append((weight + bonus, pipe_index, parts))
                existing.add(parts)
        for weld in _grid_welds(pipe, group, stocks, steps):
            parts = _windowed_two_piece(pipe, group, weld, window)
            if parts is None or parts in existing:
                continue
            proposals.append((1 + empty_bonus, pipe_index, parts))
            existing.add(parts)
    proposals.sort(key=lambda item: (-item[0], item[1], item[2]))
    added = False
    lengths = stock_lengths if stock_lengths is not None else set(stocks)
    for _score, pipe_index, parts in proposals:
        if pools is not None:
            _remember(pools, pipe_index, parts, lengths)
        if _admit(menus, pipe_index, parts, introduced, dropped):
            added = True
    return added


def _add_longer_patterns(
    group: MaterialGroup,
    menus: list[list[tuple[int, ...]]],
    deadline: float,
    *,
    introduced: set[int],
    dropped: set[int],
    pools: list[list[tuple[int, ...]]] | None = None,
    stock_lengths: set[int] | None = None,
) -> bool:
    """Three- and four-piece patterns once two-piece pricing has stalled."""

    added = False
    stocks = _avail(group)
    kerf = group.blade_margin
    max_stock = max(stocks) if stocks else 0
    lengths = stock_lengths if stock_lengths is not None else set(stocks)
    for pipe_index, pipe in enumerate(group.pipes):
        _ensure_time(deadline)
        if pipe.max_joints < 2:
            continue
        existing = set(menus[pipe_index])
        for parts in _sample_multi(pipe, group, _sample_steps(), deadline):
            if pools is not None:
                _remember(pools, pipe_index, parts, lengths)
            if parts in existing or len(menus[pipe_index]) >= _pattern_cap():
                continue
            if _admit(menus, pipe_index, parts, introduced, dropped):
                existing.add(parts)
                added = True
        if len(menus[pipe_index]) >= _pattern_cap():
            continue
        piece_counts = [3, 4]
        if max_stock and pipe.length > max_stock:
            need = (pipe.length + max_stock - 1) // max_stock
            upper = min(pipe.max_joints + 1, need + 1 + _tier(), 8)
            piece_counts = list(range(max(need, 2), upper + 1))
        for pieces in piece_counts:
            if pieces < 2 or pieces - 1 > pipe.max_joints:
                continue
            uniform = _uniform_parts(pipe, group, pieces)
            if uniform is not None and uniform not in existing:
                if _admit(menus, pipe_index, uniform, introduced, dropped):
                    existing.add(uniform)
                    added = True
            if pieces >= 3:
                for stock in _anchor_stocks(stocks, pipe.length):
                    peeled = _stock_peel(pipe, group, stock, pieces)
                    if peeled is None or peeled in existing:
                        continue
                    if _admit(menus, pipe_index, peeled, introduced, dropped):
                        existing.add(peeled)
                        added = True
        alphabet = _alphabet(menus)
        bases = [parts for parts in menus[pipe_index] if len(parts) == 2][:4]
        for base in bases:
            if len(menus[pipe_index]) >= _pattern_cap():
                break
            longer = 0 if base[0] >= base[1] else 1
            piece = base[longer]
            targets = [piece // 2]
            for partner in list(alphabet)[:_alphabet_cap()]:
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
                if _admit(menus, pipe_index, rebuilt, introduced, dropped):
                    existing.add(rebuilt)
                    added = True
    return added


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
        shrunk.append(kept[: _pattern_cap()])
    return shrunk


def _solve_master(group, menus, deadline: float, cp_model, slice_cap: float | None = None):
    remaining = deadline - time.monotonic()
    if remaining < _STOP_REMAINING:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout
    cap = _master_cap() if slice_cap is None else slice_cap
    slice_seconds = min(cap, remaining - 0.05)
    if slice_seconds < 0.2:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout
    lengths = sorted(_alphabet(menus))
    if not lengths or any(not menu for menu in menus):
        return "infeasible", None, [], []
    stocks = _avail(group)
    kerf = group.blade_margin
    mode = group.kerf_mode
    shapes = [parts for menu in menus for parts in menu]
    patterns, supplied, exact_lengths = _cut_columns(
        lengths, stocks, kerf, mode, deadline, shapes
    )
    missing = [length for length in lengths if length not in supplied]
    weak = [length for length in lengths if length not in exact_lengths]
    if not patterns:
        return "infeasible", None, missing, weak
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
        if index % 2000 == 0 and time.monotonic() >= deadline:
            _NOTE.set("按需生成时限内未知")
            raise _Timeout
        usage.append(model.NewIntVar(0, avail[stock], f"c{index}"))
    produced_terms: dict[int, list] = {length: [] for length in lengths}
    for index, (variable, (_stock, parts)) in enumerate(zip(usage, patterns)):
        if index % 2000 == 0 and time.monotonic() >= deadline:
            _NOTE.set("按需生成时限内未知")
            raise _Timeout
        for length, copies in Counter(parts).items():
            bucket = produced_terms.get(length)
            if bucket is None:
                continue
            bucket.append(variable if copies == 1 else copies * variable)
    for length in lengths:
        produced = produced_terms[length]
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
    remaining = deadline - time.monotonic()
    if remaining < 0.2:
        _NOTE.set("按需生成时限内未知")
        raise _Timeout
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = min(slice_seconds, max(0.2, remaining - 0.05))
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 1
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        emitted = _read(group, menus, patterns, usage, choice, solver, kerf, mode)
        if emitted is None:
            _NOTE.set("按需生成找到了分配，但料段供需没有对上，结果没有采用")
            return "broken", None, missing, weak
        return "feasible", emitted, [], []
    if status == cp_model.INFEASIBLE:
        return "infeasible", None, missing, weak
    return "unknown", None, missing, weak


def _cut_columns(
    lengths: list[int],
    stocks: list[int],
    kerf: int,
    mode: str,
    deadline: float,
    shapes: list[tuple[int, ...]] | None = None,
):
    patterns: list[tuple[int, tuple[int, ...]]] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()
    supplied: set[int] = set()
    exact_lengths: set[int] = set()
    cap = _column_cap()
    pieces_cap = _pieces_cap()
    ticks = 0

    def add(stock: int, parts: tuple[int, ...], *, exact_only: bool) -> bool:
        nonlocal ticks
        ticks += 1
        if ticks >= 128:
            ticks = 0
            if time.monotonic() >= deadline:
                _NOTE.set("按需生成时限内未知")
                raise _Timeout
        if len(patterns) >= cap:
            return False
        canonical = tuple(sorted(p for p in parts if p > 0))
        if not canonical:
            return True
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
        for length in canonical:
            supplied.add(length)
            if candidate.remainder == 0:
                exact_lengths.add(length)
        return len(patterns) < cap

    def stop(stock: int, parts: tuple[int, ...], *, exact_only: bool) -> bool:
        return not add(stock, parts, exact_only=exact_only)

    for shape in shapes or []:
        if time.monotonic() >= deadline:
            _NOTE.set("按需生成时限内未知")
            raise _Timeout
        for stock in stocks:
            if stop(stock, shape, exact_only=False):
                return patterns, supplied, exact_lengths
    for length in lengths:
        if length in supplied:
            continue
        if time.monotonic() >= deadline:
            _NOTE.set("按需生成时限内未知")
            raise _Timeout
        for stock in stocks:
            if length > stock:
                continue
            if stop(stock, (length,), exact_only=False):
                return patterns, supplied, exact_lengths
            if length in supplied:
                break
    length_set = set(lengths)
    for stock in stocks:
        if time.monotonic() >= deadline:
            _NOTE.set("按需生成时限内未知")
            raise _Timeout
        for length in lengths:
            complement = stock - kerf - length
            if complement <= 0 or complement not in length_set:
                continue
            if stop(stock, (length, complement), exact_only=False):
                return patterns, supplied, exact_lengths
    for exact_only in (True, False):
        for stock in stocks:
            if time.monotonic() >= deadline:
                _NOTE.set("按需生成时限内未知")
                raise _Timeout
            for length in lengths:
                for copies in range(1, pieces_cap + 1):
                    if stop(stock, (length,) * copies, exact_only=exact_only):
                        return patterns, supplied, exact_lengths
            pair_lengths = lengths if len(lengths) <= 180 else lengths[:180]
            for index, left in enumerate(pair_lengths):
                for right in pair_lengths[index + 1 :]:
                    if stop(stock, (left, right), exact_only=exact_only):
                        return patterns, supplied, exact_lengths
                    for copies in range(2, pieces_cap):
                        if stop(stock, (left,) + (right,) * copies, exact_only=exact_only):
                            return patterns, supplied, exact_lengths
                        if stop(stock, (right,) + (left,) * copies, exact_only=exact_only):
                            return patterns, supplied, exact_lengths
            if len(lengths) <= 12:
                for i, first in enumerate(lengths):
                    for j in range(i + 1, len(lengths)):
                        second = lengths[j]
                        for k in range(j + 1, len(lengths)):
                            if stop(
                                stock,
                                (first, second, lengths[k]),
                                exact_only=exact_only,
                            ):
                                return patterns, supplied, exact_lengths
    return patterns, supplied, exact_lengths


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
