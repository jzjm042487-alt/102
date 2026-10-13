"""Partial corpus evaluation for the lexcg engine.

The success set is every DGMOM record whose legacy result status is
``success`` and whose pipe demand is positive.  A full pass at 60 seconds
per case is about a day, so this script runs a prefix (``--limit`` /
``--offset``) at ``--time-limit`` seconds and writes one CSV row per case.
Timeouts and exceptions are recorded; they are not treated as infeasible.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import time
import traceback
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))
DEFAULT_CORPUS = (
    ROOT / "backend" / "data" / "samples" / "software-io" / "batch-DGMOM" / "software_records.json"
)
DEFAULT_CSV = ROOT / "docs" / "lexcg-corpus-results.csv"

CSV_FIELDS = [
    "case_id",
    "engine",
    "used_stock_length",
    "welding_joint_quantity",
    "welding_pattern_type_quantity",
    "cutting_pattern_type_quantity",
    "utilization_rate",
    "verifier_passed",
    "elapsed_seconds",
    "layer_used_stock_length",
    "layer_welding_joint_quantity",
    "layer_welding_pattern_type_quantity",
    "layer_cutting_pattern_type_quantity",
    "solve_status",
    "result_source",
    "pricing_truncated",
    "legacy_used_stock_length",
    "legacy_welding_joint_quantity",
    "legacy_welding_pattern_type_quantity",
    "legacy_cutting_pattern_type_quantity",
    "legacy_utilization_rate",
    "versus_legacy",
    "error",
]


def _parts(value: Any) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            try:
                out.append(int(round(float(item))))
            except (TypeError, ValueError):
                pass
        return tuple(out)
    text = str(value or "").replace("+", " ")
    out = []
    for token in text.split():
        try:
            out.append(int(round(float(token))))
        except (TypeError, ValueError):
            pass
    return tuple(out)


def legacy_lex_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Legacy pattern counts, with a 0-weld sequence counted as a weld type.

    The historical extractor dropped single-segment patterns.  The lexcg
    objective counts them, so the comparison uses this definition.
    """

    gi = result.get("GeneralInfo") or {}
    inner = result.get("Result") or {}
    weld_node = inner.get("WeldingPattern") or {}
    cut_node = inner.get("CuttingPattern") or {}
    if isinstance(weld_node, list):
        weld_rows = weld_node
    elif isinstance(weld_node, dict):
        weld_rows = weld_node.get("WeldingPipe") or []
    else:
        weld_rows = []
    if isinstance(cut_node, list):
        cut_rows = cut_node
    elif isinstance(cut_node, dict):
        cut_rows = cut_node.get("CuttingPipe") or []
    else:
        cut_rows = []
    weld_sigs: set[tuple[int, ...]] = set()
    joints = 0
    for pipe in weld_rows:
        patterns = pipe.get("Pattern") or [] if isinstance(pipe, dict) else []
        if isinstance(patterns, dict):
            patterns = [patterns]
        for pattern in patterns:
            if not isinstance(pattern, dict):
                continue
            parts = _parts(pattern.get("Part"))
            if not parts:
                continue
            try:
                quantity = int(round(float(pattern.get("Number", 1) or 1)))
            except (TypeError, ValueError):
                quantity = 1
            weld_sigs.add(parts)
            joints += max(0, len(parts) - 1) * quantity
    cut_sigs: set[tuple[int, tuple[int, ...]]] = set()
    for row in cut_rows:
        if not isinstance(row, dict):
            continue
        try:
            stock = int(round(float(row.get("Length", 0) or 0)))
        except (TypeError, ValueError):
            stock = 0
        parts = tuple(sorted(_parts(row.get("Part"))))
        if stock > 0 and parts:
            cut_sigs.add((stock, parts))
    try:
        used = int(round(float(gi.get("StockLength_Of_SingleMaterialSpecification") or 0)))
    except (TypeError, ValueError):
        used = None
    try:
        util = float(gi.get("UtilRate") or 0)
    except (TypeError, ValueError):
        util = None
    return {
        "used_stock_length": used,
        "welding_joint_quantity": joints,
        "welding_pattern_type_quantity": len(weld_sigs),
        "cutting_pattern_type_quantity": len(cut_sigs),
        "utilization_rate": util,
    }


def _as_payload(problem: Any) -> dict[str, Any]:
    if isinstance(problem, str):
        problem = json.loads(problem)
    if not isinstance(problem, dict):
        raise ValueError("problem is not an object")
    return problem


