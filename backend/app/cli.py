from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .service import solve_and_verify


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取输入JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("输入JSON顶层必须是对象")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="蛇形管一维排料批处理")
    parser.add_argument("input", type=Path, help="MOM输入JSON文件")
    parser.add_argument("output", type=Path, help="结果JSON文件")
    parser.add_argument(
        "--time-limit",
        type=float,
        default=None,
        help="每个任务求解时间上限（秒）",
    )
    parser.add_argument(
        "--indent", type=int, default=2, choices=(0, 2, 4), help="输出缩进"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _load_json(args.input)
        result = solve_and_verify(payload, time_limit_seconds=args.time_limit)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=args.indent if args.indent else None,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"排料失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not result.get("verification", {}).get("passed", False):
        print("排料结果未通过独立验证", file=sys.stderr)
        return 3
    print(
        f"排料完成：status={result.get('status')} "
        f"utilization={result.get('summary', {}).get('utilization_rate')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
