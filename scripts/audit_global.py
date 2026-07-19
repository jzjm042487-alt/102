"""Audit the global joint engine against sample library.

Usage:
  python scripts/audit_global.py --limit 10 --time-limit 30
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.service import solve_and_verify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--time-limit",
        type=float,
        required=True,
        help="Per-sample wall-clock budget in seconds (required input).",
    )
    parser.add_argument(
        "--samples",
        type=Path,
        default=REPO / "frontend-next" / "public" / "samples.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO / "data" / "audit" / "sample_audit_global.csv",
    )
    args = parser.parse_args()
    if args.time_limit <= 0:
        raise SystemExit("--time-limit must be positive")

    samples = json.loads(args.samples.read_text(encoding="utf-8"))["samples"]
    rows: list[dict] = []
    args.out.parent.mkdir(parents=True, exist_ok=True)

    for sample in samples[: args.limit]:
        started = time.monotonic()
        try:
            result = solve_and_verify(
                sample["problem"],
                time_limit_seconds=args.time_limit,
                engine="global",
            )
            status = result.get("status")
            verified = bool(result.get("verification", {}).get("passed"))
            metrics = (result.get("groups") or [{}])[0].get("metrics", {})
            util = float(metrics.get("utilization_rate") or 0.0)
            joints = int(metrics.get("welding_joint_quantity") or 0)
            cut_types = int(metrics.get("cutting_pattern_type_quantity") or 0)
            weld_types = int(metrics.get("welding_pattern_type_quantity") or 0)
            solve_status = str(metrics.get("solve_status") or "")
        except Exception as exc:  # noqa: BLE001
            status = f"ERROR:{type(exc).__name__}"
            verified = False
            util = joints = cut_types = weld_types = 0
            solve_status = str(exc)

        rows.append(
            {
                "id": sample.get("id"),
                "com": sample.get("com"),
                "material": sample.get("material"),
                "spec": sample.get("spec"),
                "elapsed_s": round(time.monotonic() - started, 2),
                "time_limit": args.time_limit,
                "status": status,
                "solve_status": solve_status,
                "verified": verified,
                "util": util,
                "joints": joints,
                "cut_types": cut_types,
                "weld_types": weld_types,
            }
        )
        print(
            f"{sample.get('id')} status={status} util={util:.4f} "
            f"verified={verified} t={rows[-1]['elapsed_s']}s",
            flush=True,
        )

    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
