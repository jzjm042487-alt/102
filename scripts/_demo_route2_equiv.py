"""Route-2-NEW demo: welding-CSP reduced to an EQUIVALENT standard CSP.

Source: Ilyés & Balogh, "The Effect of Welding on the 1D-CSP" (Hindawi 2019).
Idea: build an "equivalent stock" by welding up to ``alpha = max_joints+1`` real
bars end-to-end; a pipe is cut from a contiguous span of ONE equivalent stock,
and the number of welds it incurs equals the number of bar-boundaries its span
crosses.  This collapses the two echelons (cut x weld) into a SINGLE-LAYER CSP
over equivalent stocks -- exactly the pattern class my earlier (wrong) route-2
demo lacked.  GPU parallel pattern generation would apply to this layer.

This demo tests, per real group:
  * baseline = current two-layer solver (app.service.solve_and_verify)
  * route2new = single-layer CSP over equivalent-stock cut patterns (SCIP),
                minimising material then welds (boundaries crossed)

It reports solve-rate and, for solved groups, whether route2new's weld count is
<= baseline's (a proxy for the "pattern conversion succeeded minimally").

Scope: matches the user's constraint -- FIXED stock only (no procurement).  The
equivalent-stock count is bounded by capping how many equivalent stocks we
materialise per (multiset of bar lengths) and by alpha.

Usage:
    python scripts/_demo_route2_equiv.py [--sample 30] [--seed 7]
        [--limit 12] [--time-limit 10] [--blade-margin 0] [--alpha-cap 4]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
for p in (BACKEND, REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmark_against_software import _clean_payload, _load_records  # noqa: E402


def _enumerate_equiv_cutplans(
    bar_lengths: tuple[int, ...],
    kerf: int,
    types: list[dict[str, Any]],
    min_seg: int,
    per_plan_cap: int,
) -> list[dict[str, Any]]:
    """Cut plans over ONE equivalent stock = bars welded end-to-end.

    The equivalent stock is the concatenation ``bar_lengths`` with kerf between
    welded joins.  Pipes (identified by TYPE = length+forbidden+max_joints) are
    laid head-to-tail (kerf between cuts) and, for each placed pipe, we count how
    many bar-boundaries its [start, end) span crosses -> that many welds.  A pipe
    is legal only if:
      * boundaries-crossed <= its max_joints,
      * every welded stub >= min_seg,
      * every weld position (relative to the pipe start) is weld-allowed, i.e.
        NOT inside any of the pipe type's forbidden zones.

    ``types`` is a list of {length, forbidden(tuple of (s,e)), max_joints}.
    Returns list of {parts, layout, joints, material}, where parts/layout hold
    TYPE INDICES (into ``types``), so the caller can map back to demand + verify.
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
        """Return (legal?, welds) for placing type ``ti`` at [start, end)."""
        t = types[ti]
        inner = _inner_boundaries(start, end)
        welds = len(inner)
        if welds > t["max_joints"]:
            return False, welds
        if welds:
            cuts = [start, *inner, end]
            if any(b - a < min_seg for a, b in zip(cuts, cuts[1:])):
                return False, welds  # stub too short
            forbidden = t["forbidden"]
            for bnd in inner:
                rel = bnd - start  # weld position relative to pipe start
                if not (0 < rel < t["length"]):
                    return False, welds
                if any(s <= rel <= e for s, e in forbidden):
                    return False, welds  # weld lands in a forbidden zone
        return True, welds

    results: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()
    order = sorted(range(len(types)), key=lambda i: types[i]["length"], reverse=True)

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


def _bar_multisets(
    stock_lengths: list[int], alpha: int, cap: int = 20000
):
    """Yield multisets of 1..alpha bars drawn from ``stock_lengths``.

    Emitted in non-decreasing size order and bounded by ``cap`` total multisets
    so deep alpha over several distinct lengths cannot blow up.  Yielding lazily
    lets the caller stop early on its own time/column budget.
    """

    from itertools import combinations_with_replacement

    emitted = 0
    for size in range(1, alpha + 1):
        for combo in combinations_with_replacement(stock_lengths, size):
            yield tuple(sorted(combo, reverse=True))
            emitted += 1
            if emitted >= cap:
                return


