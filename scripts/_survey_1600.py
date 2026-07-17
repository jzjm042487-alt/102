"""全量数据摸底：读 MOM 导出 JSON（RECORDS 内含 MOMPROBLEMJSON + MOMRESULTJSON）。

不做任何求解，只统计：
  第一步 规模分布：管型数/管长/定尺种类/需求/紧料度/约束取值
  第二步 旧软件规则：段长字母表、整根母料复用、切法/拼法种类、尾料分布、切拼匹配

用法：
  python scripts/_survey_1600.py <path-to-json> [--dump-hard N]
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter, defaultdict


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x, default=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _parts(s: str) -> list[int]:
    return [int(float(t)) for t in str(s).split() if t.strip()]


def _has_unweldable(area) -> bool:
    if area is None:
        return False
    if isinstance(area, (list, tuple, dict)):
        return len(area) > 0
    return bool(str(area).strip())


def load_records(path: str):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return d["RECORDS"]


def survey_problem(prob: dict) -> dict:
    pipes = prob.get("Pipe", []) or []
    stocks = prob.get("Stock", []) or []
    pipe_lens = [_f(p.get("pipe_length")) for p in pipes]
    pipe_dem = [_i(p.get("pipe_demand")) for p in pipes]
    stock_lens = [_f(s.get("stock_length")) for s in stocks]
    stock_dem = [_i(s.get("stock_demand")) for s in stocks]
    must_use = [_i(s.get("must_use")) for s in stocks]

    # 需求总长 vs 库存总长
    demand_len = sum(l * d for l, d in zip(pipe_lens, pipe_dem))
    # stock_demand 可能为 0 或很大表示不限量；用 available 概念区分
    stock_total = sum(l * d for l, d in zip(stock_lens, stock_dem))

    # 有多少管子短于最短母料（可整根落料）；有多少长于最长母料（必须拼接）
    max_stock = max(stock_lens) if stock_lens else 0.0
    min_stock = min(stock_lens) if stock_lens else 0.0
    n_need_weld = sum(1 for l in pipe_lens if l > max_stock)  # 单管超过最长母料 -> 必拼
    n_shorter = sum(1 for l in pipe_lens if l < min_stock)  # 短于最短母料

    n_bars = sum(stock_dem)

    return {
        "n_pipe_types": len(pipes),
        "n_pipes": sum(pipe_dem),
        "n_stock_types": len(stocks),
        "n_stock_bars": n_bars,
        "pipe_len_min": min(pipe_lens) if pipe_lens else 0,
        "pipe_len_max": max(pipe_lens) if pipe_lens else 0,
        "stock_len_min": min_stock,
        "stock_len_max": max_stock,
        "demand_len": demand_len,
        "stock_total": stock_total,
        "tightness": (demand_len / stock_total) if stock_total else 0.0,
        "n_need_weld": n_need_weld,
        "n_pipetype_shorter_than_minstock": n_shorter,
        "any_must_use": any(m > 0 for m in must_use),
        "min_weld_len": _f(prob.get("Min_Welding_Length")),
        "adj_dist": _f(prob.get("Adjacent_Distance")),
        "corner_dist": _f(prob.get("Corner_Distance")),
        "target_util": _f(prob.get("Target_Util_Rate")),
        "any_unweldable": any(_has_unweldable(p.get("Unweldable_Area")) for p in pipes),
        "max_joints_vals": sorted({_i(p.get("Max_Weldingjoint_Number")) for p in pipes}),
    }


def survey_legacy(res: dict) -> dict | None:
    R = res.get("Result")
    gi = res.get("GeneralInfo", {})
    if not isinstance(R, dict):
        return None
    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    wp = (R.get("WeldingPattern") or {}).get("WeldingPipe") or []

    # 切法：种类数（去重 Length+Part），母材根数，尾料
    cut_key = set()
    cut_bars = 0
    trim_list = []
    seg_alphabet = Counter()          # 段长 -> 出现次数（按 pattern*Number 加权）
    whole_bar_as_seg = 0              # 切法里把整根母料当作单段（Part 只有一段且=Length）
    seg_count_per_pat = []
    for c in cp:
        L = _i(c.get("Length"))
        parts = _parts(c.get("Part", ""))
        num = _i(c.get("Number"), 1)
        cut_key.add((L, tuple(parts)))
        cut_bars += num
        trim_list.append(_i(c.get("TrimLoss")))
        seg_count_per_pat.append(len(parts))
        for s in parts:
            seg_alphabet[s] += num
        if len(parts) == 1 and parts[0] == L:
            whole_bar_as_seg += num

    # 拼法：种类数（去重 figure+Part），单管多拼法比例
    weld_key = set()
    weld_patterns_per_pipe = defaultdict(set)
    pipe_whole = 0     # 拼法就是整根（单段，不拼）
    weld_total = 0
    for w in wp:
        fig = w.get("FigureNumber")
        jl = w.get("jlxh")
        L = _i(w.get("Length"))
        for pat in w.get("Pattern", []):
            parts = _parts(pat.get("Part", ""))
            num = _i(pat.get("Number"), 1)
            weld_key.add((fig, tuple(parts)))
            weld_patterns_per_pipe[(fig, L)].add(tuple(parts))
            if len(parts) == 1:
                pipe_whole += num
            else:
                weld_total += (len(parts) - 1) * num

    multi_pat_pipes = sum(1 for v in weld_patterns_per_pipe.values() if len(v) > 1)

    return {
        "util": _f(gi.get("UtilRate")),
        "welds_reported": _i(gi.get("WeldingJointQuantity")),
        "welds_recomputed": weld_total,
        "cut_types": len(cut_key),
        "cut_bars": cut_bars,
        "cut_types_per_bar": (len(cut_key) / cut_bars) if cut_bars else 0.0,
        "weld_types": len(weld_key),
        "seg_alphabet_size": len(seg_alphabet),
        "whole_bar_as_seg": whole_bar_as_seg,
        "pipe_whole_noweld": pipe_whole,
        "multi_pattern_pipes": multi_pat_pipes,
        "trim_max": max(trim_list) if trim_list else 0,
        "trim_mean": (sum(trim_list) / len(trim_list)) if trim_list else 0.0,
        "seg_count_mean": (sum(seg_count_per_pat) / len(seg_count_per_pat)) if seg_count_per_pat else 0.0,
        "seg_count_max": max(seg_count_per_pat) if seg_count_per_pat else 0,
    }


def pct(vals, q):
    if not vals:
        return 0
    vals = sorted(vals)
    k = int(round((len(vals) - 1) * q))
    return vals[k]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"
    recs = load_records(path)
    print(f"records: {len(recs)}")

    rows = []
    n_ok, n_fail, n_noresult = 0, 0, 0
    for r in recs:
        pj = r.get("MOMPROBLEMJSON")
        rj = r.get("MOMRESULTJSON")
        if not pj:
            continue
        try:
            prob = json.loads(pj)
        except Exception:
            continue
        sp = survey_problem(prob)
        sl = None
        if rj:
            try:
                res = json.loads(rj)
                gi = res.get("GeneralInfo", {})
                ok = str(gi.get("Result", "")).lower().startswith("success")
                if ok:
                    sl = survey_legacy(res)
                    n_ok += 1
                else:
                    n_fail += 1
            except Exception:
                n_noresult += 1
        else:
            n_noresult += 1
        rows.append({"id": r.get("ID"), "mat": r.get("MOMMATERIAL"),
                     "spec": prob.get("specifications"), **sp,
                     "legacy": sl})

    print(f"legacy success={n_ok} fail={n_fail} noresult={n_noresult}")
    print()

    # === 第一步 规模分布 ===
    def col(name):
        return [x[name] for x in rows if x.get(name) is not None]

    print("=== 规模分布 (全量 problem) ===")
    for name in ["n_pipe_types", "n_pipes", "n_stock_types", "n_stock_bars",
                 "tightness", "pipe_len_max", "stock_len_max", "n_need_weld",
                 "n_pipetype_shorter_than_minstock"]:
        v = col(name)
        print(f"  {name:34s} min={min(v):>10.2f} p50={pct(v,0.5):>10.2f} "
              f"p90={pct(v,0.9):>10.2f} max={max(v):>10.2f}")

    # 分档：按母材根数
    buckets = [(0, 100), (100, 500), (500, 1000), (1000, 2000), (2000, 10**9)]
    print("\n  母材根数分档:")
    for lo, hi in buckets:
        cnt = sum(1 for x in rows if lo <= x["n_stock_bars"] < hi)
        print(f"    [{lo:>5},{hi if hi<10**9 else '+inf':>5}) : {cnt}")

    print("\n  紧料度分档:")
    tb = [(0, 0.9), (0.9, 0.98), (0.98, 0.99), (0.99, 0.999), (0.999, 2)]
    for lo, hi in tb:
        cnt = sum(1 for x in rows if lo <= x["tightness"] < hi)
        print(f"    [{lo:.3f},{hi:.3f}) : {cnt}")

    # 约束取值
    print("\n  约束/开关:")
    print(f"    有 must_use 的组: {sum(1 for x in rows if x['any_must_use'])}")
    print(f"    有 禁焊区 的组:   {sum(1 for x in rows if x['any_unweldable'])}")
    print(f"    有 单管>最长母料(必拼) 的组: {sum(1 for x in rows if x['n_need_weld']>0)}")
    print(f"    有 短于最短母料管型 的组:   {sum(1 for x in rows if x['n_pipetype_shorter_than_minstock']>0)}")
    mwl = Counter(x["min_weld_len"] for x in rows)
    print(f"    Min_Welding_Length 取值 top: {mwl.most_common(8)}")

    # === 第二步 旧软件规则 ===
    lrows = [x["legacy"] for x in rows if x["legacy"]]
    print(f"\n=== 旧软件规则 (成功解 {len(lrows)} 组) ===")
    for name in ["util", "cut_types", "weld_types", "cut_types_per_bar",
                 "seg_alphabet_size", "seg_count_mean", "seg_count_max",
                 "trim_mean", "multi_pattern_pipes"]:
        v = [x[name] for x in lrows]
        print(f"  {name:22s} min={min(v):>9.3f} p50={pct(v,0.5):>9.3f} "
              f"p90={pct(v,0.9):>9.3f} max={max(v):>9.3f}")

    # 整根母料复用（whole_bar_as_seg / cut_bars）
    reuse = [x["whole_bar_as_seg"] / x["cut_bars"] for x in lrows if x["cut_bars"]]
    print(f"  整根母料当单段占比 whole_bar_as_seg/cut_bars: "
          f"p50={pct(reuse,0.5):.3f} p90={pct(reuse,0.9):.3f}")
    nowe = [x["pipe_whole_noweld"] for x in lrows]
    print(f"  拼法为整根(不拼)条数 pipe_whole_noweld: p50={pct(nowe,0.5):.1f} max={max(nowe)}")
    mp = sum(1 for x in lrows if x["multi_pattern_pipes"] > 0)
    print(f"  存在 '同一管型多种拼法' 的组: {mp} / {len(lrows)}")

    # welds recomputed vs reported 一致性
    diff = [abs(x["welds_recomputed"] - x["welds_reported"]) for x in lrows]
    print(f"  重算焊口 vs 报告焊口 |diff|: p50={pct(diff,0.5)} max={max(diff)}")

    # dump 最难若干组供后续拆解
    if "--dump-hard" in sys.argv:
        n = int(sys.argv[sys.argv.index("--dump-hard") + 1])
        hard = sorted(rows, key=lambda x: (x["n_stock_bars"], x["tightness"]), reverse=True)[:n]
        print(f"\n=== 最难 {n} 组 (按母材根数) ===")
        for x in hard:
            lg = x["legacy"] or {}
            print(f"  {x['id'][:8]} {x['mat']:>16s} {str(x['spec']):>10s} "
                  f"bars={x['n_stock_bars']:>5} ptypes={x['n_pipe_types']:>4} "
                  f"tight={x['tightness']:.4f} | legacy util={lg.get('util',0):.4f} "
                  f"cut={lg.get('cut_types','-')} weld={lg.get('weld_types','-')}")


if __name__ == "__main__":
    main()
