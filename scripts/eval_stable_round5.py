"""Round-5 evaluation for the near-stock on-demand seed.

Phases:

* ``pilot`` — the 21 development cases that were still unsolved after the
  300-second rerun and whose demand length is entirely longer than the
  longest stock. Refuses any blind index.
* ``unsolved`` — all 84 cases still unsolved after that rerun, once.
  Requires ``--confirm-blind``. A completed blind subset is not solved again.
* ``regression`` — the 1507 cases that were already feasible. Chain 15s,
  catalogue 15s, on-demand 60s.

The on-demand limit is 60 seconds in every phase. This script does not
change solver parameters and does not branch on a case id.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_stable_round4_full as full  # noqa: E402

PILOT_JSON = ROOT / "docs/stable-round5-pilot-split.json"
SPLIT_JSON = ROOT / "docs/stable-round3-split.json"
FEATURES = ROOT / "docs/stable-round4-extended-features.csv"
EXTENDED = ROOT / "docs/stable-round4-extended.csv"
PILOT_CSV = ROOT / "docs/stable-round5-pilot.csv"
UNSOLVED_CSV = ROOT / "docs/stable-round5-unsolved.csv"
REGRESSION_CSV = ROOT / "docs/stable-round5-regression.csv"
COMPARE_CSV = ROOT / "docs/stable-round5-regression-compare.csv"
CHAIN_SECONDS = 15.0
CPSAT_SECONDS = 15.0
ONDEMAND_SECONDS = 60.0
COLUMNS = list(full.COLUMNS)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _features() -> dict[int, dict[str, str]]:
    return {int(row["record_index"]): row for row in _rows(FEATURES)}


def _blind() -> set[int]:
    split = json.loads(SPLIT_JSON.read_text(encoding="utf-8"))
    return {int(index) for index in split["blind_record_indexes"]}


def _pilot_indexes() -> list[int]:
    payload = json.loads(PILOT_JSON.read_text(encoding="utf-8"))
    chosen = [int(index) for index in payload["record_indexes"]]
    if len(chosen) != 21 or len(chosen) != len(set(chosen)):
        raise SystemExit("pilot split is not the frozen 21 indexes")
    if set(chosen) & _blind():
        raise SystemExit("pilot split overlaps the blind set")
    return chosen


def _unsolved_rows() -> list[dict[str, str]]:
    chosen = [row for row in _rows(EXTENDED) if row.get("是否可行") != "是"]
    if len(chosen) != 84:
        raise SystemExit(f"expected 84 unsolved rows, found {len(chosen)}")
    return chosen


def _regression_rows() -> list[dict[str, str]]:
    solved = [row for row in _rows(full.FULL_CSV) if row.get("是否可行") == "是"]
    extra = [row for row in _rows(EXTENDED) if row.get("是否可行") == "是"]
    seen = {int(row["record_index"]) for row in solved}
    for row in extra:
        index = int(row["record_index"])
        if index in seen:
            raise SystemExit(f"solved index {index} is in both baselines")
        solved.append(row)
        seen.add(index)
    if len(solved) != 1507:
        raise SystemExit(f"expected 1507 solved rows, found {len(solved)}")
    return solved


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {row["case_id"] for row in _rows(path) if row.get("case_id")}


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


def _summarize(path: Path) -> None:
    rows = _rows(path) if path.exists() else []
    solved = [row for row in rows if row.get("是否可行") == "是"]
    print(
        f"summary {path.name} solved {len(solved)}/{len(rows)} "
        f"classes {dict(Counter(row.get('分类') for row in rows if row.get('是否可行') != '是'))}",
        flush=True,
    )
    times = []
    for row in solved:
        text = (row.get("按需耗时") or "").strip()
        if text:
            times.append(float(text))
    if times:
        print(
            "ondemand median/p90/max "
            f"{_percentile(times, 0.5):.3f} {_percentile(times, 0.9):.3f} {max(times):.3f}",
            flush=True,
        )
    indexes = ",".join(str(index) for index in sorted(int(row["record_index"]) for row in solved))
    print(f"solved indexes {indexes}", flush=True)


def _run(pending: list[tuple[int, str]], path: Path, workers: int) -> int:
    full._ensure_records()
    records = full._RECORDS or []
    done = _done(path)
    queue = []
    for index, label in pending:
        case_id = str(records[index].get("ID") or index)
        if case_id not in done:
            queue.append((index, label, case_id))
    print(
        f"corpus {len(pending)} done {len(done)} pending {len(queue)} "
        f"chain {CHAIN_SECONDS:.0f}s cpsat {CPSAT_SECONDS:.0f}s "
        f"ondemand {ONDEMAND_SECONDS:.0f}s workers {workers}",
        flush=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    handle = path.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        handle.flush()
    if not queue:
        handle.close()
        _summarize(path)
        return 0
    context = multiprocessing.get_context("spawn")
    wrote = 0
    with ProcessPoolExecutor(
        max_workers=max(1, workers), mp_context=context, max_tasks_per_child=1
    ) as pool:
        futures = {
            pool.submit(
                full.solve_index,
                index,
                CHAIN_SECONDS,
                CPSAT_SECONDS,
                ONDEMAND_SECONDS,
                label,
            ): (index, case_id)
            for index, label, case_id in queue
        }
        for future in as_completed(futures):
            index, case_id = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"worker failed {index} {type(exc).__name__}: {exc}; not recorded",
                    flush=True,
                )
                handle.close()
                return 1
            if row["case_id"] != case_id:
                handle.close()
                raise SystemExit(f"case id mismatch at {index}")
            writer.writerow(row)
            handle.flush()
            wrote += 1
            print(
                f"wrote {wrote}/{len(queue)} {row['record_index']} {row['集合']} "
                f"{row['分类']} {row['是否可行']} wall {row['耗时']} "
                f"ondemand {row['按需耗时']} {row['策略来源']}",
                flush=True,
            )
    handle.close()
    _summarize(path)
    return 0


def _num(value: str) -> int:
    return int(float(value))


def write_compare() -> int:
    fresh = {int(row["record_index"]): row for row in _rows(REGRESSION_CSV)}
    baseline = {int(row["record_index"]): row for row in _regression_rows()}
    missing = [index for index in sorted(baseline) if index not in fresh]
    worse = 0
    compared: list[dict[str, str]] = []
    for index in sorted(baseline):
        old = baseline[index]
        new = fresh.get(index)
        if new is None:
            continue
        comparable = new["是否可行"] == "是" and new["校验通过"] == "是"
        deltas: dict[str, int] = {}
        for name in ("用料", "焊口", "拼法种类", "切法种类"):
            deltas[name] = _num(new[name]) - _num(old[name]) if comparable else 0
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
        compared.append(
            {
                "case_id": new["case_id"],
                "record_index": str(index),
                "集合": new["集合"],
                "旧是否可行": "是",
                "新是否可行": new["是否可行"],
                "校验通过": new["校验通过"],
                "旧用料": old["用料"],
                "新用料": new["用料"],
                "用料变化": str(deltas["用料"]),
                "旧焊口": old["焊口"],
                "新焊口": new["焊口"],
                "焊口变化": str(deltas["焊口"]),
                "旧拼法种类": old["拼法种类"],
                "新拼法种类": new["拼法种类"],
                "拼法变化": str(deltas["拼法种类"]),
                "旧切法种类": old["切法种类"],
                "新切法种类": new["切法种类"],
                "切法变化": str(deltas["切法种类"]),
                "旧策略": old["策略来源"],
                "新策略": new["策略来源"],
                "质量变化": quality,
                "退化": "是" if quality in {"失去可行", "变差"} else "否",
            }
        )
    fields = list(compared[0]) if compared else ["record_index"]
    with COMPARE_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(compared)
    print(
        f"compare rows {len(compared)} missing {len(missing)} worse {worse}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["pilot", "unsolved", "regression"])
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--confirm-blind", action="store_true")
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args()
    if args.compare_only:
        return write_compare()
    if args.phase == "pilot":
        indexes = _pilot_indexes()
        return _run([(index, "开发未解") for index in indexes], PILOT_CSV, args.workers)
    if args.phase == "unsolved":
        if not args.confirm_blind:
            print("refusing the 84-case run without --confirm-blind", flush=True)
            return 2
        if UNSOLVED_CSV.exists():
            have = {int(row["record_index"]) for row in _rows(UNSOLVED_CSV)}
            blind_pending = [row for row in _unsolved_rows() if int(row["record_index"]) in _blind()]
            if blind_pending and all(int(row["record_index"]) in have for row in blind_pending):
                print("blind unsolved rows are already recorded; not solving them again", flush=True)
                return 2
        pending = [
            (int(row["record_index"]), row["集合"]) for row in _unsolved_rows()
        ]
        return _run(pending, UNSOLVED_CSV, args.workers)
    if args.phase == "regression":
        blind = _blind()
        pending = [
            (int(row["record_index"]), row["集合"])
            for row in _regression_rows()
            if args.confirm_blind or int(row["record_index"]) not in blind
        ]
        if not args.confirm_blind:
            print("regression is skipping blind solved cases", flush=True)
        elif REGRESSION_CSV.exists():
            have = {int(row["record_index"]) for row in _rows(REGRESSION_CSV)}
            blind_rows = [index for index, _label in pending if index in blind]
            if blind_rows and all(index in have for index in blind_rows):
                print("blind regression rows are already recorded; not solving them again", flush=True)
                return 2
        return _run(pending, REGRESSION_CSV, args.workers)
    print("pass --phase or --compare-only", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
