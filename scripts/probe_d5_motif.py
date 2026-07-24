"""D5 motif 专线探针：求解 + 复用 _gen_report SVG HTML 详审。

用法:
  .venv\\Scripts\\python.exe scripts/probe_d5_motif.py --limit 6 --tl 180
  .venv\\Scripts\\python.exe scripts/probe_d5_motif.py --idxs 390,1756,2747 --tl 300
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(ROOT / "backend")]

OUT = ROOT / "data" / "acceptance" / "d5_motif_probe"


def _d5_success_idxs() -> list[int]:
    cases_root = ROOT / "data" / "legacy-cases"
    pools = json.loads((cases_root / "difficulty_pools.json").read_text(encoding="utf-8"))
    by = {
        c["idx"]: c
        for c in json.loads((cases_root / "index.json").read_text(encoding="utf-8"))["cases"]
    }
    return [
        int(i)
        for i in pools["pools"]["D5-极难"]
        if by.get(i, {}).get("legacy_status") == "success"
    ]


def _legacy_layout(case: dict) -> tuple[list, list, bool]:
    from scripts._export_mock import legacy_detail

    lr = case.get("legacy_result")
    if not isinstance(lr, dict) or "Result" not in lr:
        return [], [], False
    try:
        cut_rows, weld_rows = legacy_detail({"MOMRESULTJSON": lr})
    except Exception:
        return [], [], False
    cuts = [
        {
            "L": c["L"],
            "segs": c["segs"],
            "waste": c.get("trim", 0),
            "count": c.get("num", 1),
        }
        for c in cut_rows
    ]
    welds = [
        {
            "pipe_len": w["len"],
            "seq": w["segs"],
            "joints": max(0, len(w["segs"]) - 1),
            "count": w.get("num", 1),
        }
        for w in weld_rows
    ]
    return cuts, welds, bool(cuts or welds)


def _solve_one(idx: int, tl: float, joint_slack: int = 0) -> dict:
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
    from scripts.solve_d5_motif import solve_d5

    t0 = time.monotonic()
    case = load_legacy_case(ROOT / "data" / "legacy-cases", idx)
    g = merge_equivalent_pipes(
        parse_problem(clean_mom_problem(case["problem"])).groups[0]
    )
    met = (case.get("metrics") or {}).get("metrics") or {}
    legacy = {
        "joints": met.get("welding_joints"),
        "weld_types": met.get("weld_pattern_types"),
        "cut_types": met.get("cut_pattern_types"),
        "util": met.get("utilization_rate"),
    }
    uf = None  # quality via auto_tag / HTML human review, not hard util cut
    sol = solve_d5(
        g,
        tl=tl,
        util_floor=uf,
        legacy_metrics=legacy,
        joint_slack=joint_slack,
    )
    lg_cuts, lg_welds, has_lg = _legacy_layout(case)
    dt = round(time.monotonic() - t0, 3)
    if sol is None:
        return {
            "level": idx,
            "spec": f"D5 idx={idx}",
            "solved": False,
            "auto_tag": "unsolved",
            "dt": dt,
            "legacy": legacy,
            "metrics": {},
            "cuts": [],
            "welds": [],
            "legacy_cuts": lg_cuts,
            "legacy_welds": lg_welds,
            "has_legacy_layout": has_lg,
            "self_check_ok": False,
            "demand_length": int(g.demand_length),
            "target_rate": float(legacy.get("util") or 0.95),
            "pipes": [{"length": p.length, "demand": p.demand} for p in g.pipes],
            "stocks": [{"length": s.length, "quantity": s.quantity} for s in g.stocks],
        }
    return {
        "level": idx,
        "spec": f"D5 idx={idx} tag={sol.get('auto_tag')}",
        "solved": True,
        "auto_tag": sol.get("auto_tag") or "solved_no_legacy",
        "dt": dt,
        "legacy": legacy,
        "metrics": {
            "joints": sol["joints"],
            "weld_types": sol["weld_types"],
            "cut_types": sol["cut_types"],
            "seg_types": sol["seg_types"],
            "util": sol["util"],
            "used_len": sol["used_len"],
        },
        "cuts": sol.get("cuts") or [],
        "welds": sol.get("welds") or [],
        "legacy_cuts": lg_cuts,
        "legacy_welds": lg_welds,
        "has_legacy_layout": has_lg,
        "self_check_ok": True,
        "demand_length": int(g.demand_length),
        "target_rate": float(legacy.get("util") or 0.95),
        "pipes": [{"length": p.length, "demand": p.demand} for p in g.pipes],
        "stocks": [{"length": s.length, "quantity": s.quantity} for s in g.stocks],
        "solver_path": sol.get("solver_path"),
        "diagnosis": sol.get("diagnosis"),
    }


def _remaining_idxs(idxs: list[int], cases: list[dict]) -> list[int]:
    completed = {int(row["level"]) for row in cases if row.get("level") is not None}
    return [idx for idx in idxs if idx not in completed]


def _write_html(cases: list[dict], out_path: Path) -> None:
    # 复用 _gen_report 模板与写盘逻辑
    from scripts._gen_report import HTML_TMPL
    import time as _time

    # 在标题旁带上 auto_tag（模板用 L{level}，此处 level=idx）
    html = (
        HTML_TMPL.replace("__DATA__", json.dumps(cases, ensure_ascii=False))
        .replace("__TS__", _time.strftime("%Y-%m-%d %H:%M"))
        .replace("V4 排料方案验收报告", "D5 Motif 专线验收报告（复用 V4 HTML）")
        .replace("V4 (我们)", "D5 Motif")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tl", type=float, default=180.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--idxs", type=str, default="")
    ap.add_argument("--out", type=str, default=str(OUT))
    ap.add_argument(
        "--joint-slack",
        type=int,
        default=0,
        help="允许相对最小焊口增加的绝对数量，用于 Pareto 模式压缩",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="复用 out/cases.json 中已完成的案例，并从未完成案例继续",
    )
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.idxs.strip():
        idxs = [int(x) for x in args.idxs.split(",") if x.strip()]
    else:
        idxs = _d5_success_idxs()
    if args.limit > 0:
        idxs = idxs[: args.limit]

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    cases_path = out_root / "cases.json"
    cases: list[dict] = []
    if args.resume and cases_path.exists():
        raw_cases = json.loads(cases_path.read_text(encoding="utf-8"))
        if isinstance(raw_cases, list):
            cases = raw_cases
    pending_idxs = _remaining_idxs(idxs, cases)
    joint_slack = max(0, int(args.joint_slack))
    print(
        f"d5 motif probe n={len(idxs)} pending={len(pending_idxs)} tl={args.tl} "
        f"joint_slack={joint_slack}",
        flush=True,
    )
    t0 = time.monotonic()
    for n, idx in enumerate(pending_idxs, 1):
        row = _solve_one(idx, args.tl, joint_slack=joint_slack)
        cases.append(row)
        tag = row.get("auto_tag")
        m = row.get("metrics") or {}
        print(
            f"[{n}/{len(pending_idxs)}] idx={idx} {tag} "
            f"j={m.get('joints')} u={m.get('util')} dt={row.get('dt')}",
            flush=True,
        )
        # 增量落盘防中断
        cases_path.write_text(
            json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_html(cases, out_root / "report.html")

    counts: dict[str, int] = {}
    for c in cases:
        counts[c.get("auto_tag") or "?"] = counts.get(c.get("auto_tag") or "?", 0) + 1
    brief = {
        "n": len(cases),
        "tl": args.tl,
        "joint_slack": joint_slack,
        "elapsed_wall": round(time.monotonic() - t0, 1),
        "auto_tag_counts": counts,
        "lex_non_worse_rate": counts.get("lex_non_worse", 0) / max(len(cases), 1),
        "mixed_rate": counts.get("mixed", 0) / max(len(cases), 1),
        "report_html": str(out_root / "report.html"),
    }
    (out_root / "summary_brief.json").write_text(
        json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("=== DONE ===", flush=True)
    print(json.dumps(brief, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
