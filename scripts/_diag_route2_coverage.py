"""A-diagnose: classify WHY route2 fails to solve each group.

Buckets:
  NO_COLUMNS       - generation produced zero equivalent-stock columns
  UNSOLVED(...)    - MILP found no feasible combo of columns (demand/stock/must_use)
  CONV_FAIL(...)   - solved but step-3 conversion rejected the plan
  TIME             - hit the time wall before any incumbent
  OK               - solved + converted

Also reports tightness, #types, #bar-lengths, chosen alpha, #columns, gen_s.
"""
from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmark_against_software import _clean_payload, _load_records  # noqa: E402
from app.domain import parse_problem  # noqa: E402
from app import route2_equiv  # noqa: E402


def _pin_blade(pl):
    np = pl.setdefault("NestParam", {})
    if "BladeMargin" not in np and "BladeMargin" not in pl:
        np["BladeMargin"] = 10
    return pl


def main() -> int:
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    tl = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    acap = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    records = _load_records()
    cands = [r for r in records if r.get("MOMCALCULATESTATUS") == "1"]
    picked = random.Random(seed).sample(cands, min(sample, len(cands)))

    bucket: Counter = Counter()
    for i, r in enumerate(picked):
        pl = _pin_blade(_clean_payload(json.loads(r["MOMPROBLEMJSON"])))
        prob = parse_problem(pl)
        if not prob.groups:
            continue
        g = prob.groups[0]
        n_bar_lengths = len({s.length for s in g.stocks})
        tot_stock = sum(s.length * s.quantity for s in g.stocks)
        tot_dem = sum(p.length * p.demand for p in g.pipes)
        tight = tot_dem / tot_stock if tot_stock else 9.9
        # NOTE: min_bars_needed = ceil(len/max_stock)*demand is NOT a valid
        # infeasibility bound (a bar can supply weld-pieces to several pipes).
        # It is reported only as a rough tightness signal.  Whether an UNSOLVED
        # group is truly infeasible vs solver-limited must be judged by comparing
        # against the baseline MILP, not by this bound.
        max_stock = max((s.length for s in g.stocks), default=1)
        import math as _m
        min_bars_needed = sum(
            _m.ceil(p.length / max_stock) * p.demand for p in g.pipes
        )
        total_bars = sum(s.quantity for s in g.stocks)
        # VALID necessary lower bound on consumed material:
        #   demand_length + (mandatory welds) * kerf <= total stock length.
        # A pipe longer than the longest bar needs >= ceil(len/max_stock)-1 welds,
        # each costing one kerf.  If this exceeds available stock, the group is
        # PROVABLY infeasible.  Groups that clear this bound with slack are the
        # only candidates where baseline could beat route2 (real gaps).
        kerf = g.blade_margin
        min_welds = sum(
            (_m.ceil(p.length / max_stock) - 1) * p.demand for p in g.pipes
        )
        min_material = tot_dem + min_welds * kerf
        bound_slack = tot_stock - min_material  # >=0 required for feasibility
        t0 = time.perf_counter()
        try:
            res = route2_equiv._solve_equiv_csp(g, tl, acap)
        except Exception as e:
            bucket["EXC"] += 1
            print(f"  [{i+1}] EXC {type(e).__name__}: {e}")
            continue
        st = res.get("status", "?")
        n_types = len(res.get("types", []))
        alpha = res.get("alpha")
        cols = res.get("columns")
        gen_s = res.get("gen_s")
        el = time.perf_counter() - t0
        if st == "NO_COLUMNS":
            bucket["NO_COLUMNS"] += 1
            klass = "NO_COLUMNS"
        elif st.startswith("UNSOLVED"):
            # Split by the valid length+kerf bound: provably-infeasible vs
            # candidate-real-gap (baseline might solve).  A small positive slack
            # (< 1 bar of the longest stock) is still effectively infeasible
            # because real packing wastes more than the bound accounts for.
            if bound_slack < max_stock:
                bucket["UNSOLVED_TIGHT"] += 1
                klass = f"UNSOLVED_TIGHT slack={bound_slack}"
            else:
                bucket["UNSOLVED_CANDIDATE"] += 1
                klass = f"UNSOLVED_CANDIDATE slack={bound_slack}"
        elif st in ("OPTIMAL", "FEASIBLE"):
            conv = route2_equiv._convert_to_workorder(res)
            if conv.get("ok"):
                bucket["OK"] += 1
                klass = f"OK({st})"
            else:
                bucket["CONV_FAIL"] += 1
                klass = f"CONV_FAIL({conv.get('reason')})"
        else:
            bucket["OTHER"] += 1
            klass = st
        print(f"  [{i+1}] {klass:26s} tight={tight:.2f} minbars={min_bars_needed}/"
              f"{total_bars} types={n_types} barlens={n_bar_lengths} alpha={alpha} "
              f"cols={cols} gen={gen_s}s el={el:.1f}s")

    print(f"\n==== A-diagnose ({sample} groups, tl={tl}, acap={acap}) ====")
    for k, v in bucket.most_common():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
