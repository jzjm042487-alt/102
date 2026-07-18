"""临时: 验证 L13 单管合法拼法是否存在, 列举合法焊点与可行段序列。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from backend.app.solver import _legal_pattern
from scripts._exp_colgen import merge_equivalent_pipes

samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
s = next(x for x in samples if x["level"] == 13)
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
max_stock = max(st.length for st in g.stocks)
min_wd, min_cut = g.min_weld_distance, g.min_cut_length
print(f"max_stock={max_stock} min_wd={min_wd} min_cut={min_cut}")
for p in g.pipes:
    L = p.length
    legal = [pos for pos in range(1, L) if p.weld_allowed(pos)]
    # 可作为分割点且使两段都<=max_stock的点
    valid = [pos for pos in legal if pos <= max_stock and (L - pos) <= max_stock]
    print(f"\npipe L={L} demand={p.demand} max_joints={p.max_joints}")
    print(f"  合法焊点区间数={len(p.forbidden)} 可焊点总数={len(legal)}")
    print(f"  两段均<={max_stock}的分割点数={len(valid)}")
    if valid:
        print(f"  示例分割点: {valid[:3]}...{valid[-3:]}")
        # 验证一个2段拼法合法性
        pos = valid[len(valid) // 2]
        seq = (pos, L - pos)
        ok = _legal_pattern(p, seq, min_wd, min_cut)
        print(f"  2段拼法 {seq} 合法={ok}")
