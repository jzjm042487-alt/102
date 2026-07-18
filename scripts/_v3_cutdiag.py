"""诊断 L13 切层规模爆炸来源: 段长种类数、切层可达节点/弧数。不动代码。
用法: python scripts/_v3_cutdiag.py <level> [--step N]
"""
import json
import sys
from collections import defaultdict
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
step = int(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else None
samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
s = next(x for x in samples if x["level"] == lv)
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
min_wd, min_cut = g.min_weld_distance, g.min_cut_length
max_stock = max(st.length for st in g.stocks)
stock_qty = defaultdict(int)
for st in g.stocks:
    stock_qty[st.length] += st.quantity
if step is None:
    step = max(1, min_wd)
stock_lens = sorted(stock_qty.keys())

seg_lengths = set()
for i, p in enumerate(g.pipes):
    pts, arcs = build_pipe_arcs(p, max_stock, min_cut, min_wd, step, stock_lens)
    for (_, _, seg) in arcs:
        seg_lengths.add(seg)
seg_sorted = sorted(seg_lengths)
print(f"L{lv} step={step} 段长种类={len(seg_sorted)} 范围[{seg_sorted[0]},{seg_sorted[-1]}]")

# 切层可达节点/弧(规范化非增序 arc-flow)
INF = max(seg_sorted) if seg_sorted else 0
for L in stock_qty:
    maxseg = {0: INF}
    order = [0]; idx = 0
    while idx < len(order):
        a = order[idx]; idx += 1
        cap = maxseg[a]
        for seg in seg_sorted:
            if seg > cap:
                break
            b = a + seg
            if b > L:
                continue
            if b not in maxseg:
                maxseg[b] = seg; order.append(b)
            elif seg > maxseg[b]:
                maxseg[b] = seg
    node_set = set(maxseg)
    arcs = 0
    for a in maxseg:
        cap = maxseg[a]
        for seg in seg_sorted:
            if seg > cap:
                break
            b = a + seg
            if b <= L and b in node_set:
                arcs += 1
    print(f"  母料 L={L}: 规范节点={len(maxseg)} 段弧={arcs}")

