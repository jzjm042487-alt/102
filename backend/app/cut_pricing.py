"""Exact cut pricing and material lower bounds.

The pricing routine is a reusable unbounded knapsack. It is the piece that can
later replace the fixed cut-column cap in the post-pass. This module is not
imported by the production solver.

Kerf geometry matches ``lexcg._fits``:

- between-parts loss is ``kerf * (n - 1)``;
- ``WITH_REMAINDER`` charges one extra kerf when the bar is not filled flush.

A pattern is feasible exactly when the transformed item weights fit:

- each piece of length ``L`` has weight ``L + kerf``;
- ``BETWEEN_PARTS`` allows total weight <= stock + kerf;
- ``WITH_REMAINDER`` allows total weight <= stock, or total weight == stock + kerf
  (the flush bar, which does not pay the trailing kerf).
"""

from __future__ import annotations

import ctypes
import math
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Sequence

RC_EPS = 1e-6
_NATIVE = None
_NATIVE_TRIED = False
_C_SOURCE = Path(__file__).with_name("cut_pricing_dp.c")


def fits(stock: int, parts: Sequence[int], kerf: int, kerf_mode: str) -> bool:
    """Return whether ``parts`` can be cut from one bar of ``stock``."""

    if stock <= 0 or kerf < 0 or not parts or any(part <= 0 for part in parts):
        return False
    if kerf_mode not in ("BETWEEN_PARTS", "WITH_REMAINDER"):
        raise ValueError(f"unsupported kerf mode {kerf_mode}")
    between = kerf * max(0, len(parts) - 1)
    remainder = stock - sum(parts) - between
    if kerf_mode == "WITH_REMAINDER" and remainder > 0:
        remainder -= kerf
    return remainder >= 0


def _capacity(stock: int, kerf: int) -> int:
    return stock + kerf


def _price_python(
    weights: Sequence[int],
    values: Sequence[float],
    cap: int,
    mode: int,
    stock: int,
) -> tuple[float, list[int]]:
    dp = [-1.0e300] * (cap + 1)
    choice = [-1] * (cap + 1)
    prev = [-1] * (cap + 1)
    dp[0] = 0.0
    n = len(weights)
    for index, (weight, value) in enumerate(zip(weights, values)):
        if weight <= 0 or value <= 0.0 or weight > cap:
            continue
        for cursor in range(weight, cap + 1):
            candidate = dp[cursor - weight] + value
            if candidate > dp[cursor] + 1.0e-12:
                dp[cursor] = candidate
                choice[cursor] = index
                prev[cursor] = cursor - weight
    best_c = -1
    best = 0.0
    for cursor in range(1, cap + 1):
        if mode == 1 and not (cursor <= stock or cursor == cap):
            continue
        if dp[cursor] > best + 1.0e-12:
            best = dp[cursor]
            best_c = cursor
    counts = [0] * n
    cursor = best_c
    guard = 0
    while cursor > 0 and guard <= cap:
        item = choice[cursor]
        if item < 0:
            break
        counts[item] += 1
        cursor = prev[cursor]
        guard += 1
    return best, counts


def _load_native():
    global _NATIVE, _NATIVE_TRIED
    if _NATIVE_TRIED:
        return _NATIVE
    _NATIVE_TRIED = True
    if not _C_SOURCE.exists():
        return None
    try:
        digest = str(_C_SOURCE.stat().st_mtime_ns)
        cache = Path(tempfile.gettempdir()) / f"cut_pricing_dp_{digest}.so"
        if not cache.exists():
            command = [
                "gcc",
                "-O3",
                "-fPIC",
                "-shared",
                "-o",
                str(cache),
                str(_C_SOURCE),
            ]
            subprocess.run(command, check=True, capture_output=True)
        library = ctypes.CDLL(str(cache))
        library.price_knapsack.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_double),
        ]
        library.price_knapsack.restype = ctypes.c_int
        _NATIVE = library
    except (OSError, subprocess.CalledProcessError):
        _NATIVE = None
    return _NATIVE