def load_success_cases(path: Path) -> list[dict[str, Any]]:
    from scripts.legacy_corpus_lib import clean_mom_problem, legacy_result_status

    document = json.loads(path.read_text(encoding="utf-8"))
    records = document["RECORDS"] if isinstance(document, dict) else document
    cases = []
    for record in records:
        raw_result = record.get("MOMRESULTJSON")
        if isinstance(raw_result, str):
            try:
                raw_result = json.loads(raw_result)
            except json.JSONDecodeError:
                continue
        if legacy_result_status(raw_result if isinstance(raw_result, dict) else None) != "success":
            continue
        problem = clean_mom_problem(_as_payload(record.get("MOMPROBLEMJSON")))
        demand = 0
        for pipe in problem.get("Pipe") or []:
            try:
                demand += int(float(pipe.get("pipe_demand") or 0))
            except (TypeError, ValueError):
                pass
        if demand <= 0:
            continue
        cases.append(
            {
                "case_id": str(record.get("ID") or record.get("MOMPROBLEMNO") or ""),
                "problem": problem,
                "legacy": legacy_lex_metrics(raw_result),
            }
        )
    return cases


def _layer_status(metrics: dict[str, Any], name: str) -> str:
    for layer in metrics.get("lexicographic_layers") or []:
        if layer.get("objective") == name:
            return str(layer.get("status") or "")
    return ""


def _versus(ours: tuple[Any, ...], legacy: dict[str, Any]) -> str:
    theirs = (
        legacy.get("used_stock_length"),
        legacy.get("welding_joint_quantity"),
        legacy.get("welding_pattern_type_quantity"),
        legacy.get("cutting_pattern_type_quantity"),
    )
    if any(item is None for item in ours) or any(item is None for item in theirs):
        return "incomplete"
    if ours == theirs:
        return "tie"
    return "better" if ours < theirs else "worse"


