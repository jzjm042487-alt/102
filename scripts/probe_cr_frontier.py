"""Cr 极限探测：焊口 / 拼法 / 切法 Pareto（实验脚本，不进生产路径）。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_cr_frontier.py
  .venv\\Scripts\\python.exe scripts\\probe_cr_frontier.py --tl 8 --budget 90
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts.legacy_corpus_lib import clean_mom_problem
from scripts.weld_then_cut import (
    _demand_of,
    _eval_assign_csp,
    _stock_qty,
    build_sigma,
    enum_weld_patterns,
    _filter_ok_patterns,
    solve as wtc_solve,
)


def _load_cr():
    raw = json.loads(
        (ROOT / "data/acceptance/113146/cr.json").read_text(encoding="utf-8")
    )
    return merge_equivalent_pipes(
        parse_problem(clean_mom_problem(raw)).groups[0]
    )


def _lex_key(sol: dict, uf: float):
    ok = 0 if sol["util"] >= uf - 0.005 else 1
    return (
        ok,
        sol["joints"],
        sol["weld_types"],
        sol["cut_types"],
        sol.get("seg_types", 99),
        -sol["util"],
    )


def _pick_pat(cands: list[tuple[int, ...]], prefer_sym: bool = True):
    if not cands:
        return None
    if prefer_sym:
        cands = sorted(cands, key=lambda s: (abs(s[0] - s[-1]), s))
    return cands[0]


def build_assign_variants(g, pools, stock_qty, pack: int):
    """生成一批「尽量少焊口」的拼法分配候选。"""
    scarce = [S for S in sorted(stock_qty) if S != pack]
    long_idx = [i for i, p in enumerate(g.pipes) if p.length > pack]
    short_base = {
        (i, (p.length,)): p.demand
        for i, p in enumerate(g.pipes)
        if p.length <= pack
    }

    two_scarce = {
        (i, S): [seq for seq in pools[i] if len(seq) == 2 and seq.count(S) == 1]
        for i in long_idx
        for S in scarce
    }
    three_scarce = {
        (i, S): [seq for seq in pools[i] if len(seq) == 3 and seq.count(S) == 1]
        for i in long_idx
        for S in scarce
    }
    pure_two = {
        i: [seq for seq in pools[i] if len(seq) == 2 and not any(s in stock_qty for s in seq)]
        for i in long_idx
    }
    pack_two = {
        i: [
            seq
            for seq in pools[i]
            if len(seq) == 2 and pack in seq and seq.count(pack) == 1
        ]
        for i in long_idx
    }
    pack_three = {
        i: [
            seq
            for seq in pools[i]
            if len(seq) == 3 and pack in seq and seq.count(pack) == 1
        ]
        for i in long_idx
    }

    variants: list[tuple[str, dict]] = []

    def fill_rest(assign, rem_dem, prefer_pack_three: bool = False):
        out = dict(assign)
        rem = dict(rem_dem)
        for i in long_idx:
            need = rem[i]
            if need <= 0:
                continue
            order = []
            if prefer_pack_three:
                order = [pack_three[i], pure_two[i], pack_two[i]]
            else:
                order = [pure_two[i], pack_two[i], pack_three[i]]
            picked = None
            for cands in order:
                picked = _pick_pat(cands)
                if picked is not None:
                    break
            if picked is None:
                return None
            out[i, picked] = out.get((i, picked), 0) + need
            rem[i] = 0
        return out

    # A) 全尽量 2 段：紧缺整段作一半，按宿主管分配
    #    对每种紧缺选一个 host，数量不超过 host demand
    hosts = long_idx
    S_list = scarce  # e.g. [8800, 10400] sorted
    if len(S_list) >= 2:
        S_a, S_b = S_list[0], S_list[1]  # smaller first typically
        qa, qb = stock_qty[S_a], stock_qty[S_b]
        for ha, hb in product(hosts, hosts):
            if qa > g.pipes[ha].demand:
                continue
            # hb may equal ha
            need_b_cap = g.pipes[hb].demand - (qa if ha == hb else 0)
            if qb > need_b_cap:
                continue
            # modes: 2seg or 3seg for each scarce
            for ma, mb in product(("2", "3"), ("2", "3")):
                pat_a = _pick_pat(
                    two_scarce[(ha, S_a)] if ma == "2" else three_scarce[(ha, S_a)]
                )
                pat_b = _pick_pat(
                    two_scarce[(hb, S_b)] if mb == "2" else three_scarce[(hb, S_b)]
                )
                if pat_a is None or pat_b is None:
                    continue
                rem = {i: g.pipes[i].demand for i in long_idx}
                assign = dict(short_base)
                assign[ha, pat_a] = assign.get((ha, pat_a), 0) + qa
                rem[ha] -= qa
                assign[hb, pat_b] = assign.get((hb, pat_b), 0) + qb
                rem[hb] -= qb
                if any(v < 0 for v in rem.values()):
                    continue
                # leave options: use all scarce (already), or leave 1 of largest
                for leave_name, leave_fix in (
                    ("use_all", False),
                    ("leave1_big", True),
                ):
                    asg = dict(assign)
                    rem2 = dict(rem)
                    if leave_fix and qb >= 1:
                        # peel one big scarce back
                        asg[hb, pat_b] -= 1
                        rem2[hb] += 1
                        if asg[hb, pat_b] <= 0:
                            del asg[hb, pat_b]
                    filled = fill_rest(asg, rem2, prefer_pack_three=(ma == "3" or mb == "3"))
                    if filled is None:
                        continue
                    tag = f"scarce2host ha={g.pipes[ha].length} hb={g.pipes[hb].length} {ma}/{mb} {leave_name}"
                    variants.append((tag, filled))

    # B) 对照：当前风格 — 小紧缺 3 段、大紧缺 3 段，剩余纯2/ pack2 /pack3
    for host in hosts:
        for ma, mb in (("3", "3"), ("3", "2"), ("2", "2"), ("2", "3")):
            if len(S_list) < 2:
                break
            S_a, S_b = S_list[0], S_list[1]
            qa, qb = stock_qty[S_a], stock_qty[S_b]
            if qa + qb > g.pipes[host].demand:
                # split b to another
                continue
            pat_a = _pick_pat(two_scarce[(host, S_a)] if ma == "2" else three_scarce[(host, S_a)])
            pat_b = _pick_pat(two_scarce[(host, S_b)] if mb == "2" else three_scarce[(host, S_b)])
            if pat_a is None or pat_b is None:
                continue
            rem = {i: g.pipes[i].demand for i in long_idx}
            assign = dict(short_base)
            assign[host, pat_a] = qa
            rem[host] -= qa
            assign[host, pat_b] = assign.get((host, pat_b), 0) + qb
            rem[host] -= qb
            filled = fill_rest(assign, rem)
            if filled is None:
                continue
            variants.append(
                (f"samehost L={g.pipes[host].length} {ma}/{mb}", filled)
            )

    # C) 几何极限尝试：忽略紧缺「必须整段嵌入」，只用 pure2 / pack2（紧缺当普通母料在 CSP 切）
    #    即拼层不消费 stock 段，S2 自由切 —— 这才是 joints=69 的候选
    rem = {i: g.pipes[i].demand for i in long_idx}
    assign = dict(short_base)
    ok = True
    for i in long_idx:
        pat = _pick_pat(pure_two[i]) or _pick_pat(pack_two[i])
        if pat is None:
            ok = False
            break
        assign[i, pat] = rem[i]
    if ok:
        variants.append(("geom69_pure_or_pack2", assign))

    # 去重
    seen = set()
    uniq = []
    for tag, asg in variants:
        key = tuple(sorted((k, v) for k, v in asg.items()))
        if key in seen:
            continue
        seen.add(key)
        # 预计算焊口
        _d, j = _demand_of(asg)
        uniq.append((j, tag, asg))
    uniq.sort(key=lambda x: (x[0], x[1]))
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tl", type=float, default=6.0, help="each CSP time limit")
    ap.add_argument("--budget", type=float, default=120.0, help="total wall budget")
    ap.add_argument("--util-floor", type=float, default=None)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    g = _load_cr()
    stock_qty = _stock_qty(g)
    stocks = sorted(stock_qty, reverse=True)
    pack = max(stocks)
    kerf = g.blade_margin
    uf = args.util_floor
    if uf is None:
        uf = max(0.95, float(g.target_rate or 0.95))

    print("=== Cr frontier probe ===")
    print(f"stocks={stock_qty} pack={pack} kerf={kerf}")
    print(f"pipes={[(p.length, p.demand) for p in g.pipes]}")
    print(f"util_floor={uf} demand={g.demand_length}")
    print(f"geom_lb_joints=69  legacy≈115")

    t0 = time.monotonic()
    sigma = build_sigma(g)
    raw = enum_weld_patterns(g, sigma, max_pats_per_pipe=8000)
    ok = _filter_ok_patterns(raw, set(stock_qty), stocks, kerf, set(sigma), pack)
    print(f"sigma={len(sigma)} pools={[len(ok[i]) for i in range(len(g.pipes))]}")

    variants = build_assign_variants(g, ok, stock_qty, pack)
    print(f"variants={len(variants)} (unique assigns), joints range "
          f"{variants[0][0]}..{variants[-1][0]}")

    # also run current WTC as baseline
    rows = []
    print("\n-- baseline WTC --")
    sol0 = wtc_solve(g, tl=min(20.0, args.budget * 0.25), util_floor=uf, verbose=False)
    if sol0:
        rows.append(
            {
                "tag": "wtc_current",
                "ok_floor": sol0["util"] >= uf - 0.005,
                "joints": sol0["joints"],
                "weld_types": sol0["weld_types"],
                "cut_types": sol0["cut_types"],
                "seg_types": sol0.get("seg_types"),
                "util": round(sol0["util"], 6),
                "used_len": sol0["used_len"],
                "path": sol0.get("solver_path"),
            }
        )
        print(
            f"  WTC joints={sol0['joints']} weld={sol0['weld_types']} "
            f"cut={sol0['cut_types']} util={sol0['util']:.6f}"
        )

    print(f"\n-- evaluate up to {len(variants)} assigns (budget {args.budget}s) --")
    # prioritize low joints
    evaluated = 0
    for j0, tag, asg in variants:
        if time.monotonic() - t0 >= args.budget:
            print("  budget exhausted")
            break
        rem_tl = min(args.tl, max(1.0, args.budget - (time.monotonic() - t0)))
        sol = _eval_assign_csp(
            g, asg, stock_qty, kerf, tl=rem_tl, util_floor=uf
        )
        evaluated += 1
        if sol is None:
            if evaluated <= 15 or j0 <= 80:
                print(f"  FAIL CSP j={j0} {tag}")
            continue
        row = {
            "tag": tag,
            "ok_floor": sol["util"] >= uf - 0.005,
            "joints": sol["joints"],
            "weld_types": sol["weld_types"],
            "cut_types": sol["cut_types"],
            "seg_types": sol["seg_types"],
            "util": round(sol["util"], 6),
            "used_len": sol["used_len"],
        }
        rows.append(row)
        mark = "OK " if row["ok_floor"] else "low"
        print(
            f"  {mark} j={row['joints']:3d} w={row['weld_types']:2d} "
            f"c={row['cut_types']:2d} util={row['util']:.6f} | {tag}"
        )

    rows.sort(key=lambda r: _lex_key(r, uf))
    print("\n=== PARETO / TOP (lex: floor → joints → weld → cut) ===")
    shown = 0
    best_by_j = {}
    for r in rows:
        j = r["joints"]
        if j not in best_by_j:
            best_by_j[j] = r
        if shown < args.top:
            flag = "FLOOR" if r["ok_floor"] else "below"
            print(
                f"  [{flag}] j={r['joints']:3d} weld={r['weld_types']:2d} "
                f"cut={r['cut_types']:2d} util={r['util']:.6f}  {r['tag']}"
            )
            shown += 1

    print("\n=== best per joints bucket ===")
    for j in sorted(best_by_j):
        r = best_by_j[j]
        print(
            f"  j={j:3d} → weld={r['weld_types']} cut={r['cut_types']} "
            f"util={r['util']:.6f} floor={r['ok_floor']} | {r['tag']}"
        )

    # frontier summary
    floor_ok = [r for r in rows if r["ok_floor"]]
    any_ok = rows
    print("\n=== LIMIT SUMMARY ===")
    if floor_ok:
        b = min(floor_ok, key=lambda r: _lex_key(r, uf))
        print(
            f"  under util_floor≈{uf}: BEST lex "
            f"joints={b['joints']} weld={b['weld_types']} cut={b['cut_types']} "
            f"util={b['util']} tag={b['tag']}"
        )
        print(f"  min joints under floor: {min(r['joints'] for r in floor_ok)}")
    else:
        print("  NO solution met util_floor in this probe")
    if any_ok:
        b2 = min(any_ok, key=lambda r: (r["joints"], r["weld_types"], r["cut_types"]))
        print(
            f"  ignoring floor: min joints seen feasible CSP "
            f"joints={b2['joints']} weld={b2['weld_types']} cut={b2['cut_types']} "
            f"util={b2['util']}"
        )
    print(f"  evaluated={evaluated}/{len(variants)} wall={time.monotonic()-t0:.1f}s")

    out = ROOT / "data/acceptance/113146/cr_frontier_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"util_floor": uf, "rows": rows, "elapsed": time.monotonic() - t0},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