def _price_native(
    weights: Sequence[int],
    values: Sequence[float],
    cap: int,
    mode: int,
    stock: int,
) -> tuple[float, list[int]] | None:
    library = _load_native()
    if library is None:
        return None
    n = len(weights)
    weight_buf = (ctypes.c_int * n)(*weights)
    value_buf = (ctypes.c_double * n)(*values)
    count_buf = (ctypes.c_int * n)()
    out = ctypes.c_double()
    code = library.price_knapsack(
        n, weight_buf, value_buf, cap, mode, stock, count_buf, ctypes.byref(out)
    )
    if code != 0:
        return None
    return float(out.value), [int(count_buf[index]) for index in range(n)]


def price_pattern(
    stock: int,
    segments: Sequence[tuple[int, float]],
    kerf: int,
    kerf_mode: str,
    *,
    native: bool | None = None,
) -> tuple[float, tuple[int, ...]]:
    """Return the maximum dual value and one optimal pattern.

    ``segments`` is a sequence of ``(length, dual)``. Non-positive duals are
    ignored. The pattern is sorted. The empty pattern has value 0.
    """

    if kerf_mode not in ("BETWEEN_PARTS", "WITH_REMAINDER"):
        raise ValueError(f"unsupported kerf mode {kerf_mode}")
    if stock <= 0 or kerf < 0:
        raise ValueError("stock and kerf must be non-negative, stock positive")
    merged: dict[int, float] = {}
    for length, dual in segments:
        if length <= 0 or dual <= 0.0:
            continue
        merged[length] = max(merged.get(length, 0.0), float(dual))
    lengths = sorted(merged)
    if not lengths:
        return 0.0, ()
    weights = [length + kerf for length in lengths]
    values = [merged[length] for length in lengths]
    cap = _capacity(stock, kerf)
    mode = 1 if kerf_mode == "WITH_REMAINDER" else 0
    if cap > 2_000_000:
        raise ValueError(f"stock {stock} is too long for the pricing DP")
    use_native = _load_native() is not None if native is None else native
    counted: tuple[float, list[int]] | None
    if use_native:
        counted = _price_native(weights, values, cap, mode, stock)
        if counted is None:
            counted = _price_python(weights, values, cap, mode, stock)
    else:
        counted = _price_python(weights, values, cap, mode, stock)
    value, counts = counted
    parts_list: list[int] = []
    for length, count in zip(lengths, counts):
        parts_list.extend([length] * count)
    parts = tuple(sorted(parts_list))
    if parts and not fits(stock, parts, kerf, kerf_mode):
        value, counts = _price_python(weights, values, cap, mode, stock)
        parts_list = []
        for length, count in zip(lengths, counts):
            parts_list.extend([length] * count)
        parts = tuple(sorted(parts_list))
        if parts and not fits(stock, parts, kerf, kerf_mode):
            raise RuntimeError("pricing DP returned an infeasible pattern")
    rebuilt = sum(merged[part] for part in parts)
    if abs(rebuilt - value) > 1e-6 * max(1.0, abs(value)):
        raise RuntimeError("pricing DP value does not match the recovered pattern")
    return value, parts


