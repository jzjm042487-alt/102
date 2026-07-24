"""LexMaster v1 — Recipe 有限列词典序 ILP。

Pass A: min joints（Recipe 列 → FFD 列 → FFD 原解）
Pass B/C: 在 Pass A 实际段集 σ_A 上压拼法/切法种类（复用 _compress_types）
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from scripts.recipe_builder import Recipe

COL_CAP_DEFAULT = 30_000


def _pattern_weld_legal(pipe, seq: tuple[int, ...]) -> bool:
    """拼法各内部焊口不得落在禁焊区。"""
    if sum(seq) != pipe.length:
        return False
    pos = 0
    for seg in seq[:-1]:
        pos += seg
        if not pipe.weld_allowed(pos):
            return False
    return True


def _solution_welds_legal(group, sol: dict) -> bool:
    for (i, seq), qty in (sol.get("weld_patterns") or {}).items():
        if qty <= 0:
            continue
        if not _pattern_weld_legal(group.pipes[i], tuple(seq)):
            return False
    return True


def _stock_qty(group) -> dict[int, int]:
    qty: dict[int, int] = {}
    for st in group.stocks:
        qty[st.length] = qty.get(st.length, 0) + st.quantity
    return qty


def _util_floor(group, util_floor: float | None) -> float:
    if util_floor is not None:
        return max(0.95, float(util_floor))
    if group.target_rate is not None:
        return max(0.95, float(group.target_rate))
    return 0.95


def _build_pipe_pats(group, recipe: Recipe, seg_set: set[int]) -> dict[int, list[tuple[int, ...]]] | None:
    from scripts._arcflow_v3 import _enum_pipe_patterns, _ffd_weld_incumbent
    from scripts.recipe_builder import _explicit_patterns

    min_wd = group.min_weld_distance
    min_cut = group.min_cut_length
    max_stock = max(s.length for s in group.stocks)
    stock_lens = sorted(_stock_qty(group))
    pipes = group.pipes
    out: dict[int, list[tuple[int, ...]]] = {i: [] for i in range(len(pipes))}

    for item in recipe.seed_weld or []:
        if not item:
            continue
        i, seq = item[0], tuple(item[1])
        if (
            i in out
            and all(s in seg_set for s in seq)
            and _pattern_weld_legal(pipes[i], seq)
        ):
            out[i].append(seq)

    for i, p in enumerate(pipes):
        for seq in _explicit_patterns(
            p, max_stock, max(min_cut, 1), min_wd, stock_lens, _stock_qty(group)
        ):
            if all(s in seg_set for s in seq) and _pattern_weld_legal(p, seq):
                out[i].append(seq)

    try:
        ffd = _ffd_weld_incumbent(group)
        if ffd and ffd.get("weld_patterns"):
            for (i, seq), _qty in ffd["weld_patterns"].items():
                seq_t = tuple(seq)
                if (
                    i in out
                    and all(s in seg_set for s in seq_t)
                    and _pattern_weld_legal(pipes[i], seq_t)
                ):
                    out[i].append(seq_t)
    except Exception:
        pass

    for i, p in enumerate(pipes):
        extra = _enum_pipe_patterns(p, seg_set, min_wd, min_cut, max_stock, budget=20_000)
        out[i].extend(seq for seq in extra if _pattern_weld_legal(p, seq))
        out[i] = list(dict.fromkeys(out[i]))
        if not out[i]:
            return None
    return out


def _build_cut_pats(stock_qty, seg_sorted, kerf, max_parts, budget=200_000):
    from scripts._arcflow_v3 import _enum_cut_patterns

    return {
        L: _enum_cut_patterns(L, seg_sorted, kerf, budget=budget, max_parts=max_parts)
        for L in stock_qty
    }


def _seed_cut_pats_from_ffd(group, seg_set: set[int]) -> dict[int, list[tuple[int, ...]]]:
    from scripts._arcflow_v3 import _ffd_weld_incumbent

    out: dict[int, list[tuple[int, ...]]] = {L: [] for L in _stock_qty(group)}
    try:
        ffd = _ffd_weld_incumbent(group)
    except Exception:
        return out
    if not ffd or not ffd.get("cut_patterns"):
        return out
    for (L, segs), _qty in ffd["cut_patterns"].items():
        segs_t = tuple(sorted(segs))
        if L in out and all(s in seg_set for s in segs_t):
            out[L].append(segs_t)
    for L in out:
        out[L] = list(dict.fromkeys(out[L]))
    return out


def _ffd_column_pools(group) -> tuple[dict, dict, list[int]] | None:
    """合法 FFD 拼法 + 显式拼法；切列在合法段集上重枚举。"""
    from scripts._arcflow_v3 import _ffd_weld_incumbent
    from scripts.recipe_builder import _explicit_patterns

    ffd = _ffd_weld_incumbent(group)
    if not ffd or not ffd.get("weld_patterns"):
        return None

    pipe_pats: dict[int, list[tuple[int, ...]]] = {
        i: [] for i in range(len(group.pipes))
    }
    for (i, seq), _qty in ffd["weld_patterns"].items():
        seq_t = tuple(seq)
        if _pattern_weld_legal(group.pipes[i], seq_t):
            pipe_pats[i].append(seq_t)

    active: set[int] = set()
    for pats in pipe_pats.values():
        for seq in pats:
            active.update(seq)

    max_stock = max(s.length for s in group.stocks)
    stock_lens = sorted(_stock_qty(group))
    min_cut = max(group.min_cut_length, 1)
    min_wd = group.min_weld_distance
    kerf = group.blade_margin
    for i, p in enumerate(group.pipes):
        for seq in _explicit_patterns(p, max_stock, min_cut, min_wd, stock_lens):
            if all(min_cut <= s <= max_stock for s in seq) and _pattern_weld_legal(p, seq):
                active.update(seq)
                pipe_pats[i].append(seq)
        pipe_pats[i] = list(dict.fromkeys(pipe_pats[i]))
        if not pipe_pats[i]:
            return None

    active_sorted = sorted(active)
    stock_qty = _stock_qty(group)
    # 切列：mp2 + FFD 切法种子 + 单段；严格控规模
    cut_pats = _build_cut_pats(stock_qty, active_sorted, kerf, max_parts=2)
    for (L, segs), _qty in (ffd.get("cut_patterns") or {}).items():
        segs_t = tuple(sorted(segs))
        if L in cut_pats and all(s in active for s in segs_t):
            cut_pats[L].append(segs_t)
    for L in cut_pats:
        for s in active_sorted:
            if s <= L:
                cut_pats[L].append((s,))
        cut_pats[L] = list(dict.fromkeys(cut_pats[L]))
    ncut = sum(len(v) for v in cut_pats.values())
    if ncut > 12_000:
        # 过大则只保留 FFD 切法 + 单段
        cut_pats = {L: [] for L in stock_qty}
        for (L, segs), _qty in (ffd.get("cut_patterns") or {}).items():
            segs_t = tuple(sorted(segs))
            if L in cut_pats and all(s in active for s in segs_t):
                cut_pats[L].append(segs_t)
        for L in cut_pats:
            for s in active_sorted:
                if s <= L:
                    cut_pats[L].append((s,))
            cut_pats[L] = list(dict.fromkeys(cut_pats[L]))
    return pipe_pats, cut_pats, active_sorted


def _extract(group, pipes, pipe_pats, xp, cut_pats, yc, m) -> dict[str, Any]:
    weld_patterns: dict = defaultdict(int)
    total_joints = 0
    for i, pats in pipe_pats.items():
        for k, seq in enumerate(pats):
            v = round(m.getVal(xp[(i, k)]))
            if v > 0:
                weld_patterns[(i, seq)] += v
                total_joints += (len(seq) - 1) * v
    cut_patterns: dict = defaultdict(int)
    used_len = 0
    seg_used: set[int] = set()
    for L, cps in cut_pats.items():
        for k, segs in enumerate(cps):
            v = round(m.getVal(yc[(L, k)]))
            if v > 0:
                cut_patterns[(L, tuple(sorted(segs)))] += v
                used_len += L * v
                seg_used.update(segs)
    weld_types = len({seq for (i, seq) in weld_patterns if len(seq) >= 2})
    util = group.demand_length / used_len if used_len else 0.0
    return {
        "joints": total_joints,
        "cut_types": len(cut_patterns),
        "weld_types": weld_types,
        "seg_types": len(seg_used),
        "used_len": used_len,
        "util": util,
        "cut_patterns": dict(cut_patterns),
        "weld_patterns": dict(weld_patterns),
        "solver": "lex_master",
    }


def _solve_ilp(group, pipes, pipe_pats, cut_pats, active_sorted, stock_qty, uf, tl, verbose, tag):
    from pyscipopt import Model, quicksum, SCIP_PARAMEMPHASIS

    m = Model("lex_master_A")
    m.hideOutput()
    m.setParam("limits/time", max(1.0, tl))
    m.setParam("limits/memory", 4096.0)
    # seed/小模型走默认强调，避免 FEASIBILITY 早停在高焊口可行解
    if "seed" not in tag and "core" not in tag:
        m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)

    xp = {}
    for i, pats in pipe_pats.items():
        for k in range(len(pats)):
            xp[(i, k)] = m.addVar(vtype="I", lb=0, name=f"xp_{i}_{k}")
        m.addCons(quicksum(xp[(i, k)] for k in range(len(pats))) == pipes[i].demand)

    yc = {}
    for L, cps in cut_pats.items():
        for k in range(len(cps)):
            yc[(L, k)] = m.addVar(vtype="I", lb=0, name=f"yc_{L}_{k}")
        if cps:
            m.addCons(quicksum(yc[(L, k)] for k in range(len(cps))) <= stock_qty[L])

    for seg in active_sorted:
        prod = [
            segs.count(seg) * yc[(L, k)]
            for L, cps in cut_pats.items()
            for k, segs in enumerate(cps)
            if seg in segs
        ]
        cons = [
            sum(1 for s in seq if s == seg) * xp[(i, k)]
            for i, pats in pipe_pats.items()
            for k, seq in enumerate(pats)
            if seg in seq
        ]
        if prod or cons:
            m.addCons(quicksum(prod) - quicksum(cons) >= 0)

    usedlen = quicksum(
        L * yc[(L, k)] for L, cps in cut_pats.items() for k in range(len(cps))
    )
    joints = quicksum(
        (len(seq) - 1) * xp[(i, k)]
        for i, pats in pipe_pats.items()
        for k, seq in enumerate(pats)
    )
    if uf > 0 and group.demand_length > 0:
        m.addCons(usedlen <= group.demand_length / uf + 1e-6)

    m.setObjective(joints, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(
                f"  lex[{tag}] 无解 status={m.getStatus()} ({time.time()-t0:.1f}s)",
                flush=True,
            )
        return None
    return _extract(group, pipes, pipe_pats, xp, cut_pats, yc, m)


def _pass_bc(group, sol: dict, tl: float, verbose: bool) -> dict:
    """压拼/切种类：优先在解的现有列上做 setup 压缩；再试 σ_A / 标准段。"""
    from pyscipopt import Model, quicksum, SCIP_PARAMEMPHASIS
    from scripts._arcflow_v3 import _compress_types, _try_std_compress

    if tl < 1.0 or not sol.get("weld_patterns") or not sol.get("cut_patterns"):
        return sol

    stock_qty = _stock_qty(group)
    pipes = group.pipes

    # ── B0: 仅用当前解的拼/切列做种类压缩（最稳）──
    weld_cols = {}  # i -> list[seq]
    for (i, seq), qty in sol["weld_patterns"].items():
        if qty <= 0:
            continue
        seq_t = tuple(seq)
        if not _pattern_weld_legal(pipes[i], seq_t):
            continue
        weld_cols.setdefault(i, [])
        if seq_t not in weld_cols[i]:
            weld_cols[i].append(seq_t)
    if len(weld_cols) == len(pipes) and all(weld_cols[i] for i in range(len(pipes))):
        cut_cols = {}  # L -> list[segs]
        for (L, segs), qty in sol["cut_patterns"].items():
            if qty <= 0:
                continue
            segs_t = tuple(sorted(segs))
            cut_cols.setdefault(L, [])
            if segs_t not in cut_cols[L]:
                cut_cols[L].append(segs_t)
        try:
            m = Model("lex_bc_cols")
            m.hideOutput()
            m.setParam("limits/time", min(tl, 20.0))
            m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
            xp, yc = {}, {}
            for i, pats in weld_cols.items():
                for k, seq in enumerate(pats):
                    xp[(i, k)] = m.addVar(vtype="I", lb=0)
                m.addCons(quicksum(xp[(i, k)] for k in range(len(pats))) == pipes[i].demand)
            for L, cps in cut_cols.items():
                for k in range(len(cps)):
                    yc[(L, k)] = m.addVar(vtype="I", lb=0)
                m.addCons(quicksum(yc[(L, k)] for k in range(len(cps))) <= stock_qty.get(L, 0))
            segs = sorted({s for pats in weld_cols.values() for seq in pats for s in seq})
            for seg in segs:
                prod = [
                    cps[k].count(seg) * yc[(L, k)]
                    for L, cps in cut_cols.items() for k in range(len(cps)) if seg in cps[k]
                ]
                cons = [
                    seq.count(seg) * xp[(i, k)]
                    for i, pats in weld_cols.items() for k, seq in enumerate(pats) if seg in seq
                ]
                m.addCons(quicksum(prod) - quicksum(cons) >= 0)
            joints = quicksum(
                (len(seq) - 1) * xp[(i, k)]
                for i, pats in weld_cols.items() for k, seq in enumerate(pats)
            )
            m.addCons(joints <= sol["joints"] + 1e-6)
            usedlen = quicksum(L * yc[(L, k)] for L, cps in cut_cols.items() for k in range(len(cps)))
            m.addCons(usedlen <= max(sol["used_len"], int(group.demand_length / 0.95) + 1) + 1e-6)

            # setup vars
            wtype = {}
            for i, pats in weld_cols.items():
                for k, seq in enumerate(pats):
                    if len(seq) >= 2:
                        wtype.setdefault(seq, len(wtype))
            zW = {tid: m.addVar(vtype="B") for tid in set(wtype.values())} if wtype else {}
            for i, pats in weld_cols.items():
                for k, seq in enumerate(pats):
                    if len(seq) >= 2:
                        m.addCons(xp[(i, k)] <= pipes[i].demand * zW[wtype[seq]])
            ctype = {}
            for L, cps in cut_cols.items():
                for k, segs_t in enumerate(cps):
                    ctype.setdefault((L, segs_t), len(ctype))
            zC = {tid: m.addVar(vtype="B") for tid in set(ctype.values())}
            for L, cps in cut_cols.items():
                for k, segs_t in enumerate(cps):
                    m.addCons(yc[(L, k)] <= stock_qty.get(L, 0) * zC[ctype[(L, segs_t)]])

            if zW:
                m.setObjective(quicksum(zW.values()), "minimize")
                m.optimize()
                if m.getNSols() > 0:
                    wt = round(m.getObjVal())
                    m.freeTransform()
                    m.setParam("limits/time", min(tl, 20.0))
                    m.addCons(quicksum(zW.values()) <= wt + 1e-6)
                    m.setObjective(quicksum(zC.values()), "minimize")
                    m.optimize()
            else:
                m.setObjective(quicksum(zC.values()), "minimize")
                m.optimize()
            if m.getNSols() > 0:
                out = _extract(group, pipes, weld_cols, xp, cut_cols, yc, m)
                if (out["weld_types"], out["cut_types"], out["seg_types"]) <= (
                    sol["weld_types"], sol["cut_types"], sol.get("seg_types", 10**9)
                ) and out["joints"] <= sol["joints"]:
                    out["solver"] = "lex_master"
                    out["solver_path"] = list(sol.get("solver_path") or []) + ["pass_bc_cols"]
                    out["diagnosis"] = list(sol.get("diagnosis") or []) + ["pass_bc_cols_ok"]
                    if verbose:
                        print(
                            f"  PassB/C cols: weld {sol['weld_types']}->{out['weld_types']} "
                            f"cut {sol['cut_types']}->{out['cut_types']}",
                            flush=True,
                        )
                    return out
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  PassB/C cols 异常: {exc}", flush=True)

    # ── B1: σ_A 全列压缩 ──
    sigma_a = sorted({
        *{s for (_L, segs) in sol.get("cut_patterns", {}) for s in segs},
        *{s for (_i, seq) in sol.get("weld_patterns", {}) for s in seq},
    })
    if 0 < len(sigma_a) <= 36:
        try:
            comp = _compress_types(
                group, pipes, sigma_a, stock_qty,
                j_cap=sol["joints"],
                u_cap=max(sol["used_len"], int(group.demand_length / 0.95) + 1),
                tl=min(tl, 30.0), verbose=verbose, col_cap=25_000,
                cut_tl=min(max(tl, 15.0), 45.0),
                cut_max_parts=3,
            )
            if (
                comp is not None
                and comp["joints"] <= sol["joints"]
                and (comp["weld_types"], comp["cut_types"], comp["seg_types"])
                <= (sol["weld_types"], sol["cut_types"], sol.get("seg_types", 10**9))
            ):
                out = dict(comp)
                out["solver"] = "lex_master"
                out["solver_path"] = list(sol.get("solver_path") or []) + ["pass_bc_sigma_a"]
                out["diagnosis"] = list(sol.get("diagnosis") or []) + ["pass_bc_sigma_a_ok"]
                return out
        except Exception:
            pass

    try:
        comp = _try_std_compress(
            group, pipes, stock_qty, sol,
            tl=min(tl, 25.0), verbose=verbose,
            cut_tl=min(max(tl, 15.0), 45.0),
        )
    except Exception:
        return sol
    if (
        comp is not None
        and _solution_welds_legal(group, comp)
        and comp["joints"] <= sol["joints"] * 1.05 + 1
        and (comp["weld_types"], comp["cut_types"]) < (sol["weld_types"], sol["cut_types"])
    ):
        out = dict(comp)
        out["solver"] = "lex_master"
        out["solver_path"] = list(sol.get("solver_path") or []) + ["pass_bc_std"]
        out["diagnosis"] = list(sol.get("diagnosis") or []) + ["pass_bc_std_ok"]
        return out
    return sol



def solve(
    group,
    recipe: Recipe,
    *,
    util_floor: float | None = None,
    tl: float = 30.0,
    col_cap: int = COL_CAP_DEFAULT,
    verbose: bool = False,
) -> dict[str, Any] | None:
    t_all = time.monotonic()
    diagnosis: list[str] = []
    if recipe.meta.get("hard_unreachable"):
        return None

    uf = _util_floor(group, util_floor)
    pipes = group.pipes
    kerf = group.blade_margin
    stock_qty = _stock_qty(group)
    sol = None

    # ── A1: Recipe Σ 列 ──
    attempts = [
        ("sigma_weld", list(recipe.sigma_weld)),
        ("sigma_cut", list(recipe.sigma_cut)),
    ]
    seen: set[tuple[int, ...]] = set()
    uniq = []
    for name, segs in attempts:
        key = tuple(sorted(set(segs)))
        if key and key not in seen:
            seen.add(key)
            uniq.append((name, sorted(set(segs))))

    last_reason = ""
    for seg_name, seg_sorted in uniq:
        seg_set = set(seg_sorted)
        pipe_pats = _build_pipe_pats(group, recipe, seg_set)
        if pipe_pats is None:
            last_reason = f"{seg_name}:no_pipe_pats"
            continue
        active = sorted({s for pats in pipe_pats.values() for seq in pats for s in seq})
        if not active:
            continue
        total_pats = sum(len(v) for v in pipe_pats.values())
        seeded = _seed_cut_pats_from_ffd(group, set(active))

        # ── A0: 仅 seed/经典拼法 + 小段集高段数切法（短料题先找低焊口）──
        seed_pp: dict[int, list[tuple[int, ...]]] = {i: [] for i in range(len(pipes))}
        pack_L = max(stock_qty)
        for item in recipe.seed_weld or []:
            i, seq = item[0], tuple(item[1])
            if (
                i in seed_pp
                and all(s in seg_set for s in seq)
                and _pattern_weld_legal(pipes[i], seq)
            ):
                seed_pp[i].append(seq)
        for i, pats in pipe_pats.items():
            for seq in pats:
                # 2 段 pack 尾：低焊口主力（如 7985+11000）
                if len(seq) == 2 and seq[-1] == pack_L:
                    seed_pp[i].append(seq)
        for i in seed_pp:
            # 控制 4 段数量，避免 FEASIBILITY 强调下陷入高焊口可行解
            twos = [s for s in seed_pp[i] if len(s) == 2]
            threes = [s for s in seed_pp[i] if len(s) == 3][:24]
            fours = [s for s in seed_pp[i] if len(s) >= 4][:12]
            ones = [s for s in seed_pp[i] if len(s) == 1]
            seed_pp[i] = list(dict.fromkeys(ones + twos + threes + fours))
            if len(seed_pp[i]) < 4:
                extra3 = [
                    s for s in pipe_pats[i] if len(s) == 3 and s[-1] == pack_L
                ][:12]
                seed_pp[i] = list(dict.fromkeys(seed_pp[i] + extra3))
        if seed_pp and all(seed_pp[i] for i in range(len(pipes))):
            seed_active = sorted(
                {s for pats in seed_pp.values() for seq in pats for s in seq}
            )
            n_seed = sum(len(v) for v in seed_pp.values())
            if 4 <= len(seed_active) <= 64 and n_seed + 500 < col_cap:
                from scripts._arcflow_v3 import _enum_cut_patterns

                for mp in (3, 4, 5):
                    rem = max(1.0, tl - (time.monotonic() - t_all))
                    if rem < 1.0:
                        break
                    cp = {
                        L: _enum_cut_patterns(
                            L, seed_active, kerf, budget=100_000, max_parts=mp
                        )
                        for L in stock_qty
                    }
                    for L, cps in seeded.items():
                        cp.setdefault(L, [])
                        cp[L] = list(dict.fromkeys(list(cps) + list(cp[L])))
                    ncut = sum(len(v) for v in cp.values())
                    if ncut == 0 or n_seed + ncut > col_cap:
                        continue
                    tag = f"{seg_name}/seed_mp{mp}"
                    if verbose:
                        print(
                            f"  lex[{tag}]: active={len(seed_active)} weld={n_seed} "
                            f"cut={ncut}",
                            flush=True,
                        )
                    cand = _solve_ilp(
                        group,
                        pipes,
                        seed_pp,
                        cp,
                        seed_active,
                        stock_qty,
                        uf,
                        rem,
                        verbose,
                        tag,
                    )
                    if cand is not None:
                        cand["solver_path"] = ["lex_master", "pass_a", tag]
                        cand["diagnosis"] = diagnosis + [f"pass_a_ok:{tag}"]
                        sol = cand
                        break
                if sol is not None:
                    break

        cut_attempts = []
        # 核心段：seed 经典拼法字母表；过大则优先保留 4 段种子段
        core_segs = sorted(
            {
                s
                for item in (recipe.seed_weld or [])
                for s in item[1]
                if s in set(active)
            }
        )
        if len(core_segs) > 28:
            prefer = {
                s
                for item in (recipe.seed_weld or [])
                if len(item[1]) >= 3
                for s in item[1]
                if s in set(active)
            }
            # 补上 2 段种子中的大段（如 7985）
            prefer |= {
                s
                for item in (recipe.seed_weld or [])
                if len(item[1]) == 2
                for s in item[1]
                if s in set(active) and s >= max(stock_qty) // 3
            }
            if 8 <= len(prefer) <= 28:
                core_segs = sorted(prefer)
            else:
                pack_L = max(stock_qty)
                core_segs = sorted(
                    core_segs,
                    key=lambda s: (
                        0 if s in prefer else 1,
                        0 if s >= pack_L // 3 or s <= pack_L // 5 else 1,
                        s,
                    ),
                )[:28]
                core_segs = sorted(core_segs)
        elif len(core_segs) < 4:
            core_segs = sorted(
                {
                    s
                    for pats in pipe_pats.values()
                    for seq in pats
                    if len(seq) >= 2
                    for s in seq
                }
            )
            if len(core_segs) > 28:
                pack_L = max(stock_qty)
                core_segs = sorted(
                    core_segs,
                    key=lambda s: (0 if s >= pack_L // 3 or s <= pack_L // 5 else 1, s),
                )[:28]
                core_segs = sorted(core_segs)

        for mp, tag in ((2, "mp2"), (3, "mp3"), (4, "mp4"), (5, "mp5"), (6, "mp6")):
            if mp <= 3:
                cp = _build_cut_pats(stock_qty, active, kerf, mp)
            else:
                # 高段数：禁止在全 active 上通用枚举（必爆 col_cap）
                pieces_4 = sorted(
                    {
                        s
                        for pats in pipe_pats.values()
                        for seq in pats
                        if len(seq) >= 4
                        for s in seq
                        if s <= max(stock_qty) // 2
                    }
                )
                cp = {L: [] for L in stock_qty}
                for L in stock_qty:
                    for s in active:
                        if s <= L:
                            cp[L].append((s,))
                    for s in pieces_4:
                        if s <= 0:
                            continue
                        for k in range(2, mp + 1):
                            need = k * s + kerf * (k - 1)
                            if need <= L:
                                cp[L].append(tuple([s] * k))
                        for r in (L - s - kerf, L - s):
                            if r in set(active) and r >= 1:
                                cp[L].append(tuple(sorted((s, r))))
                    cp[L] = list(dict.fromkeys(cp[L]))

            # 小核心段集：补 mp 全枚举（解锁 1040+7985+1965 / 1448×5 等混切）
            if core_segs and 4 <= len(core_segs) <= 32 and mp >= 3:
                from scripts._arcflow_v3 import _enum_cut_patterns

                for L in stock_qty:
                    cp.setdefault(L, [])
                    extra = _enum_cut_patterns(
                        L, core_segs, kerf, budget=80_000, max_parts=mp
                    )
                    cp[L] = list(dict.fromkeys(list(cp[L]) + list(extra)))

            for L, cps in seeded.items():
                cp.setdefault(L, [])
                cp[L] = list(dict.fromkeys(list(cps) + list(cp[L])))
            ncut = sum(len(v) for v in cp.values())
            if ncut == 0 or total_pats + ncut > col_cap:
                continue
            cut_attempts.append((f"{seg_name}/{tag}", cp))

        # 额外：仅核心段的高段数切法（列更少，优先尝试）
        if core_segs and 4 <= len(core_segs) <= 32:
            from scripts._arcflow_v3 import _enum_cut_patterns

            for mp in (5, 4, 3):
                cp = {
                    L: _enum_cut_patterns(L, core_segs, kerf, budget=80_000, max_parts=mp)
                    for L in stock_qty
                }
                for L, cps in seeded.items():
                    cp.setdefault(L, [])
                    cp[L] = list(dict.fromkeys(list(cps) + list(cp[L])))
                # 单段兜底
                for L in cp:
                    for s in core_segs:
                        if s <= L:
                            cp[L].append((s,))
                    cp[L] = list(dict.fromkeys(cp[L]))
                ncut = sum(len(v) for v in cp.values())
                if ncut == 0 or total_pats + ncut > col_cap:
                    continue
                cut_attempts.insert(0, (f"{seg_name}/core_mp{mp}", cp))

        for tag, cut_pats in cut_attempts:
            rem = max(1.0, tl - (time.monotonic() - t_all))
            if rem < 0.8:
                break
            if verbose:
                print(
                    f"  lex[{tag}]: active={len(active)} weld={total_pats} "
                    f"cut={sum(len(v) for v in cut_pats.values())}",
                    flush=True,
                )
            cand = _solve_ilp(
                group, pipes, pipe_pats, cut_pats, active, stock_qty, uf, rem, verbose, tag
            )
            if cand is None:
                last_reason = f"{tag}:fail"
                diagnosis.append(last_reason)
                continue
            cand["solver_path"] = ["lex_master", "pass_a", tag]
            cand["diagnosis"] = diagnosis + [f"pass_a_ok:{tag}"]
            sol = cand
            break
        if sol is not None:
            break

    # ── A2: FFD 列池 ILP（专治 Cr）；列过大或剩余时间不足则跳过 ──
    if sol is None:
        rem = max(1.0, tl - (time.monotonic() - t_all))
        pools = _ffd_column_pools(group) if rem >= 3.0 else None
        if pools is not None:
            pipe_pats, cut_pats, active = pools
            ncut = sum(len(v) for v in cut_pats.values())
            nweld = sum(len(v) for v in pipe_pats.values())
            if nweld + ncut <= col_cap and rem >= 3.0:
                if verbose:
                    print(
                        f"  lex[ffd_cols]: active={len(active)} weld={nweld} cut={ncut}",
                        flush=True,
                    )
                cand = _solve_ilp(
                    group, pipes, pipe_pats, cut_pats, active, stock_qty, uf,
                    min(rem, 8.0), verbose, "ffd_cols",
                )
                if cand is not None:
                    cand["solver_path"] = ["lex_master", "pass_a", "ffd_cols"]
                    cand["diagnosis"] = diagnosis + ["pass_a_ok:ffd_cols"]
                    sol = cand
                else:
                    last_reason = "ffd_cols:fail"
                    diagnosis.append(last_reason)
            else:
                diagnosis.append(f"ffd_cols:skip_cols={nweld}+{ncut}")

    # ── A3: 合法 FFD 快交付（种类交给 Pass B/C）──
    if sol is None:
        from scripts._arcflow_v3 import _ffd_weld_incumbent

        ffd = _ffd_weld_incumbent(group)
        if ffd is not None and _solution_welds_legal(group, ffd):
            sol = dict(ffd)
            sol["solver_path"] = ["lex_master", "pass_a_recover_ffd_legal"]
            sol["diagnosis"] = diagnosis + [f"ilp_fail:{last_reason}", "recover:ffd_legal"]
        if sol is None:
            return None

    sol["solver"] = "lex_master"
    sol["elapsed_s"] = time.monotonic() - t_all
    if verbose:
        print(
            f"  lex PassA done: joints={sol.get('joints')} util={sol.get('util')} "
            f"path={sol.get('solver_path')} ({sol['elapsed_s']:.2f}s)",
            flush=True,
        )

    # ── Pass B/C ──
    rem = max(0.0, tl - (time.monotonic() - t_all))
    if rem >= 2.0:
        sol = _pass_bc(group, sol, tl=rem, verbose=verbose)
        sol["elapsed_s"] = time.monotonic() - t_all

    return sol
