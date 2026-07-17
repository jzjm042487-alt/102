"""诚实性检验：拆穿 _survey_segsource.py 的 R3 是否循环论证。

R3 判定用 (mid ∪ stock) 的组合去减管长来解释 mid 段——mid 本身就是答案，
等于"用答案解释答案"。本脚本做三组对照：
  H0  只用 stock_lens（不含任何 mid 答案）作为可减集合去解释 mid       —— 真正的先验能力
  H1  用 stock + pipe 作为可减集合                                    —— 加上整管
  H2  原口径 mid+stock（会泄漏）                                       —— 复现原来的 90.8%
再用一个 sanity check：把 mid 换成"同样数量的随机段长"，看 H2 口径能解释多少
（若随机段也能被高覆盖，说明这个检验根本没有判别力）。
"""

from __future__ import annotations

import json
import random
import sys


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


def subset_sums(vals, cap, maxk=3):
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


def explain(mid, pipe_lens, reduce_set, tol=5):
    """mid 中有多少段能被 (管长 - reduce_set 组合) 解释。"""
    if not pipe_lens:
        return 0
    maxpl = max(pipe_lens)
    small = [x for x in reduce_set if x <= maxpl]
    sums = subset_sums(small, maxpl, maxk=3)
    r3 = set()
    for pl in pipe_lens:
        for ss in sums:
            d = pl - ss
            if d > 0:
                r3.add(d)
    cnt = 0
    for s in mid:
        if any(abs(s - y) <= tol for y in r3):
            cnt += 1
    return cnt


def analyze(prob, res):
    R = res.get("Result")
    if not isinstance(R, dict):
        return None
    pipes = prob.get("Pipe", []) or []
    stocks = prob.get("Stock", []) or []
    pipe_lens = sorted({round(_f(p.get("pipe_length"))) for p in pipes if _f(p.get("pipe_length")) > 0})
    stock_lens = sorted({round(_f(s.get("stock_length"))) for s in stocks if _f(s.get("stock_length")) > 0})
    if not pipe_lens or not stock_lens:
        return None
    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    seg_alpha = set()
    for c in cp:
        seg_alpha.update(_parts(c.get("Part", "")))
    stock_set, pipe_set = set(stock_lens), set(pipe_lens)
    mid = sorted(s for s in seg_alpha if s not in stock_set and s not in pipe_set)
    if not mid:
        return None

    maxpl = max(pipe_lens)
    # 随机对照：同数量、同范围的随机段
    rnd = sorted(random.randint(50, maxpl) for _ in range(len(mid)))

    h0 = explain(mid, pipe_lens, stock_lens)                    # 只用定尺（先验）
    h1 = explain(mid, pipe_lens, stock_lens + pipe_lens)        # 定尺+整管
    h2 = explain(mid, pipe_lens, list(set(mid)) + stock_lens)   # 原口径(泄漏)
    hr = explain(rnd, pipe_lens, list(set(rnd)) + stock_lens)   # 随机段用泄漏口径
    return len(mid), h0, h1, h2, hr


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"
    random.seed(0)
    recs = load(path)
    tot = h0 = h1 = h2 = hr = 0
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
        a = analyze(prob, res)
        if not a:
            continue
        m, a0, a1, a2, ar = a
        tot += m; h0 += a0; h1 += a1; h2 += a2; hr += ar
        n += 1

    print(f"组数 {n}, 中间段总数 {tot}")
    print(f"  H0 只用定尺(先验，无泄漏)          : {h0/tot:.1%}")
    print(f"  H1 定尺+整管(先验)                 : {h1/tot:.1%}")
    print(f"  H2 原口径 mid+stock (数据泄漏)      : {h2/tot:.1%}  <- 之前报的90.8%")
    print(f"  随机段 用H2口径(判别力 sanity)      : {hr/tot:.1%}  <- 越高说明检验越没用")


if __name__ == "__main__":
    main()
