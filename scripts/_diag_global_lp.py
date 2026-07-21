"""Deeper diagnosis: LP feasibility and expanded candidate pools."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum

from app.domain import parse_problem
from app.global_candidates import (
    MAX_PIECES,
    MAX_TRIM,
    build_cut_pool,
    build_weld_pool,
)
from app.solver_global import UTIL_FLOOR, _max_used_for_util_floor

ROOT = Path(__file__).resolve().parents[1]
samples = json.loads(
    (ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
)["samples"]
sid = "194ca29c-1ba1-4721-98c6-2af6382a64bb"
rec = next(s for s in samples if s["id"] == sid)
group = parse_problem(rec["problem"]).groups[0]


def lp_feasible(welds, cuts, time_limit=15.0, max_used=None):
    m = Model("lp")
    m.setParam("limits/time", time_limit)
    m.hideOutput()
    bars = defaultdict(int)
    for s in group.stocks:
        bars[s.length] += s.quantity
    u = {}
    for i, pats in welds.items():
        for wi in range(len(pats)):
            u[(i, wi)] = m.addVar(vtype="C", lb=0, name=f"u{i}_{wi}")
        m.addCons(quicksum(u[(i, wi)] for wi in range(len(pats))) == group.pipes[i].demand)
    x = {p: m.addVar(vtype="C", lb=0, name=f"x{p}") for p in range(len(cuts))}
    by = defaultdict(list)
    for p, c in enumerate(cuts):
        by[c["stock"]].append(p)
    for L, plist in by.items():
        m.addCons(quicksum(x[p] for p in plist) <= bars[L])
    produced = defaultdict(list)
    for p, c in enumerate(cuts):
        for seg, cnt in c["counts"].items():
            produced[seg].append((cnt, x[p]))
    consumed = defaultdict(list)
    for i, pats in welds.items():
        for wi, parts in enumerate(pats):
            cnts = defaultdict(int)
            for seg in parts:
                cnts[seg] += 1
            for seg, cnt in cnts.items():
                consumed[seg].append((cnt, u[(i, wi)]))
    for seg in set(produced) | set(consumed):
        m.addCons(
            quicksum(c * v for c, v in produced.get(seg, []))
            >= quicksum(c * v for c, v in consumed.get(seg, []))
        )
    length = quicksum(cuts[p]["stock"] * x[p] for p in range(len(cuts)))
    if max_used is not None:
        m.addCons(length <= max_used)
    m.setObjective(length, "minimize")
    m.optimize()
    return m.getNSols() > 0, str(m.getStatus()), (m.getObjVal() if m.getNSols() else None)


configs = [
    ("default", {}, {}),
    ("trim200_pieces8", {"max_trim": 200, "max_pieces": 8}, {"split_cap": 20, "two_cap": 40}),
    ("trim500_pieces10", {"max_trim": 500, "max_pieces": 10}, {"split_cap": 24, "two_cap": 60}),
    ("trim1200_pieces12", {"max_trim": 1200, "max_pieces": 12}, {"split_cap": 24, "two_cap": 80}),
]

max_used = _max_used_for_util_floor(group.demand_length, UTIL_FLOOR)
print("group demand", group.demand_length, "stock", group.stock_length, "max_used", max_used)
for name, cut_kw, weld_kw in configs:
    t0 = time.monotonic()
    welds, alph = build_weld_pool(group, **weld_kw)
    cuts = build_cut_pool(group, alph, **cut_kw)
    build_s = time.monotonic() - t0
    ok, status, obj = lp_feasible(welds, cuts, 20.0, max_used=None)
    ok2, status2, obj2 = lp_feasible(welds, cuts, 20.0, max_used=max_used)
    print(
        name,
        "alph",
        len(alph),
        "cuts",
        len(cuts),
        "build",
        round(build_s, 2),
        "lp",
        ok,
        status,
        obj,
        "lp_floor",
        ok2,
        status2,
        obj2,
    )
