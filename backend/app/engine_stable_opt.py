"""Lexicographic post-pass on a verified stable plan.

The stable feasibility search is unchanged. This module is opt-in. A new plan
replaces the stable plan only when it verifies and its
(used length, joints, weld types, cut types) key is lexicographically no worse.
Utilization is not constrained.

Layers, each with its own budget:

1. minimize used length, with the stable columns plus a bounded lexcg pricing pass
2. minimize joints without increasing that used length
3. minimize weld types, then cut types, without worsening the first two locks
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import time
from typing import Any

from .domain import MaterialGroup, from_units
from .lexcg import (
    CUT_NODE_BUDGET,
    SEED_NODE_BUDGET,
    _Budget,
    _CutCol,
    _WeldCol,
    _fits,
    _lp_duals,
    _merge_cuts,
    _merge_welds,
    _price_cuts,
    _price_welds,
    _seed_cut_patterns,
    _seed_weld_patterns,
    knapsack_best_pattern,
)
from .solver import (
    _CutCandidate,
    _WeldCandidate,
    _assemble_group_result,
)

LAYER_SECONDS = (20.0, 20.0, 20.0)
PRICING_CAP_SECONDS = 12.0
RANDOM_SEED = 1
OBJECTIVES = (
    "used_stock_length",
    "welding_joint_quantity",
    "welding_pattern_type_quantity",
    "cutting_pattern_type_quantity",
)


def lex_key(metrics: dict[str, Any]) -> tuple[int, int, int, int]:
    return tuple(int(metrics[name]) for name in OBJECTIVES)  # type: ignore[return-value]


def _as_int(value: Any) -> int:
    return int(round(float(value)))


def _stable_columns(
    group: MaterialGroup, solved: dict[str, Any]
) -> tuple[list[_WeldCol], list[_CutCol], dict[tuple[int, tuple[int, ...]], int], dict[tuple[int, tuple[int, ...]], int]] | None:
    index_of = {pipe.pipe_id: index for index, pipe in enumerate(group.pipes)}
    weld_qty: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    cut_qty: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for row in solved.get("welding_patterns") or []:
        pipe_index = index_of.get(row.get("pipe_id"))
        if pipe_index is None:
            return None
        parts = tuple(_as_int(part) for part in row.get("parts") or [])
        quantity = _as_int(row.get("quantity") or 0)
        if not parts or quantity <= 0:
            return None
        if sum(parts) != group.pipes[pipe_index].length:
            return None
        weld_qty[(pipe_index, parts)] += quantity
    for row in solved.get("cutting_patterns") or []:
        parts = tuple(sorted(_as_int(part) for part in row.get("parts") or []))
        stock = _as_int(row.get("stock_length") or 0)
        quantity = _as_int(row.get("quantity") or 0)
        if not parts or stock <= 0 or quantity <= 0:
            return None
        if not _fits(stock, parts, group.blade_margin, group.kerf_mode):
            return None
        cut_qty[(stock, parts)] += quantity
    for index, pipe in enumerate(group.pipes):
        produced = sum(quantity for (pipe_index, _), quantity in weld_qty.items() if pipe_index == index)
        if produced != pipe.demand:
            return None
    welds = [_WeldCol(pipe_index, parts, {"stable-incumbent"}) for pipe_index, parts in weld_qty]
    cuts = [_CutCol(stock, parts, {"stable-incumbent"}) for stock, parts in cut_qty]
    return welds, cuts, weld_qty, cut_qty


def _extend_pool(
    group: MaterialGroup,
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    deadline: float,
) -> tuple[list[_WeldCol], list[_CutCol]]:
    stocks = [stock.length for stock in group.stocks]
    quantities = {stock.length: stock.quantity for stock in group.stocks}
    pipe_nodes = max(1, SEED_NODE_BUDGET // max(1, len(group.pipes)))
    for pipe_index, pipe in enumerate(group.pipes):
        if time.monotonic() >= deadline:
            break
        patterns, _truncated = _seed_weld_patterns(
            pipe,
            stocks,
            kerf=group.blade_margin,
            min_weld=group.min_weld_distance,
            min_cut=group.min_cut_length,
            budget=_Budget(deadline, pipe_nodes),
        )
        for parts in patterns:
            welds.append(_WeldCol(pipe_index, parts, {"native-seed"}))
    welds = _merge_welds(welds)
    segments = sorted({part for column in welds for part in column.parts})
    if segments and time.monotonic() < deadline:
        for stock, parts in _seed_cut_patterns(
            stocks,
            quantities,
            segments,
            kerf=group.blade_margin,
            kerf_mode=group.kerf_mode,
            budget=_Budget(deadline, CUT_NODE_BUDGET),
            repeated_patterns=[column.parts for column in welds],
        ):
            cuts.append(_CutCol(stock, parts, {"native-seed"}))
        cuts = _merge_cuts(cuts)
        pack_budget = _Budget(deadline, CUT_NODE_BUDGET)
        for stock in group.stocks:
            if pack_budget.expired or time.monotonic() >= deadline:
                break
            pattern, _truncated, nodes = knapsack_best_pattern(
                stock.length,
                segments,
                [float(segment) for segment in segments],
                kerf=group.blade_margin,
                kerf_mode=group.kerf_mode,
                node_budget=max(1, pack_budget.node_limit // max(1, len(group.stocks))),
                deadline=deadline,
            )
            pack_budget.nodes += nodes
            if pattern is not None:
                cuts.append(_CutCol(stock.length, pattern, {"pricing"}))
        cuts = _merge_cuts(cuts)
    price_budget = _Budget(deadline, 80_000)
    for _round in range(3):
        if time.monotonic() > deadline - 0.2:
            break
        duals = _lp_duals(
            group,
            welds,
            cuts,
            min(1.5, max(0.05, deadline - time.monotonic())),
        )
        if duals is None:
            break
        new_welds = _price_welds(group, welds, duals, price_budget)
        new_cuts = _price_cuts(group, cuts, welds, duals, price_budget)
        if not new_welds and not new_cuts:
            break
        welds = _merge_welds(welds + new_welds)
        cuts = _merge_cuts(cuts + new_cuts)
    return welds, cuts


def _scip_setup(model: Any, seconds: float) -> None:
    model.hideOutput()
    model.setRealParam("limits/time", max(0.05, seconds))
    # SCIP 8 has no randomization/randomseed. randomseedshift is the seed knob.
    for name, value in (
        ("parallel/maxnthreads", 1),
        ("randomization/randomseedshift", RANDOM_SEED),
        ("randomization/permutationseed", RANDOM_SEED),
        ("randomization/lpseed", RANDOM_SEED),
    ):
        try:
            model.setIntParam(name, value)
        except Exception:
            continue


def _build_master(
    group: MaterialGroup,
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    seconds: float,
    locks: dict[str, int],
    start_x: list[int] | None,
    start_y: list[int] | None,
):
    from pyscipopt import Model, quicksum

    model = Model("stable-opt")
    _scip_setup(model, seconds)
    x_vars = [
        model.addVar(vtype="INTEGER", lb=0, ub=group.pipes[column.pipe_index].demand, name=f"x{index}")
        for index, column in enumerate(welds)
    ]
    y_vars = [model.addVar(vtype="INTEGER", lb=0, name=f"y{index}") for index in range(len(cuts))]
    by_pipe: dict[int, list[int]] = defaultdict(list)
    for index, column in enumerate(welds):
        by_pipe[column.pipe_index].append(index)
    for pipe_index, pipe in enumerate(group.pipes):
        model.addCons(quicksum(x_vars[index] for index in by_pipe[pipe_index]) == pipe.demand)
    quantity = {stock.length: stock.quantity for stock in group.stocks}
    usage_by_length: dict[int, Any] = {}
    for stock in group.stocks:
        indexes = [index for index, column in enumerate(cuts) if column.stock_length == stock.length]
        expression = quicksum(y_vars[index] for index in indexes)
        model.addCons(expression <= stock.quantity)
        usage_by_length[stock.length] = expression
    must_use_total = sum(stock.must_use_quantity for stock in group.stocks)
    use_optional = None
    must_use_bindings: list[tuple[Any, Any, int, int, int]] = []
    if must_use_total > 0:
        use_optional = model.addVar(vtype="BINARY", name="use_available_stock")
        used_must_terms = []
        for stock in group.stocks:
            available = stock.quantity - stock.must_use_quantity
            used_must = model.addVar(vtype="INTEGER", lb=0, ub=stock.must_use_quantity, name=f"mu{stock.length}")
            used_available = model.addVar(vtype="INTEGER", lb=0, ub=max(0, available), name=f"av{stock.length}")
            model.addCons(used_must + used_available == usage_by_length[stock.length])
            if available > 0:
                model.addCons(used_available <= available * use_optional)
            else:
                model.addCons(used_available == 0)
            used_must_terms.append(used_must)
            must_use_bindings.append((used_must, used_available, stock.length, stock.must_use_quantity, max(0, available)))
        model.addCons(quicksum(used_must_terms) >= must_use_total * use_optional)
    weld_of: dict[tuple[int, ...], list[int]] = defaultdict(list)
    for index, column in enumerate(welds):
        weld_of[column.parts].append(index)
    weld_active = []
    for _signature, indexes in weld_of.items():
        active = model.addVar(vtype="BINARY", name=f"zw{len(weld_active)}")
        weld_active.append(active)
        usage = quicksum(x_vars[index] for index in indexes)
        pipes = {welds[index].pipe_index for index in indexes}
        maximum = sum(group.pipes[index].demand for index in pipes)
        model.addCons(usage <= maximum * active)
        model.addCons(usage >= active)
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
    segments = sorted({part for column in welds for part in column.parts} | {part for column in cuts for part in column.parts})
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
    expressions = {
        "used_stock_length": quicksum(column.stock_length * y_vars[index] for index, column in enumerate(cuts)),
        "welding_joint_quantity": quicksum(column.joints * x_vars[index] for index, column in enumerate(welds)),
        "welding_pattern_type_quantity": quicksum(weld_active),
        "cutting_pattern_type_quantity": quicksum(cut_active),
    }
    for name, bound in locks.items():
        model.addCons(expressions[name] <= int(bound))
    if start_x is not None and start_y is not None and len(start_x) == len(x_vars) and len(start_y) == len(y_vars):
        _install_start(
            model,
            x_vars,
            y_vars,
            weld_active,
            list(weld_of.values()),
            cut_active,
            list(cut_of.values()),
            cuts,
            start_x,
            start_y,
            must_use_bindings,
            use_optional,
            must_use_total,
        )
    return model, x_vars, y_vars, expressions


def _install_start(
    model: Any,
    x_vars: list[Any],
    y_vars: list[Any],
    weld_active: list[Any],
    weld_groups: list[list[int]],
    cut_active: list[Any],
    cut_groups: list[list[int]],
    cuts: list[_CutCol],
    start_x: list[int],
    start_y: list[int],
    must_use_bindings: list[tuple[Any, Any, int, int, int]],
    use_optional: Any,
    must_use_total: int,
) -> None:
    """Load the incumbent when it satisfies the master. A rejected start still solves."""

    try:
        solution = model.createSol()
        for var, value in zip(x_vars, start_x):
            model.setSolVal(solution, var, int(value))
        for var, value in zip(y_vars, start_y):
            model.setSolVal(solution, var, int(value))
        for var, column_indexes in zip(weld_active, weld_groups):
            model.setSolVal(solution, var, 1 if any(start_x[index] > 0 for index in column_indexes) else 0)
        for var, column_indexes in zip(cut_active, cut_groups):
            model.setSolVal(solution, var, 1 if any(start_y[index] > 0 for index in column_indexes) else 0)
        if use_optional is not None:
            must_used = 0
            optional_used = False
            for used_must, used_available, length, must_qty, available in must_use_bindings:
                usage = sum(
                    int(start_y[index])
                    for index, column in enumerate(cuts)
                    if column.stock_length == length
                )
                take_must = min(usage, must_qty)
                take_available = usage - take_must
                if take_available > available:
                    return
                model.setSolVal(solution, used_must, take_must)
                model.setSolVal(solution, used_available, take_available)
                must_used += take_must
                optional_used = optional_used or take_available > 0
            flag = 1 if optional_used else 0
            if flag and must_used < must_use_total:
                return
            model.setSolVal(solution, use_optional, flag)
        model.addSol(solution)
    except Exception:
        return


def _optimize_objective(
    group: MaterialGroup,
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    objective: str,
    seconds: float,
    locks: dict[str, int],
    start_x: list[int] | None,
    start_y: list[int] | None,
) -> dict[str, Any] | None:
    if seconds <= 0 or not welds or not cuts:
        return None
    model, x_vars, y_vars, expressions = _build_master(
        group, welds, cuts, seconds, locks, start_x, start_y
    )
    model.setObjective(expressions[objective], "minimize")
    model.optimize()
    if model.getNSols() <= 0:
        return None
    solution = model.getBestSol()
    x_values = [int(round(model.getSolVal(solution, var))) for var in x_vars]
    y_values = [int(round(model.getSolVal(solution, var))) for var in y_vars]
    key = []
    for name in OBJECTIVES:
        key.append(int(round(model.getSolVal(solution, expressions[name]))))
    return {
        "x": x_values,
        "y": y_values,
        "key": tuple(key),
        "status": str(model.getStatus()).lower(),
    }


def _key_of(x_values: list[int], y_values: list[int], welds: list[_WeldCol], cuts: list[_CutCol]) -> tuple[int, int, int, int]:
    used = sum(cuts[index].stock_length * quantity for index, quantity in enumerate(y_values) if quantity > 0)
    joints = sum(welds[index].joints * quantity for index, quantity in enumerate(x_values) if quantity > 0)
    weld_types = len({welds[index].parts for index, quantity in enumerate(x_values) if quantity > 0})
    cut_types = len({(cuts[index].stock_length, cuts[index].parts) for index, quantity in enumerate(y_values) if quantity > 0})
    return used, joints, weld_types, cut_types


def _within_locks(candidate: tuple[int, int, int, int], locks: dict[str, int]) -> bool:
    for index, name in enumerate(OBJECTIVES):
        bound = locks.get(name)
        if bound is not None and candidate[index] > int(bound):
            return False
    return True


def accept_candidate(
    candidate: tuple[int, int, int, int],
    incumbent: tuple[int, int, int, int],
    locks: dict[str, int],
) -> bool:
    """Keep a candidate only when it respects the locks and is lexicographically no worse."""

    return _within_locks(candidate, locks) and candidate <= incumbent


def _quantities_feasible(
    group: MaterialGroup,
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    x_values: list[int],
    y_values: list[int],
) -> bool:
    if len(x_values) != len(welds) or len(y_values) != len(cuts):
        return False
    produced = [0] * len(group.pipes)
    consumed: dict[int, int] = defaultdict(int)
    for index, quantity in enumerate(x_values):
        if quantity < 0:
            return False
        column = welds[index]
        produced[column.pipe_index] += quantity
        if quantity:
            for part in column.parts:
                consumed[part] += quantity
    if any(produced[index] != pipe.demand for index, pipe in enumerate(group.pipes)):
        return False
    supplied: dict[int, int] = defaultdict(int)
    used_stock: dict[int, int] = defaultdict(int)
    for index, quantity in enumerate(y_values):
        if quantity < 0:
            return False
        if not quantity:
            continue
        column = cuts[index]
        if not _fits(column.stock_length, column.parts, group.blade_margin, group.kerf_mode):
            return False
        used_stock[column.stock_length] += quantity
        for part in column.parts:
            supplied[part] += quantity
    inventory = {stock.length: stock.quantity for stock in group.stocks}
    for length, quantity in used_stock.items():
        if quantity > inventory.get(length, -1):
            return False
    return supplied == consumed


def _assemble(
    group: MaterialGroup,
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    x_values: list[int],
    y_values: list[int],
    elapsed: float,
    original: dict[str, Any],
) -> dict[str, Any]:
    weld_counts = [
        (_WeldCandidate(welds[index].pipe_index, welds[index].parts), x_values[index])
        for index in range(len(welds))
        if x_values[index] > 0
    ]
    cut_counts = [
        (
            _CutCandidate(cuts[index].stock_length, cuts[index].parts, group.blade_margin, group.kerf_mode),
            y_values[index],
        )
        for index in range(len(cuts))
        if y_values[index] > 0
    ]
    result = _assemble_group_result(group, "stable-opt", "FEASIBLE", weld_counts, cut_counts, elapsed)
    metrics = result["metrics"]
    metrics["solve_status"] = "FEASIBLE"
    metrics["lexicographic_optimal"] = False
    metrics["core_lexicographic_optimal"] = False
    metrics["stable_strategy"] = original.get("metrics", {}).get("stable_strategy", "")
    metrics["opt_source"] = "opt-improved"
    return result


def _start_vectors(
    welds: list[_WeldCol],
    cuts: list[_CutCol],
    weld_qty: dict[tuple[int, tuple[int, ...]], int],
    cut_qty: dict[tuple[int, tuple[int, ...]], int],
) -> tuple[list[int], list[int]]:
    x_values = [weld_qty.get((column.pipe_index, column.parts), 0) for column in welds]
    y_values = [cut_qty.get((column.stock_length, column.parts), 0) for column in cuts]
    return x_values, y_values


def optimize_group(
    group: MaterialGroup,
    solved: dict[str, Any],
    layer_seconds: tuple[float, float, float] = LAYER_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the kept group and a layer log. The kept group is lexicographically safe."""

    started = time.monotonic()
    baseline_key = lex_key(solved["metrics"])
    log: dict[str, Any] = {
        "kept": "stable",
        "reason": "",
        "baseline": list(baseline_key),
        "layers": [],
        "seconds": 0.0,
    }
    extracted = _stable_columns(group, solved)
    if extracted is None:
        log["reason"] = "起点列无法还原"
        log["seconds"] = round(time.monotonic() - started, 3)
        return solved, log
    stable_welds, stable_cuts, weld_qty, cut_qty = extracted
    material_budget, joint_budget, pattern_budget = (float(item) for item in layer_seconds)
    pricing_deadline = time.monotonic() + min(PRICING_CAP_SECONDS, max(0.0, material_budget))
    try:
        welds, cuts = _extend_pool(group, list(stable_welds), list(stable_cuts), pricing_deadline)
    except Exception as exc:  # noqa: BLE001
        log["reason"] = f"定价失败 {type(exc).__name__}: {exc}"
        log["seconds"] = round(time.monotonic() - started, 3)
        return solved, log
    pricing_seconds = time.monotonic() - started
    material_mip = max(0.0, material_budget - pricing_seconds)
    log["weld_columns"] = len(welds)
    log["cut_columns"] = len(cuts)
    start_x, start_y = _start_vectors(welds, cuts, weld_qty, cut_qty)
    best_x, best_y = start_x, start_y
    best_key = _key_of(best_x, best_y, welds, cuts)
    if best_key != baseline_key:
        log["reason"] = "起点列复算与指标不一致"
        log["recomputed"] = list(best_key)
        log["seconds"] = round(time.monotonic() - started, 3)
        return solved, log
    locks = {"used_stock_length": best_key[0]}
    stages = (
        ("used_stock_length", material_mip, 0),
        ("welding_joint_quantity", joint_budget, 1),
        ("welding_pattern_type_quantity", pattern_budget * 0.5, 2),
        ("cutting_pattern_type_quantity", pattern_budget * 0.5, 2),
    )
    # The third layer owns one clock. The two pattern objectives share it
    # in order; unused time from the first objective stays available.
    pattern_deadline: float | None = None
    for objective, budget, layer_index in stages:
        if layer_index == 2:
            if pattern_deadline is None:
                pattern_deadline = time.monotonic() + pattern_budget
            budget = max(0.0, pattern_deadline - time.monotonic())
        stage_started = time.monotonic()
        entered = best_key
        improved = False
        status = "skipped"
        try:
            found = _optimize_objective(
                group, welds, cuts, objective, budget, locks, best_x, best_y
            )
        except Exception as exc:  # noqa: BLE001
            found = None
            status = f"error:{type(exc).__name__}"
            log["reason"] = f"{objective} {exc}"
        if found is not None:
            status = found["status"]
            if not _quantities_feasible(group, welds, cuts, found["x"], found["y"]):
                status = f"rejected:infeasible:{status}"
            else:
                candidate = _key_of(found["x"], found["y"], welds, cuts)
                if accept_candidate(candidate, entered, locks):
                    improved = candidate < entered
                    best_key = candidate
                    best_x, best_y = found["x"], found["y"]
                else:
                    status = f"rejected:{status}"
        if objective == "used_stock_length":
            locks = {"used_stock_length": best_key[0]}
        elif objective == "welding_joint_quantity":
            locks = {"used_stock_length": best_key[0], "welding_joint_quantity": best_key[1]}
        elif objective == "welding_pattern_type_quantity":
            locks = {
                "used_stock_length": best_key[0],
                "welding_joint_quantity": best_key[1],
                "welding_pattern_type_quantity": best_key[2],
            }
        log["layers"].append(
            {
                "objective": objective,
                "seconds": round(time.monotonic() - stage_started, 3),
                "accepted": improved,
                "status": status,
                "entered": list(entered),
                "key": list(best_key),
            }
        )
    log["pricing_seconds"] = round(pricing_seconds, 3)
    log["final"] = list(best_key)
    if best_key > baseline_key:
        log["reason"] = "词典序差于起点"
        log["seconds"] = round(time.monotonic() - started, 3)
        return solved, log
    if best_key == baseline_key:
        log["reason"] = "与起点相同"
        log["seconds"] = round(time.monotonic() - started, 3)
        solved = deepcopy(solved)
        solved.setdefault("metrics", {})["opt_source"] = "stable-incumbent"
        return solved, log
    try:
        optimized = _assemble(group, welds, cuts, best_x, best_y, time.monotonic() - started, solved)
    except Exception as exc:  # noqa: BLE001
        log["reason"] = f"组装失败 {type(exc).__name__}: {exc}"
        log["seconds"] = round(time.monotonic() - started, 3)
        return solved, log
    if lex_key(optimized["metrics"]) != best_key:
        log["reason"] = "组装指标与主问题不一致"
        log["seconds"] = round(time.monotonic() - started, 3)
        return solved, log
    optimized["metrics"]["opt_layers"] = log["layers"]
    log["kept"] = "optimized"
    log["reason"] = "词典序改进"
    log["seconds"] = round(time.monotonic() - started, 3)
    return optimized, log


