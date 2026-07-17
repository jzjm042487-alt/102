"""A/B: does the acceleration provider救活硬组 without回退已解组？

For each sampled group we solve twice in one process:
  * baseline  = NESTING_ACCEL=cpu       (CPU no-op provider, historical pool)
  * accel     = NESTING_ACCEL from --accel-mode (default gpu-cpu = NumPy backend;
                use --accel-mode gpu for the real CuPy path on a CUDA host)

Solve quality is three-tiered (see ``_solve_quality``): 2=真解, 1=超时凑合,
0=无解.  The summary reports 救活 (0->{1,2}), 升级 (1->2, a timeout stopgap
becomes a proven solve) and 回退 (strictly worse tier -- a red-line failure of
the append-only guarantee).

用法:
    python scripts/benchmark_accel_ab.py [--status 1] [--sample 40] [--limit 40] \
        [--seed 42] [--time-limit 20] [--blade-margin 0] [--accel-mode gpu] \
        [--out <csv>]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Reuse the software-benchmark's record loader and payload cleaner.
from benchmark_against_software import _clean_payload, _load_records  # noqa: E402

_SOLVED = {
    "OPTIMAL_LEXICOGRAPHIC",
    "OPTIMAL",
    "FEASIBLE",
    "FEASIBLE_TARGET_REACHED",
    "TARGET_REACHED",
}


def _solve_quality(status: str | None) -> int:
    """Three-tier quality of a solve status.

    * 2 = a genuine solve (target reached / optimal / feasible within limits)
    * 1 = a time-limited *usable* plan (``TIMELIMIT_INCOMPLETE*``) -- a stopgap,
          not a proven answer, so distinct from tier 2
    * 0 = no usable plan (UNSOLVED, INFEASIBLE, empty, ...)
    """

    if not status:
        return 0
    if status in _SOLVED:
        return 2
    if status.startswith("TIMELIMIT_INCOMPLETE"):
        return 1
    return 0


def _solve(problem_json: str, mode: str, time_limit: float, blade_margin) -> dict[str, Any]:
    os.environ["NESTING_ACCEL"] = mode
    # The provider is process-cached; clear it so the mode switch takes effect.
    from app.accel import select_provider

    select_provider.cache_clear()
    from app.service import solve_and_verify

    payload = _clean_payload(json.loads(problem_json))
    if blade_margin is not None:
        payload["BladeMargin"] = blade_margin
    t0 = time.monotonic()
    result = solve_and_verify(payload, time_limit_seconds=time_limit)
    elapsed = time.monotonic() - t0
    groups = result.get("groups", [])
    m = groups[0].get("metrics", {}) if groups else {}
    return {
        "status": m.get("solve_status"),
        "util": m.get("utilization_rate"),
        "joints": m.get("welding_joint_quantity"),
        "verified": result.get("verification", {}).get("passed"),
        "elapsed": round(elapsed, 2),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--status", default="1")
    p.add_argument("--sample", type=int, default=40)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--time-limit", type=float, default=20.0)
    p.add_argument("--blade-margin", type=float, default=None)
    p.add_argument(
        "--accel-mode",
        default="gpu-cpu",
        choices=("gpu", "gpu-cpu", "auto"),
        help="accel arm: 'gpu' = real CuPy on device; 'gpu-cpu' = NumPy backend",
    )
    p.add_argument("--out", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = _load_records()
    wanted = {s.strip() for s in str(args.status).split(",") if s.strip()}
    cands = [
        r
        for r in records
        if r.get("MOMCALCULATESTATUS") in wanted
        and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")
    ]
    if args.sample > 0:
        cands = random.Random(args.seed).sample(cands, min(args.sample, len(cands)))
    picked = cands[args.offset : args.offset + args.limit]

    print(
        f"A/B {len(picked)} 组 (baseline=cpu vs accel={args.accel_mode}, "
        f"time_limit={args.time_limit}s, blade_margin={args.blade_margin})\n"
    )
    rescued = regressed = same = upgraded = 0
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(picked):
        spec = f'{r.get("MOMMATERIAL","")}/{r.get("MOMOUTSIDEDIAMETER","")}x{r.get("MOMWALLTHICKNESS","")}'
        pj = r["MOMPROBLEMJSON"]
        try:
            base = _solve(pj, "cpu", args.time_limit, args.blade_margin)
            acc = _solve(pj, args.accel_mode, args.time_limit, args.blade_margin)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}] {spec} ERROR {type(exc).__name__}: {exc}")
            continue
        b_q, a_q = _solve_quality(base["status"]), _solve_quality(acc["status"])
        # Rescue = 0 -> {1,2}; regression = anything -> strictly worse tier;
        # upgrade = 1 -> 2 (timeout stopgap becomes a proven solve).
        tag = "="
        if b_q == 0 and a_q > 0:
            rescued += 1
            tag = "救活"
        elif a_q < b_q:
            regressed += 1
            tag = "!!回退!!"
        elif b_q == 1 and a_q == 2:
            upgraded += 1
            tag = "升级(超时->真解)"
        else:
            same += 1
        print(
            f"  [{i+1}] {spec} | base={base['status']}({base['elapsed']}s) "
            f"accel={acc['status']}({acc['elapsed']}s) | {tag}"
        )
        rows.append({"spec": spec, **{f"base_{k}": v for k, v in base.items()},
                     **{f"accel_{k}": v for k, v in acc.items()}, "tag": tag})

    print(
        f"\n================ A/B 汇总 ================\n"
        f"救活 (无解->有解): {rescued}\n"
        f"升级 (超时凑合->真解): {upgraded}\n"
        f"回退 (质量变差): {regressed}\n"
        f"不变: {same}\n"
    )
    if args.out and rows:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        fields = sorted({k for row in rows for k in row})
        with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"逐组结果写入 {args.out}")
    # A regression (误杀) is a red-line failure of the append-only guarantee.
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
