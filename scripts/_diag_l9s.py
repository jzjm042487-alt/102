import json
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, ".")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes

s = [x for x in json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
     if x["level"] == 9][0]
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
dem = {p.length: p.demand for p in g.pipes}
sq = defaultdict(int)
for st in g.stocks:
    sq[st.length] += st.quantity
print("=== L9", s["spec"], "===")
print(" 管明细", sorted(dem.items()))
print(" 母料", dict(sorted(sq.items())), "总根数", sum(sq.values()))
print(" kerf", g.blade_margin, "min_cut", g.min_cut_length, "min_wd", g.min_weld_distance)
print(" 需求总长", g.demand_length, " 库存总长", g.stock_length, " 比", round(g.demand_length/g.stock_length, 4))
print(" 需求根数", sum(dem.values()))
print(" 老软件", s["legacy"])
# 老软件 21 焊口/8切法/5拼法. 需求 3150x89 + 3151x1 = 90根管.
# 母料能装几根 3150? 8000/3150=2, 8600/3150=2, 10400/3150=3
for L in sorted(sq):
    print(f"  母料{L}(x{sq[L]}): 可装3150={L//3150}根 余={L%3150}, 2根后余={L-2*3150}")
