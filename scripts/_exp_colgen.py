"""实证Ⅲ：列生成最小原型 —— 段长由 pricing 隐式生成（不预枚举字母表）。

依据 §11.33（子代理 ba3a7af3）：
  - Master LP：切列 x_p + 拼列 u_{i,w} + 段平衡(==) 耦合。
  - 切侧 pricing：格点背包 DP，在当前 Σ 上按 π 填满母料。
  - 拼侧 pricing：RCSP，在整数位置格点上按 π 拆管 —— **新段长的唯一来源**。
  - 两侧共享 Σ + π 迭代协同，Σ 动态增长。

验证目标（fc829dcc，管长仅 10100，oracle 已知 6 段）：
  列生成能否自动吐出那 6 段附近的段、达到老软件利用率与切/拼种类。

用法: python scripts/_exp_colgen.py <samples.json> <id前缀> [--tl 60] [--iters 60]
"""

from __future__ import annotations

import functools
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain import MaterialGroup, PipeDemand, parse_problem  # noqa: E402
from app.solver import _legal_pattern  # noqa: E402
import dataclasses  # noqa: E402


def merge_equivalent_pipes(group: MaterialGroup) -> MaterialGroup:
    """把物理等价的管型（同 长/max_joints/禁区）合并为单一需求。

    切/焊物理只看 (length, max_joints, forbidden)，图号仅是输出标签。
    合并后 pricing 只对唯一管型产段 → 切/拼种类自然收敛到少数几种，
    对齐老软件真实复杂度（同一 parts 被多图号复用不应计为多种）。
    """
    buckets: dict[tuple, list[PipeDemand]] = defaultdict(list)
    for p in group.pipes:
        key = (p.length, p.max_joints, tuple((iv.start, iv.end) for iv in p.forbidden))
        buckets[key].append(p)
    merged: list[PipeDemand] = []
    for key, members in buckets.items():
        rep = members[0]
        total = sum(m.demand for m in members)
        merged.append(dataclasses.replace(rep, demand=total))
    merged.sort(key=lambda p: (-p.length, -p.demand))
    return dataclasses.replace(group, pipes=tuple(merged))



# ---------------------------------------------------------------------------
# 候选段长集 L（拼侧 RCSP 的弧长来源）
#   优先复用 Σ（压种类）；补"母料边界导出段"（stock_len ⊖ Σ段，让段能恰好填满母料）；
#   禁区邻域；不再灌 100mm 粗网格（那是整百垃圾段的来源）。
# ---------------------------------------------------------------------------

