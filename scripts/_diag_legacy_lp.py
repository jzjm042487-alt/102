"""Check LP feasibility using legacy-like weld templates only."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum

from app.domain import parse_problem
from app.global_candidates import build_cut_pool
from app.solver import _legal_pattern

ROOT = Path(__file__).resolve().parents[1]
samples = json.loads(
    (ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
)["samples"]
group = parse_problem(
    next(s for s in samples if s["id"] == "194ca29c-1ba1-4721-98c6-2af6382a64bb")["problem"]
).groups[0]

legacy = {
    21253: [(4323, 12000, 3519, 1411), (4323, 12000, 4930)],
    20918: [(1246, 12000, 7672)],
    20584: [(8584, 12000), (3584, 8000, 9000)],
    20249: [(9500, 10749)],
}

welds = {}
alph = set()
for i, pipe in enumerate(group.pipes):
    pats = []
    for parts in legacy[pipe.length]:
        ok = _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length)
        print(pipe.length, parts, "legal", ok)
        if ok:
            pats.append(parts)
            alph.update(parts)
    welds[i] = pats

cuts = build_cut_pool(group, alph, tier=3)
print("alph", sorted(alph), "cuts", len(cuts))

m = Model("legacy_lp")
m.setParam("limits/time", 30)
m.hideOutput()
bars = defaultdict(int)
for s in group.stocks:
    bars[s.length] += s.quantity
u = {}
for i, pats in welds.items():
    for wi in range(len(pats)):
        u[(i, wi)] = m.addVar(vtype="C", lb=0)
    m.addCons(quicksum(u[(i, wi)] for wi in range(len(pats))) == group.pipes[i].demand)
x = {p: m.addVar(vtype="C", lb=0) for p in range(len(cuts))}
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
m.setObjective(length, "minimize")
m.optimize()
print("status", m.getStatus(), "sols", m.getNSols(), "obj", m.getObjVal() if m.getNSols() else None)
if m.getNSols():
    print("util", group.demand_length / m.getObjVal())
