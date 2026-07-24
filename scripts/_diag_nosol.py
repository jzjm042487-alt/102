"""诊断单个无解档: 打印输入参数 + verbose 求解全过程。
用法: python scripts/_diag_nosol.py <idx> [tl]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow, classify_group
import scripts._corpus_worker as w

CORPUS = r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"

idx = int(sys.argv[1])
tl = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
data = json.loads(Path(CORPUS).read_text(encoding="utf-8"))
rec = data["RECORDS"][idx]
res = json.loads(rec["MOMRESULTJSON"])
lg = w.legacy_metrics(res)
g = merge_equivalent_pipes(parse_problem(json.loads(rec["MOMPROBLEMJSON"])).groups[0])

print(f"=== idx={idx} material={rec.get('MOMMATERIAL')} ===")
print("管型:", [(p.length, p.demand, "焊上限", p.max_joints) for p in g.pipes])
print("母料:", [(s.length, s.quantity) for s in g.stocks])
print(f"min_wd={g.min_weld_distance} min_cut={g.min_cut_length} kerf={g.blade_margin} "
      f"target={g.target_rate}")
print("需求总长:", g.demand_length, " 母料总长:",
      sum(s.length * s.quantity for s in g.stocks))
print("老软件:", lg)
print("floor(=legacy_util):", lg["util"])
print("\n--- classify ---")
cls = classify_group(g, verbose=True, util_floor=lg["util"])
print("kind:", cls["kind"], "geom:", cls.get("geom"), "reason:", cls.get("reason"))
print("\n--- solve_arcflow verbose ---")
r = solve_arcflow(g, tl=tl, verbose=True, util_floor=lg["util"])
print("\nRESULT:", None if r is None else {k: r.get(k) for k in
      ("joints", "weld_types", "cut_types", "util", "solver")})
