"""验证: 放开利用率硬约束(逐步加大slack), 焊口数能降到多少? 
用户认知: 焊口第一, 利用率是自然结果不该当硬门槛。819b1899 用整根+少段段集测试。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from app.domain import parse_problem
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics
from _exp_ga import evaluate

SRC = Path(r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")
data = json.loads(SRC.read_text(encoding="utf-8"))
recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
rec = next(r for r in recs if str(r.get("id") or r.get("ID") or "").startswith("819b1899"))
g = merge_equivalent_pipes(parse_problem(json.loads(rec["MOMPROBLEMJSON"])).groups[0])
_o, lm = legacy_alpha_and_metrics(rec)
target = g.demand_length / lm["util"]

# 段集: 整根10100 + GA的3段(给ILP充分选择空间)
segs = sorted(set([10100, 287, 3994, 5819]))
print(f"段集={segs}")
print(f"老软件: joints={lm['joints']} weld={lm['weld_types']} cut={lm['cut_types']} util={lm['util']:.4f}\n")
print("放开利用率约束(加大slack), 观察焊口数变化:")
for slack in [0.005, 0.02, 0.05, 0.10, 0.20, 0.50]:
    res = evaluate(g, segs, target, slack=slack, time_limit=25, exact=True, indiv=None)
    if res is None:
        print(f"  slack={slack:.3f}: 不可行")
    else:
        print(f"  slack={slack:.3f}: joints={res['joints']:3} weld={res['weld_types']} "
              f"cut={res['cut_types']} seg={res['seg_types']} util={res['util']:.4f}")
