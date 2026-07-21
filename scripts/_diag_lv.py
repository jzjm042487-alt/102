import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow

lv = int(sys.argv[1]) if len(sys.argv) > 1 else 9
tl = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
s = [x for x in json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
     if x["level"] == lv][0]
g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
print(f"=== L{lv} {s['spec']} ===")
print(" 老软件", s["legacy"])
uf = s["legacy"]["util"] - 1e-3 if s["legacy"].get("util") else None
r = solve_arcflow(g, tl=tl, verbose=True, util_floor=uf)
print("=== RESULT ===")
print("joints", r.get("joints"), "util", round(r.get("util"), 4),
      "weld_types", r.get("weld_types"), "cut_types", r.get("cut_types"),
      "solver", r.get("solver"))
lg = s["legacy"]
print(f"对比: 焊口 V4={r.get('joints')} vs 老={lg['joints']} | "
      f"拼法 {r.get('weld_types')} vs {lg['weld_types']} | "
      f"切法 {r.get('cut_types')} vs {lg['cut_types']} | "
      f"util {r.get('util'):.4f} vs {lg['util']:.4f}")
