import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes

lv = int(sys.argv[1]) if len(sys.argv) > 1 else 7
samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
s = next(x for x in samples if x["level"] == lv)
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
print(f"L{lv} {s['spec']}  老软件 {s['legacy']}")
print(f"min_weld_distance={g.min_weld_distance} min_cut={g.min_cut_length} target_rate={g.target_rate}")
print("管型(长,需求,max_joints,禁焊区):")
for p in g.pipes:
    print(f"  L={p.length} demand={p.demand} max_joints={p.max_joints} forbidden={p.forbidden}")
print("库存(定尺长,数量,必用):")
for st in g.stocks:
    print(f"  {st.length} x{st.quantity} must_use={st.must_use_quantity}")
print(f"需求总长={g.demand_length}  库存总长={g.stock_length}")
print(f"理论最优用料(=需求长/target): {g.demand_length/g.target_rate:.0f}")
