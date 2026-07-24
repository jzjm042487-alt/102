"""D5 列工厂 v1.7：motif 焊模板 + base 切过滤 + 全 sigma k-copy。"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.teacher_inspired_construct_v0 import (
    _add_weld,
    _enum_fit_cuts,
    _kerf_candidates,
    _primary_kerfs,
    _weld_ok,
    construct_columns,
)

ROOT = Path(__file__).resolve().parents[1]
MOTIF_PATH = ROOT / "data" / "acceptance" / "d5_motif_dict" / "motif_dict.json"
_MOTIF_CACHE: dict[str, Any] | None = None


def load_motif_dict() -> dict[str, Any]:
    global _MOTIF_CACHE
    if _MOTIF_CACHE is not None:
        return _MOTIF_CACHE
    if MOTIF_PATH.exists():
        _MOTIF_CACHE = json.loads(MOTIF_PATH.read_text(encoding="utf-8"))
    else:
        _MOTIF_CACHE = {"remnant_alphabet": [], "cut_nearfill": [], "cut_piece_templates": []}
    return _MOTIF_CACHE


def _nearfill_for_stocks(motif: dict[str, Any], stocks: set[int]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in (motif.get("cut_nearfill") or [])
        if int(row.get("L") or 0) in stocks or int(row.get("L") or 0) == 0
    ]
    return sorted(rows, key=lambda r: -int(r.get("n") or 0))


def _partner_lengths(
    nf_rows: list[dict[str, Any]],
    stock_qty: dict[int, int],
    *,
    min_wd: int,
    s_max: int,
    pipe_lens: set[int],
    per_stock: int = 10,
    cap: int = 64,
) -> list[int]:
    by_L: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in nf_rows:
        by_L[int(row.get("L") or 0)].append(row)
    out: list[int] = []
    # Remnants paired with this group's actual pipe lengths (legacy-critical)
    for row in nf_rows:
        a, b = int(row["a"]), int(row["b"])
        if a in pipe_lens and min_wd <= b <= s_max:
            out.append(b)
        if b in pipe_lens and min_wd <= a <= s_max:
            out.append(a)
        if a in pipe_lens and min_wd <= a <= s_max:
            out.append(a)
        if b in pipe_lens and min_wd <= b <= s_max:
            out.append(b)
    for Lf, rows in by_L.items():
        if Lf and Lf not in stock_qty:
            continue
        for row in rows[:per_stock]:
            for k in ("a", "b"):
                v = int(row[k])
                if min_wd <= v <= s_max:
                    out.append(v)
    return list(dict.fromkeys(out))[:cap]


def _refine_welds(
    group,
    weld_pool: dict[int, list[tuple[int, ...]]],
    stock_qty: dict[int, int],
    partners: list[int],
    motif_weld_by_sum: dict[int, list[tuple[int, ...]]],
    *,
    cap: int,
) -> list[str]:
    notes: list[str] = []
    stocks = sorted(stock_qty)
    s_max = max(stocks)
    min_wd = max(int(group.min_weld_distance or 0), 1)
    pipe_lens = sorted({int(p.length) for p in group.pipes})
    partner_set = set(partners)
    notes.append(f"partners={len(partners)} weld_tmpl_sums={len(motif_weld_by_sum)}")

    def valid(x: int) -> bool:
        return min_wd <= x <= s_max

    for i, pipe in enumerate(group.pipes):
        L = int(pipe.length)
        # stock anchors: always for long pipes; gated for short pipes
        for S in stocks:
            R = L - S
            if not (S < L and valid(R)):
                continue
            if L > s_max or R in partner_set or R in pipe_lens or R <= max(800, s_max // 8):
                _add_weld(weld_pool, i, (R, S), pipe)
        # partner splits
        for r in partners:
            other = L - r
            if valid(r) and r < L and valid(other):
                _add_weld(weld_pool, i, (r, other), pipe)
        # motif weld templates matching this pipe length
        for segs in motif_weld_by_sum.get(L, [])[:40]:
            if segs and sum(segs) == L and all(valid(x) for x in segs):
                _add_weld(weld_pool, i, segs, pipe)
        if L <= s_max:
            _add_weld(weld_pool, i, (L,), pipe)
        else:
            for a_i, Sa in enumerate(stocks):
                for Sb in stocks[a_i:]:
                    rem = L - Sa - Sb
                    if valid(rem):
                        _add_weld(weld_pool, i, (rem, Sa, Sb), pipe)
            n4 = 0
            for a_i, Sa in enumerate(stocks):
                for b_i, Sb in enumerate(stocks[a_i:], start=a_i):
                    for Sc in stocks[b_i:]:
                        rem = L - Sa - Sb - Sc
                        if not valid(rem):
                            continue
                        _add_weld(weld_pool, i, (rem, Sa, Sb, Sc), pipe)
                        n4 += 1
                        if n4 >= 20:
                            break
                    if n4 >= 20:
                        break
                if n4 >= 20:
                    break

        uniq: dict[tuple[int, ...], tuple[int, ...]] = {}
        for seq in weld_pool.get(i) or []:
            if sum(seq) != L or any(s > s_max or s < min_wd for s in seq):
                continue
            if len(seq) >= 2 and not _weld_ok(pipe, seq):
                continue
            ms = tuple(sorted(seq))
            if ms not in uniq:
                uniq[ms] = seq
        if not uniq and L > s_max:
            for S in sorted(stocks, reverse=True):
                R = L - S
                if valid(R):
                    uniq[tuple(sorted((R, S)))] = (R, S)
                    break
        if not uniq and L <= s_max:
            uniq[(L,)] = (L,)

        exclusive = L + (min(pipe_lens) if pipe_lens else 0) > s_max
        motif_set = {tuple(sorted(s)) for s in motif_weld_by_sum.get(L, [])}

        def keep_candidate(seq: tuple[int, ...]) -> bool:
            if len(seq) == 1:
                return True
            if L > s_max:
                return True  # long pipes need full multi-seg diversity
            if max(seq) in stock_qty:
                return True
            if any(x in partner_set for x in seq):
                return True
            if tuple(sorted(seq)) in motif_set:
                return True
            if len(seq) == 2 and min(seq) <= max(800, s_max // 8):
                return True
            return False

        filtered = {k: v for k, v in uniq.items() if keep_candidate(v)}
        if not filtered:
            filtered = uniq

        def rank(seq: tuple[int, ...]):
            pen = 40 if (exclusive and len(seq) == 1) else 0
            hit = sum(1 for x in seq if x in partner_set)
            mot = 0 if tuple(sorted(seq)) in motif_set else 1
            return (
                pen,
                mot,
                -hit,
                0 if max(seq) in stock_qty else 1,
                len(seq),
                -sum(1 for x in seq if x in stock_qty),
                seq,
            )

        floor = [s for s in filtered.values() if len(s) >= 2 and max(s) in stock_qty]
        partner_splits = [
            s
            for s in filtered.values()
            if len(s) == 2 and (s[0] in partner_set or s[1] in partner_set)
        ]
        motif_hits = [s for s in filtered.values() if tuple(sorted(s)) in motif_set]
        ones = [s for s in filtered.values() if len(s) == 1]
        kept = sorted(
            (s for s in filtered.values() if len(s) != 1),
            key=rank,
        )[:cap]
        ordered: list[tuple[int, ...]] = []
        seen_m: set[tuple[int, ...]] = set()
        for s in ones + motif_hits[:30] + floor + partner_splits[:24] + kept:
            ms = tuple(sorted(s))
            if ms in seen_m:
                continue
            seen_m.add(ms)
            ordered.append(s)
        if L > s_max:
            ordered = [s for s in ordered if len(s) >= 2] or ordered
        weld_pool[i] = ordered[: max(cap, len(floor) + len(ones) + len(motif_hits), 24)]
        if not weld_pool[i]:
            notes.append(f"pipe{i}:EMPTY L={L}")
    return notes


def construct_d5_columns(group, *, max_weld_per_pipe: int = 32) -> dict[str, Any]:
    motif = load_motif_dict()
    notes: list[str] = []
    stock_qty: dict[int, int] = defaultdict(int)
    for st in group.stocks:
        stock_qty[int(st.length)] += int(st.quantity)
    nf_rows = _nearfill_for_stocks(motif, set(stock_qty))
    s_max = max(stock_qty)
    min_wd = max(int(group.min_weld_distance or 0), 1)
    pipe_lens = {int(p.length) for p in group.pipes}
    partners = _partner_lengths(
        nf_rows,
        stock_qty,
        min_wd=min_wd,
        s_max=s_max,
        pipe_lens=pipe_lens,
        per_stock=10,
        cap=64,
    )
    motif_weld_by_sum: dict[int, list[tuple[int, ...]]] = defaultdict(list)
    for row in motif.get("weld_pattern_templates") or []:
        segs = tuple(int(x) for x in (row.get("segs") or []))
        if len(segs) < 2:
            continue
        motif_weld_by_sum[int(row.get("sum") or sum(segs))].append(segs)

    base = construct_columns(group, max_weld_per_pipe=max_weld_per_pipe)
    if base.get("ok"):
        weld_pool = {i: list(pats) for i, pats in base["weld_pool"].items()}
        notes.extend(list(base.get("notes") or [])[:4])
    else:
        weld_pool = {i: [] for i in range(len(group.pipes))}
        notes.append(f"base_incomplete:{base.get('error')}")

    notes.extend(
        _refine_welds(
            group, weld_pool, stock_qty, partners, motif_weld_by_sum, cap=max_weld_per_pipe
        )
    )

    full_sigma: set[int] = set()
    for pats in weld_pool.values():
        for seq in pats:
            full_sigma.update(seq)
    short_pipes = {int(p.length) for p in group.pipes if int(p.length) <= s_max}
    full_sigma |= short_pipes
    sigma_list = sorted(full_sigma)
    notes.append(f"sigma={len(full_sigma)}")

    # Cuts: filter teacher base cut pool to weld sigma (keeps packing k-copies),
    # then add motif nearfill + light complements.
    primary = _primary_kerfs(group)
    kerfs = _kerf_candidates(group)
    hot = sorted(full_sigma, reverse=True)[:40]
    small = sorted(s for s in full_sigma if s <= s_max // 2)[:28]
    base_cuts = base.get("cut_pool") if base.get("ok") else {}

    cut_pool: dict[int, list[tuple[int, ...]]] = {}
    for L in sorted(stock_qty):
        seen: set[tuple[int, ...]] = set()
        cps: list[tuple[int, ...]] = []

        def add(segs: tuple[int, ...]) -> None:
            if not segs or sum(segs) > L:
                return
            if any(s not in full_sigma for s in segs):
                return
            key = tuple(sorted(segs))
            if key in seen:
                return
            seen.add(key)
            cps.append(segs)

        for segs in base_cuts.get(L) or []:
            add(tuple(int(x) for x in segs))

        for row in nf_rows:
            if int(row.get("L") or 0) not in (0, L):
                continue
            a, b = int(row["a"]), int(row["b"])
            if a in full_sigma and b in full_sigma and a + b <= L:
                add((a, b))

        for row in (motif.get("cut_piece_templates") or [])[:1500]:
            segs = tuple(int(x) for x in (row.get("segs") or []))
            if segs and sum(segs) <= L and all(s in full_sigma for s in segs):
                add(segs)

        for s in sigma_list:
            if s <= L:
                add((s,))
                # k-copies of every weld seg (e.g. legacy 4851×2 packs)
                kmax = min(L // s, 12)
                for k in range(2, kmax + 1):
                    add(tuple([s] * k))

        enum_sigma = (
            sigma_list
            if len(sigma_list) <= 180
            else sorted(
                x
                for x in (set(hot) | set(small) | set(partners) | short_pipes)
                if x in full_sigma
            )
        )
        for i, a in enumerate(enum_sigma):
            for b in enum_sigma[i:]:
                if a + b <= L and (L - a - b) <= 80:
                    add((a, b))
        # mixed: k-copies + one other nearfill partner
        for s in enum_sigma:
            for k in range(1, min(L // max(s, 1), 8) + 1):
                rem = L - k * s
                if rem in full_sigma and rem > 0:
                    add(tuple([s] * k + [rem]))
                for kerf in primary[:2] + [0, 5]:
                    remk = L - kerf - k * s
                    if remk in full_sigma and remk > 0:
                        add(tuple([s] * k + [remk]))
        for kerf in primary[:3] + [0, 5, 10]:
            for a in enum_sigma:
                b = L - kerf - a
                if b > 0 and b in full_sigma:
                    add((a, b))
        alphabet = sorted(set(hot) | set(small) | set(enum_sigma), reverse=True)[:48]
        for pat in _enum_fit_cuts(L, alphabet, kerfs, max_parts=8, max_cuts=800):
            add(pat)

        near = [p for p in cps if len(p) >= 2 and L - sum(p) <= 80]
        multi = [p for p in cps if len(p) >= 2 and p not in near]
        singles = [p for p in cps if len(p) == 1]
        multi = sorted(multi, key=lambda s: (L - sum(s), len(s)))[:800]
        cut_pool[L] = near[:800] + multi + singles

    missing = [i for i, v in weld_pool.items() if not v]
    n_weld = sum(len(v) for v in weld_pool.values())
    n_cut = sum(len(v) for v in cut_pool.values())
    notes.append(f"n_weld={n_weld} n_cut={n_cut}")
    return {
        "ok": len(missing) == 0,
        "error": None if not missing else f"no_weld_for_pipes={missing}",
        "weld_pool": weld_pool,
        "cut_pool": cut_pool,
        "sigma": sigma_list,
        "stock_qty": dict(stock_qty),
        "n_weld": n_weld,
        "n_cut": n_cut,
        "notes": notes[:80],
        "factory": "d5_motif_v1_7",
    }
