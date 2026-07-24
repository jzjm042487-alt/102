"""113146 LexMaster Pass A 探针。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_lex_113146.py [--tl 15]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ACCEPT = ROOT / "data" / "acceptance" / "113146"
CASES = (("t91", ACCEPT / "t91.json"), ("cr", ACCEPT / "cr.json"))


def _load_group(path: Path):
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem

    payload = clean_mom_problem(json.loads(path.read_text(encoding="utf-8")))
    g = merge_equivalent_pipes(parse_problem(payload).groups[0])
    return g, payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tl", type=float, default=15.0)
    args = ap.parse_args()

    from scripts.recipe_builder import build
    from scripts.lex_master import solve as lex_solve
    from scripts.solve_v4_production import solve as l0_solve

    rows = []
    for name, path in CASES:
        group, payload = _load_group(path)
        tr = payload.get("Target_Util_Rate", group.target_rate)
        try:
            uf = float(tr)
            if uf > 1.5:
                uf /= 100
        except (TypeError, ValueError):
            uf = 0.95
        uf = max(0.95, uf)

        print(f"\n=== {name} tl={args.tl} ===")
        recipe = build(group, util_floor=uf)
        print(
            f"recipe |Σ_weld|={recipe.meta.get('sigma_weld_n')} "
            f"|Σ_cut|={recipe.meta.get('sigma_cut_n')} "
            f"hard={recipe.meta.get('hard_unreachable')}"
        )

        t0 = time.monotonic()
        sol = lex_solve(group, recipe, util_floor=uf, tl=args.tl, verbose=True)
        dt = time.monotonic() - t0
        if sol is None:
            print(f"lex: FAIL ({dt:.2f}s)")
            row = {"name": name, "lex_ok": False, "lex_s": dt}
        else:
            print(
                f"lex: OK joints={sol['joints']} util={sol['util']:.4f} "
                f"weld={sol['weld_types']} cut={sol['cut_types']} "
                f"seg={sol['seg_types']} path={sol.get('solver_path')} ({dt:.2f}s)"
            )
            row = {
                "name": name,
                "lex_ok": True,
                "lex_s": dt,
                "joints": sol["joints"],
                "util": sol["util"],
                "weld_types": sol["weld_types"],
                "cut_types": sol["cut_types"],
                "seg_types": sol["seg_types"],
                "solver_path": sol.get("solver_path"),
            }

        t1 = time.monotonic()
        l0 = l0_solve(group, tl=args.tl, util_floor=uf, verbose=False)
        d0 = time.monotonic() - t1
        if l0 is None:
            print(f"L0: FAIL ({d0:.2f}s)")
            row["l0_ok"] = False
            row["l0_s"] = d0
        else:
            print(
                f"L0: OK joints={l0.get('joints')} util={l0.get('util')} "
                f"solver={l0.get('solver')} path={l0.get('solver_path')} ({d0:.2f}s)"
            )
            row.update(
                {
                    "l0_ok": True,
                    "l0_s": d0,
                    "l0_joints": l0.get("joints"),
                    "l0_util": l0.get("util"),
                    "l0_solver": l0.get("solver"),
                    "l0_path": l0.get("solver_path"),
                }
            )
        rows.append(row)

    out = ACCEPT / "lex_probe.json"
    out.write_text(json.dumps({"tl": args.tl, "cases": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    ok = all(r.get("lex_ok") for r in rows)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
