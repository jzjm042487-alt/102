"""把 v2 / colgen 的聚合 ILP 解喂给 app.verifier，验证段平衡 / 焊口合法 / 禁焊区。

ILP 只给出聚合数量（每种拼法用几根、每种切法开几根母材），本脚本把它展开成
verifier 需要的 solution schema（welding_patterns / cutting_patterns + metrics +
input_normalizations），复用与 route3 相同的字段口径，然后调 verify_solution。

段平衡由 ILP 的 produced>=consumed 约束保证；这里只做实例化与格式转换。
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
for p in (str(BACKEND), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.domain import MaterialGroup, from_units  # noqa: E402
from app.verifier import verify_solution  # noqa: E402
import _poc_setcover_ilp_v2 as v2  # noqa: E402
import _poc_colgen as cg  # noqa: E402


def _input_norms(payload: dict[str, Any]) -> list[dict[str, Any]]:
    norms: list[dict[str, Any]] = []

    def _add(container: dict, key: str, path: str, rule: str) -> None:
        if key not in container:
            return
        try:
            val = float(container[key])
        except (TypeError, ValueError):
            return
        norm = math.floor(val) if rule == "FLOOR_TO_INTEGER_MM" else math.ceil(val)
        if val != norm:
            norms.append({"path": path, "original": container[key], "normalized": int(norm), "rule": rule})

    for i, pipe in enumerate(payload.get("Pipe", []) or []):
        if isinstance(pipe, dict):
            _add(pipe, "pipe_length", f"Pipe[{i}].pipe_length", "CEILING_TO_INTEGER_MM")
            for j, iv in enumerate(pipe.get("Unweldable_Area", []) or []):
                if isinstance(iv, (list, tuple)) and len(iv) >= 2:
                    syn = {"start": iv[0], "end": iv[1]}
                    _add(syn, "start", f"Pipe[{i}].Unweldable_Area[{j}][0]", "FLOOR_TO_INTEGER_MM")
                    _add(syn, "end", f"Pipe[{i}].Unweldable_Area[{j}][1]", "CEILING_TO_INTEGER_MM")
    for i, stock in enumerate(payload.get("Stock", []) or []):
        if isinstance(stock, dict):
            _add(stock, "stock_length", f"Stock[{i}].stock_length", "CEILING_TO_INTEGER_MM")
    norms.sort(key=lambda r: (r["path"], r["rule"]))
    return norms


def v2_to_solution(group: MaterialGroup, res: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    kerf = group.blade_margin

    # --- welding patterns: each res["welds"] = {"pipe": i, "parts": [...], "count": n} ---
    weld_agg: dict[tuple[str, tuple[int, ...]], int] = defaultdict(int)
    pipe_of: dict[tuple[str, tuple[int, ...]], Any] = {}
    for w in res["welds"]:
        pipe = group.pipes[w["pipe"]]
        parts = tuple(w["parts"])
        key = (pipe.pipe_id, parts)
        weld_agg[key] += w["count"]
        pipe_of[key] = pipe

    welding_patterns: list[dict[str, Any]] = []
    for idx, (key, qty) in enumerate(sorted(weld_agg.items())):
        pipe_id, parts = key
        p = pipe_of[key]
        positions: list[Any] = []
        acc = 0
        for part in parts[:-1]:
            acc += part
            positions.append(from_units(acc))
        welding_patterns.append({
            "pattern_id": f"W{idx}",
            "pipe_id": pipe_id,
            "figure_number": p.figure_number,
            "parent_node": p.parent_node,
            "jlxh": p.jlxh,
            "cube_no": p.cube_no,
            "pipe_length": from_units(p.length),
            "parts": [from_units(x) for x in parts],
            "weld_positions": positions,
            "quantity": qty,
            "joint_count": len(parts) - 1,
        })

    # --- reconcile surplus (produced > consumed) into trim: method-2, option-1 ---
    # The ILP uses produced >= consumed, so some segments are cut but welded into
    # nothing.  Physically that means "cut one fewer piece and leave a longer
    # remnant".  We compute per-segment surplus = produced - consumed, then expand
    # cut columns to per-bar instances and DROP surplus segments (largest columns
    # first so the freed length becomes remnant on the least-dense bars), turning
    # them into trim.  This keeps every remaining cut segment welded => verifier
    # segment balance holds.  (Option-2 "carry remnant to next batch" is a future
    # enhancement, see design doc.)
    produced_cnt: dict[int, int] = defaultdict(int)
    for c in res["cuts"]:
        for seg, nseg in c["counts"].items():
            produced_cnt[seg] += nseg * c["count"]
    consumed_cnt: dict[int, int] = defaultdict(int)
    for w in res["welds"]:
        for seg in w["parts"]:
            consumed_cnt[seg] += w["count"]
    surplus: dict[int, int] = {
        seg: produced_cnt[seg] - consumed_cnt.get(seg, 0)
        for seg in produced_cnt
        if produced_cnt[seg] - consumed_cnt.get(seg, 0) > 0
    }

    # expand cuts to per-bar instances (list of segment lists), one per bar
    bar_instances: list[list[int]] = []
    for c in res["cuts"]:
        segs = []
        for seg, nseg in c["counts"].items():
            segs.extend([seg] * nseg)
        for _ in range(c["count"]):
            bar_instances.append(list(segs))

    # drop surplus segments from bars (prefer bars that contain the surplus seg;
    # remove one occurrence at a time until surplus for that seg is exhausted)
    if surplus:
        by_seg_bars: dict[int, list[int]] = defaultdict(list)
        for bi, segs in enumerate(bar_instances):
            for seg in set(segs):
                if seg in surplus:
                    by_seg_bars[seg].append(bi)
        for seg, need in surplus.items():
            removed = 0
            for bi in by_seg_bars.get(seg, []):
                while removed < need and seg in bar_instances[bi]:
                    bar_instances[bi].remove(seg)
                    removed += 1
                if removed >= need:
                    break

    # re-aggregate bars into cutting patterns by (stock inferred, sorted parts)
    # stock length per bar: recover from the original cut column order.
    stock_of_bar: list[int] = []
    for c in res["cuts"]:
        stock_of_bar.extend([c["stock"]] * c["count"])
    agg: dict[tuple[int, tuple[int, ...]], int] = defaultdict(int)
    for bi, segs in enumerate(bar_instances):
        if not segs:
            continue  # bar fully emptied (shouldn't happen) -> skip
        agg[(stock_of_bar[bi], tuple(sorted(segs, reverse=True)))] += 1

    # --- cutting patterns from reconciled aggregation ---
    cutting_patterns: list[dict[str, Any]] = []
    for idx, ((L, parts_t), qty) in enumerate(sorted(agg.items())):
        parts = list(parts_t)
        cuts = len(parts)
        kerf_loss = kerf * max(0, cuts - 1)
        used = sum(parts) + kerf_loss
        remainder = L - used
        cut_positions: list[Any] = []
        cursor = 0
        for i, part in enumerate(parts):
            cursor += part
            if i < cuts - 1 or remainder > 0:
                cut_positions.append(from_units(cursor))
            if i < cuts - 1:
                cursor += kerf
        cutting_patterns.append({
            "pattern_id": f"C{idx}",
            "stock_length": from_units(L),
            "parts": [from_units(x) for x in parts],
            "cut_positions": cut_positions,
            "quantity": qty,
            "kerf_per_cut": from_units(kerf),
            "kerf_loss_per_stock": from_units(kerf_loss),
            "remainder_per_stock": from_units(max(0, remainder)),
            "used_length_per_stock": from_units(used),
        })

    demand_len = sum(p.length * p.demand for p in group.pipes)
    weld_type_ids = {tuple(w["parts"]) for w in res["welds"]}
    cut_type_ids = set(agg.keys())
    total_welds = sum((len(w["parts"]) - 1) * w["count"] for w in res["welds"])
    used_stock = sum(cp_L * qty for (cp_L, _), qty in agg.items())
    metrics = {
        "utilization_rate": demand_len / used_stock if used_stock else 0.0,
        "welding_joint_quantity": total_welds,
        "welding_pattern_type_quantity": len(weld_type_ids),
        "cutting_pattern_type_quantity": len(cut_type_ids),
        "solve_status": "SETCOVER_V2",
    }
    group_result = {
        "material": group.material,
        "specifications": group.specifications,
        "metrics": metrics,
        "welding_patterns": welding_patterns,
        "cutting_patterns": cutting_patterns,
        "input_normalizations": _input_norms(payload),
    }
    return {"status": "SUCCESS", "groups": [group_result]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-id")
    ap.add_argument("--sample-file")
    ap.add_argument("--blade-margin", type=float, default=None)
    ap.add_argument("--splits", type=int, default=4)
    ap.add_argument("--max-pieces", type=int, default=4)
    ap.add_argument("--seed-cap", type=int, default=30000)
    ap.add_argument("--seed-max-trim", type=int, default=120)
    ap.add_argument("--int-time", type=float, default=180.0)
    args = ap.parse_args(argv)

    group, legacy = v2._load(args)
    payload = v2._load_payload(args) if hasattr(v2, "_load_payload") else _load_payload(args)
    kerf = group.blade_margin
    print(f"Group {group.material}/{group.specifications}: {len(group.pipes)} pipes, "
          f"{sum(s.quantity for s in group.stocks)} bars, kerf={kerf}\n", flush=True)

    weld_cands, alphabet = v2.build_weld_candidates(group, args.splits)
    stock_lengths = [s.length for s in group.stocks]
    cols = v2.enumerate_cut_columns(sorted(alphabet), stock_lengths, kerf,
                                    args.seed_cap, args.seed_max_trim, args.max_pieces)
    cols = v2.ensure_coverage(cols, sorted(alphabet), stock_lengths, kerf, args.max_pieces)
    print(f"pool = {len(cols)} cut columns\n", flush=True)

    res = v2.solve_two_phase(group, weld_cands, cols, args.int_time / 2, args.int_time / 2,
                             slack=0.0)
    if not res["feasible"]:
        print("ILP infeasible / no solution")
        return 1
    print(f"ILP status={res['status']} phase={res.get('phase')} "
          f"cut_types={res['cut_types']} weld_types={res['weld_types']} bars={res['total_bars']}\n", flush=True)

    solution = v2_to_solution(group, res, payload)
    # verifier defaults BladeMargin to 10 when absent; but this dataset (and the
    # legacy algorithm) uses kerf=0.  Pin it explicitly so the verifier recomputes
    # kerf/remainder/used-length with the SAME kerf we built the solution with.
    vpayload = dict(payload)
    vpayload["NestParam"] = {**(payload.get("NestParam") or {}), "BladeMargin": kerf}
    report = verify_solution(vpayload, solution)
    ok = report.get("passed")
    print(f"=== VERIFIER: {'PASS' if ok else 'FAIL'} ===")
    issues = report.get("issues") or []
    errs = [i for i in issues if i.get("severity") == "error"]
    warns = [i for i in issues if i.get("severity") != "error"]
    if errs:
        seen: dict[str, int] = defaultdict(int)
        for e in errs:
            seen[e.get("code", "?")] += 1
        print(f"  {len(errs)} errors by code: {dict(seen)}")
        for e in errs[:20]:
            print("   -", e.get("code"), e.get("message", "")[:120], "@", e.get("path", ""))
    else:
        print("  no errors (segment balance, weld legality, forbidden zones, metrics all pass)")
    if warns:
        print(f"  ({len(warns)} warnings)")
    return 0 if ok else 2


def _load_payload(args) -> dict[str, Any]:
    import json
    if args.sample_file:
        return json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
    samples = json.loads((REPO_ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8"))
    rec = next(s for s in samples["samples"] if s["id"] == args.sample_id)
    return rec["problem"]


if __name__ == "__main__":
    raise SystemExit(main())
