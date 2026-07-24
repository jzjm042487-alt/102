"""V4 全档复测：D1–D5 success 语料，对标老软件词典序。

用法:
  .venv\\Scripts\\python.exe scripts\\probe_v4_all_tiers.py
  .venv\\Scripts\\python.exe scripts\\probe_v4_all_tiers.py --tl 12 --workers 4
  .venv\\Scripts\\python.exe scripts\\probe_v4_all_tiers.py --tiers D1-入门 --limit 30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

CASES = ROOT / "data" / "legacy-cases"
OUT = ROOT / "data" / "acceptance" / "v4_all_tiers_probe"


def _num(v, as_float: bool = False):
    if v is None:
        return None
    try:
        return float(v) if as_float else int(float(v))
    except (TypeError, ValueError):
        return None


def _legacy_metrics(entry: dict, case: dict) -> dict:
    m = (case.get("metrics") or {}).get("metrics") or {}
    inner = m if "welding_joints" in m else (
        (case.get("metrics") or {}).get("metrics") or {}
    )
    return {
        "joints": _num(inner.get("welding_joints", entry.get("welding_joints"))),
        "weld_types": _num(
            inner.get("weld_pattern_types", entry.get("weld_pattern_types"))
        ),
        "cut_types": _num(
            inner.get("cut_pattern_types", entry.get("cut_pattern_types"))
        ),
        "util": _num(
            inner.get("utilization_rate", entry.get("utilization_rate")), True
        ),
    }


def _verdict(ours: dict | None, lg: dict) -> str:
    if ours is None:
        return "无解"
    ou = float(ours.get("util") or 0)
    lu = lg.get("util")
    if lu is not None and ou < float(lu) - 1e-3:
        return "util劣"
    for name, a, b in (
        ("焊口", ours.get("joints"), lg.get("joints")),
        ("拼法", ours.get("weld_types"), lg.get("weld_types")),
        ("切法", ours.get("cut_types"), lg.get("cut_types")),
    ):
        if b is None:
            continue
        if a is None:
            continue
        if int(a) < int(b):
            return f"优于({name})"
        if int(a) > int(b):
            return f"劣于({name})"
    return "打平"


def _solve_one(idx: int, tl: float, tier: str | None = None) -> dict:
    from backend.app.domain import parse_problem
    from scripts._exp_colgen import merge_equivalent_pipes
    from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
    from scripts.solve_v4_production import solve as v4_solve

    index = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
    by = {c["idx"]: c for c in index["cases"]}
    entry = by[idx]
    case = load_legacy_case(CASES, idx)
    lg = _legacy_metrics(entry, case)
    uf = None
    if lg.get("util") is not None:
        uf = max(0.95, float(lg["util"]) - 1e-3)

    t0 = time.monotonic()
    try:
        groups = parse_problem(clean_mom_problem(case["problem"])).groups
        if not groups:
            raise RuntimeError("no groups")
        acc = {
            "joints": 0,
            "weld_types": 0,
            "cut_types": 0,
            "used_len": 0.0,
            "demand_len": 0.0,
            "paths": [],
        }
        ok = True
        for g0 in groups:
            g = merge_equivalent_pipes(g0)
            sol = v4_solve(g, tl=tl, util_floor=uf, verbose=False)
            if sol is None:
                ok = False
                break
            acc["joints"] += int(sol.get("joints") or 0)
            acc["weld_types"] += int(sol.get("weld_types") or 0)
            acc["cut_types"] += int(sol.get("cut_types") or 0)
            acc["used_len"] += float(sol.get("used_len") or 0)
            acc["demand_len"] += float(g.demand_length)
            acc["paths"].append(list(sol.get("solver_path") or []))
        dt = round(time.monotonic() - t0, 3)
        tier_v = tier or entry.get("difficulty_tier") or entry.get("tier")
        if not ok:
            return {
                "idx": idx,
                "tier": tier_v,
                "ok": False,
                "verdict": "无解",
                "ours": None,
                "legacy": lg,
                "elapsed_s": dt,
                "geom_class": entry.get("geom_class"),
            }
        util = (
            acc["demand_len"] / acc["used_len"] if acc["used_len"] > 0 else 0.0
        )
        ours = {
            "joints": acc["joints"],
            "weld_types": acc["weld_types"],
            "cut_types": acc["cut_types"],
            "util": util,
            "path": acc["paths"][0] if acc["paths"] else [],
        }
        return {
            "idx": idx,
            "tier": tier_v,
            "ok": True,
            "verdict": _verdict(ours, lg),
            "ours": ours,
            "legacy": lg,
            "elapsed_s": dt,
            "geom_class": entry.get("geom_class"),
            "used_fp": any(
                "fingerprint_setcover" in (p or []) for p in acc["paths"]
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "idx": idx,
            "tier": tier or entry.get("difficulty_tier") or entry.get("tier"),
            "ok": False,
            "verdict": "ERROR",
            "error": f"{type(exc).__name__}:{exc}",
            "ours": None,
            "legacy": lg,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "geom_class": entry.get("geom_class"),
        }


def _worker(args: tuple[int, float, str]) -> dict:
    idx, tl, tier = args
    return _solve_one(idx, tl, tier)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tl", type=float, default=12.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--tiers",
        nargs="*",
        default=[],
        help="默认全部 D1–D5；可指定如 D1-入门 D2-常规",
    )
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    pools = json.loads((CASES / "difficulty_pools.json").read_text(encoding="utf-8"))
    index = json.loads((CASES / "index.json").read_text(encoding="utf-8"))
    by = {c["idx"]: c for c in index["cases"]}
    tier_labels = args.tiers or list(pools.get("tier_labels") or pools["pools"].keys())

    jobs: list[tuple[str, int]] = []
    for tier in tier_labels:
        ids = [
            i
            for i in pools["pools"][tier]
            if by.get(i, {}).get("legacy_status") == "success"
        ]
        if args.limit > 0:
            ids = ids[: args.limit]
        for i in ids:
            jobs.append((tier, i))

    OUT.mkdir(parents=True, exist_ok=True)
    print(
        f"v4 all-tiers probe n={len(jobs)} tiers={tier_labels} "
        f"tl={args.tl} workers={args.workers}",
        flush=True,
    )
    t0 = time.monotonic()
    rows: list[dict] = []
    done = 0
    work = [(idx, args.tl, tier) for tier, idx in jobs]

    if args.workers <= 1:
        for item in work:
            row = _worker(item)
            rows.append(row)
            done += 1
            print(
                f"[{done}/{len(work)}] {row['tier']} idx={row['idx']} {row['verdict']} "
                f"t={row['elapsed_s']}",
                flush=True,
            )
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_worker, item): item[0] for item in work}
            for fut in as_completed(futs):
                row = fut.result()
                rows.append(row)
                done += 1
                if done % 25 == 0 or done == len(work):
                    print(
                        f"[{done}/{len(work)}] last idx={row['idx']} "
                        f"{row['verdict']}",
                        flush=True,
                    )

    rows.sort(key=lambda r: (str(r.get("tier")), int(r["idx"])))
    by_tier: dict[str, list] = {}
    for r in rows:
        by_tier.setdefault(str(r.get("tier")), []).append(r)

    def summarize(rs: list[dict]) -> dict:
        vc = Counter(r["verdict"] for r in rs)
        ok_n = sum(1 for r in rs if r.get("ok"))
        worse = sum(
            1
            for r in rs
            if str(r["verdict"]).startswith("劣于") or r["verdict"] == "util劣"
        )
        unsolved = sum(1 for r in rs if r["verdict"] in ("无解", "ERROR"))
        fp_n = sum(1 for r in rs if r.get("used_fp"))
        return {
            "n": len(rs),
            "ok": ok_n,
            "ok_rate": ok_n / len(rs) if rs else 0.0,
            "verdict_counts": dict(vc),
            "n_worse_or_util": worse,
            "n_unsolved": unsolved,
            "n_fingerprint_setcover": fp_n,
            "not_worse_rate": (len(rs) - worse - unsolved) / len(rs) if rs else 0.0,
        }

    summary = {
        "tl": args.tl,
        "workers": args.workers,
        "elapsed_wall": round(time.monotonic() - t0, 1),
        "n": len(rows),
        "all": summarize(rows),
        "by_tier": {t: summarize(rs) for t, rs in sorted(by_tier.items())},
        "rows": rows,
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # slim table without full rows for quick read
    (OUT / "summary_brief.json").write_text(
        json.dumps(
            {k: summary[k] for k in ("tl", "elapsed_wall", "n", "all", "by_tier")},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n=== DONE ===", flush=True)
    print(json.dumps(summary["by_tier"], ensure_ascii=False, indent=2))
    print(f"all: {json.dumps(summary['all'], ensure_ascii=False)}")
    print(f"wrote {OUT / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
