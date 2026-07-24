"""将老软件 MOM 导出拆成单案例落盘，供后续通用回归测试。

每条语料记录 → 一个目录：
  input.json, output.json, metrics.json, analysis.md

并生成 index.json 与汇总 Markdown。

用法:
  python scripts/export_legacy_cases.py
  python scripts/export_legacy_cases.py --from 0 --to 99
  python scripts/export_legacy_cases.py --only-solved
  python scripts/export_legacy_cases.py --source path/to/export.json -o data/legacy-cases
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scripts.legacy_corpus_lib import (  # noqa: E402
    CASE_FORMAT_VERSION,
    DEFAULT_CORPUS_PATH,
    analyze_record,
    case_dir_name,
    clean_mom_problem,
    load_corpus_records,
    render_case_analysis_md,
)


def write_case(
    out_root: Path,
    record: dict,
    idx: int,
    *,
    slim_output: bool,
) -> dict:
    analysis = analyze_record(record, idx)
    dirname = case_dir_name(idx, record.get("ID"))
    case_path = out_root / dirname
    case_path.mkdir(parents=True, exist_ok=True)

    prob_raw = record.get("MOMPROBLEMJSON")
    problem = None
    if prob_raw:
        try:
            problem = json.loads(prob_raw) if isinstance(prob_raw, str) else prob_raw
            problem = clean_mom_problem(problem)
        except (json.JSONDecodeError, TypeError):
            problem = {"_raw_unparsed": str(prob_raw)[:500]}

    res_raw = record.get("MOMRESULTJSON")
    output_obj = None
    if res_raw and str(res_raw).strip()[:1] in ("{", "["):
        try:
            output_obj = json.loads(res_raw) if isinstance(res_raw, str) else res_raw
        except (json.JSONDecodeError, TypeError):
            output_obj = None

    if problem is not None:
        (case_path / "input.json").write_text(
            json.dumps(problem, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if slim_output and output_obj:
        gi = output_obj.get("GeneralInfo") or {}
        inner = output_obj.get("Result") or {}
        slim = {
            "GeneralInfo": gi,
            "Result": {
                "WeldingPattern": inner.get("WeldingPattern"),
                "CuttingPattern": inner.get("CuttingPattern"),
                "UnusedMaterials": inner.get("UnusedMaterials"),
            },
        }
        (case_path / "output.json").write_text(
            json.dumps(slim, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        (case_path / "output.json").write_text(
            json.dumps(output_obj, ensure_ascii=False, indent=2) if output_obj else "null",
            encoding="utf-8",
        )

    metrics_doc = {
        "format_version": CASE_FORMAT_VERSION,
        "meta": analysis.get("meta"),
        "metrics": analysis.get("metrics"),
        "input_error": analysis.get("input_error"),
        "output_error": analysis.get("output_error"),
        "aggregate": (analysis.get("input") or {}).get("aggregate"),
        "output_summary": {
            k: (analysis.get("output") or {}).get(k)
            for k in (
                "legacy_status",
                "welding_joints",
                "welding_joints_recomputed",
                "weld_pattern_types",
                "cut_pattern_types",
                "utilization_rate",
            )
        },
    }
    (case_path / "metrics.json").write_text(
        json.dumps(metrics_doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (case_path / "analysis.md").write_text(
        render_case_analysis_md(analysis),
        encoding="utf-8",
    )

    met = analysis.get("metrics") or {}
    return {
        "idx": idx,
        "dir": dirname,
        "id": record.get("ID"),
        "problem_no": record.get("MOMPROBLEMNO"),
        "material": record.get("MOMMATERIAL"),
        "legacy_status": met.get("legacy_status"),
        "difficulty": met.get("difficulty"),
        "geom_class": met.get("geom_class"),
        "pipe_types": met.get("pipe_types"),
        "pipe_pieces": met.get("pipe_pieces"),
        "stock_types": met.get("stock_types"),
        "stock_pieces": met.get("stock_pieces"),
        "welding_joints": met.get("welding_joints"),
        "weld_pattern_types": met.get("weld_pattern_types"),
        "cut_pattern_types": met.get("cut_pattern_types"),
        "utilization_rate": met.get("utilization_rate"),
        "input_ok": analysis.get("input_error") is None,
        "output_ok": analysis.get("output_error") is None and met.get("legacy_status") == "success",
    }


def write_summary_md(out_root: Path, index: dict, elapsed: float) -> None:
    cases = index.get("cases") or []
    n = len(cases)
    status_ctr = Counter(c.get("legacy_status") for c in cases)
    diff_ctr = Counter(c.get("difficulty") for c in cases)
    geom_ctr = Counter(c.get("geom_class") for c in cases)
    solved = [c for c in cases if c.get("legacy_status") == "success" and c.get("input_ok")]
    lines = [
        "# 老软件语料落盘汇总",
        "",
        f"> 由 `scripts/export_legacy_cases.py` 生成，勿手改。",
        "",
        f"- 语料路径: `{index.get('source')}`",
        f"- 落盘目录: `{index.get('out_root')}`",
        f"- 记录范围: {index.get('range')}",
        f"- 案例数: **{n}**",
        f"- 耗时: {elapsed:.1f}s",
        "",
        "## 老软件结果状态",
        "",
        "| 状态 | 数量 |",
        "|------|-----:|",
    ]
    for st, cnt in status_ctr.most_common():
        lines.append(f"| `{st}` | {cnt} |")
    lines.extend(
        [
            "",
            "## 难度分布（可解析输入）",
            "",
            "| 难度 | 数量 |",
            "|------|-----:|",
        ]
    )
    for d, cnt in diff_ctr.most_common():
        lines.append(f"| {d} | {cnt} |")
    lines.extend(
        [
            "",
            "## 几何分类（首组）",
            "",
            "| 分类 | 数量 |",
            "|------|-----:|",
        ]
    )
    for g, cnt in geom_ctr.most_common():
        lines.append(f"| `{g}` | {cnt} |")
    if solved:
        joints = [c["welding_joints"] for c in solved if c.get("welding_joints") is not None]
        utils = [c["utilization_rate"] for c in solved if c.get("utilization_rate")]
        lines.extend(
            [
                "",
                "## 成功案例规模（摘要）",
                "",
                f"- 成功且输入可解析: **{len(solved)}**",
            ]
        )
        if joints:
            lines.append(
                f"- 焊口: min={min(joints)}, max={max(joints)}, "
                f"median={sorted(joints)[len(joints)//2]}"
            )
        if utils:
            lines.append(
                f"- 利用率: min={min(utils):.4f}, max={max(utils):.4f}"
            )
    lines.extend(
        [
            "",
            "## 单案例目录结构",
            "",
            "见 `docs/legacy-corpus-case-format.md`。",
            "",
            "## 测试加载示例",
            "",
            "```python",
            "from pathlib import Path",
            "from scripts.legacy_corpus_lib import load_legacy_case",
            "case = load_legacy_case(Path('data/legacy-cases'), idx=0)",
            "problem = case['problem']",
            "legacy = case['legacy_result']",
            "```",
            "",
        ]
    )
    summary_path = ROOT / "docs" / "legacy-corpus-export-summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("-o", "--out", type=Path, default=ROOT / "data" / "legacy-cases")
    parser.add_argument("--from", dest="from_idx", type=int, default=0)
    parser.add_argument("--to", dest="to_idx", type=int, default=-1, help="-1 = 到最后一条")
    parser.add_argument("--only-solved", action="store_true", help="只导出老软件 Success 且输入可解析的")
    parser.add_argument(
        "--full-output",
        action="store_true",
        help="output.json 写完整 MOMRESULTJSON（默认写 slim 布局字段）",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"语料文件不存在: {args.source}", file=sys.stderr)
        return 1

    records = load_corpus_records(args.source)
    hi = len(records) - 1 if args.to_idx < 0 else min(args.to_idx, len(records) - 1)
    lo = max(0, args.from_idx)
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    index_cases: list[dict] = []
    skipped = 0
    for idx in range(lo, hi + 1):
        rec = records[idx]
        if args.only_solved:
            analysis = analyze_record(rec, idx)
            met = analysis.get("metrics") or {}
            if met.get("legacy_status") != "success" or analysis.get("input_error"):
                skipped += 1
                continue
        entry = write_case(
            args.out,
            rec,
            idx,
            slim_output=not args.full_output,
        )
        index_cases.append(entry)
        if (idx - lo + 1) % 200 == 0:
            print(f"  ... {idx - lo + 1}/{hi - lo + 1}", flush=True)

    index = {
        "format_version": CASE_FORMAT_VERSION,
        "source": str(args.source.resolve()),
        "out_root": str(args.out.resolve()),
        "range": f"{lo}..{hi}",
        "only_solved": args.only_solved,
        "skipped": skipped,
        "count": len(index_cases),
        "cases": index_cases,
    }
    (args.out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    elapsed = time.monotonic() - t0
    write_summary_md(args.out, index, elapsed)
    print(
        f"导出 {len(index_cases)} 案例 -> {args.out} "
        f"(index.json + docs/legacy-corpus-export-summary.md) [{elapsed:.1f}s]"
    )
    if args.only_solved:
        print(f"跳过非成功/不可解析: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
