"""无泄漏验证：老软件的中间段长是否落在'量化/受控'的小集合上（对齐文献的 special-length 思想）。

不喂答案。只看：老软件解里的中间段长，是否满足下列'受控'特征：
  Q1  段长是否按某公差(50/100mm)量化——即末两位是否高度集中（不是任意毫米）
  Q2  段长与'库存定尺'的关系：段长 mod 定尺 的分布（是否常等于 定尺-余段）
  Q3  一个组里中间段长的'取值密度'：跨度/种类数 —— 是否远小于可能范围（说明被压缩到少数刻度）
  Q4  跨样本：相同材质规格下，中间段长集合是否稳定复用（说明是'标准下料长度库'）
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


def mids_of(prob, res):
    R = res.get("Result")
    if not isinstance(R, dict):
        return None
    pipes = prob.get("Pipe", []) or []
    stocks = prob.get("Stock", []) or []
    pipe_set = {round(_f(p.get("pipe_length"))) for p in pipes}
    stock_set = {round(_f(s.get("stock_length"))) for s in stocks}
    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    seg = set()
    for c in cp:
        seg.update(_parts(c.get("Part", "")))
    mid = sorted(s for s in seg if s not in pipe_set and s not in stock_set)
    return mid, sorted(stock_set), sorted(pipe_set)


def pct(v, q):
    if not v:
        return 0
    v = sorted(v)
    return v[int(round((len(v) - 1) * q))]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"
    recs = load(path)

    last2 = Counter()          # 段长 mod 100（量化检验）
    last1 = Counter()          # 段长 mod 10
    tot_mid = 0
    density = []               # 每组 中间段种类 / (跨度/最小刻度) 的压缩比
    by_matspec = defaultdict(list)  # (mat,spec) -> [中间段集合]

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
        m = mids_of(prob, res)
        if not m:
            continue
        mid, stocks, pipes = m
        if not mid:
            continue
        for s in mid:
            last2[s % 100] += 1
            last1[s % 10] += 1
            tot_mid += 1
        span = max(mid) - min(mid) if len(mid) > 1 else 1
        # 若按 50mm 刻度，可能取值数
        possible = max(1, span // 50)
        density.append(len(mid) / possible)
        key = (r.get("MOMMATERIAL"), prob.get("specifications"))
        by_matspec[key].append(frozenset(mid))

    print(f"中间段总数: {tot_mid}")
    print("\n=== Q1 量化检验：段长 mod 10 分布 ===")
    for k in range(10):
        print(f"  末位 {k}: {last1.get(k,0)/tot_mid:.1%}")
    print("  段长 mod 100 top10:", [(k, round(v/tot_mid,3)) for k, v in last2.most_common(10)])
    zero_mod10 = last1.get(0, 0) / tot_mid
    zero_mod50 = sum(v for k, v in last2.items() if k % 50 == 0) / tot_mid
    zero_mod100 = last2.get(0, 0) / tot_mid
    print(f"  能被10整除: {zero_mod10:.1%}  能被50整除: {zero_mod50:.1%}  能被100整除: {zero_mod100:.1%}")

    print("\n=== Q3 取值密度（中间段种类 / 按50mm刻度的可能取值数） ===")
    print(f"  p50={pct(density,0.5):.3f} p90={pct(density,0.9):.3f}  (越小=越被压到少数刻度)")

    print("\n=== Q4 同材质规格跨样本段长复用（标准下料长度库？） ===")
    reuse_scores = []
    multi = 0
    for key, sets in by_matspec.items():
        if len(sets) < 2:
            continue
        multi += 1
        # 两两 Jaccard 平均
        js = []
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                a, b = sets[i], sets[j]
                if a or b:
                    js.append(len(a & b) / len(a | b))
        if js:
            reuse_scores.append(sum(js) / len(js))
    print(f"  有多样本的材质规格数: {multi}")
    print(f"  同规格跨样本段长 Jaccard 相似度: p50={pct(reuse_scores,0.5):.2f} "
          f"p90={pct(reuse_scores,0.9):.2f}  (高=有稳定标准长度库)")


if __name__ == "__main__":
    main()
