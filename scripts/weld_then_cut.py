"""泛化「先拼后切」主求解器。

S1: 互补连锁混拼（纯二段种子 → core mate → 紧缺母料嵌入 → 短管整段 → 填充）
S2: 经典一维 CSP（SCIP）
反馈: S2 失败换种子 / primary / 紧缺消耗量

不含任何老软件题解写死。详见 docs/v4-weld-then-cut-plan.md
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any


def _stock_qty(group) -> dict[int, int]:
    out: dict[int, int] = {}
    for st in group.stocks:
        out[st.length] = out.get(st.length, 0) + st.quantity
    return out


def _pattern_legal(
    pipe, seq: tuple[int, ...], min_cut: int, min_wd: int, max_stock: int
) -> bool:
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
            if i >= 1 and seg < min_wd:
                return False
    return True


def build_sigma(group, *, closure_iters: int = 6, sigma_cap: int = 4000) -> list[int]:
    """母料对齐段字母表：残段 + 装箱补对闭包。"""
    kerf = group.blade_margin
    min_cut = max(group.min_cut_length, 1)
    stocks = sorted({s.length for s in group.stocks})
    max_stock = max(stocks)
    sigma: set[int] = set(stocks)

    for S in stocks:
        for n in (2, 3, 4):
            piece = (S - kerf * (n - 1)) // n
            if min_cut <= piece <= max_stock:
                sigma.add(piece)

    for p in group.pipes:
        if min_cut <= p.length <= max_stock:
            sigma.add(p.length)
        for S in stocks:
            if p.length <= S:
                continue
            for pos in (S, p.length - S):
                if not (0 < pos < p.length and p.weld_allowed(pos)):
                    continue
                a, b = pos, p.length - pos
                if min_cut <= a <= max_stock:
                    sigma.add(a)
                if min_cut <= b <= max_stock:
                    sigma.add(b)
        for iv in p.forbidden:
            for pos in (iv.start, iv.end, iv.start - 1, iv.end + 1):
                if 0 < pos < p.length and p.weld_allowed(pos):
                    a, b = pos, p.length - pos
                    if min_cut <= a <= max_stock:
                        sigma.add(a)
                    if min_cut <= b <= max_stock:
                        sigma.add(b)
        # 禁焊窗口抽样：补稠密 2 段字母表（D2 短料题）
        L = p.length
        forbidden = sorted(p.forbidden, key=lambda iv: iv.start)
        gaps: list[tuple[int, int]] = []
        cur = 0
        for iv in forbidden:
            if cur < iv.start:
                gaps.append((cur, iv.start))
            cur = max(cur, iv.end)
        if cur < L:
            gaps.append((cur, L))
        for lo, hi in gaps:
            a0 = max(lo, min_cut)
            a1 = min(hi - 1, L - min_cut, max_stock)
            if a0 > a1:
                continue
            stride = max(1, (a1 - a0) // 20)
            for pos in range(a0, a1 + 1, stride):
                if not p.weld_allowed(pos):
                    continue
                a, b = pos, L - pos
                if min_cut <= a <= max_stock:
                    sigma.add(a)
                if min_cut <= b <= max_stock:
                    sigma.add(b)

    def expand(sig: set[int]) -> set[int]:
        neu = set(sig)
        base = list(sig)
        for S in stocks:
            for a in base:
                for r in (S - a - kerf, S - a):
                    if min_cut <= r <= max_stock:
                        neu.add(r)
        for p in group.pipes:
            for a in base:
                if a >= p.length or a < min_cut:
                    continue
                if p.weld_allowed(a):
                    b = p.length - a
                    if min_cut <= b <= max_stock:
                        neu.add(b)
                pos = p.length - a
                if 0 < pos < p.length and p.weld_allowed(pos):
                    if min_cut <= a <= max_stock:
                        neu.add(a)
                    if min_cut <= pos <= max_stock:
                        neu.add(pos)
        return neu

    for _ in range(closure_iters):
        sigma = expand(sigma)

    out = sorted(s for s in sigma if min_cut <= s <= max_stock)
    if len(out) <= sigma_cap:
        return out

    mate_count: dict[int, int] = {s: 0 for s in out}
    out_set = set(out)
    for S in stocks:
        for a in out:
            for r in (S - a - kerf, S - a):
                if r in out_set:
                    mate_count[a] = mate_count.get(a, 0) + 1
                    mate_count[r] = mate_count.get(r, 0) + 1

    scored = []
    for s in out:
        score = mate_count.get(s, 0)
        if s in stocks:
            score += 10_000
        if any(p.length == s for p in group.pipes):
            score += 5_000
        for p in group.pipes:
            if p.length > s and (p.weld_allowed(s) or p.weld_allowed(p.length - s)):
                score += 50
                break
        scored.append((-score, s))
    scored.sort()
    kept = [s for _, s in scored[:sigma_cap]]
    kept_set = set(kept)
    for _ in range(4):
        added = False
        for S in stocks:
            for a in list(kept_set):
                for r in (S - a - kerf, S - a):
                    if r in out_set and r not in kept_set:
                        kept_set.add(r)
                        added = True
        if not added:
            break
        if len(kept_set) > int(sigma_cap * 1.25):
            break
    return sorted(kept_set)


def enum_weld_patterns(
    group,
    sigma: list[int],
    *,
    max_pats_per_pipe: int = 5000,
) -> dict[int, list[tuple[int, ...]]]:
    min_cut = max(group.min_cut_length, 1)
    min_wd = max(group.min_weld_distance, 1)
    max_stock = max(s.length for s in group.stocks)
    stocks = sorted({s.length for s in group.stocks}, reverse=True)
    sig_set = {s for s in sigma if min_cut <= s <= max_stock}
    kerf = group.blade_margin
    pack_splitters: set[int] = set(stocks)
    for p in group.pipes:
        if min_cut <= p.length <= max_stock:
            pack_splitters.add(p.length)
        for S in stocks:
            if p.length <= S:
                continue
            for pos in (S, p.length - S):
                if 0 < pos < p.length and p.weld_allowed(pos):
                    a, b = pos, p.length - pos
                    if a in sig_set:
                        pack_splitters.add(a)
                    if b in sig_set:
                        pack_splitters.add(b)
    for _ in range(10):
        grew = False
        for S in stocks:
            for a in list(pack_splitters):
                for r in (S - a - kerf, S - a):
                    if r in sig_set and r not in pack_splitters:
                        pack_splitters.add(r)
                        grew = True
        for p in group.pipes:
            for a in list(pack_splitters):
                if a >= p.length or a < min_cut:
                    continue
                if p.weld_allowed(a):
                    b = p.length - a
                    if b in sig_set and b not in pack_splitters:
                        pack_splitters.add(b)
                        grew = True
        if not grew:
            break
    complements: set[int] = set(stocks)
    for S in stocks:
        for a in list(pack_splitters):
            for r in (S - a - kerf, S - a):
                if r in sig_set:
                    complements.add(a)
                    complements.add(r)
    splitters = stocks + sorted(complements - set(stocks), reverse=True)
    out: dict[int, list[tuple[int, ...]]] = {}

    for i, pipe in enumerate(group.pipes):
        seen: set[tuple[int, ...]] = set()
        raw: list[tuple[int, ...]] = []

        def add(seq: tuple[int, ...]) -> None:
            if seq in seen:
                return
            if _pattern_legal(pipe, seq, min_cut, min_wd, max_stock):
                seen.add(seq)
                raw.append(seq)

        def rec(filled: int, parts: list[int]) -> None:
            if len(raw) >= max_pats_per_pipe * 3:
                return
            rem = pipe.length - filled
            if rem == 0:
                add(tuple(parts))
                return
            if min_cut <= rem <= max_stock:
                add(tuple(parts + [rem]))
            if len(parts) >= pipe.max_joints:
                return
            for seg in splitters[:300]:
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
                if len(raw) >= max_pats_per_pipe * 3:
                    return

        if min_cut <= pipe.length <= max_stock:
            add((pipe.length,))
        for a in complements:
            b = pipe.length - a
            if b < min_cut or b > max_stock:
                continue
            if b not in complements and b not in stocks:
                continue
            if pipe.weld_allowed(a):
                add((a, b))
        for S in stocks:
            if S > pipe.length or S < min_cut:
                continue
            left = pipe.length - S
            if min_cut <= left <= max_stock and pipe.weld_allowed(left):
                add((left, S))
                for a in splitters:
                    b = left - a
                    if b < min_wd or b > max_stock or a < min_cut:
                        continue
                    if a not in sig_set or b not in sig_set:
                        continue
                    if a not in complements and b not in complements:
                        continue
                    if pipe.weld_allowed(a):
                        add((a, b, S))
            if min_cut <= left <= max_stock and pipe.weld_allowed(S):
                add((S, left))
            for a in splitters:
                if a + S >= pipe.length:
                    continue
                c = pipe.length - a - S
                if c < min_cut or c > max_stock or S < min_wd:
                    continue
                if a not in sig_set or c not in sig_set:
                    continue
                if a not in complements and c not in complements:
                    continue
                if pipe.weld_allowed(a) and pipe.weld_allowed(a + S):
                    add((a, S, c))

        rec(0, [])

        def pack_score(seq: tuple[int, ...]) -> tuple:
            exact = sum(1 for x in seq if x in stocks)
            comp = sum(1 for x in seq if x in complements)
            return (len(seq) - 1, -exact, -comp, seq)

        selected: list[tuple[int, ...]] = []
        selected_set: set[tuple[int, ...]] = set()

        def take(seq: tuple[int, ...]) -> None:
            if seq in selected_set:
                return
            selected_set.add(seq)
            selected.append(seq)

        hard_cap = max(max_pats_per_pipe * 3, 15000)
        for seq in sorted(raw, key=pack_score):
            take(seq)
            if len(selected) >= hard_cap:
                break
        if not selected:
            raise RuntimeError(f"no weld pattern for pipe L={pipe.length}")
        out[i] = selected
    return out


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
        return {
            "used_len": 0,
            "cut_types": 0,
            "cut_patterns": {},
            "bars_left": dict(stock_qty),
        }

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


def _mates(seg: int, stocks: list[int], kerf: int, sigma: set[int]) -> set[int]:
    out: set[int] = set()
    for S in stocks:
        for r in (S - seg - kerf, S - seg):
            if r > 0 and r in sigma:
                out.add(r)
    return out


def _mate_on(
    seg: int, stock: int, kerf: int, sigma: set[int]
) -> int | None:
    for r in (stock - seg - kerf, stock - seg):
        if r > 0 and r in sigma:
            return r
    return None


def _best_mate(seg: int, stocks: list[int], kerf: int, sigma: set[int]) -> int:
    ms = _mates(seg, stocks, kerf, sigma)
    return max(ms) if ms else 0


def _near_stock_fake(seg: int, stocks: list[int], kerf: int, stock_set: set[int]) -> bool:
    if seg in stock_set:
        return False
    return any(0 < abs(S - seg) <= max(kerf * 2, 30) for S in stocks)


def _filter_ok_patterns(
    raw: dict[int, list[tuple[int, ...]]],
    stock_set: set[int],
    stocks: list[int],
    kerf: int,
    sigma: set[int],
    max_stock: int,
) -> dict[int, list[tuple[int, ...]]]:
    mate_floor = max(1500, int(0.12 * max_stock))
    huge = int(0.70 * max_stock)

    def hostile(s: int) -> bool:
        if s in stock_set:
            return False
        if _near_stock_fake(s, stocks, kerf, stock_set):
            return True
        if s >= huge and _best_mate(s, stocks, kerf, sigma) < mate_floor:
            return True
        return False

    def pattern_ok(seq: tuple[int, ...]) -> bool:
        if any(hostile(s) for s in seq):
            return False
        has_st = any(s in stock_set for s in seq)
        if len(seq) == 1:
            return True
        if len(seq) == 2 and not has_st:
            return all(_best_mate(s, stocks, kerf, sigma) >= mate_floor for s in seq)
        if has_st:
            return all(
                s in stock_set or _best_mate(s, stocks, kerf, sigma) >= mate_floor
                for s in seq
            )
        return False

    return {
        i: [seq for seq in pats if pattern_ok(seq)] for i, pats in raw.items()
    }


def _compact_pools(
    ok: dict[int, list[tuple[int, ...]]],
    group,
    stock_set: set[int],
    stocks: list[int],
    scarce: list[int],
    max_stock: int,
    kerf: int,
    sigma: set[int],
) -> dict[int, list[tuple[int, ...]]]:
    """压缩拼法池：按 core-mate 连锁保留种子与可挂紧缺/充裕拼法。"""
    # 1) 收集「双 max-mate」纯二段，按 core={ma,mb} 分组
    core_seeds: dict[frozenset[int], list[tuple[int, int, tuple[int, ...]]]] = defaultdict(
        list
    )
    for i, pipe in enumerate(group.pipes):
        if pipe.length <= max_stock:
            continue
        for seq in ok[i]:
            if len(seq) != 2 or any(s in stock_set for s in seq):
                continue
            a, b = seq
            ma = _mate_on(a, max_stock, kerf, sigma)
            mb = _mate_on(b, max_stock, kerf, sigma)
            if ma is None or mb is None:
                continue
            core = frozenset({ma, mb})
            core_seeds[core].append((i, abs(a - b), seq))

    # 2) 只保留能挂到紧缺三段的 core
    scarce_three_segs: set[int] = set()
    for i, pats in ok.items():
        for seq in pats:
            if len(seq) != 3 or not any(S in seq for S in scarce):
                continue
            for x in seq:
                if x not in stock_set:
                    scarce_three_segs.add(x)

    # 段频：紧缺三段中出现次数（连锁强度）
    seg_freq: Counter[int] = Counter()
    for pats in ok.values():
        for seq in pats:
            if len(seq) != 3 or not any(S in seq for S in scarce):
                continue
            for x in seq:
                if x not in stock_set:
                    seg_freq[x] += 1
                    scarce_three_segs.add(x)

    live_ranked: list[tuple[int, int, int, frozenset[int]]] = []
    for core in core_seeds:
        hits = sum(1 for c in core if c in scarce_three_segs)
        if hits <= 0 and scarce:
            continue
        strength = sum(seg_freq.get(c, 0) for c in core)
        max_ad = max(ad for _i, ad, _s in core_seeds[core])
        live_ranked.append((hits, strength, max_ad, core))
    # 双 hit + 高频 core 优先（不要被极端不对称挤掉 1875|4177）
    live_ranked.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    live_cores = [c for h, _s, _a, c in live_ranked if h >= 2][:30]
    if len(live_cores) < 8:
        for _h, _s, _a, c in live_ranked:
            if c not in live_cores:
                live_cores.append(c)
            if len(live_cores) >= 15:
                break
    if not live_cores and core_seeds:
        live_cores = [
            c
            for c, _ in sorted(
                core_seeds.items(),
                key=lambda kv: -max(ad for _i, ad, _s in kv[1]),
            )[:12]
        ]

    core_union: set[int] = set().union(*live_cores) if live_cores else set()

    out: dict[int, list[tuple[int, ...]]] = {}
    for i, pipe in enumerate(group.pipes):
        keep: list[tuple[int, ...]] = []
        seen: set[tuple[int, ...]] = set()

        def take(seq: tuple[int, ...]) -> None:
            if seq not in seen:
                seen.add(seq)
                keep.append(seq)

        if pipe.length <= max_stock:
            take((pipe.length,))

        for core in live_cores:
            cands = [(ad, seq) for (pi, ad, seq) in core_seeds[core] if pi == i]
            cands.sort(reverse=True)
            for _ad, seq in cands[:10]:
                take(seq)

        # 只保留含 core 段的拼法（禁止 mate 闭包膨胀挤掉 4177/1875）
        for seq in ok[i]:
            if any(x in core_union for x in seq):
                take(seq)

        if not core_union or pipe.length > max_stock:
            stock_pats = [
                seq
                for seq in ok[i]
                if any(s in stock_set for s in seq) and len(seq) in (2, 3)
            ]
            stock_pats.sort(
                key=lambda seq: (
                    len(seq) - 1,
                    -sum(1 for x in seq if x in stock_set),
                    seq,
                )
            )
            for seq in stock_pats[:150]:
                take(seq)

        out[i] = keep if keep else ok[i][:300]
    return out


def _build_scarce_index(
    pools: dict[int, list[tuple[int, ...]]], scarce: list[int]
) -> dict[int, list[tuple[int, tuple[int, ...]]]]:
    idx: dict[int, list[tuple[int, tuple[int, ...]]]] = {S: [] for S in scarce}
    for i, pats in pools.items():
        for seq in pats:
            for S in scarce:
                if seq.count(S) == 1:
                    idx[S].append((i, seq))
    return idx


def _seed_link_score_fast(
    seq: tuple[int, ...],
    scarce_idx: dict[int, list[tuple[int, tuple[int, ...]]]],
    max_stock: int,
    kerf: int,
    sigma: set[int],
    stocks: list[int],
) -> tuple:
    a, b = seq
    ma = _mate_on(a, max_stock, kerf, sigma)
    mb = _mate_on(b, max_stock, kerf, sigma)
    if ma is None or mb is None:
        return (10**9,)
    core = {ma, mb}
    link_hits = 0
    for S, items in scarce_idx.items():
        hit_pipes: set[int] = set()
        for i, pat in items:
            if any(c in pat for c in core):
                hit_pipes.add(i)
        link_hits += len(hit_pipes)
    return (
        -link_hits,
        -abs(a - b),
        -min(_best_mate(a, stocks, kerf, sigma), _best_mate(b, stocks, kerf, sigma)),
        seq,
    )


def _try_seed_construct(
    group,
    pools: dict[int, list[tuple[int, ...]]],
    scarce_idx: dict[int, list[tuple[int, tuple[int, ...]]]],
    seed_i: int,
    seed_seq: tuple[int, ...],
    stock_qty: dict[int, int],
    stocks: list[int],
    scarce: list[int],
    pack_stock: int,
    kerf: int,
    sigma: set[int],
    *,
    primary: int | None,
    leave_scarce: dict[int, int] | None = None,
) -> dict[tuple[int, tuple[int, ...]], int] | None:
    """互补连锁构造：短管整段 → 紧缺嵌入(primary) → core+充裕母料填充。"""
    stock_set = set(stock_qty)
    max_len = max(stocks)
    non_stock = [s for s in seed_seq if s not in stock_set]
    core = {
        m
        for s in non_stock
        for m in [_mate_on(s, pack_stock, kerf, sigma)]
        if m is not None
    }
    if not core:
        # 母料种子：用非母料段的任意 mate 作 core
        for s in non_stock:
            core |= _mates(s, stocks, kerf, sigma)
        core = {c for c in core if c not in stock_set}
    if not core and non_stock:
        core = set(non_stock)
    if not core:
        return None
    if primary is None:
        primary = max(core)

    remaining = {i: group.pipes[i].demand for i in range(len(group.pipes))}
    assign: dict[tuple[int, tuple[int, ...]], int] = {
        (seed_i, seed_seq): remaining[seed_i]
    }
    remaining[seed_i] = 0
    scarce_left = dict(stock_qty)
    leave = leave_scarce or {}
    for S, keep in leave.items():
        scarce_left[S] = max(0, scarce_left.get(S, 0) - keep)

    def consume(i: int, seq: tuple[int, ...], c: int) -> None:
        assign[i, seq] = assign.get((i, seq), 0) + c
        remaining[i] -= c
        for S in stocks:
            if S in seq:
                scarce_left[S] -= c * seq.count(S)

    # 短管强制整段（不依赖池内是否保留）
    for i, pipe in enumerate(group.pipes):
        if remaining[i] <= 0:
            continue
        if pipe.length <= max_len:
            consume(i, (pipe.length,), remaining[i])

    def pick_scarce(
        S: int, require_primary: bool
    ) -> tuple[int, tuple[int, ...], int] | None:
        best = None
        for i, seq in scarce_idx.get(S, []):
            rem = remaining[i]
            if rem <= 0:
                continue
            prim_hit = 1 if primary in seq else 0
            if require_primary and not prim_hit:
                continue
            core_hit = sum(1 for x in seq if x in core)
            max_c = rem
            for SS in scarce:
                cnt = seq.count(SS)
                if cnt:
                    max_c = min(max_c, scarce_left[SS] // cnt)
            if max_c <= 0:
                continue
            score = (
                -prim_hit,
                -core_hit,
                0 if len(seq) == 3 else 1,
                -rem,
                len(seq) - 1,
                seq,
            )
            if best is None or score < best[0]:
                best = (score, i, seq, max_c)
        if best is None:
            return None
        return best[1], best[2], best[3]

    for S in sorted(scarce, key=lambda x: stock_qty[x]):
        while scarce_left[S] > 0 and any(v > 0 for v in remaining.values()):
            picked = pick_scarce(S, require_primary=True)
            if picked is None:
                picked = pick_scarce(S, require_primary=False)
            if picked is None:
                break
            i, seq, max_c = picked
            consume(i, seq, max_c)

    while any(v > 0 for v in remaining.values()):
        best = None
        for i, rem in remaining.items():
            if rem <= 0:
                continue
            for seq in pools[i]:
                max_c = rem
                for SS in scarce:
                    cnt = seq.count(SS)
                    if cnt:
                        max_c = min(max_c, scarce_left[SS] // cnt)
                if max_c <= 0:
                    continue
                core_hit = sum(1 for x in seq if x in core)
                prim_hit = 1 if primary in seq else 0
                ab = 1 if pack_stock in seq else 0
                score = (-core_hit, -prim_hit, -ab, len(seq) - 1, seq)
                if best is None or score < best[0]:
                    best = (score, i, seq, max_c)
        if best is None:
            return None
        _, i, seq, max_c = best
        consume(i, seq, max_c)

    used = Counter()
    for (_i, seq), c in assign.items():
        for s in seq:
            if s in stock_set:
                used[s] += c
    if any(used[S] > stock_qty[S] for S in stock_qty):
        return None
    if any(remaining[i] != 0 for i in remaining):
        return None
    return assign


def _demand_of(
    assign: dict[tuple[int, tuple[int, ...]], int]
) -> tuple[Counter[int], int]:
    demand: Counter[int] = Counter()
    joints = 0
    for (_i, seq), c in assign.items():
        joints += (len(seq) - 1) * c
        for s in seq:
            demand[s] += c
    return demand, joints


def _eval_assign_csp(
    group,
    assign: dict[tuple[int, tuple[int, ...]], int],
    stock_qty: dict[int, int],
    kerf: int,
    *,
    tl: float,
    util_floor: float,
) -> dict[str, Any] | None:
    used = Counter()
    for (_i, seq), c in assign.items():
        for s in seq:
            if s in stock_qty:
                used[s] += c
    if any(used[S] > stock_qty[S] for S in stock_qty):
        return None
    demand, joints = _demand_of(assign)
    cut = cut_csp_ilp(demand, stock_qty, kerf, tl=tl)
    if cut is None:
        return None
    util = group.demand_length / cut["used_len"] if cut["used_len"] else 0.0
    if util < util_floor - 0.005:
        # 仍返回，由调用方按词典序筛选
        pass
    return {
        "joints": joints,
        "weld_types": len(assign),
        "cut_types": cut["cut_types"],
        "seg_types": len(demand),
        "used_len": cut["used_len"],
        "util": util,
        "weld_patterns": dict(assign),
        "cut_patterns": dict(cut["cut_patterns"]),
    }


def _scarce_mix_search(
    group,
    pools: dict[int, list[tuple[int, ...]]],
    stock_qty: dict[int, int],
    stocks: list[int],
    scarce: list[int],
    pack_stock: int,
    kerf: int,
    *,
    util_floor: float,
    deadline: float,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """压焊口二阶段：紧缺优先用 2 段嵌入，其余用充裕母料 2/3 段，CSP 验收。

    几何下界常为「每根长管 1 焊口」；全 2 段往往切不可行。本搜索在可行前提下压焊口。
    """
    if not scarce or len(group.pipes) < 2:
        return None
    max_len = max(stocks)
    long_idx = [i for i, p in enumerate(group.pipes) if p.length > max_len]
    short_assign: dict[tuple[int, tuple[int, ...]], int] = {}
    for i, p in enumerate(group.pipes):
        if p.length <= max_len:
            short_assign[i, (p.length,)] = p.demand

    # 每管：含紧缺 S 的 2/3 段；含 pack 的 2/3 段；纯 2 段
    def best_two_with(i: int, S: int) -> tuple[int, ...] | None:
        cands = [seq for seq in pools[i] if seq.count(S) == 1 and len(seq) == 2]
        if not cands:
            return None
        cands.sort(key=lambda seq: (abs(seq[0] - seq[1]), seq))
        return cands[0]

    def best_three_with(i: int, S: int) -> tuple[int, ...] | None:
        cands = [seq for seq in pools[i] if seq.count(S) == 1 and len(seq) == 3]
        if not cands:
            return None
        cands.sort(
            key=lambda seq: (
                -abs(seq[0] - seq[1]),
                seq,
            )
        )
        return cands[0]

    def best_pack_two(i: int) -> tuple[int, ...] | None:
        cands = [
            seq
            for seq in pools[i]
            if pack_stock in seq and len(seq) == 2 and seq.count(pack_stock) == 1
        ]
        return cands[0] if cands else None

    def best_pack_three(i: int) -> tuple[int, ...] | None:
        cands = [
            seq
            for seq in pools[i]
            if pack_stock in seq and len(seq) == 3 and seq.count(pack_stock) == 1
        ]
        if not cands:
            return None
        cands.sort(key=lambda seq: (-abs(seq[0] - seq[1]), seq))
        return cands[0]

    def best_pure_two(i: int) -> tuple[int, ...] | None:
        cands = [
            seq
            for seq in pools[i]
            if len(seq) == 2 and not any(s in stock_qty for s in seq)
        ]
        if not cands:
            return None
        cands.sort(key=lambda seq: (-abs(seq[0] - seq[1]), seq))
        return cands[0]

    # 仅支持 1~2 种紧缺（Cr 形态）；更多种时跳过网格
    if len(scarce) > 2:
        scarce = sorted(scarce, key=lambda S: stock_qty[S])[:2]
    S_small, S_big = (scarce + [None, None])[:2]
    if S_small is None:
        return None

    best: dict[str, Any] | None = None
    tried = 0

    # 网格：小紧缺全用 2 段或 3 段挂到各长管；大紧缺拆到各长管 2 段
    # 简化：小紧缺 qty 全挂到同一长管（2 段或 3 段），大紧缺在长管间分配 2 段
    for host in long_idx:
        if time.monotonic() >= deadline - 0.3:
            break
        two_s = best_two_with(host, S_small) if S_small else None
        three_s = best_three_with(host, S_small) if S_small else None
        modes_small = []
        if three_s:
            modes_small.append(("3", three_s))
        if two_s:
            modes_small.append(("2", two_s))
        if not modes_small:
            continue
        q_small = stock_qty[S_small]
        if q_small > group.pipes[host].demand:
            continue

        for mode_s, pat_s in modes_small:
            # 大紧缺分配：在 long_idx 上拆份（步长）
            q_big = stock_qty[S_big] if S_big else 0
            # 生成有限分配向量
            allocs: list[dict[int, int]] = []
            if not S_big or q_big <= 0:
                allocs.append({i: 0 for i in long_idx})
            else:
                # 宿主管优先吃满，其余分给其它长管
                for take_host in range(0, min(q_big, group.pipes[host].demand - q_small) + 1):
                    rem = q_big - take_host
                    others = [i for i in long_idx if i != host]
                    if not others:
                        if rem == 0:
                            allocs.append({host: take_host})
                        continue
                    # rem 全给第一个 other，或均分到 others
                    for o in others:
                        if rem > group.pipes[o].demand:
                            continue
                        al = {i: 0 for i in long_idx}
                        al[host] = take_host
                        al[o] = rem
                        allocs.append(al)
                    # rem 拆成两份
                    if len(others) >= 2 and rem >= 2:
                        o1, o2 = others[0], others[1]
                        for a in range(0, rem + 1):
                            b = rem - a
                            if a <= group.pipes[o1].demand and b <= group.pipes[o2].demand:
                                al = {i: 0 for i in long_idx}
                                al[host] = take_host
                                al[o1] = a
                                al[o2] = b
                                allocs.append(al)

            # 去重
            seen_al: set[tuple] = set()
            uniq_al = []
            for al in allocs:
                key = tuple(sorted(al.items()))
                if key in seen_al:
                    continue
                seen_al.add(key)
                uniq_al.append(al)

            for al in uniq_al:
                if time.monotonic() >= deadline - 0.25:
                    break
                # 构造 assign
                rem_dem = {i: group.pipes[i].demand for i in long_idx}
                assign = dict(short_assign)
                # small scarce on host
                assign[host, pat_s] = assign.get((host, pat_s), 0) + q_small
                rem_dem[host] -= q_small
                if rem_dem[host] < 0:
                    continue
                ok = True
                for i, cnt in al.items():
                    if cnt <= 0:
                        continue
                    pat = best_two_with(i, S_big)
                    if pat is None or cnt > rem_dem[i]:
                        ok = False
                        break
                    assign[i, pat] = assign.get((i, pat), 0) + cnt
                    rem_dem[i] -= cnt
                if not ok:
                    continue
                # 填充剩余：优先纯 2 段 / pack 2 段，再 pack 3 段
                for i in long_idx:
                    need = rem_dem[i]
                    if need <= 0:
                        continue
                    filled = False
                    for pat in (
                        best_pure_two(i),
                        best_pack_two(i),
                        best_pack_three(i),
                    ):
                        if pat is None:
                            continue
                        assign[i, pat] = assign.get((i, pat), 0) + need
                        filled = True
                        break
                    if not filled:
                        ok = False
                        break
                if not ok:
                    continue
                tried += 1
                rem_tl = max(0.5, min(2.5, deadline - time.monotonic()))
                sol = _eval_assign_csp(
                    group, assign, stock_qty, kerf, tl=rem_tl, util_floor=util_floor
                )
                if sol is None:
                    continue
                key = (
                    0 if sol["util"] >= util_floor - 0.005 else 1,
                    sol["joints"],
                    sol["weld_types"],
                    -sol["util"],
                )
                if best is None or key < best.get("_key", (9,)):
                    best = sol
                    best["_key"] = key
                    best["solver"] = "weld_then_cut"
                    best["solver_path"] = [
                        "weld_then_cut",
                        "scarce_mix_s1",
                        "cut_csp_s2",
                    ]
                    best["meta"] = {
                        "host": group.pipes[host].length,
                        "small_mode": mode_s,
                        "tried_mix": tried,
                    }
                    if verbose:
                        print(
                            f"  MIX joints={sol['joints']} util={sol['util']:.6f} "
                            f"host={group.pipes[host].length} mode={mode_s}",
                            flush=True,
                        )
    if best is not None:
        best.pop("_key", None)
        if verbose:
            print(f"  MIX done tried={tried} best_j={best['joints']}", flush=True)
    return best


def _local_reduce_joints(
    group,
    assign: dict[tuple[int, tuple[int, ...]], int],
    pools: dict[int, list[tuple[int, ...]]],
    stock_qty: dict[int, int],
    scarce: list[int],
    pack_stock: int,
    kerf: int,
    *,
    util_floor: float,
    deadline: float,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """在可行解邻域压焊口：把某管上的紧缺三段挪成另一管的紧缺二段，本管改充裕三段。"""
    if not assign or not scarce:
        return None
    max_len = max(stock_qty)
    long_idx = [i for i, p in enumerate(group.pipes) if p.length > max_len]
    best_eval = _eval_assign_csp(
        group, assign, stock_qty, kerf, tl=2.0, util_floor=util_floor
    )
    if best_eval is None:
        return None
    best_assign = dict(assign)

    def pack_three(i: int) -> tuple[int, ...] | None:
        cands = [
            seq
            for seq in pools[i]
            if pack_stock in seq and len(seq) == 3 and seq.count(pack_stock) == 1
        ]
        if not cands:
            return None
        cands.sort(key=lambda seq: (-abs(seq[0] - seq[1]), seq))
        return cands[0]

    def scarce_two(i: int, S: int) -> tuple[int, ...] | None:
        cands = [seq for seq in pools[i] if seq.count(S) == 1 and len(seq) == 2]
        return cands[0] if cands else None

    improved = True
    rounds = 0
    while improved and rounds < 20 and time.monotonic() < deadline - 0.4:
        improved = False
        rounds += 1
        # 找：管 A 上含紧缺 S 的三段拼法
        for S in scarce:
            donors = []
            for (i, seq), cnt in list(best_assign.items()):
                if cnt > 0 and S in seq and len(seq) == 3 and i in long_idx:
                    donors.append((i, seq, cnt))
            for di, dseq, dcnt in donors:
                p3 = pack_three(di)
                if p3 is None:
                    continue
                for ti in long_idx:
                    if ti == di:
                        continue
                    t2 = scarce_two(ti, S)
                    if t2 is None:
                        continue
                    # receiver 上必须有可替换的充裕三段
                    recv_pack_cnt = sum(
                        c
                        for (ii, seq), c in best_assign.items()
                        if ii == ti and pack_stock in seq and len(seq) == 3
                    )
                    if recv_pack_cnt <= 0:
                        continue
                    max_k = min(dcnt, recv_pack_cnt)
                    for k in range(1, max_k + 1):
                        if time.monotonic() >= deadline - 0.3:
                            break
                        trial = defaultdict(int)
                        for key, c in best_assign.items():
                            trial[key] += c
                        # donor: 紧缺三段 -> 充裕三段
                        trial[di, dseq] -= k
                        if trial[di, dseq] <= 0:
                            del trial[di, dseq]
                        trial[di, p3] += k
                        # receiver: 充裕三段 -> 紧缺二段
                        need = k
                        for (ii, seq) in list(trial.keys()):
                            if need <= 0:
                                break
                            if ii != ti or pack_stock not in seq or len(seq) != 3:
                                continue
                            take = min(trial[ii, seq], need)
                            trial[ii, seq] -= take
                            need -= take
                            if trial[ii, seq] <= 0:
                                del trial[ii, seq]
                        if need > 0:
                            continue
                        trial[ti, t2] += k
                        ok_dem = True
                        for i, p in enumerate(group.pipes):
                            got = sum(c for (ii, _), c in trial.items() if ii == i)
                            if got != p.demand:
                                ok_dem = False
                                break
                        if not ok_dem:
                            continue
                        sol = _eval_assign_csp(
                            group,
                            dict(trial),
                            stock_qty,
                            kerf,
                            tl=1.5,
                            util_floor=util_floor,
                        )
                        if sol is None:
                            continue
                        if (sol["joints"], -sol["util"]) < (
                            best_eval["joints"],
                            -best_eval["util"],
                        ):
                            best_eval = sol
                            best_assign = dict(trial)
                            improved = True
                            if verbose:
                                print(
                                    f"  LOCAL j={sol['joints']} util={sol['util']:.6f} "
                                    f"move {S}: {group.pipes[di].length}->2seg@"
                                    f"{group.pipes[ti].length} x{k}",
                                    flush=True,
                                )
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                break

    if best_eval is None:
        return None
    best_eval["weld_patterns"] = best_assign
    best_eval["solver"] = "weld_then_cut"
    best_eval["solver_path"] = [
        "weld_then_cut",
        "complement_link_s1",
        "local_reduce_joints",
        "cut_csp_s2",
    ]
    best_eval["meta"] = {"local_rounds": rounds}
    return best_eval


def solve(
    group,
    *,
    tl: float = 30.0,
    util_floor: float | None = None,
    closure_iters: int = 6,
    sigma_cap: int = 8000,
    max_pats_per_pipe: int = 5000,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """先拼后切主入口：互补连锁 S1 → CSP S2 → 多种子反馈。"""
    t0 = time.monotonic()
    deadline = t0 + max(2.0, float(tl))
    stock_qty = _stock_qty(group)
    stocks = sorted(stock_qty)
    max_stock = max(stocks)
    # 充裕母料：按根数（再按长度）——切拼配对与填充基准，勿用最长（T91 最长可能很紧）
    pack_stock = max(stocks, key=lambda S: (stock_qty[S], S))
    stock_set = set(stocks)
    kerf = group.blade_margin
    uf = 0.95 if util_floor is None else max(0.95, float(util_floor))
    if group.target_rate is not None and util_floor is None:
        uf = max(0.95, float(group.target_rate))

    total_pipes = sum(p.demand for p in group.pipes) or 1
    scarce = [
        S
        for S in stocks
        if S != pack_stock and stock_qty[S] < max(total_pipes * 0.45, stock_qty[pack_stock])
    ]

    sigma_list = build_sigma(group, closure_iters=closure_iters, sigma_cap=sigma_cap)
    sigma = set(sigma_list)
    raw = enum_weld_patterns(group, sigma_list, max_pats_per_pipe=max_pats_per_pipe)
    ok_full = _filter_ok_patterns(raw, stock_set, stocks, kerf, sigma, max_stock)
    pools = _compact_pools(
        ok_full, group, stock_set, stocks, scarce, pack_stock, kerf, sigma
    )
    scarce_idx = _build_scarce_index(pools, scarce)
    # 压焊口搜索用完整合法池（避免 compact 丢掉 2 段母料拼）
    mix_pools = ok_full

    if verbose:
        print(
            f"  sigma={len(sigma)} pack={pack_stock} scarce={scarce} "
            f"pools={[len(pools[i]) for i in range(len(group.pipes))]}",
            flush=True,
        )

    seeds: list[tuple[int, tuple[int, ...], tuple]] = []
    for i, pipe in enumerate(group.pipes):
        if pipe.length <= max_stock:
            continue
        for seq in pools[i]:
            if len(seq) == 2 and not any(s in stock_set for s in seq):
                a, b = seq
                if (
                    _mate_on(a, pack_stock, kerf, sigma) is None
                    or _mate_on(b, pack_stock, kerf, sigma) is None
                ):
                    continue
                sc = _seed_link_score_fast(
                    seq, scarce_idx, pack_stock, kerf, sigma, stocks
                )
                seeds.append((i, seq, sc))
            elif len(seq) == 2 and any(s in stock_set for s in seq):
                # 母料二段种子（T91 等无纯互补时）
                other = next(x for x in seq if x not in stock_set)
                sc = (
                    5,
                    -_best_mate(other, stocks, kerf, sigma),
                    len(seq) - 1,
                    seq,
                )
                seeds.append((i, seq, sc))
            elif len(seq) == 3 and pack_stock in seq:
                others = [x for x in seq if x not in stock_set]
                if len(others) == 2 and all(
                    _best_mate(x, stocks, kerf, sigma) >= 500 for x in others
                ):
                    sc = (
                        8,
                        -sum(_best_mate(x, stocks, kerf, sigma) for x in others),
                        len(seq) - 1,
                        seq,
                    )
                    seeds.append((i, seq, sc))
            elif len(seq) == 3 and any(s in scarce for s in seq):
                # 短料 3 段种子：极紧库存必须吃掉 scarce
                sc = (
                    6,
                    -sum(1 for s in seq if s in scarce),
                    len(seq) - 1,
                    seq,
                )
                seeds.append((i, seq, sc))
    seeds.sort(key=lambda x: x[2])

    selected_seeds: list[tuple[int, tuple[int, ...]]] = []
    per_pipe: dict[int, int] = defaultdict(int)
    for i, seq, _sc in seeds:
        # 纯互补多种子；母料种子每管少留
        lim = 25 if not any(s in stock_set for s in seq) else 8
        if per_pipe[i] >= lim:
            continue
        selected_seeds.append((i, seq))
        per_pipe[i] += 1

    if not selected_seeds:
        # 极端兜底：任意合法二/三段
        for i, pipe in enumerate(group.pipes):
            if pipe.length <= max_stock:
                continue
            for seq in pools[i]:
                if len(seq) >= 2:
                    selected_seeds.append((i, seq))
                    break

    if verbose:
        print(f"  seeds={len(selected_seeds)}", flush=True)

    best: dict[str, Any] | None = None
    tried = 0
    hits = 0

    leave_opts: list[dict[int, int]] = []
    if scarce:
        largest_scarce = max(scarce, key=lambda S: stock_qty[S])
        if stock_qty[largest_scarce] >= 2:
            leave_opts.append({largest_scarce: 1})
    leave_opts.append({})

    for seed_i, seed_seq in selected_seeds:
        if time.monotonic() >= deadline - 0.4:
            break
        a0 = seed_seq[0]
        b0 = seed_seq[1] if len(seed_seq) > 1 else seed_seq[0]
        core = {
            m
            for m in (
                _mate_on(a0, pack_stock, kerf, sigma),
                _mate_on(b0, pack_stock, kerf, sigma),
            )
            if m is not None
        }
        # 对母料/三段种子，用构造函数内相同逻辑的 primary 候选
        non_stock = [s for s in seed_seq if s not in stock_set]
        if not core:
            for s in non_stock:
                core |= _mates(s, stocks, kerf, sigma)
            core = {c for c in core if c not in stock_set}
        if not core and non_stock:
            core = set(non_stock)
        prim_list: list[int | None] = []
        seen_p: set = set()
        for p in (sorted(core, reverse=True) if core else []) + (
            [min(core)] if len(core) > 1 else []
        ) + [None]:
            if p in seen_p:
                continue
            seen_p.add(p)
            prim_list.append(p)

        for primary in prim_list:
            for leave in leave_opts:
                if time.monotonic() >= deadline - 0.3:
                    break
                tried += 1
                assign = _try_seed_construct(
                    group,
                    pools,
                    scarce_idx,
                    seed_i,
                    seed_seq,
                    stock_qty,
                    stocks,
                    scarce,
                    pack_stock,
                    kerf,
                    sigma,
                    primary=primary,
                    leave_scarce=leave,
                )
                if assign is None:
                    continue
                demand, joints = _demand_of(assign)
                rem_tl = max(0.6, min(3.5, deadline - time.monotonic()))
                cut = cut_csp_ilp(demand, stock_qty, kerf, tl=rem_tl)
                if cut is None:
                    continue
                util = (
                    group.demand_length / cut["used_len"] if cut["used_len"] else 0.0
                )
                hits += 1
                weld_patterns = dict(assign)
                sol = {
                    "joints": joints,
                    "weld_types": len(weld_patterns),
                    "cut_types": cut["cut_types"],
                    "seg_types": len(demand),
                    "used_len": cut["used_len"],
                    "util": util,
                    "weld_patterns": weld_patterns,
                    "cut_patterns": dict(cut["cut_patterns"]),
                    "solver": "weld_then_cut",
                    "solver_path": [
                        "weld_then_cut",
                        "complement_link_s1",
                        "cut_csp_s2",
                    ],
                    "meta": {
                        "seed_pipe": group.pipes[seed_i].length,
                        "seed_seq": list(seed_seq),
                        "primary": primary,
                        "leave_scarce": leave,
                        "tried": tried,
                        "hits": hits,
                    },
                }
                key = (
                    0 if util >= uf - 0.005 else 1,
                    joints,
                    sol["weld_types"],
                    sol["cut_types"],
                    -util,
                )
                if best is None or key < best["_key"]:
                    best = sol
                    best["_key"] = key
                    if verbose:
                        print(
                            f"  HIT joints={joints} util={util:.6f} "
                            f"seed={group.pipes[seed_i].length}{seed_seq} "
                            f"prim={primary} leave={leave}",
                            flush=True,
                        )
                # 达标且焊口已较好：可早停
                if (
                    best is not None
                    and best["util"] >= uf - 0.005
                    and hits >= 3
                    and time.monotonic() - t0 >= min(6.0, tl * 0.35)
                ):
                    break
            if (
                best is not None
                and best["util"] >= uf - 0.005
                and hits >= 3
                and time.monotonic() - t0 >= min(6.0, tl * 0.35)
            ):
                break
        if (
            best is not None
            and best["util"] >= uf - 0.005
            and hits >= 3
            and time.monotonic() - t0 >= min(6.0, tl * 0.35)
        ):
            break

    if best is None:
        if verbose:
            print(
                f"  FAILED link-search tried={tried} wall={time.monotonic() - t0:.2f}s",
                flush=True,
            )
    else:
        best.pop("_key", None)

    # 二阶段：邻域压焊口（合法前提下可优于老软件 115）
    if best is not None and time.monotonic() < deadline - 0.5:
        loc = _local_reduce_joints(
            group,
            dict(best["weld_patterns"]),
            mix_pools,
            stock_qty,
            scarce,
            pack_stock,
            kerf,
            util_floor=uf,
            deadline=deadline,
            verbose=verbose,
        )
        if loc is not None:
            loc_key = (
                0 if loc["util"] >= uf - 0.005 else 1,
                loc["joints"],
                loc["weld_types"],
                loc["cut_types"],
                -loc["util"],
            )
            cur_key = (
                0 if best["util"] >= uf - 0.005 else 1,
                best["joints"],
                best["weld_types"],
                best.get("cut_types", 10**9),
                -best["util"],
            )
            if loc_key < cur_key:
                best = loc

    # 三阶段：紧缺混拼（从短管+种子重建，补盲区）
    if time.monotonic() < deadline - 0.5:
        mix = _scarce_mix_search(
            group,
            mix_pools,
            stock_qty,
            stocks,
            scarce,
            pack_stock,
            kerf,
            util_floor=uf,
            deadline=deadline,
            verbose=verbose,
        )
        if mix is not None:
            mix_key = (
                0 if mix["util"] >= uf - 0.005 else 1,
                mix["joints"],
                mix["weld_types"],
                mix["cut_types"],
                -mix["util"],
            )
            if best is None:
                best = mix
            else:
                cur_key = (
                    0 if best["util"] >= uf - 0.005 else 1,
                    best["joints"],
                    best["weld_types"],
                    best.get("cut_types", 10**9),
                    -best["util"],
                )
                if mix_key < cur_key:
                    best = mix

    if best is None:
        if verbose:
            print(
                f"  FAILED tried={tried} wall={time.monotonic() - t0:.2f}s",
                flush=True,
            )
        return None

    best["elapsed_s"] = time.monotonic() - t0
    best.setdefault("meta", {})
    best["meta"]["tried"] = tried
    best["meta"]["hits"] = hits
    best["solver"] = best.get("solver") or "weld_then_cut"
    if "solver_path" not in best:
        best["solver_path"] = ["weld_then_cut", "complement_link_s1", "cut_csp_s2"]
    if best["util"] < uf - 0.005:
        best["diagnosis"] = [f"util_below_floor:{best['util']:.4f}<{uf}"]
    if verbose:
        print(
            f"  DONE joints={best['joints']} util={best['util']:.6f} "
            f"cut={best['cut_types']} hits={hits}/{tried} "
            f"wall={best['elapsed_s']:.2f}s path={best.get('solver_path')}",
            flush=True,
        )
    return best
