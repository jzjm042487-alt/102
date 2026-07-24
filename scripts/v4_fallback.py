"""V4 Fallback Ladder — L0 唯一兜底实现点（3c/3d；Phase2 再补 3a/3b）。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _lex_key(sol: dict[str, Any] | None, util_floor: float) -> tuple | None:
    """词典序键: 焊口 → 拼法 → 切法 → 是否低于底线 → 用料。

    util&lt;0.95 的纯切(0 焊口)不再被直接丢弃——否则 FFD 焊接会凭「达 0.95」
    挤掉焊口更优的纯切(D1 回归根因)。
    """
    if sol is None:
        return None
    util = float(sol.get("util") or 0.0)
    joints = int(sol.get("joints") or 0)
    below = 0 if util >= util_floor - 0.005 else 1
    # util<0.95 且已是 0 焊口: 视为「焊口优先可接受」, 不额外惩罚 below
    if joints == 0 and util < 0.95 - 1e-9:
        below = 0
    return (
        joints,
        int(sol.get("weld_types") or 0),
        int(sol.get("cut_types") or 0),
        below,
        int(sol.get("seg_types") or 10**9),
        int(sol.get("used_len") or 0),
    )


def _lex_better(a: dict | None, b: dict | None, util_floor: float) -> dict | None:
    ka, kb = _lex_key(a, util_floor), _lex_key(b, util_floor)
    if ka is None:
        return b if kb is not None else a
    if kb is None:
        return a
    return a if ka <= kb else b


def ladder(
    group,
    *,
    util_floor: float,
    budget_s: float,
    incumbent: dict | None = None,
    recipe=None,
    verbose: bool = False,
) -> dict | None:
    """Phase1 最小梯: 3c FFD→std_compress → 3d pure_cut。"""
    del recipe  # Phase2 3a 用
    from scripts._arcflow_v3 import (
        _ffd_weld_incumbent,
        _try_std_compress,
        classify_group,
        _util_floor,
    )

    uf = _util_floor(group, util_floor)
    best = incumbent
    path: list[str] = list((incumbent or {}).get("solver_path") or [])
    diagnosis: list[str] = list((incumbent or {}).get("diagnosis") or [])
    if isinstance(diagnosis, dict):
        diagnosis = [f"{c}: {m}" for c, m in diagnosis.get("codes") or []]

    tl = max(1.0, float(budget_s))

    # 3c
    from scripts.lex_master import _solution_welds_legal

    ffd = _ffd_weld_incumbent(group)
    if ffd is not None and _solution_welds_legal(group, ffd):
        stock_qty: dict[int, int] = defaultdict(int)
        for st in group.stocks:
            stock_qty[st.length] += st.quantity
        cut_tl = min(max(tl * 1.5, 30.0), 120.0)
        comp = _try_std_compress(
            group, group.pipes, stock_qty, ffd, tl=min(tl, 60.0),
            verbose=verbose, cut_tl=cut_tl,
        )
        if comp is not None and _solution_welds_legal(group, comp):
            comp = dict(comp)
            comp["solver"] = "fallback_3c"
            path3 = path + ["fallback_3c"]
            comp["solver_path"] = path3
            best = _lex_better(best, comp, uf)
            diagnosis.append("fallback_3c=std_compress")
        else:
            ffd = dict(ffd)
            ffd["solver"] = "fallback_3c_ffd_legal"
            ffd["solver_path"] = path + ["fallback_3c_ffd_legal"]
            diagnosis.append("fallback_3c=ffd_legal")
            if best is None:
                best = ffd

    # 3d
    cls = classify_group(group, verbose=verbose, util_floor=uf)
    pure = cls.get("pure_result")
    if pure is not None:
        pure = dict(pure)
        pure["solver"] = "fallback_3d"
        pure["solver_path"] = path + ["fallback_3d"]
        best = _lex_better(best, pure, uf)
        diagnosis.append("fallback_3d=pure_cut")

    if best is not None:
        best = dict(best)
        best.setdefault("solver_path", path + ["ladder"])
        best["diagnosis"] = diagnosis
    return best
