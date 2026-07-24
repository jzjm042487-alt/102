"""D5 专线：motif 列工厂 + SPEC 字典序 setcover。

目标顺序：joints → weld_types → cut_types → util（used_len）
导出 cuts/welds 布局供 _gen_report HTML 复用。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]


def _lex_tag(ours: dict, legacy: dict | None) -> str:
    if not ours.get("ok") and ours.get("joints") is None:
        return "unsolved"
    if not legacy or legacy.get("joints") is None:
        return "solved_no_legacy"
    oj, lj = int(ours["joints"]), int(legacy["joints"])
    ow, lw = int(ours["weld_types"]), int(legacy.get("weld_types") or 10**9)
    oc, lc = int(ours["cut_types"]), int(legacy.get("cut_types") or 10**9)
    ou = float(ours["util"])
    lu = float(legacy.get("util") or 0.0)
    util_worse = ou + 1e-12 < lu - 1e-3
    util_better = ou > lu + 1e-9
    j_cmp = (oj > lj) - (oj < lj)
    w_cmp = (ow > lw) - (ow < lw)
    c_cmp = (oc > lc) - (oc < lc)
    u_cmp = (1 if util_worse else 0) - (1 if util_better else 0)
    comps = [j_cmp, w_cmp, c_cmp, u_cmp]
    has_better = any(x < 0 for x in comps)
    has_worse = any(x > 0 for x in comps)
    if has_better and has_worse:
        return "mixed"
    if not has_worse:
        return "lex_non_worse"
    return "lex_worse"


def solve_setcover_lex(
    group,
    constructed: dict,
    *,
    tl: float = 120.0,
    util_floor: float | None = None,
    joint_slack: int = 0,
    max_weld_types: int | None = None,
    max_cut_types: int | None = None,
) -> dict[str, Any]:
    from pyscipopt import Model, quicksum

    if not constructed.get("ok"):
        return {"ok": False, "error": constructed.get("error")}

    pipes = group.pipes
    weld_pool = constructed["weld_pool"]
    cut_pool = constructed["cut_pool"]
    stock_qty = constructed["stock_qty"]
    sigma = constructed["sigma"]
    demand = float(group.demand_length)
    uf = float(util_floor) if util_floor is not None else 0.0

    t0 = time.monotonic()
    m = Model("d5_motif_setcover")
    m.hideOutput()
    m.setParam("limits/time", max(1.0, float(tl)))

    xp = {}
    for i, pats in weld_pool.items():
        for k, seq in enumerate(pats):
            xp[(i, k)] = m.addVar(vtype="I", lb=0, name=f"xp_{i}_{k}")
        m.addCons(quicksum(xp[(i, k)] for k in range(len(pats))) == pipes[i].demand)

    yc = {}
    for L, cps in cut_pool.items():
        for k, segs in enumerate(cps):
            yc[(L, k)] = m.addVar(vtype="I", lb=0, name=f"yc_{L}_{k}")
        if cps:
            m.addCons(
                quicksum(yc[(L, k)] for k in range(len(cps))) <= stock_qty.get(L, 0)
            )

    for seg in sigma:
        prod = [
            segs.count(seg) * yc[(L, k)]
            for L, cps in cut_pool.items()
            for k, segs in enumerate(cps)
            if seg in segs
        ]
        cons = [
            seq.count(seg) * xp[(i, k)]
            for i, pats in weld_pool.items()
            for k, seq in enumerate(pats)
            if seg in seq
        ]
        if cons:
            if not prod:
                return {
                    "ok": False,
                    "error": f"seg_{seg}_no_cut",
                    "elapsed_s": round(time.monotonic() - t0, 3),
                }
            m.addCons(quicksum(prod) - quicksum(cons) >= 0)

    joints = quicksum(
        (len(seq) - 1) * xp[(i, k)]
        for i, pats in weld_pool.items()
        for k, seq in enumerate(pats)
    )
    used_len = quicksum(
        L * yc[(L, k)] for L, cps in cut_pool.items() for k in range(len(cps))
    )
    if uf > 0 and demand > 0:
        m.addCons(used_len * uf <= demand + 1e-3)

    # Phase A: minimize joints.  Later phases must retain this incumbent exactly.
    m.setObjective(joints, "minimize")
    m.optimize()
    if m.getNSols() == 0:
        return {
            "ok": False,
            "error": f"status={m.getStatus()}",
            "elapsed_s": round(time.monotonic() - t0, 3),
            "n_weld": constructed["n_weld"],
            "n_cut": constructed["n_cut"],
            "sigma_n": len(sigma),
        }

    best_j = int(round(m.getVal(joints)))
    joint_limit = best_j + max(0, int(joint_slack))
    phase_objectives: dict[str, int | float] = {
        "joints": best_j,
        "joint_limit": joint_limit,
    }
    if max_weld_types is not None:
        phase_objectives["weld_type_limit"] = max(0, int(max_weld_types))
    if max_cut_types is not None:
        phase_objectives["cut_type_limit"] = max(0, int(max_cut_types))
    zw = {}
    zc = {}
    n_weld_types = None
    n_cut_types = None
    left = max(0.0, float(tl) - (time.monotonic() - t0))
    can_compress = left >= 8.0 and (constructed["n_cut"] + constructed["n_weld"]) <= 25_000
    if can_compress:
        try:
            m.freeTransform()
            m.addCons(joints <= joint_limit, name="joint_budget")
            for i, pats in weld_pool.items():
                for k in range(len(pats)):
                    zw[(i, k)] = m.addVar(vtype="B", name=f"zw_{i}_{k}")
                    m.addCons(xp[(i, k)] <= int(pipes[i].demand) * zw[(i, k)])
            for L, cps in cut_pool.items():
                for k in range(len(cps)):
                    zc[(L, k)] = m.addVar(vtype="B", name=f"zc_{L}_{k}")
                    m.addCons(yc[(L, k)] <= int(stock_qty.get(L, 0)) * zc[(L, k)])
            n_weld_types = quicksum(zw.values())
            n_cut_types = quicksum(zc.values())
            if max_weld_types is not None:
                m.addCons(n_weld_types <= max(0, int(max_weld_types)), name="weld_type_cap")
            if max_cut_types is not None:
                m.addCons(n_cut_types <= max(0, int(max_cut_types)), name="cut_type_cap")

            # Phase B: with joints locked, minimize weld recipe types.
            left = max(0.0, float(tl) - (time.monotonic() - t0))
            m.setParam("limits/time", max(1.0, left / 3))
            m.setObjective(n_weld_types, "minimize")
            m.optimize()
            if m.getNSols() == 0:
                raise RuntimeError("lost incumbent while locking weld types")
            best_w = int(round(m.getVal(n_weld_types)))
            phase_objectives["weld_types"] = best_w

            # Phase C: with joints and weld types locked, minimize cut recipes.
            m.freeTransform()
            m.addCons(n_weld_types == best_w, name="lock_weld_types")
            left = max(0.0, float(tl) - (time.monotonic() - t0))
            m.setParam("limits/time", max(1.0, left / 2))
            m.setObjective(n_cut_types, "minimize")
            m.optimize()
            if m.getNSols() == 0:
                raise RuntimeError("lost incumbent while locking cut types")
            best_c = int(round(m.getVal(n_cut_types)))
            phase_objectives["cut_types"] = best_c

            # Phase D: utilization is only a tie-break after all recipe objectives.
            m.freeTransform()
            m.addCons(n_cut_types == best_c, name="lock_cut_types")
            left = max(0.0, float(tl) - (time.monotonic() - t0))
            if left >= 1.0:
                m.setParam("limits/time", left)
                m.setObjective(used_len, "minimize")
                m.optimize()
        except Exception as exc:
            # The phase-A incumbent remains valid; do not return a partial model error.
            phase_objectives["compression"] = f"skipped:{type(exc).__name__}"

    dt = time.monotonic() - t0
    if m.getNSols() == 0:
        return {
            "ok": False,
            "error": f"status={m.getStatus()}",
            "elapsed_s": round(dt, 3),
            "n_weld": constructed["n_weld"],
            "n_cut": constructed["n_cut"],
            "sigma_n": len(sigma),
        }

    cuts = []
    welds = []
    used = 0.0
    cut_types_used = 0
    weld_types_used = 0
    segs_used: set[int] = set()
    for L, cps in cut_pool.items():
        for k, segs in enumerate(cps):
            val = m.getVal(yc[(L, k)])
            if val > 0.5:
                cnt = int(round(val))
                used += L * cnt
                cut_types_used += 1
                segs_used.update(segs)
                waste = L - sum(segs)
                cuts.append(
                    {
                        "L": int(L),
                        "segs": list(segs),
                        "waste": max(0, int(waste)),
                        "count": cnt,
                    }
                )
    for i, pats in weld_pool.items():
        for k, seq in enumerate(pats):
            val = m.getVal(xp[(i, k)])
            if val > 0.5:
                cnt = int(round(val))
                weld_types_used += 1
                segs_used.update(seq)
                welds.append(
                    {
                        "pipe_len": int(pipes[i].length),
                        "seq": list(seq),
                        "joints": max(0, len(seq) - 1),
                        "count": cnt,
                    }
                )

    joints_v = int(round(m.getVal(joints)))
    util = demand / used if used else 0.0
    if util > 1.001:
        return {
            "ok": False,
            "error": f"util_gt_1:{util:.6f}",
            "elapsed_s": round(dt, 3),
            "n_weld": constructed["n_weld"],
            "n_cut": constructed["n_cut"],
            "sigma_n": len(sigma),
        }
    return {
        "ok": True,
        "joints": joints_v,
        "util": util,
        "used_len": used,
        "weld_types": weld_types_used,
        "cut_types": cut_types_used,
        "seg_types": len(segs_used),
        "cuts": cuts,
        "welds": welds,
        "solver": "d5_motif_setcover",
        "elapsed_s": round(dt, 3),
        "n_weld": constructed["n_weld"],
        "n_cut": constructed["n_cut"],
        "sigma_n": len(sigma),
        "factory_notes": (constructed.get("notes") or [])[:20],
        "phase_objectives": phase_objectives,
    }


def solve_d5(
    group,
    *,
    tl: float = 120.0,
    util_floor: float | None = None,
    legacy_metrics: dict | None = None,
    joint_slack: int = 0,
) -> dict[str, Any] | None:
    from scripts.d5_column_factory import construct_d5_columns

    cons = construct_d5_columns(group)
    if not cons.get("ok"):
        return None
    sol = solve_setcover_lex(
        group,
        cons,
        tl=tl,
        util_floor=util_floor,
        joint_slack=joint_slack,
    )
    if not sol.get("ok"):
        # fallback: teacher objective without type binaries (same columns)
        from scripts.teacher_inspired_construct_v0 import solve_setcover

        raw = solve_setcover(group, cons, tl=max(10.0, float(tl) * 0.8))
        if not raw.get("ok"):
            return None
        # one more lex attempt for layout export
        sol = solve_setcover_lex(group, cons, tl=min(45.0, float(tl)), util_floor=None)
        if not sol.get("ok"):
            out = {
                "joints": int(round(float(raw["joints"]))),
                "weld_types": int(raw.get("weld_types") or 0),
                "cut_types": int(raw.get("cut_types") or 0),
                "seg_types": int(raw.get("seg_types") or 0),
                "util": float(raw["util"]),
                "used_len": float(raw.get("used_len") or 0),
                "cuts": [],
                "welds": [],
                "solver": "d5_motif_setcover",
                "solver_path": ["d5_motif_v1", "fingerprint_setcover"],
                "elapsed_s": raw.get("elapsed_s"),
                "diagnosis": [
                    f"n_weld={cons.get('n_weld')}",
                    f"n_cut={cons.get('n_cut')}",
                    "layout_missing",
                ],
                "ok": True,
            }
            if legacy_metrics:
                out["auto_tag"] = _lex_tag(out, legacy_metrics)
            return out

    out = {
        "joints": int(round(float(sol["joints"]))),
        "weld_types": int(sol.get("weld_types") or 0),
        "cut_types": int(sol.get("cut_types") or 0),
        "seg_types": int(sol.get("seg_types") or 0),
        "util": float(sol["util"]),
        "used_len": float(sol.get("used_len") or 0),
        "cuts": sol.get("cuts") or [],
        "welds": sol.get("welds") or [],
        "solver": "d5_motif_setcover",
        "solver_path": ["d5_motif_v1", sol.get("solver") or "setcover_lex"],
        "elapsed_s": sol.get("elapsed_s"),
        "phase_objectives": sol.get("phase_objectives") or {},
        "joint_slack": max(0, int(joint_slack)),
        "diagnosis": [
            f"sigma={len(cons.get('sigma') or [])}",
            f"n_weld={cons.get('n_weld')}",
            f"n_cut={cons.get('n_cut')}",
            f"factory={cons.get('factory')}",
        ]
        + list(cons.get("notes") or [])[:6],
        "ok": True,
    }
    if legacy_metrics:
        out["auto_tag"] = _lex_tag(out, legacy_metrics)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idx", type=int, required=True)
    ap.add_argument("--tl", type=float, default=120.0)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case

    case = load_legacy_case(ROOT / "data" / "legacy-cases", args.idx)
    g = merge_equivalent_pipes(
        parse_problem(clean_mom_problem(case["problem"])).groups[0]
    )
    met = (case.get("metrics") or {}).get("metrics") or {}
    legacy = {
        "joints": met.get("welding_joints"),
        "weld_types": met.get("weld_pattern_types"),
        "cut_types": met.get("cut_pattern_types"),
        "util": met.get("utilization_rate"),
    }
    uf = None  # D5 motif: no hard util floor; compare via auto_tag / human review
    sol = solve_d5(g, tl=args.tl, util_floor=uf, legacy_metrics=legacy)
    print(
        {
            "idx": args.idx,
            "sol": None
            if sol is None
            else {
                k: sol.get(k)
                for k in (
                    "joints",
                    "weld_types",
                    "cut_types",
                    "util",
                    "auto_tag",
                    "elapsed_s",
                    "solver",
                )
            },
            "legacy": legacy,
            "n_cuts": 0 if sol is None else len(sol.get("cuts") or []),
            "n_welds": 0 if sol is None else len(sol.get("welds") or []),
        }
    )
    return 0 if sol else 1


if __name__ == "__main__":
    raise SystemExit(main())