def _solve_case(task: dict[str, Any]) -> dict[str, Any]:
    from app.solver import solve_payload
    from app.verifier import verify_solution

    case_id = task["case_id"]
    engine = task["engine"]
    legacy = task["legacy"]
    row: dict[str, Any] = {field: "" for field in CSV_FIELDS}
    row.update(
        {
            "case_id": case_id,
            "engine": engine,
            "legacy_used_stock_length": legacy.get("used_stock_length"),
            "legacy_welding_joint_quantity": legacy.get("welding_joint_quantity"),
            "legacy_welding_pattern_type_quantity": legacy.get("welding_pattern_type_quantity"),
            "legacy_cutting_pattern_type_quantity": legacy.get("cutting_pattern_type_quantity"),
            "legacy_utilization_rate": legacy.get("utilization_rate"),
        }
    )
    started = time.perf_counter()
    try:
        result = solve_payload(
            task["problem"],
            time_limit_seconds=float(task["time_limit"]),
            engine=engine,
        )
        report = verify_solution(task["problem"], result)
        elapsed = time.perf_counter() - started
        metrics = result["groups"][0]["metrics"] if result.get("groups") else {}
        ours = (
            metrics.get("used_stock_length"),
            metrics.get("welding_joint_quantity"),
            metrics.get("welding_pattern_type_quantity"),
            metrics.get("cutting_pattern_type_quantity"),
        )
        solved = metrics.get("solve_status") not in {None, "UNSOLVED"} and ours[0] not in (None, 0)
        row.update(
            {
                "used_stock_length": ours[0] if solved else "",
                "welding_joint_quantity": ours[1] if solved else "",
                "welding_pattern_type_quantity": ours[2] if solved else "",
                "cutting_pattern_type_quantity": ours[3] if solved else "",
                "utilization_rate": metrics.get("utilization_rate") if solved else "",
                "verifier_passed": bool(report.get("passed")) if solved else False,
                "elapsed_seconds": round(elapsed, 3),
                "layer_used_stock_length": _layer_status(metrics, "used_stock_length"),
                "layer_welding_joint_quantity": _layer_status(metrics, "welding_joint_quantity"),
                "layer_welding_pattern_type_quantity": _layer_status(
                    metrics, "welding_pattern_type_quantity"
                ),
                "layer_cutting_pattern_type_quantity": _layer_status(
                    metrics, "cutting_pattern_type_quantity"
                ),
                "solve_status": result.get("status"),
                "result_source": metrics.get("result_source", ""),
                "pricing_truncated": metrics.get("pricing_truncated", ""),
                "versus_legacy": _versus(ours, legacy) if solved and report.get("passed") else "unsolved",
                "error": "",
            }
        )
    except Exception as exc:  # recorded, not hidden
        row.update(
            {
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "solve_status": "ERROR",
                "versus_legacy": "error",
                "verifier_passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        row["error"] = row["error"][:500]
        traceback.print_exc()
    return row


def _child(task: dict[str, Any], queue: Any) -> None:
    queue.put(_solve_case(task))


def _timeout_row(task: dict[str, Any], elapsed: float) -> dict[str, Any]:
    legacy = task["legacy"]
    row = {field: "" for field in CSV_FIELDS}
    row.update(
        {
            "case_id": task["case_id"],
            "engine": task["engine"],
            "elapsed_seconds": round(elapsed, 3),
            "solve_status": "TIMEOUT",
            "versus_legacy": "timeout",
            "verifier_passed": False,
            "error": "process exceeded the case budget and was terminated",
            "legacy_used_stock_length": legacy.get("used_stock_length"),
            "legacy_welding_joint_quantity": legacy.get("welding_joint_quantity"),
            "legacy_welding_pattern_type_quantity": legacy.get("welding_pattern_type_quantity"),
            "legacy_cutting_pattern_type_quantity": legacy.get("cutting_pattern_type_quantity"),
            "legacy_utilization_rate": legacy.get("utilization_rate"),
        }
    )
    return row


def _run_tasks(tasks: list[dict[str, Any]], workers: int, time_limit: float) -> list[dict[str, Any]]:
    """Run cases in a process pool that is killed when a case overruns."""

    ctx = mp.get_context("spawn")
    pending = list(enumerate(tasks))
    active: list[tuple[Any, Any, dict[str, Any], float, int]] = []
    rows: list[dict[str, Any]] = []
    finished = 0
    total = len(tasks)
    while pending or active:
        while pending and len(active) < workers:
            index, task = pending.pop(0)
            queue = ctx.Queue()
            proc = ctx.Process(target=_child, args=(task, queue))
            proc.start()
            active.append((proc, queue, task, time.perf_counter(), index))
        still = []
        progressed = False
        for proc, queue, task, started, index in active:
            proc.join(0.05)
            elapsed = time.perf_counter() - started
            if proc.is_alive():
                if elapsed > time_limit + 25:
                    proc.terminate()
                    proc.join(5)
                    if proc.is_alive():
                        proc.kill()
                    row = _timeout_row(task, elapsed)
                    rows.append(row)
                    finished += 1
                    progressed = True
                    print(
                        f"[{finished}/{total}] {row['engine']} {row['case_id']} TIMEOUT {row['elapsed_seconds']}s",
                        flush=True,
                    )
                else:
                    still.append((proc, queue, task, started, index))
                continue
            progressed = True
            try:
                row = queue.get(timeout=2)
            except Exception as exc:  # noqa: BLE001
                row = _timeout_row(task, elapsed)
                row["solve_status"] = "ERROR"
                row["versus_legacy"] = "error"
                row["error"] = f"worker exited without a row: {exc}"
            rows.append(row)
            finished += 1
            print(
                f"[{finished}/{total}] {row['engine']} {row['case_id']} "
                f"{row['solve_status']} {row['versus_legacy']} {row['elapsed_seconds']}s",
                flush=True,
            )
        active = still
        if not progressed:
            time.sleep(0.2)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--time-limit", type=float, default=8.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--engines", default="lexcg,baseline")
    args = parser.parse_args()
    cases = load_success_cases(args.corpus)
    window = cases[args.offset : args.offset + args.limit if args.limit else None]
    engines = [item.strip() for item in args.engines.split(",") if item.strip()]
    tasks = [
        {
            "case_id": case["case_id"],
            "problem": case["problem"],
            "legacy": case["legacy"],
            "engine": engine,
            "time_limit": args.time_limit,
        }
        for case in window
        for engine in engines
    ]
    print(
        f"success_cases={len(cases)} running={len(window)} engines={engines} "
        f"time_limit={args.time_limit}s workers={args.workers}",
        flush=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if args.workers <= 1:
        rows = []
        for index, task in enumerate(tasks, start=1):
            row = _solve_case(task)
            rows.append(row)
            print(
                f"[{index}/{len(tasks)}] {row['engine']} {row['case_id']} "
                f"{row['solve_status']} {row['versus_legacy']}",
                flush=True,
            )
    else:
        rows = _run_tasks(tasks, args.workers, args.time_limit)
    rows.sort(key=lambda item: (item["case_id"], item["engine"]))
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    elapsed = time.perf_counter() - started
    print(f"wrote {args.output} rows={len(rows)} wall_seconds={elapsed:.1f}", flush=True)


if __name__ == "__main__":
    main()
