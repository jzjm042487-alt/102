"""Pure feasibility check for group [1] equiv-stock model at given alpha."""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmark_against_software import _clean_payload, _load_records  # noqa: E402
from app.domain import parse_problem  # noqa: E402
import _demo_route2_equiv as demo  # noqa: E402
from pyscipopt import Model, quicksum  # noqa: E402


def main() -> int:
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    alpha = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    records = _load_records()
    cands = [r for r in records if r.get("MOMCALCULATESTATUS") == "1"
             and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")]
    r = random.Random(7).sample(cands, min(30, len(cands)))[:4][idx - 1]
    payload = _clean_payload(json.loads(r["MOMPROBLEMJSON"]))
    payload["BladeMargin"] = 0.0
    group = parse_problem(payload).groups[0]

    kerf = group.blade_margin
    min_seg = max(group.min_cut_length, group.min_weld_distance, 1)
    stock_lengths = sorted({s.length for s in group.stocks})
    stock_qty = {s.length: s.quantity for s in group.stocks}
    pipe_lengths = sorted({p.length for p in group.pipes})
    mj = {}
    demand = defaultdict(int)
    for p in group.pipes:
        mj[p.length] = max(mj.get(p.length, 0), p.max_joints)
        demand[p.length] += p.demand

    cols = []
    for bars in demo._bar_multisets(stock_lengths, alpha):
        usage = defaultdict(int)
        for b in bars:
            usage[b] += 1
        for plan in demo._enumerate_equiv_cutplans(bars, kerf, pipe_lengths, mj, min_seg, 300):
            cols.append({**plan, "bars": dict(usage)})
    print(f"alpha={alpha} cols={len(cols)} demand={dict(demand)} stock={stock_qty}")

    m = Model()
    m.hideOutput()
    x = [m.addVar(vtype="INTEGER", lb=0) for _ in cols]
    for pl, need in demand.items():
        m.addCons(quicksum(c["parts"].count(pl) * x[i] for i, c in enumerate(cols)) >= need)
    for s in stock_lengths:
        m.addCons(quicksum(c["bars"].get(s, 0) * x[i] for i, c in enumerate(cols)) <= stock_qty[s])
    m.setObjective(quicksum(x), "minimize")
    m.setRealParam("limits/time", 20)
    m.optimize()
    print("feasibility status:", m.getStatus(), "nsols:", m.getNSols())
    if m.getNSols() > 0:
        sol = m.getBestSol()
        used_bars = defaultdict(int)
        chosen = 0
        for i, c in enumerate(cols):
            v = int(round(m.getSolVal(sol, x[i])))
            if v:
                chosen += v
                for b, n in c["bars"].items():
                    used_bars[b] += n * v
        print("columns used:", chosen, "bars used:", dict(used_bars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
