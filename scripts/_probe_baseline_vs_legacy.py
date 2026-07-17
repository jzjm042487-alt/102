"""前置③ 朴素 LNS 最小验证 — 探针脚本。

目的：先实测 baseline 在验证样本上的单轮表现 vs 老软件基线四元组，
判断"外环增益"该用哪种最朴素形式验证。纯只读，不改生产代码。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

os.environ["NESTING_ACCEL"] = "cpu"

from app.service import solve_and_verify  # noqa: E402

SAMPLES = REPO / "frontend-next" / "public" / "samples.json"

# 老软件基线四元组（来自 §11.21）
BASELINE = {
    "9f618d9f-5dd5-4a2d-9348-91597d4f0d03": dict(util=0.99656, joints=836, cut=48, weld=64, bars=1142),
    "6a4fecf7-d070-4629-8bcc-a0ffa5c3b091": dict(util=0.99729, joints=546, cut=70, weld=68, bars=546),
    "3040bd13-9108-40bf-9900-c5cdeeec51f0": dict(util=0.99489, joints=765, cut=26, weld=44, bars=5109),
    "350f2dbb-0858-4398-b00f-b2015be43e58": dict(util=0.99850, joints=968, cut=35, weld=64, bars=1105),
}


def _clean_payload(p: dict) -> dict:
    return p


def run(sample_id: str, time_limit: float) -> None:
    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    rec = next(s for s in data["samples"] if s["id"] == sample_id)
    payload = rec["problem"]

    t0 = time.monotonic()
    result = solve_and_verify(payload, time_limit_seconds=time_limit)
    elapsed = time.monotonic() - t0

    g = result.get("groups", [])
    m = g[0].get("metrics", {}) if g else {}
    ver = result.get("verification", {})
    base = BASELINE.get(sample_id, {})

    print(f"\n=== {sample_id}  (time_limit={time_limit}s, elapsed={elapsed:.1f}s) ===")
    print(f"  top status      : {result.get('status')}")
    print(f"  group status    : {m.get('solve_status')}")
    print(f"  verifier passed : {ver.get('passed')}")
    print(f"  {'指标':<14}{'我方baseline':>14}{'老软件':>12}{'达标?':>8}")

    def row(name, ours, theirs, better_low=True):
        if ours is None:
            mark = "-"
        elif better_low:
            mark = "OK" if ours <= theirs else "劣"
        else:
            mark = "OK" if ours >= theirs else "劣"
        ov = f"{ours:.5f}" if isinstance(ours, float) else str(ours)
        tv = f"{theirs:.5f}" if isinstance(theirs, float) else str(theirs)
        print(f"  {name:<14}{ov:>14}{tv:>12}{mark:>8}")

    row("利用率(≥)", m.get("utilization_rate"), base.get("util"), better_low=False)
    row("焊口数(≤)", m.get("welding_joint_quantity"), base.get("joints"))
    row("切法种类(≤)", m.get("cutting_pattern_type_quantity"), base.get("cut"))
    row("拼法种类(≤)", m.get("welding_pattern_type_quantity"), base.get("weld"))


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "6a4fecf7-d070-4629-8bcc-a0ffa5c3b091"
    tl = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    run(sid, tl)