def _solve_route2new(
    group, time_limit: float, alpha_cap: int, *, ignore_constraints: bool = False
) -> dict[str, Any]:
    from pyscipopt import Model, quicksum  # type: ignore[import-not-found]

    started = time.monotonic()
    kerf = group.blade_margin
    min_seg = max(group.min_cut_length, group.min_weld_distance, 1)
    stock_lengths = sorted({s.length for s in group.stocks})
    stock_qty = {s.length: s.quantity for s in group.stocks}

    # A "pipe type" bundles everything that makes two demands weld-distinct:
    # length + forbidden-weld zones + max_joints.  Two pipes of equal length but
    # different forbidden zones are DIFFERENT types (7/60 real groups need this).
    # forbidden zones are measured from the pipe's own start.
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
    # Legacy views still used by the model constraints / adaptive alpha.
    demand = defaultdict(int)
    for t in types:
        demand[t["length"]] += t["demand"]

    # alpha = how many real bars concatenate into ONE equivalent stock (the
    # "pooling depth" for leftovers).  It is NOT max_joints+1: the per-pipe weld
    # cap is enforced separately in _welds_for_span during enumeration.  A larger
    # alpha lets short leftovers on many bars be welded together to lift yield.
    #
    # Adaptive: ultra-tight groups (demand/stock -> 1.0) need deep pooling to be
    # feasible at all, so scale alpha toward the total bar count as tightness
    # rises; loose groups stay shallow to avoid column explosion.  alpha_cap is a
    # hard ceiling that bounds the enumeration cost regardless.
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

    # Enumerate equivalent stocks (multisets of bars) and their cut plans.
    # To avoid combinatorial blow-up at large alpha we (a) cap total columns and
    # (b) keep, per distinct pipe-multiset "parts", only the cheapest realisation
    # (least material, then fewest joints) -- a Best-Waste style dedup that keeps
    # the model small without discarding any dominating column.
    col_budget = 8000
    gen_deadline = started + min(time_limit * 0.4, 6.0)
    best_by_parts: dict[tuple[int, ...], dict[str, Any]] = {}
    for bars in _bar_multisets(stock_lengths, alpha):
        if time.monotonic() > gen_deadline:
            break  # hard generation wall-clock guard
        usage = defaultdict(int)
        for b in bars:
            usage[b] += 1
        plans = _enumerate_equiv_cutplans(
            bars, kerf, types, min_seg, per_plan_cap=200
        )
        for plan in plans:
            key = (plan["parts"], tuple(sorted(usage.items())))
            prev = best_by_parts.get(key)
            cand = {**plan, "bars": dict(usage)}
            if prev is None or (plan["material"], plan["joints"]) < (
                prev["material"], prev["joints"]
            ):
                best_by_parts[key] = cand
        if len(best_by_parts) > col_budget:
            break
        if time.monotonic() - started > time_limit * 0.5:
            break  # generation budget guard
    columns = list(best_by_parts.values())
    gen_elapsed = time.monotonic() - started
    if not columns:
        return {"status": "NO_COLUMNS", "elapsed": round(gen_elapsed, 2)}

    model = Model("route2new-equiv-csp")
    model.hideOutput()

    x = [model.addVar(vtype="INTEGER", lb=0, name=f"x{i}") for i in range(len(columns))]
    # Demand cover per pipe TYPE (parts hold type indices).
    for ti, t in enumerate(types):
        model.addCons(
            quicksum(col["parts"].count(ti) * x[i] for i, col in enumerate(columns))
            >= t["demand"]
        )
    # Stock cap: total real bars of each length used across chosen equiv stocks.
    for s in stock_lengths:
        model.addCons(
            quicksum(col["bars"].get(s, 0) * x[i] for i, col in enumerate(columns))
            <= stock_qty[s]
        )
    # must_use: at least ``must_use_quantity`` bars of that length must be used.
    if not ignore_constraints:
        for stock in group.stocks:
            if stock.must_use_quantity > 0:
                model.addCons(
                    quicksum(
                        col["bars"].get(stock.length, 0) * x[i]
                        for i, col in enumerate(columns)
                    )
                    >= stock.must_use_quantity
                )

    material = quicksum(col["material"] * x[i] for i, col in enumerate(columns))
    joints = quicksum(col["joints"] * x[i] for i, col in enumerate(columns))

    best_mat = best_j = None
    status = "UNKNOWN"
    # Phase 0: minimise material. Phase 1: fix material to its optimum (or best
    # found) and minimise joints. Split remaining time so phase 1 always runs.
    for phase, (obj, sense) in enumerate(((material, "minimize"), (joints, "minimize"))):
        remaining = time_limit - (time.monotonic() - started)
        if remaining <= 0.05:
            status = "TIME_LIMIT_INCOMPLETE"
            break
        # Reserve ~half the remaining budget for the joints phase.
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
            # Bound material to what we achieved (optimum if OPTIMAL, else the
            # best feasible), then minimise joints under that cap.
            model.freeTransform()
            model.addCons(obj <= val)
        else:
            best_j = val

    # Report OPTIMAL only if the joints phase proved optimality; otherwise we
    # still hold a material-optimal, joints-feasible plan -> FEASIBLE.
    if best_mat is not None and best_j is not None:
        final = "OPTIMAL" if status == "OPTIMAL" else "FEASIBLE"
    else:
        final = status

    # Extract the chosen columns with multiplicities (for step-3 conversion).
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
    """Step-3 of Ilyes-Balogh: turn the equivalent cutting plan back into a
    concrete cut-and-weld work order and VERIFY its legality.

    For each chosen equivalent-stock column we replay the concrete layout: real
    bars laid in a fixed (descending-length) order, pipes cut head-to-tail
    largest-first (the same order the enumerator used).  We then read off, per
    pipe, which real bar(s) each segment comes from and where the welds land, and
    re-check every production red line on the CONCRETE geometry:

      * welds per pipe <= its max_joints
      * every welded stub >= min_seg (min_weld_distance / min_cut)
      * total real bars consumed per length <= available stock
      * demand exactly covered (>=)

    A failure of any check = a step-3 "pattern conversion" failure (the risk the
    paper flags).  Returns {ok, reason, orders, bars_used, produced}.
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
        # Boundary offsets on the concrete equivalent stock (weld consumes kerf).
        boundaries: list[int] = []
        pos = 0
        for i, b in enumerate(bar_lengths):
            pos += b - (kerf if i > 0 else 0)
            boundaries.append(pos)
        total_usable = boundaries[-1]

        chain = list(col.get("layout") or sorted(col["parts"]))
        cursor = 0
        pipe_segments: list[dict[str, Any]] = []
        # Per-bar cut segments: which pieces get cut out of each real bar in the
        # chain.  A pipe crossing a boundary contributes one stub to each bar it
        # spans -> those stubs are the segments the cutting stage must produce,
        # and welding then joins them back into the pipe.  This is the two-sided
        # (cut vs weld) decomposition the production verifier balances.
        n_bars = len(bar_lengths)
        bar_segments: list[list[int]] = [[] for _ in range(n_bars)]

        def _bar_index(offset: int) -> int:
            # Which bar span contains a point at global ``offset`` (0-based).
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
            # A welded stub must satisfy BOTH min_cut and min_weld_distance.
            # An unwelded whole pipe is bound only by min_cut_length.
            if inner and any(s < weld_floor for s in stubs):
                return {"ok": False, "reason": "SHORT_STUB",
                        "detail": f"pipe {pl}: welded stub {min(stubs)} "
                                  f"< floor {weld_floor}"}
            if pl < min_cut:
                return {"ok": False, "reason": "SHORT_CUT",
                        "detail": f"pipe {pl} < min_cut {min_cut}"}
            # Forbidden-weld zones: each weld position, relative to the pipe
            # start, must not fall inside any of this type's forbidden zones.
            for bnd in inner:
                rel = bnd - start
                if any(s <= rel <= e for s, e in t["forbidden"]):
                    return {"ok": False, "reason": "FORBIDDEN_WELD",
                            "detail": f"pipe {pl}: weld at {rel} in forbidden zone"}
            # Assign each stub to the bar span it lies in (segment start -> bar).
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

    # Stock non-overuse.
    for length, used in bars_used.items():
        if used > stock_qty.get(length, 0):
            return {"ok": False, "reason": "STOCK_OVERUSE",
                    "detail": f"length {length}: used {used} > stock "
                              f"{stock_qty.get(length, 0)}"}
    # must_use satisfied.
    for length, need in must_use.items():
        if bars_used.get(length, 0) < need:
            return {"ok": False, "reason": "MUST_USE_SHORT",
                    "detail": f"length {length}: used {bars_used.get(length, 0)} "
                              f"< must_use {need}"}
    # Demand coverage per pipe TYPE.
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


def _solve_baseline(problem_json: str, time_limit: float, blade_margin) -> dict[str, Any]:
    os.environ["NESTING_ACCEL"] = "cpu"
    from app.accel import select_provider

    select_provider.cache_clear()
    from app.service import solve_and_verify

    payload = _clean_payload(json.loads(problem_json))
    if blade_margin is not None:
        payload["BladeMargin"] = blade_margin
    t0 = time.monotonic()
    result = solve_and_verify(payload, time_limit_seconds=time_limit)
    elapsed = time.monotonic() - t0
    g = result.get("groups", [])
    m = g[0].get("metrics", {}) if g else {}
    return {
        "status": m.get("solve_status"),
        "joints": m.get("welding_joint_quantity"),
        "elapsed": round(elapsed, 2),
    }


def _parse_group(problem_json: str, blade_margin):
    from app.domain import parse_problem

    payload = _clean_payload(json.loads(problem_json))
    if blade_margin is not None:
        payload["BladeMargin"] = blade_margin
    problem = parse_problem(payload)
    return problem.groups[0] if problem.groups else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sample", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--time-limit", type=float, default=10.0)
    p.add_argument("--blade-margin", type=float, default=0.0)
    p.add_argument("--alpha-cap", type=int, default=4)
    return p


def main(argv: list[str] | None = None) -> int:
    import random

    args = build_parser().parse_args(argv)
    records = _load_records()
    cands = [
        r for r in records
        if r.get("MOMCALCULATESTATUS") == "1"
        and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")
    ]
    cands = random.Random(args.seed).sample(cands, min(args.sample, len(cands)))
    picked = cands[: args.limit]

    print(f"Route-2-NEW (equiv-stock CSP) demo: {len(picked)} groups, "
          f"time_limit={args.time_limit}s, blade={args.blade_margin}, "
          f"alpha_cap={args.alpha_cap}\n")
    r2_ok = base_ok = both = r2_wins = base_wins = neither = 0
    j_better = j_worse = j_equal = 0
    conv_ok = conv_fail = 0
    conv_reasons: dict[str, int] = defaultdict(int)
    for i, r in enumerate(picked):
        spec = f'{r.get("MOMMATERIAL","")}/{r.get("MOMOUTSIDEDIAMETER","")}x{r.get("MOMWALLTHICKNESS","")}'
        pj = r["MOMPROBLEMJSON"]
        try:
            base = _solve_baseline(pj, args.time_limit, args.blade_margin)
            group = _parse_group(pj, args.blade_margin)
            if group is None:
                print(f"  [{i+1}] {spec} | no group")
                continue
            r2 = _solve_route2new(group, args.time_limit, args.alpha_cap)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}] {spec} ERROR {type(exc).__name__}: {exc}")
            continue

        b_ok = base["status"] in {
            "OPTIMAL_LEXICOGRAPHIC", "OPTIMAL", "FEASIBLE",
            "FEASIBLE_TARGET_REACHED", "TARGET_REACHED",
        }
        r_ok = r2["status"] in {"OPTIMAL", "FEASIBLE"}
        # Step-3: convert the equivalent plan into a concrete cut-weld work order.
        conv = {"ok": None, "reason": "-"}
        if r_ok:
            conv = _convert_to_workorder(r2)
            if conv["ok"]:
                conv_ok += 1
            else:
                conv_fail += 1
                conv_reasons[conv["reason"]] += 1
        base_ok += int(b_ok)
        r2_ok += int(r_ok)
        if b_ok and r_ok:
            both += 1
            if base["joints"] is not None and r2["joints"] is not None:
                if r2["joints"] < base["joints"]:
                    j_better += 1
                elif r2["joints"] > base["joints"]:
                    j_worse += 1
                else:
                    j_equal += 1
        elif r_ok and not b_ok:
            r2_wins += 1
        elif b_ok and not r_ok:
            base_wins += 1
        else:
            neither += 1
        conv_str = ""
        if r_ok:
            conv_str = f" conv={'OK' if conv['ok'] else conv['reason']}"
        print(
            f"  [{i+1}] {spec} | base={base['status']}(j={base['joints']},{base['elapsed']}s) "
            f"r2new={r2['status']}(mat={r2.get('material')},j={r2.get('joints')},"
            f"a={r2.get('alpha')},cols={r2.get('columns')},{r2['elapsed']}s){conv_str}"
        )

    conv_total = conv_ok + conv_fail
    conv_reason_str = ", ".join(f"{k}:{v}" for k, v in conv_reasons.items()) or "-"
    print(
        f"\n============ Route-2-NEW demo 汇总 ============\n"
        f"baseline 解出: {base_ok}/{len(picked)}\n"
        f"route2new 解出: {r2_ok}/{len(picked)}\n"
        f"  both-OK: {both}  (其中焊口 r2更少:{j_better} 相等:{j_equal} 更多:{j_worse})\n"
        f"  route2new 独赢: {r2_wins}\n"
        f"  baseline 独赢: {base_wins}\n"
        f"  both-fail: {neither}\n"
        f"③模式转换成功: {conv_ok}/{conv_total}  失败原因: {conv_reason_str}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
