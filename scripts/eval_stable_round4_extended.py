"""Re-run only the 88 cases the 60-second full run left unsolved.

The on-demand stage gets 300 seconds. The strategy chain and the catalogue
stage stay at 15 seconds. Cases the full CSV already marks feasible are
refused. The limit is the same for every case; this script does not retune
alphabet caps, column caps, or anything else, and it does not branch on the
blind split.
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_stable_round4_full as full  # noqa: E402

OUT_CSV = ROOT / "docs/stable-round4-extended.csv"
CHAIN_SECONDS = 15.0
CPSAT_SECONDS = 15.0
ONDEMAND_SECONDS = 300.0
EXPECTED = 88
EXPECTED_BY_LABEL = {"开发未解": 53, "盲测": 35}
PREVIOUS_SOLVED = 1503
DEV_PREVIOUS = 60
BLIND_PREVIOUS = 14
DEV_SIZE = 113
BLIND_SIZE = 49
CORPUS = 1591
CHECKPOINTS = (60.0, 120.0, 180.0, 300.0)
EXTRA_COLUMNS = ["原分类", "原失败类", "原按需耗时", "原档位记录"]
COLUMNS = [*full.COLUMNS, *EXTRA_COLUMNS]


def _unsolved_rows() -> list[dict[str, str]]:
    rows = full._load_rows(full.FULL_CSV)
    if len(rows) != CORPUS:
        raise SystemExit(f"full CSV has {len(rows)} rows, expected {CORPUS}")
    feasible = {int(row["record_index"]) for row in rows if row.get("是否可行") == "是"}
    chosen = [row for row in rows if row.get("是否可行") != "是"]
    if len(chosen) != EXPECTED:
        raise SystemExit(f"unsolved count is {len(chosen)}, expected {EXPECTED}")
    labels = Counter(row["集合"] for row in chosen)
    if dict(labels) != EXPECTED_BY_LABEL:
        raise SystemExit(f"unsolved labels are {dict(labels)}, expected {EXPECTED_BY_LABEL}")
    indexes = [int(row["record_index"]) for row in chosen]
    if len(indexes) != len(set(indexes)):
        raise SystemExit("unsolved indexes are not unique")
    if set(indexes) & feasible:
        raise SystemExit("an unsolved row collides with a feasible index")
    return chosen


def _done(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    found: dict[str, dict[str, str]] = {}
    for row in full._load_rows(path):
        case_id = row.get("case_id") or ""
        if case_id:
            found[case_id] = row
    return found


def _attach(row: dict[str, str], source: dict[str, str]) -> dict[str, str]:
    attached = dict(row)
    attached["原分类"] = source.get("分类", "")
    attached["原失败类"] = source.get("失败类", "")
    attached["原按需耗时"] = source.get("按需耗时", "")
    attached["原档位记录"] = source.get("档位记录", "")
    return attached


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


def _float(value: str) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt(value: float) -> str:
    if value != value:
        return ""
    return f"{value:.3f}"


def summarize(path: Path) -> int:
    rows = list(_done(path).values())
    print(f"rows {len(rows)} expected {EXPECTED}", flush=True)
    solved = [row for row in rows if row.get("是否可行") == "是"]
    failed = [row for row in rows if row.get("是否可行") != "是"]
    by_label = Counter(row["集合"] for row in solved)
    print(
        f"new solves {len(solved)} dev {by_label.get('开发未解', 0)} "
        f"blind {by_label.get('盲测', 0)}",
        flush=True,
    )
    total = PREVIOUS_SOLVED + len(solved)
    dev_total = DEV_PREVIOUS + by_label.get("开发未解", 0)
    blind_total = BLIND_PREVIOUS + by_label.get("盲测", 0)
    print(
        f"coverage {total}/{CORPUS} = {total / CORPUS:.4%} "
        f"dev {dev_total}/{DEV_SIZE} = {dev_total / DEV_SIZE:.4%} "
        f"blind {blind_total}/{BLIND_SIZE} = {blind_total / BLIND_SIZE:.4%}",
        flush=True,
    )
    print(f"new strategies {dict(Counter(row['策略来源'] for row in solved))}", flush=True)
    print(f"verified {sum(1 for row in solved if row.get('校验通过') == '是')}", flush=True)
    print(
        "transition "
        + str(
            dict(
                Counter((row.get("原分类", ""), row.get("分类", "")) for row in rows)
            )
        ),
        flush=True,
    )
    print(
        "unsolved "
        + str(dict(Counter(row.get("分类", "") for row in failed))),
        flush=True,
    )
    for label in ("开发未解", "盲测"):
        subset = [row for row in failed if row["集合"] == label]
        print(
            f"unsolved {label} "
            + str(dict(Counter(row.get("分类", "") for row in subset))),
            flush=True,
        )
    times = [value for row in solved if (value := _float(row.get("按需耗时", ""))) is not None]
    walls = [value for row in solved if (value := _float(row.get("耗时", ""))) is not None]
    print(
        "solve ondemand median/p90/max "
        f"{_fmt(_percentile(times, 0.5))} {_fmt(_percentile(times, 0.9))} "
        f"{_fmt(max(times) if times else float('nan'))} n={len(times)}",
        flush=True,
    )
    print(
        "solve wall median/p90/max "
        f"{_fmt(_percentile(walls, 0.5))} {_fmt(_percentile(walls, 0.9))} "
        f"{_fmt(max(walls) if walls else float('nan'))} n={len(walls)}",
        flush=True,
    )
    for limit in CHECKPOINTS:
        count = sum(1 for value in times if value <= limit)
        print(f"cumulative ondemand<={limit:.0f}s {count}", flush=True)
    late = [value for value in times if value > 300.0]
    print(f"ondemand over 300s {len(late)} max {_fmt(max(late) if late else float('nan'))}", flush=True)
    failed_times = [
        value for row in failed if (value := _float(row.get("按需耗时", ""))) is not None
    ]
    print(
        "unsolved ondemand median/p90/max "
        f"{_fmt(_percentile(failed_times, 0.5))} {_fmt(_percentile(failed_times, 0.9))} "
        f"{_fmt(max(failed_times) if failed_times else float('nan'))} n={len(failed_times)}",
        flush=True,
    )
    for label in ("开发未解", "盲测"):
        subset_times = [
            value
            for row in solved
            if row["集合"] == label and (value := _float(row.get("按需耗时", ""))) is not None
        ]
        print(
            f"solve ondemand {label} median/p90/max "
            f"{_fmt(_percentile(subset_times, 0.5))} {_fmt(_percentile(subset_times, 0.9))} "
            f"{_fmt(max(subset_times) if subset_times else float('nan'))} n={len(subset_times)}",
            flush=True,
        )
        for limit in CHECKPOINTS:
            count = sum(1 for value in subset_times if value <= limit)
            print(f"cumulative {label} ondemand<={limit:.0f}s {count}", flush=True)
    indexes = ",".join(
        str(index)
        for index in sorted(int(row["record_index"]) for row in solved)
    )
    print(f"solved indexes {indexes}", flush=True)
    return 0 if len(rows) == EXPECTED else 1


def run(workers: int) -> int:
    selected = _unsolved_rows()
    by_id = {row["case_id"]: row for row in selected}
    full._ensure_records()
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    finished = _done(OUT_CSV)
    unknown = [case_id for case_id in finished if case_id not in by_id]
    if unknown:
        raise SystemExit(f"extended CSV contains cases outside the 88: {unknown[:5]}")
    pending = [row for row in selected if row["case_id"] not in finished]
    print(
        f"extended corpus {len(selected)} done {len(finished)} pending {len(pending)} "
        f"chain {CHAIN_SECONDS:.0f}s cpsat {CPSAT_SECONDS:.0f}s "
        f"ondemand {ONDEMAND_SECONDS:.0f}s workers {workers}",
        flush=True,
    )
    new_file = not OUT_CSV.exists()
    handle = OUT_CSV.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        handle.flush()
    if not pending:
        handle.close()
        return summarize(OUT_CSV)
    # spawn so each case can exit after one task and release the column-generation
    # model. fork cannot combine with max_tasks_per_child.
    context = multiprocessing.get_context("spawn")
    wrote = 0
    with ProcessPoolExecutor(max_workers=max(1, workers), mp_context=context, max_tasks_per_child=1) as pool:
        futures = {
            pool.submit(
                full.solve_index,
                int(row["record_index"]),
                CHAIN_SECONDS,
                CPSAT_SECONDS,
                ONDEMAND_SECONDS,
                row["集合"],
            ): row
            for row in pending
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                print(
                    f"worker failed {source['record_index']} {name}: {exc}; "
                    "this case was not recorded and will be retried on resume",
                    flush=True,
                )
                handle.close()
                return 1
            if row["case_id"] != source["case_id"]:
                handle.close()
                raise SystemExit(
                    f"case id mismatch index {source['record_index']}: "
                    f"{row['case_id']} != {source['case_id']}"
                )
            writer.writerow(_attach(row, source))
            handle.flush()
            wrote += 1
            print(
                f"wrote {wrote}/{len(pending)} {row['record_index']} {row['集合']} "
                f"{row['分类']} {row['是否可行']} wall {row['耗时']} "
                f"ondemand {row['按需耗时']} {row['策略来源']}",
                flush=True,
            )
    handle.close()
    return summarize(OUT_CSV)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        selected = _unsolved_rows()
        indexes = [int(row["record_index"]) for row in selected]
        print(f"check ok {len(selected)} indexes {min(indexes)}..{max(indexes)}", flush=True)
        return 0
    if args.summarize:
        return summarize(OUT_CSV)
    return run(args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
