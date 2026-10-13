"""Compare a new stable run with the frozen round-2 feasible rows.

Exits with status 1 when any previously feasible record is missing, infeasible,
or rejected by the verifier. Quality deltas are printed for records that stay
feasible with a different material, joint, or pattern-type count.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUND2 = ROOT / "docs/stable-feasibility-results.csv"
QUALITY = ("用料", "焊口", "拼法种类", "切法种类")


def _load(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["record_index"]: row for row in csv.DictReader(handle)}


def main() -> int:
    new_path = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs/stable-round3-regression.csv")
    previous = _load(ROUND2)
    current = _load(new_path)
    base = [row for row in previous.values() if row["是否可行"] == "是"]
    missing = []
    infeasible = []
    unverified = []
    changed = []
    for row in base:
        index = row["record_index"]
        now = current.get(index)
        if now is None:
            missing.append(index)
            continue
        if now["是否可行"] != "是":
            infeasible.append((index, now.get("失败类", ""), now.get("策略来源", "")))
            continue
        if now.get("校验通过") != "是":
            unverified.append(index)
            continue
        deltas = {
            column: (row[column], now[column])
            for column in QUALITY
            if str(row[column]) != str(now[column])
        }
        if deltas or row.get("策略来源") != now.get("策略来源"):
            changed.append((index, row.get("策略来源"), now.get("策略来源"), deltas))
    print(f"previous feasible {len(base)}")
    print(f"missing {len(missing)} infeasible {len(infeasible)} unverified {len(unverified)} changed {len(changed)}")
    for index, failure, strategy in infeasible:
        print(f"INFEASIBLE {index} {failure} {strategy}")
    for index in unverified:
        print(f"UNVERIFIED {index}")
    for index, before, after, deltas in changed:
        print(f"CHANGED {index} strategy {before} -> {after} {deltas}")
    if missing or infeasible or unverified:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
