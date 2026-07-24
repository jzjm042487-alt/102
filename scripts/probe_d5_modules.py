"""Smoke probe for the D5 role-module candidate path.

The role catalog is trained from the frozen D5 training partition. This probe
must be pointed at validation or holdout IDs when reporting generalization.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(ROOT / "backend")]


def _load_split(name: str) -> list[int]:
    manifest = ROOT / "data" / "acceptance" / "d5_teacher_modules" / "d5_only_support2" / "split_manifest.json"
    return [int(value) for value in json.loads(manifest.read_text(encoding="utf-8"))[name]]


def _solve_one(idx: int, tl: float) -> dict:
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
    from scripts.solve_d5_modules import solve_d5_modules

    started = time.monotonic()
    case = load_legacy_case(ROOT / "data" / "legacy-cases", idx)
    group = merge_equivalent_pipes(parse_problem(clean_mom_problem(case["problem"])).groups[0])
    metrics = (case.get("metrics") or {}).get("metrics") or {}
    legacy = {
        "joints": metrics.get("welding_joints"),
        "weld_types": metrics.get("weld_pattern_types"),
        "cut_types": metrics.get("cut_pattern_types"),
        "util": metrics.get("utilization_rate"),
    }
    solution = solve_d5_modules(group, tl=tl, legacy_metrics=legacy)
    return {
        "idx": idx,
        "solved": bool(solution.get("ok")),
        "auto_tag": solution.get("auto_tag", "unsolved"),
        "elapsed_s": round(time.monotonic() - started, 3),
        "legacy": legacy,
        "metrics": {
            key: solution.get(key)
            for key in ("joints", "weld_types", "cut_types", "util", "used_len")
        },
        "candidate_counts": solution.get("candidate_counts"),
        "role_limits": solution.get("role_limits"),
        "error": solution.get("error"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("validation", "holdout"), default="validation")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--tl", type=float, default=60)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "acceptance" / "d5_teacher_modules" / "role_probe.json")
    args = parser.parse_args()
    ids = _load_split(args.split)[: args.limit] if args.limit > 0 else _load_split(args.split)
    rows = []
    for idx in ids:
        row = _solve_one(idx, args.tl)
        rows.append(row)
        print(f"idx={idx} solved={row['solved']} tag={row['auto_tag']} elapsed={row['elapsed_s']}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"split": args.split, "cases": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
