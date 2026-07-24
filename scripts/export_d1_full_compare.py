"""导出某难度档全量：我方/老软件全部指标 + 每案实际输出 JSON。

落盘:
  data/acceptance/{d1|d2|...}-full-compare/
    summary.json          # 汇总
    metrics_table.json    # 全部案例指标对照表
    metrics_table.csv     # 同上 CSV
    cases/{dir}/
      compare.json        # 双边指标 + 判定
      ours_solution.json  # 我方完整求解输出
      legacy_output.json  # 老软件原始输出
      legacy_metrics.json # 老软件指标

用法:
  .venv\\Scripts\\python.exe scripts\\export_d1_full_compare.py
  .venv\\Scripts\\python.exe scripts\\export_d1_full_compare.py --tier D2-常规
  .venv\\Scripts\\python.exe scripts\\export_d1_full_compare.py --tier D2-常规 --tl 15 --limit 0
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts.legacy_corpus_lib import load_legacy_case

CASES = ROOT / "data" / "legacy-cases"
WORKER = ROOT / "scripts" / "_export_tier_case_worker.py"


def _tier_out_dir(tier: str) -> Path:
    m = re.match(r"^(D\d+)", tier)
    slug = (m.group(1) if m else tier).lower()
    return ROOT / "data" / "acceptance" / f"{slug}-full-compare"


def _num(v, as_float: bool = False):
    if v is None:
        return None
    try:
        return float(v) if as_float else int(float(v))
    except (TypeError, ValueError):
        return None


def _legacy_side(entry: dict, case: dict) -> dict:
    m = (case.get("metrics") or {}).get("metrics") or {}
    return {
        "joints": _num(m.get("welding_joints", entry.get("welding_joints"))),
        "weld_types": _num(m.get("weld_pattern_types", entry.get("weld_pattern_types"))),
        "cut_types": _num(m.get("cut_pattern_types", entry.get("cut_pattern_types"))),
        "seg_types": None,  # 老软件落盘未单独记段种类
        "util": _num(m.get("utilization_rate", entry.get("utilization_rate")), True),
        "used_len": None,
        "pipe_types": _num(m.get("pipe_types", entry.get("pipe_types"))),
        "pipe_pieces": _num(m.get("pipe_pieces", entry.get("pipe_pieces"))),
        "stock_types": _num(m.get("stock_types", entry.get("stock_types"))),
        "stock_pieces": _num(m.get("stock_pieces", entry.get("stock_pieces"))),
        "legacy_status": m.get("legacy_status") or entry.get("legacy_status"),
        "geom_class": m.get("geom_class") or entry.get("geom_class"),
    }


def _ours_side(sol: dict | None, demand_len: float) -> dict | None:
    if sol is None:
        return None
    used = float(sol.get("used_len") or 0)
    util = float(sol.get("util") or (demand_len / used if used else 0))
    return {
        "joints": int(sol.get("joints") or 0),
        "weld_types": int(sol.get("weld_types") or 0),
        "cut_types": int(sol.get("cut_types") or 0),
        "seg_types": int(sol.get("seg_types") or 0) if sol.get("seg_types") is not None else None,
        "util": util,
        "used_len": used,
        "solver": sol.get("solver"),
        "solver_path": sol.get("solver_path"),
        "elapsed_s": sol.get("elapsed_s"),
        "diagnosis": sol.get("diagnosis"),
    }


def _verdict_spec(ours: dict | None, lg: dict) -> str:
    """按 SPEC 词典序：焊口→拼法→切法→(段略)→最后 util 下限。"""
    if ours is None:
        return "无解"
    for name, a, b in (
        ("焊口", ours.get("joints"), lg.get("joints")),
        ("拼法", ours.get("weld_types"), lg.get("weld_types")),
        ("切法", ours.get("cut_types"), lg.get("cut_types")),
    ):
        if b is None or a is None:
            continue
        if a < b:
            return f"优于({name})"
        if a > b:
            return f"劣于({name})"
    lu, ou = lg.get("util"), ours.get("util")
    if lu is not None and ou is not None and float(ou) < float(lu) - 1e-3:
        return "util劣"
    return "打平"


def _run_case_worker(idx: int, tl: float, util_floor: float, proc_timeout: float) -> dict:
    cmd = [
        sys.executable,
        str(WORKER),
        "--idx",
        str(idx),
        "--tl",
        str(tl),
        "--util-floor",
        str(util_floor),
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=proc_timeout,
        )
        dt = time.monotonic() - t0
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"rc={proc.returncode}",
                "elapsed_s": round(dt, 4),
                "solution": None,
                "stderr": (proc.stderr or "")[-500:],
            }
        line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        if not line:
            return {
                "ok": False,
                "error": "EMPTY_STDOUT",
                "elapsed_s": round(dt, 4),
                "solution": None,
                "stderr": (proc.stderr or "")[-500:],
            }
        data = json.loads(line)
        data["elapsed_s"] = round(float(data.get("elapsed_s") or dt), 4)
        return data
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"PROC_TIMEOUT>{proc_timeout}s",
            "elapsed_s": round(time.monotonic() - t0, 4),
            "solution": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.monotonic() - t0, 4),
            "solution": None,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tl", type=float, default=15.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tier", default="D1-入门", help="难度档，如 D1-入门 / D2-常规")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument(
        "--proc-timeout",
        type=float,
        default=0.0,
        help="单案子进程硬超时秒；默认 tl+45",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="不清空已有 cases，跳过已有 compare.json 的案例",
    )
    args = ap.parse_args()
    proc_timeout = args.proc_timeout or (args.tl + 45.0)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    out_root = _tier_out_dir(args.tier)
    pools = json.loads((CASES / "difficulty_pools.json").read_text(encoding="utf-8"))
    index = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
    by = {c["idx"]: c for c in index["cases"]}
    if args.tier not in pools["pools"]:
        raise SystemExit(f"unknown tier {args.tier!r}; known={list(pools['pools'])}")
    selected = [
        i
        for i in pools["pools"][args.tier]
        if by[i].get("legacy_status") == "success"
    ]
    selected = selected[args.offset :]
    if args.limit > 0:
        selected = selected[: args.limit]

    if out_root.exists() and not args.resume:
        import shutil

        if (out_root / "cases").exists():
            shutil.rmtree(out_root / "cases")
    (out_root / "cases").mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    t_all = time.monotonic()
    print(
        f"{args.tier} success export n={len(selected)} tl={args.tl} "
        f"proc_timeout={proc_timeout} resume={args.resume} -> {out_root}",
        flush=True,
    )

    for n, idx in enumerate(selected, 1):
        entry = by[idx]
        case_name = entry.get("dir") or f"{idx:04d}"
        case_dir = out_root / "cases" / case_name
        compare_path = case_dir / "compare.json"
        if args.resume and compare_path.exists():
            try:
                rows.append(json.loads(compare_path.read_text(encoding="utf-8")))
                print(f"[{n}/{len(selected)}] idx={idx} SKIP resume", flush=True)
                continue
            except Exception:
                pass

        case = load_legacy_case(CASES, idx)
        lg = _legacy_side(entry, case)
        case_dir.mkdir(parents=True, exist_ok=True)

        legacy_out = case.get("legacy_result")
        (case_dir / "legacy_output.json").write_text(
            json.dumps(legacy_out, ensure_ascii=False, indent=2)
            if legacy_out is not None
            else "null",
            encoding="utf-8",
        )
        (case_dir / "legacy_metrics.json").write_text(
            json.dumps(lg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        uf = max(0.95, float(lg["util"]) - 1e-3) if lg.get("util") is not None else 0.95
        result = _run_case_worker(idx, args.tl, uf, proc_timeout)
        ok = bool(result.get("ok"))
        err = result.get("error")
        dt = float(result.get("elapsed_s") or 0)
        ours_sol = result.get("solution") if ok else None

        ours_metrics = _ours_side(
            ours_sol, float((ours_sol or {}).get("demand_len") or 0)
        )
        if ok:
            verdict = _verdict_spec(ours_metrics, lg)
        elif err and str(err).startswith("PROC_TIMEOUT"):
            verdict = "TIMEOUT"
        elif err:
            verdict = f"ERROR:{err}"
        else:
            verdict = "无解"

        (case_dir / "ours_solution.json").write_text(
            json.dumps(ours_sol, ensure_ascii=False, indent=2)
            if ours_sol is not None
            else json.dumps({"ok": False, "error": err}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        compare = {
            "idx": idx,
            "dir": case_name,
            "material": entry.get("material"),
            "problem_no": entry.get("problem_no"),
            "verdict": verdict,
            "elapsed_s": round(dt, 4),
            "ours": ours_metrics,
            "legacy": lg,
            "files": {
                "ours_solution": "ours_solution.json",
                "legacy_output": "legacy_output.json",
                "legacy_metrics": "legacy_metrics.json",
            },
        }
        compare_path.write_text(
            json.dumps(compare, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rows.append(compare)

        o = ours_metrics or {}
        print(
            f"[{n}/{len(selected)}] idx={idx} {verdict} "
            f"j={o.get('joints')}/{lg.get('joints')} "
            f"w={o.get('weld_types')}/{lg.get('weld_types')} "
            f"c={o.get('cut_types')}/{lg.get('cut_types')} "
            f"u={None if o.get('util') is None else round(o['util'],4)}/"
            f"{None if lg.get('util') is None else round(lg['util'],4)} "
            f"t={dt:.1f}s",
            flush=True,
        )

    # metrics table
    (out_root / "metrics_table.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    csv_path = out_root / "metrics_table.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "idx",
                "dir",
                "material",
                "verdict",
                "ours_joints",
                "legacy_joints",
                "ours_weld_types",
                "legacy_weld_types",
                "ours_cut_types",
                "legacy_cut_types",
                "ours_seg_types",
                "ours_util",
                "legacy_util",
                "ours_used_len",
                "ours_solver",
                "elapsed_s",
                "pipe_types",
                "pipe_pieces",
                "case_dir",
            ]
        )
        for r in rows:
            o, lg = r.get("ours") or {}, r.get("legacy") or {}
            w.writerow(
                [
                    r["idx"],
                    r.get("dir"),
                    r.get("material"),
                    r.get("verdict"),
                    o.get("joints"),
                    lg.get("joints"),
                    o.get("weld_types"),
                    lg.get("weld_types"),
                    o.get("cut_types"),
                    lg.get("cut_types"),
                    o.get("seg_types"),
                    o.get("util"),
                    lg.get("util"),
                    o.get("used_len"),
                    o.get("solver"),
                    r.get("elapsed_s"),
                    lg.get("pipe_types"),
                    lg.get("pipe_pieces"),
                    f"cases/{r.get('dir')}",
                ]
            )

    from collections import Counter

    summary = {
        "tier": args.tier,
        "n": len(rows),
        "tl": args.tl,
        "proc_timeout": proc_timeout,
        "resume": args.resume,
        "elapsed_wall": round(time.monotonic() - t_all, 1),
        "verdict_counts": dict(Counter(r["verdict"] for r in rows)),
        "out_root": str(out_root),
        "note": (
            "判定按 SPEC：焊口→拼法→切法，最后 util≥legacy-1e-3；"
            "每案 cases/<dir>/ 含 ours_solution.json 与 legacy_output.json；"
            "单案用子进程硬超时防止卡死"
        ),
    }
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== DONE ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
