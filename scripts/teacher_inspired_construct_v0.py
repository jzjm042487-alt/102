"""教师指纹驱动的无教师 Σ/拼法构造器 v0。

规则来源：legacy-fingerprints 聚合挖掘（scripts/mine_fingerprint_rules.py），
非题号特判。当前指纹主导动机：

焊：
1. 长管：主锚几乎全是 S_max；(R,S)。仅当长管总需求 > S_max 库存时启用次长锚；
   半料锚（~0.5·S_max）可并存。禁止「所有高库存母料」产锚（会污染 Σ）
2. R 的 leftover / k·kerf / m·lo / 二阶 lo2：在全部母料上算切点，焊锚仍用 S
3. 短管：1 段 + gap/pack leftover（主 kerf 优先）；半料拆；(S-kerf)//2
4. 短管 3 段：对 2 段的两侧都做 leftover 拆（不只拆大段）
5. 跨管 pack leftover + 长度差修正：A 的 1 段 leftover 可作 B 的焊缝件
6. 禁焊区：仅在算术 2 段稀缺时，对合法焊位「区间端点/中点/四分位」采样

切：
7. 纯 k 拷贝不受 max_parts=8 限制（短余料棒可切 12–18 同件）
8. 全局枚举 + 每条焊缝局部字母表必覆盖（防大 Σ 挤掉混合 pack）

卫生：
9. Σ 严格来自最终焊缝；4 段最多 1 条

用法:
  .venv\\Scripts\\python.exe scripts\\teacher_inspired_construct_v0.py --probe-fails
  .venv\\Scripts\\python.exe scripts\\mine_fingerprint_rules.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case

CASES = ROOT / "data" / "legacy-cases"
D2_COMPARE = ROOT / "data" / "acceptance" / "d2-full-compare"
OUT = ROOT / "data" / "acceptance" / "legacy-fingerprints"


def _kerf_candidates(group) -> list[int]:
    blade = int(getattr(group, "blade_margin", 0) or 0)
    out: list[int] = []
    # never lead with 0 when blade is 0 — fingerprint slack is 5/6/10
    ordered = ([blade] if blade > 0 else []) + [5, 6, 10, 0, 15]
    for x in ordered:
        if x >= 0 and x not in out:
            out.append(x)
    return out


def _primary_kerfs(group) -> list[int]:
    blade = int(getattr(group, "blade_margin", 0) or 0)
    if blade > 0:
        return [blade] + [k for k in (5, 6) if k != blade][:1]
    return [5, 6]


def _weld_ok(pipe, segs: tuple[int, ...]) -> bool:
    if sum(segs) != pipe.length:
        return False
    pos = 0
    for s in segs[:-1]:
        pos += s
        if not pipe.weld_allowed(pos):
            return False
    return True


def _allowed_weld_positions(pipe, min_wd: int, length: int) -> list[int]:
    """Legal single-weld positions (forbidden-zone aware)."""
    out: list[int] = []
    for pos in range(min_wd, length - min_wd + 1):
        if pipe.weld_allowed(pos):
            out.append(pos)
    return out


def _add_weld_zone_splits(
    pool: dict[int, list[tuple[int, ...]]],
    i: int,
    pipe,
    *,
    min_wd: int,
    s_max: int,
    max_2seg: int = 16,
    max_3seg: int = 8,
) -> None:
    """Split at legal weld runs — endpoints/mid/quartiles only.

    Dense stride pollutes Σ and times out set-cover (fingerprint 1813:
    ~200 legal positions in one run; teacher uses a few run samples).
    """
    L = int(pipe.length)
    positions = _allowed_weld_positions(pipe, min_wd, L)
    if not positions:
        return

    runs: list[tuple[int, int]] = []
    start = prev = positions[0]
    for x in positions[1:]:
        if x != prev + 1:
            runs.append((start, prev))
            start = x
        prev = x
    runs.append((start, prev))

    key_pos: list[int] = []
    for a, b in runs:
        span = b - a
        cands = {a, b, (a + b) // 2}
        if span >= 8:
            cands.update((a + span // 4, a + 3 * span // 4))
        if span >= 32:
            cands.update((a + span // 8, a + 7 * span // 8))
        for pos in sorted(cands):
            if min_wd <= pos <= L - min_wd and pipe.weld_allowed(pos):
                key_pos.append(pos)
    seen: set[int] = set()
    key_pos = [p for p in key_pos if not (p in seen or seen.add(p))][:max_2seg]

    for pos in key_pos:
        left, right = pos, L - pos
        if max(left, right) > s_max:
            continue
        _add_weld(pool, i, (left, right), pipe)

    n3 = 0
    for ai, a in enumerate(key_pos):
        for b in key_pos[ai + 1 :]:
            if b - a < min_wd or L - b < min_wd:
                continue
            parts = (a, b - a, L - b)
            if max(parts) > s_max:
                continue
            before = len(pool[i])
            _add_weld(pool, i, parts, pipe)
            if len(pool[i]) > before:
                n3 += 1
                if n3 >= max_3seg:
                    return


def _add_weld(
    pool: dict[int, list[tuple[int, ...]]],
    i: int,
    segs: tuple[int, ...],
    pipe,
    *,
    max_orders: int = 2,
) -> None:
    """Add a few weld-legal orders; prefer stock-like max segment at end."""
    if not segs or sum(segs) != pipe.length:
        return
    # canonicalize: try largest piece last first
    pieces = list(segs)
    max_s = max(pieces)
    heads = [p for p in pieces if p != max_s] or pieces[:-1]
    anchors = [max_s] if pieces.count(max_s) == 1 else [pieces[-1]]

    cands: list[tuple[int, ...]] = []
    if len(segs) == 1:
        cands = [segs]
    elif len(segs) == 2:
        a, b = segs
        cands = [(a, b), (b, a)]
    elif len(segs) == 3:
        a, b, c = sorted(heads) + anchors if len(heads) == 2 else list(segs)
        # rebuild from multiset
        ms = list(segs)
        stock = max(ms)
        ms.remove(stock)
        x, y = ms[0], ms[1]
        cands = [(x, y, stock), (y, x, stock), (x, stock, y), (stock, x, y)]
    else:
        ms = list(segs)
        stock = max(ms)
        ms.remove(stock)
        from itertools import permutations

        for head in permutations(ms):
            cands.append(tuple(head) + (stock,))
            if len(cands) >= 8:
                break

    added = 0
    for seq in cands:
        if added >= max_orders:
            break
        if _weld_ok(pipe, seq) and seq not in pool[i]:
            pool[i].append(seq)
            added += 1


def _enum_fit_cuts(
    stock_len: int,
    sigma_list: list[int],
    kerfs: list[int],
    *,
    max_parts: int = 8,
    max_cuts: int = 12000,
) -> list[tuple[int, ...]]:
    """Fingerprint-first cut enum: k-copies & near-full before combinatorial fill."""
    segs = sorted({s for s in sigma_list if 0 < s <= stock_len}, reverse=True)
    out: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()

    def add(parts: list[int]) -> None:
        if not parts or sum(parts) > stock_len:
            return
        key = tuple(sorted(parts))
        if key not in seen:
            seen.add(key)
            out.append(key)

    sigma_set = set(segs)

    # Phase 0 (fingerprint-critical): k-copies & near-full packs FIRST.
    # Pure k-copies must NOT be capped by max_parts — teacher packs 12–18
    # identical remnants on short leftover bars (fingerprint cut columns).
    copy_cap = max(max_parts, stock_len // max(segs[-1], 1) if segs else max_parts)
    copy_cap = min(copy_cap, 64)
    for s in segs:
        add([s])
        kmax = min(stock_len // s, copy_cap)
        for k in range(2, kmax + 1):
            add([s] * k)
    for kerf in kerfs:
        room = stock_len - kerf
        if room <= 0:
            continue
        for a in segs:
            b = room - a
            if b in sigma_set:
                add([a, b])
        for s in segs:
            if s > room:
                continue
            kmax = min(room // s, copy_cap)
            for k in range(1, kmax + 1):
                lo = room - k * s
                if lo == 0:
                    add([s] * k)
                elif lo in sigma_set:
                    # mixed pack may exceed max_parts only for remnant+copies
                    parts = [s] * k + [lo]
                    if len(parts) <= max(max_parts, copy_cap):
                        add(parts)
                else:
                    add([s] * k)  # partial k-copies (fingerprint cut_partial)

    # Phase 1: pairs (and triples only if Σ small)
    for i, a in enumerate(segs):
        for b in segs[i:]:
            if a + b <= stock_len:
                add([a, b])
    if len(segs) <= 16:
        for i, a in enumerate(segs):
            for b in segs[i:]:
                for c in segs[i:]:
                    if a + b + c <= stock_len:
                        add([a, b, c])

    # Phase 2: DFS on hottest (largest) segs only when Σ moderate
    hot = segs[:12] if len(segs) > 12 else segs
    if len(hot) <= 16:

        def rec(i0: int, left: int, cur: list[int]) -> None:
            if cur:
                add(cur)
            if len(cur) >= max_parts or len(out) >= max_cuts:
                return
            for i in range(i0, len(hot)):
                s = hot[i]
                if s <= left:
                    rec(i, left - s, cur + [s])

        rec(0, stock_len, [])

    return out[:max_cuts]


def construct_columns(group, *, max_weld_per_pipe: int = 96) -> dict[str, Any]:
    pipes = group.pipes
    stock_qty: dict[int, int] = defaultdict(int)
    for st in group.stocks:
        stock_qty[int(st.length)] += int(st.quantity)
    stocks = sorted(stock_qty)
    if not stocks:
        return {"ok": False, "error": "no_stock"}
    s_max = max(stocks)
    min_wd = max(int(group.min_weld_distance or 0), 1)
    min_cut = max(int(group.min_cut_length or 0), 0)
    kerfs_all = _kerf_candidates(group)
    primary = _primary_kerfs(group)
    main_kerf = primary[:1]  # single main kerf to avoid 5/6 twin pollution

    weld_pool: dict[int, list[tuple[int, ...]]] = {i: [] for i in range(len(pipes))}
    meta_notes: list[str] = []

    def valid_piece(x: int, cap: int = s_max) -> bool:
        return min_wd <= x <= cap

    # Fingerprint: long-pipe weld max-seg is S_max in ~92%+ of teacher qty.
    # Half-stock anchors are rare but real; mid/near-max only when S_max scarce.
    long_demand = sum(int(p.demand) for p in pipes if int(p.length) > s_max)
    half_stocks = [
        S
        for S in stocks
        if S != s_max and int(0.45 * s_max) <= S <= int(0.55 * s_max)
    ]
    anchor_stocks = [s_max]
    for S in half_stocks:
        if S not in anchor_stocks:
            anchor_stocks.append(S)
    if long_demand > stock_qty.get(s_max, 0):
        # need secondary stock-length anchors to cover demand
        for S in sorted(stocks, reverse=True):
            if S == s_max:
                continue
            if S not in anchor_stocks:
                anchor_stocks.append(S)
            if sum(stock_qty[x] for x in anchor_stocks) >= long_demand:
                break
    meta_notes.append(
        f"anchors={anchor_stocks} long_demand={long_demand} s_max_qty={stock_qty.get(s_max, 0)}"
    )

    for i, pipe in enumerate(pipes):
        L = int(pipe.length)
        local_core: set[int] = set()

        if L <= s_max and (min_cut <= 0 or L >= min_cut):
            _add_weld(weld_pool, i, (L,), pipe)
            local_core.add(L)
            short_parts: set[int] = {L}

            def split_points(P: int, kerfs: list[int]) -> set[int]:
                pts: set[int] = set()
                for kerf in kerfs:
                    for Sp in stocks:
                        if P > Sp:
                            continue
                        gap = Sp - kerf - P
                        if valid_piece(gap) and gap < P:
                            pts.add(gap)
                        room = Sp - kerf
                        if room >= P:
                            k = room // P
                            if k >= 1:
                                lo = room - k * P
                                if valid_piece(lo) and lo < P:
                                    pts.add(lo)
                        if kerf <= 0:
                            continue
                        kmax = min(Sp // max(P, 1), 8)
                        for n in range(1, kmax + 1):
                            lo = Sp - n * kerf - n * P
                            if valid_piece(lo) and lo < P:
                                pts.add(lo)
                return pts

            # level-0: prefer main kerf; expand to all kerfs only if scarce
            pts0 = split_points(L, main_kerf)
            if len(pts0) < 2:
                pts0 |= split_points(L, kerfs_all)
            # half-bar splits (fingerprint 2200: (S-kerf)//2 + rest)
            for Sp in stocks:
                for kerf in main_kerf + [k for k in (10, 15) if k not in main_kerf]:
                    half = (Sp - kerf) // 2
                    if valid_piece(half) and half < L and valid_piece(L - half):
                        pts0.add(half)
            for pos in pts0:
                if valid_piece(L - pos):
                    _add_weld(weld_pool, i, (pos, L - pos), pipe)
                    short_parts.update((pos, L - pos))
                    local_core.update((pos, L - pos))

            # level-1: 3-seg by splitting EITHER half of a 2-seg
            # (fingerprint 2077: split the smaller 2701 → 1176+1525)
            for seq in list(weld_pool[i])[:32]:
                if len(seq) != 2:
                    continue
                a, b = seq
                for part, other in ((a, b), (b, a)):
                    if part < 2 * min_wd:
                        continue
                    for lo in split_points(part, main_kerf):
                        mid = part - lo
                        if mid < min_wd or lo < min_wd:
                            continue
                        if lo + mid + other != L:
                            continue
                        _add_weld(weld_pool, i, (lo, mid, other), pipe)
                        local_core.update((lo, mid, other))

            # Weld-zone samples only when arithmetic 2-seg is scarce
            n_multi = sum(1 for s in weld_pool[i] if len(s) >= 2)
            if n_multi < 3:
                before_wz = len(weld_pool[i])
                _add_weld_zone_splits(
                    weld_pool,
                    i,
                    pipe,
                    min_wd=min_wd,
                    s_max=s_max,
                    max_2seg=16,
                    max_3seg=8,
                )
                if len(weld_pool[i]) > before_wz:
                    meta_notes.append(
                        f"pipe{i}:weld_zone_splits +{len(weld_pool[i]) - before_wz}"
                    )
            for seq in weld_pool[i]:
                local_core.update(seq)

        # long-pipe anchors: S_max (+ half / scarcity secondaries)
        anchors = [
            S
            for S in anchor_stocks
            if S < L and valid_piece(L - S) and (L - S) <= s_max
        ]
        for S in anchors:
            R = L - S
            if not valid_piece(R):
                continue
            primary_pieces: set[int] = {R, S}
            _add_weld(weld_pool, i, (R, S), pipe)
            halves: list[int] = []
            lo_kerfs = main_kerf + [k for k in (10, 15) if k not in main_kerf]
            # Leftover arithmetic uses ALL stocks (cuts happen on secondary bars);
            # weld stock-anchor S stays from anchor_stocks only.
            leftover_pts: set[int] = set()

            for kerf in lo_kerfs:
                for Sp in stocks:
                    room = Sp - kerf
                    if room >= R:
                        k = room // R
                        if k >= 1:
                            lo = room - k * R
                            if valid_piece(lo) and lo < R and valid_piece(R - lo):
                                leftover_pts.add(lo)
                                primary_pieces.update((lo, R - lo))
                                _add_weld(weld_pool, i, (lo, R - lo, S), pipe)
                    kmax = Sp // max(R, 1)
                    for k in range(1, min(kmax, 6) + 1):
                        lo = Sp - k * kerf - k * R
                        if valid_piece(lo) and lo < R and valid_piece(R - lo):
                            leftover_pts.add(lo)
                            primary_pieces.update((lo, R - lo))
                            _add_weld(weld_pool, i, (lo, R - lo, S), pipe)
            # Fingerprint: 2·lo (and 3·lo) of a leftover often forms another 3-seg
            for lo in list(leftover_pts):
                for m in (2, 3):
                    piece = m * lo
                    if piece < R and valid_piece(piece) and valid_piece(R - piece):
                        primary_pieces.update((piece, R - piece))
                        _add_weld(weld_pool, i, (piece, R - piece, S), pipe)
            for kerf in main_kerf:
                half = (S - kerf) // 2
                if valid_piece(half) and half < R and valid_piece(R - half):
                    primary_pieces.update((half, R - half))
                    if half not in halves:
                        halves.append(half)
                    _add_weld(weld_pool, i, (half, R - half, S), pipe)

            base = set(primary_pieces)
            for seg in list(base):
                for Sp in stocks:
                    for kerf in lo_kerfs:
                        c = Sp - kerf - seg
                        if not valid_piece(c, Sp) or c in primary_pieces:
                            continue
                        if c < R and valid_piece(R - c):
                            primary_pieces.update((c, R - c))
                            _add_weld(weld_pool, i, (c, R - c, S), pipe)
                        # second-order: Sp - kerf - n*R - seg
                        for n_r in (1, 2):
                            lo2 = Sp - kerf - n_r * R - seg
                            if valid_piece(lo2) and lo2 < R and valid_piece(R - lo2):
                                primary_pieces.update((lo2, R - lo2))
                                _add_weld(weld_pool, i, (lo2, R - lo2, S), pipe)

            core_q = {x for x in primary_pieces if x not in (R, S) and x < R}
            four_ms: dict[tuple[int, ...], tuple[int, ...]] = {}
            for kerf in [k for k in kerfs_all if k in main_kerf or k in (10,)]:
                for Sp in anchor_stocks:
                    room = Sp - kerf
                    if room <= R:
                        continue
                    for q in list(core_q):
                        if q <= 0 or R + q > room:
                            continue
                        lo2 = room - R - q
                        if not valid_piece(lo2) or lo2 >= R or lo2 == q:
                            continue
                        for half in halves[:1]:
                            rem = R - half
                            if lo2 >= rem:
                                continue
                            other = rem - lo2
                            if not valid_piece(other):
                                continue
                            seq = (lo2, half, other, S)
                            ms = tuple(sorted(seq))
                            if ms not in four_ms:
                                four_ms[ms] = seq
                            if len(four_ms) >= 12:
                                break
                        if len(four_ms) >= 12:
                            break
                    if len(four_ms) >= 12:
                        break
                if len(four_ms) >= 12:
                    break
            for seq in four_ms.values():
                _add_weld(weld_pool, i, seq, pipe)

            local_core |= primary_pieces

        uniq: dict[tuple[int, ...], tuple[int, ...]] = {}
        for seq in weld_pool[i]:
            ms = tuple(sorted(seq))
            if ms not in uniq:
                uniq[ms] = seq

        core3: set[int] = set()
        for seq in uniq.values():
            if len(seq) <= 3:
                core3.update(seq)
        threes = [seq for seq in uniq.values() if len(seq) <= 3]
        fours = []
        for seq in uniq.values():
            if len(seq) != 4:
                continue
            stock = max(seq)
            rem = [x for x in seq if x != stock]
            if stock not in stock_qty or sum(rem) != L - stock:
                continue
            if not any(x in core3 for x in rem):
                continue
            score = 0
            Rrem = L - stock
            for kerf in (5, 6, 10, 15):
                for Sp in anchor_stocks:
                    for q in core3:
                        if q <= 0:
                            continue
                        lo2 = Sp - kerf - Rrem - q
                        if lo2 in rem and (Rrem - lo2) in rem:
                            score += 10 if kerf == 10 else 3
            fours.append((score, seq))
        fours_kept = [seq for _, seq in sorted(fours, key=lambda t: -t[0])[:1]]

        def keep_key(seq: tuple[int, ...]):
            core_hit = sum(1 for x in seq if x in local_core)
            return (0 if len(seq) <= 3 else 1, len(seq) - 1, -core_hit, seq)

        # Long pipes need more 3-seg kerf variants; short pipes stay tighter.
        cap = max_weld_per_pipe if L <= s_max else max(max_weld_per_pipe, 64)
        weld_pool[i] = sorted(threes + fours_kept, key=keep_key)[:cap]

        if not weld_pool[i]:
            meta_notes.append(f"pipe{i}:no_weld_patterns L={L}")

    # Cross-pipe pack leftovers (fingerprint 2245/2651/2461)
    one_seg_pieces = sorted(
        {
            int(pipes[i].length)
            for i, pats in weld_pool.items()
            if any(len(seq) == 1 for seq in pats)
        }
    )
    cross_kerfs = main_kerf + [k for k in (10, 15, 20) if k not in main_kerf]
    for i, pipe in enumerate(pipes):
        L = int(pipe.length)
        if L > s_max:
            continue
        cross_added = 0
        cross_cap = 12
        donors = sorted(one_seg_pieces, key=lambda P: (abs(P - L), P))
        for P in donors:
            if cross_added >= cross_cap:
                break
            for Sp in stocks:
                if P > Sp:
                    continue
                kmax = Sp // max(P, 1)
                for k in range(1, min(kmax, 8) + 1):
                    if cross_added >= cross_cap:
                        break
                    for kerf in cross_kerfs:
                        rem = Sp - kerf - k * P
                        candidates = {rem, rem + k * (P - L), rem + (P - L)}
                        for lo in candidates:
                            if not valid_piece(lo) or lo >= L:
                                continue
                            other = L - lo
                            if valid_piece(other):
                                before = len(weld_pool[i])
                                _add_weld(weld_pool, i, (lo, other), pipe)
                                if len(weld_pool[i]) > before:
                                    cross_added += 1
                                    if cross_added >= cross_cap:
                                        break

    # Cap per-pipe again after cross-pipe adds
    for i, pipe in enumerate(pipes):
        if len(weld_pool[i]) > max_weld_per_pipe:
            def rank(seq: tuple[int, ...]):
                stock_hit = sum(1 for x in seq if x in stock_qty)
                return (len(seq), -stock_hit, -max(seq), seq)

            L = int(pipes[i].length)
            cap = 64 if L <= s_max else max_weld_per_pipe
            weld_pool[i] = sorted(weld_pool[i], key=rank)[:cap]

    # rebuild sigma strictly from final weld pools
    sigma: set[int] = set()
    for pats in weld_pool.values():
        for seq in pats:
            sigma.update(seq)
    sigma_list = sorted(sigma)

    # cut alphabet = weld pieces (+ raw short pipes)
    cut_set: set[int] = set(sigma)
    cut_set |= {int(p.length) for p in pipes if int(p.length) <= s_max}
    cut_list = sorted(cut_set)
    max_parts = 8 if len(cut_list) <= 40 else 6

    cut_pool: dict[int, list[tuple[int, ...]]] = {
        L: _enum_fit_cuts(
            L,
            cut_list,
            kerfs_all,
            max_parts=max_parts,
            max_cuts=12000,
        )
        for L in stocks
    }

    # Fingerprint-critical: each weld's local alphabet must get its own cut enum.
    # Large global Σ starves DFS of mixed packs like (948×4, 2721×3).
    local_alphabets: set[tuple[int, ...]] = set()
    for pats in weld_pool.values():
        for seq in pats:
            local_alphabets.add(tuple(sorted(set(seq))))
    for alpha in sorted(local_alphabets, key=lambda a: (len(a), a))[:64]:
        a_list = list(alpha)
        if len(a_list) <= 1:
            continue
        for L in stocks:
            extra = _enum_fit_cuts(
                L,
                a_list,
                kerfs_all,
                max_parts=8,
                max_cuts=4000,
            )
            seen = set(cut_pool[L])
            for pat in extra:
                if pat not in seen:
                    cut_pool[L].append(pat)
                    seen.add(pat)

    for L, cps in cut_pool.items():
        have = {s for segs in cps for s in segs}
        for s in sigma_list:
            if s <= L and s not in have:
                cps.append((s,))
                have.add(s)

    n_weld = sum(len(v) for v in weld_pool.values())
    n_cut = sum(len(v) for v in cut_pool.values())
    missing = [i for i, v in weld_pool.items() if not v]
    return {
        "ok": len(missing) == 0,
        "error": None if not missing else f"no_weld_for_pipes={missing}",
        "weld_pool": weld_pool,
        "cut_pool": cut_pool,
        "sigma": sigma_list,
        "stock_qty": dict(stock_qty),
        "n_weld": n_weld,
        "n_cut": n_cut,
        "notes": meta_notes[:50],
    }


def solve_setcover(group, constructed: dict, *, tl: float = 20.0) -> dict[str, Any]:
    from pyscipopt import Model, quicksum

    if not constructed.get("ok"):
        return {"ok": False, "error": constructed.get("error")}

    pipes = group.pipes
    weld_pool = constructed["weld_pool"]
    cut_pool = constructed["cut_pool"]
    stock_qty = constructed["stock_qty"]
    sigma = constructed["sigma"]

    t0 = time.monotonic()
    m = Model("teacher_inspired_v0")
    m.hideOutput()
    m.setParam("limits/time", tl)

    xp = {}
    for i, pats in weld_pool.items():
        for k, seq in enumerate(pats):
            xp[(i, k)] = m.addVar(vtype="I", lb=0, name=f"xp_{i}_{k}")
        m.addCons(quicksum(xp[(i, k)] for k in range(len(pats))) == pipes[i].demand)

    yc = {}
    for L, cps in cut_pool.items():
        for k, segs in enumerate(cps):
            yc[(L, k)] = m.addVar(vtype="I", lb=0, name=f"yc_{L}_{k}")
        if cps:
            m.addCons(
                quicksum(yc[(L, k)] for k in range(len(cps))) <= stock_qty.get(L, 0)
            )

    for seg in sigma:
        prod = [
            segs.count(seg) * yc[(L, k)]
            for L, cps in cut_pool.items()
            for k, segs in enumerate(cps)
            if seg in segs
        ]
        cons = [
            seq.count(seg) * xp[(i, k)]
            for i, pats in weld_pool.items()
            for k, seq in enumerate(pats)
            if seg in seq
        ]
        if cons:
            if not prod:
                return {
                    "ok": False,
                    "error": f"seg_{seg}_no_cut",
                    "elapsed_s": round(time.monotonic() - t0, 3),
                }
            m.addCons(quicksum(prod) - quicksum(cons) >= 0)

    joints = quicksum(
        (len(seq) - 1) * xp[(i, k)]
        for i, pats in weld_pool.items()
        for k, seq in enumerate(pats)
    )
    bars = quicksum(yc[(L, k)] for L, cps in cut_pool.items() for k in range(len(cps)))
    m.setObjective(joints * 100000 + bars, "minimize")
    m.optimize()
    dt = time.monotonic() - t0
    if m.getNSols() == 0:
        return {
            "ok": False,
            "error": f"status={m.getStatus()}",
            "elapsed_s": round(dt, 3),
            "n_weld": constructed["n_weld"],
            "n_cut": constructed["n_cut"],
            "sigma_n": len(sigma),
        }

    used = 0.0
    cut_types_used = 0
    segs_used: set[int] = set()
    for L, cps in cut_pool.items():
        for k, segs in enumerate(cps):
            val = m.getVal(yc[(L, k)])
            if val > 0.5:
                used += L * val
                cut_types_used += 1
                segs_used.update(segs)

    weld_types_used = 0
    for i, pats in weld_pool.items():
        for k, seq in enumerate(pats):
            if m.getVal(xp[(i, k)]) > 0.5:
                weld_types_used += 1
                segs_used.update(seq)

    util = group.demand_length / used if used else 0.0
    joints_v = float(m.getVal(joints))
    bars_v = float(m.getVal(bars))
    return {
        "ok": True,
        "joints": joints_v,
        "bars": bars_v,
        "util": util,
        "used_len": used,
        "weld_types": weld_types_used,
        "cut_types": cut_types_used,
        "seg_types": len(segs_used),
        "solver": "fingerprint_setcover",
        "elapsed_s": round(dt, 3),
        "n_weld": constructed["n_weld"],
        "n_cut": constructed["n_cut"],
        "sigma_n": len(sigma),
    }


def solve_fingerprint_setcover(group, *, tl: float = 15.0) -> dict[str, Any] | None:
    """V4 可调用的指纹集合覆盖入口；失败返回 None。"""
    cons = construct_columns(group)
    sol = solve_setcover(group, cons, tl=tl)
    if not sol.get("ok"):
        return None
    out = {
        "joints": int(round(float(sol["joints"]))),
        "weld_types": int(sol.get("weld_types") or 0),
        "cut_types": int(sol.get("cut_types") or 0),
        "seg_types": int(sol.get("seg_types") or 0),
        "util": float(sol["util"]),
        "used_len": float(sol.get("used_len") or 0),
        "bars": float(sol.get("bars") or 0),
        "solver": "fingerprint_setcover",
        "solver_path": ["fingerprint_setcover"],
        "elapsed_s": sol.get("elapsed_s"),
        "diagnosis": [
            f"sigma={sol.get('sigma_n')}",
            f"n_weld={sol.get('n_weld')}",
            f"n_cut={sol.get('n_cut')}",
        ],
    }
    return out


def fail_indices() -> list[int]:
    rows = json.loads((D2_COMPARE / "metrics_table.json").read_text(encoding="utf-8"))
    return [
        int(r["idx"])
        for r in rows
        if str(r.get("verdict")) in ("ERROR:NO_SOLUTION", "TIMEOUT")
        or str(r.get("verdict")).startswith("ERROR")
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-fails", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tl", type=float, default=20.0)
    ap.add_argument("--idx", type=int, default=-1)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.idx >= 0:
        idxs = [args.idx]
    elif args.probe_fails:
        idxs = fail_indices()
    else:
        raise SystemExit("use --probe-fails or --idx")
    if args.limit > 0:
        idxs = idxs[: args.limit]

    rows = []
    ok_n = 0
    print(f"teacher-inspired v0 probe n={len(idxs)} tl={args.tl}", flush=True)
    for n, idx in enumerate(idxs, 1):
        case = load_legacy_case(CASES, idx)
        g = merge_equivalent_pipes(
            parse_problem(clean_mom_problem(case["problem"])).groups[0]
        )
        lg = ((case.get("metrics") or {}).get("metrics") or {})
        cons = construct_columns(g)
        sol = solve_setcover(g, cons, tl=args.tl)
        row = {
            "idx": idx,
            "construct_ok": cons.get("ok"),
            "sigma_n": len(cons.get("sigma") or []),
            "n_weld": cons.get("n_weld"),
            "n_cut": cons.get("n_cut"),
            "legacy_joints": lg.get("welding_joints"),
            "legacy_util": lg.get("utilization_rate"),
            **{
                k: sol.get(k)
                for k in ("ok", "joints", "bars", "util", "error", "elapsed_s")
            },
        }
        rows.append(row)
        if sol.get("ok"):
            ok_n += 1
        print(
            f"[{n}/{len(idxs)}] idx={idx} ok={sol.get('ok')} "
            f"j={sol.get('joints')}/{lg.get('welding_joints')} "
            f"u={None if sol.get('util') is None else round(sol['util'], 4)}/"
            f"{None if lg.get('utilization_rate') is None else round(lg['utilization_rate'], 4)} "
            f"sigma={row['sigma_n']} nw={cons.get('n_weld')} nc={cons.get('n_cut')} "
            f"err={sol.get('error')}",
            flush=True,
        )

    summary = {
        "n": len(rows),
        "ok": ok_n,
        "fail": len(rows) - ok_n,
        "ok_rate": ok_n / len(rows) if rows else 0.0,
        "rows": rows,
    }
    out_path = OUT / "teacher_inspired_v0_probe.json"
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== DONE ===", flush=True)
    print(json.dumps({k: summary[k] for k in ("n", "ok", "fail", "ok_rate")}, indent=2))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
