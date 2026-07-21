"""Diagnose global engine failures on smoke samples."""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.domain import parse_problem
from app.global_candidates import build_cut_pool, build_weld_pool
from app.solver_global import UTIL_FLOOR, _max_used_for_util_floor, _solve_joint

ROOT = Path(__file__).resolve().parents[1]
samples = json.loads(
    (ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
)["samples"]
ids = [
    "194ca29c-1ba1-4721-98c6-2af6382a64bb",
    "553497ed-5c82-4e8e-9b90-c9f4c2141505",
    "a6795f7a-7425-4fea-8229-ac91d09dd96c",
]
for sid in ids:
    rec = next(s for s in samples if s["id"] == sid)
    group = parse_problem(rec["problem"]).groups[0]
    print("====", sid, rec.get("com"), "====")
    print(
        "pipes",
        len(group.pipes),
        "demand",
        group.demand_length,
        "stock",
        group.stock_length,
        "ratio",
        round(group.stock_length / group.demand_length, 6),
    )
    print("stocks", [(s.length, s.quantity) for s in group.stocks])
    print("max_joints", sorted({p.max_joints for p in group.pipes}))
    lens = sorted({p.length for p in group.pipes})
    print("pipe_types", len(lens), "lens", lens[:12])
    t0 = time.monotonic()
    try:
        welds, alph = build_weld_pool(group)
        cuts = build_cut_pool(group, alph)
        print(
            "weld_pats",
            sum(len(v) for v in welds.values()),
            "alphabet",
            len(alph),
            "cuts",
            len(cuts),
            "build_s",
            round(time.monotonic() - t0, 2),
        )
        max_used = _max_used_for_util_floor(group.demand_length, UTIL_FLOOR)
        print("max_used_for_95", max_used)
        probe = _solve_joint(group, welds, cuts, 10.0, minimise="length")
        if probe is None:
            print("probe_no_floor", None)
        else:
            print(
                "probe_no_floor",
                probe["used_len"],
                "ok_floor",
                probe["used_len"] <= max_used,
                "util",
                round(group.demand_length / probe["used_len"], 6),
            )
        probe2 = _solve_joint(
            group, welds, cuts, 10.0, minimise="length", max_used_len=max_used
        )
        if probe2 is None:
            print("probe_with_floor", None)
        else:
            print(
                "probe_with_floor",
                probe2["used_len"],
                "util",
                round(group.demand_length / probe2["used_len"], 6),
            )
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)
