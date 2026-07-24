"""先拼后切探针：S1 按根混选合法拼法 → S2 经典 CSP（SCIP）。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_weld_then_cut.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from itertools import combinations_with_replacement
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ACCEPT = ROOT / "data" / "acceptance" / "113146"
CASES = (
    ("t91", ACCEPT / "t91.json"),
    ("cr", ACCEPT / "cr.json"),
)

LEGACY_CR_PATTERNS: dict[int, list[tuple[int, ...]]] = {
    17929: [(4177, 3352, 10400), (4177, 4952, 8800), (7529, 10400), (8800, 9129)],
    17928: [(10115, 7813), (7528, 10400), (9128, 8800)],
    16659: [(1875, 12000, 2784), (4659, 12000), (6259, 10400), (7859, 8800)],
    4598: [(4598,)],
    198: [(198,)],
}


def _load_group(path: Path):
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem

    payload = clean_mom_problem(json.loads(path.read_text(encoding="utf-8")))
    prob = parse_problem(payload)
    if len(prob.groups) != 1:
        raise ValueError(f"{path.name}: groups={len(prob.groups)}")
    return merge_equivalent_pipes(prob.groups[0]), payload


def _stock_qty(group) -> dict[int, int]:
    out: dict[int, int] = defaultdict(int)
    for st in group.stocks:
        out[st.length] += st.quantity
    return dict(out)


def _pattern_legal(pipe, seq: tuple[int, ...], min_cut: int, min_wd: int,
                   max_stock: int) -> bool:
    if sum(seq) != pipe.length:
        return False
    if len(seq) - 1 > pipe.max_joints:
        return False
    if any(s < min_cut or s > max_stock for s in seq):
        return False
    pos = 0
    for i, seg in enumerate(seq):
        pos += seg
        if i < len(seq) - 1:
            if not pipe.weld_allowed(pos):
                return False
            if seg < min_cut:
                return False
            if i >= 1 and seq[i] < min_wd:
                return False
    return True


def _preferred_segs(group) -> list[int]:
    kerf = group.blade_margin
    min_cut = max(group.min_cut_length, 1)
    max_stock = max(s.length for s in group.stocks)
    segs: set[int] = set()
    for L in _stock_qty(group):
        segs.add(L)
        for pieces in (2, 3):
            piece = (L - kerf * (pieces - 1)) // pieces
            if min_cut <= piece <= max_stock:
                segs.add(piece)
    for p in group.pipes:
        if min_cut <= p.length <= max_stock:
            segs.add(p.length)
    return sorted(segs)


def _dfs_patterns(pipe, preferred: list[int], min_cut: int, min_wd: int,
                  max_stock: int, max_pats: int = 60) -> list[tuple[int, ...]]:
    prefs = sorted({s for s in preferred if min_cut <= s <= max_stock}, reverse=True)
    out: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()
    L = pipe.length

    def rec(filled: int, parts: list[int]) -> None:
        if len(out) >= max_pats:
            return
        rem = L - filled
        joints = len(parts)  # welds already placed = len(parts) when rem completes?
        # parts are completed segments; weld count after finishing = len(parts) if rem>0 else len(parts)-1
        if rem == 0:
            tup = tuple(parts)
            if tup not in seen and _pattern_legal(pipe, tup, min_cut, min_wd, max_stock):
                seen.add(tup)
                out.append(tup)
            return
        # finish with rem as last segment
        if min_cut <= rem <= max_stock:
            cand = tuple(parts + [rem])
            if cand not in seen and _pattern_legal(pipe, cand, min_cut, min_wd, max_stock):
                seen.add(cand)
                out.append(cand)
        if len(parts) >= pipe.max_joints:
            return  # cannot add another weld
        for seg in prefs:
            if seg >= rem or seg < min_cut:
                continue
            if parts and seg < min_wd:
                continue
            nxt = filled + seg
            if not pipe.weld_allowed(nxt):
                continue
            parts.append(seg)
            rec(nxt, parts)
            parts.pop()

    rec(0, [])
    out.sort(key=lambda s: (len(s) - 1, s))
    return out


def _stock_embedded_patterns(pipe, stock_lens: list[int], preferred: list[int],
                             min_cut: int, min_wd: int, max_stock: int) -> list[tuple[int, ...]]:
    """母料整段嵌入拼法：把某定尺 S 作为一段，其余用母料/偏好段补齐。"""
    L = pipe.length
    out: list[tuple[int, ...]] = []
    extras = sorted(set(stock_lens) | set(preferred))
    for S in stock_lens:
        if S > L or S < min_cut:
            continue
        # S 在末端
        left = L - S
        if min_cut <= left <= max_stock and pipe.weld_allowed(left):
            out.append((left, S))
            for a in extras:
                b = left - a
                if not (min_cut <= a <= max_stock and min_cut <= b <= max_stock):
                    continue
                if b < min_wd:
                    continue
                if pipe.weld_allowed(a) and pipe.weld_allowed(left):
                    out.append((a, b, S))
            # 禁焊网格拆 left（补偏好段之外的合法三点焊）
            step = max(min_wd, 50)
            for a in range(min_cut, left - min_cut + 1, step):
                b = left - a
                if b < min_wd or b > max_stock:
                    continue
                if pipe.weld_allowed(a) and pipe.weld_allowed(left):
                    out.append((a, b, S))
                    if len(out) > 200:
                        break
        # S 在首端
        right = L - S
        if min_cut <= right <= max_stock and pipe.weld_allowed(S):
            out.append((S, right))
            for b in extras:
                c = right - b
                if not (min_cut <= b <= max_stock and min_cut <= c <= max_stock):
                    continue
                if b < min_wd or c < min_cut:
                    continue
                if pipe.weld_allowed(S) and pipe.weld_allowed(S + b):
                    out.append((S, b, c))
        # S 在中段
        for a in extras:
            if a + S >= L:
                continue
            c = L - a - S
            if not (min_cut <= a <= max_stock and min_cut <= c <= max_stock):
                continue
            if S < min_wd or c < min_cut:
                continue
            if pipe.weld_allowed(a) and pipe.weld_allowed(a + S):
                out.append((a, S, c))
    return out


def build_pools(group, *, use_legacy: bool) -> list[list[tuple[int, ...]]]:
    from scripts.recipe_builder import _explicit_patterns

    min_cut = max(group.min_cut_length, 1)
    min_wd = max(group.min_weld_distance, 1)
    max_stock = max(s.length for s in group.stocks)
    stock_qty = _stock_qty(group)
    stock_lens = sorted(stock_qty)
    preferred = _preferred_segs(group)
    # 残段：管长对母料取模类，扩大混拼空间
    for pipe in group.pipes:
        for S in stock_lens:
            if pipe.length > S:
                r = pipe.length % S
                if min_cut <= r <= max_stock:
                    preferred.append(r)
                r2 = pipe.length - S
                if min_cut <= r2 <= max_stock:
                    preferred.append(r2)
    preferred = sorted(set(preferred))

    pools: list[list[tuple[int, ...]]] = []
    for pipe in group.pipes:
        pats: list[tuple[int, ...]] = []
        pats.extend(_explicit_patterns(pipe, max_stock, min_cut, min_wd, stock_lens))
        pats.extend(_stock_embedded_patterns(
            pipe, stock_lens, preferred, min_cut, min_wd, max_stock
        ))
        pats.extend(_dfs_patterns(pipe, preferred, min_cut, min_wd, max_stock))
        if use_legacy:
            for seq in LEGACY_CR_PATTERNS.get(pipe.length, []):
                if _pattern_legal(pipe, seq, min_cut, min_wd, max_stock):
                    pats.append(seq)
        uniq = [
            p for p in dict.fromkeys(pats)
            if _pattern_legal(pipe, p, min_cut, min_wd, max_stock)
        ]
        uniq.sort(key=lambda s: (len(s) - 1, s))
        if not uniq:
            raise RuntimeError(f"no pattern for L={pipe.length}")
        pools.append(uniq)
    return pools


def _seg_score(seg: int, stock_lens: list[int], kerf: int) -> float:
    if seg in stock_lens:
        return 10.0
    best = 0.0
    for S in stock_lens:
        if seg > S:
            continue
        rem = S - seg
        best = max(best, 8.0 if rem == 0 else (6.0 if rem <= kerf else 2.0 + seg / S))
        for pieces in (2, 3):
            if (S - kerf * (pieces - 1)) // pieces == seg:
                best = max(best, 7.5)
    return best


def pattern_rank(seq: tuple[int, ...], stock_lens: list[int], kerf: int) -> tuple:
    return (len(seq) - 1, -sum(_seg_score(s, stock_lens, kerf) for s in seq), len(set(seq)))


def cut_csp_ilp(
    demand: Counter[int],
    stock_qty: dict[int, int],
    kerf: int,
    *,
    tl: float = 8.0,
    max_patterns_per_stock: int = 4000,
) -> dict[str, Any] | None:
    """S2：给定段需求，有限库存一维 CSP。"""
    from pyscipopt import Model, quicksum

    segs = sorted(s for s, q in demand.items() if q > 0)
    if not segs:
        return {"used_len": 0, "cut_types": 0, "cut_patterns": {}, "bars_left": dict(stock_qty)}

    patterns: list[tuple[int, tuple[int, ...], Counter]] = []

    def gen_for_stock(L: int) -> None:
        local: list[tuple[int, ...]] = []

        def dfs(start: int, rem: int, parts: list[int]) -> None:
            if parts:
                local.append(tuple(parts))
            if len(local) >= max_patterns_per_stock:
                return
            for i in range(start, len(segs)):
                s = segs[i]
                cost = s if not parts else s + kerf
                if cost > rem:
                    continue
                # 粗剪：不超过需求
                if parts.count(s) + 1 > demand[s]:
                    continue
                parts.append(s)
                dfs(i, rem - cost, parts)
                parts.pop()
                if len(local) >= max_patterns_per_stock:
                    return

        dfs(0, L, [])
        for seq in dict.fromkeys(local):
            c = Counter(seq)
            if any(c[s] > demand[s] for s in c):
                continue
            patterns.append((L, tuple(sorted(seq)), c))

    for L, q in stock_qty.items():
        if q > 0:
            gen_for_stock(L)

    if not patterns:
        return None

    m = Model("weld_then_cut_csp")
    m.hideOutput()
    x = {
        i: m.addVar(vtype="I", lb=0, ub=stock_qty[patterns[i][0]], name=f"p{i}")
        for i in range(len(patterns))
    }
    for L, q in stock_qty.items():
        m.addCons(
            quicksum(x[i] for i, (LL, _, __) in enumerate(patterns) if LL == L) <= q
        )
    for s, need in demand.items():
        if need <= 0:
            continue
        m.addCons(
            quicksum(
                x[i] * patterns[i][2][s]
                for i in range(len(patterns))
                if patterns[i][2][s]
            )
            >= need
        )
    m.setObjective(
        quicksum(x[i] * patterns[i][0] for i in range(len(patterns))), "minimize"
    )
    m.setParam("limits/time", float(tl))
    m.optimize()
    if m.getNSols() <= 0:
        return None

    cut_patterns: dict[tuple[int, tuple[int, ...]], int] = {}
    used_len = 0
    bars_used: dict[int, int] = defaultdict(int)
    for i, var in x.items():
        val = int(round(m.getVal(var)))
        if val <= 0:
            continue
        L, seq, _ = patterns[i]
        cut_patterns[(L, seq)] = cut_patterns.get((L, seq), 0) + val
        used_len += val * L
        bars_used[L] += val
    bars_left = {L: stock_qty[L] - bars_used.get(L, 0) for L in stock_qty}
    return {
        "used_len": used_len,
        "cut_types": len(cut_patterns),
        "cut_patterns": cut_patterns,
        "bars_left": bars_left,
        "csp_status": m.getStatus(),
    }


def _mix_candidates(
    pool: list[tuple[int, ...]],
    demand: int,
    stock_lens: list[int],
    kerf: int,
    *,
    stock_qty: dict[int, int] | None = None,
    max_mixes: int = 80,
) -> list[list[tuple[tuple[int, ...], int]]]:
    """生成 (pattern, count) 混拼方案，counts 之和 = demand。"""
    ranked = sorted(pool, key=lambda s: pattern_rank(s, stock_lens, kerf))
    top = ranked[: min(8, len(ranked))]
    mixes: list[list[tuple[tuple[int, ...], int]]] = []
    sq = stock_qty or {}

    # 单拼法
    for seq in top:
        mixes.append([(seq, demand)])

    # 紧库存：含整根母料段的拼法，按库存件数封顶混配
    scarce = [S for S in stock_lens if sq.get(S, 0) and sq[S] < demand]
    for S in scarce:
        users = [seq for seq in ranked if S in seq][:6]
        others = [seq for seq in ranked if S not in seq][:6]
        for u in users:
            for o in others:
                for ca in sorted({1, sq[S], min(sq[S], demand), 5, 4, 18, 19}):
                    consume = u.count(S)
                    if consume <= 0:
                        continue
                    max_ca = min(demand, sq[S] // consume)
                    if ca <= 0 or ca > max_ca:
                        continue
                    cb = demand - ca
                    if cb <= 0:
                        mixes.append([(u, demand)])
                    else:
                        mixes.append([(u, ca), (o, cb)])

    # 两拼法分配
    if demand >= 1 and len(top) >= 2:
        for a, b in combinations_with_replacement(range(len(top)), 2):
            if a == b:
                continue
            sa, sb = top[a], top[b]
            step = max(1, demand // 8)
            for ca in list(range(0, demand + 1, step)) + [demand // 2, 5, 18, 19, 4]:
                if ca < 0 or ca > demand:
                    continue
                cb = demand - ca
                if ca == 0:
                    mixes.append([(sb, demand)])
                elif cb == 0:
                    mixes.append([(sa, demand)])
                else:
                    mixes.append([(sa, ca), (sb, cb)])
                if len(mixes) >= max_mixes * 2:
                    break

    # 三拼法：少量
    if len(top) >= 3 and demand >= 3:
        a, b, c = top[0], top[1], top[2]
        for ca in (5, demand // 3, 0):
            rem = demand - ca
            for cb in (0, rem // 2, rem):
                cc = demand - ca - cb
                if cc < 0:
                    continue
                parts = []
                if ca:
                    parts.append((a, ca))
                if cb:
                    parts.append((b, cb))
                if cc:
                    parts.append((c, cc))
                if parts:
                    mixes.append(parts)

    return _dedupe_mixes(mixes)[:max_mixes]


def _dedupe_mixes(
    mixes: list[list[tuple[tuple[int, ...], int]]],
) -> list[list[tuple[tuple[int, ...], int]]]:
    seen: set[tuple] = set()
    out = []
    for mix in mixes:
        key = tuple(sorted((seq, cnt) for seq, cnt in mix if cnt > 0))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append([(s, c) for s, c in key])
    return out


def solve_weld_then_cut(
    group,
    *,
    use_legacy: bool = False,
    csp_tl: float = 5.0,
    max_mix_combos: int = 120,
    verbose: bool = False,
) -> dict[str, Any] | None:
    t0 = time.monotonic()
    pools = build_pools(group, use_legacy=use_legacy)
    stock_qty = _stock_qty(group)
    stock_lens = sorted(stock_qty)
    kerf = group.blade_margin

    per_pipe_mixes = [
        _mix_candidates(
            pools[i],
            group.pipes[i].demand,
            stock_lens,
            kerf,
            stock_qty=stock_qty,
            max_mixes=50,
        )
        for i in range(len(pools))
    ]

    # 组合搜索：先各取最优拼，再坐标下降换某一管型的 mix
    idxs = [0] * len(pools)
    tried = 0
    hits = 0
    best: dict[str, Any] | None = None

    def eval_choice(choice_idxs: list[int]) -> dict[str, Any] | None:
        nonlocal tried, hits, best
        tried += 1
        demand: Counter[int] = Counter()
        joints = 0
        weld_patterns: dict[tuple[int, tuple[int, ...]], int] = {}
        choice_desc: dict[str, list] = {}
        for pi, mix_i in enumerate(choice_idxs):
            mix = per_pipe_mixes[pi][mix_i % len(per_pipe_mixes[pi])]
            L = group.pipes[pi].length
            choice_desc[str(L)] = [[list(seq), cnt] for seq, cnt in mix]
            for seq, cnt in mix:
                weld_patterns[(pi, seq)] = weld_patterns.get((pi, seq), 0) + cnt
                joints += (len(seq) - 1) * cnt
                for seg in seq:
                    demand[seg] += cnt
        cut = cut_csp_ilp(demand, stock_qty, kerf, tl=csp_tl)
        if cut is None:
            return None
        hits += 1
        util = group.demand_length / cut["used_len"] if cut["used_len"] else 0.0
        sol = {
            "joints": joints,
            "weld_types": len(weld_patterns),
            "cut_types": cut["cut_types"],
            "seg_types": len(demand),
            "used_len": cut["used_len"],
            "util": util,
            "weld_patterns": {f"{k[0]}|{k[1]}": v for k, v in weld_patterns.items()},
            "pattern_choice": choice_desc,
            "bars_left": cut["bars_left"],
            "csp_status": cut.get("csp_status"),
            "solver": "weld_then_cut" + ("_legacy" if use_legacy else ""),
        }
        key = (joints, sol["weld_types"], sol["cut_types"], -util, cut["used_len"])
        if best is None or key < (
            best["joints"], best["weld_types"], best["cut_types"],
            -best["util"], best["used_len"],
        ):
            best = sol
        return sol

    # 初始
    eval_choice(idxs)

    # 坐标下降 / 轮换
    budget = max_mix_combos
    for r in range(budget):
        if tried >= budget:
            break
        pi = r % len(pools)
        for alt in range(len(per_pipe_mixes[pi])):
            trial = list(idxs)
            trial[pi] = alt
            sol = eval_choice(trial)
            if sol is not None and (
                best is None
                or (sol["joints"], -sol["util"])
                <= (best["joints"], -best["util"])
            ):
                idxs = trial
            if tried >= budget:
                break

    # legacy 捷径：若 use_legacy，额外试一组已知好混法
    if use_legacy and len(group.pipes) >= 3:
        # 人工种子：与老软件结构接近
        seed_mixes = {
            17929: [((4177, 3352, 10400), 5), ((4177, 4952, 8800), 18)],
            17928: [((10115, 7813), 23)],
            16659: [((1875, 12000, 2784), 23)],
            4598: [((4598,), 46)],
            198: [((198,), 115)],
        }
        demand = Counter()
        joints = 0
        weld_patterns = {}
        choice_desc = {}
        ok_seed = True
        for pi, pipe in enumerate(group.pipes):
            mix = seed_mixes.get(pipe.length)
            if not mix:
                ok_seed = False
                break
            choice_desc[str(pipe.length)] = [[list(s), c] for s, c in mix]
            for seq, cnt in mix:
                if sum(seq) != pipe.length or cnt <= 0:
                    ok_seed = False
                    break
                weld_patterns[(pi, seq)] = cnt
                joints += (len(seq) - 1) * cnt
                for seg in seq:
                    demand[seg] += cnt
            if not ok_seed:
                break
        if ok_seed:
            tried += 1
            cut = cut_csp_ilp(demand, stock_qty, kerf, tl=csp_tl)
            if cut is not None:
                hits += 1
                util = group.demand_length / cut["used_len"]
                sol = {
                    "joints": joints,
                    "weld_types": len(weld_patterns),
                    "cut_types": cut["cut_types"],
                    "seg_types": len(demand),
                    "used_len": cut["used_len"],
                    "util": util,
                    "weld_patterns": {
                        f"{k[0]}|{k[1]}": v for k, v in weld_patterns.items()
                    },
                    "pattern_choice": choice_desc,
                    "bars_left": cut["bars_left"],
                    "csp_status": cut.get("csp_status"),
                    "solver": "weld_then_cut_legacy_seed",
                }
                key = (joints, sol["weld_types"], sol["cut_types"], -util, cut["used_len"])
                if best is None or key < (
                    best["joints"], best["weld_types"], best["cut_types"],
                    -best["util"], best["used_len"],
                ):
                    best = sol

    if best is not None:
        best["elapsed_s"] = time.monotonic() - t0
        best["attempts"] = tried
        best["feasible_hits"] = hits
        best["pool_sizes"] = [len(p) for p in pools]
        if verbose:
            print(
                f"  [{best['solver']}] joints={best['joints']} util={best['util']:.6f} "
                f"cut_types={best['cut_types']} weld_types={best['weld_types']} "
                f"hits={hits}/{tried} wall={best['elapsed_s']:.3f}s"
            )
    elif verbose:
        print(f"  FAILED tried={tried} wall={time.monotonic() - t0:.3f}s")
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csp-tl", type=float, default=5.0)
    ap.add_argument("--budget", type=int, default=80)
    ap.add_argument("--skip-v4", action="store_true")
    args = ap.parse_args()

    rows = []
    for name, path in CASES:
        print(f"\n=== {name} ===")
        group, payload = _load_group(path)
        print(
            f"pipes={[(p.length, p.demand) for p in group.pipes]} "
            f"stock={sorted(_stock_qty(group).items())} "
            f"demand_len={group.demand_length}"
        )

        t0 = time.monotonic()
        from scripts._arcflow_v3 import _ffd_weld_incumbent

        ffd = _ffd_weld_incumbent(group)
        ffd_row = None
        if ffd:
            ffd_row = {
                "joints": ffd["joints"],
                "util": round(ffd["util"], 6),
                "weld_types": ffd["weld_types"],
                "cut_types": ffd["cut_types"],
                "elapsed_s": round(time.monotonic() - t0, 4),
            }
        print(f"  ffd_weld: {ffd_row}")

        modes = [(False, "stock_align")]
        if name == "cr":
            modes.append((True, "legacy_seed"))

        for use_legacy, tag in modes:
            sol = solve_weld_then_cut(
                group,
                use_legacy=use_legacy,
                csp_tl=args.csp_tl,
                max_mix_combos=args.budget,
                verbose=True,
            )
            row = {
                "case": name,
                "mode": tag,
                "ok": sol is not None,
                "joints": None if sol is None else sol["joints"],
                "weld_types": None if sol is None else sol["weld_types"],
                "cut_types": None if sol is None else sol["cut_types"],
                "util": None if sol is None else round(sol["util"], 6),
                "elapsed_s": None if sol is None else round(sol["elapsed_s"], 4),
                "attempts": None if sol is None else sol.get("attempts"),
                "feasible_hits": None if sol is None else sol.get("feasible_hits"),
                "pattern_choice": None if sol is None else sol.get("pattern_choice"),
                "bars_left": None if sol is None else sol.get("bars_left"),
                "solver": None if sol is None else sol.get("solver"),
                "ffd_weld": ffd_row,
            }
            rows.append(row)

        if not args.skip_v4:
            t0 = time.monotonic()
            from scripts.solve_v4_production import solve as v4_solve

            tr = payload.get("Target_Util_Rate", group.target_rate)
            try:
                uf = float(tr)
                if uf > 1.5:
                    uf /= 100
            except (TypeError, ValueError):
                uf = 0.95
            v4 = v4_solve(group, tl=15.0, util_floor=max(0.95, uf), verbose=False)
            rows.append({
                "case": name,
                "mode": "v4_current",
                "ok": v4 is not None,
                "joints": None if v4 is None else v4.get("joints"),
                "weld_types": None if v4 is None else v4.get("weld_types"),
                "cut_types": None if v4 is None else v4.get("cut_types"),
                "util": None if v4 is None else round(float(v4.get("util") or 0), 6),
                "elapsed_s": round(time.monotonic() - t0, 4),
                "solver_path": None if v4 is None else v4.get("solver_path"),
            })
            print(
                f"  [v4_current] joints={rows[-1]['joints']} util={rows[-1]['util']} "
                f"wall={rows[-1]['elapsed_s']}s path={rows[-1]['solver_path']}"
            )

    # 参照：老软件 Cr
    rows.append({
        "case": "cr",
        "mode": "legacy_reference",
        "ok": True,
        "joints": 115,
        "util": 0.997,
        "note": "from prior old-software JSON comparison",
    })

    out_path = ACCEPT / "weld_then_cut_probe.json"
    out_path.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")

    print("\n=== SUMMARY ===")
    for r in rows:
        if r.get("mode") == "legacy_reference":
            print(f"  {r['case']:4} {r['mode']:<16} joints={r.get('joints')} util≈{r.get('util')}")
        else:
            print(
                f"  {r['case']:4} {r['mode']:<16} ok={r.get('ok')} "
                f"joints={r.get('joints')} util={r.get('util')} "
                f"wall={r.get('elapsed_s')}s"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
