"""深挖 819b1899: 为何GA拆3段焊(52焊口), 而非整根切(0焊口)?
手动分析 10100 从各定尺整根切的可行性 + 库存约束。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from app.domain import parse_problem
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics

SRC = Path(r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")
data = json.loads(SRC.read_text(encoding="utf-8"))
recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
rec = next(r for r in recs if str(r.get("id") or r.get("ID") or "").startswith("819b1899"))
g = merge_equivalent_pipes(parse_problem(json.loads(rec["MOMPROBLEMJSON"])).groups[0])

pipe = g.pipes[0]
print(f"管长={pipe.length} 需求={pipe.demand} max_joints={pipe.max_joints}")
print(f"禁焊区={[(iv.start, iv.end) for iv in pipe.forbidden]}")
print(f"min_weld_distance={g.min_weld_distance} min_cut={g.min_cut_length}")
print("库存:")
for s in g.stocks:
    print(f"  定尺{s.length} x{s.quantity} (整根切10100? {'能' if s.length >= pipe.length else '不能'}, "
          f"废料{s.length - pipe.length if s.length >= pipe.length else '-'})")

print(f"\n需求总长={g.demand_length}")
# 整根切方案: 需 26 根 10100. 哪些定尺能整根出?
whole_stocks = [s for s in g.stocks if s.length >= pipe.length]
cap = sum(s.quantity for s in whole_stocks)
print(f"能整根切的定尺总库存: {cap} 根 (需求 {pipe.demand} 根)")
if cap >= pipe.demand:
    # 贪心: 用废料最小的定尺
    whole_stocks.sort(key=lambda s: s.length)
    need = pipe.demand
    used_len = 0
    plan = []
    for s in whole_stocks:
        take = min(need, s.quantity)
        if take > 0:
            plan.append((s.length, take))
            used_len += s.length * take
            need -= take
        if need == 0:
            break
    print(f"整根切方案: {plan}")
    print(f"  用料总长={used_len}  利用率={g.demand_length/used_len:.4f}  焊口=0")
else:
    print(f"整根切库存不足({cap}<{pipe.demand}), 必须部分焊接")

# 老软件利用率 0.9926 对应用料
_o, lm = legacy_alpha_and_metrics(rec)
print(f"\n老软件: util={lm['util']:.4f} → 用料={g.demand_length/lm['util']:.0f}  joints={lm['joints']}")
