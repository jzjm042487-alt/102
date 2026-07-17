"""逆向老软件算法指纹 —— 从其完整解的结构反推它用了哪种全局策略。

不改任何生产代码，纯只读分析。目的：在投入"全局优化重工程"前，先摸清老软件
到底是"列生成/集合覆盖"还是"遗传/退火"路线，避免抄错方向。

量化 6 个能暴露算法本质的指纹：
  1. 切法复用度：一种切法平均被多少母材使用（Number 分布）。高复用=集合覆盖/列生成典型特征。
  2. 切法/母材比：cut_types / total_cut_bars。极低=强 pattern 复用（全局优化标志）。
  3. 切法段数结构：几段切法各占多少（1段整料 / 2段配对 / 3+段）。
  4. TrimLoss 分布：废料是均摊还是被"挤"到少数母材（全局把 trim 集中的标志）。
  5. 料段配对：切法内"段长"的组合规律——是否大量出现"大段+互补小段"精确配对。
  6. 同管拼法多样性：同一 (figure,jlxh) 管用了几种拼法（固定模板 vs 按料灵活拼）。
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent.parent / "frontend-next" / "public" / "samples.json"

TARGETS = [
    "9f618d9f-5dd5-4a2d-9348-91597d4f0d03",  # A
    "6a4fecf7-d070-4629-8bcc-a0ffa5c3b091",  # B
    "3040bd13-9108-40bf-9900-c5cdeeec51f0",  # C
    "350f2dbb-0858-4398-b00f-b2015be43e58",  # D
]


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _parts(s: str) -> list[int]:
    return [int(round(_f(t))) for t in str(s).split() if t.strip()]


def analyze(sample: dict, tag: str) -> None:
    pr = sample["problem"]
    lg = (sample.get("legacy") or {}).get("Result") or {}
    gi = (sample.get("legacy") or {}).get("GeneralInfo") or {}
    cp = (lg.get("CuttingPattern") or {}).get("CuttingPipe", []) or []
    wp = (lg.get("WeldingPattern") or {}).get("WeldingPipe", []) or []

    pipe_lengths = {round(_f(p.get("pipe_length"))) for p in pr.get("Pipe", [])}

    # --- 指纹1/2: 切法复用度 & 切法/母材比 ---
    reuse = [int(_f(c.get("Number"))) for c in cp]
    total_bars = sum(reuse)
    cut_types = len(cp)

    # --- 指纹3: 切法段数结构 ---
    seg_count_hist = Counter(len(_parts(c.get("Part", ""))) for c in cp)

    # --- 指纹4: TrimLoss 分布 ---
    trims = []
    for c in cp:
        trims.extend([_f(c.get("TrimLoss"))] * int(_f(c.get("Number"))))
    trims_sorted = sorted(trims, reverse=True)
    total_trim = sum(trims)
    # 前10%母材承担了多少比例的废料？（集中度）
    k10 = max(1, len(trims_sorted) // 10)
    top10_trim = sum(trims_sorted[:k10])
    concentration = top10_trim / total_trim if total_trim else 0.0
    zero_trim_bars = sum(1 for t in trims if t < 1)

    # --- 指纹5: 料段"整料/成品"占比 ---
    #  一种切法里，有多少段恰好是某成品管长（整料成品，不需再拼）？
    whole_product_segs = 0
    total_segs = 0
    for c in cp:
        for sl in _parts(c.get("Part", "")):
            total_segs += 1
            if sl in pipe_lengths:
                whole_product_segs += 1

    # --- 指纹6: 同管拼法多样性 ---
    weld_by_pipe: dict[tuple, set] = defaultdict(set)
    weld_pattern_total = 0
    for w in wp:
        key = (str(w.get("FigureNumber")), str(w.get("jlxh")))
        for pat in (w.get("Pattern") or []):
            weld_by_pipe[key].add(tuple(_parts(pat.get("Part", ""))))
            weld_pattern_total += 1
    multi_pattern_pipes = sum(1 for v in weld_by_pipe.values() if len(v) > 1)

    # --- 指纹5b: 母材长度种类（用了几种规格母材）---
    stock_lengths = Counter()
    for s in pr.get("Stock", []):
        stock_lengths[round(_f(s.get("stock_length")))] += int(_f(s.get("stock_demand")))

    print(f"\n===== [{tag}] {sample['id']}  {sample.get('material')} {sample.get('spec')} =====")
    print(f"  规模: {total_bars} 下料母材 / {len(pr.get('Pipe',[]))} 管型 | 利用率={_f(gi.get('UtilRate')):.5f}")
    print(f"  母材规格: {dict(stock_lengths)}")
    print(f"\n  [指纹1] 切法复用度: 切法 {cut_types} 种覆盖 {total_bars} 母材")
    print(f"          平均每种切法用于 {total_bars/cut_types:.1f} 根母材 | Number 分布 min/中位/max = "
          f"{min(reuse)}/{int(statistics.median(reuse))}/{max(reuse)}")
    print(f"  [指纹2] 切法/母材比 = {cut_types/total_bars:.4f}  (越低=pattern复用越强=越像全局优化)")
    print(f"  [指纹3] 切法段数结构: " + ", ".join(f"{k}段×{v}种" for k, v in sorted(seg_count_hist.items())))
    print(f"  [指纹4] TrimLoss: 总废料={total_trim:.0f}mm | 零废料母材={zero_trim_bars}/{total_bars} "
          f"({zero_trim_bars/total_bars*100:.0f}%) | 前10%母材承担 {concentration*100:.0f}% 废料")
    print(f"  [指纹5] 整料成品段: {whole_product_segs}/{total_segs} 段是完整成品管长 "
          f"({whole_product_segs/total_segs*100:.0f}%) —— 高=长短混切/整料直用")
    print(f"  [指纹6] 同管多拼法: {multi_pattern_pipes}/{len(weld_by_pipe)} 根管用了>1种拼法 "
          f"| 拼法总条目={weld_pattern_total}")


def main() -> None:
    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in data["samples"]}
    ids = sys.argv[1:] or TARGETS
    for i, sid in enumerate(ids):
        s = by_id.get(sid)
        if s is None:
            print(f"[{sid}] NOT FOUND")
            continue
        analyze(s, "ABCD"[i] if i < 4 else str(i))


if __name__ == "__main__":
    main()
