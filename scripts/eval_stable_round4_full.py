"""Full feasibility run for the stable engine, including on-demand pricing.

Phases, in the order they must be executed:

* ``solved-nonblind`` — the round-3 feasible cases that are not in the
  frozen blind split (1429 regression rows plus 14 development solves).
* ``dev-unsolved`` — development cases that round 3 left unsolved.
* ``blind`` — the seed-20261013 blind split, once. Requires
  ``--confirm-blind``. A completed blind CSV is not solved again.

``--time-limit`` is the existing strategy chain, ``--cpsat-time-limit`` is
the catalogue stage, and ``--ondemand-time-limit`` is the on-demand stage.
The three clocks are independent. Defaults are 15s, 15s, and 60s.
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS_PATH = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
SPLIT_JSON = ROOT / "docs/stable-round3-split.json"
ROUND3_REGRESSION = ROOT / "docs/stable-round3-regression.csv"
ROUND3_DEV = ROOT / "docs/stable-round3-dev.csv"
ROUND3_BLIND = ROOT / "docs/stable-round3-blind.csv"

PHASE_CSV = {
    "solved-nonblind": ROOT / "docs/stable-round4-solved-nonblind.csv",
    "dev-unsolved": ROOT / "docs/stable-round4-dev-unsolved.csv",
    "blind": ROOT / "docs/stable-round4-blind.csv",
}
PHASE_LABEL = {
    "solved-nonblind": "已解非盲测",
    "dev-unsolved": "开发未解",
    "blind": "盲测",
}
COMPARE_CSV = ROOT / "docs/stable-round4-regression-compare.csv"
FULL_CSV = ROOT / "docs/stable-round4-full.csv"

COLUMNS = [
    "case_id",
    "record_index",
    "集合",
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
COMPARE_COLUMNS = [
    "case_id",
    "record_index",
    "集合",
    "旧是否可行",
    "新是否可行",
    "校验通过",
    "旧用料",
    "新用料",
    "用料变化",
    "旧焊口",
    "新焊口",
    "焊口变化",
    "旧拼法种类",
    "新拼法种类",
    "拼法变化",
    "旧切法种类",
    "新切法种类",
    "切法变化",
    "旧策略",
    "新策略",
    "质量变化",
    "退化",
]

_RECORDS: list[dict] | None = None


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _indexes(path: Path, *, feasible_only: bool | None) -> list[int]:
    chosen: list[int] = []
    for row in _load_rows(path):
        if feasible_only is True and row.get("是否可行") != "是":
            continue
        if feasible_only is False and row.get("是否可行") == "是":
            continue
        chosen.append(int(row["record_index"]))
    return chosen


def phase_indexes(phase: str) -> list[int]:
    """Frozen round-3 membership. This does not redraw the split."""

    blind = set(_indexes(ROUND3_BLIND, feasible_only=None))
    split = json.loads(SPLIT_JSON.read_text(encoding="utf-8"))
    if set(int(index) for index in split["blind_record_indexes"]) != blind:
        raise SystemExit("round-3 blind CSV does not match the frozen split")
    if phase == "solved-nonblind":
        chosen = _indexes(ROUND3_REGRESSION, feasible_only=True)
        chosen.extend(_indexes(ROUND3_DEV, feasible_only=True))
    elif phase == "dev-unsolved":
        chosen = _indexes(ROUND3_DEV, feasible_only=False)
    elif phase == "blind":
        chosen = sorted(blind)
    else:
        raise SystemExit(f"unknown phase {phase}")
    if len(chosen) != len(set(chosen)):
        raise SystemExit(f"{phase} has duplicate indexes")
    if set(chosen) & blind and phase != "blind":
        raise SystemExit(f"{phase} overlaps the blind split")
    expected = {"solved-nonblind": 1443, "dev-unsolved": 99, "blind": 49}[phase]
    if len(chosen) != expected:
        raise SystemExit(f"{phase} has {len(chosen)} cases, expected {expected}")
    return chosen


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


def _ensure_records() -> list[dict]:
    global _RECORDS
    if _RECORDS is None:
        _RECORDS = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))["RECORDS"]
    return _RECORDS


def solve_index(
    index: int,
    time_limit: float,
    cpsat_time_limit: float,
    ondemand_time_limit: float,
    label: str,
) -> dict[str, str]:
    from app.solver import solve_payload
    from app.verifier import verify_solution

    records = _ensure_records()
    record = records[index]
    raw = record["MOMPROBLEMJSON"]
    problem = json.loads(raw) if isinstance(raw, str) else raw
    started = time.monotonic()

    def _hard_stop(_signum, _frame):
        raise TimeoutError("全量进程到达硬时限")

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
        failure = f"{exc}，结果未知"
    except Exception as exc:  # noqa: BLE001
        status = "INPUT_ERROR"
        failure_class = "INPUT_ERROR"
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return {
        "case_id": str(record.get("ID") or index),
        "record_index": str(index),
        "集合": label,
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


def _error_row(index: int, label: str, message: str) -> dict[str, str]:
    records = _ensure_records()
    record = records[index] if index < len(records) else {}
    return {
        "case_id": str(record.get("ID") or index),
        "record_index": str(index),
        "集合": label,
        "是否可行": "否",
        "用料": "",
        "焊口": "",
        "拼法种类": "",
        "切法种类": "",
        "耗时": "",
        "策略来源": "",
        "失败类": "INPUT_ERROR",
        "失败说明": message[:500],
        "利用率": "",
        "校验通过": "否",
        "状态": "INPUT_ERROR",
        "兜底耗时": "",
        "按需耗时": "",
        "按需说明": "",
        "档位记录": "",
        "分类": "缺口说明",
    }


def run_phase(args: argparse.Namespace) -> int:
    if args.phase == "blind" and not args.confirm_blind:
        print("refusing to run the blind split without --confirm-blind", flush=True)
        return 2
    indexes = phase_indexes(args.phase)
    if args.limit_cases:
        indexes = indexes[: args.limit_cases]
    _ensure_records()
    records = _RECORDS or []
    path: Path = args.csv or PHASE_CSV[args.phase]
    path.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(path)
    pending = []
    for index in indexes:
        case_id = str(records[index].get("ID") or index)
        if case_id not in done:
            pending.append(index)
    if args.phase == "blind" and not pending and len(done) >= 49 and not args.limit_cases:
        print("blind CSV already has every case; not solving them again", flush=True)
        return 0
    label = PHASE_LABEL[args.phase]
    print(
        f"phase {args.phase} corpus {len(indexes)} done {len(done)} pending {len(pending)} "
        f"chain {args.time_limit}s cpsat {args.cpsat_time_limit}s "
        f"ondemand {args.ondemand_time_limit}s",
        flush=True,
    )
    new_file = not path.exists()
    handle = path.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        handle.flush()
    finished = 0
    label_every = 25 if args.phase == "solved-nonblind" else 1
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                solve_index,
                index,
                args.time_limit,
                args.cpsat_time_limit,
                args.ondemand_time_limit,
                label,
            ): index
            for index in pending
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001
                row = _error_row(index, label, f"{type(exc).__name__}: {exc}")
            writer.writerow(row)
            handle.flush()
            finished += 1
            if finished % label_every == 0 or finished == len(pending):
                print(
                    f"wrote {finished}/{len(pending)} {row['record_index']} "
                    f"{row['分类']} {row['是否可行']} {row['耗时']} {row['策略来源']}",
                    flush=True,
                )
    handle.close()
    _print_summary(path)
    return 0


def _print_summary(path: Path) -> None:
    rows = _load_rows(path)
    total = len(rows)
    ok = sum(1 for row in rows if row["是否可行"] == "是")
    classes = Counter(row["分类"] for row in rows if row["是否可行"] != "是")
    strategies = Counter(row["策略来源"] for row in rows if row["是否可行"] == "是")
    rate = ok / total if total else 0.0
    print(f"summary {ok}/{total} = {rate:.3%} unsolved {dict(classes)}", flush=True)
    print(f"strategies {dict(strategies)}", flush=True)


def _num(value: str) -> int:
    return int(float(value))


def _baseline_map() -> dict[int, dict[str, str]]:
    found: dict[int, dict[str, str]] = {}
    for path in (ROUND3_REGRESSION, ROUND3_DEV, ROUND3_BLIND):
        for row in _load_rows(path):
            if row.get("是否可行") == "是":
                found[int(row["record_index"])] = row
    return found


def write_compare() -> int:
    fresh: dict[int, dict[str, str]] = {}
    for path in PHASE_CSV.values():
        if not path.exists():
            continue
        for row in _load_rows(path):
            fresh[int(row["record_index"])] = row
    baseline = _baseline_map()
    missing = [index for index in sorted(baseline) if index not in fresh]
    rows: list[dict[str, str]] = []
    worse = 0
    for index in sorted(baseline):
        old = baseline[index]
        new = fresh.get(index)
        if new is None:
            continue
        deltas: dict[str, int] = {}
        comparable = new["是否可行"] == "是" and new["校验通过"] == "是"
        for old_key, new_key, name in (
            ("用料", "用料", "用料"),
            ("焊口", "焊口", "焊口"),
            ("拼法种类", "拼法种类", "拼法"),
            ("切法种类", "切法种类", "切法"),
        ):
            if comparable:
                deltas[name] = _num(new[new_key]) - _num(old[old_key])
            else:
                deltas[name] = 0
        if not comparable:
            quality = "失去可行"
        elif any(value > 0 for value in deltas.values()):
            quality = "变差"
        elif any(value < 0 for value in deltas.values()):
            quality = "改善"
        else:
            quality = "不变"
        if quality in {"失去可行", "变差"}:
            worse += 1
        rows.append(
            {
                "case_id": new["case_id"],
                "record_index": str(index),
                "集合": new["集合"],
                "旧是否可行": "是",
                "新是否可行": new["是否可行"],
                "校验通过": new["校验通过"],
                "旧用料": old["用料"],
                "新用料": new["用料"],
                "用料变化": deltas["用料"],
                "旧焊口": old["焊口"],
                "新焊口": new["焊口"],
                "焊口变化": deltas["焊口"],
                "旧拼法种类": old["拼法种类"],
                "新拼法种类": new["拼法种类"],
                "拼法变化": deltas["拼法"],
                "旧切法种类": old["切法种类"],
                "新切法种类": new["切法种类"],
                "切法变化": deltas["切法"],
                "旧策略": old["策略来源"],
                "新策略": new["策略来源"],
                "质量变化": quality,
                "退化": "是" if quality in {"失去可行", "变差"} else "否",
            }
        )
    COMPARE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with COMPARE_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMPARE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"compare rows {len(rows)} baseline {len(baseline)} missing {len(missing)} worse {worse}",
        flush=True,
    )
    if missing:
        print("missing baseline indexes " + ",".join(str(index) for index in missing[:30]), flush=True)
    return 0


def write_full() -> int:
    rows: list[dict[str, str]] = []
    seen: set[int] = set()
    for phase in ("solved-nonblind", "dev-unsolved", "blind"):
        path = PHASE_CSV[phase]
        if not path.exists():
            print(f"missing {path}", flush=True)
            return 2
        for row in _load_rows(path):
            index = int(row["record_index"])
            if index in seen:
                raise SystemExit(f"duplicate record {index}")
            seen.add(index)
            rows.append(row)
    if len(rows) != 1591:
        print(f"full CSV has {len(rows)} rows, expected 1591", flush=True)
        return 2
    rows.sort(key=lambda row: int(row["record_index"]))
    with FULL_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {FULL_CSV} rows {len(rows)}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=["solved-nonblind", "dev-unsolved", "blind"],
        help="Which frozen slice to solve.",
    )
    parser.add_argument("--time-limit", type=float, default=15.0)
    parser.add_argument("--cpsat-time-limit", type=float, default=15.0)
    parser.add_argument("--ondemand-time-limit", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--limit-cases", type=int, default=0)
    parser.add_argument("--confirm-blind", action="store_true")
    parser.add_argument("--compare-only", action="store_true")
    parser.add_argument("--merge-only", action="store_true")
    args = parser.parse_args()
    if args.compare_only:
        return write_compare()
    if args.merge_only:
        return write_full()
    if not args.phase:
        print("pass --phase or --compare-only or --merge-only", flush=True)
        return 2
    return run_phase(args)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "backend"))
    raise SystemExit(main())