def _refresh_summary(result: dict[str, Any], groups: list[dict[str, Any]]) -> None:
    summary = result.get("summary")
    if not isinstance(summary, dict):
        return
    summary["used_stock_length"] = from_units(sum(int(group["metrics"]["used_stock_length"]) for group in groups))
    summary["welding_joint_quantity"] = sum(int(group["metrics"]["welding_joint_quantity"]) for group in groups)
    summary["welding_pattern_type_quantity"] = sum(
        int(group["metrics"]["welding_pattern_type_quantity"]) for group in groups
    )
    summary["cutting_pattern_type_quantity"] = sum(
        int(group["metrics"]["cutting_pattern_type_quantity"]) for group in groups
    )
    summary["kerf_loss"] = from_units(sum(int(group["metrics"]["kerf_loss"]) for group in groups))
    summary["remainder_length"] = from_units(sum(int(group["metrics"]["remainder_length"]) for group in groups))
    summary["reused_welding_pattern_type_quantity"] = sum(
        int(group["metrics"]["reused_welding_pattern_type_quantity"]) for group in groups
    )
    summary["reused_cutting_pattern_type_quantity"] = sum(
        int(group["metrics"]["reused_cutting_pattern_type_quantity"]) for group in groups
    )
    summary["must_use_stock_quantity"] = sum(int(group["metrics"]["must_use_stock_quantity"]) for group in groups)
    summary["must_use_used_quantity"] = sum(int(group["metrics"]["must_use_used_quantity"]) for group in groups)
    summary["must_use_stock_length"] = from_units(
        sum(int(group["metrics"]["must_use_stock_length"]) for group in groups)
    )
    demand = float(summary.get("demand_length") or 0)
    used = float(summary["used_stock_length"])
    summary["utilization_rate"] = demand / used if used else 0.0
    summary["target_reached"] = all(bool(group["metrics"]["target_reached"]) for group in groups)
    summary["lexicographic_optimal"] = False
    summary["core_lexicographic_optimal"] = False
    summary["optimality_claimed"] = False


