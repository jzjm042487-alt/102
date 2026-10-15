"""Fill mother-bar counts for round-5 plans that reproduce the official four metrics.

This re-solves with the same stable limits as the round-5 full run. It does
not replace 用料 / 焊口 / 拼法种类 / 切法种类. A bar count is recorded only
when those four integers match docs/stable-round5-full.csv and the independent
verifier accepts the fresh plan.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import signal
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS_PATH = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
FULL = ROOT / "docs/stable-round5-full.csv"
OUT = ROOT / "docs/stable-round5-our-bars.csv"
CHAIN = 15.0
CPSAT = 15.0
ONDEMAND = 60.0
COLUMNS = [
    "case_id",
    "record_index",
    "用料",
    "焊口",
    "拼法种类",
    "切法种类",
    "母料根数",
    "四项一致",
    "校验通过",
    "耗时",
    "说明",
]

_RECORDS: list[dict] | None = None


def _init() -> None:
    global _RECORDS
    _RECORDS = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))["RECORDS"]


def _num(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def solve_case(task: dict[str, str]) -> dict[str, str]:
    from app.solver import solve_payload
    from app.verifier import verify_solution

    index = int(task["record_index"])
    records = _RECORDS or []
    record = records[index]
    raw = record["MOMPROBLEMJSON"]
    problem = json.loads(raw) if isinstance(raw, str) else raw
    started = time.monotonic()

    def _hard_stop(_signum, _frame):
        raise TimeoutError("母料根数复算到达硬时限")

    signal.signal(signal.SIGALRM, _hard_stop)
    signal.setitimer(signal.ITIMER_REAL, CHAIN + CPSAT + ONDEMAND + 20.0)
    row = {column: "" for column in COLUMNS}
    row["case_id"] = task["case_id"]
    row["record_index"] = task["record_index"]
    try:
        result = solve_payload(
            problem,
            time_limit_seconds=CHAIN,
            engine="stable",
            cpsat_time_limit=CPSAT,
            ondemand_time_limit=ONDEMAND,
        )
        prepared = result.pop("_stable_prepared_payload", problem)
        group = (result.get("groups") or [{}])[0]
        metrics = group.get("metrics") or {}
        used = _num(metrics.get("used_stock_length"))
        joints = _num(metrics.get("welding_joint_quantity"))
        weld_types = _num(metrics.get("welding_pattern_type_quantity"))
        cut_types = _num(metrics.get("cutting_pattern_type_quantity"))
        report = verify_solution(prepared, result)
        verified = report.get("passed") is True
        bars = _num((report.get("recomputed_metrics") or {}).get("used_stock_quantity"))
        fresh = (used, joints, weld_types, cut_types)
        official = (
            _num(task["用料"]),
            _num(task["焊口"]),
            _num(task["拼法种类"]),
            _num(task["切法种类"]),
        )
        matched = verified and fresh == official and bars is not None
        row.update(
            {
                "用料": "" if used is None else str(used),
                "焊口": "" if joints is None else str(joints),
                "拼法种类": "" if weld_types is None else str(weld_types),
                "切法种类": "" if cut_types is None else str(cut_types),
                "母料根数": str(bars) if matched and bars is not None else "",
                "四项一致": "是" if matched else "否",
                "校验通过": "是" if verified else "否",
                "说明": "" if matched else f"fresh {fresh} official {official}",
            }
        )
    except TimeoutError as exc:
        row["四项一致"] = "否"
        row["说明"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        row["四项一致"] = "否"
        row["说明"] = f"{type(exc).__name__}: {exc}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    row["耗时"] = f"{time.monotonic() - started:.3f}"
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    feas = [
        row
        for row in csv.DictReader(FULL.open(encoding="utf-8"))
        if row["是否可行"] == "是" and row["校验通过"] == "是"
    ]
    done: set[str] = set()
    if OUT.exists():
        done = {row["case_id"] for row in csv.DictReader(OUT.open(encoding="utf-8")) if row.get("case_id")}
    queue = [row for row in feas if row["case_id"] not in done]
    print(f"feasible {len(feas)} done {len(done)} pending {len(queue)} workers {args.workers}", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    new_file = not OUT.exists()
    handle = OUT.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        handle.flush()
    if not queue:
        handle.close()
        return
    context = multiprocessing.get_context("spawn")
    wrote = 0
    with ProcessPoolExecutor(
        max_workers=max(1, args.workers),
        mp_context=context,
        initializer=_init,
    ) as pool:
        futures = {pool.submit(solve_case, row): row["record_index"] for row in queue}
        for future in as_completed(futures):
            index = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"worker failed {index} {type(exc).__name__}: {exc}", flush=True)
                continue
            writer.writerow(row)
            handle.flush()
            wrote += 1
            if wrote % 25 == 0 or row["四项一致"] != "是":
                print(
                    f"wrote {wrote}/{len(queue)} {row['record_index']} "
                    f"match {row['四项一致']} bars {row['母料根数']} wall {row['耗时']}",
                    flush=True,
                )
    handle.close()
    rows = list(csv.DictReader(OUT.open(encoding="utf-8")))
    matched = sum(row["四项一致"] == "是" for row in rows)
    print(f"finished rows {len(rows)} matched {matched}", flush=True)


if __name__ == "__main__":
    main()
