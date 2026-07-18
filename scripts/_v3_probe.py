"""V3 arc-flow 建图前置探针: 读出 L7/L9/L10/L12/L13/L17 的结构参数,
评估 arc-flow 图规模(节点=定尺位置, 弧=段), 判定可行性。不动生效代码。

用法: python scripts/_v3_probe.py [levels...]
默认扫 7 9 10 12 13 17。
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

for lv in levels:
    s = next((x for x in samples if x["level"] == lv), None)
    if s is None:
        print(f"== L{lv}: 样本缺失 ==")
        continue
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    stocks = sorted({(st.length, st.quantity) for st in g.stocks})
    max_stock = max(st.length for st in g.stocks)
    total_stock_qty = sum(st.quantity for st in g.stocks)
    total_demand = sum(p.demand for p in g.pipes)
    print(f"== L{lv} {s['spec']} ==")
    print(f"  legacy={s['legacy']}")
    print(f"  target_rate={g.target_rate:.4f} min_wd={g.min_weld_distance} "
          f"min_cut={g.min_cut_length} kerf={g.blade_margin} min_remnant={g.min_reusable_remnant}")
    print(f"  stocks={stocks} max_stock={max_stock} total_stock_qty={total_stock_qty}")
    print(f"  demand_len={g.demand_length} stock_len={g.stock_length} "
          f"slack={g.stock_length - g.demand_length} "
          f"slack%={100*(g.stock_length-g.demand_length)/g.stock_length:.3f}%")
    print(f"  n_pipe_types={len(g.pipes)} total_demand={total_demand}")
    # arc-flow 图规模评估: 节点 = 每种定尺长度的位置 [0..L]
    arc_nodes = sum(L + 1 for (L, _) in stocks)
    # 管>料 判定
    pipe_gt_stock = [p for p in g.pipes if p.length > max_stock]
    for p in g.pipes:
        rel = "管>料" if p.length > max_stock else "管<=料"
        print(f"    pipe len={p.length} demand={p.demand} max_joints={p.max_joints} "
              f"{rel} forbidden={[(iv.start, iv.end) for iv in p.forbidden]}")
    print(f"  arc-flow 节点估算(母料位置和)={arc_nodes} 管>料数={len(pipe_gt_stock)}")
    print()