def apply_optimization(
    prepared_payload: dict[str, Any],
    result: dict[str, Any],
    layer_seconds: tuple[float, float, float] = LAYER_SECONDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Optimize each feasible group. Reject a plan the verifier does not accept."""

    from .domain import parse_problem
    from .verifier import verify_solution

    problem = parse_problem(prepared_payload)
    solved_groups = list(result.get("groups") or [])
    if len(problem.groups) != len(solved_groups):
        return result, [{"kept": "stable", "reason": "组数不一致", "layers": []}]
    logs: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    replaced = False
    for group, solved in zip(problem.groups, solved_groups):
        status = str((solved.get("metrics") or {}).get("solve_status") or "")
        if status != "FEASIBLE":
            groups.append(solved)
            logs.append({"kept": "stable", "reason": "起点不可行", "layers": []})
            continue
        optimized, log = optimize_group(group, solved, layer_seconds)
        if log.get("kept") == "optimized":
            candidate = deepcopy(result)
            candidate_groups = list(groups) + [optimized] + solved_groups[len(groups) + 1 :]
            candidate["groups"] = candidate_groups
            _refresh_summary(candidate, candidate_groups)
            report = verify_solution(prepared_payload, candidate)
            better = lex_key(optimized["metrics"]) < lex_key(solved["metrics"])
            if report.get("passed") is True and better:
                groups.append(optimized)
                replaced = True
            else:
                log["kept"] = "stable"
                if report.get("passed") is not True:
                    codes: list[str] = []
                    for issue in report.get("issues") or []:
                        code = str(issue.get("code") or "")
                        if code and code not in codes:
                            codes.append(code)
                    log["issue_codes"] = codes[:12]
                    log["reason"] = "校验未通过"
                else:
                    log["reason"] = "校验通过但词典序未改进"
                groups.append(solved)
        else:
            groups.append(solved)
        logs.append(log)
    if not replaced:
        return result, logs
    updated = deepcopy(result)
    updated["groups"] = groups
    _refresh_summary(updated, groups)
    return updated, logs
