"""对新梳理语料 data/legacy-cases 的指定难度档跑 V4 主路并对标老软件。

默认：D1-入门 + legacy_status=success。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_legacy_tier_v4.py
  .venv\\Scripts\\python.exe scripts\\probe_legacy_tier_v4.py --tier D1-入门 --tl 20
  .venv\\Scripts\\python.exe scripts\\probe_legacy_tier_v4.py --tier D1-入门 --limit 50
  .venv\\Scripts\\python.exe scripts\\probe_legacy_tier_v4.py --only-weld   # 仅老软件焊口>0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
from scripts.solve_v4_production import solve as v4_solve

CASES = ROOT / "data" / "legacy-cases"


def _legacy_metrics(entry: dict, case: dict) -> dict:
    """优先 index 摘要；拼/切以 metrics.json 为准。"""
    m = (case.get("metrics") or {}).get("metrics") or case.get("metrics") or {}
    # metrics.json 常见结构: { meta, metrics: {...} }
    inner = m if "welding_joints" in m or "weld_pattern_types" in m else (
        (case.get("metrics") or {}).get("metrics") or {}
    )
    return {
        "joints": _num(inner.get("welding_joints", entry.get("welding_joints"))),
        "weld_types": _num(
            inner.get("weld_pattern_types", entry.get("weld_pattern_types"))
        ),
        "cut_types": _num(
            inner.get("cut_pattern_types", entry.get("cut_pattern_types"))
        ),
        "util": _num(
            inner.get("utilization_rate", entry.get("utilization_rate")),
            as_float=True,
        ),
    }


def _num(v, as_float: bool = False):
    if v is None:
        return None
    try:
        return float(v) if as_float else int(float(v))
    except (TypeError, ValueError):
        return None


def _verdict(ours: dict | None, lg: dict) -> str:
    if ours is None:
        return "无解"
    ou = float(ours["util"] or 0)
    lu = lg.get("util")
    if lu is not None and ou < float(lu) - 1e-3:
        return "util劣"
    for name, a, b in (
        ("焊口", ours["joints"], lg.get("joints")),
        ("拼法", ours["weld_types"], lg.get("weld_types")),
        ("切法", ours["cut_types"], lg.get("cut_types")),
    ):
        if b is None:
            continue
        if a < b:
            return f"优于({name})"
        if a > b:
            return f"劣于({name})"
    return "打平"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="D1-入门")
    ap.add_argument("--tl", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    ap.add_argument("--only-weld", action="store_true", help="仅老软件焊口>0")
    ap.add_argument("--only-pure", action="store_true", help="仅老软件焊口=0")
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    pools = json.loads((CASES / "difficulty_pools.json").read_text(encoding="utf-8"))
    index = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
    by = {c["idx"]: c for c in index["cases"]}
    tier_ids = list(pools["pools"][args.tier])

    selected = []
    for i in tier_ids:
        e = by[i]
        if e.get("legacy_status") != "success":
            continue
        j = _num(e.get("welding_joints")) or 0
        if args.only_weld and j <= 0:
            continue
        if args.only_pure and j > 0:
            continue
        selected.append(i)

    selected = selected[args.offset :]
    if args.limit > 0:
        selected = selected[: args.limit]

    print(
        f"tier={args.tier} selected={len(selected)} "
        f"(from pool={len(tier_ids)}, success-filtered) tl={args.tl}"
    )
    print(
        f"{'idx':>6} {'焊口 我/老':>12} {'拼 我/老':>10} {'切 我/老':>10} "
        f"{'util 我/老':>18} {'判定':>10} {'秒':>6}"
    )
    print("-" * 90)

    rows = []
    t_all = time.monotonic()
    for n, idx in enumerate(selected, 1):
        entry = by[idx]
        try:
            case = load_legacy_case(CASES, idx)
            lg = _legacy_metrics(entry, case)
            problem = clean_mom_problem(case["problem"])
            groups = parse_problem(problem).groups
            if not groups:
                raise RuntimeError("no groups")
            # 单 group 为主；多 group 逐组求和指标（与语料粗口径一致）
            uf = None
            if lg.get("util") is not None:
                uf = max(0.95, float(lg["util"]) - 1e-3)

            ours_acc = {
                "joints": 0,
                "weld_types": 0,
                "cut_types": 0,
                "used_len": 0.0,
                "demand_len": 0.0,
                "paths": [],
            }
            ok = True
            t0 = time.monotonic()
            for g0 in groups:
                g = merge_equivalent_pipes(g0)
                sol = v4_solve(g, tl=args.tl, util_floor=uf, verbose=False)
                if sol is None:
                    ok = False
                    break
                ours_acc["joints"] += int(sol.get("joints") or 0)
                ours_acc["weld_types"] += int(sol.get("weld_types") or 0)
                ours_acc["cut_types"] += int(sol.get("cut_types") or 0)
                ours_acc["used_len"] += float(sol.get("used_len") or 0)
                ours_acc["demand_len"] += float(g.demand_length)
                ours_acc["paths"].append("/".join((sol.get("solver_path") or [])[-3:]))
            dt = time.monotonic() - t0
            if not ok:
                print(f"{idx:6d} 无解 ({dt:.1f}s)")
                rows.append(
                    {
                        "idx": idx,
                        "ours": None,
                        "legacy": lg,
                        "verdict": "无解",
                        "elapsed": round(dt, 2),
                    }
                )
            else:
                util = (
                    ours_acc["demand_len"] / ours_acc["used_len"]
                    if ours_acc["used_len"] > 0
                    else 0.0
                )
                ours = {
                    "joints": ours_acc["joints"],
                    "weld_types": ours_acc["weld_types"],
                    "cut_types": ours_acc["cut_types"],
                    "util": util,
                    "path": ours_acc["paths"][0] if ours_acc["paths"] else "",
                }
                v = _verdict(ours, lg)
                print(
                    f"{idx:6d} {ours['joints']:>4}/{str(lg.get('joints')):<4} "
                    f"{ours['weld_types']:>4}/{str(lg.get('weld_types')):<4} "
                    f"{ours['cut_types']:>4}/{str(lg.get('cut_types')):<4} "
                    f"{ours['util']:.4f}/{float(lg.get('util') or 0):.4f} "
                    f"{v:>10} {dt:5.1f}s"
                )
                rows.append(
                    {
                        "idx": idx,
                        "ours": ours,
                        "legacy": lg,
                        "verdict": v,
                        "elapsed": round(dt, 2),
                        "spec": entry.get("material"),
                        "pipe_pieces": entry.get("pipe_pieces"),
                    }
                )
        except Exception as e:
            print(f"{idx:6d} ERROR {type(e).__name__}: {e}")
            rows.append({"idx": idx, "verdict": f"ERROR:{type(e).__name__}", "error": str(e)})

        if n % 20 == 0:
            c = Counter(r.get("verdict", "?") for r in rows)
            print(f"  ... {n}/{len(selected)} partial {dict(c)} wall={time.monotonic()-t_all:.0f}s")

    out = ROOT / "data" / "acceptance" / f"v4_tier_{args.tier.replace('-', '_')}.json"
    # sanitize filename
    safe = args.tier.encode("ascii", "ignore").decode() or "tier"
    out = ROOT / "data" / "acceptance" / f"v4_legacy_{safe or 'D1'}_probe.json"
    if "入门" in args.tier:
        out = ROOT / "data" / "acceptance" / "v4_legacy_D1_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = dict(Counter(r.get("verdict", "?") for r in rows))
    payload = {
        "tier": args.tier,
        "n": len(rows),
        "tl": args.tl,
        "summary": summary,
        "elapsed_wall": round(time.monotonic() - t_all, 1),
        "rows": rows,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== SUMMARY {args.tier} n={len(rows)} ===")
    for k, v in sorted(summary.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print(f"Wrote {out} wall={payload['elapsed_wall']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
