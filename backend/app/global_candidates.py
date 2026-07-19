"""Candidate weld/cut pools for the global joint nesting engine.

Strengthened generation (sample-agnostic):
1. Seed a shared segment alphabet from stock lengths, low-waste remainders,
   and stock bipartitions (mirrors legacy "shared segment" structure).
2. Compose multi-segment weld patterns from that alphabet.
3. Enumerate cut columns that can actually produce the alphabet densely enough
   for tight stock ratios.
"""

from __future__ import annotations

from typing import Any, Iterable

from .domain import MaterialGroup, PipeDemand
from .solver import _legal_pattern

WELD_SPLIT_CAP = 12
WELD_TWO_CAP = 20
WELD_K_CAP = 30
WELD_COMPOSE_CAP = 40
WELD_COMPOSE_NODE_BUDGET = 80_000
CUT_PER_STOCK_CAP = 800
CUT_TOTAL_CAP = 12_000
MAX_TRIM = 50
MAX_TRIM_FEASIBLE = 800
MAX_PIECES = 8
ALPHABET_SEED_CAP = 120


def _stock_lengths(group: MaterialGroup) -> list[int]:
    return sorted({stock.length for stock in group.stocks})


def _packing_waste(parts: Iterable[int], stocks: list[int]) -> int:
    """Minimal leftover if each part is cut alone from the smallest fitting stock."""
    waste = 0
    for part in parts:
        fit = next((stock for stock in stocks if stock >= part), None)
        if fit is None:
            return 10**12
        waste += fit - part
    return waste


