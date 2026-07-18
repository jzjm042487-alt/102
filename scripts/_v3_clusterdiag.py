"""检查 L13 段长聚类潜力: 不同容差下段长种类能压到多少。
用法: python scripts/_v3_clusterdiag.py <level>
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
from scripts._arcflow_v3 import build_pipe_arcs

lv = int(sys.argv[1])
s = next(x for x in json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8")) if x["level"] == lv)
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
min_wd, min_cut = g.min_weld_distance, g.min_cut_length
max_stock = max(st.length for st in g.stocks)
stock_lens = sorted({st.length for st in g.stocks})
step = max(1, min_wd)

segs = set()
for p in g.pipes:
    _, arcs = build_pipe_arcs(p, max_stock, min_cut, min_wd, step, stock_lens)
    for (_, _, seg) in arcs:
        segs.add(seg)
segs = sorted(segs)
print(f"L{lv} 原始段长种类={len(segs)} kerf={g.blade_margin} min_cut={min_cut}")

def cluster(vals, tol):
    """相邻差<=tol 合并为一类, 取类内最大值(向上取, 保守不违反 min_wd)。"""
    out = []
    grp = [vals[0]]
    for v in vals[1:]:
        if v - grp[-1] <= tol:
            grp.append(v)
        else:
            out.append(max(grp)); grp = [v]
    out.append(max(grp))
    return out

for tol in [1, 5, 10, 20, 50, 100]:
    c = cluster(segs, tol)
    print(f"  tol={tol:>4}mm -> 段长种类={len(c)}")