def max_copies(stock: int, length: int, kerf: int, kerf_mode: str) -> int:
    """Largest number of equal pieces that fit on one bar."""

    if length <= 0 or not fits(stock, (length,), kerf, kerf_mode):
        return 0
    low = 1
    high = 1 + stock // length
    best = 1
    while low <= high:
        mid = (low + high) // 2
        if fits(stock, (length,) * mid, kerf, kerf_mode):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def minimum_inventory_length(
    stocks: Sequence[tuple[int, int]], target: int
) -> int | None:
    """Minimum total bar length, within inventory, that is at least ``target``.

    This is exact for the volume relaxation: bar lengths are additive and no
    other constraint is enforced. Returns None when the inventory is shorter
    than ``target``. A non-positive target needs no bar.
    """

    if target <= 0:
        return 0
    merged: dict[int, int] = {}
    for length, quantity in stocks:
        if length <= 0 or quantity <= 0:
            continue
        merged[length] = merged.get(length, 0) + int(quantity)
    if not merged:
        return None
    total = sum(length * quantity for length, quantity in merged.items())
    if total < target:
        return None
    max_length = max(merged)
    # An optimal cover overshoots by less than one bar, so sums at or above
    # target + max_length are never needed.
    limit = target + max_length
    reachable = 1
    for length, quantity in sorted(merged.items()):
        chunk = 1
        remaining = quantity
        while remaining > 0:
            take = min(chunk, remaining)
            shift = length * take
            reachable |= reachable << shift
            if reachable.bit_length() > limit:
                reachable &= (1 << limit) - 1
            remaining -= take
            chunk <<= 1
    higher = reachable >> target
    if higher == 0:
        return None
    return target + ((higher & -higher).bit_length() - 1)


def _pattern_key(stock: int, parts: Iterable[int]) -> tuple[int, tuple[int, ...]]:
    return stock, tuple(sorted(int(part) for part in parts))


def _incumbent_used(
    demand: Mapping[int, int],
    stocks: Sequence[tuple[int, int]],
    incumbent: Sequence[tuple[int, tuple[int, ...], int]],
    kerf: int,
    kerf_mode: str,
) -> int | None:
    supply: Counter[int] = Counter()
    usage: Counter[int] = Counter()
    used = 0
    available = {length: quantity for length, quantity in stocks}
    for stock, parts, quantity in incumbent:
        if quantity <= 0 or not fits(stock, parts, kerf, kerf_mode):
            return None
        if stock not in available:
            return None
        usage[stock] += quantity
        used += stock * quantity
        for part in parts:
            supply[part] += quantity
    if any(usage[stock] > available[stock] for stock in usage):
        return None
    if any(supply[length] < quantity for length, quantity in demand.items()):
        return None
    return used


def _integer_lb(value: float) -> int:
    if value <= 0:
        return 0
    nearest = round(value)
    if abs(value - nearest) <= 1e-6 * max(1.0, abs(value)):
        return int(nearest)
    return int(math.ceil(value - 1e-9))


def _valid_dual_bound(
    demand: Mapping[int, int],
    stocks: Sequence[tuple[int, int]],
    pi: Mapping[int, float],
    beta: Mapping[int, float],
    kappa: Mapping[int, float],
) -> tuple[float, float, float]:
    """Return (best valid LB, inventory-adjusted LB, Farley LB)."""

    pi_dot = sum(float(pi.get(length, 0.0)) * quantity for length, quantity in demand.items())
    raw_dual = pi_dot
    penalty = 0.0
    for length, quantity in stocks:
        dual = float(beta.get(length, 0.0))
        raw_dual += dual * quantity
        scarcity = max(0.0, -dual)
        needed = max(0.0, float(kappa.get(length, 0.0)) - length)
        penalty += max(0.0, needed - scarcity) * quantity
    inventory_bound = raw_dual - penalty
    theta = 1.0
    for length, _quantity in stocks:
        if length <= 0:
            continue
        theta = max(theta, float(kappa.get(length, 0.0)) / float(length))
    farley = pi_dot / theta if theta > 0 else 0.0
    best = max(0.0, inventory_bound, farley)
    return best, inventory_bound, farley