def weld_positions(pipe: PipeDemand, group: MaterialGroup, max_stock: int, want: int) -> list[int]:
    """Legal single-joint positions biased toward stock-aligned splits."""
    length = pipe.length
    stock_lengths = _stock_lengths(group)
    raw: set[int] = set()
    for stock_len in stock_lengths:
        raw.add(stock_len)
        raw.add(length - stock_len)
        raw.add(stock_len - group.blade_margin)
        raw.add(length - stock_len + group.blade_margin)
    for step in range(1, want + 1):
        raw.add(length * step // (want + 1))
    legal: list[int] = []
    for position in sorted(raw):
        if not (0 < position < length):
            continue
        parts = (position, length - position)
        if max(parts) > max_stock:
            continue
        if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
            legal.append(position)
    return legal


def _pipe_stock_remainders(
    group: MaterialGroup,
    stocks: list[int],
    max_stock: int,
    min_cut: int,
    min_internal: int,
) -> set[int]:
    """Remainders after embedding one or two stock bars into a long pipe."""
    rems: set[int] = set()
    for pipe in group.pipes:
        if pipe.length <= max_stock:
            continue
        for stock_len in stocks:
            if stock_len >= pipe.length:
                continue
            rem = pipe.length - stock_len
            if min_cut <= rem <= max_stock:
                rems.add(rem)
            for second in stocks:
                rem2 = pipe.length - stock_len - second
                if min_internal <= rem2 <= max_stock:
                    rems.add(rem2)
    return rems


def _dominant_remainders(
    group: MaterialGroup,
    stocks: list[int],
    max_stock: int,
    min_cut: int,
) -> set[int]:
    """Remainders after peeling a near-max stock — primary sources to split further."""
    peel = [length for length in stocks if length >= int(max_stock * 0.9)] or [max_stock]
    rems: set[int] = set()
    for pipe in group.pipes:
        if pipe.length <= max_stock:
            continue
        for stock_len in peel:
            if stock_len >= pipe.length:
                continue
            rem = pipe.length - stock_len
            if min_cut <= rem <= max_stock:
                rems.add(rem)
    return rems


def _has_near_stock_partner(
    seg: int,
    known: set[int],
    stocks: list[int],
    min_cut: int,
    trim_values: tuple[int, ...],
) -> bool:
    for stock_len in stocks:
        if seg >= stock_len:
            continue
        for trim in trim_values:
            partner = stock_len - seg - trim
            if partner < min_cut:
                break
            if partner in known:
                return True
    return False


def _remainder_pack_parts(
    remainders: set[int],
    stocks: list[int],
    anchors: set[int],
    min_cut: int,
    max_stock: int,
    *,
    trim_values: tuple[int, ...] = (0, 1, 2, 5, 10, 15, 20),
    discover_cap: int = 400,
    fill_targets: set[int] | None = None,
) -> set[int]:
    """Derive packable parts by complement close + remainder fill.

    Fixed waves with scored caps (no combinatorial blow-up):
      wave1: complements of large remainders into max_stock
      wave2: top-scored fills of dominant remainders with wave1 pieces
      wave3: complements of those fills + one more fill round
    Example: 10749?1246?(fill 8918)?7672?4323?(fill 9253)?4930.
    """
    if not remainders:
        return set()

    targets = set(fill_targets) if fill_targets is not None else set(remainders)
    known = set(anchors) | set(remainders) | set(stocks)
    discovered: set[int] = set()

    def absorb(values: set[int]) -> set[int]:
        added: set[int] = set()
        for seg in values:
            if min_cut <= seg <= max_stock and seg not in known:
                known.add(seg)
                discovered.add(seg)
                added.add(seg)
        return added

    large_rems = {seg for seg in remainders if seg >= max_stock // 2}

    scored_comp: list[tuple[int, int]] = []
    comp_trim: dict[int, int] = {}
    for seg in large_rems:
        for trim in trim_values:
            partner = max_stock - seg - trim
            if not (min_cut <= partner <= max_stock):
                continue
            if not any(
                rem > partner and min_cut <= (rem - partner) <= max_stock
                for rem in targets
            ):
                continue
            score = trim * 10 - seg // 50
            scored_comp.append((score, partner))
            prev = comp_trim.get(partner)
            if prev is None or trim < prev:
                comp_trim[partner] = trim
    scored_comp.sort()
    wave1_cap = min(120, max(48, discover_cap // 2))
    wave1: set[int] = set()
    for _score, partner in scored_comp:
        if len(wave1) >= wave1_cap:
            break
        wave1.add(partner)
    absorb(wave1)

    def scored_fills(pieces: set[int], rem_set: set[int], cap: int) -> set[int]:
        """Keep the best few fills *per remainder* so one rem cannot starve others."""
        # Tiny complement crumbs (e.g. 80) create near-full rests that drown out
        # the shared industrial pieces (1246?7672). Keep mid-size fillers only.
        min_filler = max(min_cut, min(500, max_stock // 24))
        max_filler = max_stock // 2
        usable = {seg for seg in pieces if min_filler <= seg <= max_filler}
        per_rem = max(8, cap // max(1, len(rem_set)))
        picked: set[int] = set()
        for rem in sorted(rem_set, reverse=True):
            if rem < 2 * min_cut or rem > max_stock:
                continue
            scored: list[tuple[int, int]] = []
            for seg in usable:
                if seg >= rem:
                    continue
                rest = rem - seg
                if not (min_cut <= rest <= max_stock) or rest in known:
                    continue
                trim = comp_trim.get(seg, 20)
                partner = max_stock - rest
                pack_bonus = 0
                if partner in known or partner in pieces:
                    pack_bonus = -1000
                else:
                    for t in trim_values:
                        if max_stock - rest - t in known or max_stock - rest - t in pieces:
                            pack_bonus = -1000 + t
                            break
                # Prefer small trim, then larger shared rests.
                score = trim * 10 + pack_bonus - rest // 25
                scored.append((score, rest))
            scored.sort()
            for _score, rest in scored[:per_rem]:
                picked.add(rest)
                if len(picked) >= cap:
                    return picked
        return picked

    wave2 = absorb(scored_fills(wave1, targets, cap=80))
    # Exact per-fill complements with small trim ? do not globally cap away
    # near-complements like 7672+4323+5=12000.
    wave3_comp: set[int] = set()
    for seg in wave2:
        for trim in trim_values[:4]:
            partner = max_stock - seg - trim
            if min_cut <= partner <= max_stock:
                wave3_comp.add(partner)
                prev = comp_trim.get(partner)
                if prev is None or trim < prev:
                    comp_trim[partner] = trim
    absorb(wave3_comp)

    targets |= {seg for seg in wave2 if seg >= 2 * min_cut}
    wave3_fill = absorb(scored_fills(wave1 | wave3_comp, targets, cap=120))
    final_comp: set[int] = set()
    for seg in wave3_fill:
        for trim in trim_values[:4]:
            partner = max_stock - seg - trim
            if min_cut <= partner <= max_stock:
                final_comp.add(partner)
                prev = comp_trim.get(partner)
                if prev is None or trim < prev:
                    comp_trim[partner] = trim
    absorb(final_comp)

    return {seg for seg in discovered if 0 < seg <= max_stock}



def build_seed_alphabet(
    group: MaterialGroup,
    *,
    seed_cap: int = ALPHABET_SEED_CAP,
) -> set[int]:
    """Shared segment alphabet seeded for cross-pipe packing.

    Critical industrial structure (seen in legacy solutions): many weld segments
    are either stock lengths or complementary pieces that pack into a stock
    together (a + b = stock, or a + b ≈ stock with tiny trim).  We therefore:
    1) force-keep stocks and pipe-stock remainders;
    2) split dominant remainders into stock-packable parts (cross-pipe nesting);
    3) close under stock bipartitions / near-complements;
    4) only then cap optional extras under ``seed_cap``.
    """
    stocks = _stock_lengths(group)
    max_stock = stocks[-1]
    min_cut = max(group.min_cut_length, 1)
    min_internal = max(group.min_weld_distance, min_cut)

    forced: set[int] = set(stocks)

    for pipe in group.pipes:
        if pipe.length <= max_stock:
            forced.add(pipe.length)

    remainders = _pipe_stock_remainders(
        group, stocks, max_stock, min_cut, min_internal
    )
    forced.update(remainders)
    dominant = _dominant_remainders(group, stocks, max_stock, min_cut)
    pack_parts = _remainder_pack_parts(
        remainders,
        stocks,
        forced,
        min_cut,
        max_stock,
        fill_targets=dominant,
    )
    forced.update(pack_parts)

    # Light optional extras only — heavy complement densification is already
    # handled sparsely inside ``_remainder_pack_parts``.
    optional: set[int] = set()
    for stock_len in stocks:
        for n_parts in (2, 3, 4):
            if stock_len // n_parts < min_cut:
                continue
            piece = stock_len // n_parts
            optional.add(piece)
            optional.add(stock_len - piece * (n_parts - 1))

    ranked_optional = sorted(
        (seg for seg in optional if seg not in forced),
        key=lambda seg: (_packing_waste((seg,), stocks), seg),
    )
    kept = set(forced)
    for seg in ranked_optional:
        if len(kept) >= max(seed_cap, len(forced)):
            break
        kept.add(seg)
    return {seg for seg in kept if 0 < seg <= max_stock}


def _compose_remainder(
    rem: int,
    alphabet: list[int],
    *,
    max_parts: int,
    min_cut: int,
    min_weld: int,
    max_stock: int,
    cap: int,
) -> list[tuple[int, ...]]:
    """Compose a peeled remainder into <= max_parts alphabet segments.

    Prefers 1-part and mid-size 2-part alphabet packs (shared nesting pieces)
    over crumb-filled DFS enumerations.
    """
    if rem <= 0 or max_parts < 1:
        return []
    alph_set = {seg for seg in alphabet if min_cut <= seg <= max_stock}
    found: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()

    def push(parts: tuple[int, ...]) -> None:
        if len(found) >= cap or not parts:
            return
        key = tuple(sorted(parts, reverse=True))
        if key in seen:
            return
        if max(parts) > max_stock or sum(parts) != rem:
            return
        seen.add(key)
        found.append(key)

    # 1-part
    if rem <= max_stock and rem >= min_cut:
        push((rem,))

    min_filler = max(min_cut, min(500, max_stock // 24))
    max_filler = max_stock // 2

    def has_partner(seg: int) -> bool:
        for trim in (0, 1, 2, 5, 10, 15, 20):
            if max_stock - seg - trim in alph_set:
                return True
        return False

    quality = {
        seg
        for seg in alph_set
        if min_filler <= seg <= max_filler and has_partner(seg)
    }

    # 3-part from stock-partnered pieces only (avoids crumb triples).
    if max_parts >= 3:
        quality_list = sorted(quality, reverse=True)
        for seg in quality_list:
            rest = rem - seg
            if rest < 2 * min_cut:
                continue
            for mid in quality_list:
                if mid > rest:
                    continue
                last = rest - mid
                if last < min_cut or last > max_stock:
                    continue
                if last in alph_set and (last in quality or has_partner(last) or last <= max_filler):
                    push((seg, mid, last))
                if len(found) >= max(8, cap // 3):
                    break
            if len(found) >= max(8, cap // 3):
                break

    # 2-part mid-size packs; prefer partnered fillers.
    if max_parts >= 2 and len(found) < cap:
        fillers = sorted(quality or alph_set, reverse=True)
        for seg in fillers:
            if not (min_filler <= seg <= max_filler):
                continue
            other = rem - seg
            if other < min_cut or other > max_stock:
                continue
            if other in alph_set:
                push((seg, other) if seg >= other else (other, seg))
            if len(found) >= cap:
                break

    return found[:cap]


def _stock_peel_patterns(
    pipe: PipeDemand,
    group: MaterialGroup,
    alphabet: list[int],
    *,
    cap: int,
) -> list[tuple[int, ...]]:
    """Patterns that embed one/two full stock bars then compose the leftover.

    This is the dominant industrial structure for long serpentine pipes.
    """
    stocks = _stock_lengths(group)
    max_stock = stocks[-1]
    patterns: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()

    def push(parts: tuple[int, ...]) -> None:
        if len(patterns) >= cap:
            return
        if max(parts) > max_stock:
            return
        if not _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
            return
        for seq in {parts, tuple(reversed(parts))}:
            if seq not in seen and _legal_pattern(
                pipe, seq, group.min_weld_distance, group.min_cut_length
            ):
                seen.add(seq)
                patterns.append(seq)

    alph_set = set(alphabet)

    # Multi-SKU packs first — free scarce long bars (e.g. 3584+8000+9000).
    if pipe.max_joints >= 1:
        sku = [length for length in stocks if length in alph_set]
        sku.sort(reverse=True)
        for i, s1 in enumerate(sku):
            rem1 = pipe.length - s1
            if max(group.min_cut_length, 1) <= rem1 <= max_stock and rem1 in alph_set:
                push((s1, rem1))
                push((rem1, s1))
            if pipe.max_joints >= 2:
                for s2 in sku[i:]:
                    rem = pipe.length - s1 - s2
                    if not (max(group.min_cut_length, 1) <= rem <= max_stock):
                        continue
                    if rem in alph_set or rem in stocks:
                        push((s1, s2, rem))
                        push((rem, s1, s2))
                        push((s1, rem, s2))
            if len(patterns) >= cap // 2:
                break

    # Longest stocks first for peel embeds.
    peel_stocks = sorted(
        (length for length in stocks if length >= int(max_stock * 0.9)),
        reverse=True,
    ) or [max_stock]

    for stock_len in peel_stocks:
        if stock_len >= pipe.length:
            continue
        rem = pipe.length - stock_len
        max_parts = max(1, pipe.max_joints)
        rem_pats = _compose_remainder(
            rem,
            alphabet,
            max_parts=max_parts,
            min_cut=max(group.min_cut_length, 1),
            min_weld=group.min_weld_distance,
            max_stock=max_stock,
            cap=max(32, cap // 2),
        )
        alph_set = set(alphabet)

        def rem_priority(parts: tuple[int, ...]) -> tuple[int, int, int]:
            if len(parts) < 2:
                return (2, 0, 0)
            partner_hits = 0
            for seg in parts:
                for trim in (0, 1, 2, 5, 10, 15, 20):
                    if max_stock - seg - trim in alph_set:
                        partner_hits += 1
                        break
            # Prefer packs whose pieces also bipartition stocks (shared cuts).
            return (0 if partner_hits else 1, -partner_hits, -max(parts))

        rem_pats = sorted(rem_pats, key=rem_priority)
        for rem_parts in rem_pats:
            push((stock_len, *rem_parts))
            push((*rem_parts, stock_len))
            if len(rem_parts) >= 2:
                push((rem_parts[0], stock_len, *rem_parts[1:]))
        if pipe.max_joints >= 2:
            for second in peel_stocks:
                rem2 = pipe.length - stock_len - second
                if rem2 < max(group.min_cut_length, 1) or rem2 > max_stock:
                    continue
                rem2_pats = _compose_remainder(
                    rem2,
                    alphabet,
                    max_parts=max(1, pipe.max_joints - 1),
                    min_cut=max(group.min_cut_length, 1),
                    min_weld=group.min_weld_distance,
                    max_stock=max_stock,
                    cap=6,
                )
                for rem_parts in rem2_pats:
                    push((stock_len, second, *rem_parts))
                    push((stock_len, *rem_parts, second))
        if len(patterns) >= cap:
            return patterns
    return patterns


def _compose_from_alphabet(
    pipe: PipeDemand,
    group: MaterialGroup,
    alphabet: list[int],
    *,
    cap: int,
    node_budget: int,
) -> list[tuple[int, ...]]:
    """DFS compositions of alphabet segments that form a legal weld pattern."""
    stocks = _stock_lengths(group)
    max_stock = stocks[-1]
    found: list[tuple[int, ...]] = []
    nodes = [0]
    segs = [seg for seg in alphabet if seg <= max_stock]
    segs.sort(reverse=True)
    # Prefer starting with full stock bars -- legacy patterns almost always embed
    # one or more exact stock lengths inside long pipes.
    stock_first = [seg for seg in stocks if seg in set(segs)] + [
        seg for seg in segs if seg not in set(stocks)
    ]

    def consider(parts: tuple[int, ...]) -> None:
        if len(found) >= cap:
            return
        if max(parts) > max_stock:
            return
        if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
            found.append(parts)

    def dfs(remaining: int, chosen: list[int], order: list[int]) -> None:
        if len(found) >= cap or nodes[0] > node_budget:
            return
        nodes[0] += 1
        if remaining == 0:
            consider(tuple(chosen))
            return
        if len(chosen) > pipe.max_joints:
            return
        for seg in order:
            if seg > remaining:
                continue
            leftover_slots = pipe.max_joints - len(chosen) + 1
            if remaining - seg > max_stock * leftover_slots:
                continue
            chosen.append(seg)
            dfs(remaining - seg, chosen, order)
            chosen.pop()
            if len(found) >= cap or nodes[0] > node_budget:
                return

    # Pass 1: stock-first ordering.
    dfs(pipe.length, [], stock_first)
    # Pass 2: general large-to-small if still thin.
    if len(found) < max(3, cap // 4):
        dfs(pipe.length, [], segs)

    ordered: list[tuple[int, ...]] = []
    for parts in found:
        for seq in {parts, tuple(reversed(parts))}:
            if _legal_pattern(pipe, seq, group.min_weld_distance, group.min_cut_length):
                ordered.append(seq)
    ordered = sorted(
        set(ordered),
        key=lambda parts: (_packing_waste(parts, stocks), len(parts), parts),
    )
    return ordered[:cap]


def build_weld_pool(
    group: MaterialGroup,
    *,
    split_cap: int = WELD_SPLIT_CAP,
    two_cap: int = WELD_TWO_CAP,
    k_cap: int = WELD_K_CAP,
    compose_cap: int = WELD_COMPOSE_CAP,
    seed_cap: int = ALPHABET_SEED_CAP,
    tier: int = 1,
) -> tuple[dict[int, list[tuple[int, ...]]], set[int]]:
    """Return (weld candidates per pipe index, shared segment alphabet).

    ``tier`` widens the pool without sample-specific rules:
      1 = default strengthened pool
      2 = larger alphabet + more compositions
      3 = aggressive expansion for tight instances
    """
    if not group.stocks:
        raise ValueError("material group has no stock supply")
    max_stock = max(stock.length for stock in group.stocks)
    stocks = _stock_lengths(group)

    if tier >= 2:
        seed_cap = max(seed_cap, 120)
        compose_cap = max(compose_cap, 60)
        split_cap = max(split_cap, 20)
        two_cap = max(two_cap, 40)
    if tier >= 3:
        seed_cap = max(seed_cap, 180)
        compose_cap = max(compose_cap, 100)
        split_cap = max(split_cap, 28)
        two_cap = max(two_cap, 80)
        k_cap = max(k_cap, 60)

    alphabet = build_seed_alphabet(group, seed_cap=seed_cap)
    alphabet_list = sorted(alphabet, reverse=True)
    candidates: dict[int, list[tuple[int, ...]]] = {}

    for pipe_index, pipe in enumerate(group.pipes):
        patterns: set[tuple[int, ...]] = set()
        if pipe.length <= max_stock and _legal_pattern(
            pipe, (pipe.length,), group.min_weld_distance, group.min_cut_length
        ):
            patterns.add((pipe.length,))

        # Low-waste stock-complement splits.
        scored_splits: list[tuple[int, tuple[int, ...]]] = []
        if pipe.max_joints >= 1:
            positions = weld_positions(pipe, group, max_stock, split_cap)
            for position in positions:
                parts = (position, pipe.length - position)
                scored_splits.append((_packing_waste(parts, stocks), parts))
            scored_splits.sort()
            for _, parts in scored_splits[:split_cap]:
                patterns.add(parts)

        if pipe.max_joints >= 2:
            positions = weld_positions(pipe, group, max_stock, split_cap)
            picks = positions[: min(6, len(positions))]
            two_joint: list[tuple[int, tuple[int, ...]]] = []
            for left in picks:
                for right in picks:
                    if right <= left:
                        continue
                    parts = (left, right - left, pipe.length - right)
                    if max(parts) > max_stock:
                        continue
                    if _legal_pattern(
                        pipe, parts, group.min_weld_distance, group.min_cut_length
                    ):
                        two_joint.append((_packing_waste(parts, stocks), parts))
            two_joint.sort()
            patterns.update(parts for _, parts in two_joint[:two_cap])

        # Shared-alphabet compositions (the key strengthening vs 2-split only).
        composed = _compose_from_alphabet(
            pipe,
            group,
            alphabet_list,
            cap=compose_cap,
            node_budget=WELD_COMPOSE_NODE_BUDGET * tier,
        )
        patterns.update(composed)

        # Stock-peel compositions: embed full bars then pack the leftover from
        # the shared alphabet (legacy-like 1246+12000+7672 structures).
        peel = _stock_peel_patterns(
            pipe,
            group,
            alphabet_list,
            cap=max(compose_cap, 80),
        )
        patterns.update(peel)

        if pipe.max_joints >= 3 and len(patterns) < 3:
            patterns.update(_deeper_splits(pipe, group, max_stock, k_cap))

        if not patterns:
            raise ValueError(
                f"A_PROCESS_INFEASIBLE: no legal weld pattern for pipe "
                f"{pipe.pipe_id} (L={pipe.length})"
            )

        ranked = sorted(
            patterns,
            key=lambda parts: (_packing_waste(parts, stocks), len(parts), parts),
        )
        # Always retain stock-peel patterns — they unlock shared-segment packing
        # even when their solo packing-waste rank is worse than a plain 2-split.
        reserved = [parts for parts in peel if parts in patterns]
        limit = max(compose_cap, split_cap + two_cap)
        kept: list[tuple[int, ...]] = []
        seen_keep: set[tuple[int, ...]] = set()
        for parts in reserved + ranked:
            if parts in seen_keep:
                continue
            seen_keep.add(parts)
            kept.append(parts)
            if len(kept) >= limit:
                break
        candidates[pipe_index] = kept
        for parts in candidates[pipe_index]:
            alphabet.update(parts)

    return candidates, alphabet


def _deeper_splits(
    pipe: PipeDemand,
    group: MaterialGroup,
    max_stock: int,
    cap: int,
) -> list[tuple[int, ...]]:
    """Bounded multi-joint splits when shallower pools cannot fit max_stock."""
    found: list[tuple[int, ...]] = []
    positions = weld_positions(pipe, group, max_stock, WELD_SPLIT_CAP)
    if len(positions) < 2:
        return found

    def dfs(start: int, chosen: list[int]) -> None:
        if len(found) >= cap:
            return
        if chosen:
            parts = _parts_from_cuts(pipe.length, chosen)
            if max(parts) <= max_stock and _legal_pattern(
                pipe, parts, group.min_weld_distance, group.min_cut_length
            ):
                found.append(parts)
                if len(found) >= cap:
                    return
        if len(chosen) >= pipe.max_joints:
            return
        for index in range(start, len(positions)):
            nxt = positions[index]
            if chosen and nxt - chosen[-1] < group.min_weld_distance:
                continue
            chosen.append(nxt)
            dfs(index + 1, chosen)
            chosen.pop()
            if len(found) >= cap:
                return

    dfs(0, [])
    return found


def _parts_from_cuts(length: int, cuts: list[int]) -> tuple[int, ...]:
    points = [0, *cuts, length]
    return tuple(points[index + 1] - points[index] for index in range(len(points) - 1))


def focus_cut_alphabet(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    seed_alphabet: set[int],
) -> set[int]:
    """Shrink cut alphabet to weld-used segments + stocks + near complements.

    A huge seed alphabet makes DFS crowd out essential multi-piece columns
    (empirically: 13 focused segs → LP feasible, +10 extras → LP infeasible
    on the same weld templates).
    """
    stocks = set(_stock_lengths(group))
    max_stock = max(stocks) if stocks else 0
    used = {seg for patterns in weld_cands.values() for parts in patterns for seg in parts}
    focused = set(used) | stocks
    for seg in list(used):
        for trim in (0, 1, 2, 5, 10, 15, 20):
            partner = max_stock - seg - trim
            if partner > 0:
                focused.add(partner)
        # Keep a few seed extras that pack with used segments into stocks.
        for stock_len in stocks:
            if seg >= stock_len:
                continue
            rem = stock_len - seg
            if rem in seed_alphabet:
                focused.add(rem)
    # Hard cap so pair-injection in build_cut_pool stays enabled (<=120).
    if len(focused) > 80:
        ranked = sorted(
            focused,
            key=lambda seg: (
                0 if seg in used or seg in stocks else 1,
                _packing_waste((seg,), sorted(stocks)),
                seg,
            ),
        )
        focused = set(ranked[:80])
        focused |= used | stocks
        if len(focused) > 100:
            # Extreme weld diversity: keep used+stocks only.
            focused = used | stocks
    return {seg for seg in focused if 0 < seg <= max_stock}


def build_cut_pool(
    group: MaterialGroup,
    alphabet: set[int],
    *,
    max_trim: int = MAX_TRIM_FEASIBLE,
    max_pieces: int = MAX_PIECES,
    per_stock_cap: int = CUT_PER_STOCK_CAP,
    total_cap: int = CUT_TOTAL_CAP,
    tier: int = 1,
) -> list[dict[str, Any]]:
    """Enumerate cutting columns over the shared segment alphabet."""
    if not alphabet:
        return []
    if tier >= 2:
        max_trim = max(max_trim, 1200)
        max_pieces = max(max_pieces, 10)
        per_stock_cap = max(per_stock_cap, 1200)
        total_cap = max(total_cap, 20_000)
    if tier >= 3:
        max_trim = max(max_trim, 2500)
        max_pieces = max(max_pieces, 12)
        total_cap = max(total_cap, 30_000)

    seg_lengths = sorted(alphabet, reverse=True)
    stock_lengths = _stock_lengths(group)
    kerf = group.blade_margin
    columns: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[tuple[int, int], ...]]] = set()
    remaining = [total_cap]
    smallest = seg_lengths[-1]

    # Always keep exact single-segment identity columns for every stock/seg pair.
    for stock_len in stock_lengths:
        for seg in seg_lengths:
            if seg > stock_len:
                continue
            remnant = stock_len - seg
            key = (stock_len, ((seg, 1),))
            if key in seen:
                continue
            seen.add(key)
            columns.append(
                {
                    "stock": stock_len,
                    "counts": {seg: 1},
                    "used": seg,
                    "remnant": remnant,
                }
            )
            remaining[0] -= 1

    # Inject stock bipartitions / near-bipartitions from the alphabet.
    # These are the shared industrial cuts (e.g. 4323+7672, 1246+10749≈12000)
    # and are easily missed by DFS when the alphabet is large.
    alph_set = set(seg_lengths)
    for stock_len in stock_lengths:
        if remaining[0] <= 0:
            break
        for seg in seg_lengths:
            if seg >= stock_len:
                continue
            for trim in (0, 1, 2, 5, 10, 15, 20):
                other = stock_len - seg - trim
                if other < seg:
                    break
                if other not in alph_set:
                    continue
                if kerf and seg + other + kerf > stock_len:
                    continue
                used = seg + other + (kerf if kerf else 0)
                if used > stock_len:
                    continue
                counts = {seg: 1} if seg != other else {seg: 2}
                if seg != other:
                    counts[other] = 1
                key = (stock_len, tuple(sorted(counts.items())))
                if key in seen:
                    continue
                seen.add(key)
                columns.append(
                    {
                        "stock": stock_len,
                        "counts": counts,
                        "used": used,
                        "remnant": stock_len - used,
                    }
                )
                remaining[0] -= 1
                if remaining[0] <= 0:
                    break

    # Inject every alphabet pair that fits on some stock. Critical on tight
    # instances (e.g. 1411+3519 on 5000). Only when alphabet is focused.
    if len(alph_set) <= 120:
        alph_asc = sorted(alph_set)
        for stock_len in stock_lengths:
            if remaining[0] <= 0:
                break
            for i, seg_a in enumerate(alph_asc):
                if seg_a > stock_len:
                    break
                for seg_b in alph_asc[i:]:
                    used = seg_a + seg_b + (kerf if kerf else 0)
                    if used > stock_len:
                        break
                    counts = {seg_a: 1} if seg_a != seg_b else {seg_a: 2}
                    if seg_a != seg_b:
                        counts[seg_b] = 1
                    key = (stock_len, tuple(sorted(counts.items())))
                    if key in seen:
                        continue
                    seen.add(key)
                    columns.append(
                        {
                            "stock": stock_len,
                            "counts": counts,
                            "used": used,
                            "remnant": stock_len - used,
                        }
                    )
                    remaining[0] -= 1
                    if remaining[0] <= 0:
                        break
                if remaining[0] <= 0:
                    break

    for stock_len in stock_lengths:
        if remaining[0] <= 0:
            break
        counts: dict[int, int] = {}
        budget = [min(per_stock_cap, remaining[0])]

        def dfs(index: int, used: int, n_pieces: int) -> None:
            if budget[0] <= 0 or remaining[0] <= 0:
                return
            remnant = stock_len - used
            if n_pieces >= 1 and remnant <= max_trim:
                key = (stock_len, tuple(sorted(counts.items())))
                if key not in seen:
                    seen.add(key)
                    columns.append(
                        {
                            "stock": stock_len,
                            "counts": dict(counts),
                            "used": used,
                            "remnant": remnant,
                        }
                    )
                    budget[0] -= 1
                    remaining[0] -= 1
                    if budget[0] <= 0 or remaining[0] <= 0:
                        return
            if remnant < smallest or n_pieces >= max_pieces:
                return
            for next_index in range(index, len(seg_lengths)):
                seg = seg_lengths[next_index]
                extra = seg + (kerf if n_pieces >= 1 else 0)
                if used + extra > stock_len:
                    continue
                counts[seg] = counts.get(seg, 0) + 1
                dfs(next_index, used + extra, n_pieces + 1)
                counts[seg] -= 1
                if counts[seg] == 0:
                    del counts[seg]
                if budget[0] <= 0 or remaining[0] <= 0:
                    return

        dfs(0, 0, 0)

    return ensure_coverage(columns, seg_lengths, stock_lengths, kerf, max_pieces)


def ensure_coverage(
    columns: list[dict[str, Any]],
    seg_lengths: list[int],
    stock_lengths: list[int],
    kerf: int,
    max_pieces: int,
) -> list[dict[str, Any]]:
    """Guarantee every alphabet segment has at least one producing column."""
    produced = {seg for column in columns for seg in column["counts"]}
    missing = [seg for seg in seg_lengths if seg not in produced]
    if not missing:
        return columns
    segs_desc = sorted(set(seg_lengths), reverse=True)
    max_stock = max(stock_lengths)
    existing = {
        (column["stock"], tuple(sorted(column["counts"].items()))) for column in columns
    }
    for seg in missing:
        stock_len = min(
            (length for length in sorted(set(stock_lengths)) if length >= seg),
            default=max_stock,
        )
        counts: dict[int, int] = {seg: 1}
        used = seg
        n_pieces = 1
        for fill in segs_desc:
            while (
                n_pieces < max_pieces
                and used + fill + (kerf if n_pieces >= 1 else 0) <= stock_len
            ):
                extra = fill + (kerf if n_pieces >= 1 else 0)
                counts[fill] = counts.get(fill, 0) + 1
                used += extra
                n_pieces += 1
        key = (stock_len, tuple(sorted(counts.items())))
        if key in existing:
            continue
        existing.add(key)
        columns.append(
            {
                "stock": stock_len,
                "counts": dict(counts),
                "used": used,
                "remnant": stock_len - used,
            }
        )
    return columns


__all__ = [
    "WELD_SPLIT_CAP",
    "WELD_TWO_CAP",
    "WELD_K_CAP",
    "CUT_PER_STOCK_CAP",
    "CUT_TOTAL_CAP",
    "MAX_TRIM",
    "MAX_TRIM_FEASIBLE",
    "MAX_PIECES",
    "build_seed_alphabet",
    "build_weld_pool",
    "build_cut_pool",
    "focus_cut_alphabet",
    "ensure_coverage",
    "weld_positions",
]