def _bar_fill_lengths(P: int, stock_lens: list[int], max_stock: int) -> set[int]:
    """碎段塔满母料的收尾段长（段长隐式来自"填满"目标，非整百网格）。

    一根母料上放 k 根整管 P，剩余 tail=S-k*P 需要碎段填满；把 tail 再 2/3 等分
    得到更细的候选段。这覆盖老软件"把 12000 剩余拆成几段"的来源。
    """
    lens: set[int] = set()
    for S in stock_lens:
        k = 0
        while k * P <= S:
            tail = S - k * P
            if 0 < tail <= max_stock and tail <= P:
                lens.add(tail)
            for q in (2, 3, 4):
                if tail % q == 0 and tail // q > 0 and tail // q <= P:
                    lens.add(tail // q)
            k += 1
    return lens


def candidate_lengths(pipe: PipeDemand, group: MaterialGroup, sigma: set[int]) -> list[int]:
    max_stock = max(s.length for s in group.stocks)
    stock_lens = sorted({s.length for s in group.stocks})
    P = pipe.length
    lens: set[int] = set()

    # 1) Σ 里可作弧的段（复用优先，pricing 迭代已产出的精确段）
    for s in sigma:
        if 0 < s <= max_stock:
            lens.add(s)

    # 2) 母料填充导出段（碎段塔满母料的收尾长度，隐式来自"填满"目标）
    lens |= _bar_fill_lengths(P, stock_lens, max_stock)

    # 3) 管长互补：P ⊖ 已有段（拆管的另一段），两段互补拼成整管
    base = sorted(lens)
    for a in base:
        r = P - a
        if 0 < r <= max_stock:
            lens.add(r)

    # 4) 禁区邻域
    for interval in pipe.forbidden:
        for pos in (interval.start - 1, interval.end + 1):
            if 0 < pos <= max_stock:
                lens.add(pos)
            tail = P - pos
            if 0 < tail <= max_stock:
                lens.add(tail)

    # 5) 整管
    if P <= max_stock:
        lens.add(P)

    return sorted(l for l in lens if 0 <= P - l)


# ---------------------------------------------------------------------------
# 拼侧 pricing：RCSP —— 按 π 把管拆成段，新段长在此生成
# ---------------------------------------------------------------------------

def price_weld(pipe: PipeDemand, group: MaterialGroup, pi: dict[int, float],
               sigma: set[int], max_stock: int,
               new_seg_penalty: float = 0.0) -> tuple[float, tuple[int, ...]] | None:
    """返回 (Σπ_true, parts)：按 (π + 新段罚λ) 引导路径的合法拆管。

    A2：DP 路径成本 = Σ(π_ℓ + λ·[ℓ∉Σ])，逼 pricing 优先复用已有段、只在
    真正划算时才开新段。加入列的判定仍用**真实** Σπ（不含罚），罚仅引导选路。
    """
    L = pipe.length
    cand = candidate_lengths(pipe, group, sigma)
    maxseg = pipe.max_joints + 1
    INF = float("inf")
    # Dijkstra over (pos, nseg)
    import heapq
    dist: dict[tuple[int, int], float] = {(0, 0): 0.0}
    parent: dict[tuple[int, int], tuple[int, int, int]] = {}
    pq: list[tuple[float, int, int]] = [(0.0, 0, 0)]
    best: tuple[float, int, int] | None = None
    while pq:
        cost, c, j = heapq.heappop(pq)
        if cost > dist.get((c, j), INF):
            continue
        if c == L and j >= 1:
            if best is None or cost < best[0]:
                best = (cost, c, j)
            continue
        if j >= maxseg:
            continue
        for ell in cand:
            nc = c + ell
            if nc > L:
                continue
            is_first = (j == 0)
            is_last = (nc == L)
            if ell > max_stock:
                continue
            # min_weld_distance：内部段（非首非末）
            if not is_first and not is_last and ell < group.min_weld_distance:
                continue
            # forbidden：中间焊缝位置 nc 不能落禁区
            if not is_last and not pipe.weld_allowed(nc):
                continue
            arc = pi.get(ell, 0.0) + (new_seg_penalty if ell not in sigma else 0.0)
            ncost = cost + arc
            key = (nc, j + 1)
            if ncost < dist.get(key, INF) - 1e-12:
                dist[key] = ncost
                parent[key] = (c, j, ell)
                heapq.heappush(pq, (ncost, nc, j + 1))
    if best is None:
        return None
    _cost, c, j = best
    parts: list[int] = []
    while (c, j) != (0, 0):
        pc, pj, ell = parent[(c, j)]
        parts.append(ell)
        c, j = pc, pj
    parts.reverse()
    # 用 _legal_pattern 做最终合法性兜底（min_cut_length 等）
    if not _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
        return None
    # 返回真实 Σπ（不含新段罚），供 RMP reduced cost 判定
    true_sum_pi = sum(pi.get(ell, 0.0) for ell in parts)
    return true_sum_pi, tuple(parts)


# ---------------------------------------------------------------------------
# 切侧 pricing：格点背包 DP —— 在当前 Σ 上按 π 填满母料
# ---------------------------------------------------------------------------

def price_cut(L: int, sigma: set[int], pi: dict[int, float], kerf: int,
              max_pieces: int) -> tuple[float, dict[int, int]] | None:
    """返回 (Σπ, counts)：一根长 L 母料上使 Σπ 最大的切法。只用 Σ 里 π>0 的段。"""
    segs = sorted((s for s in sigma if pi.get(s, 0.0) > 1e-9 and s <= L), reverse=True)
    if not segs:
        return None
    NEG = float("-inf")
    # V[c][k]
    V = [[NEG] * (max_pieces + 1) for _ in range(L + 1)]
    par: list[list[tuple[int, int, int] | None]] = [[None] * (max_pieces + 1) for _ in range(L + 1)]
    V[0][0] = 0.0
    for c in range(L + 1):
        for k in range(max_pieces + 1):
            if V[c][k] == NEG:
                continue
            for s in segs:
                extra = s + (kerf if k >= 1 else 0)
                nc = c + extra
                if nc > L or k + 1 > max_pieces:
                    continue
                val = V[c][k] + pi[s]
                if val > V[nc][k + 1]:
                    V[nc][k + 1] = val
                    par[nc][k + 1] = (c, k, s)
    best = None
    for c in range(L + 1):
        for k in range(1, max_pieces + 1):
            if V[c][k] > NEG and (best is None or V[c][k] > best[0]):
                best = (V[c][k], c, k)
    if best is None:
        return None
    val, c, k = best
    counts: dict[int, int] = defaultdict(int)
    while par[c][k]:
        pc, pk, s = par[c][k]
        counts[s] += 1
        c, k = pc, pk
    return val, dict(counts)


# ---------------------------------------------------------------------------
# 收敛后富化候选池：用最终 Σ 枚举"少种类高复用"候选列，供 Phase-2 压种类挑选
#   拼侧：管长 L 的全部 1..maxseg 合法切分（段∈Σ）
#   切侧：每种母料长的密排低尾料切法（段∈Σ）
# ---------------------------------------------------------------------------

def enumerate_weld_patterns(pipe: PipeDemand, group: MaterialGroup,
                            sigma: set[int], cap: int = 4000) -> list[tuple[int, ...]]:
    """枚举管长 pipe.length 用 Σ 段拼出的全部合法切分（升序段、去重）。"""
    L = pipe.length
    max_stock = max(s.length for s in group.stocks)
    segs = sorted(s for s in sigma if 0 < s <= min(L, max_stock))
    maxseg = pipe.max_joints + 1
    results: set[tuple[int, ...]] = set()

    # DFS：按位置拼段，中间焊缝须合法（避禁区、min_weld_distance）
    def dfs(pos: int, nseg: int, path: tuple[int, ...]):
        if len(results) >= cap:
            return
        if pos == L and nseg >= 1:
            if _legal_pattern(pipe, list(path), group.min_weld_distance,
                              group.min_cut_length):
                results.add(path)
            return
        if nseg >= maxseg:
            return
        for s in segs:
            nc = pos + s
            if nc > L:
                continue
            is_first = (nseg == 0)
            is_last = (nc == L)
            if not is_first and not is_last and s < group.min_weld_distance:
                continue
            if not is_last and not pipe.weld_allowed(nc):
                continue
            dfs(nc, nseg + 1, path + (s,))

    dfs(0, 0, ())
    return sorted(results)


def enumerate_cut_columns(group: MaterialGroup, sigma: set[int], kerf: int,
                          max_pieces: int, max_trim: int = 400,
                          cap: int = 20000) -> list[tuple[int, dict[int, int]]]:
    """每种母料长枚举密排切法（段∈Σ，尾料≤max_trim，段数≤max_pieces）。"""
    stock_lens = sorted({s.length for s in group.stocks})
    segs = sorted((s for s in sigma if s > 0), reverse=True)
    cols: dict[tuple[int, tuple], int] = {}

    def rec(L: int, remain: int, pieces: int, start: int, counts: dict[int, int]):
        if len(cols) >= cap:
            return
        used = L - remain
        if pieces >= 1 and (L - used) <= max_trim:
            key = (L, tuple(sorted(counts.items())))
            cols.setdefault(key, 1)
        if pieces >= max_pieces:
            return
        for idx in range(start, len(segs)):
            s = segs[idx]
            need = s + (kerf if pieces >= 1 else 0)
            if need > remain:
                continue
            counts[s] = counts.get(s, 0) + 1
            rec(L, remain - need, pieces + 1, idx, counts)
            counts[s] -= 1
            if counts[s] == 0:
                del counts[s]

    for L in stock_lens:
        rec(L, L, 0, 0, {})
    return [(L, dict(c)) for (L, c) in cols]



# ---------------------------------------------------------------------------
# 真实可行种子：顺序构造一个"能排完"的切+拼方案（零人工变量）
# ---------------------------------------------------------------------------

def build_feasible_seed(group: MaterialGroup, max_stock: int, kerf: int, verbose=False):
    """顺序把每根管拆成段、从库存母料上切出，凑够全部需求（零人工变量）。

    返回 {"weld": [(pipe_index, parts)], "cut": [(stock_len, counts)], "used_len": int}
    段平衡天然成立：每根拼管消耗的段都由某根母料实际切出并记账。
    """
    stock_lengths = sorted({s.length for s in group.stocks}, reverse=True)
    bars_left: dict[int, int] = defaultdict(int)
    for s in group.stocks:
        bars_left[s.length] += s.quantity
    min_wd = group.min_weld_distance
    min_cut = group.min_cut_length

    all_bars: list[list] = []  # [stock_len, remaining, cut_list]
    open_bars: list[list] = []

    def open_new():
        for L in stock_lengths:
            if bars_left[L] > 0:
                bars_left[L] -= 1
                bar = [L, L, []]
                all_bars.append(bar)
                open_bars.append(bar)
                return bar
        return None

    def bar_avail(bar):
        # 可再切长度（预留段间 kerf）
        return bar[1] - (kerf if bar[2] else 0)

    def cut_from(bar, seg):
        bar[2].append(seg)
        bar[1] -= seg + (kerf if len(bar[2]) > 1 else 0)
        if bar[1] <= 0 and bar in open_bars:
            open_bars.remove(bar)

    def close(bar):
        if bar in open_bars:
            open_bars.remove(bar)

    weld: list[tuple[int, tuple[int, ...]]] = []
    for i, pipe in enumerate(group.pipes):
        for _ in range(pipe.demand):
            need = pipe.length
            parts: list[int] = []
            pos = 0
            guard = 0
            while need > 0:
                guard += 1
                if guard > 500:
                    if verbose:
                        print(f"    种子: 管型 {i} 死循环 need={need} parts={parts}")
                    return None
                # 选一根开着的、能切出合法段的母料
                bar = next((b for b in open_bars if bar_avail(b) > 0), None) or open_new()
                if bar is None:
                    if verbose:
                        print(f"    种子: 库存用尽, 管型 {i} 还差 {need}")
                    return None
                avail = bar_avail(bar)
                if avail <= 0:
                    close(bar)
                    continue
                is_first = (len(parts) == 0)
                seg = min(avail, need)
                is_last = (seg == need)
                # 段数上限：若这段还不是末段，但已用满 max_joints+1-1 段，须整段收尾
                segs_used = len(parts)
                if not is_last and segs_used + 1 >= pipe.max_joints + 1:
                    # 只剩一段配额却拼不完 -> 当前母料容量不足以整根收尾，封掉换新料
                    close(bar)
                    continue
                if not is_last:
                    fitted = _fit_seg(pipe, seg, pos, need, min_wd, min_cut, is_first)
                    if fitted is None:
                        close(bar)
                        continue
                    seg = fitted
                    is_last = (seg == need)
                # min_cut：多段拼法每段 >= min_cut
                if not (is_first and is_last) and min_cut > 0 and seg < min_cut:
                    close(bar)
                    continue
                cut_from(bar, seg)
                parts.append(seg)
                pos += seg
                need -= seg
            if not _legal_pattern(pipe, parts, min_wd, min_cut):
                if verbose:
                    print(f"    种子: 管型 {i} parts={parts} 非法")
                return None
            weld.append((i, tuple(parts)))

    cut_agg: dict[tuple[int, tuple], int] = defaultdict(int)
    used_len = 0
    for L, _rem, cut_list in all_bars:
        if not cut_list:
            continue
        used_len += L
        key = (L, tuple(sorted(cut_list)))
        cut_agg[key] += 1
    cut_cols = [(L, dict(_counts(parts))) for (L, parts) in cut_agg]
    return {"weld": weld, "cut": cut_cols, "used_len": used_len}


def _counts(parts):
    d = defaultdict(int)
    for p in parts:
        d[p] += 1
    return d


def _fit_seg(pipe, seg, pos, need, min_wd, min_cut, is_first):
    """调整 seg 使焊缝位置 pos+seg 合法且剩余能收尾。返回段长或 None。"""
    lo = max(min_cut if min_cut > 0 else 1, min_wd if not is_first else 1)
    s = seg
    while s >= lo:
        rem = need - s
        wpos = pos + s
        if rem == 0:
            return s  # 末段无内部焊缝
        if rem >= max(min_wd, min_cut, 1) and pipe.weld_allowed(wpos):
            return s
        s -= 1
    return None

def solve_colgen(group: MaterialGroup, time_limit: float, max_iters: int, verbose=True,
                 new_seg_penalty: float = 0.0):
    from pyscipopt import Model, quicksum

    max_stock = max(s.length for s in group.stocks)
    stock_lengths = sorted({s.length for s in group.stocks})
    bars_by_len: dict[int, int] = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity
    kerf = group.blade_margin
    smallest_pipe = min(p.length for p in group.pipes)
    max_pieces = min(30, max_stock // max(1, min(smallest_pipe, group.min_weld_distance or smallest_pipe)) + 2)

    # ---- 真实可行种子（零人工变量）：顺序 DP 把管拆段塞满母料 ----
    # 目标：构造一组切列+拼列，使需求全满足、段平衡成立，作为 LP 的可行起点。
    sigma: set[int] = set()
    weld_cols: list[tuple[int, tuple[int, ...]]] = []
    cut_cols: list[tuple[int, dict[int, int]]] = []
    seen_cut: set[tuple[int, tuple]] = set()

    def add_cut(L: int, counts: dict[int, int]):
        key = (L, tuple(sorted(counts.items())))
        if key in seen_cut or not counts:
            return
        seen_cut.add(key)
        cut_cols.append((L, dict(counts)))

    seed = build_feasible_seed(group, max_stock, kerf, verbose)
    if seed is None:
        if verbose:
            print("  种子构造失败：无法在库存内排完（问题本身可能不可行）")
        return None
    for (pi_idx, parts) in seed["weld"]:
        weld_cols.append((pi_idx, parts))
        sigma.update(parts)
    for (L, counts) in seed["cut"]:
        add_cut(L, counts)
    if verbose:
        print(f"  可行种子: weldcols={len(weld_cols)} cutcols={len(cut_cols)} |Σ|={len(sigma)} "
              f"用料={seed['used_len']} util={group.demand_length/seed['used_len']:.4f}")

    started = time.monotonic()
    it = 0
    while it < max_iters and time.monotonic() - started < time_limit:
        it += 1
        # ---- 解 RMP LP（连续松弛，== 段平衡） ----
        m = Model("rmp")
        m.hideOutput()
        m.setParam("limits/time", 20)
        m.setPresolve(0)
        m.setParam("presolving/maxrounds", 0)
        m.setParam("propagating/maxrounds", 0)
        m.setParam("propagating/maxroundsroot", 0)
        m.setParam("lp/disablecutoff", 1)
        u = {}
        dem_cons = {}
        for wi, (pi_idx, parts) in enumerate(weld_cols):
            u[wi] = m.addVar(vtype="C", lb=0, name=f"u{wi}")
        # 无人工变量：可行种子已保证 RMP 恒可行，对偶干净。
        for i, pipe in enumerate(group.pipes):
            cons = m.addCons(
                quicksum(u[wi] for wi, (pidx, _) in enumerate(weld_cols) if pidx == i)
                == pipe.demand, name=f"dem{i}")
            dem_cons[i] = cons
        x = {}
        for ci in range(len(cut_cols)):
            x[ci] = m.addVar(vtype="C", lb=0, name=f"x{ci}")
        # 段平衡 ==（取对偶用）
        seg_cons = {}
        segs_all = set(sigma)
        prod = defaultdict(list)
        cons_seg = defaultdict(list)
        for ci, (L, counts) in enumerate(cut_cols):
            for s, c in counts.items():
                prod[s].append((c, x[ci]))
        for wi, (pidx, parts) in enumerate(weld_cols):
            cnt = defaultdict(int)
            for s in parts:
                cnt[s] += 1
            for s, c in cnt.items():
                cons_seg[s].append((c, u[wi]))
        for s in segs_all:
            seg_cons[s] = m.addCons(
                quicksum(c * v for c, v in prod.get(s, []))
                - quicksum(c * v for c, v in cons_seg.get(s, [])) == 0, name=f"bal{s}")
        # 母料预算
        len_cons = {}
        cols_by_len = defaultdict(list)
        for ci, (L, _) in enumerate(cut_cols):
            cols_by_len[L].append(ci)
        for L, plist in cols_by_len.items():
            len_cons[L] = m.addCons(quicksum(x[ci] for ci in plist) <= bars_by_len[L], name=f"cap{L}")
        m.setObjective(
            quicksum(cut_cols[ci][0] * x[ci] for ci in range(len(cut_cols))), "minimize")
        m.optimize()
        if m.getNSols() == 0 or str(m.getStatus()).upper() not in ("OPTIMAL", "TIMELIMIT"):
            if verbose:
                print(f"  iter {it}: RMP LP status={m.getStatus()} 无解")
            return None
        obj = m.getObjVal()
        real_len = sum(cut_cols[ci][0] * m.getVal(x[ci]) for ci in range(len(cut_cols)))
        art_used = 0.0
        # 取对偶
        delta = {i: m.getDualsolLinear(dem_cons[i]) for i in dem_cons}
        pi = {}
        for s, cons in seg_cons.items():
            try:
                pi[s] = m.getDualsolLinear(cons)
            except Exception:
                pi[s] = 0.0
        sigma_dual = {}
        for L, cons in len_cons.items():
            try:
                sigma_dual[L] = m.getDualsolLinear(cons)
            except Exception:
                sigma_dual[L] = 0.0

        added = 0
        new_segs: set[int] = set()
        # A2 新段罚：以当前 π 规模为基准（π≈段的边际价值/长度），逼优先复用 Σ。
        pi_ref = max((abs(v) for v in pi.values()), default=0.0)
        lam = new_seg_penalty * pi_ref
        # ---- 拼侧 pricing ----
        for i, pipe in enumerate(group.pipes):
            got = price_weld(pipe, group, pi, sigma, max_stock, new_seg_penalty=lam)
            if got is None:
                continue
            sum_pi, parts = got
            rc = -delta.get(i, 0.0) + sum_pi
            if rc < -1e-6:
                if (i, parts) not in {(w[0], w[1]) for w in weld_cols}:
                    weld_cols.append((i, parts))
                    for s in parts:
                        if s not in sigma:
                            new_segs.add(s)
                        sigma.add(s)
                    added += 1
        # 新段进 Σ 后：立刻生成"把新段与其它 Σ 段一起塞满母料"的高质量切列
        # （而非 {新段:1} 垃圾种子——那会一根母料只切一段，浪费巨大，LP 不肯用）
        for s in new_segs:
            L = min((x2 for x2 in stock_lengths if x2 >= s), default=max_stock)
            counts = _greedy_pack(L, s, sigma, kerf, max_pieces)
            add_cut(L, counts)
        # ---- 切侧 pricing ----
        for L in stock_lengths:
            got = price_cut(L, sigma, pi, kerf, max_pieces)
            if got is None:
                continue
            val, counts = got
            rc = L - val - sigma_dual.get(L, 0.0)
            if rc < -1e-6:
                key = (L, tuple(sorted(counts.items())))
                if key not in seen_cut:
                    add_cut(L, counts)
                    added += 1
        pi_max = max(pi.values()) if pi else 0
        if verbose:
            util_s = group.demand_length / real_len if real_len > 1e-9 else 0
            print(f"  iter {it}: real_len={real_len:.0f} util={util_s:.4f} art={art_used:.1f} "
                  f"|Σ|={len(sigma)} weldcols={len(weld_cols)} cutcols={len(cut_cols)} "
                  f"added={added} pi_max={pi_max:.1f} new_segs={len(new_segs)}")
        if added == 0:
            if verbose:
                print(f"  收敛于 iter {it} (art={art_used:.1f})")
            break

    return {"sigma": sorted(sigma), "weld_cols": weld_cols, "cut_cols": cut_cols,
            "lp_obj": real_len, "art": art_used, "iters": it}


def _build_base_int(group, weld_cols, cut_cols, sigma, bars_by_len,
                    weld_idx, cut_idx, time_limit):
    """构建整数 RMP 基础模型（限定在给定列子集上）。返回 (model, u, x)。"""
    from pyscipopt import Model, quicksum
    m = Model("int")
    m.hideOutput()
    m.setParam("limits/time", time_limit)
    u = {wi: m.addVar(vtype="I", lb=0, name=f"u{wi}") for wi in weld_idx}
    x = {ci: m.addVar(vtype="I", lb=0, name=f"x{ci}") for ci in cut_idx}
    # 需求
    weld_by_pipe = defaultdict(list)
    for wi in weld_idx:
        weld_by_pipe[weld_cols[wi][0]].append(wi)
    for i, pipe in enumerate(group.pipes):
        m.addCons(quicksum(u[wi] for wi in weld_by_pipe.get(i, [])) == pipe.demand)
    # 段平衡 >=（多切段视为尾料）
    prod = defaultdict(list)
    cons_seg = defaultdict(list)
    for ci in cut_idx:
        for s, c in cut_cols[ci][1].items():
            prod[s].append((c, x[ci]))
    for wi in weld_idx:
        cnt = defaultdict(int)
        for s in weld_cols[wi][1]:
            cnt[s] += 1
        for s, c in cnt.items():
            cons_seg[s].append((c, u[wi]))
    for s in sigma:
        m.addCons(quicksum(c * v for c, v in prod.get(s, []))
                  - quicksum(c * v for c, v in cons_seg.get(s, [])) >= 0)
    # 母料预算
    cols_by_len = defaultdict(list)
    for ci in cut_idx:
        cols_by_len[cut_cols[ci][0]].append(ci)
    for L, plist in cols_by_len.items():
        m.addCons(quicksum(x[ci] for ci in plist) <= bars_by_len[L])
    return m, u, x


def _metrics(group, weld_cols, cut_cols, u, x):
    used_cut = {ci for ci in x if x[ci] is not None and _val(x[ci]) > 0.5}
    used_weld = {wi for wi in u if _val(u[wi]) > 0.5}
    used_len = sum(cut_cols[ci][0] * round(_val(x[ci])) for ci in used_cut)
    cut_types = len({(cut_cols[ci][0], tuple(sorted(cut_cols[ci][1].items()))) for ci in used_cut})
    weld_types = len({(weld_cols[wi][0], weld_cols[wi][1]) for wi in used_weld})
    return used_cut, used_weld, used_len, cut_types, weld_types


_MODEL_REF = {}


def _val(var):
    return _MODEL_REF["m"].getVal(var)


# ---------------------------------------------------------------------------
# A2 weld-first：先选极少数拼法锁定小字母表，再在其上纯切 CSP
# ---------------------------------------------------------------------------

def solve_weld_first(group, kerf, time_limit=120, seg_cap=3000, verbose=True):
    """老软件真实结构：先选每管型 ≤k 条拼法（最小化启用段型），锁定小字母表；
    再枚举该字母表上的密排切法，联合整数求解（利用率 + 种类词典序）。

    返回 dict(cut_types, weld_types, util, used_len, sigma) 或 None。
    """
    from pyscipopt import Model, quicksum
    max_stock = max(s.length for s in group.stocks)
    bars_by_len = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity

    # 1) 每管型枚举合法拼法 + 收集全部候选段
    pipe_pats: list[list[tuple[int, ...]]] = []
    all_segs: set[int] = set()
    for pipe in group.pipes:
        pats = enumerate_weld_patterns(pipe, group, _all_split_segs(pipe, group, max_stock),
                                       cap=seg_cap)
        if not pats:
            if verbose:
                print(f"  拼法枚举失败: 管型 len={pipe.length}")
            return None
        pipe_pats.append(pats)
        for p in pats:
            all_segs.update(p)
    if verbose:
        print(f"  拼法候选: {[len(p) for p in pipe_pats]} 候选段总数={len(all_segs)}")

    # 2) 字母表最小化 ILP：选拼法使每管型有解且启用段型最少
    m = Model("alpha")
    m.hideOutput()
    m.setParam("limits/time", time_limit)
    # y[i][j] 管型 i 选拼法 j；t[s] 段 s 是否启用
    y = {}
    for i, pats in enumerate(pipe_pats):
        for j in range(len(pats)):
            y[(i, j)] = m.addVar(vtype="B", name=f"y{i}_{j}")
    t = {s: m.addVar(vtype="B", name=f"t{s}") for s in all_segs}
    # 每管型至少选 1 条拼法（可多条，但压段型会自然收敛到少数）
    for i, pats in enumerate(pipe_pats):
        m.addCons(quicksum(y[(i, j)] for j in range(len(pats))) >= 1)
    # 选了拼法 → 其所有段必须启用
    for i, pats in enumerate(pipe_pats):
        for j, p in enumerate(pats):
            for s in set(p):
                m.addCons(t[s] >= y[(i, j)])
    # 目标：最小化启用段型（主）+ 拼法条数（次，弱权重）
    nseg = len(all_segs)
    m.setObjective(quicksum(t[s] for s in all_segs) * (nseg + 1)
                   + quicksum(y[(i, j)] for i in range(len(pipe_pats))
                              for j in range(len(pipe_pats[i]))), "minimize")
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print("  字母表 ILP 无解")
        return None
    sigma = {s for s in all_segs if m.getVal(t[s]) > 0.5}
    # 每管型选中的拼法
    weld_cols = []
    for i, pats in enumerate(pipe_pats):
        for j, p in enumerate(pats):
            if m.getVal(y[(i, j)]) > 0.5:
                weld_cols.append((i, p))
    if verbose:
        print(f"  锁定字母表 |Σ'|={len(sigma)}: {sorted(sigma)}")
        print(f"  选中拼法 {len(weld_cols)} 条")

    # 3) 在小字母表上枚举密排切法
    max_pieces = min(40, max_stock // max(1, min(sigma)) + 2)
    cut_cols = enumerate_cut_columns(group, sigma, kerf, max_pieces,
                                     max_trim=max(600, group.min_cut_length),
                                     cap=40000)
    if verbose:
        print(f"  切法候选 {len(cut_cols)} 条（max_pieces={max_pieces}）")
    if not cut_cols:
        return None

    # 4) 联合整数求解（词典序：先利用率、再种类）
    return solve_integer(group, weld_cols, cut_cols, sigma, kerf,
                         time_limit=time_limit, verbose=verbose)


def _all_split_segs(pipe, group, max_stock):
    """weld-first 的候选段集：使拼法枚举能覆盖"能密排母料"的段。

    段的价值=既能合法拼成整管、又能高密度平铺母料。因此候选段来自：
      (a) 母料填充收尾 tail 及等分（k 整管后）；
      (b) 母料近似均分 S/n（平铺母料 → 尾料小）；
      (c) 上述的管长互补 P⊖a。
    """
    P = pipe.length
    stock_lens = sorted({s.length for s in group.stocks})
    segs: set[int] = set()
    if P <= max_stock:
        segs.add(P)
    segs |= _bar_fill_lengths(P, stock_lens, max_stock)
    # 母料近似均分（平铺）：S//n，n=2..S//min_cut
    min_cut = max(group.min_cut_length, 1)
    for S in stock_lens:
        nmax = min(60, S // max(min_cut, 1))
        for n in range(2, nmax + 1):
            q = S // n
            if 0 < q <= min(P, max_stock):
                segs.add(q)
    # 互补
    for a in list(segs):
        r = P - a
        if 0 < r <= max_stock:
            segs.add(r)
    return {s for s in segs if 0 < s <= min(P, max_stock)}


def solve_integer(group, weld_cols, cut_cols, sigma, kerf, time_limit=60, verbose=True):
    """两阶段词典序：Phase-1 最小化用料 → Phase-2 固定用料上界、最小化切种类→拼种类。

    Phase-2 只在 Phase-1 的活跃列上跑（big-M 种类最小化不可套全池，§11.28/11.29）。
    """
    bars_by_len = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity
    all_weld = list(range(len(weld_cols)))
    all_cut = list(range(len(cut_cols)))

    # ---- Phase 1：全池最小化用料 ----
    m1, u1, x1 = _build_base_int(group, weld_cols, cut_cols, sigma, bars_by_len,
                                 all_weld, all_cut, time_limit)
    from pyscipopt import quicksum
    m1.setObjective(quicksum(cut_cols[ci][0] * x1[ci] for ci in all_cut), "minimize")
    m1.optimize()
    if m1.getNSols() == 0:
        return None
    _MODEL_REF["m"] = m1
    ucut1, uweld1, used_len1, ct1, wt1 = _metrics(group, weld_cols, cut_cols, u1, x1)
    if verbose:
        print(f"  Phase1(最小用料): util={group.demand_length/used_len1:.4f} "
              f"cut_types={ct1} weld_types={wt1} 活跃列 cut={len(ucut1)} weld={len(uweld1)}")

    # ---- Phase 2：贪心种类压缩（无 big-M，可规模化）----
    target_len = int(used_len1 * 1.001)  # 允许 0.1% slack 换取更少种类
    con = consolidate_types(group, weld_cols, cut_cols, sigma, target_len,
                            seed_cut=sorted(ucut1), seed_weld=sorted(uweld1))
    if con is None:
        return {"cut_types": ct1, "weld_types": wt1, "used_len": used_len1,
                "util": group.demand_length / used_len1, "phase": 1,
                "status": str(m1.getStatus())}
    # 用压缩后的列子集解最终整数解，读取真实指标
    m2, u2, x2 = _build_base_int(group, weld_cols, cut_cols, sigma, bars_by_len,
                                 con["sel_weld"], con["sel_cut"], time_limit)
    m2.addCons(quicksum(cut_cols[ci][0] * x2[ci] for ci in con["sel_cut"]) <= target_len)
    m2.setObjective(quicksum(cut_cols[ci][0] * x2[ci] for ci in con["sel_cut"]), "minimize")
    m2.optimize()
    if m2.getNSols() == 0:
        return {"cut_types": ct1, "weld_types": wt1, "used_len": used_len1,
                "util": group.demand_length / used_len1, "phase": 1,
                "status": str(m1.getStatus())}
    _MODEL_REF["m"] = m2
    ucut2, uweld2, used_len2, ct2, wt2 = _metrics(group, weld_cols, cut_cols, u2, x2)
    return {"cut_types": ct2, "weld_types": wt2, "used_len": used_len2,
            "util": group.demand_length / used_len2 if used_len2 else 0,
            "phase": 2, "status": str(m2.getStatus())}


def consolidate_types(group, weld_cols, cut_cols, sigma, target_len,
                      seed_cut, seed_weld, time_each=10):
    """种类压缩（无 big-M）：从 Phase-1 活跃列（已知可行）出发，贪心删冗余列。

    每步试删一条列，若剩余仍整数可行（用料≤target_len）则永久删除。
    先删拼列（压拼种类，主目标之一），再删切列。保证不劣于起点。
    """
    from pyscipopt import Model, quicksum
    bars_by_len = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity

    def feasible(sel_cut, sel_weld):
        m = Model("sub")
        m.hideOutput()
        m.setParam("limits/time", time_each)
        u = {wi: m.addVar(vtype="I", lb=0) for wi in sel_weld}
        x = {ci: m.addVar(vtype="I", lb=0) for ci in sel_cut}
        wbp = defaultdict(list)
        for wi in sel_weld:
            wbp[weld_cols[wi][0]].append(wi)
        for i, pipe in enumerate(group.pipes):
            m.addCons(quicksum(u[wi] for wi in wbp.get(i, [])) == pipe.demand)
        prod = defaultdict(list)
        cons = defaultdict(list)
        for ci in sel_cut:
            for s, c in cut_cols[ci][1].items():
                prod[s].append((c, x[ci]))
        for wi in sel_weld:
            cnt = defaultdict(int)
            for s in weld_cols[wi][1]:
                cnt[s] += 1
            for s, c in cnt.items():
                cons[s].append((c, u[wi]))
        for s in sigma:
            m.addCons(quicksum(c * v for c, v in prod.get(s, []))
                      - quicksum(c * v for c, v in cons.get(s, [])) >= 0)
        cbl = defaultdict(list)
        for ci in sel_cut:
            cbl[cut_cols[ci][0]].append(ci)
        for L, pl in cbl.items():
            m.addCons(quicksum(x[ci] for ci in pl) <= bars_by_len[L])
        m.addCons(quicksum(cut_cols[ci][0] * x[ci] for ci in sel_cut) <= target_len)
        m.setObjective(quicksum(cut_cols[ci][0] * x[ci] for ci in sel_cut), "minimize")
        m.optimize()
        return m.getNSols() > 0

    sel_cut = list(seed_cut)
    sel_weld = list(seed_weld)
    if not feasible(sel_cut, sel_weld):
        return None
    # 删冗余拼列（低 demand 覆盖优先试删）
    changed = True
    while changed:
        changed = False
        for wi in sorted(sel_weld, key=lambda w: len(weld_cols[w][1])):
            trial = [w for w in sel_weld if w != wi]
            if not trial:
                continue
            if feasible(sel_cut, trial):
                sel_weld = trial
                changed = True
                break
    # 删冗余切列
    changed = True
    while changed:
        changed = False
        for ci in sorted(sel_cut, key=lambda c: -sum(cut_cols[c][1].values())):
            trial = [c for c in sel_cut if c != ci]
            if not trial:
                continue
            if feasible(trial, sel_weld):
                sel_cut = trial
                changed = True
                break

    # ---- B：LNS swap（删2换1，跳出"只删不换"局部最优）----
    # 全 MIP 每候选不可行（O(n²·池)次求解）。改：LP 松弛快筛 + 有界搜索：
    #   仅试删"用量最低的若干列对"，替换列仅限"产/覆盖被删段/管型"者，最后整数复核。
    all_cut_pool = list(range(len(cut_cols)))
    all_weld_pool = list(range(len(weld_cols)))

    def feasible_lp(sel_cut, sel_weld):
        m = Model("lp")
        m.hideOutput()
        m.setParam("limits/time", time_each)
        u = {wi: m.addVar(vtype="C", lb=0) for wi in sel_weld}
        x = {ci: m.addVar(vtype="C", lb=0) for ci in sel_cut}
        wbp = defaultdict(list)
        for wi in sel_weld:
            wbp[weld_cols[wi][0]].append(wi)
        for i, pipe in enumerate(group.pipes):
            m.addCons(quicksum(u[wi] for wi in wbp.get(i, [])) == pipe.demand)
        prod = defaultdict(list)
        cons = defaultdict(list)
        for ci in sel_cut:
            for s, c in cut_cols[ci][1].items():
                prod[s].append((c, x[ci]))
        for wi in sel_weld:
            cnt = defaultdict(int)
            for s in weld_cols[wi][1]:
                cnt[s] += 1
            for s, c in cnt.items():
                cons[s].append((c, u[wi]))
        for s in sigma:
            m.addCons(quicksum(c * v for c, v in prod.get(s, []))
                      - quicksum(c * v for c, v in cons.get(s, [])) >= 0)
        cbl = defaultdict(list)
        for ci in sel_cut:
            cbl[cut_cols[ci][0]].append(ci)
        for L, pl in cbl.items():
            m.addCons(quicksum(x[ci] for ci in pl) <= bars_by_len[L])
        m.addCons(quicksum(cut_cols[ci][0] * x[ci] for ci in sel_cut) <= target_len)
        m.setObjective(0, "minimize")
        m.optimize()
        return m.getStatus() not in ("infeasible", "unknown")

    def swap_reduce(sel, other, pool, is_cut, max_pairs=8, max_repl=40):
        """删2换1：仅试删用量最低的 max_pairs² 对，替换列限产/覆盖被删项者。
        LP 快筛通过后再整数复核，避免全池 MIP 爆炸。"""
        if len(sel) < 3:
            return None
        sel_set = set(sel)

        def freed_items(idxs):
            it = set()
            if is_cut:
                for ci in idxs:
                    it.update(cut_cols[ci][1].keys())      # 产出段
            else:
                for wi in idxs:
                    it.add(weld_cols[wi][0])               # 覆盖管型
            return it

        def repl_score(r, need):
            if is_cut:
                return len(set(cut_cols[r][1].keys()) & need)
            return int(weld_cols[r][0] in need)

        # 用量最低者优先删（LP 复用不到者）
        if is_cut:
            low = sorted(sel, key=lambda c: sum(cut_cols[c][1].values()))[:max_pairs]
        else:
            low = sorted(sel, key=lambda w: len(weld_cols[w][1]))[:max_pairs]

        for i in range(len(low)):
            for j in range(i + 1, len(low)):
                rm = [low[i], low[j]]
                keep = [c for c in sel if c not in rm]
                # 删2直接可行 → 净减2
                ok = feasible_lp(keep, other) if is_cut else feasible_lp(other, keep)
                if ok and (feasible(keep, other) if is_cut else feasible(other, keep)):
                    return keep
                # 删2补1：替换列限"覆盖被删项"者，取交集最大的前 max_repl
                need = freed_items(rm)
                cands = sorted((r for r in pool if r not in sel_set),
                               key=lambda r: -repl_score(r, need))
                cands = [r for r in cands if repl_score(r, need) > 0][:max_repl]
                for r in cands:
                    trial = keep + [r]
                    ok = feasible_lp(trial, other) if is_cut else feasible_lp(other, trial)
                    if not ok:
                        continue
                    if feasible(trial, other) if is_cut else feasible(other, trial):
                        return trial
        return None

    improved = True
    rounds = 0
    while improved and rounds < 6:
        improved = False
        rounds += 1
        new_cut = swap_reduce(sel_cut, sel_weld, all_cut_pool, is_cut=True)
        if new_cut is not None and len(new_cut) < len(sel_cut):
            sel_cut = new_cut
            improved = True
        new_weld = swap_reduce(sel_weld, sel_cut, all_weld_pool, is_cut=False)
        if new_weld is not None and len(new_weld) < len(sel_weld):
            sel_weld = new_weld
            improved = True

    return {"sel_cut": sel_cut, "sel_weld": sel_weld}


def _ctypes(idxs, cut_cols):
    return [(cut_cols[c][0], tuple(sorted(cut_cols[c][1].items()))) for c in idxs]


def _wtypes(idxs, weld_cols):
    return [(weld_cols[w][0], weld_cols[w][1]) for w in idxs]


def _consolidate_types_greedy(group, weld_cols, cut_cols, sigma, target_len, time_each=10):
    from pyscipopt import Model, quicksum
    bars_by_len = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity

    def try_solve(sel_cut, sel_weld, integer):
        m = Model("sub")
        m.hideOutput()
        m.setParam("limits/time", time_each)
        vt = "I" if integer else "C"
        u = {wi: m.addVar(vtype=vt, lb=0) for wi in sel_weld}
        x = {ci: m.addVar(vtype=vt, lb=0) for ci in sel_cut}
        wbp = defaultdict(list)
        for wi in sel_weld:
            wbp[weld_cols[wi][0]].append(wi)
        for i, pipe in enumerate(group.pipes):
            m.addCons(quicksum(u[wi] for wi in wbp.get(i, [])) == pipe.demand)
        prod = defaultdict(list)
        cons = defaultdict(list)
        for ci in sel_cut:
            for s, c in cut_cols[ci][1].items():
                prod[s].append((c, x[ci]))
        for wi in sel_weld:
            cnt = defaultdict(int)
            for s in weld_cols[wi][1]:
                cnt[s] += 1
            for s, c in cnt.items():
                cons[s].append((c, u[wi]))
        for s in sigma:
            m.addCons(quicksum(c * v for c, v in prod.get(s, []))
                      - quicksum(c * v for c, v in cons.get(s, [])) >= 0)
        cbl = defaultdict(list)
        for ci in sel_cut:
            cbl[cut_cols[ci][0]].append(ci)
        for L, pl in cbl.items():
            m.addCons(quicksum(x[ci] for ci in pl) <= bars_by_len[L])
        m.addCons(quicksum(cut_cols[ci][0] * x[ci] for ci in sel_cut) <= target_len)
        m.setObjective(quicksum(cut_cols[ci][0] * x[ci] for ci in sel_cut), "minimize")
        m.optimize()
        if m.getNSols() == 0:
            return None
        return m.getStatus()

    # 初始：按覆盖力排序的 weld/cut 列（demand 覆盖、段复用高者优先）
    order_weld = sorted(range(len(weld_cols)),
                        key=lambda wi: -group.pipes[weld_cols[wi][0]].demand)
    order_cut = sorted(range(len(cut_cols)),
                       key=lambda ci: -sum(cut_cols[ci][1].values()))
    sel_weld: list[int] = []
    sel_cut: list[int] = []
    wi_iter = iter(order_weld)
    ci_iter = iter(order_cut)
    # 先加足够覆盖所有管型的 weld 列（每个管型至少 1 列，优先高 demand）
    pipes_covered = set()
    for wi in order_weld:
        pi = weld_cols[wi][0]
        if pi not in pipes_covered:
            sel_weld.append(wi)
            pipes_covered.add(pi)
    # 选中 weld 列需要的段集合 → 必须有 cut 列产出这些段
    need_segs = set()
    for wi in sel_weld:
        need_segs.update(weld_cols[wi][1])
    # 段感知地加 cut 列：优先能覆盖最多未满足段的列
    covered_segs = set()
    remaining_cut = list(order_cut)
    while covered_segs < need_segs and remaining_cut:
        best_ci = max(remaining_cut,
                      key=lambda ci: len((set(cut_cols[ci][1]) & need_segs) - covered_segs))
        gain = (set(cut_cols[best_ci][1]) & need_segs) - covered_segs
        remaining_cut.remove(best_ci)
        if not gain:
            break
        sel_cut.append(best_ci)
        covered_segs |= gain
    # 再逐步加 cut 列直到整数可行（数量/预算可行）
    for ci in order_cut:
        if ci in sel_cut:
            continue
        if try_solve(sel_cut, sel_weld, integer=True) is not None:
            break
        sel_cut.append(ci)
    if try_solve(sel_cut, sel_weld, integer=True) is None:
        # 单一拼法+段感知切列仍不可行：逐步增拼法列（放宽到 2~3 种拼法）
        for wi in order_weld:
            if wi in sel_weld:
                continue
            sel_weld.append(wi)
            need_segs.update(weld_cols[wi][1])
            for ci in order_cut:
                if ci in sel_cut:
                    continue
                if try_solve(sel_cut, sel_weld, integer=True) is not None:
                    break
                if set(cut_cols[ci][1]) & set(weld_cols[wi][1]):
                    sel_cut.append(ci)
            if try_solve(sel_cut, sel_weld, integer=True) is not None:
                break
        if try_solve(sel_cut, sel_weld, integer=True) is None:
            return None
    # 尝试删冗余 weld 列（若删掉仍可行则删，压拼种类）
    changed = True
    while changed:
        changed = False
        for wi in list(sel_weld):
            trial = [w for w in sel_weld if w != wi]
            # 至少保证每管型有列
            if {weld_cols[w][0] for w in trial} != pipes_covered:
                continue
            if try_solve(sel_cut, trial, integer=True) is not None:
                sel_weld = trial
                changed = True
                break
    # 删冗余 cut 列
    changed = True
    while changed:
        changed = False
        for ci in list(sel_cut):
            trial = [c for c in sel_cut if c != ci]
            if try_solve(trial, sel_weld, integer=True) is not None:
                sel_cut = trial
                changed = True
                break
    return {"sel_cut": sel_cut, "sel_weld": sel_weld}


def _greedy_pack(L: int, required: int, sigma: set[int], kerf: int, max_pieces: int) -> dict[int, int]:
    """把长 L 母料塞满：先放 required 段，再用 Σ 里的段 best-fit 填剩余。返回段计数。"""
    counts: dict[int, int] = defaultdict(int)
    counts[required] += 1
    used = required
    pieces = 1
    segs = sorted((x for x in sigma if x <= L), reverse=True)
    while pieces < max_pieces:
        rem = L - used - (kerf if pieces >= 1 else 0)
        best = next((x for x in segs if x <= rem), None)
        if best is None:
            break
        counts[best] += 1
        used += best + kerf
        pieces += 1
    return dict(counts)


def legacy_alpha_and_metrics(sample):
    res = sample.get("MOMRESULTJSON") or sample.get("legacy")
    if isinstance(res, str):
        res = json.loads(res)
    gi = res.get("GeneralInfo", {})
    R = res.get("Result", {})
    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    wp = (R.get("WeldingPattern") or {}).get("WeldingPipe") or []

    def parts(s):
        return [int(float(t)) for t in str(s).split() if t.strip()]
    seg = set()
    for c in cp:
        seg.update(parts(c.get("Part", "")))
    pipe_set = set()
    stock_set = set()
    ct = len({(int(float(c.get("Length"))), tuple(sorted(parts(c.get("Part", ""))))) for c in cp})
    # 拼法种类按 parts 物理去重（同一切分被多图号复用不重复计），对齐合并管型口径
    wt = len({tuple(parts(p.get("Part", "")))
              for w in wp for p in w.get("Pattern", [])
              if len(parts(p.get("Part", ""))) >= 2})
    total_joints = 0
    for w in wp:
        for pat in w.get("Pattern", []):
            nseg = len(parts(pat.get("Part", "")))
            num = int(float(pat.get("Number", 1) or 1))
            if nseg >= 1:
                total_joints += (nseg - 1) * num
    joints = total_joints if wp else None
    return sorted(seg), {"util": float(gi.get("UtilRate", 0)), "cut_types": ct, "weld_types": wt, "joints": joints}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    path = sys.argv[1]
    pref = sys.argv[2]
    tl = float(sys.argv[sys.argv.index("--tl") + 1]) if "--tl" in sys.argv else 60.0
    iters = int(sys.argv[sys.argv.index("--iters") + 1]) if "--iters" in sys.argv else 60
    lam = float(sys.argv[sys.argv.index("--lam") + 1]) if "--lam" in sys.argv else 0.0
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    samples = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
    for s in samples:
        sid = (s.get("id") or s.get("ID") or "")
        if not sid.startswith(pref):
            continue
        prob = json.loads(s["MOMPROBLEMJSON"]) if "MOMPROBLEMJSON" in s else s.get("problem")
        problem = parse_problem(prob)
        group = problem.groups[0]
        n_before = len(group.pipes)
        group = merge_equivalent_pipes(group)
        oracle_seg, lm = legacy_alpha_and_metrics(s)
        print(f"id={sid[:12]} 管长={sorted({p.length for p in group.pipes})} "
              f"定尺={sorted({st.length for st in group.stocks})}")
        print(f"  管型合并: {n_before} -> {len(group.pipes)}（物理等价）")
        print(f"  老软件: util={lm['util']:.4f} cut_types={lm['cut_types']} weld_types={lm['weld_types']}")
        print(f"  老软件字母表({len(oracle_seg)}): {oracle_seg}")
        t0 = time.monotonic()
        out = solve_colgen(group, tl, iters, new_seg_penalty=lam)
        if out is None:
            print("  列生成失败")
            return
        used_len = out["lp_obj"]
        print(f"  列生成完成: {time.monotonic()-t0:.1f}s iters={out['iters']} "
              f"LP util={group.demand_length/used_len:.4f} |Σ|={len(out['sigma'])}")
        # A1 可行性诊断：老软件字母表有几个落在我们的 Σ 里
        sig_set = set(out["sigma"])
        hit = [x for x in oracle_seg if x in sig_set]
        print(f"  [A1诊断] Σ含老软件段 {len(hit)}/{len(oracle_seg)}: 命中={hit} "
              f"缺失={[x for x in oracle_seg if x not in sig_set]}")
        # 整数解 + 种类量化（真正的验收口径）
        kerf = group.blade_margin
        intr = solve_integer(group, out["weld_cols"], out["cut_cols"],
                             set(out["sigma"]), kerf, time_limit=60)
        if intr is None:
            print("  整数解: 无解/超时")
            return
        def cmp(new, old, lower_better=True):
            if abs(new - old) < 1e-9:
                return "[=]"
            better = (new < old) if lower_better else (new > old)
            return "[BETTER]" if better else "[WORSE]"
        print("  ── 整数解 vs 老软件（验收口径）──")
        print(f"    (Phase{intr.get('phase','?')})")
        print(f"    利用率:  {intr['util']:.4f} vs {lm['util']:.4f}  "
              f"{cmp(intr['util'], lm['util'], lower_better=False)}")
        print(f"    切法种类: {intr['cut_types']} vs {lm['cut_types']}  "
              f"{cmp(intr['cut_types'], lm['cut_types'])}")
        print(f"    拼法种类: {intr['weld_types']} vs {lm['weld_types']}  "
              f"{cmp(intr['weld_types'], lm['weld_types'])}")
        return
    print("未找到样本")


if __name__ == "__main__":
    main()
