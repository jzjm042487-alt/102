"""Profile CPU column generation: where does time go per group?"""
from __future__ import annotations

import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmark_against_software import _clean_payload, _load_records  # noqa: E402
from app.domain import parse_problem  # noqa: E402
from _demo_route2_equiv import (  # noqa: E402
    _bar_multisets,
    _enumerate_equiv_cutplans,
)


def _types_of(group):
    pt: dict[tuple, dict] = {}
    for p in group.pipes:
        fb = tuple((iv.start, iv.end) for iv in p.forbidden)
        key = (p.length, fb, p.max_joints)
        e = pt.setdefault(key, {"length": p.length, "forbidden": fb,
                               "max_joints": p.max_joints, "demand": 0})
        e["demand"] += p.demand
    return list(pt.values())


def main() -> int:
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    alpha = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    records = _load_records()
    cands = [r for r in records if r.get("MOMCALCULATESTATUS") == "1"
             and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")]
    picked = random.Random(seed).sample(cands, min(sample, len(cands)))

    for i, r in enumerate(picked):
        payload = _clean_payload(json.loads(r["MOMPROBLEMJSON"]))
        payload["BladeMargin"] = 0.0
        prob = parse_problem(payload)
        if not prob.groups:
            continue
        g = prob.groups[0]
        kerf = g.blade_margin
        min_seg = max(g.min_cut_length, g.min_weld_distance, 1)
        stock_lengths = sorted({s.length for s in g.stocks})
        types = _types_of(g)

        t0 = time.perf_counter()
        multisets = list(_bar_multisets(stock_lengths, alpha))
        t_ms = time.perf_counter() - t0

        t1 = time.perf_counter()
        total_plans = 0
        calls = 0
        for bars in multisets:
            calls += 1
            plans = _enumerate_equiv_cutplans(bars, kerf, types, min_seg,
                                              per_plan_cap=200)
            total_plans += len(plans)
            if time.perf_counter() - t1 > 6.0:
                break
        t_dfs = time.perf_counter() - t1

        spec = f'{r.get("MOMMATERIAL","")}/{r.get("MOMOUTSIDEDIAMETER","")}'
        print(f"  [{i+1}] {spec} stocks={len(stock_lengths)} types={len(types)} "
              f"multisets={len(multisets)}({t_ms*1000:.0f}ms) "
              f"DFS_calls={calls} plans={total_plans}({t_dfs:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
