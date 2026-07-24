"""跑泛化 weld_then_cut（无老软件写死）。"""
from __future__ import annotations

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


def _load(path: Path):
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem

    payload = clean_mom_problem(json.loads(path.read_text(encoding="utf-8")))
    prob = parse_problem(payload)
    return merge_equivalent_pipes(prob.groups[0]), payload


def main() -> int:
    from scripts.weld_then_cut import solve, build_sigma

    rows = []
    for name in ("t91", "cr"):
        path = ACCEPT / f"{name}.json"
        print(f"\n=== {name} GENERAL weld_then_cut ===", flush=True)
        group, payload = _load(path)
        tr = payload.get("Target_Util_Rate", group.target_rate)
        try:
            uf = float(tr)
            if uf > 1.5:
                uf /= 100
        except (TypeError, ValueError):
            uf = 0.95
        uf = max(0.95, uf)

        sig = build_sigma(group, closure_iters=6, sigma_cap=8000)
        keys = [1875, 2784, 4177, 3352, 4952, 7813, 10115]
        if name == "cr":
            print(
                f"  |sigma|={len(sig)} key segs:",
                {k: k in sig for k in keys},
                flush=True,
            )

        t0 = time.monotonic()
        sol = solve(
            group,
            tl=90.0,
            util_floor=uf,
            closure_iters=6,
            sigma_cap=8000,
            max_pats_per_pipe=5000,
            verbose=True,
        )
        wall = time.monotonic() - t0
        row = {
            "case": name,
            "mode": "general_weld_then_cut",
            "ok": sol is not None,
            "joints": None if sol is None else sol.get("joints"),
            "weld_types": None if sol is None else sol.get("weld_types"),
            "cut_types": None if sol is None else sol.get("cut_types"),
            "util": None if sol is None else round(float(sol.get("util") or 0), 6),
            "elapsed_s": round(wall, 4),
            "meta": None if sol is None else sol.get("meta"),
            "solver": None if sol is None else sol.get("solver"),
            "diagnosis": None if sol is None else sol.get("diagnosis"),
        }
        rows.append(row)
        print(
            f"  RESULT joints={row['joints']} util={row['util']} "
            f"weld_types={row['weld_types']} cut_types={row['cut_types']} "
            f"wall={row['elapsed_s']}s",
            flush=True,
        )
        if sol and sol.get("weld_patterns"):
            # 打印选用的拼法混配（证明非写死）
            mix = {}
            for (i, seq), q in sol["weld_patterns"].items():
                L = group.pipes[i].length
                mix.setdefault(L, []).append((list(seq), q))
            print(f"  mix={mix}", flush=True)

    out = ACCEPT / "weld_then_cut_general.json"
    out.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
