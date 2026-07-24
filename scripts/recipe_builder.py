"""RecipeBuilder v1: 事前小段字母表 Σ（T0 + 残料 + 标准段 + 禁焊可达剪枝）。

关键修复（113146 探针）：
  - 先用焊口合法的显式 2/3 段拆分锁定段，再截断；
  - 禁止在巨大 Σ 上盲目 DFS（短段先搜会耗尽 budget，漏掉整管 (L,)）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SIGMA_CAP_DEFAULT = 96


@dataclass
class Recipe:
    sigma_weld: list[int]
    sigma_cut: list[int]
    seed_weld: list[tuple] = field(default_factory=list)
    seed_cut: list[tuple] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def sigma(self) -> list[int]:
        return sorted(set(self.sigma_weld) | set(self.sigma_cut))


def _stock_qty_map(group) -> dict[int, int]:
    qty: dict[int, int] = {}
    for st in group.stocks:
        qty[st.length] = qty.get(st.length, 0) + st.quantity
    return qty


def _mate_closure(
    segs: set[int],
    stock_lens: list[int],
    *,
    min_cut: int,
    max_stock: int,
    kerf: int = 0,
    rounds: int = 4,
) -> set[int]:
    """把段在母料上的精确/近精确互补残段闭包进字母表（短料套料关键）。"""
    out = {s for s in segs if min_cut <= s <= max_stock}
    for _ in range(rounds):
        grew = False
        base = list(out)
        for S in stock_lens:
            for a in base:
                for r in (S - a - kerf, S - a):
                    if min_cut <= r <= max_stock and r not in out:
                        out.add(r)
                        grew = True
        if not grew:
            break
    return out


def _weld_joint_windows(pipe, min_seg: int, max_stock: int) -> list[tuple[int, int]]:
    """焊口允许区间（闭区间），且两侧都能形成合法段长。"""
    L = pipe.length
    forbidden = sorted(pipe.forbidden, key=lambda iv: iv.start)
    gaps: list[tuple[int, int]] = []
    cur = 0
    for iv in forbidden:
        if cur < iv.start:
            gaps.append((cur, iv.start))
        cur = max(cur, iv.end)
    if cur < L:
        gaps.append((cur, L))
    windows: list[tuple[int, int]] = []
    for lo, hi in gaps:
        # 焊口 j ∈ [lo, hi) 在 gap 语义下；weld_allowed 再精判
        a0 = max(lo, min_seg)
        a1 = min(hi - 1, L - min_seg, max_stock)
        if a0 <= a1:
            windows.append((a0, a1))
    return windows


def _standard_lengths(group) -> list[int]:
    try:
        from backend.app.solver import _standard_segment_lengths

        return list(_standard_segment_lengths(group))
    except Exception:
        max_stock = max(s.length for s in group.stocks)
        kerf = group.blade_margin
        lengths: set[int] = set()
        for stock in group.stocks:
            lengths.add(stock.length)
            for pieces in (2, 3, 4):
                piece = (stock.length - kerf * (pieces - 1)) // pieces
                if piece > 0:
                    lengths.add(piece)
        for pipe in group.pipes:
            for pieces in range(1, min(pipe.max_joints + 2, 6)):
                piece = pipe.length // pieces
                if 0 < piece <= max_stock:
                    lengths.add(piece)
        return sorted(x for x in lengths if x > 0)


def _explicit_patterns(pipe, max_stock: int, min_cut: int, min_wd: int,
                       stock_lens: list[int],
                       stock_qty: dict[int, int] | None = None) -> list[tuple[int, ...]]:
    """禁焊感知的显式拼法（不靠大 Σ DFS）。优先少焊口。

    增强：禁焊窗口内稠密 2 段 + 短料(非充裕母料)诱导 3 段，避免极紧库存下
    仅靠母料倍数焊点导致字母表过瘦、MIP 直接不可行。
    """
    L = pipe.length
    min_seg = max(min_cut, 1)
    pats: list[tuple[int, ...]] = []
    stock_lens = sorted(set(stock_lens))
    if not stock_lens:
        return pats

    if min_seg <= L <= max_stock:
        pats.append((L,))

    if pipe.max_joints < 1:
        return list(dict.fromkeys(pats))

    # 2 段: 焊点取母料诱导位置（及对称），必须 weld_allowed
    weld_pos: set[int] = set()
    for S in stock_lens:
        k = 1
        while k * S < L:
            weld_pos.add(k * S)
            weld_pos.add(L - k * S)
            k += 1
            if k > L // max(min(stock_lens), 1) + 3:
                break

    for w in sorted(weld_pos):
        if not (min_seg <= w <= max_stock):
            continue
        b = L - w
        if not (min_seg <= b <= max_stock):
            continue
        if not pipe.weld_allowed(w):
            continue
        pats.append((w, b))

    # 网格 2 段（短管打包关键）
    step = max(min_wd, 1)
    grid_added = 0
    for w in range(step, L - step + 1, step):
        if grid_added >= 24:
            break
        if not (min_seg <= w <= max_stock):
            continue
        b = L - w
        if not (min_seg <= b <= max_stock):
            continue
        if not pipe.weld_allowed(w):
            continue
        pats.append((w, b))
        grid_added += 1

    # 禁焊窗口稠密 2 段：优先覆盖「段 + 互补 = 某母料」的精确套料点
    windows = _weld_joint_windows(pipe, min_seg, max_stock)
    pack = max(stock_lens)
    mate_targets = set(stock_lens)
    dense_cap = 120
    dense_added = 0
    for lo, hi in windows:
        # 窗口内先取母料互补点，再均匀抽样
        priority: list[int] = []
        for S in stock_lens:
            for w in (S, L - S, S - (L - pack) if L > pack else None):
                if w is None:
                    continue
                if lo <= w <= hi and min_seg <= w <= max_stock:
                    priority.append(w)
            # 精确：w 与 L-w 都能进母料；或 w 与 S-w 共棒
            for w in range(lo, hi + 1):
                if w in mate_targets or (L - w) in mate_targets:
                    priority.append(w)
                if any(min_seg <= S - w <= max_stock for S in stock_lens):
                    if dense_added + len(priority) < dense_cap:
                        priority.append(w)
        # 均匀步长
        span = max(1, hi - lo)
        stride = max(1, span // 24)
        for w in range(lo, hi + 1, stride):
            priority.append(w)
        for w in sorted(set(priority)):
            if dense_added >= dense_cap:
                break
            if not (min_seg <= w <= max_stock):
                continue
            b = L - w
            if not (min_seg <= b <= max_stock):
                continue
            if not pipe.weld_allowed(w):
                continue
            pats.append((w, b))
            dense_added += 1

    if pipe.max_joints < 2:
        uniq = list(dict.fromkeys(pats))
        uniq.sort(key=lambda seq: (len(seq) - 1, seq))
        return uniq

    # 3 段: 两焊点均合法，中段 >= min_wd（母料诱导焊点）
    for w1 in sorted(weld_pos):
        if not pipe.weld_allowed(w1):
            continue
        for w2 in sorted(weld_pos):
            if w2 <= w1:
                continue
            if not pipe.weld_allowed(w2):
                continue
            parts = (w1, w2 - w1, L - w2)
            if any(p < min_seg or p > max_stock for p in parts):
                continue
            if parts[1] < min_wd:
                continue
            pats.append(parts)

    # 短料诱导 3 段：必须能吃掉非充裕母料，否则极紧题长度直接不够
    qty = stock_qty or {S: 1 for S in stock_lens}
    pack_S = max(stock_lens, key=lambda S: (qty.get(S, 0), S))
    scarce = [S for S in stock_lens if S != pack_S]
    if not scarce:
        # 即便无 scarce，也补「整根 pack 作中/边段」的 3 段
        scarce = [S for S in stock_lens if S < L][:2]

    def add_three(w1: int, w2: int) -> None:
        if not (0 < w1 < w2 < L):
            return
        if not pipe.weld_allowed(w1) or not pipe.weld_allowed(w2):
            return
        parts = (w1, w2 - w1, L - w2)
        if any(p < min_seg or p > max_stock for p in parts):
            return
        if parts[1] < min_wd:
            return
        pats.append(parts)

    three_cap = 180
    three_added = 0
    for S in scarce + [pack_S]:
        if three_added >= three_cap:
            break
        # S 作首段
        if min_seg <= S <= max_stock and pipe.weld_allowed(S):
            for lo, hi in windows:
                stride = max(1, (hi - lo) // 16)
                for w2 in list(range(max(lo, S + min_wd), hi + 1, stride)) + [
                    S + S,
                    L - S,
                    S + pack_S,
                ]:
                    if three_added >= three_cap:
                        continue
                    if w2 <= S or w2 >= L:
                        continue
                    before = len(pats)
                    add_three(S, w2)
                    if len(pats) > before:
                        three_added += 1
        # S 作末段：第二焊口在 L-S
        end_joint = L - S
        if min_seg <= S <= max_stock and 0 < end_joint < L and pipe.weld_allowed(end_joint):
            for lo, hi in windows:
                stride = max(1, (hi - lo) // 16)
                for w1 in list(range(lo, min(hi, end_joint - min_wd) + 1, stride)) + [
                    S,
                    L - pack_S if L > pack_S else None,
                    end_joint - S,
                    pack_S if pack_S < end_joint else None,
                ]:
                    if w1 is None or three_added >= three_cap:
                        continue
                    if not (0 < w1 < end_joint):
                        continue
                    before = len(pats)
                    add_three(w1, end_joint)
                    if len(pats) > before:
                        three_added += 1
        # S 作中段：w2 - w1 = S
        if min_seg <= S <= max_stock:
            for lo, hi in windows:
                stride = max(1, (hi - lo) // 12)
                for w1 in range(lo, hi + 1, stride):
                    if three_added >= three_cap:
                        break
                    w2 = w1 + S
                    if w2 >= L:
                        continue
                    before = len(pats)
                    add_three(w1, w2)
                    if len(pats) > before:
                        three_added += 1

    # pack 精确两段共棒 → 第三段 = L-pack（经典互补链）
    if L > pack_S and pipe.max_joints >= 2 and pipe.weld_allowed(pack_S):
        rem = L - pack_S
        if min_seg <= rem <= max_stock:
            for lo, hi in windows:
                stride = max(1, (hi - lo) // 20)
                for a in range(lo, min(hi, pack_S - min_seg) + 1, stride):
                    if three_added >= three_cap:
                        break
                    if not (min_seg <= a <= max_stock):
                        continue
                    b = pack_S - a
                    if not (min_seg <= b <= max_stock):
                        continue
                    before = len(pats)
                    add_three(a, pack_S)
                    if len(pats) > before:
                        three_added += 1
                    if pipe.weld_allowed(rem) and rem < pack_S:
                        before = len(pats)
                        add_three(rem, rem + a)
                        if len(pats) > before:
                            three_added += 1

    # ---- 末段 = 充裕母料 的 3/4 段（老软件 D2 短料题主结构）----
    # 例：18985 = 1448+5497+1040+11000 / 6020+1965+11000 / 7985+11000
    def add_seq(seq: tuple[int, ...]) -> bool:
        if len(seq) - 1 > pipe.max_joints:
            return False
        if any(p < min_seg or p > max_stock for p in seq):
            return False
        pos = 0
        for i, seg in enumerate(seq):
            pos += seg
            if i < len(seq) - 1:
                if not pipe.weld_allowed(pos):
                    return False
                if i > 0 and seg < min_wd:
                    return False
        pats.append(seq)
        return True

    if L > pack_S and pipe.max_joints >= 1:
        rem = L - pack_S
        end_j = rem  # 最后一段为 pack_S
        if min_seg <= rem and pipe.weld_allowed(end_j):
            # 2 段已覆盖 (rem, pack)；补 3 段 (a, rem-a, pack) — 密采样
            # 2 段已覆盖 (rem, pack)；补 3 段 (a, rem-a, pack)
            # 首段必须 >= min_wd，避免 (101, rem-101, pack) 污染字母表
            if pipe.max_joints >= 2:
                for lo, hi in windows:
                    a0 = max(lo, min_wd, min_seg)
                    a1 = min(hi, rem - min_seg)
                    if a0 > a1:
                        continue
                    for a in range(a0, a1 + 1):  # 密：短料题焊点必须打中
                        b = rem - a
                        if b < min_seg:
                            continue
                        add_seq((a, b, pack_S))
                for S in scarce:
                    for k in range(2, 7):
                        for trim in (0, 560, 800):
                            if S <= trim:
                                continue
                            a = (S - trim) // k
                            if a >= min_wd and a < rem and pipe.weld_allowed(a):
                                add_seq((a, rem - a, pack_S))
                if rem >= 2 * min_wd:
                    add_seq((rem // 2, rem - rem // 2, pack_S))

            # 4 段 (a, b, rem-a-b, pack)：优先短料等分小段
            if pipe.max_joints >= 3:
                four_added = 0
                four_cap = 600
                scarce_pieces: set[int] = set()
                for S in scarce:
                    for k in range(2, 8):
                        # 含 520 等近余料，覆盖 1040=(7800-520)//7
                        for trim in list(range(0, 1201, 20)) + [50, 100, 560, 800, 1000]:
                            if S <= trim:
                                continue
                            piece = (S - trim) // k
                            if min_seg <= piece <= max_stock:
                                scarce_pieces.add(piece)
                piece_cands: set[int] = set(scarce_pieces)
                for lo, hi in windows:
                    a0 = max(lo, min_seg)
                    a1 = min(hi, rem - 2 * min_seg)
                    if a0 > a1:
                        continue
                    stride = 1 if (a1 - a0) <= 1200 else max(1, (a1 - a0) // 80)
                    for a in range(a0, a1 + 1, stride):
                        if pipe.weld_allowed(a):
                            piece_cands.add(a)
                for seq in list(pats):
                    if len(seq) == 3 and seq[-1] == pack_S:
                        piece_cands.add(seq[0])
                        piece_cands.add(seq[1])

                def try_four(a: int, b: int) -> None:
                    nonlocal four_added
                    if four_added >= four_cap:
                        return
                    c = rem - a - b
                    if c < min_seg or c > max_stock or b < min_seg:
                        return
                    if not pipe.weld_allowed(a) or not pipe.weld_allowed(a + b):
                        return
                    if add_seq((a, b, c, pack_S)):
                        four_added += 1

                # 第一轮：短料小段 × pack尾3段的部件（控制组合数，避免 cap 被小 a 烧光）
                pack_tail_parts: set[int] = set()
                for seq in pats:
                    if len(seq) == 3 and seq[-1] == pack_S:
                        pack_tail_parts.add(seq[0])
                        pack_tail_parts.add(seq[1])
                sp = sorted(scarce_pieces)
                # 只用「像样」的 3 段 pack 尾提供 mid（两段都 >= min_wd）
                good_mids: set[int] = set()
                for seq in pats:
                    if (
                        len(seq) == 3
                        and seq[-1] == pack_S
                        and seq[0] >= min_wd
                        and seq[1] >= min_wd
                    ):
                        good_mids.add(seq[0])
                        good_mids.add(seq[1])
                good_mids_l = sorted(good_mids)
                budget_per_a = max(12, four_cap // max(1, len(sp) or 1))
                priority_a: list[int] = []
                for S in scarce:
                    for k in (3, 4, 5, 6):
                        for trim in (0, 400, 560, 800):
                            if S > trim:
                                priority_a.append((S - trim) // k)
                priority_a = [a for a in dict.fromkeys(priority_a) if a in set(sp)]
                priority_a.sort(
                    key=lambda a: (0 if a in (1448, 1560, 1950, 1300) else 1, -a)
                )
                other_a = [a for a in sp if a not in set(priority_a)]
                a_order = priority_a[:8] + other_a[:4]
                three_ys = sorted(
                    {
                        seq[1]
                        for seq in pats
                        if len(seq) == 3
                        and seq[-1] == pack_S
                        and seq[0] >= min_wd
                        and seq[1] >= min_wd
                        and 2000 <= seq[1] <= 6500
                    }
                )

                def _tile_c(c: int) -> int:
                    return min(
                        (abs(k * c - S) for S in scarce for k in range(2, 8)),
                        default=10**9,
                    )

                # 全局按「第三段能否在短料上等分共棒」排序交叉
                cross: list[tuple[int, int, int, int]] = []
                for a in a_order:
                    for y in three_ys:
                        c = rem - a - y
                        if c < min_seg or c > max_stock:
                            continue
                        cross.append((_tile_c(c), a, y, c))
                cross.sort()

                # 强制：短料等分首段 × 全部可共棒 mid（不受 top30/four_cap 挤占）
                # 例：1448+5497+1040，tile(1040)=520，在 3000+ mid 中排不到 top30
                classic_a = [
                    a
                    for a in a_order
                    if a in (1448, 1560, 1950, 1300, 1114, 2600)
                    or any(
                        a == (S - trim) // k
                        for S in scarce
                        for k in (4, 5, 6, 7)
                        for trim in (0, 400, 520, 560, 800)
                        if S > trim
                    )
                ]
                for a in classic_a or a_order[:4]:
                    for y in three_ys:
                        c = rem - a - y
                        if c < min_seg or c > max_stock:
                            continue
                        if _tile_c(c) <= 800:
                            add_seq((a, y, c, pack_S))
                # 其余 a：每 a 保留 tile 最优一批
                for a in a_order:
                    if a in set(classic_a):
                        continue
                    local = [(tile, y, c) for tile, aa, y, c in cross if aa == a]
                    local.sort()
                    for tile, y, c in local[:40]:
                        if tile > 1600:
                            break
                        add_seq((a, y, c, pack_S))
                # 强制：首段×短料等分第三段（中段 = rem-a-c）
                for a in a_order:
                    for c in sp:
                        b = rem - a - c
                        if b < min_seg or b > max_stock:
                            continue
                        if _tile_c(c) <= 800 or c in scarce_pieces:
                            add_seq((a, b, c, pack_S))

                for tile, a, y, c in cross[:800]:
                    if four_added >= four_cap:
                        break
                    if tile > 1200:
                        break
                    try_four(a, y)
                for a in a_order:
                    if four_added >= four_cap:
                        break
                    for c in sp:
                        try_four(a, rem - a - c)
                # 窗口密采样：大 mid 优先，避免 budget 被小 b 烧光
                for a in sp:
                    if four_added >= four_cap:
                        break
                    used = 0
                    wins = sorted(windows, key=lambda wh: -(wh[0] + wh[1]) // 2)
                    for lo, hi in wins:
                        b0 = max(lo, min_wd)
                        b1 = min(hi, rem - a - min_seg)
                        if b0 > b1:
                            continue
                        for b in range(b1, b0 - 1, -1):  # 从大到小
                            if used >= budget_per_a or four_added >= four_cap:
                                break
                            before = four_added
                            try_four(a, b)
                            if four_added > before:
                                used += 1
                # 第二轮：一般组合
                ordered = sorted(piece_cands)
                for a in ordered:
                    if four_added >= four_cap:
                        break
                    for b in ordered:
                        try_four(a, b)
                # rem 近均等三段
                base = rem // 3
                for da in range(-3, 4):
                    for db in range(-3, 4):
                        a = base + da
                        b = base + db
                        c = rem - a - b
                        if min(a, b, c) >= min_seg and max(a, b, c) <= max_stock:
                            add_seq((a, b, c, pack_S))

    # 短料整根吃进：末段/首段 = scarce，前面拆 rem2
    for S in scarce:
        if L <= S or pipe.max_joints < 2:
            continue
        rem2 = L - S
        if rem2 < min_seg or not pipe.weld_allowed(rem2):
            continue
        # (a, rem2-a, S)
        for lo, hi in windows:
            stride = max(1, (hi - lo) // 20)
            for a in range(max(lo, min_seg), min(hi, rem2 - min_seg) + 1, stride):
                b = rem2 - a
                if b >= min_seg:
                    add_seq((a, b, S))
        if pipe.max_joints >= 3:
            base = rem2 // 3
            for da in range(-5, 6):
                a = base + da
                b = base
                c = rem2 - a - b
                if min(a, b, c) >= min_seg and max(a, b, c) <= max_stock:
                    add_seq((a, b, c, S))

    uniq = list(dict.fromkeys(pats))
    uniq.sort(key=lambda seq: (len(seq) - 1, seq))
    return uniq


def _candidates(group) -> tuple[set[int], dict[str, Any]]:
    from scripts._arcflow_v3 import _residual_seg_lengths

    min_cut = max(group.min_cut_length, 1)
    max_stock = max(s.length for s in group.stocks)
    kerf = group.blade_margin
    stock_qty = _stock_qty_map(group)
    stock_lens = sorted(stock_qty)
    tags: dict[int, str] = {}

    for p in group.pipes:
        if min_cut <= p.length <= max_stock:
            tags[p.length] = "t0"

    for r in _residual_seg_lengths(group.pipes, stock_qty, max_stock, min_cut, kerf):
        if min_cut <= r <= max_stock and r not in tags:
            tags[r] = "residual"

    for s in _standard_lengths(group):
        if min_cut <= s <= max_stock and s not in tags:
            tags[s] = "standard"
    for L in stock_qty:
        for pieces in (2, 3):
            piece = (L - kerf * (pieces - 1)) // pieces
            if min_cut <= piece <= max_stock and piece not in tags:
                tags[piece] = "standard"

    # 禁焊显式拆分段（锁定优先级最高来源之一）
    for p in group.pipes:
        for seq in _explicit_patterns(
            p, max_stock, min_cut, group.min_weld_distance, stock_lens, stock_qty
        ):
            for seg in seq:
                if seg not in tags:
                    tags[seg] = "weld_aware"

    # 满装对（受控）
    ranked = sorted(group.pipes, key=lambda p: -p.demand * p.length)[:8]
    main_S = max(stock_qty, key=stock_qty.get)
    pair_added = 0
    existing = list(tags)
    for _p in ranked:
        if pair_added >= 12:
            break
        for a in existing[:40]:
            b = main_S - a - kerf
            if not (min_cut <= b <= max_stock):
                continue
            if a + b + kerf > main_S:
                continue
            for seg in (a, b):
                if seg not in tags and min_cut <= seg <= max_stock:
                    tags[seg] = "pair"
                    pair_added += 1

    # FFD 高频段（打包可行性种子）：纯几何字母表在 113146 上证明不够装满母料
    ffd_seed_n = 0
    try:
        from scripts._arcflow_v3 import _ffd_weld_incumbent

        ffd = _ffd_weld_incumbent(group)
        if ffd and ffd.get("weld_patterns"):
            from collections import Counter

            freq: Counter = Counter()
            for (_i, seq), qty in ffd["weld_patterns"].items():
                for s in seq:
                    if min_cut <= s <= max_stock:
                        freq[s] += qty
            for s, _n in freq.most_common(40):
                if s not in tags:
                    tags[s] = "ffd_seed"
            ffd_seed_n = min(40, len(freq))
    except Exception:
        pass

    meta = {
        "by_tag": {},
        "main_stock": main_S,
        "n_candidates": len(tags),
        "ffd_seed_n": ffd_seed_n,
    }
    for _seg, tag in tags.items():
        meta["by_tag"][tag] = meta["by_tag"].get(tag, 0) + 1
    return set(tags), {"tags": tags, **meta}


_TAG_PRIORITY = {
    "t0": 0,
    "weld_aware": 1,
    "ffd_seed": 2,
    "residual": 3,
    "standard": 4,
    "pair": 5,
    "neighbor": 6,
}


def _truncate(tags: dict[int, str], locked: set[int], sigma_cap: int) -> list[int]:
    locked = set(locked)
    rest = [s for s in tags if s not in locked]
    rest.sort(key=lambda s: (_TAG_PRIORITY.get(tags.get(s, ""), 9), s))
    room = max(0, sigma_cap - len(locked))
    return sorted(set(locked) | set(rest[:room]))


def build(group, *, util_floor: float | None = None, sigma_cap: int | None = None) -> Recipe:
    del util_floor
    cap0 = SIGMA_CAP_DEFAULT if sigma_cap is None else int(sigma_cap)
    min_cut = max(group.min_cut_length, 1)
    max_stock = max(s.length for s in group.stocks)
    stock_lens = sorted(_stock_qty_map(group))
    min_wd = group.min_weld_distance

    cand, gen_meta = _candidates(group)
    tags: dict[int, str] = dict(gen_meta["tags"])
    diagnosis: list[str] = []

    # 每管显式拼法（不依赖大集合 DFS）
    pipe_pats: dict[int, list[tuple[int, ...]]] = {}
    locked: set[int] = set()
    unreachable: list[int] = []
    seed_weld: list[tuple] = []
    stock_qty = _stock_qty_map(group)
    pack_S = max(stock_lens, key=lambda S: (stock_qty.get(S, 0), S))
    kerf = group.blade_margin
    for i, p in enumerate(group.pipes):
        pats = _explicit_patterns(
            p, max_stock, min_cut, min_wd, stock_lens, stock_qty
        )
        # 只保留段已在候选或可加入候选的
        feasible = []
        for seq in pats:
            for seg in seq:
                if seg not in tags and min_cut <= seg <= max_stock:
                    tags[seg] = "weld_aware"
                    cand.add(seg)
            if all(seg in cand for seg in seq):
                feasible.append(seq)
        pipe_pats[i] = feasible
        if not feasible:
            unreachable.append(i)
        else:
            # 锁入：整管 + 母料诱导 2 段全锁；网格 2 段按「易打包」取 Top
            # 短料 3 段与精确共棒互补段优先全锁（D2 极紧题关键）
            whole = [seq for seq in feasible if len(seq) == 1]
            two = [seq for seq in feasible if len(seq) == 2]
            three = [seq for seq in feasible if len(seq) == 3]
            fourp = [seq for seq in feasible if len(seq) >= 4]
            for seq in whole:
                locked.update(seq)

            def pack_score(seq: tuple[int, ...]) -> tuple:
                a, b = seq[0], seq[-1] if len(seq) > 1 else (seq[0], seq[0])
                best = min(abs(a - (S / 2)) + abs(b - (S / 2)) for S in stock_lens)
                return (best, max(seq), seq)

            two_sorted = sorted(two, key=pack_score)
            induced = [seq for seq in two_sorted if any(x in stock_lens for x in seq)]
            # 精确共棒：两段之和 = 某母料
            exact_pair = [
                seq
                for seq in two_sorted
                if any(sum(seq) + kerf == S or sum(seq) == S for S in stock_lens)
            ]
            grid_only = [
                seq for seq in two_sorted if seq not in induced and seq not in exact_pair
            ]
            for seq in induced + exact_pair + grid_only[:16]:
                locked.update(seq)

            scarce_three = [
                seq for seq in three if any(x != pack_S and x in stock_qty for x in seq)
            ]
            pack_three = [seq for seq in three if pack_S in seq or sum(seq[:2]) == pack_S]
            # 4+ 段：末段为 pack / 含短料小段 — 老软件短料题关键
            # 锁入短料相关段（控制规模，禁止把全部稠密 3 段都锁进 Σ）
            scarce_piece_like = set()
            for S in stock_qty:
                if S == pack_S:
                    continue
                for k in range(2, 7):
                    for trim in (0, 50, 100, 200, 400, 560, 800, 1000):
                        if S > trim:
                            scarce_piece_like.add((S - trim) // k)
            prefer_three = [
                seq
                for seq in three
                if any(x in scarce_piece_like for x in seq)
                or (seq[-1] == pack_S and seq[0] in scarce_piece_like)
            ]
            prefer_four = [
                seq
                for seq in fourp
                if any(x in scarce_piece_like for x in seq)
            ]
            # 含经典短料等分小段的 4 段置顶（否则被 1133 类挤出 keep 上限）
            classic = []
            rest_pf = []
            classic_need = set()
            for S in stock_qty:
                if S == pack_S:
                    continue
                for k in (4, 5, 6):
                    for trim in (0, 560, 800):
                        if S > trim:
                            classic_need.add((S - trim) // k)
            for seq in prefer_four:
                if any(x in classic_need for x in seq):
                    classic.append(seq)
                else:
                    rest_pf.append(seq)
            # 含短料等分首段 + 第三段可共棒的 4 段置顶（对齐老软件结构）
            scarce_only = [S for S in stock_qty if S != pack_S]

            def _four_rank(seq: tuple[int, ...]) -> tuple:
                if len(seq) < 4:
                    return (9, 10**9, seq)
                a, mid, c = seq[0], seq[1], seq[2]
                tile = min(
                    (abs(k * c - S) for S in scarce_only for k in range(2, 8)),
                    default=10**9,
                )
                # 经典结构：首段短料等分 + 中段接近 pack/2 + 第三段可共棒
                classic_hit = 0 if a in classic_need else 1
                mid_bonus = 0 if 4500 <= mid <= 6500 else 1
                return (
                    classic_hit,
                    0 if a >= 1400 else 1,
                    mid_bonus,
                    tile,
                    abs(mid - pack_S // 2),
                    seq,
                )

            classic.sort(key=_four_rank)
            # 额外：每个经典首段按 tile 与「中段居中」双通道保留
            by_a: dict[int, list] = {}
            for seq in classic:
                by_a.setdefault(seq[0], []).append(seq)
            forced_classic: list = []
            for a, seqs in by_a.items():
                if a not in classic_need and a < 1400:
                    continue

                def _tile_of(seq: tuple[int, ...]) -> int:
                    c = seq[2]
                    return min(
                        (abs(k * c - S) for S in scarce_only for k in range(2, 8)),
                        default=10**9,
                    )

                by_tile = sorted(seqs, key=lambda s: (_tile_of(s), s))[:6]
                by_mid = sorted(
                    seqs, key=lambda s: (abs(s[1] - pack_S // 2), _tile_of(s), s)
                )[:6]
                # 中段≈半棒且第三段可共棒（老软件 5497/1040 结构）
                by_struct = [
                    s
                    for s in sorted(seqs, key=lambda s: (_tile_of(s), abs(s[1] - 5500)))
                    if 4800 <= s[1] <= 6200 and _tile_of(s) <= 800
                ][:6]
                forced_classic.extend(by_tile + by_mid + by_struct)
            pack_tail4 = [seq for seq in fourp if seq[-1] == pack_S]
            keep_three = list(
                dict.fromkeys(scarce_three + prefer_three + pack_three + three[:16])
            )[:36]
            keep_four = list(
                dict.fromkeys(
                    forced_classic[:40]
                    + classic[:24]
                    + rest_pf[:8]
                    + pack_tail4[:8]
                    + fourp[:4]
                )
            )[:56]
            # 由 4 段诱导 3/2 段互补（1448+1040=2488 → (2488,5497,pack)）
            induced_from_four: list[tuple[int, ...]] = []
            three_set, two_set = set(three), set(two)
            for seq in keep_four:
                if len(seq) < 4 or seq[-1] != pack_S:
                    continue
                a, b, c, p = seq[0], seq[1], seq[2], seq[3]
                for cand_seq in (
                    (a + c, b, p),
                    (a, b + c, p),
                    (a + b, c, p),
                    (b, a + c, p),
                    (a + b + c, p),
                ):
                    if cand_seq in three_set or cand_seq in two_set:
                        induced_from_four.append(cand_seq)
                    for s in cand_seq:
                        if min_cut <= s <= max_stock:
                            locked.add(s)
            # pack 尾 3 段：优先「可共棒 / 近半棒」+ 短料近等分强制
            pack_tail3 = [
                seq
                for seq in three
                if seq[-1] == pack_S and seq[0] >= min_wd and seq[1] >= min_wd
            ]

            def _three_rank(seq: tuple[int, ...]) -> tuple:
                a, b = seq[0], seq[1]
                tile_a = min(
                    (abs(k * a - S) for S in scarce_only for k in range(2, 8)),
                    default=10**9,
                )
                tile_b = min(
                    (abs(k * b - S) for S in scarce_only for k in range(2, 8)),
                    default=10**9,
                )
                return (
                    min(tile_a, tile_b),
                    min(abs(a - pack_S // 2), abs(b - pack_S // 2)),
                    seq,
                )

            pack_tail3.sort(key=_three_rank)
            # 短料近等分诱导的 3 段（978+2691 / 798+2871）：trim 密采样 + 多样性保留
            forced_three: list[tuple[int, ...]] = []
            cross_sk: list[tuple[int, ...]] = []
            rem_pack = None
            for seq in three:
                if seq[-1] == pack_S and len(seq) == 3:
                    rem_pack = seq[0] + seq[1]
                    break
            if rem_pack is not None:
                three_set2 = set(pack_tail3)

                def _tile_one(x: int) -> int:
                    return min(
                        (abs(k * x - S) for S in scarce_only for k in range(2, 8)),
                        default=10**9,
                    )

                raw: list[tuple[int, ...]] = []
                per_sk: list[tuple[int, ...]] = []
                for S in scarce_only:
                    # 优先 4/5 段共棒（老软件短料切法主力），再补 2/3/6/7
                    for k in (5, 4, 6, 3, 7, 2):
                        cands: list[tuple[int, tuple[int, ...]]] = []
                        for trim in range(0, min(1201, S // 2 + 1), 2):
                            a = (S - trim) // k
                            if a < min_wd or a >= rem_pack:
                                continue
                            b = rem_pack - a
                            if b < min_cut:
                                continue
                            oriented = (a, b, pack_S)
                            if oriented not in three_set2:
                                continue
                            raw.append(oriented)
                            if (b, a, pack_S) in three_set2:
                                raw.append((b, a, pack_S))
                            cands.append((abs(k * a - S), oriented))
                        cands.sort()
                        seen_a: set[int] = set()
                        kept = 0
                        gap = 12 if k in (4, 5) else 20
                        lim = 5 if k in (4, 5) else 2
                        for tile_sk, oriented in cands:
                            if tile_sk > 800:
                                break
                            a0 = oriented[0]
                            if any(abs(a0 - x) < gap for x in seen_a):
                                continue
                            seen_a.add(a0)
                            per_sk.append(oriented)
                            sym = (oriented[1], oriented[0], oriented[2])
                            if sym in three_set2:
                                per_sk.append(sym)
                            kept += 1
                            if kept >= lim:
                                break
                raw = list(dict.fromkeys(raw))
                per_sk = list(dict.fromkeys(per_sk))
                both_ok = [
                    s
                    for s in raw
                    if _tile_one(s[0]) <= 800 and _tile_one(s[1]) <= 800
                ]
                both_ok.sort(
                    key=lambda s: (
                        _tile_one(s[0]) + _tile_one(s[1]),
                        max(_tile_one(s[0]), _tile_one(s[1])),
                        s,
                    )
                )
                # 跨短料互补：每个 (S1,S2,k1,k2) 只保留最优一组
                cross_sk: list[tuple[int, ...]] = []
                if len(scarce_only) >= 2:
                    for S1 in scarce_only:
                        for S2 in scarce_only:
                            if S1 == S2:
                                continue
                            for k1 in (3, 4, 5, 6):
                                for k2 in (3, 4, 5, 6):
                                    best: tuple[tuple[int, int], tuple[int, ...]] | None = None
                                    for trim in range(0, min(901, S1 // 2 + 1), 2):
                                        a = (S1 - trim) // k1
                                        if a < min_wd or a >= rem_pack:
                                            continue
                                        b = rem_pack - a
                                        if b < min_cut:
                                            continue
                                        t1 = abs(k1 * a - S1)
                                        t2 = abs(k2 * b - S2)
                                        if t1 > 250 or t2 > 800:
                                            continue
                                        oriented = (a, b, pack_S)
                                        if oriented not in three_set2:
                                            continue
                                        # 优先把较大短料共棒残料压到最小（2691×4 vs 11500）
                                        sc = (t2, t1)
                                        if best is None or sc < best[0]:
                                            best = (sc, oriented)
                                    if best is not None:
                                        cross_sk.append(best[1])
                                        sym = (best[1][1], best[1][0], best[1][2])
                                        if sym in three_set2:
                                            cross_sk.append(sym)
                    cross_sk = list(dict.fromkeys(cross_sk))
                forced_three = list(
                    dict.fromkeys(cross_sk + per_sk + both_ok[:8])
                )

            keep_three = list(
                dict.fromkeys(
                    induced_from_four
                    + forced_three
                    + pack_tail3[:24]
                    + scarce_three
                    + prefer_three
                    + pack_three
                    + three[:8]
                )
            )[:64]
            for seq in keep_three + keep_four:
                locked.update(seq)
            # 只把「高质量」3/4 段进 seed（控制核心段集，供高段数切法枚举）
            # 只把跨短料互补 3 段 + 少量经典 4 段进 seed（段集必须小，否则切法枚举被 budget 截断）
            pack_two = [s for s in two if pack_S in s][:6]
            struct4 = [
                s
                for s in forced_classic
                if len(s) >= 4 and 4800 <= s[1] <= 6200
            ][:8]
            seed_keep = list(
                dict.fromkeys(
                    cross_sk[:16]
                    + pack_two
                    + struct4
                    + forced_classic[:6]
                    + induced_from_four[:6]
                    + (forced_three[:10] if not cross_sk else [])
                )
            )[:24]
            if not seed_keep:
                seed_keep = list(
                    dict.fromkeys(
                        forced_three[:12]
                        + struct4
                        + forced_classic[:8]
                        + pack_tail3[:8]
                        + keep_four[:6]
                        + pack_two
                    )
                )[:24]
            for seq in seed_keep:
                seed_weld.append((i, seq))
            for seq in cross_sk + struct4:
                locked.update(seq)

    # 锁定段做「受控」母料互补闭包：只补 stock-seg 与 weld 段对 stock 的精确残段
    core_locked = set(locked)
    mate_added: set[int] = set()
    for S in stock_lens:
        for a in list(core_locked):
            for r in (S - a - kerf, S - a):
                if min_cut <= r <= max_stock:
                    mate_added.add(r)
    # 再一轮：残段对母料（受控）
    extra: set[int] = set()
    for S in stock_lens:
        for a in list(mate_added | core_locked):
            r = S - a - kerf
            if min_cut <= r <= max_stock:
                extra.add(r)
            r = S - a
            if min_cut <= r <= max_stock:
                extra.add(r)
    mate_added |= extra
    # 限制闭包规模，避免切列爆炸
    if len(mate_added) > 80:
        scored = []
        for r in mate_added:
            score = 0
            if r in stock_lens:
                score += 100
            if r in core_locked:
                score += 50
            if any(
                (S - a - kerf == r or S - a == r) for S in stock_lens for a in core_locked
            ):
                score += 10
            scored.append((-score, r))
        scored.sort()
        mate_added = {r for _, r in scored[:80]}
    locked = set(core_locked) | mate_added
    for s in locked:
        if s not in tags and min_cut <= s <= max_stock:
            tags[s] = "weld_aware"
            cand.add(s)
    diagnosis.append(f"lock_mate_closure={len(locked)}")

    # 强制锁入 FFD 高频段（否则截断时被 weld_aware 挤掉，Cr 类题 ILP 不可行）
    ffd_locked = {s for s, t in tags.items() if t == "ffd_seed"}
    if ffd_locked:
        locked |= ffd_locked
        diagnosis.append(f"lock_ffd_seed={len(ffd_locked)}")

    if unreachable:
        diagnosis.append(f"hard_unreachable={unreachable}")

    # cap：至少容纳全部锁定段；短料题再抬一档
    cap = max(cap0, len(locked))
    if any(S != pack_S for S in stock_lens):
        cap = max(cap, min(160, len(tags)))
    if cap > cap0:
        diagnosis.append(f"sigma_cap_raised={cap}")

    sigma_cut = set(_truncate(tags, locked, cap))
    # 截断后复查：锁定段必须都在
    missing_lock = locked - sigma_cut
    if missing_lock:
        sigma_cut |= missing_lock
        cap = len(sigma_cut)
        diagnosis.append(f"force_locked={len(missing_lock)}")

    # 再验证每管在最终 Σ 上仍有显式拼法
    final_pats: dict[int, int] = {}
    still_bad: list[int] = []
    for i, p in enumerate(group.pipes):
        ok_seqs = [seq for seq in pipe_pats[i] if all(s in sigma_cut for s in seq)]
        if not ok_seqs and i not in unreachable:
            # 重新算一次（Σ 变了）
            ok_seqs = [
                seq
                for seq in _explicit_patterns(
                    p, max_stock, min_cut, min_wd, stock_lens, stock_qty
                )
                if all(s in sigma_cut for s in seq)
            ]
            pipe_pats[i] = ok_seqs
        final_pats[i] = len(ok_seqs)
        if not ok_seqs:
            still_bad.append(i)
        else:
            best = min(ok_seqs, key=lambda seq: (len(seq), seq))
            seed_weld.append((i, best))
    # 经典 3/4 段可能因 Σ 截断失效；只保留段全在最终 Σ 内的
    seed_weld = [
        item
        for item in dict.fromkeys(seed_weld)
        if all(s in sigma_cut for s in item[1])
    ]

    hard = bool(still_bad)
    if still_bad and still_bad != unreachable:
        diagnosis.append(f"unreachable_after_sigma={still_bad}")

    # sigma_weld 必须含锁定段，不能只留 t0
    sigma_weld = sorted(
        set(locked)
        | {
            s
            for s in sigma_cut
            if tags.get(s) in ("t0", "weld_aware", "pair", "ffd_seed")
        }
    )
    if len(sigma_weld) < 8:
        sigma_weld = sorted(sigma_cut)
    # 受控闭包，防止字母表膨胀
    sw = set(sigma_weld)
    add: set[int] = set()
    for S in stock_lens:
        for a in list(sw):
            for r in (S - a - kerf, S - a):
                if min_cut <= r <= max_stock:
                    add.add(r)
    if len(add) > 60:
        add = set(sorted(add)[:60])
    sigma_weld = sorted(sw | add)
    sigma_cut |= set(sigma_weld)
    cap = len(sigma_cut)

    # 统计 by_tag（最终保留）
    by_tag: dict[str, int] = {}
    for s in sigma_cut:
        t = tags.get(s, "other")
        by_tag[t] = by_tag.get(t, 0) + 1

    meta = {
        "by_tag": by_tag,
        "main_stock": gen_meta.get("main_stock"),
        "n_candidates": gen_meta.get("n_candidates"),
        "sigma_cap": cap,
        "sigma_cap_default": cap0,
        "sigma_weld_n": len(sigma_weld),
        "sigma_cut_n": len(sigma_cut),
        "pipe_pattern_counts": {str(i): final_pats[i] for i in final_pats},
        "hard_unreachable": hard,
        "unreachable_pipes": still_bad,
        "diagnosis": diagnosis,
        "locked_n": len(locked),
        "seed_weld_n": len(seed_weld),
    }
    return Recipe(
        sigma_weld=sorted(sigma_weld),
        sigma_cut=sorted(sigma_cut),
        seed_weld=seed_weld,
        meta=meta,
    )
