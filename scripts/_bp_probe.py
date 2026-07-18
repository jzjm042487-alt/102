"""临时探针: 读出 L12/L13 等样本的结构参数, 为 B&P 模块设计提供依据。"""
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
for lv in (int(a) for a in sys.argv[1:]):
    s = next(x for x in samples if x["level"] == lv)
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    stocks = sorted({(st.length, st.quantity) for st in g.stocks})
    print(f"== L{lv} {s['spec']} ==")
    print(f"  legacy={s['legacy']}")
    print(f"  target_rate={g.target_rate:.4f} min_wd={g.min_weld_distance} "
          f"min_cut={g.min_cut_length} kerf={g.blade_margin}")
    print(f"  stocks={stocks}  stock_len={g.stock_length}")
    print(f"  demand_len={g.demand_length}  n_pipe_types={len(g.pipes)}")
    for p in g.pipes:
        print(f"    pipe len={p.length} demand={p.demand} max_joints={p.max_joints} "
              f"forbidden={[(iv.start, iv.end) for iv in p.forbidden]}")
