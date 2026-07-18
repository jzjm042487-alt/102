"""统计有焊口样本的难度分布: 管长/最大定尺比值(≈每管段数), 帮助理解样本结构。"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
idx = json.loads((ROOT / "data" / "sample_index.json").read_text(encoding="utf-8"))

weld = [r for r in idx if r.get("legacy_joints")]
print(f"总样本 {len(idx)}, 有焊口(需拼接) {len(weld)}")

# 按 最大管长/最大定尺 的比值分桶(≈每管至少段数)
buckets = Counter()
for r in weld:
    ratio = max(r["pipe_lens"]) / max(r["stock_lens"])
    seg = int(ratio) + 1  # 至少段数
    buckets[seg] += 1
print("\n有焊口样本按 '最长管需切成几段' 分布:")
for seg in sorted(buckets):
    print(f"  {seg} 段起: {buckets[seg]} 个样本")

# 挑几个"最长管只需2段"(最简单的需焊接)样本
simple_weld = [r for r in weld if max(r["pipe_lens"]) <= 2 * max(r["stock_lens"])
               and r["n_pipes"] <= 4 and r["demand"] <= 30]
simple_weld.sort(key=lambda r: (r["n_pipes"], r["legacy_joints"]))
print(f"\n'最长管≤2段' 且 ≤4管型 ≤30需求 的候选: {len(simple_weld)}")
for r in simple_weld[:10]:
    print(f"  {r['id'][:12]} 管型{r['n_pipes']} 需求{r['demand']} "
          f"joints{r['legacy_joints']} weld{r['legacy_weld']} cut{r['legacy_cut']} "
          f"管长{r['pipe_lens']} 定尺{r['stock_lens']}")