def cutting_stock_bound(
    demand: Mapping[int, int],
    stocks: Sequence[tuple[int, int]],
    kerf: int,
    kerf_mode: str,
    incumbent: Sequence[tuple[int, tuple[int, ...], int]] | None = None,
    *,
    deadline: float | None = None,
    seed: int = 1,
    threads: int = 1,
) -> dict[str, object]:
    """Column-generation lower bound for fixed segment demand.

    The master minimizes opened bar length. Pricing is the exact knapsack.
    The returned ``lp_bound`` is always a lower bound on the LP (and therefore
    on every integer solution): the restricted-master objective is used only
    after pricing certifies that no negative reduced-cost pattern remains.
    Before that, the bound is the better of the inventory-adjusted dual and
    the Farley bound of the infinite-inventory relaxation.

    An integer solution is proved optimal only when its opened length equals
    the integer lower bound. Optimality of a SCIP solve on the generated
    columns alone is not enough, because a missing pattern could still improve
    the integer solution.
    """

    import time

    from ortools.linear_solver import pywraplp

    started = time.monotonic()
    clean_demand = {
        int(length): int(quantity)
        for length, quantity in demand.items()
        if int(quantity) > 0 and int(length) > 0
    }
    clean_stocks = [
        (int(length), int(quantity))
        for length, quantity in stocks
        if int(length) > 0 and int(quantity) > 0
    ]
    # Identical lengths share one inventory row.
    stock_qty: dict[int, int] = {}
    for length, quantity in clean_stocks:
        stock_qty[length] = stock_qty.get(length, 0) + quantity
    clean_stocks = sorted(stock_qty.items())
    incumbent = tuple(incumbent or ())
    incumbent_used = _incumbent_used(
        clean_demand, clean_stocks, incumbent, kerf, kerf_mode
    )

    patterns: list[tuple[int, tuple[int, ...]]] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()

    def add_pattern(stock: int, parts: tuple[int, ...]) -> bool:
        if not parts or not fits(stock, parts, kerf, kerf_mode):
            return False
        key = _pattern_key(stock, parts)
        if key in seen or key[0] not in stock_qty:
            return False
        seen.add(key)
        patterns.append(key)
        return True

    for stock, _quantity in clean_stocks:
        for length in sorted(clean_demand):
            copies = max_copies(stock, length, kerf, kerf_mode)
            if copies:
                add_pattern(stock, (length,) * copies)
    for stock, parts, _quantity in incumbent:
        add_pattern(stock, tuple(parts))

    solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        raise RuntimeError("GLOP is not available")
    objective = solver.Objective()
    objective.SetMinimization()
    demand_cons = {}
    for length, quantity in sorted(clean_demand.items()):
        demand_cons[length] = solver.Constraint(float(quantity), solver.infinity())
    stock_cons = {}
    for length, quantity in clean_stocks:
        stock_cons[length] = solver.Constraint(-solver.infinity(), float(quantity))
    big_m = float(sum(length * quantity for length, quantity in clean_stocks) + 1)
    slacks = {}
    for length in demand_cons:
        slack = solver.NumVar(0.0, solver.infinity(), f"s{length}")
        demand_cons[length].SetCoefficient(slack, 1.0)
        objective.SetCoefficient(slack, big_m)
        slacks[length] = slack
    variables = []

    def add_column(stock: int, parts: tuple[int, ...]) -> None:
        variable = solver.NumVar(0.0, solver.infinity(), f"y{len(variables)}")
        variables.append(variable)
        counts = Counter(parts)
        for length, count in counts.items():
            cons = demand_cons.get(length)
            if cons is not None and count:
                cons.SetCoefficient(variable, float(count))
        stock_cons[stock].SetCoefficient(variable, 1.0)
        objective.SetCoefficient(variable, float(stock))

    for stock, parts in patterns:
        add_column(stock, parts)

    rounds = 0
    certified = False
    last_bound: float | None = None
    last_source = "none"
    last_inventory = 0.0
    last_farley = 0.0
    last_slack = 0.0
    note = ""

    def _accept(value: float, source: str) -> None:
        nonlocal last_bound, last_source
        if incumbent_used is not None and value > float(incumbent_used) + 1e-4:
            return
        if last_bound is None or value > last_bound + 1e-9:
            last_bound = value
            last_source = source

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            note = "时限"
            break
        remaining_ms = 86_400_000
        if deadline is not None:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        solver.SetTimeLimit(remaining_ms)
        status = solver.Solve()
        if status != pywraplp.Solver.OPTIMAL:
            note = note or f"主问题状态 {status}"
            break
        last_slack = sum(var.solution_value() for var in slacks.values())
        material = float(solver.Objective().Value()) - big_m * last_slack
        pi = {
            length: max(0.0, cons.dual_value())
            for length, cons in demand_cons.items()
        }
        beta = {length: cons.dual_value() for length, cons in stock_cons.items()}
        kappa: dict[int, float] = {}
        proposals: list[tuple[int, tuple[int, ...], float]] = []
        worst_rc = 0.0
        priced_all = True
        for stock, _quantity in clean_stocks:
            if deadline is not None and time.monotonic() >= deadline:
                priced_all = False
                note = "时限"
                break
            value, parts = price_pattern(
                stock,
                tuple((length, pi[length]) for length in sorted(pi)),
                kerf,
                kerf_mode,
            )
            kappa[stock] = value
            reduced = float(stock) - value - float(beta.get(stock, 0.0))
            worst_rc = min(worst_rc, reduced)
            if reduced < -RC_EPS and parts:
                proposals.append((stock, parts, reduced))
        _best, inventory_bound, farley = _valid_dual_bound(
            clean_demand, clean_stocks, pi, beta, kappa
        )
        last_inventory = inventory_bound
        last_farley = farley
        # Positive slack prices the penalty, not opened length. A partial
        # pricing pass has not seen every stock, so its dual is not valid.
        if last_slack <= 1e-6 and priced_all:
            if worst_rc >= -RC_EPS:
                # No missing column has negative reduced cost, so the
                # restricted master is the LP optimum.
                certified = True
                last_source = "certified"
                last_bound = material
            else:
                _accept(inventory_bound, "inventory-dual")
                _accept(farley, "farley")
        if certified or not priced_all:
            break
        added = 0
        for stock, parts, _reduced in proposals:
            if add_pattern(stock, parts):
                add_column(stock, parts)
                added += 1
        rounds += 1
        if added == 0:
            note = note or "负减价列已在主问题中"
            break
        if rounds > 5000:
            note = "列数安全停"
            break

    lp_bound = None if last_bound is None else float(last_bound)
    integer_bound = None if lp_bound is None else _integer_lb(lp_bound)
    if (
        incumbent_used is not None
        and integer_bound is not None
        and integer_bound > incumbent_used
    ):
        note = (note + "；" if note else "") + "下界高于已知可行用料，已丢弃"
        lp_bound = None
        integer_bound = None
        last_source = "invalid"
        certified = False

    integer_solution: int | None = None
    integer_proved = False
    scip_status = ""
    if (
        incumbent_used is not None
        and integer_bound is not None
        and integer_bound == incumbent_used
    ):
        integer_solution = incumbent_used
        integer_proved = True
        scip_status = "incumbent-equals-bound"
    elif integer_bound is not None and patterns and (
        deadline is None or time.monotonic() < deadline - 0.3
    ):
        ip_seconds = 30.0 if deadline is None else max(0.1, deadline - time.monotonic())
        integer_solution, _opened_matches_bound, scip_status = _solve_integer(
            patterns,
            clean_demand,
            clean_stocks,
            incumbent if incumbent_used is not None else (),
            kerf,
            kerf_mode,
            integer_bound,
            ip_seconds,
            seed,
            threads,
        )
        if incumbent_used is not None and (
            integer_solution is None or incumbent_used < integer_solution
        ):
            integer_solution = incumbent_used
        if integer_solution is not None and integer_solution < integer_bound:
            note = (note + "；" if note else "") + "整数解低于下界，下界已丢弃"
            lp_bound = None
            integer_bound = None
            integer_proved = False
            last_source = "invalid"
        elif integer_solution is not None and integer_solution == integer_bound:
            integer_proved = True

    return {
        "lp_bound": lp_bound if last_source != "invalid" else None,
        "lp_source": last_source,
        "integer_lb": integer_bound if last_source != "invalid" else None,
        "integer_solution": integer_solution,
        "integer_proved": integer_proved,
        "columns": len(patterns),
        "rounds": rounds,
        "seconds": time.monotonic() - started,
        "incumbent": incumbent_used,
        "slack": last_slack,
        "inventory_dual": last_inventory,
        "farley": last_farley,
        "certified": certified,
        "scip_status": scip_status,
        "note": note,
    }


