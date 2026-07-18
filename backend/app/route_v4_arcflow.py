"""v4 引擎: V3 arc-flow 全局整数模型 + 分类器路由, 接入生产求解路径。

设计见 docs/V3_HANDOFF.md §0.2/§4.7。本模块只做"薄适配":
  1. 调用 scripts/_arcflow_v3.solve_arcflow(group) 拿到 arc-flow 解;
  2. 把它的 weld_patterns / cut_patterns 翻译成 _WeldCandidate / _CutCandidate;
  3. 复用 solver._assemble_group_result 产出与 baseline/route3 同构的结果 dict。

与 route3 一致: 产出 >= 消耗的余量段在拼装前被回收成料头(drop surplus),
使独立校验器的"段供需相等"检查通过。求解失败/不可行返回 None -> 回退 baseline。
"""
from __future__ import annotations

import sys
import time as _time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .domain import MaterialGroup

# scripts/ 不在 backend 包内。_arcflow_v3.py 内部 `from backend.app...` 需要项目根在
# path(作为 PEP 420 命名空间包解析), 而 `import _arcflow_v3` 本身需要 scripts/ 在 path。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
for _p in (str(_PROJECT_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _reconcile_to_counts(
    group: MaterialGroup, res: dict[str, Any]
) -> tuple[list, list]:
    """把 arc-flow 解翻译成 (_WeldCandidate, qty) 与 (_CutCandidate, qty) 列表。

    arc-flow 允许切出的段多于焊接消耗的段(废料弧). 物理上等价于"少切一段, 留更长料头".
    与 route3._reconcile_to_counts 同法: 把余量段从切法列里剔除, 保证每个剩余切段都被
    焊接消耗 -> 校验器"produced == consumed"成立。
    """
    from .solver import _CutCandidate, _WeldCandidate

    kerf = group.blade_margin

    # 拼层: weld_patterns 键 = (pipe_index, ordered_seq), 值 = 根数。
    # 纯切档 weld_patterns 为空: 每个切段整根即一根管(单段=0 焊口), 按管长合成 1 段拼法,
    # 使 metrics/校验器的"每根管需求由拼层覆盖"成立。
    weld_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    if res.get("weld_patterns"):
        for (pipe_index, seq), count in res["weld_patterns"].items():
            if count <= 0:
                continue
            weld_agg[(pipe_index, tuple(seq))] += count
    else:
        for i, pipe in enumerate(group.pipes):
            if pipe.demand > 0:
                weld_agg[(i, (pipe.length,))] += pipe.demand
    weld_counts = [
        (_WeldCandidate(pipe_index, parts), qty)
        for (pipe_index, parts), qty in sorted(weld_agg.items())
    ]

    # 切层: cut_patterns 键 = (stock_length, sorted_segs), 值 = 根数。
    produced_cnt: dict[int, int] = defaultdict(int)
    for (stock_len, segs), count in res.get("cut_patterns", {}).items():
        for seg in segs:
            produced_cnt[seg] += count
    consumed_cnt: dict[int, int] = defaultdict(int)
    for (pipe_index, seq), count in weld_agg.items():
        for seg in seq:
            consumed_cnt[seg] += count
    surplus: dict[int, int] = {
        seg: produced_cnt[seg] - consumed_cnt.get(seg, 0)
        for seg in produced_cnt
        if produced_cnt[seg] - consumed_cnt.get(seg, 0) > 0
    }

    bar_instances: list[list[int]] = []
    stock_of_bar: list[int] = []
    for (stock_len, segs), count in res.get("cut_patterns", {}).items():
        seg_list = list(segs)
        for _ in range(count):
            bar_instances.append(list(seg_list))
            stock_of_bar.append(stock_len)

    if surplus:
        by_seg_bars: dict[int, list[int]] = defaultdict(list)
        for bi, segs in enumerate(bar_instances):
            for seg in set(segs):
                if seg in surplus:
                    by_seg_bars[seg].append(bi)
        for seg, need in surplus.items():
            removed = 0
            for bi in by_seg_bars.get(seg, []):
                while removed < need and seg in bar_instances[bi]:
                    bar_instances[bi].remove(seg)
                    removed += 1
                if removed >= need:
                    break

    cut_agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for bi, segs in enumerate(bar_instances):
        if not segs:
            continue
        cut_agg[(stock_of_bar[bi], tuple(sorted(segs)))] += 1
    cut_counts = [
        (_CutCandidate(stock_length, parts, kerf, group.kerf_mode), qty)
        for (stock_length, parts), qty in sorted(cut_agg.items())
    ]
    return weld_counts, cut_counts


def solve_group(group: MaterialGroup, time_limit: float) -> dict[str, Any] | None:
    """用 v4(arc-flow + 分类器路由)求解一个物料组。

    返回与 baseline/route3 同构的结果 dict(经 _assemble_group_result), 或 None
    (不可行/无解/缺 PySCIPOpt/部分解) -> 由调用方回退 baseline。不抛异常, 不改 group。
    """
    started = _time.monotonic()
    try:
        from pyscipopt import Model  # noqa: F401
    except Exception:
        return None

    try:
        from _arcflow_v3 import solve_arcflow

        res = solve_arcflow(group, tl=time_limit, verbose=False)
    except Exception:
        return None
    if res is None:
        return None

    from .solver import _assemble_group_result

    weld_counts, cut_counts = _reconcile_to_counts(group, res)
    if not weld_counts or not cut_counts:
        return None

    # 拒绝部分解: 每根管的需求都必须由拼层完全覆盖。
    produced_pipes: dict[int, int] = defaultdict(int)
    for cand, qty in weld_counts:
        produced_pipes[cand.pipe_index] += qty
    for i, pipe in enumerate(group.pipes):
        if produced_pipes.get(i, 0) != pipe.demand:
            return None

    result = _assemble_group_result(
        group, "ARCFLOW-V4", "ARCFLOW_V4", weld_counts, cut_counts,
        _time.monotonic() - started,
    )
    result["metrics"]["solve_status"] = (
        "ARCFLOW_TARGET_REACHED"
        if result["metrics"]["target_reached"]
        else "ARCFLOW_FEASIBLE"
    )
    return result
