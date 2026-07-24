"""从老软件完整解提取算法指纹 + 教师列（只读，不改求解器）。

子集:
  d2_fail_worse     — D2 无解/超时/焊口劣于（旧默认，写入 legacy-fingerprints/）
  tier_success      — 指定 --tier 的 success 案
  all_tiers_success — D1–D5 全部 success（默认写入 legacy-fingerprints-all/）

用法:
  .venv\\Scripts\\python.exe scripts\\extract_legacy_fingerprint.py --subset all_tiers_success
  .venv\\Scripts\\python.exe scripts\\extract_legacy_fingerprint.py --tier D1-入门 --subset tier_success
  .venv\\Scripts\\python.exe scripts\\extract_legacy_fingerprint.py --probe-teacher 8
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts.legacy_corpus_lib import _parts_from_field, load_legacy_case

CASES = ROOT / "data" / "legacy-cases"
OUT = ROOT / "data" / "acceptance" / "legacy-fingerprints"
D2_COMPARE = ROOT / "data" / "acceptance" / "d2-full-compare"


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x: Any, default: int = 0) -> int:
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return default


def _result_inner(legacy: dict) -> tuple[dict, dict]:
    gi = legacy.get("GeneralInfo") or {}
    inner = legacy.get("Result") or legacy
    return gi, inner if isinstance(inner, dict) else {}


def extract_teacher_and_fingerprint(legacy: dict) -> dict[str, Any]:
    gi, inner = _result_inner(legacy)
    cut_rows = (inner.get("CuttingPattern") or {}).get("CuttingPipe") or []
    weld_rows = (inner.get("WeldingPattern") or {}).get("WeldingPipe") or []

    cuts: list[dict] = []
    cut_reuse: list[int] = []
    seg_count_hist: Counter[int] = Counter()
    trims: list[float] = []
    near_fill_bars = 0
    exact_fill_bars = 0
    two_seg_bars = 0
    cut_seg_multiset: Counter[int] = Counter()

    for cp in cut_rows:
        if not isinstance(cp, dict):
            continue
        stock_len = _i(cp.get("Length"))
        parts = _parts_from_field(cp.get("Part"))
        if not parts or stock_len <= 0:
            continue
        n = max(1, _i(cp.get("Number"), 1))
        fill = sum(parts)
        trim = _f(cp.get("TrimLoss"), stock_len - fill)
        cuts.append(
            {
                "stock_len": stock_len,
                "segs": list(parts),
                "segs_sorted": list(sorted(parts)),
                "number": n,
                "trim": trim,
                "fill": fill,
            }
        )
        cut_reuse.append(n)
        seg_count_hist[len(parts)] += 1  # by type
        for _ in range(n):
            trims.append(trim)
            for s in parts:
                cut_seg_multiset[s] += 1
            if len(parts) == 2:
                two_seg_bars += 1
            if abs(fill - stock_len) < 1:
                exact_fill_bars += 1
            elif abs(fill - stock_len) <= 50:
                near_fill_bars += 1

    # weld patterns keyed by pipe identity
    weld_by_pipe: dict[str, list[dict]] = defaultdict(list)
    weld_seg_multiset: Counter[int] = Counter()
    joint_hist: Counter[int] = Counter()
    segn_hist: Counter[int] = Counter()
    weld_templates: list[dict] = []

    for wp in weld_rows:
        if not isinstance(wp, dict):
            continue
        fig = str(wp.get("FigureNumber") or "")
        jlxh = str(wp.get("jlxh") or "")
        length = _i(wp.get("Length") or wp.get("PipeLength"))
        pipe_key = f"{fig}|{jlxh}|{length}"
        patterns = wp.get("Pattern") or []
        if isinstance(patterns, dict):
            patterns = [patterns]
        if not patterns:
            # some exports put Part on WeldingPipe itself
            parts = _parts_from_field(wp.get("Part"))
            if parts:
                patterns = [{"Part": wp.get("Part"), "Number": wp.get("Number", 1)}]
        for pat in patterns:
            if not isinstance(pat, dict):
                continue
            parts = _parts_from_field(pat.get("Part"))
            if not parts:
                continue
            qty = max(1, _i(pat.get("Number"), 1))
            item = {"segs": list(parts), "number": qty, "joints": max(0, len(parts) - 1)}
            weld_by_pipe[pipe_key].append(item)
            weld_templates.append(
                {"pipe_key": pipe_key, "figure": fig, "jlxh": jlxh, "pipe_len": length, **item}
            )
            joint_hist[item["joints"]] += qty
            segn_hist[len(parts)] += qty
            for s in parts:
                weld_seg_multiset[s] += qty

    multi_pattern_pipes = sum(
        1
        for items in weld_by_pipe.values()
        if len({tuple(it["segs"]) for it in items}) > 1
    )
    total_bars = sum(cut_reuse) if cut_reuse else 0
    cut_types = len(cuts)
    total_trim = sum(trims)
    zero_trim = sum(1 for t in trims if t < 1)
    trims_sorted = sorted(trims, reverse=True)
    k10 = max(1, len(trims_sorted) // 10) if trims_sorted else 1
    top10_share = (
        sum(trims_sorted[:k10]) / total_trim if total_trim > 0 else 0.0
    )

    sigma = sorted(set(weld_seg_multiset) | set(cut_seg_multiset))

    # complementary pairs among 2-seg cuts: a+b ≈ stock
    complements = []
    for c in cuts:
        if len(c["segs"]) == 2:
            a, b = c["segs"][0], c["segs"][1]
            complements.append(
                {
                    "stock_len": c["stock_len"],
                    "a": a,
                    "b": b,
                    "number": c["number"],
                    "slack": c["stock_len"] - (a + b),
                }
            )

    fingerprint = {
        "util": _f(gi.get("UtilRate"), None) if gi.get("UtilRate") is not None else None,
        "joints_gi": _i(gi.get("WeldingJointQuantity"), None)
        if gi.get("WeldingJointQuantity") is not None
        else None,
        "cut_types": cut_types,
        "cut_bars": total_bars,
        "cut_reuse_avg": (total_bars / cut_types) if cut_types else 0.0,
        "cut_to_bar_ratio": (cut_types / total_bars) if total_bars else None,
        "cut_reuse_min_med_max": (
            [min(cut_reuse), int(statistics.median(cut_reuse)), max(cut_reuse)]
            if cut_reuse
            else None
        ),
        "cut_seg_count_types": dict(sorted(seg_count_hist.items())),
        "trim_total": total_trim,
        "trim_zero_bar_frac": (zero_trim / total_bars) if total_bars else 0.0,
        "trim_top10_share": top10_share,
        "two_seg_bars": two_seg_bars,
        "exact_fill_bars": exact_fill_bars,
        "near_fill_bars_le50": near_fill_bars,
        "pipe_groups": len(weld_by_pipe),
        "multi_pattern_pipes": multi_pattern_pipes,
        "multi_pattern_frac": (
            multi_pattern_pipes / len(weld_by_pipe) if weld_by_pipe else 0.0
        ),
        "joint_hist": dict(sorted(joint_hist.items())),
        "weld_segn_hist": dict(sorted(segn_hist.items())),
        "sigma_n": len(sigma),
        "sigma": sigma,
        "weld_seg_top": weld_seg_multiset.most_common(20),
        "complement_pairs": complements[:30],
        "complement_pair_types": len(complements),
    }

    teacher = {
        "cut_columns": [
            {
                "stock_len": c["stock_len"],
                "segs": c["segs_sorted"],
                "number": c["number"],
            }
            for c in cuts
        ],
        "weld_columns": weld_templates,
        "sigma": sigma,
        "demand_by_pipe": {
            k: sum(it["number"] for it in v) for k, v in weld_by_pipe.items()
        },
    }
    return {"fingerprint": fingerprint, "teacher": teacher}


def select_indices(args: argparse.Namespace) -> list[tuple[int, str, str]]:
    """Return list of (idx, tag, tier)."""
    pools = json.loads((CASES / "difficulty_pools.json").read_text(encoding="utf-8"))
    index = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
    by = {c["idx"]: c for c in index["cases"]}

    if args.subset == "d2_fail_worse":
        rows = json.loads((D2_COMPARE / "metrics_table.json").read_text(encoding="utf-8"))
        out = []
        for r in rows:
            v = str(r.get("verdict") or "")
            if v == "ERROR:NO_SOLUTION" or v == "TIMEOUT" or v.startswith("劣于(焊口)"):
                out.append((int(r["idx"]), v, "D2-常规"))
        return out

    if args.subset == "tier_success":
        tier = args.tier
        ids = [
            i
            for i in pools["pools"][tier]
            if by[i].get("legacy_status") == "success"
        ]
        return [(i, "success", tier) for i in ids]

    if args.subset == "all_tiers_success":
        out: list[tuple[int, str, str]] = []
        seen: set[int] = set()
        for tier in pools.get("tier_labels") or list(pools["pools"].keys()):
            for i in pools["pools"][tier]:
                if i in seen:
                    continue
                if by.get(i, {}).get("legacy_status") != "success":
                    continue
                seen.add(i)
                out.append((i, "success", tier))
        return out

    raise SystemExit(f"unknown subset {args.subset}")


def probe_teacher(idx: int, tl: float = 20.0) -> dict[str, Any]:
    """灌教师列进简化集合覆盖：段需求由焊列导出，切列满足 produced>=consumed。"""
    from pyscipopt import Model, quicksum

    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem

    case = load_legacy_case(CASES, idx)
    legacy = case.get("legacy_result")
    if not isinstance(legacy, dict):
        return {"ok": False, "error": "no_legacy"}
    packed = extract_teacher_and_fingerprint(legacy)
    teacher = packed["teacher"]
    fp = packed["fingerprint"]

    # segment demand from weld columns
    demand: Counter[int] = Counter()
    for w in teacher["weld_columns"]:
        for s in w["segs"]:
            demand[s] += int(w["number"])

    # stock qty from problem
    g = merge_equivalent_pipes(
        parse_problem(clean_mom_problem(case["problem"])).groups[0]
    )
    stock_qty: dict[int, int] = defaultdict(int)
    for st in g.stocks:
        stock_qty[int(st.length)] += int(st.quantity)

    cuts = teacher["cut_columns"]
    if not cuts or not demand:
        return {"ok": False, "error": "empty_teacher", "joints_gi": fp.get("joints_gi")}

    t0 = time.monotonic()
    m = Model(f"teacher_{idx}")
    m.hideOutput()
    m.setParam("limits/time", tl)
    y = []
    for k, c in enumerate(cuts):
        y.append(m.addVar(vtype="I", lb=0, ub=stock_qty.get(c["stock_len"], 0), name=f"y{k}"))
    # per stock length total
    by_L: dict[int, list[int]] = defaultdict(list)
    for k, c in enumerate(cuts):
        by_L[c["stock_len"]].append(k)
    for L, ks in by_L.items():
        m.addCons(quicksum(y[k] for k in ks) <= stock_qty.get(L, 0))

    for seg, need in demand.items():
        prod = []
        for k, c in enumerate(cuts):
            cnt = c["segs"].count(seg)
            if cnt:
                prod.append(cnt * y[k])
        if not prod:
            return {
                "ok": False,
                "error": f"seg_{seg}_not_in_cuts",
                "elapsed_s": round(time.monotonic() - t0, 3),
            }
        m.addCons(quicksum(prod) >= need)

    # minimize bars (feasibility emphasis)
    m.setObjective(quicksum(y), "minimize")
    m.optimize()
    dt = time.monotonic() - t0
    if m.getNSols() == 0:
        return {
            "ok": False,
            "error": f"status={m.getStatus()}",
            "elapsed_s": round(dt, 3),
            "joints_gi": fp.get("joints_gi"),
            "sigma_n": fp.get("sigma_n"),
            "cut_types": fp.get("cut_types"),
        }
    bars = sum(m.getVal(v) for v in y)
    return {
        "ok": True,
        "bars": bars,
        "elapsed_s": round(dt, 3),
        "joints_gi": fp.get("joints_gi"),
        "util": fp.get("util"),
        "sigma_n": fp.get("sigma_n"),
        "cut_types": fp.get("cut_types"),
        "multi_pattern_frac": fp.get("multi_pattern_frac"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="D2-常规")
    ap.add_argument(
        "--subset",
        default="d2_fail_worse",
        choices=("d2_fail_worse", "tier_success", "all_tiers_success"),
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="输出目录；all_tiers_success 默认写 legacy-fingerprints-all",
    )
    ap.add_argument(
        "--probe-teacher",
        type=int,
        default=0,
        help="对无解案做教师列可行性探针的数量（0=跳过）",
    )
    ap.add_argument("--probe-tl", type=float, default=15.0)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    out_root = Path(args.out_dir) if args.out_dir else OUT
    if args.subset == "all_tiers_success" and not args.out_dir:
        out_root = ROOT / "data" / "acceptance" / "legacy-fingerprints-all"

    selected = select_indices(args)
    if args.limit > 0:
        selected = selected[: args.limit]

    if out_root.exists():
        import shutil

        if (out_root / "cases").exists():
            shutil.rmtree(out_root / "cases")
    (out_root / "cases").mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    t_all = time.monotonic()
    print(
        f"fingerprint subset={args.subset} n={len(selected)} -> {out_root}",
        flush=True,
    )

    jsonl = (out_root / "fingerprints.jsonl").open("w", encoding="utf-8")
    index = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
    by = {c["idx"]: c for c in index["cases"]}

    for n, item in enumerate(selected, 1):
        idx, tag, tier = item
        entry = by[idx]
        case_name = entry.get("dir") or f"{idx:04d}"
        case = load_legacy_case(CASES, idx)
        legacy = case.get("legacy_result")
        if not isinstance(legacy, dict):
            print(f"[{n}/{len(selected)}] idx={idx} SKIP no legacy", flush=True)
            continue
        packed = extract_teacher_and_fingerprint(legacy)
        fp = packed["fingerprint"]
        teacher = packed["teacher"]
        case_dir = out_root / "cases" / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        geom = entry.get("geom_class") or (
            (case.get("metrics") or {}).get("metrics") or {}
        ).get("geom_class")
        doc = {
            "idx": idx,
            "dir": case_name,
            "tier": tier,
            "tag": tag,
            "material": entry.get("material"),
            "geom_class": geom,
            "fingerprint": fp,
        }
        (case_dir / "fingerprint.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (case_dir / "teacher_columns.json").write_text(
            json.dumps(teacher, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        row = {
            "idx": idx,
            "dir": case_name,
            "tier": tier,
            "tag": tag,
            "geom_class": geom,
            "multi_pattern_frac": fp["multi_pattern_frac"],
            "sigma_n": fp["sigma_n"],
            "cut_types": fp["cut_types"],
            "cut_to_bar_ratio": fp["cut_to_bar_ratio"],
            "near_fill_bars_le50": fp["near_fill_bars_le50"],
            "two_seg_bars": fp["two_seg_bars"],
            "joints_gi": fp["joints_gi"],
            "util": fp["util"],
            "weld_segn_hist": fp["weld_segn_hist"],
            "complement_pair_types": fp["complement_pair_types"],
            "zero_joint": int(fp.get("joints_gi") or 0) == 0,
        }
        rows.append(row)
        jsonl.write(json.dumps(row, ensure_ascii=False) + "\n")
        if n % 100 == 0 or n == len(selected):
            print(
                f"[{n}/{len(selected)}] idx={idx} tier={tier} "
                f"sigma={fp['sigma_n']} multi={fp['multi_pattern_frac']:.2f} "
                f"cuts={fp['cut_types']} j={fp['joints_gi']}",
                flush=True,
            )
    jsonl.close()

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None

    def agg_block(rs: list[dict]) -> dict:
        if not rs:
            return {"n": 0}
        zero = sum(1 for r in rs if r.get("zero_joint"))
        return {
            "n": len(rs),
            "zero_joint_frac": zero / len(rs),
            "multi_pattern_frac_mean": mean(r["multi_pattern_frac"] for r in rs),
            "sigma_n_mean": mean(r["sigma_n"] for r in rs),
            "cut_types_mean": mean(r["cut_types"] for r in rs),
            "cut_to_bar_ratio_mean": mean(r["cut_to_bar_ratio"] for r in rs),
            "complement_pair_types_mean": mean(
                r["complement_pair_types"] for r in rs
            ),
            "util_mean": mean(r["util"] for r in rs),
            "joints_mean": mean(r["joints_gi"] for r in rs),
            "geom": dict(Counter(str(r.get("geom_class") or "?") for r in rs)),
        }

    fail_rows = [
        r
        for r in rows
        if str(r["tag"]).startswith("ERROR") or r["tag"] == "TIMEOUT"
    ]
    worse_rows = [r for r in rows if str(r["tag"]).startswith("劣于")]
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_tier[str(r.get("tier") or "?")].append(r)

    summary = {
        "subset": args.subset,
        "n": len(rows),
        "elapsed_wall": round(time.monotonic() - t_all, 1),
        "out_root": str(out_root),
        "agg_all": agg_block(rows),
        "agg_by_tier": {t: agg_block(rs) for t, rs in sorted(by_tier.items())},
        "agg_fail": {
            "n": len(fail_rows),
            "multi_pattern_frac_mean": mean(r["multi_pattern_frac"] for r in fail_rows),
            "sigma_n_mean": mean(r["sigma_n"] for r in fail_rows),
        },
        "agg_worse_joints": {
            "n": len(worse_rows),
            "multi_pattern_frac_mean": mean(
                r["multi_pattern_frac"] for r in worse_rows
            ),
            "sigma_n_mean": mean(r["sigma_n"] for r in worse_rows),
        },
        "probe": None,
    }

    if args.probe_teacher > 0:
        fail_idxs = [r["idx"] for r in fail_rows][: args.probe_teacher]
        if not fail_idxs:
            fail_idxs = [r["idx"] for r in rows][: args.probe_teacher]
        probe_rows = []
        print(
            f"\n=== teacher-column probe n={len(fail_idxs)} tl={args.probe_tl} ===",
            flush=True,
        )
        for idx in fail_idxs:
            pr = probe_teacher(idx, tl=args.probe_tl)
            pr["idx"] = idx
            probe_rows.append(pr)
            print(
                f"  idx={idx} ok={pr.get('ok')} err={pr.get('error')} "
                f"bars={pr.get('bars')} t={pr.get('elapsed_s')}s",
                flush=True,
            )
        summary["probe"] = {
            "n": len(probe_rows),
            "ok": sum(1 for p in probe_rows if p.get("ok")),
            "fail": sum(1 for p in probe_rows if not p.get("ok")),
            "rows": probe_rows,
        }

    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== DONE ===", flush=True)
    brief = {
        "subset": summary["subset"],
        "n": summary["n"],
        "elapsed_wall": summary["elapsed_wall"],
        "agg_by_tier": summary["agg_by_tier"],
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2), flush=True)
    print(f"wrote {out_root / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
