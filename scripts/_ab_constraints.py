"""A/B: measure how forbidden zones + must_use affect the equiv-stock solver.

For each sampled group that actually HAS forbidden zones or must_use, solve
twice with route2new: once ignoring those constraints, once enforcing them.
Report status/joints deltas so we can see whether the constraints truly bite.
"""
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
from _demo_route2_equiv import _solve_route2new, _convert_to_workorder  # noqa: E402

SOLVED = {"OPTIMAL", "FEASIBLE"}


def main() -> int:
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    tl = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    acap = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    records = _load_records()
    cands = [r for r in records if r.get("MOMCALCULATESTATUS") == "1"
             and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")]
    picked = random.Random(seed).sample(cands, min(sample, len(cands)))

    checked = 0
    lost = 0          # solvable when ignored, INFEASIBLE when enforced
    joints_up = 0     # solved both ways but more joints when enforced
    joints_same = 0
    conv_fail_reasons: dict[str, int] = defaultdict(int)
    for r in picked:
        payload = _clean_payload(json.loads(r["MOMPROBLEMJSON"]))
        payload["BladeMargin"] = 0.0
        prob = parse_problem(payload)
        if not prob.groups:
            continue
        g = prob.groups[0]
        has_fb = any(p.forbidden for p in g.pipes)
        has_mu = any(s.must_use_quantity for s in g.stocks)
        if not (has_fb or has_mu):
            continue
        checked += 1
        free = _solve_route2new(g, tl, acap, ignore_constraints=True)
        strict = _solve_route2new(g, tl, acap, ignore_constraints=False)
        f_ok = free["status"] in SOLVED
        s_ok = strict["status"] in SOLVED
        spec = f'{r.get("MOMMATERIAL","")}/{r.get("MOMOUTSIDEDIAMETER","")}'
        tag = []
        if has_fb:
            tag.append("FB")
        if has_mu:
            tag.append("MU")
        if f_ok and not s_ok:
            lost += 1
        if f_ok and s_ok:
            df, ds = free.get("joints"), strict.get("joints")
            if df is not None and ds is not None:
                if ds > df:
                    joints_up += 1
                elif ds == df:
                    joints_same += 1
        # conversion check on strict
        conv = "-"
        if s_ok:
            c = _convert_to_workorder(strict)
            conv = "OK" if c["ok"] else c["reason"]
            if not c["ok"]:
                conv_fail_reasons[c["reason"]] += 1
        print(f"  {spec} [{'+'.join(tag)}] free={free['status']}(j={free.get('joints')}) "
              f"strict={strict['status']}(j={strict.get('joints')}) conv={conv}")

    print(f"\n==== forbidden/must_use A/B: {checked} 组含约束 ====")
    print(f"  约束导致从可解变 INFEASIBLE: {lost}/{checked}")
    print(f"  两边都解出且焊口增加: {joints_up}  (相等 {joints_same})")
    print(f"  strict 转换失败原因: "
          f"{dict(conv_fail_reasons) or '无(全部通过)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
