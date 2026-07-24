"""算法资产扫盘：从简单案例起逐引擎计算，只记录结果、不改算法。

默认：D1-入门 前 80 例 × 6 引擎，子进程硬超时。

用法:
  python scripts/algorithm_sweep.py
  python scripts/algorithm_sweep.py --tier D2-常规 --limit 50 --tl 90
  python scripts/algorithm_sweep.py --engines baseline,v4 --limit 20

输出:
  data/algorithm-sweep/<run_id>/results.jsonl
  data/algorithm-sweep/<run_id>/summary.json
  docs/algorithm-sweep-report.md  (指向最近一次 run)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINES = ("baseline", "route3", "rcg", "global", "v4", "arcflow_direct")
# 子进程硬超时（秒）：global 在部分小案例上易顶满 tl，单独收紧以便扫盘推进
PROC_TIMEOUT_BY_ENGINE: dict[str, float] = {
    "global": 75.0,
}
DEFAULT_PROC_TIMEOUT = 150.0


def _load_done_pairs(path: Path | None) -> set[tuple[int, str]]:
    if not path or not path.is_file():
        return set()
    done: set[tuple[int, str]] = set()
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
            done.add((int(row["idx"]), str(row["engine"])))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return done


def _run_worker(
    engine: str,
    cases_root: Path,
    idx: int,
    tl: float,
    proc_timeout: float,
) -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "_algorithm_sweep_worker.py"),
        engine,
        str(cases_root),
        str(idx),
        str(tl),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=proc_timeout,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return {
            "idx": idx,
            "engine": engine,
            "solve_status": "PROC_TIMEOUT",
            "verified": False,
            "error": f"timeout>{proc_timeout}s",
        }
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    err = (proc.stderr or proc.stdout or "")[-500:]
    return {
        "idx": idx,
        "engine": engine,
        "solve_status": "WORKER_FAIL",
        "verified": False,
        "error": err,
        "returncode": proc.returncode,
    }


def _is_solved(row: dict) -> bool:
    st = str(row.get("solve_status") or "")
    if st in ("UNSOLVED", "PROC_TIMEOUT", "WORKER_FAIL", "UNSUPPORTED_MULTI_GROUP"):
        return False
    if st.startswith("ERROR"):
        return False
    if st in ("LENGTH_SHORTFALL", "INFEASIBLE"):
        return False
    if row.get("verified") is True:
        return True
    if st == "SOLVED":
        return True
    if "OPTIMAL" in st.upper() or st.upper().startswith("TARGET"):
        return row.get("utilization_rate") is not None
    return row.get("utilization_rate") is not None and st not in ("", "?")


def write_report_md(run_dir: Path, summary: dict) -> None:
    lines = [
        "# 算法扫盘报告（最近一次）",
        "",
        f"> run_id: `{summary['run_id']}` · 生成于 {summary['finished_at']}",
        "",
        f"- 难度档: **{summary['tier']}**",
        f"- 案例数: {summary['case_count']}（idx 列表见 summary.json）",
        f"- 引擎: {', '.join(summary['engines'])}",
        f"- time_limit: {summary['time_limit']}s · proc_timeout: {summary['proc_timeout']}s",
        f"- 原始结果: `{run_dir.as_posix()}/results.jsonl`",
        "",
        "资产说明见 [algorithm-asset-inventory.md](algorithm-asset-inventory.md)。",
        "",
        "## 各引擎汇总",
        "",
        "| 引擎 | 有解 | 校验通过 | 超时/失败 | 中位耗时(s) |",
        "|------|-----:|--------:|----------:|------------:|",
    ]
    for eng, s in summary["by_engine"].items():
        lines.append(
            f"| `{eng}` | {s['solved']} | {s['verified']} | {s['failed']} | {s.get('elapsed_median') or '-'} |"
        )
    lines.extend(["", "## 按 solve_status（各引擎 Top）", ""])
    for eng, ctr in summary.get("status_by_engine", {}).items():
        top = ", ".join(f"`{k}`×{v}" for k, v in ctr.most_common(5))
        lines.append(f"- **{eng}**: {top}")
    lines.append("")
    Path(ROOT / "docs" / "algorithm-sweep-report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c",
        "--cases",
        type=Path,
        default=ROOT / "data" / "legacy-cases",
    )
    parser.add_argument("--tier", default="D1-入门", help="difficulty_pools.json 中的档位名")
    parser.add_argument("--limit", type=int, default=80, help="该档最多跑多少 idx")
    parser.add_argument("--offset", type=int, default=0, help="档位列表内偏移")
    parser.add_argument("--tl", type=float, default=60.0, help="引擎 time_limit_seconds")
    parser.add_argument("--proc-timeout", type=float, default=180.0, help="单任务子进程硬超时")
    parser.add_argument(
        "--engines",
        default=",".join(DEFAULT_ENGINES),
        help="逗号分隔引擎名",
    )
    parser.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="已有 results.jsonl，跳过其中 (idx,engine) 任务",
    )
    args = parser.parse_args()

    pools_path = args.cases / "difficulty_pools.json"
    if not pools_path.exists():
        print("缺少 difficulty_pools.json，请先 classify_legacy_difficulty.py", file=sys.stderr)
        return 1
    pools = json.loads(pools_path.read_text(encoding="utf-8"))
    tier_list = (pools.get("pools") or {}).get(args.tier)
    if not tier_list:
        print(f"未知档位: {args.tier}", file=sys.stderr)
        return 1
    idx_list = tier_list[args.offset : args.offset + args.limit]
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    done_pairs = _load_done_pairs(args.resume)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = ROOT / "data" / "algorithm-sweep" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    results_path = run_dir / "results.jsonl"
    if args.resume and args.resume.is_file():
        # 继承已完成行
        results_path.write_text(args.resume.read_text(encoding="utf-8"), encoding="utf-8")

    t0 = time.monotonic()
    n_task = len(idx_list) * len(engines)
    done = len(done_pairs)

    with results_path.open("a", encoding="utf-8") as fh:
        for idx in idx_list:
            for engine in engines:
                if (idx, engine) in done_pairs:
                    continue
                proc_timeout = PROC_TIMEOUT_BY_ENGINE.get(engine, args.proc_timeout)
                row = _run_worker(engine, args.cases, idx, args.tl, proc_timeout)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                done += 1
                if done % 30 == 0:
                    print(f"  ... {done}/{n_task}", flush=True)

    # 汇总
    rows = [json.loads(ln) for ln in results_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    by_engine: dict[str, dict] = {}
    status_by_engine: dict[str, Counter] = defaultdict(Counter)
    for eng in engines:
        erows = [r for r in rows if r.get("engine") == eng]
        solved = sum(1 for r in erows if _is_solved(r))
        verified = sum(1 for r in erows if r.get("verified") is True)
        failed = len(erows) - solved
        elapsed = sorted(
            r["elapsed_s"] for r in erows if isinstance(r.get("elapsed_s"), (int, float))
        )
        med = elapsed[len(elapsed) // 2] if elapsed else None
        by_engine[eng] = {
            "total": len(erows),
            "solved": solved,
            "verified": verified,
            "failed": failed,
            "elapsed_median": med,
        }
        for r in erows:
            status_by_engine[eng][str(r.get("solve_status") or "?")] += 1

    summary = {
        "run_id": run_id,
        "tier": args.tier,
        "case_count": len(idx_list),
        "idx_list": idx_list,
        "engines": engines,
        "time_limit": args.tl,
        "proc_timeout": args.proc_timeout,
        "elapsed_total_s": round(time.monotonic() - t0, 1),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "by_engine": by_engine,
        "status_by_engine": {k: dict(v) for k, v in status_by_engine.items()},
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ROOT / "data" / "algorithm-sweep" / "latest.json").write_text(
        json.dumps({"run_id": run_id, "run_dir": str(run_dir)}, indent=2),
        encoding="utf-8",
    )
    write_report_md(run_dir, {**summary, "status_by_engine": status_by_engine})
    print(
        f"扫盘完成 {done} 任务 -> {run_dir}\n"
        f"报告: docs/algorithm-sweep-report.md"
    )
    for eng, s in by_engine.items():
        print(f"  {eng}: solved {s['solved']}/{s['total']} verified {s['verified']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
