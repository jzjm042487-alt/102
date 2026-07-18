"""验证根因: 若段集包含'整根10100', 内层ILP能否自己选出低焊口方案?
对比: (a)只有GA拆分段集 (b)段集额外加入管长本身。"""
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

# 方案a: GA找到的拆分段集
segs_a = [287, 3994, 5819]
# 方案b: 额外加入管长本身(整根) + 那三段
segs_b = sorted(set(segs_a + [10100]))
# 方案c: 只有整根 + 一个补充短段(焊接4根用)
segs_c = sorted(set([10100, 2150, 7950]))  # 10100=2150+7950, 从2250定尺切2150

for name, segs in [("a: GA的3段", segs_a), ("b: 3段+整根10100", segs_b), ("c: 整根+2150+7950", segs_c)]:
    res = evaluate(g, segs, target, slack=0.01, time_limit=20, exact=True, indiv=None)
    if res is None:
        print(f"[{name}] 段集{segs}: 不可行")
    else:
        print(f"[{name}] 段集{segs}:")
        print(f"    joints={res['joints']} weld={res['weld_types']} cut={res['cut_types']} "
              f"seg={res['seg_types']} util={res['util']:.4f}")
print(f"\n老软件: joints={lm['joints']} weld={lm['weld_types']} cut={lm['cut_types']} util={lm['util']:.4f}")
