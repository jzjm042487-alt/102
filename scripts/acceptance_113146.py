"""113146 生产验收：墙钟 + verifier + 指标门禁。

用法:
  .venv\\Scripts\\python.exe scripts\\acceptance_113146.py --tl 15 --engines v4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCEPT = ROOT / "data" / "acceptance" / "113146"
CASES = (
    ("t91", ACCEPT / "t91.json", 4.0),
    ("cr", ACCEPT / "cr.json", 9.0),
)


def _run_engine(engine: str, path: Path, tl: float, proc_timeout: float) -> dict:
    worker = ROOT / "scripts" / "_acceptance_113146_worker.py"
    cmd = [
        sys.executable, str(worker),
        "--engine", engine, "--path", str(path), "--tl", str(tl),
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=proc_timeout,
        )
        wall = time.monotonic() - t0
        if proc.returncode != 0:
            return {
                "engine": engine, "ok": False,
                "error": f"rc={proc.returncode}",
                "stderr": (proc.stderr or "")[-500:], "wall_s": wall,
            }
        line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        data = json.loads(line)
        data["wall_s"] = wall
        return data
    except subprocess.TimeoutExpired:
        return {
            "engine": engine, "ok": False, "error": "PROC_TIMEOUT",
            "wall_s": time.monotonic() - t0,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "engine": engine, "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "wall_s": time.monotonic() - t0,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tl", type=float, default=15.0)
    ap.add_argument("--engines", type=str, default="v4")
    ap.add_argument("--proc-timeout", type=float, default=0.0)
    ap.add_argument("--max-wall", type=float, default=15.0,
                    help="门禁：单题墙钟上限（秒）")
    args = ap.parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    proc_timeout = args.proc_timeout or (args.tl + 30.0)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    snapshot = {
        "tl": args.tl,
        "engines": engines,
        "max_wall_s": args.max_wall,
        "target_wall_clock_s": {"t91": 4.0, "cr": 9.0},
        "cases": {},
        "gate_pass": True,
    }
    for name, path, target_s in CASES:
        if not path.exists():
            print(f"[SKIP] {name}: missing {path}")
            snapshot["gate_pass"] = False
            continue
        print(f"\n=== {name} target≈{target_s}s gate≤{args.max_wall}s ===")
        rows = []
        for eng in engines:
            row = _run_engine(eng, path, args.tl, proc_timeout)
            rows.append(row)
            ok = bool(row.get("ok")) and row.get("verified") is not False
            wall = float(row.get("wall_s") or 0)
            wall_ok = wall <= args.max_wall + 0.5
            case_pass = ok and wall_ok
            if not case_pass:
                snapshot["gate_pass"] = False
            status = "PASS" if case_pass else "FAIL"
            print(
                f"  {eng:<10} {status:<5} joints={row.get('joints')} "
                f"weld={row.get('weld_types')} cut={row.get('cut_types')} "
                f"util={row.get('util')} wall={wall:.1f}s "
                f"ver={row.get('verified')} path={row.get('solver_path')} "
                f"{row.get('error','')}"
            )
        snapshot["cases"][name] = rows

    out = ACCEPT / "baseline_snapshot.json"
    ACCEPT.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    print("GATE:", "PASS" if snapshot["gate_pass"] else "FAIL")
    return 0 if snapshot["gate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
