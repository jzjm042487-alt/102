"""113146 Recipe 探针（开工 V0/V1）。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_recipe_113146.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ACCEPT = ROOT / "data" / "acceptance" / "113146"
CASES = (
    ("t91", ACCEPT / "t91.json"),
    ("cr", ACCEPT / "cr.json"),
)


def _load_group(path: Path):
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem

    payload = clean_mom_problem(json.loads(path.read_text(encoding="utf-8")))
    prob = parse_problem(payload)
    if len(prob.groups) != 1:
        raise ValueError(f"{path.name}: groups={len(prob.groups)}")
    return merge_equivalent_pipes(prob.groups[0]), payload


def probe_one(name: str, path: Path) -> dict:
    from scripts.recipe_builder import build

    group, payload = _load_group(path)
    tr = payload.get("Target_Util_Rate", group.target_rate)
    try:
        uf = float(tr)
        if uf > 1.5:
            uf /= 100
    except (TypeError, ValueError):
        uf = 0.95
    uf = max(0.95, uf)

    recipe = build(group, util_floor=uf)
    counts = recipe.meta.get("pipe_pattern_counts") or {}
    unreachable = recipe.meta.get("unreachable_pipes") or []
    ok = (not recipe.meta.get("hard_unreachable")) and all(
        int(counts.get(str(i), 0)) >= 1 for i in range(len(group.pipes))
    )
    summary = {
        "name": name,
        "file": path.name,
        "material": group.material,
        "pipe_types": len(group.pipes),
        "pipe_lengths": [p.length for p in group.pipes],
        "stock": sorted({s.length: s.quantity for s in group.stocks}.items()),
        "sigma_weld_n": recipe.meta.get("sigma_weld_n"),
        "sigma_cut_n": recipe.meta.get("sigma_cut_n"),
        "sigma_cap": recipe.meta.get("sigma_cap"),
        "sigma_weld": recipe.sigma_weld,
        "sigma_cut": recipe.sigma_cut,
        "pipe_pattern_counts": counts,
        "hard_unreachable": recipe.meta.get("hard_unreachable"),
        "unreachable_pipes": unreachable,
        "diagnosis": recipe.meta.get("diagnosis"),
        "by_tag": recipe.meta.get("by_tag"),
        "v0_v1_pass": ok,
    }
    return summary


def main() -> int:
    if not ACCEPT.is_dir():
        print(f"缺少目录: {ACCEPT}")
        return 2
    results = []
    all_ok = True
    for name, path in CASES:
        if not path.exists():
            print(f"[FAIL] 缺少文件 {path}")
            all_ok = False
            continue
        print(f"\n=== {name} ({path.name}) ===")
        s = probe_one(name, path)
        results.append(s)
        print(
            f"管型={s['pipe_types']}  lengths={s['pipe_lengths']}\n"
            f"|Σ_weld|={s['sigma_weld_n']}  |Σ_cut|={s['sigma_cut_n']}  "
            f"cap={s['sigma_cap']}  tags={s['by_tag']}"
        )
        print(f"每管拼列数: {s['pipe_pattern_counts']}")
        print(f"hard_unreachable={s['hard_unreachable']}  diagnosis={s['diagnosis']}")
        print(f"V0/V1: {'PASS' if s['v0_v1_pass'] else 'FAIL'}")
        all_ok = all_ok and s["v0_v1_pass"]

    out = ACCEPT / "sigma_probe.json"
    out.write_text(
        json.dumps({"cases": results, "all_pass": all_ok}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n已写入 {out}")
    print("总体:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
