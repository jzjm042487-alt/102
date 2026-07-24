"""Diagnose one benchmark group by global idx (status=1, max-demand<=200 filter)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
for p in (str(BACKEND), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from benchmark_against_software import (  # noqa: E402
    _clean_payload, _load_records, _software_metrics, _total_demand,
)


def _cands():
    recs = _load_records()
    c = [r for r in recs if r.get("MOMCALCULATESTATUS") == "1"
         and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")]
    return [r for r in c if 0 <= _total_demand(r.get("MOMPROBLEMJSON", "")) <= 200]


def main() -> int:
    idxs = [int(x) for x in sys.argv[1:2]] or [44]
    tl = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    cands = _cands()
    from app.service import solve_and_verify
    for idx in idxs:
        r = cands[idx]
        spec = f'{r.get("MOMMATERIAL","")}/{r.get("MOMOUTSIDEDIAMETER","")}x{r.get("MOMWALLTHICKNESS","")}'
        sw = _software_metrics(r["MOMRESULTJSON"])
        payload = _clean_payload(json.loads(r["MOMPROBLEMJSON"]))
        t0 = time.monotonic()
        res = solve_and_verify(payload, time_limit_seconds=tl, engine="rcg")
        dt = time.monotonic() - t0
        g = res.get("groups", [])
        m = g[0].get("metrics", {}) if g else {}
        ok = res.get("verification", {}).get("passed")
        print(f"\n=== idx {idx} spec={spec} demand={_total_demand(r['MOMPROBLEMJSON'])} ({dt:.1f}s) ===")
        print(f"  软件 util={sw.get('util_rate')} joints={sw.get('welding_joints')} "
              f"cutT={sw.get('cut_pattern_types')} weldT={sw.get('weld_pattern_types')}")
        print(f"  我们 status={m.get('solve_status')} util={m.get('utilization_rate')} "
              f"joints={m.get('welding_joint_quantity')} "
              f"cutT={m.get('cutting_pattern_type_quantity')} "
              f"weldT={m.get('welding_pattern_type_quantity')} verified={ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
