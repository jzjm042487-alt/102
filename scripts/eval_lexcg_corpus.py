"""Corpus evaluation for lexcg and baseline.

The success set is every DGMOM record whose legacy result status is
``success`` and whose pipe demand is positive (1589 cases in the checked-in
file).  ``--resume`` appends and skips pairs already in the CSV.  Each
finished case is flushed immediately.  A process that is still alive
``time_limit + grace`` seconds after start is killed and recorded as
``TIMEOUT``.
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
    "time_limit_seconds",
    "failure_category",
    "case_index",
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


def failure_category(row: dict[str, Any]) -> str:
    """Bucket a finished row.  Solved+verified is the only non-failure."""

    status = str(row.get("solve_status") or "")
    versus = str(row.get("versus_legacy") or "")
    verified = str(row.get("verifier_passed")) in {"True", "true", "1"}
    used = row.get("used_stock_length")
    has_plan = used not in ("", None)
    if status == "TIMEOUT" or versus == "timeout":
        return "timeout"
    if status == "ERROR" or versus == "error":
        return "error"
    if has_plan and verified:
        return "solved_verified"
    if has_plan and not verified:
        return "verifier_failed"
    if status == "INFEASIBLE":
        return "infeasible"
    if str(row.get("pricing_truncated")) in {"True", "true", "1"}:
        return "unsolved_pricing_truncated"
    if status == "UNSOLVED":
        return "unsolved"
    return "unsolved_other"


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
                "versus_legacy": (
                    _versus(ours, legacy)
                    if solved and report.get("passed")
                    else ("verifier_failed" if solved else "unsolved")
                ),
                "error": "" if report.get("passed") or not solved else "verifier rejected the plan",
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
    row["time_limit_seconds"] = task.get("time_limit", "")
    row["case_index"] = task.get("case_index", "")
    row["failure_category"] = failure_category(row)
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
            "time_limit_seconds": task.get("time_limit", ""),
            "case_index": task.get("case_index", ""),
        }
    )
    row["failure_category"] = failure_category(row)
    return row


def _run_tasks(
    tasks: list[dict[str, Any]],
    workers: int,
    time_limit: float,
    *,
    on_row,
    grace: float,
) -> None:
    """Run cases in worker processes that are killed when a case overruns."""

    ctx = mp.get_context("spawn")
    pending = list(tasks)
    active: list[tuple[Any, Any, dict[str, Any], float]] = []
    finished = 0
    while pending or active:
        while pending and len(active) < workers:
            task = pending.pop(0)
            queue = ctx.Queue()
            proc = ctx.Process(target=_child, args=(task, queue))
            proc.start()
            active.append((proc, queue, task, time.perf_counter()))
        still = []
        progressed = False
        for proc, queue, task, started in active:
            proc.join(0.05)
            elapsed = time.perf_counter() - started
            if proc.is_alive():
                if elapsed > time_limit + grace:
                    proc.terminate()
                    proc.join(5)
                    if proc.is_alive():
                        proc.kill()
                    row = _timeout_row(task, elapsed)
                    finished += 1
                    progressed = True
                    on_row(row, finished)
                else:
                    still.append((proc, queue, task, started))
                continue
            progressed = True
            try:
                row = queue.get(timeout=2)
            except Exception as exc:  # noqa: BLE001
                row = _timeout_row(task, elapsed)
                row["solve_status"] = "ERROR"
                row["versus_legacy"] = "error"
                row["failure_category"] = "error"
                row["error"] = f"worker exited without a row: {exc}"
            finished += 1
            on_row(row, finished)
        active = still
        if not progressed:
            time.sleep(0.2)


def _load_done(path: Path) -> set[tuple[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open(encoding="utf-8", newline="") as handle:
        return {(row["case_id"], row["engine"]) for row in csv.DictReader(handle)}


def _append_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--engines", default="lexcg,baseline")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--grace",
        type=float,
        default=30.0,
        help="Seconds past the solver limit before the worker process is killed",
    )
    args = parser.parse_args()
    cases = load_success_cases(args.corpus)
    window = cases[args.offset : args.offset + args.limit if args.limit else None]
    engines = [item.strip() for item in args.engines.split(",") if item.strip()]
    done = _load_done(args.output) if args.resume else set()
    if not args.resume and args.output.exists():
        args.output.unlink()
    tasks = []
    for index, case in enumerate(window):
        for engine in engines:
            key = (case["case_id"], engine)
            if key in done:
                continue
            tasks.append(
                {
                    "case_id": case["case_id"],
                    "case_index": args.offset + index,
                    "problem": case["problem"],
                    "legacy": case["legacy"],
                    "engine": engine,
                    "time_limit": args.time_limit,
                }
            )
    print(
        f"success_cases={len(cases)} window={len(window)} todo={len(tasks)} "
        f"skipped={len(window) * len(engines) - len(tasks)} engines={engines} "
        f"time_limit={args.time_limit}s workers={args.workers}",
        flush=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    def _keep(row: dict[str, Any], finished: int) -> None:
        _append_row(args.output, row)
        print(
            f"[{finished}/{len(tasks)}] {row['engine']} {row['case_id']} "
            f"{row['solve_status']} {row['versus_legacy']} {row['failure_category']} "
            f"{row['elapsed_seconds']}s",
            flush=True,
        )

    if args.workers <= 1:
        for index, task in enumerate(tasks, start=1):
            _keep(_solve_case(task), index)
    else:
        _run_tasks(tasks, args.workers, args.time_limit, on_row=_keep, grace=args.grace)
    elapsed = time.perf_counter() - started
    print(f"wrote {args.output} new_rows={len(tasks)} wall_seconds={elapsed:.1f}", flush=True)


if __name__ == "__main__":
    main()
