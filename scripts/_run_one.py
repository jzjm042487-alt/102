"""跑指定中等样本 819b1899, 对比老软件。"""
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from app.domain import parse_problem
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics
from _exp_ga import ga_run

SID = sys.argv[1] if len(sys.argv) > 1 else "819b1899"
SRC = Path(r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")
data = json.loads(SRC.read_text(encoding="utf-8"))
recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
rec = next(r for r in recs if str(r.get("id") or r.get("ID") or "").startswith(SID))
g = merge_equivalent_pipes(parse_problem(json.loads(rec["MOMPROBLEMJSON"])).groups[0])
_o, lm = legacy_alpha_and_metrics(rec)

print(f"=== 样本 {SID} ===")
print(f"  管型={len(g.pipes)} 需求={sum(p.demand for p in g.pipes)} "
      f"管长={sorted({p.length for p in g.pipes})} 定尺={sorted({s.length for s in g.stocks})}")
print(f"  老软件: joints={lm['joints']} weld={lm['weld_types']} cut={lm['cut_types']} util={lm['util']:.4f}")
print(f"=== 跑 GA (pop=30 gen=25 tl=20) ===", flush=True)
t0 = time.monotonic()
res, segs = ga_run(g, lm, pop_size=30, gens=25, tl=20, rng=random.Random(0), verbose=True)
dt = time.monotonic() - t0
if res is None:
    print(f"!!! GA 未找到可行解 ({dt:.1f}s)")
else:
    def cmp(new, old, low=True):
        if abs(new - old) < 1e-9: return "[=]"
        return "[BETTER]" if ((new < old) == low) else "[WORSE]"
    print(f"\n=== 对比 (GA 耗时 {dt:.1f}s) ===")
    print(f"  总焊口数: {res['joints']:5} vs {lm['joints']:5}  {cmp(res['joints'], lm['joints'])}")
    print(f"  拼法种类: {res['weld_types']:5} vs {lm['weld_types']:5}  {cmp(res['weld_types'], lm['weld_types'])}")
    print(f"  切法种类: {res['cut_types']:5} vs {lm['cut_types']:5}  {cmp(res['cut_types'], lm['cut_types'])}")
    print(f"  段种类:   {res['seg_types']:5}  → {res['segs']}")
    print(f"  利用率:   {res['util']:.4f} vs {lm['util']:.4f}  {cmp(res['util'], lm['util'], low=False)}")
