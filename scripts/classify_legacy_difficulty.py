"""为落盘语料批量打难度标签（D1–D5 / 不可计算）。

读取 data/legacy-cases 下各案例，写入 difficulty.json，并更新 index 与汇总文档。

用法:
  python scripts/classify_legacy_difficulty.py
  python scripts/classify_legacy_difficulty.py -o data/legacy-cases
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

from scripts.legacy_corpus_lib import clean_mom_problem, extract_input_metrics  # noqa: E402
from scripts.legacy_difficulty import (  # noqa: E402
    DIFFICULTY_TAXONOMY_VERSION,
    TIER_LABELS,
    classify_difficulty,
    is_computable_for_solver,
)


def _load_legacy_output(case_path: Path) -> dict | None:
    p = case_path / "output.json"
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    if not raw or raw == "null":
        return None
    return json.loads(raw)


def _input_from_case(case_path: Path) -> tuple[dict | None, str | None]:
    p = case_path / "input.json"
    if not p.exists():
        return None, "missing input.json"
    try:
        problem = json.loads(p.read_text(encoding="utf-8"))
        if "_raw_unparsed" in problem:
            return None, "unparsed input"
        return extract_input_metrics(clean_mom_problem(problem)), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def write_summary_md(out_root: Path, rows: list[dict], elapsed: float) -> None:
    computable = [r for r in rows if r.get("computable")]
    ctr = Counter(r.get("tier") for r in computable)
    legacy_ok = sum(1 for r in computable if r.get("legacy_status") == "success")
    lines = [
        "# 语料难度分类汇总",
        "",
        f"> 由 `scripts/classify_legacy_difficulty.py` 生成；taxonomy v{DIFFICULTY_TAXONOMY_VERSION}",
        "",
        f"- 落盘目录: `{out_root.resolve()}`",
        f"- 总案例: **{len(rows)}**",
        f"- **可计算**（输入可解析且非 infeasible_len）: **{len(computable)}**",
        f"- 可计算且老软件 success（有真实输出离散度）: **{legacy_ok}**",
        f"- 耗时: {elapsed:.1f}s",
        "",
        "方法论见 `docs/legacy-corpus-difficulty-taxonomy.md`。",
        "",
        "## 可计算案例 — 五档分布",
        "",
        "| 档位 | 数量 | 占比 |",
        "|------|-----:|-----:|",
    ]
    for label in TIER_LABELS:
        n = ctr.get(label, 0)
        pct = 100 * n / len(computable) if computable else 0
        lines.append(f"| {label} | {n} | {pct:.1f}% |")
    blocked = Counter(r.get("block_reason") for r in rows if not r.get("computable"))
    lines.extend(["", "## 不可计算原因", "", "| 原因 | 数量 |", "|------|-----:|"])
    for reason, n in blocked.most_common():
        lines.append(f"| `{reason}` | {n} |")
    lines.extend(
        [
            "",
            "## 各档抽样 idx（每档最多 5 个）",
            "",
        ]
    )
    by_tier: dict[str, list[int]] = {t: [] for t in TIER_LABELS}
    for r in computable:
        t = r.get("tier")
        if t in by_tier and len(by_tier[t]) < 5:
            by_tier[t].append(r["idx"])
    for label in TIER_LABELS:
        lines.append(f"- **{label}**: {', '.join(str(i) for i in by_tier[label])}")
    lines.append("")
    Path(ROOT / "docs" / "legacy-corpus-difficulty-summary.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out", type=Path, default=ROOT / "data" / "legacy-cases")
    args = parser.parse_args()
    index_path = args.out / "index.json"
    if not index_path.exists():
        print(f"缺少 {index_path}，请先运行 export_legacy_cases.py", file=sys.stderr)
        return 1

    index = json.loads(index_path.read_text(encoding="utf-8"))
    cases_meta = index.get("cases") or []
    t0 = time.monotonic()
    rows: list[dict] = []

    for i, entry in enumerate(cases_meta):
        idx = entry["idx"]
        case_path = args.out / entry["dir"]
        metrics_path = case_path / "metrics.json"
        metrics_doc = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
        inp_extract, inp_err = _input_from_case(case_path)
        if inp_err and not metrics_doc.get("input_error"):
            inp_err = inp_err
        input_error = metrics_doc.get("input_error") or inp_err
        if inp_extract:
            agg = inp_extract["aggregate"]
            per_group = inp_extract["per_group"]
        else:
            agg = metrics_doc.get("aggregate") or {}
            per_group = []

        met = metrics_doc.get("metrics") or {}
        legacy_result = _load_legacy_output(case_path)
        legacy_status = met.get("legacy_status")

        diff = classify_difficulty(
            aggregate=agg,
            per_group=per_group,
            geom_class=met.get("geom_class") or (
                per_group[0].get("geom_class") if per_group else None
            ),
            input_error=input_error,
            legacy_result=legacy_result,
            legacy_status=legacy_status,
        )
        (case_path / "difficulty.json").write_text(
            json.dumps(diff, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        row = {
            "idx": idx,
            "dir": entry.get("dir"),
            "legacy_status": legacy_status,
            "computable": diff["computable"],
            "block_reason": diff.get("block_reason"),
            "tier_id": diff.get("tier_id"),
            "tier": diff.get("tier"),
            "composite_score": diff.get("composite_score"),
            "pipe_pieces": agg.get("pipe_pieces"),
            "stock_pieces": agg.get("stock_pieces"),
            "input_unique_lengths": diff["axes"]["input_length_dispersion"].get("unique_lengths"),
            "output_unique_segments": diff["axes"]["output_segment_dispersion"].get(
                "unique_segments"
            ),
            "output_disp_source": diff["axes"]["output_segment_dispersion"].get("source"),
            "geom_class": met.get("geom_class"),
            "reasons": diff.get("reasons"),
        }
        rows.append(row)
        entry["difficulty"] = {
            "tier": diff.get("tier"),
            "tier_id": diff.get("tier_id"),
            "composite_score": diff.get("composite_score"),
            "computable": diff.get("computable"),
        }
        if (i + 1) % 500 == 0:
            print(f"  ... {i + 1}/{len(cases_meta)}", flush=True)

    index["difficulty_taxonomy_version"] = DIFFICULTY_TAXONOMY_VERSION
    index["difficulty_computable_count"] = sum(1 for r in rows if r["computable"])
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    by_tier = {t: [] for t in TIER_LABELS}
    for r in rows:
        if r["computable"] and r["tier"] in by_tier:
            by_tier[r["tier"]].append(r["idx"])
    (args.out / "difficulty_pools.json").write_text(
        json.dumps(
            {
                "taxonomy_version": DIFFICULTY_TAXONOMY_VERSION,
                "tier_labels": list(TIER_LABELS),
                "pools": by_tier,
                "computable_idx": [r["idx"] for r in rows if r["computable"]],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    elapsed = time.monotonic() - t0
    write_summary_md(args.out, rows, elapsed)
    print(
        f"难度标注完成: 可计算 {index['difficulty_computable_count']}/{len(rows)} "
        f"-> difficulty_pools.json + docs/legacy-corpus-difficulty-summary.md [{elapsed:.1f}s]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
