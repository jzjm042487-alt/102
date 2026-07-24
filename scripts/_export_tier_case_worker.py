"""单案 V4 worker：stdout 最后一行 JSON。供 export_d1_full_compare 子进程硬超时调用。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
from scripts.solve_v4_production import solve as v4_solve


def _jsonable(obj):
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = json.dumps(list(k), ensure_ascii=False) if isinstance(k, tuple) else str(k)
            out[key] = _jsonable(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, set):
        return sorted(_jsonable(x) for x in obj)
    return str(obj)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idx", type=int, required=True)
    ap.add_argument("--tl", type=float, default=15.0)
    ap.add_argument("--util-floor", type=float, default=0.95)
    args = ap.parse_args()

    cases = ROOT / "data" / "legacy-cases"
    case = load_legacy_case(cases, args.idx)
    problem = clean_mom_problem(case["problem"])
    groups = parse_problem(problem).groups

    acc = {
        "joints": 0,
        "weld_types": 0,
        "cut_types": 0,
        "seg_types": 0,
        "used_len": 0.0,
        "demand_len": 0.0,
        "paths": [],
        "solvers": [],
        "parts": [],
    }
    t0 = time.monotonic()
    try:
        for g0 in groups:
            g = merge_equivalent_pipes(g0)
            sol = v4_solve(g, tl=args.tl, util_floor=args.util_floor, verbose=False)
            if sol is None:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "NO_SOLUTION",
                            "elapsed_s": round(time.monotonic() - t0, 4),
                            "solution": None,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            acc["joints"] += int(sol.get("joints") or 0)
            acc["weld_types"] += int(sol.get("weld_types") or 0)
            acc["cut_types"] += int(sol.get("cut_types") or 0)
            acc["seg_types"] += int(sol.get("seg_types") or 0)
            acc["used_len"] += float(sol.get("used_len") or 0)
            acc["demand_len"] += float(g.demand_length)
            acc["paths"].append(sol.get("solver_path"))
            acc["solvers"].append(sol.get("solver"))
            acc["parts"].append(_jsonable(sol))
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_s": round(time.monotonic() - t0, 4),
                    "solution": None,
                },
                ensure_ascii=False,
            )
        )
        return 0

    util = acc["demand_len"] / acc["used_len"] if acc["used_len"] > 0 else 0.0
    sol_out = {
        "ok": True,
        "joints": acc["joints"],
        "weld_types": acc["weld_types"],
        "cut_types": acc["cut_types"],
        "seg_types": acc["seg_types"],
        "used_len": acc["used_len"],
        "util": util,
        "solver": "+".join(str(s) for s in acc["solvers"] if s),
        "solver_path": acc["paths"],
        "elapsed_s": round(time.monotonic() - t0, 4),
        "groups": acc["parts"] if len(acc["parts"]) > 1 else acc["parts"][0],
    }
    print(
        json.dumps(
            {
                "ok": True,
                "error": None,
                "elapsed_s": sol_out["elapsed_s"],
                "solution": sol_out,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
