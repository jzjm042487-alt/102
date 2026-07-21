"""Recheck strengthened pool LP feasibility on the hard sample."""
from __future__ import annotations

import json
from pathlib import Path

from app.domain import parse_problem
from app.global_candidates import build_cut_pool, build_seed_alphabet, build_weld_pool
from app.solver_global import _lp_feasible, _max_used_for_util_floor, UTIL_FLOOR, solve_group

ROOT = Path(__file__).resolve().parents[1]
samples = json.loads(
    (ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
)["samples"]
group = parse_problem(
    next(s for s in samples if s["id"] == "194ca29c-1ba1-4721-98c6-2af6382a64bb")["problem"]
).groups[0]

legacy_segs = {1246, 1411, 3519, 3584, 4323, 4930, 7672, 8584, 10749, 12000, 8000, 9000, 9500}
seed = build_seed_alphabet(group, seed_cap=160)
print("seed size", len(seed), "legacy hit", sorted(legacy_segs & seed), "miss", sorted(legacy_segs - seed))

for tier in (1, 2, 3):
    welds, alph = build_weld_pool(group, tier=tier)
    cuts = build_cut_pool(group, alph, tier=tier)
    ok = _lp_feasible(group, welds, cuts, 20.0)
    ok_floor = _lp_feasible(
        group, welds, cuts, 20.0, max_used_len=_max_used_for_util_floor(group.demand_length, UTIL_FLOOR)
    )
    # does any pipe include a legacy-like multi segment?
    multi = sum(1 for pats in welds.values() for p in pats if len(p) >= 3)
    print(
        f"tier={tier} alph={len(alph)} cuts={len(cuts)} weld_pats={sum(len(v) for v in welds.values())} "
        f"multi3+={multi} lp={ok} lp_floor={ok_floor} legacy_in_alph={sorted(legacy_segs & alph)[:8]}"
    )

print("solve_group 45s...")
res = solve_group(group, 45.0)
print(
    None
    if res is None
    else (
        res["metrics"]["solve_status"],
        res["metrics"]["utilization_rate"],
        res["metrics"]["cutting_pattern_type_quantity"],
        res["metrics"]["welding_pattern_type_quantity"],
    )
)
