"""对比工具：把我们引擎的排料结果与现有排料软件的返回并排比较。

用法：
    python scripts/compare_with_software.py <input.json> [--software <software_result.json>] \
        [--ours <ours_result.json>] [--time-limit 60]

行为：
    - 若未提供 --ours，则调用本项目引擎现算一份结果。
    - 若提供 --software，则解析现有软件返回并按材质规格逐组对比关键指标
      （用料长度、焊口数、利用率、切法/拼法种类）。
    - 现有软件返回结构未知时，本工具做「尽力解析」：先尝试与我们相同的
      结构，失败再退回到只展示我们自己的结果，并提示需要补充解析规则。

现有软件返回的字段名可能与我们不同。请在拿到真实返回样例后，
在 _extract_software_groups 中补充对应的字段映射。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _run_our_engine(input_path: Path, time_limit: float | None) -> dict[str, Any]:
    from app.service import solve_and_verify

    payload = _load_json(input_path)
    return solve_and_verify(payload, time_limit_seconds=time_limit)


def _our_group_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in result.get("groups", []):
        metrics = group.get("metrics", {})
        rows.append(
            {
                "key": f"{group.get('material')}|{group.get('specifications')}",
                "status": metrics.get("solve_status"),
                "demand_length": metrics.get("demand_length"),
                "used_stock_length": metrics.get("used_stock_length"),
                "utilization_rate": metrics.get("utilization_rate"),
                "welding_joint_quantity": metrics.get("welding_joint_quantity"),
                "welding_pattern_type_quantity": metrics.get(
                    "welding_pattern_type_quantity"
                ),
                "cutting_pattern_type_quantity": metrics.get(
                    "cutting_pattern_type_quantity"
                ),
            }
        )
    return rows


def _extract_software_groups(software: dict[str, Any]) -> list[dict[str, Any]]:
    """尽力从现有软件返回中提取逐组指标。

    先尝试与我们相同的结构（groups[].metrics.*）。若现有软件采用不同结构，
    请在此补充字段映射。返回空列表表示无法解析。
    """

    groups = software.get("groups")
    if not isinstance(groups, list):
        return []
    rows: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        metrics = group.get("metrics", {}) if isinstance(group.get("metrics"), dict) else group
        rows.append(
            {
                "key": f"{group.get('material')}|{group.get('specifications')}",
                "used_stock_length": metrics.get("used_stock_length"),
                "utilization_rate": metrics.get("utilization_rate"),
                "welding_joint_quantity": metrics.get("welding_joint_quantity"),
                "welding_pattern_type_quantity": metrics.get(
                    "welding_pattern_type_quantity"
                ),
                "cutting_pattern_type_quantity": metrics.get(
                    "cutting_pattern_type_quantity"
                ),
            }
        )
    return rows


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _print_ours(rows: list[dict[str, Any]]) -> None:
    print("\n=== 我们引擎的结果 ===")
    header = ["规格key", "状态", "需求", "用料", "利用率", "焊口", "拼法种类", "切法种类"]
    print(" | ".join(header))
    for row in rows:
        print(
            " | ".join(
                _fmt(v)
                for v in (
                    row["key"],
                    row["status"],
                    row["demand_length"],
                    row["used_stock_length"],
                    row["utilization_rate"],
                    row["welding_joint_quantity"],
                    row["welding_pattern_type_quantity"],
                    row["cutting_pattern_type_quantity"],
                )
            )
        )


def _print_comparison(
    ours: list[dict[str, Any]], software: list[dict[str, Any]]
) -> None:
    sw_by_key = {r["key"]: r for r in software}
    print("\n=== 逐组对比（我们 vs 软件）===")
    print("规格key | 用料(我们/软件) | 利用率(我们/软件) | 焊口(我们/软件)")
    for row in ours:
        sw = sw_by_key.get(row["key"], {})
        print(
            f"{row['key']} | "
            f"{_fmt(row['used_stock_length'])}/{_fmt(sw.get('used_stock_length'))} | "
            f"{_fmt(row['utilization_rate'])}/{_fmt(sw.get('utilization_rate'))} | "
            f"{_fmt(row['welding_joint_quantity'])}/{_fmt(sw.get('welding_joint_quantity'))}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="与现有排料软件返回对比")
    parser.add_argument("input", type=Path, help="MOM 输入 JSON")
    parser.add_argument(
        "--software", type=Path, default=None, help="现有软件返回 JSON（可选）"
    )
    parser.add_argument(
        "--ours",
        type=Path,
        default=None,
        help="我们引擎的结果 JSON（可选；不给则现算）",
    )
    parser.add_argument("--time-limit", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.ours and args.ours.exists():
        our_result = _load_json(args.ours)
    else:
        our_result = _run_our_engine(args.input, args.time_limit)

    ours = _our_group_rows(our_result)
    _print_ours(ours)

    if args.software:
        if not args.software.exists():
            print(f"\n[提示] 未找到现有软件返回文件：{args.software}")
            return 0
        software_raw = _load_json(args.software)
        software = _extract_software_groups(software_raw)
        if not software:
            print(
                "\n[提示] 无法从现有软件返回中解析出逐组指标。"
                "请把返回样例结构告知，我在 _extract_software_groups 中补充字段映射。"
            )
            return 0
        _print_comparison(ours, software)
    else:
        print("\n[提示] 未提供 --software，仅展示我们的结果。拿到现有软件返回后用 --software 传入即可对比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
