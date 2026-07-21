import json
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, ".")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow

s = [x for x in json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
     if x["level"] == 4][0]
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
dem = {p.length: p.demand for p in g.pipes}
sq = defaultdict(int)
for st in g.stocks:
    sq[st.length] += st.quantity
print("=== L4", s["spec"], "===")
print(" 管明细", sorted(dem.items()))
print(" 母料", dict(sorted(sq.items())))
print(" kerf", g.blade_margin, "min_cut", g.min_cut_length, "target", round(g.target_rate, 4))
print(" 长度比", round(g.demand_length / g.stock_length, 4))
print(" 老软件", s["legacy"])
print()
r = solve_arcflow(g, tl=120.0, verbose=True, util_floor=s["legacy"]["util"] - 1e-3)
print("=== RESULT ===")
print("joints", r.get("joints"), "util", round(r.get("util"), 4),
      "solver", r.get("solver"), "cut_types", r.get("cut_types"))
print(" V4切法明细:")
for (L, segs), cnt in sorted(r.get("cut_patterns", {}).items() if isinstance(r.get("cut_patterns"), dict) else [((L, tuple(segs)), 0) for (L, segs) in r.get("cut_patterns", [])]):
    pass
cp = r.get("cut_patterns")
if isinstance(cp, dict):
    for (L, segs), cnt in sorted(cp.items()):
        print(f"   母料{L}: {list(segs)} x{cnt}")
else:
    from collections import Counter
    c = Counter((L, tuple(segs)) for (L, segs) in cp)
    for (L, segs), cnt in sorted(c.items()):
        print(f"   母料{L}: {list(segs)} x{cnt}")
