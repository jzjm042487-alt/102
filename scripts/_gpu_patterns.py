"""GPU-accelerated batch pattern generation for the equiv-stock CSP (Route-2).

Mirrors the CPU DFS in ``_demo_route2_equiv._enumerate_equiv_cutplans`` exactly,
but replaces per-multiset recursion with a vectorized "enumerate ordered
type-sequences of length k, then mask illegal ones" scheme that GPUs excel at.

Semantics reproduced 1:1 with the CPU enumerator:
  * equivalent stock = bars welded head-to-tail; boundary i sits at
    ``Σ_{j<=i} bar_j - i*kerf`` (kerf consumed at each internal join);
  * pipes laid head-to-tail, kerf between consecutive cuts;
  * pipe placed at [start, end): welds = # internal bar-boundaries strictly
    inside; legal iff welds<=max_joints, every stub>=min_seg, and every weld's
    position relative to the pipe start is outside all its forbidden zones;
  * a plan is any prefix-legal ordered chain; dedup by SORTED chain (multiset).

Only NumPy/CuPy array ops on the (large) column axis; the tiny axes (boundary
count, forbidden-interval count, k) are looped in Python.
"""
from __future__ import annotations

from typing import Any


def _xp(use_gpu: bool):
    if use_gpu:
        import cupy as cp  # type: ignore[import-not-found]
        return cp
    import numpy as np
    return np


