"""第二/三步：主力区间(紧料 0.99~0.999 且母材 100~2000 根)上反推旧软件规则。

重点回答：
  (1) 段长字母表是怎么构成的？—— 有多少段是"整根母料原样"、多少是"整管原样"、
      多少是真正的中间切段？中间切段有没有跨管型复用（同一段长被多个管型用）？
  (2) 切/拼/匹配三分法：拼法产出的段 == 切法消耗的段 吗？段长字母表能否先独立定，
      再让切、拼各自去覆盖？
  (3) 整根母料复用、单管单拼法 的量化。

不做求解，只统计。
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict


def _i(x, d=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return d


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _parts(s):
    return [int(float(t)) for t in str(s).split() if t.strip()]


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["RECORDS"]


def analyze_one(prob, res):
    """返回单组的规则指标，None 表示不适用。"""
    R = res.get("Result")
    gi = res.get("GeneralInfo", {})
    if not isinstance(R, dict):
        return None

    pipes = prob.get("Pipe", []) or []
    stocks = prob.get("Stock", []) or []
    pipe_len_set = {round(_f(p.get("pipe_length"))) for p in pipes}
    stock_len_set = {round(_f(s.get("stock_length"))) for s in stocks}

    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    wp = (R.get("WeldingPattern") or {}).get("WeldingPipe") or []

    # ---- 段长字母表：切法里出现的所有段 ----
    cut_segs = Counter()          # 段长 -> 加权出现次数
    cut_seg_by_pattype = defaultdict(set)  # 段长 -> 出现它的切法(Length,Part)集合
    for c in cp:
        L = _i(c.get("Length"))
        parts = _parts(c.get("Part", ""))
        num = _i(c.get("Number"), 1)
        key = (L, tuple(parts))
        for s in parts:
            cut_segs[s] += num
            cut_seg_by_pattype[s].add(key)

    seg_alpha = set(cut_segs)
    # 段的来源分类
    seg_is_stock = {s for s in seg_alpha if s in stock_len_set}
    seg_is_pipe = {s for s in seg_alpha if s in pipe_len_set}
    seg_is_mid = seg_alpha - seg_is_stock - seg_is_pipe  # 既非整根母料也非整管 -> 真正中间切段

    # 中间切段的跨切法复用：一个中间段被多少种切法用
    mid_reuse = [len(cut_seg_by_pattype[s]) for s in seg_is_mid]

    # ---- 拼法产出的段 vs 切法消耗的段（三分法核心） ----
    weld_segs = Counter()
    weld_pat_per_pipe = defaultdict(set)
    for w in wp:
        fig = w.get("FigureNumber")
        L = _i(w.get("Length"))
        for pat in w.get("Pattern", []):
            parts = _parts(pat.get("Part", ""))
            num = _i(pat.get("Number"), 1)
            for s in parts:
                weld_segs[s] += num
            weld_pat_per_pipe[(fig, L)].add(tuple(parts))

    # 拼法用到但切法没产出的段（应为空 -> 说明切拼共用同一段字母表）
    weld_only = set(weld_segs) - set(cut_segs)
    cut_only = set(cut_segs) - set(weld_segs)  # 切了但没用于拼（可能整根母料余料? 或本身是成品）

    single_pat = sum(1 for v in weld_pat_per_pipe.values() if len(v) == 1)
    total_pipe_types = len(weld_pat_per_pipe)

    return {
        "util": _f(gi.get("UtilRate")),
        "seg_alpha_size": len(seg_alpha),
        "seg_stock": len(seg_is_stock),
        "seg_pipe": len(seg_is_pipe),
        "seg_mid": len(seg_is_mid),
        "mid_reuse_mean": (sum(mid_reuse) / len(mid_reuse)) if mid_reuse else 0.0,
        "mid_reuse_max": max(mid_reuse) if mid_reuse else 0,
        "weld_only_segs": len(weld_only),      # 三分法关键：应≈0
        "cut_only_segs": len(cut_only),
        "cut_types": len({(_i(c.get('Length')), tuple(_parts(c.get('Part','')))) for c in cp}),
        "weld_types": len({(w.get('FigureNumber'), tuple(_parts(p.get('Part',''))))
                           for w in wp for p in w.get('Pattern', [])}),
        "single_pat_ratio": (single_pat / total_pipe_types) if total_pipe_types else 1.0,
    }


def pct(v, q):
    if not v:
        return 0
    v = sorted(v)
    return v[int(round((len(v) - 1) * q))]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"
    recs = load(path)

    out = []
    for r in recs:
        pj, rj = r.get("MOMPROBLEMJSON"), r.get("MOMRESULTJSON")
        if not pj or not rj:
            continue
        try:
            prob = json.loads(pj)
            res = json.loads(rj)
        except Exception:
            continue
        gi = res.get("GeneralInfo", {})
        if not str(gi.get("Result", "")).lower().startswith("success"):
            continue
        util = _f(gi.get("UtilRate"))
        if not (0.5 < util < 1.05):
            continue
        # 主力区间：紧料 0.99~0.999 且母材 100~2000 根
        stocks = prob.get("Stock", []) or []
        pipes = prob.get("Pipe", []) or []
        bars = sum(_i(s.get("stock_demand")) for s in stocks)
        dl = sum(_f(p.get("pipe_length")) * _i(p.get("pipe_demand")) for p in pipes)
        sl = sum(_f(s.get("stock_length")) * _i(s.get("stock_demand")) for s in stocks)
        tight = dl / sl if sl else 0
        if not (0.99 <= tight < 0.999 and 100 <= bars <= 2000):
            continue
        a = analyze_one(prob, res)
        if a:
            out.append(a)

    print(f"主力区间样本数: {len(out)}")
    print()
    print("=== 段长字母表构成 ===")
    for name in ["seg_alpha_size", "seg_stock", "seg_pipe", "seg_mid"]:
        v = [x[name] for x in out]
        print(f"  {name:16s} p50={pct(v,0.5):>6.1f} p90={pct(v,0.9):>6.1f} max={max(v):>6}")
    # 占比
    mid_ratio = [x["seg_mid"] / x["seg_alpha_size"] for x in out if x["seg_alpha_size"]]
    stock_ratio = [x["seg_stock"] / x["seg_alpha_size"] for x in out if x["seg_alpha_size"]]
    pipe_ratio = [x["seg_pipe"] / x["seg_alpha_size"] for x in out if x["seg_alpha_size"]]
    print(f"  整根母料段占字母表: p50={pct(stock_ratio,0.5):.2f}")
    print(f"  整管段占字母表:     p50={pct(pipe_ratio,0.5):.2f}")
    print(f"  中间切段占字母表:   p50={pct(mid_ratio,0.5):.2f} p90={pct(mid_ratio,0.9):.2f}")

    print("\n=== 中间切段的跨切法复用 ===")
    v = [x["mid_reuse_mean"] for x in out]
    print(f"  一个中间段平均被几种切法用: p50={pct(v,0.5):.2f} p90={pct(v,0.9):.2f}")

    print("\n=== 三分法(切拼共用段字母表)验证 ===")
    wo = [x["weld_only_segs"] for x in out]
    co = [x["cut_only_segs"] for x in out]
    print(f"  拼法用到但切法没产出的段数(应≈0): p50={pct(wo,0.5)} p90={pct(wo,0.9)} max={max(wo)}")
    print(f"  完全为0的组占比: {sum(1 for x in wo if x==0)/len(wo):.2%}")
    print(f"  切了但没拼用的段(整根母料余料/成品): p50={pct(co,0.5)} p90={pct(co,0.9)}")

    print("\n=== 单管单拼法 ===")
    sp = [x["single_pat_ratio"] for x in out]
    print(f"  管型只用一种拼法的比例: p50={pct(sp,0.5):.2f} p10={pct(sp,0.1):.2f}")

    print("\n=== 切法/拼法种类 (主力区间) ===")
    for name in ["cut_types", "weld_types"]:
        v = [x[name] for x in out]
        print(f"  {name:12s} p50={pct(v,0.5):>4} p90={pct(v,0.9):>4} max={max(v):>4}")


if __name__ == "__main__":
    main()
