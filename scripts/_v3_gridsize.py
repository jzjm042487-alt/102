"""量化 arc-flow 在不同焊点粒度下的弧规模, 为 V3 建图选粒度提供依据。不动代码。

对每个样本, 报告拼层在几种焊点候选策略下的节点/弧规模:
  A. 全毫米合法焊点(每个 weld_allowed 位置)
  B. min_wd 网格 (每 min_wd 一个候选点, 落到最近合法焊点)
  C. min_wd/2 网格
段长上界统一 = max_stock。

用法: python scripts/_v3_gridsize.py [levels...]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes

samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
levels = [int(a) for a in sys.argv[1:]] or [7, 9, 10, 12, 13, 17]


def count_arcs(pts, max_stock, min_seg, min_wd, L):
    n = len(pts)
    arcs = 0
    for ai in range(n):
        a = pts[ai]
        for bi in range(ai + 1, n):
            b = pts[bi]
            seg = b - a
            if seg > max_stock:
                break
            if seg < min_seg:
                continue
            if a > 0 and b < L and seg < min_wd:
                continue
            arcs += 1
    return arcs


def grid_points(pipe, step):
    """在 step 网格上取候选点, 落到最近合法焊点; 含端点。"""
    L = pipe.length
    pts = {0, L}
    pos = step
    while pos < L:
        # 找最近合法焊点
        best = None
        for d in range(0, step):
            for cand in (pos + d, pos - d):
                if 0 < cand < L and pipe.weld_allowed(cand):
                    best = cand
                    break
            if best is not None:
                break
        if best is not None:
            pts.add(best)
        pos += step
    return sorted(pts)


for lv in levels:
    s = next((x for x in samples if x["level"] == lv), None)
    if s is None:
        continue
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    max_stock = max(st.length for st in g.stocks)
    min_wd = g.min_weld_distance
    min_seg = max(g.min_cut_length, 1)
    print(f"== L{lv} {s['spec']} max_stock={max_stock} min_wd={min_wd} ==")
    for p in g.pipes:
        L = p.length
        full = [0] + [pos for pos in range(1, L) if p.weld_allowed(pos)] + [L]
        gA = count_arcs(full, max_stock, min_seg, min_wd, L)
        gB_pts = grid_points(p, min_wd)
        gB = count_arcs(gB_pts, max_stock, min_seg, min_wd, L)
        gC_pts = grid_points(p, max(1, min_wd // 2))
        gC = count_arcs(gC_pts, max_stock, min_seg, min_wd, L)
        print(f"  pipe L={L} 合法焊点={len(full)-2} "
              f"| A全mm弧={gA} | B(step={min_wd})点={len(gB_pts)}弧={gB} "
              f"| C(step={min_wd//2})点={len(gC_pts)}弧={gC}")
    print()
