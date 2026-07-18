"""从索引秒选中等难度样本, 直接跑 GA(带硬性子进程无关的进度)。"""
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

idx = json.loads((ROOT / "data" / "sample_index.json").read_text(encoding="utf-8"))
SRC = Path(r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")

# 中等难度: 4-6 管型, 有焊口, 需求适中. 管长不超过定尺太多(避免深段拆分栈溢出),
# 先验证典型中等样本; 超长管(需拆3+段)的栈溢出单独作为已知bug修复。
med = [r for r in idx if r.get("legacy_joints") and 4 <= r["n_pipes"] <= 6
       and r["demand"] <= 60 and len(r["pipe_lens"]) <= 6
       and max(r["pipe_lens"]) <= 2 * max(r["stock_lens"])]
med.sort(key=lambda r: r["legacy_joints"])
if not med:
    print("索引里无符合条件样本"); sys.exit(1)
pick = med[len(med) // 2]
sid = pick["id"]
print(f"候选 {len(med)} 个, 选中位难度: {sid[:12]}")
print(f"  管型={pick['n_pipes']} 需求={pick['demand']} 管长={pick['pipe_lens']} 定尺={pick['stock_lens']}")
print(f"  老软件: joints={pick['legacy_joints']} weld={pick['legacy_weld']} cut={pick['legacy_cut']} util={pick['legacy_util']}")

data = json.loads(SRC.read_text(encoding="utf-8"))
recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
rec = next(r for r in recs if str(r.get("id") or r.get("ID") or "") == sid)
g = merge_equivalent_pipes(parse_problem(json.loads(rec["MOMPROBLEMJSON"])).groups[0])
_o, lm = legacy_alpha_and_metrics(rec)

print(f"=== 开始跑 GA (pop=30 gen=25 tl=25) ===", flush=True)
t0 = time.monotonic()
res, segs = ga_run(g, lm, pop_size=30, gens=25, tl=25, rng=random.Random(0), verbose=True)
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
