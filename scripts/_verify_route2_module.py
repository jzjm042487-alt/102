"""Offline: exercise the PRODUCTION app.route2_equiv.solve_group -> real verify."""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmark_against_software import _clean_payload, _load_records  # noqa: E402
from app.domain import parse_problem  # noqa: E402
from app.verifier import verify_solution  # noqa: E402
from app.solver import from_units  # noqa: E402
from app import route2_equiv  # noqa: E402


def main() -> int:
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    tl = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    records = _load_records()
    cands = [r for r in records if r.get("MOMCALCULATESTATUS") == "1"
             and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")]
    picked = random.Random(seed).sample(cands, min(sample, len(cands)))

    solved = verified = failed = 0
    tally: dict[str, int] = {}
    for i, r in enumerate(picked):
        payload = _clean_payload(json.loads(r["MOMPROBLEMJSON"]))
        payload["BladeMargin"] = 0.0
        prob = parse_problem(payload)
        if not prob.groups:
            continue
        group = prob.groups[0]
        gres = route2_equiv.solve_group(group, tl)
        if gres is None:
            continue
        solved += 1
        m = gres["metrics"]
        norm_paths = {rec["path"] for rec in gres.get("input_normalizations", [])}
        solution = {
            "status": "FEASIBLE", "task_id": prob.task_id, "groups": [gres],
            "summary": {
                "group_count": 1,
                "demand_length": from_units(group.demand_length),
                "used_stock_length": m["used_stock_length"],
                "utilization_rate": m["utilization_rate"],
                "welding_joint_quantity": m["welding_joint_quantity"],
                "welding_pattern_type_quantity": m["welding_pattern_type_quantity"],
                "cutting_pattern_type_quantity": m["cutting_pattern_type_quantity"],
                "reused_welding_pattern_type_quantity":
                    m["reused_welding_pattern_type_quantity"],
                "reused_cutting_pattern_type_quantity":
                    m["reused_cutting_pattern_type_quantity"],
                "kerf_loss": m["kerf_loss"], "remainder_length": m["remainder_length"],
                "must_use_stock_quantity": m["must_use_stock_quantity"],
                "must_use_used_quantity": m["must_use_used_quantity"],
                "must_use_stock_length": m["must_use_stock_length"],
                "normalized_length_field_quantity": len(norm_paths),
                "target_reached": m["target_reached"],
            },
            "verification": {"passed": None, "issues": [], "source": "pending"},
        }
        v = verify_solution(payload, solution)
        if v.get("passed"):
            verified += 1
            print(f"  [{i+1}] {group.material} OK util={m['utilization_rate']:.3f} "
                  f"welds={m['welding_joint_quantity']}")
        else:
            failed += 1
            errs = [x for x in v.get("issues", []) if x.get("severity") == "error"]
            for e in errs[:3]:
                tally[e["code"]] = tally.get(e["code"], 0) + 1
            codes = ", ".join(sorted({e["code"] for e in errs}))
            print(f"  [{i+1}] {group.material} FAIL [{codes}]")

    print(f"\n==== app.route2_equiv.solve_group -> real verify ====")
    print(f"  solved: {solved}  verified: {verified}/{solved}  failed: {failed}")
    print(f"  errors: {tally or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
