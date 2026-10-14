"""Compare instance features of the frozen development and blind splits.

This script only reads orders and the already published CSVs. It does not
solve, and it does not change any solver parameter. Slack is raw stock length
minus raw demand length; kerf is not subtracted.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain import parse_problem  # noqa: E402
from app.engine_stable import prepare_stable_payload  # noqa: E402

RECORDS_PATH = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
SPLIT_JSON = ROOT / "docs/stable-round3-split.json"
PILOT_JSON = ROOT / "docs/stable-round4-pilot-split.json"
ROUND3_DEV = ROOT / "docs/stable-round3-dev.csv"
ROUND3_BLIND = ROOT / "docs/stable-round3-blind.csv"
FULL_CSV = ROOT / "docs/stable-round4-full.csv"
OUT_CSV = ROOT / "docs/stable-round4-extended-features.csv"

COLUMNS = [
    "record_index",
    "case_id",
    "集合",
    "试算样本",
    "第三轮解出",
    "第四轮60秒解出",
    "第四轮60秒分类",
    "第四轮60秒策略",
    "材料组数",
    "管型数",
    "需求根数",
    "管长种类",
    "单组最多管型",
    "单组最多管长种类",
    "定尺种类数",
    "定尺长度种类",
    "定尺根数",
    "最长管",
    "最长定尺",
    "超定尺管型数",
    "超定尺管型比例",
    "超定尺根数",
    "超定尺根数比例",
    "超定尺长度",
    "超定尺长度比例",
    "需求长度",
    "供料长度",
    "松弛量",
    "松弛比例",
    "最紧组松弛比例",
    "锯缝",
    "最大焊口上限",
    "禁焊区间数",
    "最小焊距",
    "最小下料",
]


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _feasible_indexes(path: Path) -> set[int]:
    return {int(row["record_index"]) for row in _load_rows(path) if row.get("是否可行") == "是"}


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


def _case_features(problem_payload: dict) -> dict[str, float]:
    prepared = prepare_stable_payload(problem_payload)
    problem = parse_problem(prepared)
    pipe_types = 0
    copies = 0
    demand_length = 0
    stock_length = 0
    stock_types = 0
    stock_bars = 0
    over_types = 0
    over_copies = 0
    over_length = 0
    forbidden = 0
    max_pipe = 0
    max_stock = 0
    max_joints = 0
    kerf = 0
    min_weld = 0
    min_cut = 0
    pipe_lengths: set[int] = set()
    stock_lengths: set[int] = set()
    max_group_types = 0
    max_group_lengths = 0
    min_slack_ratio = float("inf")
    for group in problem.groups:
        group_max_stock = max((stock.length for stock in group.stocks), default=0)
        group_types = len(group.pipes)
        group_lengths = {pipe.length for pipe in group.pipes}
        max_group_types = max(max_group_types, group_types)
        max_group_lengths = max(max_group_lengths, len(group_lengths))
        pipe_types += group_types
        stock_types += len(group.stocks)
        pipe_lengths.update(group_lengths)
        stock_lengths.update(stock.length for stock in group.stocks)
        demand_length += group.demand_length
        stock_length += group.stock_length
        stock_bars += sum(stock.quantity for stock in group.stocks)
        max_stock = max(max_stock, group_max_stock)
        kerf = max(kerf, group.blade_margin)
        min_weld = max(min_weld, group.min_weld_distance)
        min_cut = max(min_cut, group.min_cut_length)
        group_demand = group.demand_length
        if group_demand > 0:
            min_slack_ratio = min(
                min_slack_ratio,
                (group.stock_length - group_demand) / group_demand,
            )
        for pipe in group.pipes:
            copies += pipe.demand
            max_pipe = max(max_pipe, pipe.length)
            max_joints = max(max_joints, pipe.max_joints)
            forbidden += len(pipe.forbidden)
            if pipe.length > group_max_stock:
                over_types += 1
                over_copies += pipe.demand
                over_length += pipe.length * pipe.demand
    slack = stock_length - demand_length
    return {
        "材料组数": float(len(problem.groups)),
        "管型数": float(pipe_types),
        "需求根数": float(copies),
        "管长种类": float(len(pipe_lengths)),
        "单组最多管型": float(max_group_types),
        "单组最多管长种类": float(max_group_lengths),
        "定尺种类数": float(stock_types),
        "定尺长度种类": float(len(stock_lengths)),
        "定尺根数": float(stock_bars),
        "最长管": float(max_pipe),
        "最长定尺": float(max_stock),
        "超定尺管型数": float(over_types),
        "超定尺管型比例": over_types / pipe_types if pipe_types else 0.0,
        "超定尺根数": float(over_copies),
        "超定尺根数比例": over_copies / copies if copies else 0.0,
        "超定尺长度": float(over_length),
        "超定尺长度比例": over_length / demand_length if demand_length else 0.0,
        "需求长度": float(demand_length),
        "供料长度": float(stock_length),
        "松弛量": float(slack),
        "松弛比例": slack / demand_length if demand_length else 0.0,
        "最紧组松弛比例": 0.0 if min_slack_ratio == float("inf") else min_slack_ratio,
        "锯缝": float(kerf),
        "最大焊口上限": float(max_joints),
        "禁焊区间数": float(forbidden),
        "最小焊距": float(min_weld),
        "最小下料": float(min_cut),
    }


def _median_line(name: str, rows: list[dict[str, str]], fields: list[str]) -> None:
    print(f"## {name} n={len(rows)}", flush=True)
    for field in fields:
        values = [float(row[field]) for row in rows]
        print(
            f"{field} median {_percentile(values, 0.5):.4f} "
            f"p90 {_percentile(values, 0.9):.4f} "
            f"max {max(values):.4f} mean {sum(values) / len(values):.4f}",
            flush=True,
        )


def main() -> int:
    split = json.loads(SPLIT_JSON.read_text(encoding="utf-8"))
    pilot = set(int(index) for index in json.loads(PILOT_JSON.read_text(encoding="utf-8"))["record_indexes"])
    dev = [int(index) for index in split["dev_record_indexes"]]
    blind = [int(index) for index in split["blind_record_indexes"]]
    if len(dev) != 113 or len(blind) != 49 or set(dev) & set(blind):
        raise SystemExit("frozen split is not the expected 113/49 partition")
    round3 = _feasible_indexes(ROUND3_DEV) | _feasible_indexes(ROUND3_BLIND)
    full_rows = {int(row["record_index"]): row for row in _load_rows(FULL_CSV)}
    records = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))["RECORDS"]
    membership = {index: "开发" for index in dev}
    membership.update({index: "盲测" for index in blind})
    written: list[dict[str, str]] = []
    for index in sorted(membership):
        record = records[index]
        raw = record["MOMPROBLEMJSON"]
        payload = json.loads(raw) if isinstance(raw, str) else raw
        features = _case_features(payload)
        published = full_rows[index]
        row = {
            "record_index": str(index),
            "case_id": str(record.get("ID") or index),
            "集合": membership[index],
            "试算样本": "是" if index in pilot else "否",
            "第三轮解出": "是" if index in round3 else "否",
            "第四轮60秒解出": published["是否可行"],
            "第四轮60秒分类": published["分类"],
            "第四轮60秒策略": published["策略来源"],
        }
        for key, value in features.items():
            row[key] = f"{value:.6f}" if isinstance(value, float) else str(value)
        written.append(row)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(written)
    print(f"wrote {OUT_CSV} rows {len(written)}", flush=True)
    fields = [
        "管型数",
        "需求根数",
        "管长种类",
        "单组最多管长种类",
        "定尺根数",
        "定尺长度种类",
        "最长管",
        "最长定尺",
        "超定尺管型比例",
        "超定尺长度比例",
        "需求长度",
        "松弛量",
        "松弛比例",
        "最紧组松弛比例",
        "锯缝",
        "最大焊口上限",
        "禁焊区间数",
        "材料组数",
    ]
    groups = {
        "开发全集": [row for row in written if row["集合"] == "开发"],
        "盲测全集": [row for row in written if row["集合"] == "盲测"],
        "开发进入按需": [row for row in written if row["集合"] == "开发" and row["第三轮解出"] != "是"],
        "盲测进入按需": [row for row in written if row["集合"] == "盲测" and row["第三轮解出"] != "是"],
        "开发60秒仍未解": [
            row for row in written if row["集合"] == "开发" and row["第四轮60秒解出"] != "是"
        ],
        "盲测60秒仍未解": [
            row for row in written if row["集合"] == "盲测" and row["第四轮60秒解出"] != "是"
        ],
        "开发按需解出": [
            row
            for row in written
            if row["集合"] == "开发" and row["第三轮解出"] != "是" and row["第四轮60秒解出"] == "是"
        ],
        "盲测按需解出": [
            row
            for row in written
            if row["集合"] == "盲测" and row["第三轮解出"] != "是" and row["第四轮60秒解出"] == "是"
        ],
        "试算20": [row for row in written if row["试算样本"] == "是"],
        "开发未解中非试算": [
            row
            for row in written
            if row["集合"] == "开发" and row["第三轮解出"] != "是" and row["试算样本"] != "是"
        ],
    }
    for name, subset in groups.items():
        _median_line(name, subset, fields)
        solved = sum(1 for row in subset if row["第四轮60秒解出"] == "是")
        print(f"{name} round4 solved {solved}/{len(subset)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
