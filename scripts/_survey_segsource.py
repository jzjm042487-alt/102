"""深挖：段长字母表里的"中间切段"到底是怎么来的？

对每个中间段长 s（既非整根母料、也非整管），检验它能否被下列规则解释：
  R1  s = 某个整管长 L_pipe                              -> 就是整管（前一脚本已归为 seg_pipe，这里再细看组合）
  R2  s = 母料长 - k*整管 或 母料长 - Σ整管               -> 母料切掉若干整管后的余段
  R3  s = 管长 - Σ其他段（拼接补齐段）                     -> 拼接时为凑满管长而切的补段
  R4  s = 两个/多个更小中间段之和                          -> 派生段
  R5  s 与库存定尺同余 / 与管长同余
统计每条规则的解释覆盖率，看主导机制是什么。
"""

from __future__ import annotations

import json
import sys
from collections import Counter


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


def subset_sums(vals, cap, maxk=4):
    """所有由 vals 中<=maxk个元素构成、且<=cap 的和（含单元素）。返回集合。"""
    sums = {0}
    for _ in range(maxk):
        new = set()
        for base in sums:
            for v in vals:
                t = base + v
                if 0 < t <= cap:
                    new.add(t)
        sums |= new
    sums.discard(0)
    return sums


def analyze_one(prob, res, tol=5):
    R = res.get("Result")
    if not isinstance(R, dict):
        return None
    pipes = prob.get("Pipe", []) or []
    stocks = prob.get("Stock", []) or []
    pipe_lens = sorted({round(_f(p.get("pipe_length"))) for p in pipes if _f(p.get("pipe_length")) > 0})
    stock_lens = sorted({round(_f(s.get("stock_length"))) for s in stocks if _f(s.get("stock_length")) > 0})
    if not pipe_lens or not stock_lens:
        return None
    max_stock = max(stock_lens)

    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    seg_alpha = set()
    for c in cp:
        seg_alpha.update(_parts(c.get("Part", "")))
    if not seg_alpha:
        return None

    stock_set = set(stock_lens)
    pipe_set = set(pipe_lens)
    mid = sorted(s for s in seg_alpha if s not in stock_set and s not in pipe_set)
    if not mid:
        return {"n_mid": 0, "R1": 0, "R2": 0, "R3": 0, "R4": 0, "unexplained": 0}

    # 预生成候选集合
    pipe_sums = subset_sums(pipe_lens, max_stock, maxk=4)         # 母料里放整管的组合
    # R2: 母料 - 管组合
    r2_set = set()
    for st in stock_lens:
        for ps in pipe_sums:
            d = st - ps
            if d > 0:
                r2_set.add(d)
    # R3: 管长 - 段组合（用 mid+stock 里较小段近似）—— 用管长减去其它段
    small = [x for x in (mid + stock_lens) if x <= max(pipe_lens)]
    small_sums = subset_sums(small, max(pipe_lens), maxk=3)
    r3_set = set()
    for pl in pipe_lens:
        for ss in small_sums:
            d = pl - ss
            if d > 0:
                r3_set.add(d)

    def near(x, S):
        return any(abs(x - y) <= tol for y in S)

    counts = {"R1": 0, "R2": 0, "R3": 0, "R4": 0, "unexplained": 0}
    mid_arr = mid
    for s in mid_arr:
        if near(s, pipe_set):
            counts["R1"] += 1
        elif near(s, r2_set):
            counts["R2"] += 1
        elif near(s, r3_set):
            counts["R3"] += 1
        else:
            # R4: 两个更小中间段之和
            found = False
            for i in range(len(mid_arr)):
                for j in range(i, len(mid_arr)):
                    if abs(mid_arr[i] + mid_arr[j] - s) <= tol and mid_arr[i] != s and mid_arr[j] != s:
                        found = True
                        break
                if found:
                    break
            if found:
                counts["R4"] += 1
            else:
                counts["unexplained"] += 1
    counts["n_mid"] = len(mid_arr)
    return counts


def pct(v, q):
    if not v:
        return 0
    v = sorted(v)
    return v[int(round((len(v) - 1) * q))]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"
    recs = load(path)
    agg = Counter()
    per_group_unexpl = []
    n = 0
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
        stocks = prob.get("Stock", []) or []
        pipes = prob.get("Pipe", []) or []
        bars = sum(_i(s.get("stock_demand")) for s in stocks)
        dl = sum(_f(p.get("pipe_length")) * _i(p.get("pipe_demand")) for p in pipes)
        sl = sum(_f(s.get("stock_length")) * _i(s.get("stock_demand")) for s in stocks)
        tight = dl / sl if sl else 0
        if not (0.99 <= tight < 0.999 and 100 <= bars <= 2000):
            continue
        a = analyze_one(prob, res)
        if not a or a["n_mid"] == 0:
            continue
        n += 1
        for k in ("R1", "R2", "R3", "R4", "unexplained", "n_mid"):
            agg[k] += a[k]
        per_group_unexpl.append(a["unexplained"] / a["n_mid"] if a["n_mid"] else 0)

    tot = agg["n_mid"]
    print(f"分析组数: {n}, 中间段总数: {tot}")
    print("中间段来源解释覆盖率 (容差 5mm):")
    for k in ("R1", "R2", "R3", "R4", "unexplained"):
        print(f"  {k:12s} {agg[k]:>7} ({agg[k]/tot:.1%})")
    print(f"\n每组未解释比例: p50={pct(per_group_unexpl,0.5):.2%} "
          f"p90={pct(per_group_unexpl,0.9):.2%}")
    print("R1=整管 R2=母料-整管组合(余段) R3=管长-段组合(补段) R4=小段之和")


if __name__ == "__main__":
    main()
