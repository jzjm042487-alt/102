from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.domain import parse_problem  # noqa: E402
from app.service import solve_and_verify  # noqa: E402


def _parts(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(round(float(item))))
            except (TypeError, ValueError):
                pass
        return out
    text = str(value or "").replace("+", " ")
    out = []
    for token in text.split():
        try:
            out.append(int(round(float(token))))
        except (TypeError, ValueError):
            pass
    return out


def legacy_metrics(legacy: Any) -> dict[str, Any] | None:
    if isinstance(legacy, str):
        try:
            legacy = json.loads(legacy)
        except json.JSONDecodeError:
            return None
    if not isinstance(legacy, dict):
        return None
    result = legacy.get("Result") or legacy
    general = legacy.get("GeneralInfo") or {}
    cutting = ((result.get("CuttingPattern") or {}).get("CuttingPipe")) or result.get("cutting_patterns") or []
    welding = ((result.get("WeldingPattern") or {}).get("WeldingPipe")) or result.get("welding_patterns") or []
    if isinstance(cutting, dict):
        cutting = [cutting]
    if isinstance(welding, dict):
        welding = [welding]

    cut_sigs: set[tuple[int, tuple[int, ...]]] = set()
    used_len = 0
    for row in cutting:
        if not isinstance(row, dict):
            continue
        try:
            length = int(round(float(row.get("Length") or row.get("stock_length") or row.get("Pipe_length") or 0)))
        except (TypeError, ValueError):
            length = 0
        parts = tuple(sorted(_parts(row.get("Part") or row.get("parts") or [])))
        try:
            qty = int(round(float(row.get("Number") or row.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        if length and parts:
            cut_sigs.add((length, parts))
            used_len += length * qty

    weld_sigs: set[tuple[int, ...]] = set()
    joints = 0
    for row in welding:
        if not isinstance(row, dict):
            continue
        patterns = row.get("Pattern")
        if patterns is None:
            patterns = [row]
        elif isinstance(patterns, dict):
            patterns = [patterns]
        elif not isinstance(patterns, list):
            patterns = [{"Part": patterns, "Number": row.get("Number", 1)}]
        for pat in patterns:
            if isinstance(pat, dict):
                parts = tuple(_parts(pat.get("Part") or pat.get("parts") or []))
                try:
                    qty = int(round(float(pat.get("Number") or pat.get("quantity") or row.get("Number") or 1)))
                except (TypeError, ValueError):
                    qty = 1
            else:
                parts = tuple(_parts(pat))
                qty = 1
            if parts:
                if len(parts) >= 2:
                    weld_sigs.add(parts)
                joints += max(0, len(parts) - 1) * qty

    try:
        util = float(general.get("UtilRate") or general.get("utilization_rate") or 0)
    except (TypeError, ValueError):
        util = 0.0
    return {
        "legacy_util": util,
        "legacy_joints": joints,
        "legacy_cut_types": len(cut_sigs),
        "legacy_weld_types": len(weld_sigs),
        "legacy_used_len": used_len or None,
    }


def problem_stats(problem: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_problem(problem)
    groups = parsed.groups
    total_demand = sum(group.demand_length for group in groups)
    total_stock = sum(group.stock_length for group in groups)
    total_pipes = sum(len(group.pipes) for group in groups)
    total_pipe_qty = sum(sum(pipe.demand for pipe in group.pipes) for group in groups)
    return {
        "groups": len(groups),
        "pipe_types": total_pipes,
        "pipe_qty": total_pipe_qty,
        "demand_length": total_demand,
        "stock_length": total_stock,
        "stock_ratio": total_stock / total_demand if total_demand else None,
    }


def classify(row: dict[str, Any]) -> str:
    if row.get("parse_error"):
        return "A_PARSE_ERROR"
    ratio = row.get("stock_ratio")
    if isinstance(ratio, (int, float)) and ratio < 1:
        return "A_LENGTH_SHORTFALL"
    status = str(row.get("solve_status") or row.get("result_status") or "")
    if status in {"LENGTH_SHORTFALL", "INFEASIBLE"}:
        return "A_PHYSICAL_OR_PROCESS_INFEASIBLE"
    if status in {"UNSOLVED", "INCONCLUSIVE", "NO_COLUMNS"} or status.startswith("ERROR"):
        return "C_ALGORITHM_UNSOLVED"
    if row.get("verified") is not True:
        return "C_VERIFICATION_FAILED"
    legacy_joints = row.get("legacy_joints")
    ours_joints = row.get("ours_joints")
    legacy_util = row.get("legacy_util")
    ours_util = row.get("ours_util")
    legacy_cut = row.get("legacy_cut_types")
    ours_cut = row.get("ours_cut_types")
    legacy_weld = row.get("legacy_weld_types")
    ours_weld = row.get("ours_weld_types")
    if all(isinstance(v, (int, float)) for v in [legacy_joints, ours_joints, legacy_util, ours_util]):
        if ours_joints > legacy_joints or ours_util < legacy_util - 1e-4:
            return "D_SOLVED_BUT_WORSE"
        if isinstance(legacy_cut, (int, float)) and isinstance(ours_cut, (int, float)) and ours_cut > legacy_cut:
            return "D_SOLVED_BUT_WORSE"
        if isinstance(legacy_weld, (int, float)) and isinstance(ours_weld, (int, float)) and ours_weld > legacy_weld:
            return "D_SOLVED_BUT_WORSE"
    return "E_PRODUCTION_CANDIDATE"


def audit_sample(sample: dict[str, Any], engine: str, time_limit: float) -> dict[str, Any]:
    started = time.monotonic()
    row: dict[str, Any] = {
        "id": sample.get("id"),
        "com": sample.get("com"),
        "material": sample.get("material"),
        "spec": sample.get("spec"),
        "engine": engine,
    }
    try:
        row.update(problem_stats(sample["problem"]))
    except Exception as exc:  # noqa: BLE001
        row["parse_error"] = f"{type(exc).__name__}: {exc}"
        row["category"] = classify(row)
        row["elapsed_s"] = round(time.monotonic() - started, 2)
        return row

    lm = legacy_metrics(sample.get("legacy"))
    if lm:
        row.update(lm)

    try:
        result = solve_and_verify(sample["problem"], time_limit_seconds=time_limit, engine=engine)
        row["result_status"] = result.get("status")
        row["verified"] = result.get("verification", {}).get("passed")
        groups = result.get("groups") or []
        if groups:
            metrics = groups[0].get("metrics", {})
            row.update(
                {
                    "solve_status": metrics.get("solve_status"),
                    "ours_util": metrics.get("utilization_rate"),
                    "ours_joints": metrics.get("welding_joint_quantity"),
                    "ours_cut_types": metrics.get("cutting_pattern_type_quantity"),
                    "ours_weld_types": metrics.get("welding_pattern_type_quantity"),
                    "ours_used_len": metrics.get("used_stock_length"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        row["solve_status"] = f"ERROR:{type(exc).__name__}"
        row["error"] = str(exc)
    row["elapsed_s"] = round(time.monotonic() - started, 2)
    row["category"] = classify(row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit sample solve outcomes into root-cause buckets.")
    parser.add_argument("--samples", type=Path, default=ROOT / "frontend-next" / "public" / "samples.json")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--time-limit", type=float, default=8.0)
    parser.add_argument("--engine", choices=["baseline", "route3"], default="route3")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "audit" / "sample_audit.csv")
    args = parser.parse_args()

    data = json.loads(args.samples.read_text(encoding="utf-8"))
    samples = data.get("samples", [])
    picked = samples[args.offset : args.offset + args.limit]
    rows = []
    for index, sample in enumerate(picked, 1):
        row = audit_sample(sample, args.engine, args.time_limit)
        rows.append(row)
        print(
            f"[{index}/{len(picked)}] {row.get('id')} {row.get('category')} "
            f"status={row.get('solve_status')} verified={row.get('verified')} elapsed={row.get('elapsed_s')}s"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row.get("category") for row in rows)
    print("\nSUMMARY")
    for key, count in counts.most_common():
        pct = count / len(rows) * 100 if rows else 0
        print(f"  {key}: {count}/{len(rows)} ({pct:.1f}%)")
    print(f"CSV: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
