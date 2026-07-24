"""acceptance_113146 子进程 worker：单引擎单题，stdout 最后一行 JSON。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--path", required=True)
    ap.add_argument("--tl", type=float, default=90.0)
    args = ap.parse_args()

    path = Path(args.path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    from scripts.legacy_corpus_lib import clean_mom_problem

    payload = clean_mom_problem(payload)
    t0 = time.monotonic()
    try:
        from app.service import solve_and_verify

        result = solve_and_verify(
            payload, time_limit_seconds=args.tl, engine=args.engine
        )
        sec = time.monotonic() - t0
        groups = result.get("groups") or []
        g0 = groups[0] if groups else {}
        m = g0.get("metrics") or {}
        ver = (result.get("verification") or {}).get("passed")
        out = {
            "engine": args.engine,
            "ok": bool(groups) and ver is not False,
            "joints": m.get("welding_joint_quantity", m.get("total_joints", m.get("joints"))),
            "weld_types": m.get(
                "welding_pattern_type_quantity", m.get("weld_pattern_types", m.get("weld_types"))
            ),
            "cut_types": m.get(
                "cutting_pattern_type_quantity", m.get("cut_pattern_types", m.get("cut_types"))
            ),
            "util": m.get("utilization_rate", m.get("utilization", m.get("util"))),
            "verified": ver,
            "solve_status": m.get("solve_status"),
            "solver_path": m.get("solver_path"),
            "elapsed_s": sec,
        }
    except Exception as exc:  # noqa: BLE001
        out = {
            "engine": args.engine,
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "elapsed_s": time.monotonic() - t0,
        }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