def enumerate_equiv_cutplans_gpu(
    bar_lengths: tuple[int, ...],
    kerf: int,
    types: list[dict[str, Any]],
    min_seg: int,
    per_plan_cap: int,
    *,
    use_gpu: bool = True,
    seq_batch_cap: int = 4_000_000,
) -> list[dict[str, Any]]:
    """Vectorized equivalent of the CPU enumerator. Returns identical column set
    (order may differ; caller dedups by sorted chain anyway).

    ``seq_batch_cap`` bounds the number of ordered sequences materialised at any
    length k so a wide type set cannot blow VRAM; when exceeded we fall back to
    the recursive CPU enumerator for that stock (correctness over speed).
    """
    xp = _xp(use_gpu)
    n_types = len(types)
    if n_types == 0:
        return []

    # ---- boundaries + usable length (host scalars) ----
    boundaries: list[int] = []
    pos = 0
    for i, b in enumerate(bar_lengths):
        pos += b - (kerf if i > 0 else 0)
        boundaries.append(pos)
    total_usable = boundaries[-1]
    total_material = sum(bar_lengths)
    inner_bnds = boundaries[:-1]  # candidate internal weld positions (global)

    lengths = xp.asarray([t["length"] for t in types], dtype=xp.int64)
    max_joints = xp.asarray([t["max_joints"] for t in types], dtype=xp.int64)
    inner_arr = xp.asarray(inner_bnds, dtype=xp.int64) if inner_bnds else None

    # Per-type forbidden zones as padded (T, F, 2) with sentinel rows disabled.
    max_fb = max((len(t["forbidden"]) for t in types), default=0)
    if max_fb:
        fb_lo = xp.full((n_types, max_fb), 1, dtype=xp.int64)
        fb_hi = xp.full((n_types, max_fb), 0, dtype=xp.int64)  # lo>hi => empty
        for ti, t in enumerate(types):
            for fi, (s, e) in enumerate(t["forbidden"]):
                fb_lo[ti, fi] = s
                fb_hi[ti, fi] = e
    else:
        fb_lo = fb_hi = None

    # Max pipes that could ever fit (shortest type spanning the stock).
    min_len = int(lengths.min())
    max_k = max(1, (total_usable + kerf) // (min_len + kerf))

    # ``chains`` accumulates legal ordered sequences; we grow one type at a time.
    # Each row is a full legal plan (prefix legality guaranteed by construction).
    results: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()

    # start positions of the current frontier sequences (host->device),
    # ``seqs`` shape (N, depth) of type indices, ``starts`` shape (N,) next cursor.
    # Depth 1 seed:
    seqs = xp.arange(n_types, dtype=xp.int64).reshape(-1, 1)
    starts = xp.zeros(n_types, dtype=xp.int64)  # each pipe starts at cursor 0

    def _legal_placement(seq_types, start):
        """Vectorized legality of placing seq_types[:, -1] at [start, start+len).
        Returns (keep_mask, welds, new_cursor)."""
        ti = seq_types[:, -1]
        plen = lengths[ti]
        end = start + plen
        fits = end <= total_usable
        if inner_arr is None:
            welds = xp.zeros(seq_types.shape[0], dtype=xp.int64)
            keep = fits
            return keep, welds, end + kerf
        # crossings: boundaries strictly inside (start, end)  -> shape (N, B)
        inside = (inner_arr[None, :] > start[:, None]) & (
            inner_arr[None, :] < end[:, None]
        )
        welds = inside.sum(axis=1)
        keep = fits & (welds <= max_joints[ti])
        # min_seg: every stub between consecutive cut points >= min_seg.
        # Build cut positions per row: [start, inner*, end]; because inner set
        # differs per row we test via gaps: sort not needed (boundaries sorted).
        # stub-ok iff min gap over {start->first_inner, between inners, last->end}
        # We compute using masked boundary positions.
        # For rows with welds==0 stub check trivially passes.
        # position matrix with -inf where not inside:
        big = total_usable + kerf + 1
        bpos = xp.where(inside, inner_arr[None, :], big)  # non-inside pushed high
        bpos_sorted = xp.sort(bpos, axis=1)
        # first stub: first_inner - start ; use min inside boundary
        has_weld = welds > 0
        first_inner = bpos_sorted[:, 0]
        stub_first = xp.where(has_weld, first_inner - start, min_seg)
        # last stub: end - last_inner ; last inside = max of inside positions
        low = xp.where(inside, inner_arr[None, :], -1)
        last_inner = low.max(axis=1)
        stub_last = xp.where(has_weld, end - last_inner, min_seg)
        # middle stubs: consecutive diffs among sorted inside positions
        diffs = bpos_sorted[:, 1:] - bpos_sorted[:, :-1]
        both_inside = (bpos_sorted[:, 1:] < big) & (bpos_sorted[:, :-1] < big)
        mid_ok = xp.where(both_inside, diffs >= min_seg, True).all(axis=1) \
            if diffs.size else xp.ones(seq_types.shape[0], dtype=bool)
        stub_ok = (stub_first >= min_seg) & (stub_last >= min_seg) & mid_ok
        keep = keep & (stub_ok | ~has_weld)
        # forbidden: rel = bnd - start must be outside every zone of ti.
        if fb_lo is not None:
            rel = xp.where(inside, inner_arr[None, :] - start[:, None], -1)  # (N,B)
            lo = fb_lo[ti]  # (N, F)
            hi = fb_hi[ti]  # (N, F)
            # hit if any inside boundary rel in [lo,hi] for any zone
            # (N, B, F): rel[:,:,None] within [lo[:,None,:], hi[:,None,:]]
            in_zone = (
                (rel[:, :, None] >= lo[:, None, :])
                & (rel[:, :, None] <= hi[:, None, :])
                & inside[:, :, None]
            )
            forbidden_hit = in_zone.any(axis=(1, 2))
            keep = keep & ~forbidden_hit
        return keep, welds, end + kerf

    # joints accumulator per frontier row
    joints_acc = xp.zeros(n_types, dtype=xp.int64)
    # validate depth-1 placements
    keep, welds, next_cursor = _legal_placement(seqs, starts)
    seqs = seqs[keep]
    joints_acc = welds[keep]
    starts = next_cursor[keep]

    depth = 1
    fell_back = False
    while seqs.shape[0] > 0 and depth <= max_k:
        # emit current frontier as plans (dedup by sorted chain on host)
        seqs_host = seqs.get() if use_gpu else seqs
        joints_host = (joints_acc.get() if use_gpu else joints_acc)
        for row_idx in range(seqs_host.shape[0]):
            chain = tuple(int(v) for v in seqs_host[row_idx])
            key = tuple(sorted(chain))
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "parts": key,
                "layout": chain,
                "joints": int(joints_host[row_idx]),
                "material": total_material,
            })
            if len(results) >= per_plan_cap:
                return results

        # expand frontier: append every type to every surviving sequence
        n = seqs.shape[0]
        if n * n_types > seq_batch_cap:
            fell_back = True
            break
        rep_seqs = xp.repeat(seqs, n_types, axis=0)
        new_col = xp.tile(xp.arange(n_types, dtype=xp.int64), n).reshape(-1, 1)
        cand = xp.concatenate([rep_seqs, new_col], axis=1)
        cand_start = xp.repeat(starts, n_types)
        cand_joints = xp.repeat(joints_acc, n_types)
        keep, welds, next_cursor = _legal_placement(cand, cand_start)
        seqs = cand[keep]
        joints_acc = cand_joints[keep] + welds[keep]
        starts = next_cursor[keep]
        depth += 1

    if fell_back:
        # Correctness fallback for pathological width: finish on CPU.
        from _demo_route2_equiv import _enumerate_equiv_cutplans
        cpu = _enumerate_equiv_cutplans(bar_lengths, kerf, types, min_seg,
                                        per_plan_cap)
        for plan in cpu:
            if plan["parts"] in seen:
                continue
            seen.add(plan["parts"])
            results.append(plan)
            if len(results) >= per_plan_cap:
                break
    return results
