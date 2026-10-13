"""Resumable feasibility evaluation for ``engine="stable"``.

``--time-limit`` is the existing strategy chain. ``--cpsat-time-limit`` is
a separate clock that starts only after that chain fails. The default
``--split dev`` excludes the frozen blind cases in
``docs/stable-round3-split.json``. ``blind`` and ``all`` require
``--confirm-blind``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
DEFAULT_CSV = ROOT / "docs/stable-round3-results.csv"
ROUND2_CSV = ROOT / "docs/stable-feasibility-results.csv"
SPLIT_JSON = ROOT / "docs/stable-round3-split.json"

COLUMNS = [
    "case_id",
    "record_index",
    "是否可行",
    "用料",
    "焊口",
    "拼法种类",
    "切法种类",
    "耗时",
    "策略来源",
    "失败类",
    "失败说明",
    "利用率",
    "校验通过",
    "状态",
    "兜底耗时",
]


def _positive_pipe_demand(problem: dict) -> bool:
    total = 0.0
    stack: list = [problem]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            pipes = node.get("Pipe")
            if isinstance(pipes, list):
                for pipe in pipes:
                    if isinstance(pipe, dict):
                        try:
                            total += float(pipe.get("pipe_demand") or 0)
                        except (TypeError, ValueError):
                            pass
            else:
                stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return total > 0


def legacy_success_indexes(records: list[dict]) -> list[int]:
    """Positive-demand rows whose legacy ``GeneralInfo.Result`` starts with success."""

    chosen: list[int] = []
    for index, record in enumerate(records):
        raw_result = record.get("MOMRESULTJSON")
        raw_problem = record.get("MOMPROBLEMJSON")
        if not raw_result or not raw_problem:
            continue
        try:
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            problem = json.loads(raw_problem) if isinstance(raw_problem, str) else raw_problem
        except (TypeError, json.JSONDecodeError):
            continue
        message = str((result.get("GeneralInfo") or {}).get("Result", "")).strip()
        if not message.lower().startswith("success"):
            continue
        if not _positive_pipe_demand(problem):
            continue
        chosen.append(index)
    return chosen


def _load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["case_id"] for row in csv.DictReader(handle) if row.get("case_id")}


def solve_index(index: int, time_limit: float, cpsat_time_limit: float) -> dict[str, str]:
    from app.solver import solve_payload
    from app.verifier import verify_solution

    records = json.loads(RECORDS.read_text(encoding="utf-8"))["RECORDS"]
    record = records[index]
    raw = record["MOMPROBLEMJSON"]
    problem = json.loads(raw) if isinstance(raw, str) else raw
    started = time.monotonic()
    failure = ""
    feasible = False
    verified = False
    status = "INPUT_ERROR"
    strategy = ""
    failure_class = "INPUT_ERROR"
    used = ""
    joints = ""
    weld_types = ""
    cut_types = ""
    utilization = ""
    cpsat_elapsed = ""
    try:
        result = solve_payload(
            problem,
            time_limit_seconds=time_limit,
            engine="stable",
            cpsat_time_limit=cpsat_time_limit,
        )
        prepared = result.pop("_stable_prepared_payload", problem)
        status = str(result.get("status") or "")
        group = (result.get("groups") or [{}])[0]
        metrics = group.get("metrics") or {}
        strategy = str(metrics.get("stable_strategy") or "")
        failure_class = str(group.get("failure_class") or "")
        failure = str(group.get("failure_message") or "")
        used = metrics.get("used_stock_length", "")
        joints = metrics.get("welding_joint_quantity", "")
        weld_types = metrics.get("welding_pattern_type_quantity", "")
        cut_types = metrics.get("cutting_pattern_type_quantity", "")
        summary = result.get("summary") or {}
        utilization = summary.get("utilization_rate", "")
        cpsat_elapsed = metrics.get("cpsat_elapsed_seconds", "")
        if status == "FEASIBLE":
            verified = verify_solution(prepared, result).get("passed") is True
            feasible = verified
            if not verified:
                failure_class = "VERIFICATION_FAILED"
                failure = "求解器给出了计划，但独立校验未通过"
        elif not failure_class:
            failure_class = "UNPROVED_NO_SOLUTION"
    except Exception as exc:  # noqa: BLE001
        status = "INPUT_ERROR"
        failure_class = "INPUT_ERROR"
        failure = f"{type(exc).__name__}: {exc}"
    return {
        "case_id": str(record.get("ID") or index),
        "record_index": str(index),
        "是否可行": "是" if feasible else "否",
        "用料": used,
        "焊口": joints,
        "拼法种类": weld_types,
        "切法种类": cut_types,
        "耗时": f"{time.monotonic() - started:.3f}",
        "策略来源": strategy,
        "失败类": "" if feasible else failure_class,
        "失败说明": "" if feasible else failure[:300],
        "利用率": utilization if feasible else "",
        "校验通过": "是" if verified else "否",
        "状态": status,
        "兜底耗时": cpsat_elapsed,
    }


def _selected_indexes(kind: str) -> set[int] | None:
    """Return the record indexes for one frozen split.

    ``all`` is the only mode that includes the blind cases, and the caller
    has to pass ``--confirm-blind`` before this function is used for it.
    """

    if kind == "all":
        return None
    if kind == "regression":
        chosen: set[int] = set()
        with ROUND2_CSV.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("是否可行") == "是":
                    chosen.add(int(row["record_index"]))
        return chosen
    data = json.loads(SPLIT_JSON.read_text(encoding="utf-8"))
    key = "dev_record_indexes" if kind == "dev" else "blind_record_indexes"
    return {int(index) for index in data[key]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--time-limit", type=float, default=15.0)
    parser.add_argument(
        "--cpsat-time-limit",
        type=float,
        default=15.0,
        help="Independent budget for the last-stage catalogue search. Not taken from --time-limit.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--limit-cases", type=int, default=0)
    parser.add_argument(
        "--split",
        choices=["dev", "blind", "regression", "all"],
        default="dev",
        help="dev is the tuning set. blind and all require --confirm-blind.",
    )
    parser.add_argument("--confirm-blind", action="store_true")
    args = parser.parse_args()
    if args.split in {"blind", "all"} and not args.confirm_blind:
        print("refusing to run the blind split without --confirm-blind", flush=True)
        return 2

    records = json.loads(RECORDS.read_text(encoding="utf-8"))["RECORDS"]
    indexes = legacy_success_indexes(records)
    selected = _selected_indexes(args.split)
    if selected is not None:
        indexes = [index for index in indexes if index in selected]
    if args.limit_cases:
        indexes = indexes[: args.limit_cases]
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(args.csv)
    pending = []
    for index in indexes:
        case_id = str(records[index].get("ID") or index)
        if case_id not in done:
            pending.append(index)
    print(
        f"split {args.split} corpus {len(indexes)} done {len(done)} pending {len(pending)} "
        f"chain {args.time_limit}s cpsat {args.cpsat_time_limit}s",
        flush=True,
    )
    new_file = not args.csv.exists()
    handle = args.csv.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        handle.flush()
    finished = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(solve_index, index, args.time_limit, args.cpsat_time_limit)
            for index in pending
        ]
        for future in as_completed(futures):
            row = future.result()
            writer.writerow(row)
            handle.flush()
            finished += 1
            if finished % 25 == 0 or finished == len(pending):
                print(f"wrote {finished}/{len(pending)} last={row['case_id']} {row['是否可行']}", flush=True)
    handle.close()
    _print_summary(args.csv)
    return 0


def _print_summary(path: Path) -> None:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    total = len(rows)
    ok = sum(1 for row in rows if row["是否可行"] == "是")
    classes = Counter(row["失败类"] for row in rows if row["是否可行"] != "是")
    strategies = Counter(row["策略来源"] for row in rows if row["是否可行"] == "是")
    print(f"summary {ok}/{total} = {ok / total:.3%} failures {dict(classes)}", flush=True)
    print(f"strategies {dict(strategies)}", flush=True)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "backend"))
    raise SystemExit(main())
