"""Pilot the lexicographic post-pass on the frozen 50-case list.

Stable limits stay 15s + 15s + 60s. The post-pass then gets 20s per layer.
This script refuses blind indexes and does not accept a case list other than
docs/opt-pilot-split.json. It does not retune budgets.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
RECORDS_PATH = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
SPLIT_PATH = ROOT / "docs/opt-pilot-split.json"
BLIND_PATH = ROOT / "docs/stable-round3-split.json"
FULL_CSV = ROOT / "docs/stable-round5-full.csv"
LEGACY_CSV = ROOT / "docs/stable-round5-vs-legacy.csv"
OUT = ROOT / "docs/opt-pilot.csv"
CHAIN = 15.0
CPSAT = 15.0
ONDEMAND = 60.0
LAYER = (20.0, 20.0, 20.0)
ALARM = CHAIN + CPSAT + ONDEMAND + sum(LAYER) + 20.0
WORKERS = 3
COLUMNS = [
    "case_id",
    "record_index",
    "集合",
    "起点可行",
    "起点校验",
    "四项一致",
    "保留",
    "原因",
    "问题码",
    "起点用料",
    "起点焊口",
    "起点拼法",
    "起点切法",
    "最终用料",
    "最终焊口",
    "最终拼法",
    "最终切法",
    "用料差",
    "焊口差",
    "拼法差",
    "切法差",
    "层1键",
    "层1用时",
    "层1改进",
    "层2键",
    "层2用时",
    "层2改进",
    "层3拼法键",
    "层3拼法用时",
    "层3拼法改进",
    "层3切法键",
    "层3切法用时",
    "层3切法改进",
    "定价用时",
    "优化用时",
    "起点耗时",
    "总耗时",
    "起点词典序",
    "最终词典序",
    "老软件用料",
    "老软件焊口",
    "老软件拼法",
    "老软件切法",
    "官方用料",
    "官方焊口",
    "官方拼法",
    "官方切法",
    "焊列数",
    "切列数",
    "最终校验",
    "利用率",
]

_RECORDS: list[dict] | None = None


def _init() -> None:
    global _RECORDS
    _RECORDS = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))["RECORDS"]


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _blind() -> set[int]:
    payload = json.loads(BLIND_PATH.read_text(encoding="utf-8"))
    return {int(index) for index in payload["blind_record_indexes"]}


def pilot_indexes() -> list[int]:
    payload = json.loads(SPLIT_PATH.read_text(encoding="utf-8"))
    chosen = [int(index) for index in payload["record_indexes"]]
    if len(chosen) != 50 or len(chosen) != len(set(chosen)) or int(payload["count"]) != 50:
        raise SystemExit("pilot split is not the frozen 50 indexes")
    overlap = set(chosen) & _blind()
    if overlap:
        raise SystemExit(f"pilot split overlaps the blind set: {sorted(overlap)}")
    return chosen


def _num(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _lex(ours: tuple[int, int, int, int], legacy: tuple[int, int, int, int]) -> str:
    if ours < legacy:
        return "胜"
    if ours == legacy:
        return "平"
    return "负"


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _fmt_key(values: list[int] | tuple[int, ...] | None) -> str:
    if not values:
        return ""
    return "|".join(str(int(item)) for item in values)


def _case_layer(logs: list[dict], objective: str) -> tuple[str, str, str]:
    keys: list[int] | None = None
    seconds = 0.0
    improved = False
    seen = False
    for log in logs:
        for layer in log.get("layers") or []:
            if layer.get("objective") != objective:
                continue
            seen = True
            seconds += float(layer.get("seconds") or 0.0)
            improved = improved or bool(layer.get("accepted"))
            key = layer.get("key")
            if isinstance(key, list) and len(key) == 4:
                if keys is None:
                    keys = [0, 0, 0, 0]
                for index, value in enumerate(key):
                    keys[index] += int(value)
    if not seen:
        return "", "", ""
    return _fmt_key(keys), f"{seconds:.3f}", "是" if improved else "否"


def solve_case(task: dict[str, str]) -> dict[str, str]:
    from app.engine_stable_opt import apply_optimization, lex_key
    from app.solver import solve_payload
    from app.verifier import verify_solution

    index = int(task["record_index"])
    records = _RECORDS or []
    record = records[index]
    raw = record["MOMPROBLEMJSON"]
    problem = json.loads(raw) if isinstance(raw, str) else raw
    row = {column: "" for column in COLUMNS}
    row["case_id"] = task["case_id"]
    row["record_index"] = task["record_index"]
    row["集合"] = task.get("集合", "")
    for name, source in (
        ("老软件用料", "老软件用料"),
        ("老软件焊口", "老软件焊口"),
        ("老软件拼法", "老软件拼法"),
        ("老软件切法", "老软件切法"),
        ("官方用料", "官方用料"),
        ("官方焊口", "官方焊口"),
        ("官方拼法", "官方拼法"),
        ("官方切法", "官方切法"),
    ):
        row[name] = task.get(source, "")
    started = time.monotonic()

    def _hard_stop(_signum, _frame):
        raise TimeoutError("优化试算到达硬时限")

    signal.signal(signal.SIGALRM, _hard_stop)
    signal.setitimer(signal.ITIMER_REAL, ALARM)
    try:
        stable_started = time.monotonic()
        result = solve_payload(
            problem,
            time_limit_seconds=CHAIN,
            engine="stable",
            cpsat_time_limit=CPSAT,
            ondemand_time_limit=ONDEMAND,
        )
        stable_seconds = time.monotonic() - stable_started
        prepared = result.pop("_stable_prepared_payload")
        report = verify_solution(prepared, result)
        row["起点耗时"] = f"{stable_seconds:.3f}"
        row["起点校验"] = "是" if report.get("passed") is True else "否"
        row["起点可行"] = "是" if result.get("status") == "FEASIBLE" and report.get("passed") is True else "否"
        if row["起点可行"] != "是":
            row["保留"] = "stable"
            row["原因"] = "起点未通过"
            row["总耗时"] = f"{time.monotonic() - started:.3f}"
            return row
        summary = result["summary"]
        baseline = lex_key(summary)
        row["起点用料"], row["起点焊口"], row["起点拼法"], row["起点切法"] = (str(item) for item in baseline)
        official = tuple(_num(task.get(name)) for name in ("官方用料", "官方焊口", "官方拼法", "官方切法"))
        row["四项一致"] = "是" if official == baseline else "否"
        legacy_values = tuple(_num(task.get(name)) for name in ("老软件用料", "老软件焊口", "老软件拼法", "老软件切法"))
        if all(item is not None for item in legacy_values):
            legacy = tuple(int(item) for item in legacy_values)
            row["起点词典序"] = _lex(baseline, legacy)
        optimized, logs = apply_optimization(prepared, result, LAYER)
        final_report = verify_solution(prepared, optimized)
        final = lex_key(optimized["summary"])
        row["最终校验"] = "是" if final_report.get("passed") is True else "否"
        row["最终用料"], row["最终焊口"], row["最终拼法"], row["最终切法"] = (str(item) for item in final)
        row["用料差"] = str(final[0] - baseline[0])
        row["焊口差"] = str(final[1] - baseline[1])
        row["拼法差"] = str(final[2] - baseline[2])
        row["切法差"] = str(final[3] - baseline[3])
        row["利用率"] = str(optimized["summary"].get("utilization_rate", ""))
        kept = "optimized" if any(log.get("kept") == "optimized" for log in logs) else "stable"
        if final_report.get("passed") is not True or final > baseline:
            kept = "stable"
        row["保留"] = kept
        row["原因"] = "；".join(str(log.get("reason") or "") for log in logs if log.get("reason"))
        codes: list[str] = []
        for log in logs:
            for code in log.get("issue_codes") or []:
                if code not in codes:
                    codes.append(code)
        row["问题码"] = ";".join(codes)
        for column, objective in (
            ("层1", "used_stock_length"),
            ("层2", "welding_joint_quantity"),
            ("层3拼法", "welding_pattern_type_quantity"),
            ("层3切法", "cutting_pattern_type_quantity"),
        ):
            key, seconds, improved = _case_layer(logs, objective)
            row[f"{column}键"] = key
            row[f"{column}用时"] = seconds
            row[f"{column}改进"] = improved
        row["定价用时"] = f"{sum(float(log.get('pricing_seconds') or 0.0) for log in logs):.3f}"
        row["优化用时"] = f"{sum(float(log.get('seconds') or 0.0) for log in logs):.3f}"
        row["焊列数"] = str(sum(int(log.get("weld_columns") or 0) for log in logs))
        row["切列数"] = str(sum(int(log.get("cut_columns") or 0) for log in logs))
        if all(item is not None for item in legacy_values):
            legacy = tuple(int(item) for item in legacy_values)
            row["最终词典序"] = _lex(final, legacy)
        if kept != "optimized":
            # The returned object is the stable plan. Keep the reported final key equal to it.
            row["最终用料"], row["最终焊口"], row["最终拼法"], row["最终切法"] = (str(item) for item in baseline)
            row["用料差"] = row["焊口差"] = row["拼法差"] = row["切法差"] = "0"
            if all(item is not None for item in legacy_values):
                row["最终词典序"] = row["起点词典序"]
            row["利用率"] = str(result["summary"].get("utilization_rate", ""))
    except TimeoutError as exc:
        row["保留"] = "stable"
        row["原因"] = str(exc)
        row["起点可行"] = row["起点可行"] or "否"
    except Exception as exc:  # noqa: BLE001
        row["保留"] = "stable"
        row["原因"] = f"{type(exc).__name__}: {exc}"
        row["起点可行"] = row["起点可行"] or "否"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    row["总耗时"] = row["总耗时"] or f"{time.monotonic() - started:.3f}"
    return row


def _tasks() -> list[dict[str, str]]:
    indexes = pilot_indexes()
    full = {int(row["record_index"]): row for row in _rows(FULL_CSV)}
    legacy = {int(row["record_index"]): row for row in _rows(LEGACY_CSV)}
    tasks = []
    for index in indexes:
        official = full.get(index)
        compared = legacy.get(index)
        if official is None or compared is None:
            raise SystemExit(f"record {index} is missing from the round-5 tables")
        if official.get("是否可行") != "是" or official.get("校验通过") != "是":
            raise SystemExit(f"record {index} is not a verified round-5 plan")
        if compared.get("老软件合规") != "是" or compared.get("词典序对照") != "负":
            raise SystemExit(f"record {index} is not a compliant legacy loss")
        tasks.append(
            {
                "record_index": str(index),
                "case_id": official["case_id"],
                "集合": official.get("集合", ""),
                "官方用料": official.get("用料", ""),
                "官方焊口": official.get("焊口", ""),
                "官方拼法": official.get("拼法种类", ""),
                "官方切法": official.get("切法种类", ""),
                "老软件用料": compared.get("老软件用料", ""),
                "老软件焊口": compared.get("老软件焊口", ""),
                "老软件拼法": compared.get("老软件拼法种类", ""),
                "老软件切法": compared.get("老软件切法种类", ""),
            }
        )
    return tasks


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {row["case_id"] for row in _rows(path) if row.get("case_id")}


def _summarize(path: Path) -> None:
    rows = _rows(path) if path.exists() else []
    print(f"summary rows {len(rows)}", flush=True)
    if not rows:
        return

    def tally(column: str) -> dict[str, int]:
        counts = {"胜": 0, "平": 0, "负": 0}
        for row in rows:
            label = row.get(column) or ""
            if label in counts:
                counts[label] += 1
        return counts

    print(f"起点词典序 {tally('起点词典序')}", flush=True)
    print(f"最终词典序 {tally('最终词典序')}", flush=True)
    kept = sum(1 for row in rows if row.get("保留") == "optimized")
    mismatch = sum(1 for row in rows if row.get("四项一致") == "否")
    failed = sum(1 for row in rows if row.get("最终校验") == "否")
    print(f"optimized {kept} csv_mismatch {mismatch} final_verify_fail {failed}", flush=True)
    for label, column in (
        ("layer1", "层1用时"),
        ("layer2", "层2用时"),
        ("layer3_weld", "层3拼法用时"),
        ("layer3_cut", "层3切法用时"),
        ("opt", "优化用时"),
        ("total", "总耗时"),
    ):
        values = [float(row[column]) for row in rows if row.get(column)]
        if not values:
            continue
        print(
            f"{label} median/p90/max "
            f"{_percentile(values, 0.5):.3f} {_percentile(values, 0.9):.3f} {max(values):.3f}",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args()
    tasks = _tasks()
    done = _done(OUT)
    queue = [task for task in tasks if task["case_id"] not in done]
    print(
        f"pilot {len(tasks)} done {len(done)} pending {len(queue)} "
        f"workers {args.workers} alarm {ALARM:.0f}s",
        flush=True,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    new_file = not OUT.exists()
    handle = OUT.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        handle.flush()
    if not queue:
        handle.close()
        _summarize(OUT)
        return 0
    context = multiprocessing.get_context("spawn")
    wrote = 0
    with ProcessPoolExecutor(
        max_workers=max(1, args.workers),
        mp_context=context,
        initializer=_init,
    ) as pool:
        futures = {pool.submit(solve_case, task): task for task in queue}
        for future in as_completed(futures):
            task = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001
                row = {column: "" for column in COLUMNS}
                row["case_id"] = task["case_id"]
                row["record_index"] = task["record_index"]
                row["集合"] = task.get("集合", "")
                row["保留"] = "stable"
                row["原因"] = f"worker {type(exc).__name__}: {exc}"
            if row.get("case_id") != task["case_id"]:
                handle.close()
                raise SystemExit(f"case id mismatch at {task['record_index']}")
            writer.writerow(row)
            handle.flush()
            wrote += 1
            print(
                f"wrote {wrote}/{len(queue)} {row['record_index']} kept {row['保留']} "
                f"delta {row['用料差']},{row['焊口差']},{row['拼法差']},{row['切法差']} "
                f"lex {row['起点词典序']}->{row['最终词典序']} wall {row['总耗时']} {row['原因']}",
                flush=True,
            )
    handle.close()
    _summarize(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
