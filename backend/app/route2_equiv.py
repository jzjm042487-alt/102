"""Route-2 (equivalent-stock CSP) solver -- an alternative single-layer engine
for the cutting-stock-with-welding problem, based on Ilyes & Balogh's reduction
to a standard 1D-CSP over "equivalent stocks".

An equivalent stock is several real bars welded end-to-end; a pipe cut from a
contiguous span incurs one weld per bar-boundary it crosses.  This collapses the
two echelons (cut x weld) into one CSP whose columns are equivalent-stock cut
plans, solved lexicographically (material, then welds) by SCIP.

Public entry point: :func:`solve_group` returns a production group-result dict
(same schema as the two-layer MILP, via ``solver._assemble_group_result``) that
passes ``verifier.verify_solution``, or ``None`` if this engine cannot solve the
group within the budget.  It never raises for solver reasons and never mutates
the input group.

Correctness of the schema mapping (segment-balance, forbidden zones, must_use,
metric re-checks) is validated offline against the real verifier -- see
scripts/_verify_route2_schema.py.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from itertools import combinations_with_replacement
from typing import Any

# Generation / model budgets.  These mirror the values validated in the demo.
_COL_BUDGET = 8000
_PER_PLAN_CAP = 200
_MULTISET_CAP = 20000
# Cap on the wall-clock share of the time budget spent generating columns.
# Kept as a module constant so tuning/A-B tests can adjust it without touching
# the solve logic.
_GEN_WINDOW_CAP = 6.0


def _enumerate_equiv_cutplans(
    bar_lengths: tuple[int, ...],
    kerf: int,
    types: list[dict[str, Any]],
    min_seg: int,
    per_plan_cap: int,
) -> list[dict[str, Any]]:
    """Cut plans over ONE equivalent stock = bars welded end-to-end.

    Pipes (identified by TYPE = length+forbidden+max_joints) are laid
    head-to-tail; for each placement we count how many bar-boundaries the span
    crosses (= welds).  A placement is legal only if welds <= max_joints, every
    welded stub >= min_seg, and no weld lands in a forbidden zone (measured from
    the pipe start).  Returns list of {parts, layout, joints, material} where
    parts/layout hold TYPE INDICES into ``types``.
    """
    boundaries: list[int] = []
    pos = 0
    for i, b in enumerate(bar_lengths):
        pos += b - (kerf if i > 0 else 0)
        boundaries.append(pos)
    total_usable = boundaries[-1]
    total_material = sum(bar_lengths)

    def _inner_boundaries(start: int, end: int) -> list[int]:
        return [bnd for bnd in boundaries[:-1] if start < bnd < end]

    def _placement_ok(ti: int, start: int, end: int) -> tuple[bool, int]:
        t = types[ti]
        inner = _inner_boundaries(start, end)
        welds = len(inner)
        if welds > t["max_joints"]:
            return False, welds
        if welds:
            cuts = [start, *inner, end]
            if any(b - a < min_seg for a, b in zip(cuts, cuts[1:])):
                return False, welds
            forbidden = t["forbidden"]
            for bnd in inner:
                rel = bnd - start
                if not (0 < rel < t["length"]):
                    return False, welds
                if any(s <= rel <= e for s, e in forbidden):
                    return False, welds
        return True, welds

    results: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()
    order = sorted(range(len(types)), key=lambda i: types[i]["length"], reverse=True)

    def _recurse(cursor: int, chain: list[int], joints: int) -> None:
        if len(results) >= per_plan_cap:
            return
        if chain:
            key = tuple(sorted(chain))
            if key not in seen:
                seen.add(key)
                results.append(
                    {
                        "parts": key,
                        "layout": tuple(chain),
                        "joints": joints,
                        "material": total_material,
                    }
                )
        for ti in order:
            seg_end = cursor + types[ti]["length"]
            if seg_end > total_usable:
                continue
            ok, welds = _placement_ok(ti, cursor, seg_end)
            if not ok:
                continue
            _recurse(seg_end + kerf, chain + [ti], joints + welds)
            if len(results) >= per_plan_cap:
                return

    _recurse(0, [], 0)
    return results


def _bar_multisets(stock_lengths: list[int], alpha: int, cap: int = _MULTISET_CAP):
    """Yield multisets of 1..alpha bars drawn from ``stock_lengths`` (lazy)."""
    emitted = 0
    for size in range(1, alpha + 1):
        for combo in combinations_with_replacement(stock_lengths, size):
            yield tuple(sorted(combo, reverse=True))
            emitted += 1
            if emitted >= cap:
                return


def _solve_equiv_csp(
    group, time_limit: float, alpha_cap: int, *, ignore_constraints: bool = False
) -> dict[str, Any]:
    """Solve the equivalent-stock CSP; returns raw solve dict for conversion."""
    from pyscipopt import Model, quicksum  # type: ignore[import-not-found]

    started = time.monotonic()
    kerf = group.blade_margin
    min_seg = max(group.min_cut_length, group.min_weld_distance, 1)
    stock_lengths = sorted({s.length for s in group.stocks})
    stock_qty = {s.length: s.quantity for s in group.stocks}

    pipe_types: dict[tuple, dict[str, Any]] = {}
    for p in group.pipes:
        fb = () if ignore_constraints else tuple((iv.start, iv.end) for iv in p.forbidden)
        key = (p.length, fb, p.max_joints)
        entry = pipe_types.setdefault(
            key,
            {"length": p.length, "forbidden": fb, "max_joints": p.max_joints,
             "demand": 0},
        )
        entry["demand"] += p.demand
    types = list(pipe_types.values())
    demand: dict[int, int] = defaultdict(int)
    for t in types:
        demand[t["length"]] += t["demand"]

    total_bars = sum(stock_qty.values())
    total_stock_len = sum(length * qty for length, qty in stock_qty.items())
    total_demand_len = sum(length * qty for length, qty in demand.items())
    tightness = total_demand_len / total_stock_len if total_stock_len else 1.0
    if tightness >= 0.97:
        want = total_bars
    elif tightness >= 0.90:
        want = 6
    else:
        want = 4
    alpha = max(1, min(alpha_cap, want, total_bars))

    gen_deadline = started + min(time_limit * 0.4, _GEN_WINDOW_CAP)
    best_by_parts: dict[tuple, dict[str, Any]] = {}
    for bars in _bar_multisets(stock_lengths, alpha):
        if time.monotonic() > gen_deadline:
            break
        usage: dict[int, int] = defaultdict(int)
        for b in bars:
            usage[b] += 1
        plans = _enumerate_equiv_cutplans(bars, kerf, types, min_seg,
                                          per_plan_cap=_PER_PLAN_CAP)
        for plan in plans:
            key = (plan["parts"], tuple(sorted(usage.items())))
            prev = best_by_parts.get(key)
            cand = {**plan, "bars": dict(usage)}
            if prev is None or (plan["material"], plan["joints"]) < (
                prev["material"], prev["joints"]
            ):
                best_by_parts[key] = cand
        if len(best_by_parts) > _COL_BUDGET:
            break
        if time.monotonic() - started > time_limit * 0.5:
            break
    columns = list(best_by_parts.values())
    gen_elapsed = time.monotonic() - started
    if not columns:
        return {"status": "NO_COLUMNS", "elapsed": round(gen_elapsed, 2)}

    model = Model("route2-equiv-csp")
    model.hideOutput()
    x = [model.addVar(vtype="INTEGER", lb=0, name=f"x{i}") for i in range(len(columns))]
    for ti, t in enumerate(types):
        model.addCons(
            quicksum(col["parts"].count(ti) * x[i] for i, col in enumerate(columns))
            >= t["demand"]
        )
    for s in stock_lengths:
        model.addCons(
            quicksum(col["bars"].get(s, 0) * x[i] for i, col in enumerate(columns))
            <= stock_qty[s]
        )
    if not ignore_constraints:
        # Must-use is a group-level priority: available bars may be used only
        # after every must-use bar is consumed (see verifier).  Mirror the
        # baseline MILP's split-and-gate encoding so a small demand served from
        # must-use stock is not forced to exhaust the must-use inventory.
        must_use_total = sum(s.must_use_quantity for s in group.stocks)
        if must_use_total > 0:
            usage_expr = {
                s.length: quicksum(
                    col["bars"].get(s.length, 0) * x[i]
                    for i, col in enumerate(columns)
                )
                for s in group.stocks
            }
            z_free = model.addVar(vtype="BINARY", name="use_available_stock")
            used_must_terms = []
            for stock in group.stocks:
                avail = stock.quantity - stock.must_use_quantity
                u_len = model.addVar(
                    vtype="INTEGER", lb=0, ub=stock.must_use_quantity,
                    name=f"mu_used_{stock.length}",
                )
                a_len = model.addVar(
                    vtype="INTEGER", lb=0, ub=max(0, avail),
                    name=f"av_used_{stock.length}",
                )
                model.addCons(u_len + a_len == usage_expr[stock.length])
                if avail > 0:
                    model.addCons(a_len <= avail * z_free)
                else:
                    model.addCons(a_len == 0)
                used_must_terms.append(u_len)
            model.addCons(quicksum(used_must_terms) >= must_use_total * z_free)

    material = quicksum(col["material"] * x[i] for i, col in enumerate(columns))
    joints = quicksum(col["joints"] * x[i] for i, col in enumerate(columns))

    best_mat = best_j = None
    status = "UNKNOWN"
    for phase, (obj, sense) in enumerate(((material, "minimize"), (joints, "minimize"))):
        remaining = time_limit - (time.monotonic() - started)
        if remaining <= 0.05:
            status = "TIME_LIMIT_INCOMPLETE"
            break
        phase_limit = remaining * 0.5 if phase == 0 else remaining
        model.setRealParam("limits/time", max(0.05, phase_limit))
        model.setObjective(obj, sense)
        model.optimize()
        status = str(model.getStatus()).upper()
        if model.getNSols() <= 0:
            if best_mat is None:
                return {"status": f"UNSOLVED({status})", "columns": len(columns),
                        "alpha": alpha, "gen_s": round(gen_elapsed, 2),
                        "elapsed": round(time.monotonic() - started, 2)}
            break
        sol = model.getBestSol()
        val = int(round(model.getSolVal(sol, obj)))
        if phase == 0:
            best_mat = val
            model.freeTransform()
            model.addCons(obj <= val)
        else:
            best_j = val

    if best_mat is not None and best_j is not None:
        final = "OPTIMAL" if status == "OPTIMAL" else "FEASIBLE"
    else:
        final = status

    chosen: list[tuple[dict[str, Any], int]] = []
    if best_mat is not None:
        sol = model.getBestSol()
        for i, col in enumerate(columns):
            mult = int(round(model.getSolVal(sol, x[i])))
            if mult > 0:
                chosen.append((col, mult))

    return {
        "status": final,
        "material": best_mat,
        "joints": best_j,
        "columns": len(columns),
        "alpha": alpha,
        "gen_s": round(gen_elapsed, 2),
        "elapsed": round(time.monotonic() - started, 2),
        "chosen": chosen,
        "kerf": kerf,
        "min_seg": min_seg,
        "min_weld": group.min_weld_distance,
        "min_cut": group.min_cut_length,
        "stock_qty": dict(stock_qty),
        "demand": dict(demand),
        "types": types,
        "must_use": {s.length: s.must_use_quantity
                     for s in group.stocks if s.must_use_quantity > 0},
    }


def _convert_to_workorder(result: dict[str, Any]) -> dict[str, Any]:
    """Step-3: replay the equivalent cutting plan into a concrete cut+weld work
    order and re-check every production red line on the concrete geometry.

    Returns {ok, reason, orders, bars_used, produced}; each order carries the
    real bars used, per-pipe stubs (+type), and per-bar cut segments -- the
    two-sided decomposition the production verifier balances.
    """
    kerf = result["kerf"]
    min_weld = result["min_weld"]
    min_cut = result["min_cut"]
    stock_qty = result["stock_qty"]
    types = result["types"]
    must_use = result.get("must_use", {})
    weld_floor = max(min_cut, min_weld)

    bars_used: dict[int, int] = defaultdict(int)
    produced_type: dict[int, int] = defaultdict(int)
    orders: list[dict[str, Any]] = []

    for col, mult in result.get("chosen", []):
        bar_lengths = tuple(
            length
            for length, n in sorted(col["bars"].items(), reverse=True)
            for _ in range(n)
        )
        boundaries: list[int] = []
        pos = 0
        for i, b in enumerate(bar_lengths):
            pos += b - (kerf if i > 0 else 0)
            boundaries.append(pos)
        total_usable = boundaries[-1]

        chain = list(col.get("layout") or sorted(col["parts"]))
        cursor = 0
        pipe_segments: list[dict[str, Any]] = []
        n_bars = len(bar_lengths)
        bar_segments: list[list[int]] = [[] for _ in range(n_bars)]

        def _bar_index(offset: int) -> int:
            for bi, bnd in enumerate(boundaries):
                if offset < bnd:
                    return bi
            return n_bars - 1

        for ti in chain:
            t = types[ti]
            pl = t["length"]
            start, end = cursor, cursor + pl
            if end > total_usable + 1e-6:
                return {"ok": False, "reason": "LAYOUT_OVERFLOW",
                        "detail": f"pipe {pl} exceeds equiv stock {total_usable}"}
            inner = [bnd for bnd in boundaries[:-1] if start < bnd < end]
            if len(inner) > t["max_joints"]:
                return {"ok": False, "reason": "OVER_JOINTS",
                        "detail": f"pipe {pl}: {len(inner)} welds > cap "
                                  f"{t['max_joints']}"}
            cuts = [start, *inner, end]
            stubs = [b - a for a, b in zip(cuts, cuts[1:])]
            if inner and any(s < weld_floor for s in stubs):
                return {"ok": False, "reason": "SHORT_STUB",
                        "detail": f"pipe {pl}: welded stub {min(stubs)} "
                                  f"< floor {weld_floor}"}
            if pl < min_cut:
                return {"ok": False, "reason": "SHORT_CUT",
                        "detail": f"pipe {pl} < min_cut {min_cut}"}
            for bnd in inner:
                rel = bnd - start
                if any(s <= rel <= e for s, e in t["forbidden"]):
                    return {"ok": False, "reason": "FORBIDDEN_WELD",
                            "detail": f"pipe {pl}: weld at {rel} in forbidden zone"}
            for a, b in zip(cuts, cuts[1:]):
                bar_segments[_bar_index(a)].append(b - a)
            pipe_segments.append(
                {"type": ti, "pipe": pl, "welds": len(inner),
                 "stubs": tuple(stubs)}
            )
            cursor = end + kerf

        for _ in range(mult):
            for length in bar_lengths:
                bars_used[length] += 1
            for seg in pipe_segments:
                produced_type[seg["type"]] += 1
            orders.append({
                "bars": list(bar_lengths),
                "pipes": pipe_segments,
                "bar_segments": [list(s) for s in bar_segments],
            })

    for length, used in bars_used.items():
        if used > stock_qty.get(length, 0):
            return {"ok": False, "reason": "STOCK_OVERUSE",
                    "detail": f"length {length}: used {used} > stock "
                              f"{stock_qty.get(length, 0)}"}
    # Group-level must-use priority: available bars usable only after all
    # must-use bars are consumed (see verifier).  Small demand fully served from
    # must-use stock is fine even if must-use is not fully exhausted.
    must_use_total = sum(must_use.values())
    if must_use_total > 0:
        used_must = sum(
            min(bars_used.get(length, 0), need) for length, need in must_use.items()
        )
        used_available = sum(
            max(0, used - must_use.get(length, 0))
            for length, used in bars_used.items()
        )
        if used_available > 0 and used_must < must_use_total:
            return {"ok": False, "reason": "MUST_USE_PRIORITY",
                    "detail": f"used {used_available} available bars while "
                              f"must_use {used_must}/{must_use_total} unfilled"}
    for ti, t in enumerate(types):
        if produced_type.get(ti, 0) < t["demand"]:
            return {"ok": False, "reason": "DEMAND_SHORT",
                    "detail": f"type {ti} (len {t['length']}): made "
                              f"{produced_type.get(ti, 0)} < need {t['demand']}"}

    return {
        "ok": True,
        "reason": "OK",
        "orders": orders,
        "bars_used": dict(bars_used),
        "produced": dict(produced_type),
    }


def _build_group_result(group, result: dict[str, Any], conv: dict[str, Any],
                        elapsed: float, solve_status: str) -> dict[str, Any]:
    """Map a successful conversion to a production group-result dict."""
    from .solver import _WeldCandidate, _CutCandidate, _assemble_group_result

    kerf = group.blade_margin
    kerf_mode = group.kerf_mode
    types = result["types"]

    def _type_key(p) -> tuple:
        return (p.length, tuple((iv.start, iv.end) for iv in p.forbidden),
                p.max_joints)

    type_of_key: dict[tuple, int] = {}
    for ti, t in enumerate(types):
        type_of_key[(t["length"], t["forbidden"], t["max_joints"])] = ti
    pipes_by_type: dict[int, list[int]] = defaultdict(list)
    remaining: dict[int, int] = {}
    for idx, p in enumerate(group.pipes):
        ti = type_of_key[_type_key(p)]
        pipes_by_type[ti].append(idx)
        remaining[idx] = p.demand

    weld_counter: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    cut_counter: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)

    def _assign_pipe(ti: int) -> int:
        for idx in pipes_by_type[ti]:
            if remaining.get(idx, 0) > 0:
                remaining[idx] -= 1
                return idx
        return pipes_by_type[ti][0]

    # The MILP covers demand with '>=' so a type may be over-produced; the
    # verifier requires produced == demand per pipe_id.  Trim surplus WHOLE
    # (unwelded, single-segment) pipes: their one segment lives on one bar, so
    # removing the weld row and that cut segment keeps cut/weld balance and just
    # frees bar remainder.  Welded surplus is left in place (rare; would fall
    # through as a mismatch and this engine's result is then rejected).
    demand_by_type = {ti: t["demand"] for ti, t in enumerate(types)}
    produced_by_type: Counter = Counter()
    for order in conv["orders"]:
        for seg in order["pipes"]:
            produced_by_type[seg["type"]] += 1
    surplus = {ti: max(0, produced_by_type[ti] - demand_by_type.get(ti, 0))
               for ti in produced_by_type}

    for order in conv["orders"]:
        bar_segs = [list(s) for s in order["bar_segments"]]
        kept_pipes = []
        for seg in order["pipes"]:
            ti = seg["type"]
            stubs = tuple(seg["stubs"])
            if surplus.get(ti, 0) > 0 and len(stubs) == 1:
                seg_len = stubs[0]
                for segs in bar_segs:
                    if seg_len in segs:
                        segs.remove(seg_len)
                        surplus[ti] -= 1
                        break
                else:
                    kept_pipes.append(seg)
            else:
                kept_pipes.append(seg)
        for bar_len, segs in zip(order["bars"], bar_segs):
            parts = tuple(sorted(segs))
            if parts:
                cut_counter[(bar_len, parts)] += 1
        for seg in kept_pipes:
            idx = _assign_pipe(seg["type"])
            weld_counter[(idx, tuple(seg["stubs"]))] += 1

    weld_counts = [
        (_WeldCandidate(pipe_index=idx, parts=parts), qty)
        for (idx, parts), qty in weld_counter.items()
    ]
    cut_counts = [
        (_CutCandidate(stock_length=slen, parts=parts, kerf=kerf,
                       kerf_mode=kerf_mode), qty)
        for (slen, parts), qty in cut_counter.items()
    ]
    return _assemble_group_result(
        group, "route2-equiv", solve_status, weld_counts, cut_counts, elapsed
    )


def _synth_group_payload(group) -> dict[str, Any]:
    """Rebuild a minimal single-group payload from the group's own fields.

    ``MaterialGroup`` does not carry its source JSON, so to run the production
    verifier we reconstruct a payload equivalent to the one that produced it:
    same lengths (integer mm), demands, forbidden zones, per-pipe joint caps,
    stock quantities/must-use, and the group's kerf/weld/cut parameters.  The
    verifier re-derives everything else from this, exactly as for the MILP path.
    """
    from .domain import from_units

    pipes = []
    for p in group.pipes:
        pipes.append({
            "figure_number": p.figure_number,
            "Parent_node": p.parent_node,
            "jlxh": p.jlxh,
            "cube_no": p.cube_no,
            "pipe_length": from_units(p.length),
            "pipe_demand": p.demand,
            "Max_Weldingjoint_Number": p.max_joints,
            "Unweldable_Area": [
                [from_units(iv.start), from_units(iv.end)] for iv in p.forbidden
            ],
        })
    stocks = []
    for s in group.stocks:
        row = {"stock_length": from_units(s.length), "stock_demand": s.quantity}
        if s.must_use_quantity:
            row["must_use"] = s.must_use_quantity
        stocks.append(row)
    return {
        "material": group.material,
        "specifications": group.specifications,
        "NestParam": {"BladeMargin": from_units(group.blade_margin)},
        "Min_Welding_Length": from_units(group.min_weld_distance),
        "Min_Cut_Length": from_units(group.min_cut_length),
        "Min_Reusable_Remnant_Length": from_units(group.min_reusable_remnant),
        "KerfMode": group.kerf_mode,
        "Target_Util_Rate": group.target_rate,
        "Pipe": pipes,
        "Stock": stocks,
    }


def _self_verify(group, gres: dict[str, Any]) -> bool:
    """Run the *production* verifier on this single-group result.

    Route-2 is an alternative engine; a bug in its schema mapping (e.g. an
    unmet must_use quantity for a degenerate instance) must never surface as a
    verifier-failing production solution.  We reconstruct the exact one-group
    solution the service would emit and check it against the real verifier over
    a payload synthesised from the group, returning ``False`` on any error.
    """
    from .verifier import verify_solution
    from .solver import from_units

    m = gres["metrics"]
    norm_paths = {rec["path"] for rec in gres.get("input_normalizations", [])}
    solution = {
        "status": "FEASIBLE",
        "task_id": "route2-self-check",
        "groups": [gres],
        "summary": {
            "group_count": 1,
            "demand_length": from_units(group.demand_length),
            "used_stock_length": m["used_stock_length"],
            "utilization_rate": m["utilization_rate"],
            "welding_joint_quantity": m["welding_joint_quantity"],
            "welding_pattern_type_quantity": m["welding_pattern_type_quantity"],
            "cutting_pattern_type_quantity": m["cutting_pattern_type_quantity"],
            "reused_welding_pattern_type_quantity":
                m["reused_welding_pattern_type_quantity"],
            "reused_cutting_pattern_type_quantity":
                m["reused_cutting_pattern_type_quantity"],
            "kerf_loss": m["kerf_loss"],
            "remainder_length": m["remainder_length"],
            "must_use_stock_quantity": m["must_use_stock_quantity"],
            "must_use_used_quantity": m["must_use_used_quantity"],
            "must_use_stock_length": m["must_use_stock_length"],
            "normalized_length_field_quantity": len(norm_paths),
            "target_reached": m["target_reached"],
        },
        "verification": {"passed": None, "issues": [], "source": "pending"},
    }
    try:
        report = verify_solution(_synth_group_payload(group), solution)
    except Exception:
        return False
    return bool(report.get("passed"))


def solve_group(group, time_limit: float, *, alpha_cap: int = 12
                ) -> dict[str, Any] | None:
    """Solve one MaterialGroup via the equivalent-stock CSP.

    Returns a production group-result dict (same schema as the two-layer MILP)
    or ``None`` if this engine could not produce a verifiable solution within
    the budget.  Never raises for solver reasons.
    """
    started = time.monotonic()
    try:
        result = _solve_equiv_csp(group, time_limit, alpha_cap)
    except Exception:  # pyscipopt missing / model error -> engine unavailable
        return None
    if result.get("status") not in {"OPTIMAL", "FEASIBLE"}:
        return None
    conv = _convert_to_workorder(result)
    if not conv.get("ok"):
        return None
    try:
        gres = _build_group_result(
            group, result, conv, time.monotonic() - started, result["status"]
        )
    except Exception:
        return None
    # Final gate: only hand back a result the production verifier accepts, so a
    # schema-mapping gap can never regress the incumbent through selection.
    if not _self_verify(group, gres):
        return None
    return gres
