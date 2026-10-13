"""Run the frozen 20-case round-4 pilot and stop.

The case list is ``docs/stable-round4-pilot-split.json`` (seed 20261014,
drawn from the development split). This script refuses any index that also
sits in the seed-20261013 blind split. It does not accept a blind or full
corpus mode.

``--time-limit`` is the existing strategy chain, ``--cpsat-time-limit`` is
the catalogue stage, and ``--ondemand-time-limit`` is the on-demand stage.
The three clocks are independent. The on-demand default is 60s.
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
PILOT_JSON = ROOT / "docs/stable-round4-pilot-split.json"
SPLIT_JSON = ROOT / "docs/stable-round3-split.json"
DEFAULT_CSV = ROOT / "docs/stable-round4-pilot.csv"

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
    "按需耗时",
    "按需说明",
    "档位记录",
    "分类",
]


def _classify(feasible: bool, failure_class: str) -> str:
    if feasible:
        return "解出"
    if failure_class == "STOCK_GAP":
        return "证明不可行"
    if failure_class == "TIMEOUT_NO_SOLUTION":
        return "超时未知"
    if failure_class == "VERIFICATION_FAILED":
        return "校验失败"
    return "缺口说明"


def solve_index(
    index: int,
    time_limit: float,
    cpsat_time_limit: float,
    ondemand_time_limit: float,
) -> dict[str, str]:
    from app.solver import solve_payload
    from app.verifier import verify_solution

    records = json.loads(RECORDS.read_text(encoding="utf-8"))["RECORDS"]
    record = records[index]
    raw = record["MOMPROBLEMJSON"]
    problem = json.loads(raw) if isinstance(raw, str) else raw
    started = time.monotonic()

    def _hard_stop(_signum, _frame):
        raise TimeoutError("试点进程到达硬时限")

    # Backstop in case the solver clock is overrun. The stage itself also
    # stops on its deadline; this only forces the worker to return.
    signal.signal(signal.SIGALRM, _hard_stop)
    signal.setitimer(
        signal.ITIMER_REAL,
        max(1.0, time_limit + cpsat_time_limit + ondemand_time_limit + 20.0),
    )
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
    ondemand_elapsed = ""
    ondemand_note = ""
    tier_log = ""
    try:
        result = solve_payload(
            problem,
            time_limit_seconds=time_limit,
            engine="stable",
            cpsat_time_limit=cpsat_time_limit,
            ondemand_time_limit=ondemand_time_limit,
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
        ondemand_elapsed = metrics.get("ondemand_elapsed_seconds", "")
        ondemand_note = str(metrics.get("ondemand_note") or "")
        tier_log = str(metrics.get("ondemand_tier_log") or "")
        if not ondemand_note and "按需生成" in failure:
            ondemand_note = failure
        if status == "FEASIBLE":
            verified = verify_solution(prepared, result).get("passed") is True
            feasible = verified
            if not verified:
                failure_class = "VERIFICATION_FAILED"
                failure = "求解器给出了计划，但独立校验未通过"
        elif not failure_class:
            failure_class = "UNPROVED_NO_SOLUTION"
    except TimeoutError as exc:
        status = "TIMEOUT"
        failure_class = "TIMEOUT_NO_SOLUTION"
        failure = f"{exc}，按需生成结果未知"
    except Exception as exc:  # noqa: BLE001
        status = "INPUT_ERROR"
        failure_class = "INPUT_ERROR"
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
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
        "失败说明": "" if feasible else failure[:500],
        "利用率": utilization if feasible else "",
        "校验通过": "是" if verified else "否",
        "状态": status,
        "兜底耗时": cpsat_elapsed,
        "按需耗时": ondemand_elapsed,
        "按需说明": "" if feasible else ondemand_note[:500],
        "档位记录": tier_log[:500],
        "分类": _classify(feasible, failure_class),
    }


def _load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["case_id"] for row in csv.DictReader(handle) if row.get("case_id")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time-limit", type=float, default=15.0)
    parser.add_argument("--cpsat-time-limit", type=float, default=15.0)
    parser.add_argument("--ondemand-time-limit", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    pilot = json.loads(PILOT_JSON.read_text(encoding="utf-8"))
    split = json.loads(SPLIT_JSON.read_text(encoding="utf-8"))
    indexes = [int(index) for index in pilot["record_indexes"]]
    blind = set(split["blind_record_indexes"])
    if set(indexes) & blind:
        print("refusing: pilot list overlaps the blind split", flush=True)
        return 2
    if len(indexes) != 20:
        print("refusing: pilot list is not the frozen 20-case sample", flush=True)
        return 2

    records = json.loads(RECORDS.read_text(encoding="utf-8"))["RECORDS"]
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(args.csv)
    pending = []
    for index in indexes:
        case_id = str(records[index].get("ID") or index)
        if case_id not in done:
            pending.append(index)
    print(
        f"pilot {len(indexes)} done {len(done)} pending {len(pending)} "
        f"chain {args.time_limit}s cpsat {args.cpsat_time_limit}s "
        f"ondemand {args.ondemand_time_limit}s",
        flush=True,
    )
    new_file = not args.csv.exists()
    handle = args.csv.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        handle.flush()
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(
                solve_index,
                index,
                args.time_limit,
                args.cpsat_time_limit,
                args.ondemand_time_limit,
            )
            for index in pending
        ]
        for future in as_completed(futures):
            row = future.result()
            writer.writerow(row)
            handle.flush()
            print(
                f"{row['record_index']} {row['分类']} {row['是否可行']} "
                f"ondemand {row['按需耗时']} total {row['耗时']} {row['策略来源']} "
                f"{row['档位记录']}",
                flush=True,
            )
    handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
