"""用 teacher-inspired construct v0 探 D1，对照 legacy / V4。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_construct_d1.py
  .venv\\Scripts\\python.exe scripts\\probe_construct_d1.py --tl 15 --limit 50
  .venv\\Scripts\\python.exe scripts\\probe_construct_d1.py --workers 4
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

CASES = ROOT / "data" / "legacy-cases"
D1_COMPARE = ROOT / "data" / "acceptance" / "d1-full-compare"
OUT = ROOT / "data" / "acceptance" / "legacy-fingerprints"


def _num(v, as_float: bool = False):
    if v is None:
        return None
    try:
        return float(v) if as_float else int(float(v))
    except (TypeError, ValueError):
        return None


def _verdict_vs_legacy(sol: dict, lg_j, lg_u) -> str:
    if not sol.get("ok"):
        err = str(sol.get("error") or "")
        if "timelimit" in err or "time" in err.lower():
            return "TIMEOUT"
        return "NO_SOLUTION"
    oj = sol.get("joints")
    ou = sol.get("util")
    if lg_u is not None and ou is not None and float(ou) < float(lg_u) - 1e-3:
        return "util劣"
    if lg_j is not None and oj is not None:
        if float(oj) > float(lg_j) + 1e-6:
            return "劣于(焊口)"
        if float(oj) < float(lg_j) - 1e-6:
            return "优于(焊口)"
    return "打平/可接受"


def _solve_one(idx: int, tl: float, v4: dict | None) -> dict:
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
    from scripts.teacher_inspired_construct_v0 import construct_columns, solve_setcover

    case = load_legacy_case(CASES, idx)
    g = merge_equivalent_pipes(
        parse_problem(clean_mom_problem(case["problem"])).groups[0]
    )
    lg = ((case.get("metrics") or {}).get("metrics") or {})
    lg_j = _num(lg.get("welding_joints"))
    lg_u = _num(lg.get("utilization_rate"), as_float=True)

    cons = construct_columns(g)
    sol = solve_setcover(g, cons, tl=tl)
    verdict = _verdict_vs_legacy(sol, lg_j, lg_u)

    v4 = v4 or {}
    vs_v4 = None
    if sol.get("ok") and v4.get("ok") and v4.get("joints") is not None:
        cj = float(sol["joints"])
        vj = float(v4["joints"])
        if cj > vj + 1e-6:
            vs_v4 = "劣于V4焊口"
        elif cj < vj - 1e-6:
            vs_v4 = "优于V4焊口"
        else:
            vs_v4 = "打平V4焊口"
    elif sol.get("ok") and not v4.get("ok"):
        vs_v4 = "V4无解_本有解"
    elif not sol.get("ok") and v4.get("ok"):
        vs_v4 = "本无解_V4有解"

    return {
        "idx": idx,
        "ok": bool(sol.get("ok")),
        "verdict_vs_legacy": verdict,
        "vs_v4": vs_v4,
        "joints": sol.get("joints"),
        "bars": sol.get("bars"),
        "util": sol.get("util"),
        "legacy_joints": lg_j,
        "legacy_util": lg_u,
        "v4_joints": v4.get("joints"),
        "v4_util": v4.get("util"),
        "v4_verdict": v4.get("verdict"),
        "sigma_n": len(cons.get("sigma") or []),
        "n_weld": cons.get("n_weld"),
        "n_cut": cons.get("n_cut"),
        "elapsed_s": sol.get("elapsed_s"),
        "error": sol.get("error"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tl", type=float, default=15.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--source",
        choices=("compare", "pool"),
        default="compare",
        help="compare=d1-full-compare 同批; pool=difficulty_pools D1 success",
    )
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    v4_by_idx: dict[int, dict] = {}
    if args.source == "compare" and (D1_COMPARE / "metrics_table.json").exists():
        rows_v4 = json.loads(
            (D1_COMPARE / "metrics_table.json").read_text(encoding="utf-8")
        )
        idxs = [int(r["idx"]) for r in rows_v4]
        for r in rows_v4:
            ours = r.get("ours") or {}
            v4_by_idx[int(r["idx"])] = {
                "verdict": r.get("verdict"),
                "joints": ours.get("joints") or ours.get("welding_joints"),
                "util": ours.get("util") or ours.get("utilization_rate"),
                "ok": r.get("verdict") != "无解",
            }
    else:
        pools = json.loads((CASES / "difficulty_pools.json").read_text(encoding="utf-8"))
        index = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
        by = {c["idx"]: c for c in index["cases"]}
        idxs = [
            i
            for i in pools["pools"]["D1-入门"]
            if by.get(i, {}).get("legacy_status") == "success"
        ]

    if args.limit > 0:
        idxs = idxs[: args.limit]

    print(
        f"construct-v0 D1 probe n={len(idxs)} tl={args.tl} "
        f"workers={args.workers} source={args.source}",
        flush=True,
    )
    t_wall0 = time.monotonic()
    rows: list[dict] = []
    done = 0

    if args.workers <= 1:
        for idx in idxs:
            row = _solve_one(idx, args.tl, v4_by_idx.get(idx))
            rows.append(row)
            done += 1
            print(
                f"[{done}/{len(idxs)}] idx={idx} {row['verdict_vs_legacy']} "
                f"j={row['joints']}/{row['legacy_joints']} vsV4={row['vs_v4']} "
                f"t={row['elapsed_s']} err={row['error']}",
                flush=True,
            )
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(_solve_one, idx, args.tl, v4_by_idx.get(idx)): idx
                for idx in idxs
            }
            for fut in as_completed(futs):
                row = fut.result()
                rows.append(row)
                done += 1
                print(
                    f"[{done}/{len(idxs)}] idx={row['idx']} {row['verdict_vs_legacy']} "
                    f"j={row['joints']}/{row['legacy_joints']} vsV4={row['vs_v4']} "
                    f"t={row['elapsed_s']} err={row['error']}",
                    flush=True,
                )

    rows.sort(key=lambda r: r["idx"])
    ok_n = sum(1 for r in rows if r["ok"])
    vc = Counter(r["verdict_vs_legacy"] for r in rows)
    v4c = Counter(r["vs_v4"] for r in rows if r["vs_v4"])

    worse = [r for r in rows if r["verdict_vs_legacy"] in ("劣于(焊口)", "util劣")]
    unsolved = [
        r for r in rows if r["verdict_vs_legacy"] in ("NO_SOLUTION", "TIMEOUT")
    ]
    v4_reg = [r for r in rows if r["vs_v4"] in ("劣于V4焊口", "本无解_V4有解")]

    summary = {
        "tier": "D1-入门",
        "source": args.source,
        "n": len(rows),
        "ok": ok_n,
        "fail": len(rows) - ok_n,
        "ok_rate": ok_n / len(rows) if rows else 0.0,
        "tl": args.tl,
        "workers": args.workers,
        "elapsed_wall": round(time.monotonic() - t_wall0, 1),
        "verdict_vs_legacy": dict(vc),
        "vs_v4": dict(v4c),
        "n_worse_than_legacy": len(worse),
        "n_unsolved": len(unsolved),
        "n_v4_regression": len(v4_reg),
        "worse_idxs": [r["idx"] for r in worse][:60],
        "unsolved_idxs": [r["idx"] for r in unsolved][:60],
        "v4_regression_idxs": [r["idx"] for r in v4_reg][:60],
        "rows": rows,
    }
    out_path = OUT / "construct_v0_d1_probe.json"
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== DONE ===", flush=True)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "n",
                    "ok",
                    "fail",
                    "ok_rate",
                    "verdict_vs_legacy",
                    "vs_v4",
                    "n_worse_than_legacy",
                    "n_unsolved",
                    "n_v4_regression",
                    "elapsed_wall",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
