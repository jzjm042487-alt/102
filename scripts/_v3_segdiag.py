"""L13 焊点/段长分布诊断: 看每管焊点数、段长种类来源, 决定如何压种类。
用法: python scripts/_v3_segdiag.py <level> [--step N]
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import _weld_points, build_pipe_arcs

lv = int(sys.argv[1])
step = int(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else None
samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
s = next(x for x in samples if x["level"] == lv)
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
min_wd, min_cut = g.min_weld_distance, g.min_cut_length
max_stock = max(st.length for st in g.stocks)
stock_lens = sorted({st.length for st in g.stocks})
if step is None:
    step = max(1, min_wd)

print(f"L{lv} step={step} max_stock={max_stock} stocks={stock_lens} min_wd={min_wd}")
all_segs = set()
for i, p in enumerate(g.pipes):
    pts = _weld_points(p, step, stock_lens)
    _, arcs = build_pipe_arcs(p, max_stock, min_cut, min_wd, step, stock_lens)
    segs = sorted({seg for (_, _, seg) in arcs})
    all_segs |= set(segs)
    print(f"  pipe L={p.length} demand={p.demand} 焊点数={len(pts)} 弧={len(arcs)} 段长种类={len(segs)}")
    print(f"    焊点(前20)={pts[:20]}")
    print(f"    段长(前25)={segs[:25]}")
print(f"  全局段长种类={len(all_segs)}")
