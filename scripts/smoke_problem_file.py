"""对单个问题 JSON 文件跑各引擎冒烟。用法: python scripts/smoke_problem_file.py <path.json> [tl]"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ENGINES = ("baseline", "route3", "rcg", "global", "v4", "arcflow_direct")


def run_file(path: Path, tl: float) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    from scripts.legacy_corpus_lib import clean_mom_problem, extract_input_metrics

    try:
        inp = extract_input_metrics(payload)
        agg = inp["aggregate"]
        print(
            f"输入: 管型{agg['pipe_types']} 根数{agg['pipe_pieces']} "
            f"原料型{agg['stock_types']} 根数{agg['stock_pieces']} "
            f"geom={inp['per_group'][0].get('geom_class')}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"输入解析: {type(exc).__name__}: {exc}")

    payload = clean_mom_problem(payload)
    tr = payload.get("Target_Util_Rate", 0.9925)
    try:
        uf = float(tr)
        if uf > 1.5:
            uf /= 100
        uf = max(0.95, uf)
    except (TypeError, ValueError):
        uf = 0.95

    print(f"文件: {path.name}  tl={tl}  util_floor≈{uf}\n")
    print(f"{'engine':<16} {'status':<28} {'ver':<5} {'joints':<6} {'weld':<5} {'cut':<5} {'util':<8} {'sec':<6}")
    print("-" * 90)

    for engine in ENGINES:
        t0 = time.monotonic()
        try:
            if engine == "arcflow_direct":
                from backend.app.domain import parse_problem
                from scripts._arcflow_v3 import solve_arcflow
                from scripts._exp_colgen import merge_equivalent_pipes

                prob = parse_problem(payload)
                if len(prob.groups) != 1:
                    raise ValueError(f"groups={len(prob.groups)}")
                g = merge_equivalent_pipes(prob.groups[0])
                res = solve_arcflow(g, tl=tl, verbose=False, util_floor=uf)
                sec = time.monotonic() - t0
                if res is None:
                    print(f"{engine:<16} {'UNSOLVED':<28} {'-':<5} {'-':<6} {'-':<5} {'-':<5} {'-':<8} {sec:<6.1f}")
                else:
                    print(
                        f"{engine:<16} {str(res.get('solver','SOLVED')):<28} {'-':<5} "
                        f"{res.get('joints','-'):<6} {res.get('weld_types','-'):<5} "
                        f"{res.get('cut_types','-'):<5} {res.get('util',0):<8.4f} {sec:<6.1f}"
                    )
            else:
                from app.service import solve_and_verify

                result = solve_and_verify(payload, time_limit_seconds=tl, engine=engine)
                sec = time.monotonic() - t0
                groups = result.get("groups") or []
                ver = (result.get("verification") or {}).get("passed")
                if not groups:
                    st = result.get("status", "?")
                    print(f"{engine:<16} {st:<28} {ver!s:<5} {'-':<6} {'-':<5} {'-':<5} {'-':<8} {sec:<6.1f}")
                else:
                    m = groups[0].get("metrics") or {}
                    st = m.get("solve_status") or groups[0].get("status") or "?"
                    print(
                        f"{engine:<16} {str(st)[:28]:<28} {str(ver):<5} "
                        f"{m.get('welding_joint_quantity','-'):<6} "
                        f"{m.get('welding_pattern_type_quantity','-'):<5} "
                        f"{m.get('cutting_pattern_type_quantity','-'):<5} "
                        f"{m.get('utilization_rate',0):<8.4f} {sec:<6.1f}"
                    )
        except Exception as exc:  # noqa: BLE001
            sec = time.monotonic() - t0
            print(f"{engine:<16} ERROR:{type(exc).__name__:<22} {'-':<5} {'-':<6} {'-':<5} {'-':<5} {'-':<8} {sec:<6.1f}  {exc}"[:120])
    print()


def main() -> int:
    path = Path(sys.argv[1])
    tl = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
    if not path.is_file():
        print(f"not found: {path}", file=sys.stderr)
        return 1
    run_file(path, tl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
