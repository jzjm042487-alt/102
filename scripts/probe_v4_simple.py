"""用新 V4 主路（pure_cut → weld_then_cut → lex）跑简单档并对标老软件。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_v4_simple.py
  .venv\\Scripts\\python.exe scripts\\probe_v4_simple.py --levels 3,4,6,8,15 --tl 30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts.solve_v4_production import solve as v4_solve


def _verdict(ours: dict, lg: dict) -> str:
    if ours is None:
        return "无解"
    oj, ow, oc = ours["joints"], ours["weld_types"], ours["cut_types"]
    ou = ours["util"]
    lj, lw, lc = lg.get("joints"), lg.get("weld_types"), lg.get("cut_types")
    lu = lg.get("util")
    if lu is not None and ou < lu - 1e-3:
        return "util劣"
    # 词典序：焊口 → 拼法 → 切法
    for name, a, b in (
        ("焊口", oj, lj),
        ("拼法", ow, lw),
        ("切法", oc, lc),
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
    ap.add_argument("--levels", default="3,4,6,8,15")
    ap.add_argument("--tl", type=float, default=30.0)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]

    samples = json.loads(
        (ROOT / "scripts/_picked20_full.json").read_text(encoding="utf-8")
    )
    by_lv = {s["level"]: s for s in samples}

    rows = []
    print(
        f"{'档':>4} {'焊口 我/老':>12} {'拼 我/老':>10} {'切 我/老':>10} "
        f"{'util 我/老':>18} {'判定':>10} {'耗时':>6} path"
    )
    print("-" * 110)

    for lv in levels:
        s = by_lv[lv]
        lg = s.get("legacy") or {}
        if not lg or lg.get("joints") is None and lg.get("util") in (None, 0, 0.0):
            print(f"L{lv:<3} skip（无老软件真基准）")
            continue
        g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
        lg_util = lg.get("util")
        uf = max(0.95, float(lg_util) - 1e-3) if lg_util else None
        t0 = time.monotonic()
        sol = v4_solve(g, tl=args.tl, util_floor=uf, verbose=False)
        dt = time.monotonic() - t0
        if sol is None:
            print(f"L{lv:<3} 无解 ({dt:.1f}s)")
            rows.append({"level": lv, "spec": s["spec"], "ours": None, "legacy": lg, "elapsed": round(dt, 2)})
            continue
        ours = {
            "joints": sol.get("joints"),
            "weld_types": sol.get("weld_types"),
            "cut_types": sol.get("cut_types"),
            "util": sol.get("util"),
            "solver_path": sol.get("solver_path"),
        }
        v = _verdict(ours, lg)
        path = "/".join((sol.get("solver_path") or [])[-4:])
        print(
            f"L{lv:<3} {ours['joints']:>4}/{str(lg.get('joints')):<4} "
            f"{ours['weld_types']:>4}/{str(lg.get('weld_types')):<4} "
            f"{ours['cut_types']:>4}/{str(lg.get('cut_types')):<4} "
            f"{ours['util']:.4f}/{float(lg.get('util') or 0):.4f} "
            f"{v:>10} {dt:5.1f}s {path}"
        )
        rows.append(
            {
                "level": lv,
                "spec": s["spec"],
                "ours": ours,
                "legacy": lg,
                "verdict": v,
                "elapsed": round(dt, 2),
            }
        )

    out = ROOT / "data/acceptance/v4_simple_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    # summary
    from collections import Counter
    c = Counter(r.get("verdict", "无解") for r in rows)
    print("汇总:", dict(c))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
