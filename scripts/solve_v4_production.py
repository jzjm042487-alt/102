"""V4 生产编排 L0：engine=v4 唯一入口。

主路径：pure_cut → Weld-then-Cut(S1→S2) → Recipe/Lex refine → ladder
         → fingerprint_setcover（仅焊接路径兜底，不碰 pure_cut 早退）
详见 docs/v4-weld-then-cut-plan.md 、docs/v4-fingerprint-fallback-plan.md
"""
from __future__ import annotations

import time
from typing import Any


def _annotate(
    sol: dict | None, path: list[str], diagnosis: list[str], elapsed: float
) -> dict | None:
    if sol is None:
        return None
    out = dict(sol)
    prev = list(out.get("solver_path") or [])
    merged = list(path)
    for p in prev:
        if p not in merged:
            merged.append(p)
    out["solver_path"] = merged
    diag = list(diagnosis)
    extra = out.get("diagnosis")
    if isinstance(extra, list):
        diag.extend(str(x) for x in extra)
    out["diagnosis"] = diag
    out["elapsed_s"] = elapsed
    return out


def _lex_key(sol: dict) -> tuple:
    return (
        int(sol.get("joints") or 10**9),
        int(sol.get("weld_types") or 10**9),
        int(sol.get("cut_types") or 10**9),
        int(sol.get("seg_types") or 10**9),
        -float(sol.get("util") or 0.0),
    )


def solve(
    group,
    *,
    tl: float,
    util_floor: float | None = None,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """动态预算：先拼后切为主；失败/不达标再 Lex + ladder + 指纹集合覆盖。"""
    from scripts._arcflow_v3 import classify_group, _util_floor as _uf
    from scripts.recipe_builder import build
    from scripts.lex_master import solve as lex_solve
    from scripts.v4_fallback import ladder
    from scripts.weld_then_cut import solve as wtc_solve

    t0 = time.monotonic()
    deadline = t0 + max(1.0, float(tl))
    uf = _uf(group, util_floor)
    path: list[str] = []
    diagnosis: list[str] = []
    best: dict[str, Any] | None = None

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def consider(sol: dict | None, tag: str) -> None:
        nonlocal best
        if sol is None:
            return
        if float(sol.get("util") or 0) < uf - 0.005:
            diagnosis.append(f"{tag}_util_below:{sol.get('util')}")
        if best is None or _lex_key(sol) < _lex_key(best):
            best = sol

    # 1) pure_cut 快路
    cls = classify_group(group, verbose=verbose, util_floor=uf)
    path.append(f"classify:{cls.get('kind')}")
    if cls.get("kind") == "infeasible":
        diagnosis.append(f"infeasible:{cls.get('reason')}")
        return None
    if cls.get("kind") == "pure_cut" and cls.get("pure_result") is not None:
        path.append("pure_cut")
        return _annotate(cls["pure_result"], path, diagnosis, time.monotonic() - t0)
    # util<0.95 且纯切可行: 焊口优先, 即使 kind 被标成 needs_weld 也走纯切
    pure_fb = cls.get("pure_result")
    if (
        pure_fb is not None
        and float(pure_fb.get("util") or 0) < 0.95 - 1e-9
        and int(pure_fb.get("joints") or 0) == 0
    ):
        path.append("pure_cut_joints_first_lt95")
        diagnosis.append(cls.get("reason") or "util<0.95 joints-first pure_cut")
        return _annotate(pure_fb, path, diagnosis, time.monotonic() - t0)

    # 2) 先拼后切主路（needs_weld）— 留预算给 Lex 兜底
    wtc_budget = min(remaining() * 0.55, max(4.0, tl * 0.45))
    wtc_budget = max(1.5, min(wtc_budget, remaining() - 2.0))
    path.append("weld_then_cut")
    try:
        wtc = wtc_solve(group, tl=wtc_budget, util_floor=uf, verbose=verbose)
    except Exception as exc:  # noqa: BLE001
        diagnosis.append(f"wtc_exc:{type(exc).__name__}:{exc}")
        wtc = None

    if wtc is not None:
        consider(wtc, "wtc")
        if float(wtc.get("util") or 0) >= uf - 0.005:
            if remaining() < 3.5:
                return _annotate(wtc, path, diagnosis, time.monotonic() - t0)
            diagnosis.append("wtc_ok_try_refine")
        else:
            diagnosis.append("wtc_util_soft")
    else:
        diagnosis.append("wtc_none")

    # 3) Recipe + LexMaster（refine / 兜底）
    recipe = build(group, util_floor=uf)
    path.append("recipe")
    diagnosis.append(
        f"sigma_weld={recipe.meta.get('sigma_weld_n')} "
        f"sigma_cut={recipe.meta.get('sigma_cut_n')}"
    )
    if recipe.meta.get("hard_unreachable"):
        diagnosis.append(f"hard_unreachable={recipe.meta.get('unreachable_pipes')}")
        path.append("ladder")
        fb = ladder(
            group,
            util_floor=uf,
            budget_s=max(1.0, remaining()),
            recipe=recipe,
            incumbent=best,
            verbose=verbose,
        )
        consider(fb, "ladder")
        if best is None and remaining() > 1.0:
            path.append("fingerprint_setcover")
            try:
                from scripts.teacher_inspired_construct_v0 import (
                    solve_fingerprint_setcover,
                )

                fps = solve_fingerprint_setcover(group, tl=max(1.0, remaining()))
                consider(fps, "fp_setcover")
            except Exception as exc:  # noqa: BLE001
                diagnosis.append(f"fp_setcover_exc:{type(exc).__name__}:{exc}")
        if best is not None:
            return _annotate(best, path, diagnosis, time.monotonic() - t0)
        return None

    lex_budget = max(1.0, min(remaining() * 0.8, remaining() - 0.8))
    try:
        sol = lex_solve(
            group, recipe, util_floor=uf, tl=lex_budget, verbose=verbose
        )
        path.append("lex_master")
        consider(sol, "lex")
    except Exception as exc:  # noqa: BLE001
        diagnosis.append(f"lex_exc:{type(exc).__name__}:{exc}")

    if (
        best is not None
        and float(best.get("util") or 0) >= uf - 0.005
        and remaining() < 1.2
    ):
        return _annotate(best, path, diagnosis, time.monotonic() - t0)

    # 4) Fallback ladder
    path.append("ladder")
    fb = ladder(
        group,
        util_floor=uf,
        budget_s=max(1.0, remaining() * 0.55),
        incumbent=best,
        recipe=recipe,
        verbose=verbose,
    )
    consider(fb, "ladder")

    # 5) 指纹集合覆盖兜底（仅焊接路径；pure_cut 早退不会走到这里）
    need_fp = best is None or float(best.get("util") or 0) < uf - 0.005
    if need_fp and remaining() > 1.5:
        path.append("fingerprint_setcover")
        try:
            from scripts.teacher_inspired_construct_v0 import (
                solve_fingerprint_setcover,
            )

            fps = solve_fingerprint_setcover(group, tl=max(1.5, remaining()))
            if fps is not None:
                diagnosis.append(
                    f"fp_setcover_j={fps.get('joints')} u={fps.get('util')}"
                )
            consider(fps, "fp_setcover")
        except Exception as exc:  # noqa: BLE001
            diagnosis.append(f"fp_setcover_exc:{type(exc).__name__}:{exc}")

    if best is not None:
        return _annotate(best, path, diagnosis, time.monotonic() - t0)

    diagnosis.append("all_none")
    return None
