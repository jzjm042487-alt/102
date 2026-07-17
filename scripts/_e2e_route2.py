"""End-to-end: full production solve_and_verify with route2 OFF vs ON."""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmark_against_software import _clean_payload, _load_records  # noqa: E402


def _solve_one(pl, tl, route2):
    from app.service import solve_and_verify
    os.environ["NESTING_ROUTE2"] = "on" if route2 else ""
    res = solve_and_verify(pl, time_limit_seconds=tl)
    g = res["groups"][0] if res.get("groups") else {}
    m = g.get("metrics", {})
    return {
        "status": res.get("status"),
        "passed": res.get("verification", {}).get("passed"),
        "util": m.get("utilization_rate"),
        "welds": m.get("welding_joint_quantity"),
        "backend": m.get("solver_backend"),
        "warns": [w for w in g.get("warnings", []) if "ROUTE2" in w],
    }


def main() -> int:
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    tl = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    records = _load_records()
    cands = [r for r in records if r.get("MOMCALCULATESTATUS") == "1"]
    picked = random.Random(seed).sample(cands, min(sample, len(cands)))
    payloads = []
    for r in picked:
        pl = _clean_payload(json.loads(r["MOMPROBLEMJSON"]))
        # Ensure solver and verifier agree on kerf: production payloads always
        # carry BladeMargin, but some archived DB rows omit it -- in which case
        # parse_problem defaults to 0 while the verifier defaults to 10, an
        # unrelated data-quality divergence.  Pin it into NestParam so both
        # sides read the same value and the off-vs-on comparison stays fair.
        np = pl.setdefault("NestParam", {})
        if "BladeMargin" not in np and "BladeMargin" not in pl:
            np["BladeMargin"] = 10
        payloads.append(pl)

    # Interleave OFF/ON per payload and print incrementally with flush so we
    # get live progress and can spot a runaway group immediately.  Env is read
    # per-call in _route2_enabled, so toggling in-process is safe.
    off, on = [], []
    # A delta between OFF and ON is only attributable to route-2 when route-2 was
    # actually selected (R2 flag).  The baseline MILP is itself nondeterministic
    # under a time limit, so OFF-vs-ON deltas on non-R2 groups are solver jitter,
    # not route-2 effects -- track them separately so the pass/fail signal is
    # honest about what route-2 did.
    selected = 0
    r2_improved = r2_regressed = 0
    jitter_up = jitter_down = 0
    for i, pl in enumerate(payloads):
        import time as _time
        t0 = _time.time()
        a = _solve_one(pl, tl, route2=False)
        b = _solve_one(pl, tl, route2=True)
        off.append(a)
        on.append(b)
        dt = _time.time() - t0

        r2 = any("ROUTE2_SELECTED" in w for w in b["warns"])
        if r2:
            selected += 1
        au = a["util"] or 0.0
        bu = b["util"] or 0.0
        tag = ""
        changed = abs(au - bu) > 1e-9 or a["welds"] != b["welds"]
        if changed:
            better = (bu > au + 1e-9) or (abs(bu - au) <= 1e-9
                                          and (b["welds"] or 0) < (a["welds"] or 0))
            worse = (bu < au - 1e-9)
            if r2:
                if better:
                    r2_improved += 1
                    tag = " IMPROVED"
                if worse:
                    r2_regressed += 1
                    tag = " REGRESSED"
            else:
                # OFF and ON both ran baseline for this group; any diff is jitter.
                if better:
                    jitter_up += 1
                    tag = " ~jitter+"
                elif worse:
                    jitter_down += 1
                    tag = " ~jitter-"
        r2flag = " R2" if r2 else ""
        print(f"  [{i+1}/{sample}] {dt:5.1f}s "
              f"off(util={au:.3f},w={a['welds']},pass={a['passed']},{a['status']}) -> "
              f"on(util={bu:.3f},w={b['welds']},pass={b['passed']},{b['status']})"
              f"{r2flag}{tag}", flush=True)
    os.environ["NESTING_ROUTE2"] = ""

    off_pass = sum(1 for x in off if x["passed"])
    on_pass = sum(1 for x in on if x["passed"])

    print(f"\n==== e2e route2 OFF vs ON ({sample} groups, tl={tl}) ====", flush=True)
    print(f"  verify pass: off={off_pass}/{sample}  on={on_pass}/{sample}")
    print(f"  route2 selected: {selected}  "
          f"r2_improved: {r2_improved}  r2_regressed: {r2_regressed}")
    print(f"  baseline jitter (not route2): up={jitter_up}  down={jitter_down}")
    # Acceptance: route-2 must never verify-regress and never make a group it was
    # selected on worse.  Baseline jitter is not a route-2 failure.
    return 0 if on_pass >= off_pass and r2_regressed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