def _solve_integer(
    patterns: Sequence[tuple[int, tuple[int, ...]]],
    demand: Mapping[int, int],
    stocks: Sequence[tuple[int, int]],
    incumbent: Sequence[tuple[int, tuple[int, ...], int]],
    kerf: int,
    kerf_mode: str,
    integer_bound: int,
    seconds: float,
    seed: int,
    threads: int,
) -> tuple[int | None, bool, str]:
    from pyscipopt import Model, quicksum

    model = Model("lb1-ip")
    try:
        model.hideOutput()
        model.setRealParam("limits/time", max(0.1, seconds))
        for name, value in (
            ("parallel/maxnthreads", int(threads)),
            ("randomization/randomseedshift", int(seed)),
            ("randomization/permutationseed", int(seed)),
            ("randomization/lpseed", int(seed)),
        ):
            try:
                model.setIntParam(name, value)
            except Exception:
                continue
        variables = [
            model.addVar(vtype="INTEGER", lb=0, name=f"y{index}")
            for index in range(len(patterns))
        ]
        index_of = {pattern: index for index, pattern in enumerate(patterns)}
        by_length: dict[int, list[int]] = {length: [] for length, _quantity in stocks}
        for index, (stock, _parts) in enumerate(patterns):
            by_length.setdefault(stock, []).append(index)
        for length, quantity in stocks:
            model.addCons(
                quicksum(variables[index] for index in by_length.get(length, []))
                <= quantity
            )
        for length, quantity in demand.items():
            terms = []
            for index, (_stock, parts) in enumerate(patterns):
                count = parts.count(length)
                if count:
                    terms.append(count * variables[index])
            if not terms:
                return None, False, "missing-column"
            model.addCons(quicksum(terms) >= quantity)
        model.setObjective(
            quicksum(stock * variables[index] for index, (stock, _parts) in enumerate(patterns)),
            "minimize",
        )
        start_qty = { _pattern_key(stock, parts): quantity for stock, parts, quantity in incumbent }
        if start_qty and all(fits(stock, parts, kerf, kerf_mode) for stock, parts, _q in incumbent):
            solution = model.createSol()
            for key, quantity in start_qty.items():
                index = index_of.get(key)
                if index is not None:
                    model.setSolVal(solution, variables[index], quantity)
            model.addSol(solution)
        model.optimize()
        status = str(model.getStatus())
        if model.getNSols() <= 0:
            return None, False, status
        opened = 0
        supply: Counter[int] = Counter()
        usage: Counter[int] = Counter()
        available = {length: quantity for length, quantity in stocks}
        for index, (stock, parts) in enumerate(patterns):
            quantity = int(round(model.getVal(variables[index])))
            if quantity <= 0:
                continue
            if not fits(stock, parts, kerf, kerf_mode):
                return None, False, status
            usage[stock] += quantity
            opened += stock * quantity
            for part in parts:
                supply[part] += quantity
        if any(usage[stock] > available.get(stock, 0) for stock in usage):
            return None, False, status
        if any(supply[length] < quantity for length, quantity in demand.items()):
            return None, False, status
        proved = opened <= integer_bound
        return opened, proved, status
    finally:
        model.freeProb()
