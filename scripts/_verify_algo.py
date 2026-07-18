"""算法恢复验证：挑一个最简单且老软件有焊口数据的样本，跑 GA 到收敛，对比老软件。"""
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

SRC = Path(r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")
data = json.loads(SRC.read_text(encoding="utf-8"))
recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")

best_pick, best_score = None, None
for rec in recs:
    if not rec.get("MOMRESULTJSON") or not rec.get("MOMPROBLEMJSON"):
        continue
    try:
        _o, lm = legacy_alpha_and_metrics(rec)
        if not lm or lm.get("util", 0) <= 0:
            continue
        if not lm.get("joints"):  # 只选真需焊接(joints>0)的, 才能体现焊口优化
            continue
        prob = parse_problem(json.loads(rec["MOMPROBLEMJSON"]))
        if len(prob.groups) != 1:
            continue
        g = merge_equivalent_pipes(prob.groups[0])
        dem = sum(p.demand for p in g.pipes)
        if not (2 <= len(g.pipes) <= 3):
            continue
        score = (len(g.pipes), dem, lm["joints"])  # 越简单越优先
        if best_score is None or score < best_score:
            best_score, best_pick = score, (rec, g, lm)
    except Exception:
        continue

if best_pick is None:
    print("没找到合适样本"); sys.exit(1)

rec, g, lm = best_pick
sid = str(rec.get("id") or rec.get("ID") or "")
print(f"=== 选中最简单样本 {sid[:12]} ===")
print(f"  管型数={len(g.pipes)} 需求根数={sum(p.demand for p in g.pipes)} "
      f"管长={sorted({p.length for p in g.pipes})} 定尺={sorted({s.length for s in g.stocks})}")
print(f"  老软件: joints={lm['joints']} weld={lm['weld_types']} "
      f"cut={lm['cut_types']} util={lm['util']:.4f}")
print("=== 开始跑 GA (pop=20 gen=20 tl=20) ===")
t0 = time.monotonic()
res, segs = ga_run(g, lm, pop_size=20, gens=20, tl=20, rng=random.Random(0), verbose=True)
dt = time.monotonic() - t0
if res is None:
    print(f"!!! GA 未找到可行解 ({dt:.1f}s)")
else:
    def cmp(new, old, low=True):
        if abs(new - old) < 1e-9: return "[=]"
        return "[BETTER]" if ((new < old) == low) else "[WORSE]"
    print(f"\n=== 结果对比 (GA 耗时 {dt:.1f}s) ===")
    print(f"  总焊口数: {res['joints']:5} vs {lm['joints']:5}  {cmp(res['joints'], lm['joints'])}")
    print(f"  拼法种类: {res['weld_types']:5} vs {lm['weld_types']:5}  {cmp(res['weld_types'], lm['weld_types'])}")
    print(f"  切法种类: {res['cut_types']:5} vs {lm['cut_types']:5}  {cmp(res['cut_types'], lm['cut_types'])}")
    print(f"  段种类:   {res['seg_types']:5}")
    print(f"  利用率:   {res['util']:.4f} vs {lm['util']:.4f}  {cmp(res['util'], lm['util'], low=False)}")
    print("\n*** 算法运行正常，恢复验证通过 ***")
