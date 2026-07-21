"""Inspect whether weld alphabet is producible and balanced."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from app.domain import parse_problem
from app.global_candidates import build_cut_pool, build_weld_pool
from app.solver import _legal_pattern

ROOT = Path(__file__).resolve().parents[1]
samples = json.loads(
    (ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
)["samples"]
sid = "194ca29c-1ba1-4721-98c6-2af6382a64bb"
group = parse_problem(next(s for s in samples if s["id"] == sid)["problem"]).groups[0]
max_stock = max(s.length for s in group.stocks)
stocks = sorted({s.length for s in group.stocks})
print("max_stock", max_stock, "stocks", stocks)

welds, alph = build_weld_pool(group)
print("alphabet size", len(alph), "min", min(alph), "max", max(alph))
too_long = [s for s in alph if s > max_stock]
print("segments > max_stock", too_long)

# For each pipe, show patterns and whether all segs fit
for i, pipe in enumerate(group.pipes):
    pats = welds[i]
    print(
        f"pipe L={pipe.length} demand={pipe.demand} maxj={pipe.max_joints} "
        f"forbidden={len(pipe.forbidden)} npats={len(pats)}"
    )
    for p in pats[:8]:
        print("  ", p, "sum", sum(p), "max", max(p), "fit", max(p) <= max_stock)

# Can we build stock-exact complementary splits?
print("--- stock-exact templates ---")
for pipe in group.pipes:
    found = []
    for sl in stocks:
        if sl >= pipe.length:
            if _legal_pattern(pipe, (pipe.length,), group.min_weld_distance, group.min_cut_length):
                found.append((pipe.length,))
            continue
        # two parts using one stock length as first piece
        for a in (sl, pipe.length - sl):
            if not (0 < a < pipe.length):
                continue
            parts = (a, pipe.length - a)
            if max(parts) <= max_stock and _legal_pattern(
                pipe, parts, group.min_weld_distance, group.min_cut_length
            ):
                found.append(parts)
        # three parts: stock + stock + rem
        rem = pipe.length - 2 * sl
        if rem > 0:
            parts = (sl, sl, rem)
            if max(parts) <= max_stock and _legal_pattern(
                pipe, parts, group.min_weld_distance, group.min_cut_length
            ):
                found.append(parts)
    uniq = sorted(set(found), key=lambda p: (len(p), p))
    print(pipe.length, "exactish", len(uniq), uniq[:10])

cuts = build_cut_pool(group, alph)
produced = set()
for c in cuts:
    produced.update(c["counts"])
missing = sorted(alph - produced)
print("cut columns", len(cuts), "missing segs in cuts", missing[:20], "count", len(missing))

# Aggregate minimum segment demand if each pipe uses fewest-joint pattern
from app.global_candidates import weld_positions

seg_demand = Counter()
for i, pipe in enumerate(group.pipes):
    best = min(welds[i], key=lambda p: (len(p), max(p), p))
    print("chosen template", pipe.length, best)
    for seg in best:
        seg_demand[seg] += pipe.demand
print("seg demand entries", len(seg_demand), "total segs", sum(seg_demand.values()))
# Can each demanded seg be cut from some stock?
for seg, dem in sorted(seg_demand.items()):
    fit = [L for L in stocks if L >= seg]
    print("need", seg, "x", dem, "fit_stocks", fit[:5], "n", len(fit))
