"""列生成（切法侧 pricing）—— 攻克超紧长短混装（如 D 样本 99.78% 紧度 + 1012 根 150mm 短管）。

背景（诊断见 §11.26）：静态枚举切法列池对中等紧度（B/C）够用，但对 D 这类
"极小余量 + 大量成品短管必须零浪费嵌入长管缝隙"的样本，预枚举凑不出"长段+k个短段
恰好填满母材"的精确列 → LP 松弛都不可行。列生成按对偶价**动态生成**这些列，直击根因。

模型（拼法候选沿用 v2；切法列用列生成动态产出）：
  受限主问题 RMP (LP 松弛):
    u[i,w] >= 0    拼法 w 造管型 i 的根数,  Σ_w u[i,w] = demand_i          (对偶 δ_i)
    x[p]   >= 0    切法列 p 的母材根数
    段平衡: Σ_p prod_p[s]·x[p] >= Σ_{i,w} cons[i,w][s]·u[i,w]   ∀ 段长 s   (对偶 π_s >= 0)
    母材预算: Σ_{p: stock=L} x[p] <= bars[L]   ∀ 母材长 L                    (对偶 σ_L <= 0)
    目标: min Σ L_p·x[p]  (最小化用料 = 最大化利用率)

  切法列 p 的 reduced cost = L_p - Σ_s π_s·prod_p[s] - σ_L
  定价子问题(对每种母材长 L): 在长度 L 内选段(kerf 感知, <=max_pieces),
    最大化 Σ_s π_s·count[s], 若 max_value > L - σ_L 则该列 reduced cost < 0, 加入。
  这是一个有界背包 DP: dp[cap] = 该容量下可获得的最大 π 价值。

迭代加列直到无负 reduced cost 列 -> 解整数 RMP 得最终方案。

用法:
    python scripts/_poc_colgen.py --sample-id 350f2dbb-0858-4398-b00f-b2015be43e58 --time 300
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from app.domain import MaterialGroup, parse_problem  # noqa: E402
import _poc_setcover_ilp_v2 as v2  # noqa: E402


# ---------------------------------------------------------------------------
# Pricing subproblem: bounded knapsack over the segment alphabet for one stock
# length.  dp[c] = max Σ π·count achievable using <= remaining pieces within cap c.
# Returns (best_value, counts) for a column that fills bar length L (kerf-aware).
# ---------------------------------------------------------------------------


def price_column(
    L: int,
    segs: list[int],
    dual: dict[int, float],
    kerf: int,
    max_pieces: int,
    max_trim: int,
) -> tuple[float, dict[int, int]] | None:
    """Best cut column for a bar of length L by dual value (bounded #pieces).

    We DFS over segments (non-increasing) tracking used length and pieces,
    maximising Σ dual[seg].  kerf charged between consecutive pieces.  The
    trailing remnant is NOT capped here: the reduced cost ``L - val - σ_L``
    already penalises a poorly-filled bar (cost L is fixed), so pricing should
    freely explore any fill and let the RMP reject wasteful columns.  ``max_trim``
    is kept only as a soft record threshold: we record the best-value column
    regardless, but prefer the densest one on ties via remnant."""
    segs = sorted((s for s in segs if s <= L), reverse=True)
    best = {"val": -1.0, "counts": None, "remnant": L + 1}

    def dfs(idx: int, used: int, pieces: int, val: float, counts: dict[int, int]) -> None:
        remnant = L - used
        if pieces >= 1 and (
            val > best["val"] or (val == best["val"] and remnant < best["remnant"])
        ):
            best["val"] = val
            best["counts"] = dict(counts)
            best["remnant"] = remnant
        if pieces >= max_pieces:
            return
        for j in range(idx, len(segs)):
            seg = segs[j]
            extra = seg + (kerf if pieces >= 1 else 0)
            if used + extra > L:
                continue
            counts[seg] = counts.get(seg, 0) + 1
            dfs(j, used + extra, pieces + 1, val + dual.get(seg, 0.0), counts)
            counts[seg] -= 1
            if counts[seg] == 0:
                del counts[seg]

    dfs(0, 0, 0, 0.0, {})
    if best["counts"] is None:
        return None
    return best["val"], best["counts"]


# ---------------------------------------------------------------------------
# Restricted master problem (LP relaxation) — returns duals + is rebuilt each iter.
# ---------------------------------------------------------------------------


def solve_rmp_lp(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
    bars_by_len: dict[int, int],
    time_limit: float,
) -> dict[str, Any]:
    from pyscipopt import Model, quicksum

    m = Model("rmp_lp")
    m.setParam("limits/time", time_limit)
    m.hideOutput()
    # Need valid dual values for pricing: turn off presolve/heuristics so the
    # final LP duals are the true simplex duals of the stated constraints.
    m.setPresolve(0)
    m.setParam("propagating/maxrounds", 0)
    m.setParam("propagating/maxroundsroot", 0)

    u: dict[tuple[int, int], Any] = {}
    demand_cons: dict[int, Any] = {}
    for i, pats in weld_cands.items():
        for wi in range(len(pats)):
            u[(i, wi)] = m.addVar(vtype="C", lb=0)
        demand_cons[i] = m.addCons(
            quicksum(u[(i, wi)] for wi in range(len(pats))) == group.pipes[i].demand
        )

    n = len(cut_cols)
    x = {p: m.addVar(vtype="C", lb=0) for p in range(n)}

    cols_by_len: dict[int, list[int]] = defaultdict(list)
    for p, col in enumerate(cut_cols):
        cols_by_len[col["stock"]].append(p)
    budget_cons: dict[int, Any] = {}
    for L in bars_by_len:
        plist = cols_by_len.get(L, [])
        budget_cons[L] = m.addCons(quicksum(x[p] for p in plist) <= bars_by_len[L])

    produced: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for p, col in enumerate(cut_cols):
        for seg, c in col["counts"].items():
            produced[seg].append((c, x[p]))
    consumed: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for i, pats in weld_cands.items():
        for wi, parts in enumerate(pats):
            cnt: dict[int, int] = defaultdict(int)
            for seg in parts:
                cnt[seg] += 1
            for seg, c in cnt.items():
                consumed[seg].append((c, u[(i, wi)]))
    # Artificial "produced" variable per segment with a big cost keeps the RMP
    # LP always feasible (Farkas-free column generation); columns must drive
    # these to zero.  BIG must dominate any real stock length so the optimum
    # never keeps an artificial unless truly infeasible.
    BIG = max(bars_by_len) * 1000 if bars_by_len else 1_000_000
    art: dict[int, Any] = {}
    bal_cons: dict[int, Any] = {}
    for seg in set(produced) | set(consumed):
        art[seg] = m.addVar(vtype="C", lb=0)
        bal_cons[seg] = m.addCons(
            quicksum(c * v for c, v in produced.get(seg, [])) + art[seg]
            >= quicksum(c * v for c, v in consumed.get(seg, []))
        )

    m.setObjective(
        quicksum(cut_cols[p]["stock"] * x[p] for p in range(n))
        + quicksum(BIG * art[seg] for seg in art),
        "minimize",
    )
    m.optimize()
    st = m.getStatus()
    if m.getNSols() == 0 and st != "optimal":
        return {"feasible": False, "status": st}

    pi = {seg: m.getDualsolLinear(c) for seg, c in bal_cons.items()}
    sigma = {L: m.getDualsolLinear(c) for L, c in budget_cons.items()}
    art_active = sum(1 for seg, v in art.items() if m.getVal(v) > 1e-6)
    pimax = max(pi.values()) if pi else 0
    return {
        "feasible": True,
        "status": st,
        "obj": m.getObjVal(),
        "pi": pi,
        "sigma": sigma,
        "art_active": art_active,
        "pi_max": pimax,
    }


# ---------------------------------------------------------------------------
# Column generation loop
# ---------------------------------------------------------------------------


def column_generation(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    alphabet: set[int],
    seed_cols: list[dict[str, Any]],
    kerf: int,
    max_pieces: int,
    max_trim: int,
    time_limit: float,
    max_iters: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bars_by_len: dict[int, int] = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity
    stock_lens = sorted(bars_by_len)
    segs = sorted(alphabet)

    cols = list(seed_cols)
    seen = {(c["stock"], tuple(sorted(c["counts"].items()))) for c in cols}
    deadline = time.monotonic() + time_limit
    last_lp: dict[str, Any] = {}
    for it in range(max_iters):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            print(f"[cg] time budget exhausted at iter {it}", flush=True)
            break
        lp = solve_rmp_lp(group, weld_cands, cols, bars_by_len, min(30.0, remaining))
        if not lp["feasible"]:
            print(f"[cg] iter {it}: RMP LP infeasible (status={lp['status']}) — need more seed columns", flush=True)
            return cols, lp
        last_lp = lp
        pi, sigma = lp["pi"], lp["sigma"]
        added = 0
        best_rc = 0.0
        for L in stock_lens:
            res = price_column(L, segs, pi, kerf, max_pieces, max_trim)
            if res is None:
                continue
            val, counts = res
            # reduced cost = L - Σ π·count - σ_L  ; add if < -eps
            rc = L - val - sigma.get(L, 0.0)
            if rc < -1e-6:
                key = (L, tuple(sorted(counts.items())))
                if key not in seen:
                    seen.add(key)
                    cols.append({"stock": L, "counts": dict(counts),
                                 "used": sum(s * c for s, c in counts.items()),
                                 "remnant": L - sum(s * c for s, c in counts.items())})
                    added += 1
                    best_rc = min(best_rc, rc)
        print(f"[cg] iter {it}: LP obj={lp['obj']:.0f}, added {added} cols (best rc={best_rc:.1f}), "
              f"art_active={lp.get('art_active')}, pi_max={lp.get('pi_max',0):.0f}, pool={len(cols)}", flush=True)
        if added == 0:
            print(f"[cg] converged: no negative-reduced-cost column at iter {it}", flush=True)
            break
    return cols, last_lp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-id")
    ap.add_argument("--sample-file")
    ap.add_argument("--blade-margin", type=float, default=None)
    ap.add_argument("--splits", type=int, default=4)
    ap.add_argument("--max-pieces", type=int, default=4)
    ap.add_argument("--max-trim", type=int, default=200)
    ap.add_argument("--seed-max-trim", type=int, default=100)
    ap.add_argument("--seed-cap", type=int, default=20000)
    ap.add_argument("--time", type=float, default=300.0, help="total time budget (s)")
    ap.add_argument("--int-time", type=float, default=120.0, help="final integer MIP time (s)")
    args = ap.parse_args(argv)

    group, legacy = v2._load(args)
    kerf = group.blade_margin
    print(f"Group {group.material}/{group.specifications}: {len(group.pipes)} pipe types, "
          f"demand {sum(p.demand for p in group.pipes)} pipes, "
          f"{sum(s.quantity for s in group.stocks)} bars, kerf={kerf}\n", flush=True)

    weld_cands, alphabet = v2.build_weld_candidates(group, args.splits)
    print(f"[step1] weld candidates over {len(group.pipes)} pipes -> alphabet {len(alphabet)} segs", flush=True)

    stock_lengths = [s.length for s in group.stocks]
    seed = v2.enumerate_cut_columns(
        sorted(alphabet), stock_lengths, kerf, args.seed_cap, args.seed_max_trim, args.max_pieces
    )
    seed = v2.ensure_coverage(seed, sorted(alphabet), stock_lengths, kerf, args.max_pieces)
    print(f"[step2] seed pool = {len(seed)} columns\n", flush=True)

    t0 = time.monotonic()
    cols, lp = column_generation(
        group, weld_cands, alphabet, seed, kerf, args.max_pieces, args.max_trim, args.time
    )
    print(f"\n[step3] column generation done in {time.monotonic()-t0:.1f}s, final pool {len(cols)} cols", flush=True)
    if not lp.get("feasible"):
        print("  RMP still infeasible — columns cannot cover demand under constraints")
        return 1
    print(f"  LP lower bound (used length) = {lp['obj']:.0f}", flush=True)

    # Final integer solve on the enriched pool (two-phase lexicographic: lock
    # utilisation, then minimise cut-pattern variety on the active column subset).
    res = v2.solve_two_phase(
        group, weld_cands, cols, args.int_time / 2, args.int_time / 2, slack=0.0
    )
    if not res["feasible"]:
        print(f"[step4] integer RMP INFEASIBLE within {args.int_time}s")
        return 1
    print(f"\n[step4] integer two-phase status={res['status']} in {res['elapsed']:.1f}s "
          f"(phase={res.get('phase')})")
    demand_len = sum(p.length * p.demand for p in group.pipes)
    util = demand_len / res["used_len"] if res["used_len"] else 0
    print(f"  cut pattern types  = {res['cut_types']}")
    print(f"  weld pattern types = {res['weld_types']}")
    print(f"  stock bars used    = {res['total_bars']}")
    print(f"  utilisation        = {util:.6f}  (demand {demand_len} / used {res['used_len']})")

    if legacy:
        gi = (legacy.get("GeneralInfo") or {})
        lcp = ((legacy.get("Result") or {}).get("CuttingPattern") or {}).get("CuttingPipe", []) or []
        lwp = ((legacy.get("Result") or {}).get("WeldingPattern") or {}).get("WeldingPipe", []) or []
        lbars = sum(int(float(c.get("Number", 0))) for c in lcp)
        lwtypes = len({tuple(int(round(float(t))) for t in str(p.get("Part", "")).split())
                       for w in lwp for p in (w.get("Pattern") or [])})
        print("\n  --- vs legacy ---")
        print(f"  metric                          cg        legacy")
        print(f"  cut pattern types      {res['cut_types']:>10}   {len(lcp):>10}")
        print(f"  weld pattern types     {res['weld_types']:>10}   {lwtypes:>10}")
        print(f"  stock bars             {res['total_bars']:>10}   {lbars:>10}")
        print(f"  utilisation            {util:>10.6f}   {float(gi.get('UtilRate',0)):>10.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
