"""单任务 worker: 对一个落盘案例跑指定引擎，stdout 打印 RESULT <json>。

用法:
  python scripts/_algorithm_sweep_worker.py <engine> <cases_root> <idx> <tl>
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PRODUCTION_ENGINES = frozenset({"baseline", "route3", "rcg", "global", "v4"})


def _metrics_from_service(result: dict) -> dict:
    groups = result.get("groups") or []
    if not groups:
        return {
            "solve_status": result.get("status"),
            "verified": (result.get("verification") or {}).get("passed"),
            "error": "no_groups",
        }
    g0 = groups[0]
    m = g0.get("metrics") or {}
    return {
        "solve_status": m.get("solve_status") or g0.get("status"),
        "verified": (result.get("verification") or {}).get("passed"),
        "utilization_rate": m.get("utilization_rate"),
        "welding_joints": m.get("welding_joint_quantity"),
        "weld_pattern_types": m.get("welding_pattern_type_quantity"),
        "cut_pattern_types": m.get("cutting_pattern_type_quantity"),
        "demand_length": m.get("demand_length"),
        "used_stock_length": m.get("used_stock_length"),
        "warnings": (g0.get("warnings") or [])[:3],
    }


def _run_production(payload: dict, engine: str, tl: float) -> dict:
    from app.service import solve_and_verify

    t0 = time.monotonic()
    result = solve_and_verify(payload, time_limit_seconds=tl, engine=engine)
    out = _metrics_from_service(result)
    out["elapsed_s"] = round(time.monotonic() - t0, 3)
    out["api_status"] = result.get("status")
    return out


def _run_arcflow_direct(payload: dict, tl: float, util_floor: float | None) -> dict:
    from backend.app.domain import parse_problem
    from scripts._arcflow_v3 import solve_arcflow
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem

    payload = clean_mom_problem(payload)
    t0 = time.monotonic()
    prob = parse_problem(payload)
    if len(prob.groups) != 1:
        return {
            "solve_status": "UNSUPPORTED_MULTI_GROUP",
            "verified": False,
            "error": f"groups={len(prob.groups)}",
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
    g = merge_equivalent_pipes(prob.groups[0])
    try:
        res = solve_arcflow(g, tl=tl, verbose=False, util_floor=util_floor)
    except Exception as exc:  # noqa: BLE001
        return {
            "solve_status": f"ERROR:{type(exc).__name__}",
            "verified": False,
            "error": str(exc)[:200],
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
    if res is None:
        return {
            "solve_status": "UNSOLVED",
            "verified": False,
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
    return {
        "solve_status": "SOLVED",
        "verified": None,
        "utilization_rate": res.get("util"),
        "welding_joints": res.get("joints"),
        "weld_pattern_types": res.get("weld_types"),
        "cut_pattern_types": res.get("cut_types"),
        "solver_path": res.get("solver"),
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


def main() -> None:
    engine = sys.argv[1]
    cases_root = Path(sys.argv[2])
    idx = int(sys.argv[3])
    tl = float(sys.argv[4])

    from scripts.legacy_corpus_lib import load_legacy_case

    case = load_legacy_case(cases_root, idx)
    payload = case["problem"]
    met = case["metrics"].get("metrics") or {}
    legacy = {
        "welding_joints": met.get("welding_joints"),
        "weld_pattern_types": met.get("weld_pattern_types"),
        "cut_pattern_types": met.get("cut_pattern_types"),
        "utilization_rate": met.get("utilization_rate"),
        "legacy_status": met.get("legacy_status"),
    }
    util_floor = met.get("utilization_rate")
    if util_floor is not None:
        try:
            util_floor = float(util_floor)
        except (TypeError, ValueError):
            util_floor = None
    if util_floor is None:
        tr = payload.get("Target_Util_Rate", 99.25)
        try:
            util_floor = max(0.95, float(tr) / 100 if float(tr) > 1.5 else float(tr))
        except (TypeError, ValueError):
            util_floor = 0.95

    row: dict = {"idx": idx, "engine": engine, "legacy": legacy}
    try:
        if engine in PRODUCTION_ENGINES:
            row.update(_run_production(payload, engine, tl))
        elif engine == "arcflow_direct":
            row.update(_run_arcflow_direct(payload, tl, util_floor))
        else:
            row["solve_status"] = "UNKNOWN_ENGINE"
            row["error"] = engine
    except Exception as exc:  # noqa: BLE001
        row["solve_status"] = f"ERROR:{type(exc).__name__}"
        row["error"] = str(exc)[:200]
        row["trace"] = traceback.format_exc()[-400:]

    print("RESULT " + json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
