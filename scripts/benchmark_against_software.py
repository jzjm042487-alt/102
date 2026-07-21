"""对标现有排料软件：用我们引擎重排软件的真实输入，逐组对比切法/拼法/指标。

用法：
    python scripts/benchmark_against_software.py [--status 1] [--limit 20] \
        [--offset 0] [--sample 0] [--time-limit 30] [--quiet] [--out <csv>]

数据源：backend/data/samples/software-io/batch-DGMOM/software_records.json
每条记录 = 一个材质规格组：MOMPROBLEMJSON(输入) + MOMRESULTJSON(软件输出)。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

RECORDS = (
    BACKEND
    / "data" / "samples" / "software-io" / "batch-DGMOM" / "software_records.json"
)


def _load_records() -> list[dict[str, Any]]:
    data = json.loads(RECORDS.read_text(encoding="utf-8-sig"))
    return data["RECORDS"]


def _software_metrics(result_json: str) -> dict[str, Any]:
    """Extract the software's headline metrics + pattern-type counts."""
    res = json.loads(result_json)
    gi = res.get("GeneralInfo", {})
    result = res.get("Result", {})
    weld = result.get("WeldingPattern", {}).get("WeldingPipe", []) or []
    cut = result.get("CuttingPattern", {}).get("CuttingPipe", []) or []

    # Distinct welding patterns = distinct part tuples across all welding pipes.
    weld_sigs: set[tuple[int, ...]] = set()
    weld_joints = 0
    for wp in weld:
        if not isinstance(wp, dict):
            continue
        patterns = wp.get("Pattern", []) or []
        if isinstance(patterns, dict):
            patterns = [patterns]
        for pat in patterns:
            if not isinstance(pat, dict):
                continue
            parts = tuple(int(float(x)) for x in str(pat.get("Part", "")).split())
            if parts:
                weld_sigs.add(parts)
                weld_joints += (len(parts) - 1) * int(float(pat.get("Number", 0) or 0))
    # Distinct cutting patterns = distinct (stock_len, sorted parts) tuples.
    cut_sigs: set[tuple[int, tuple[int, ...]]] = set()
    for cp in cut:
        if not isinstance(cp, dict):
            continue
        length = int(float(cp.get("Length", 0) or 0))
        parts = tuple(sorted(int(float(x)) for x in str(cp.get("Part", "")).split()))
        if parts:
            cut_sigs.add((length, parts))

    def _f(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "util_rate": _f(gi.get("UtilRate")),
        "welding_joints": _f(gi.get("WeldingJointQuantity")),
        "demand_length": _f(gi.get("PipeLength_Of_SingleMaterialSpecification")),
        "used_length": _f(gi.get("StockLength_Of_SingleMaterialSpecification")),
        "weld_pattern_types": len(weld_sigs),
        "cut_pattern_types": len(cut_sigs),
        "weld_joints_recomputed": weld_joints,
    }


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Tidy a real-world MOM export before feeding it to the strict parser.

    Field exports carry two dirty conditions the production contract rejects:
      * pipes with ``pipe_demand == 0`` (placeholder rows) -> dropped;
      * repeated ``Parent_node|jlxh|cube_no`` identities -> demand merged onto
        the first occurrence.
    Cleaning here keeps ``domain.parse_problem`` strict for the live API while
    letting the benchmark exercise the solver on messy historical data.
    """

    pipes = payload.get("Pipe")
    if not isinstance(pipes, list):
        return payload
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for pipe in pipes:
        if not isinstance(pipe, dict):
            continue
        try:
            demand = int(float(pipe.get("pipe_demand", 0)))
        except (TypeError, ValueError):
            demand = 0
        if demand <= 0:
            continue
        key = (
            str(pipe.get("Parent_node", pipe.get("figure_number", ""))).strip(),
            str(pipe.get("jlxh", "")).strip(),
            str(pipe.get("cube_no", "")).strip(),
        )
        if key in merged:
            existing = merged[key]
            existing["pipe_demand"] = int(
                float(existing.get("pipe_demand", 0))
            ) + demand
        else:
            copy = dict(pipe)
            copy["pipe_demand"] = demand
            merged[key] = copy
            order.append(key)
    cleaned = dict(payload)
    cleaned["Pipe"] = [merged[k] for k in order]
    return cleaned


def _run_ours(
    problem_json: str, time_limit: float, blade_margin: float | None = None,
    engine: str = "baseline",
) -> dict[str, Any]:
    from app.service import solve_and_verify

    payload = _clean_payload(json.loads(problem_json))
    if blade_margin is not None:
        # Verification-only override: force the cut kerf without touching the
        # engine's built-in default, so we can measure the kerf's real impact.
        payload["BladeMargin"] = blade_margin
    result = solve_and_verify(payload, time_limit_seconds=time_limit, engine=engine)
    groups = result.get("groups", [])
    if not groups:
        return {"status": result.get("status"), "error": "no groups"}
    m = groups[0].get("metrics", {})
    return {
        "status": m.get("solve_status"),
        "util_rate": m.get("utilization_rate"),
        "welding_joints": m.get("welding_joint_quantity"),
        "demand_length": m.get("demand_length"),
        "used_length": m.get("used_stock_length"),
        "weld_pattern_types": m.get("welding_pattern_type_quantity"),
        "cut_pattern_types": m.get("cutting_pattern_type_quantity"),
        "verified": result.get("verification", {}).get("passed"),
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="对标现有排料软件")
    p.add_argument(
        "--status",
        default="1",
        help="只跑该 MOMCALCULATESTATUS 的记录，逗号分隔可多选（默认1=成功）",
    )
    p.add_argument("--limit", type=int, default=20, help="最多对比多少条")
    p.add_argument("--offset", type=int, default=0, help="跳过前 N 条（分片用）")
    p.add_argument(
        "--max-demand", type=int, default=0,
        help="只保留总需求根数 <= 此值的组（0=不限；R&CG 适用建议 200）",
    )
    p.add_argument(
        "--min-demand", type=int, default=0,
        help="只保留总需求根数 >= 此值的组（0=不限）",
    )
    p.add_argument(
        "--sample", type=int, default=0, help="从候选集中随机抽样 N 条（0=不抽样，按顺序取）"
    )
    p.add_argument("--seed", type=int, default=42, help="随机抽样种子")
    p.add_argument("--time-limit", type=float, default=30.0)
    p.add_argument(
        "--engine",
        default="baseline",
        help="选择求解引擎：baseline(默认)/rcg/route3/v4/global",
    )
    p.add_argument(
        "--blade-margin",
        type=float,
        default=None,
        help="仅验证用：强制切口宽度 mm（覆盖引擎默认，不改代码）",
    )
    p.add_argument("--out", type=Path, default=None, help="把逐条结果写入 CSV")
    p.add_argument(
        "--quiet", action="store_true", help="不逐条打印，只出汇总（大批量用）"
    )
    return p


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "0.0%"
    return f"{100.0 * part / whole:.1f}%"


def _summarize(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    status_counter: dict[str, int] = {}
    util_deltas: list[float] = []
    joint_deltas: list[float] = []
    util_wins = joints_wins = solved = verified_ok = 0
    for row in rows:
        st = str(row.get("ours_status"))
        status_counter[st] = status_counter.get(st, 0) + 1
        is_solved = st not in ("None", "UNSOLVED") and not st.startswith("ERROR")
        if not is_solved:
            continue
        solved += 1
        if row.get("ours_verified") is True:
            verified_ok += 1
        ou, su = row.get("ours_util_rate"), row.get("sw_util_rate")
        if isinstance(ou, (int, float)) and isinstance(su, (int, float)):
            util_deltas.append(ou - su)
            if ou >= su - 1e-6:
                util_wins += 1
        oj, sj = row.get("ours_welding_joints"), row.get("sw_welding_joints")
        if isinstance(oj, (int, float)) and isinstance(sj, (int, float)):
            joint_deltas.append(oj - sj)
            if oj <= sj:
                joints_wins += 1

    print("\n================ 汇总 ================")
    print(f"总对比条数：{total}")
    print("我们求解状态分布：")
    for st, cnt in sorted(status_counter.items(), key=lambda kv: -kv[1]):
        print(f"  {st}: {cnt} ({_pct(cnt, total)})")
    print(
        f"\n解出 {solved}/{total} ({_pct(solved, total)})，"
        f"其中独立校验通过 {verified_ok}/{solved}"
    )
    if util_deltas:
        print(
            f"利用率差(我们-软件)：均值 {statistics.mean(util_deltas):+.4f}，"
            f"中位 {statistics.median(util_deltas):+.4f}，"
            f"最差 {min(util_deltas):+.4f}，最好 {max(util_deltas):+.4f}；"
            f"不劣 {util_wins}/{len(util_deltas)}"
        )
    if joint_deltas:
        print(
            f"焊口差(我们-软件)：均值 {statistics.mean(joint_deltas):+.1f}，"
            f"中位 {statistics.median(joint_deltas):+.1f}，"
            f"最差 {max(joint_deltas):+.0f}，最好 {min(joint_deltas):+.0f}；"
            f"不劣 {joints_wins}/{len(joint_deltas)}"
        )


def _total_demand(problem_json: str) -> int:
    """Cheap total pipe-demand estimate from the raw payload (no full parse)."""
    try:
        payload = _clean_payload(json.loads(problem_json))
    except Exception:  # noqa: BLE001
        return -1
    pipes = payload.get("Pipe")
    if not isinstance(pipes, list):
        return 0
    total = 0
    for pipe in pipes:
        if not isinstance(pipe, dict):
            continue
        try:
            total += int(float(pipe.get("pipe_demand", 0)))
        except (TypeError, ValueError):
            continue
    return total


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = _load_records()
    wanted = {s.strip() for s in str(args.status).split(",") if s.strip()}
    candidates = [
        r
        for r in records
        if r.get("MOMCALCULATESTATUS") in wanted
        and r.get("MOMRESULTJSON") not in (None, "", "\ufffd")
    ]
    if args.max_demand > 0 or args.min_demand > 0:
        filtered = []
        for r in candidates:
            td = _total_demand(r.get("MOMPROBLEMJSON", ""))
            if td < 0:
                continue
            if args.min_demand > 0 and td < args.min_demand:
                continue
            if args.max_demand > 0 and td > args.max_demand:
                continue
            filtered.append(r)
        candidates = filtered
    if args.sample > 0:
        rng = random.Random(args.seed)
        candidates = rng.sample(candidates, min(args.sample, len(candidates)))
    picked = candidates[args.offset : args.offset + args.limit]

    print(
        f"对比 {len(picked)} 条 (engine={args.engine}, status={sorted(wanted)}, "
        f"demand∈[{args.min_demand or 0},{args.max_demand or '∞'}], "
        f"sample={args.sample or '-'}, blade_margin={args.blade_margin if args.blade_margin is not None else '默认'}, "
        f"time_limit={args.time_limit}s)\n"
    )
    if not args.quiet:
        print("spec | 软件util/焊口/拼型/切型 | 我们util/焊口/拼型/切型 | 状态 | 校验")
    rows_out: list[dict[str, Any]] = []
    progress_path = args.out.with_suffix(".progress.jsonl") if args.out else None
    if progress_path:
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text("", encoding="utf-8")
    for i, r in enumerate(picked):
        spec = f'{r.get("MOMMATERIAL","")}/{r.get("MOMOUTSIDEDIAMETER","")}x{r.get("MOMWALLTHICKNESS","")}'
        # Native-level SCIP crashes kill the whole process; log which group we are
        # about to solve so the culprit is identifiable from the tail of the log.
        print(f"  [{i + 1}/{len(picked)}] START no={r.get('MOMPROBLEMNO')} spec={spec}", flush=True)
        sw = _software_metrics(r["MOMRESULTJSON"])
        t0 = time.monotonic()
        try:
            ours = _run_ours(
                r["MOMPROBLEMJSON"], args.time_limit, args.blade_margin, args.engine
            )
        except Exception as exc:  # noqa: BLE001
            ours = {"status": f"ERROR:{type(exc).__name__}", "error": str(exc)}
        elapsed = time.monotonic() - t0

        if not args.quiet:
            print(
                f'{spec} | '
                f'{_fmt(sw["util_rate"])}/{_fmt(sw["welding_joints"])}/{sw["weld_pattern_types"]}/{sw["cut_pattern_types"]} | '
                f'{_fmt(ours.get("util_rate"))}/{_fmt(ours.get("welding_joints"))}/{_fmt(ours.get("weld_pattern_types"))}/{_fmt(ours.get("cut_pattern_types"))} | '
                f'{ours.get("status")} | {ours.get("verified")} ({elapsed:.1f}s)'
            )
        elif (i + 1) % 20 == 0:
            print(f"  ...已跑 {i + 1}/{len(picked)}")
        rows_out.append(
            {
                "problem_no": r.get("MOMPROBLEMNO"),
                "spec": spec,
                **{f"sw_{k}": v for k, v in sw.items()},
                **{f"ours_{k}": v for k, v in ours.items()},
                "elapsed_s": round(elapsed, 2),
            }
        )
        if progress_path:
            with open(progress_path, "a", encoding="utf-8") as pf:
                pf.write(json.dumps(rows_out[-1], ensure_ascii=False) + "\n")

    _summarize(rows_out)
    if args.out and rows_out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = sorted({k for row in rows_out for k in row})
        with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows_out)
        print(f"\n逐条结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
