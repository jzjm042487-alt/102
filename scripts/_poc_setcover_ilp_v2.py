"""集合覆盖 ILP —— v2：拼法为决策变量 + 段长自由 + segment-balance 联合优化。

v1（`_poc_setcover_ilp.py`）用"每管型固定单一拼法模板"，在 B 样本上全面优于
老软件；但在 C 样本上利用率卡在 98.35%，比老软件 99.49% 低 1.1 个百分点。

根因（诊断见 §11.26）：C 样本的长管（如 6237mm）被固定模板强制"整段切出"，
只能塞进 >=6237 的母材，尾料巨大。而老软件把长管**拆成任意合法中间段、跨母材
焊接**（切法里出现 827/2484/4506/1731 这类既非管长也非固定段长的中间段），从而
把每根母材几乎切满、废料压到 10~32mm。

结论：**拼法不能固定，必须与切法联合优化**。v2 让"每管型选哪种拼法"成为决策变量，
段长在合法焊口位置自由取值，切割侧与拼接侧通过 segment-balance（产出>=消耗）耦合。

模型：
  拼法侧  u_{i,w} >= 0 整数 = 用拼法 w 造管型 i 的根数
          约束 Σ_w u_{i,w} = demand_i           每根管都造出来（天然全排完）
          拼法 w 消耗段长向量 cons_w[s]
  切割侧  x_p >= 0 整数     = 用切法 p 的母材根数
          切法 p 产出段长向量 prod_p[s]
  平衡    Σ_p prod_p[s]·x_p >= Σ_{i,w} cons_{i,w}[s]·u_{i,w}   每段长产出>=消耗
  目标    最小化总用料长度 Σ L_p·x_p           （直接最大化利用率）

共享段长字母表：由所有候选拼法拆出的段长并集构成；切法只枚举这些段长的组合，
两侧自动对齐。段长字母表大小 = 候选拼法拆分点数，用 per-pipe 候选数 cap 控制规模。

用法:
    python scripts/_poc_setcover_ilp_v2.py --sample-id 3040bd13-9108-40bf-9900-c5cdeeec51f0
    python scripts/_poc_setcover_ilp_v2.py --sample-id <uuid> --splits 8 --time 120
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.domain import MaterialGroup, PipeDemand, parse_problem  # noqa: E402
from app.solver import _legal_pattern  # noqa: E402


# ---------------------------------------------------------------------------
# Step 1: candidate weld patterns per pipe type (the DECISION set).
# ---------------------------------------------------------------------------


def _weld_positions(pipe: PipeDemand, group: MaterialGroup, max_stock: int, want: int) -> list[int]:
    """Legal single-joint positions that make BOTH parts fit a stock bar.

    A position ``k`` splits the pipe into ``(k, L-k)``.  We keep positions where
    each part <= max_stock and the split is legal (min_weld_distance, forbidden
    zones, min_cut_length).  To align the two parts with stock lengths (so a part
    can exactly fill a bar), we bias candidates toward ``k = stock_len`` and
    ``k = L - stock_len`` for every stock length, plus an even spread."""
    L = pipe.length
    stock_lengths = sorted({s.length for s in group.stocks})
    raw: set[int] = set()
    # stock-aligned splits: one part equals (or nearly) a whole bar
    for sl in stock_lengths:
        raw.add(sl)
        raw.add(L - sl)
        raw.add(sl - group.blade_margin)
        raw.add(L - sl + group.blade_margin)
    # even spread as fallback diversity
    for t in range(1, want + 1):
        raw.add(L * t // (want + 1))
    legal: list[int] = []
    for k in sorted(raw):
        if not (0 < k < L):
            continue
        parts = (k, L - k)
        if max(parts) > max_stock:
            continue
        if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
            legal.append(k)
    return legal


def build_weld_candidates(
    group: MaterialGroup, splits: int
) -> tuple[dict[int, list[tuple[int, ...]]], set[int]]:
    """Return (candidate weld patterns per pipe index, shared segment alphabet).

    Each pipe gets: the whole-pipe pattern (if it fits a bar) plus up to
    ``splits`` single-joint splits, plus (when max_joints>=2) a few two-joint
    splits built from the best single-joint positions.  All resulting segment
    lengths form the shared alphabet the cut side must be able to produce."""
    max_stock = max(s.length for s in group.stocks)
    cands: dict[int, list[tuple[int, ...]]] = {}
    alphabet: set[int] = set()
    for i, pipe in enumerate(group.pipes):
        pats: set[tuple[int, ...]] = set()
        if pipe.length <= max_stock:
            pats.add((pipe.length,))
        if pipe.max_joints >= 1:
            positions = _weld_positions(pipe, group, max_stock, splits)
            # keep a bounded, well-spread subset
            if len(positions) > splits:
                step = len(positions) / splits
                positions = [positions[int(j * step)] for j in range(splits)]
            for k in positions:
                pats.add((k, pipe.length - k))
        if pipe.max_joints >= 2:
            positions = _weld_positions(pipe, group, max_stock, splits)
            picks = positions[: min(4, len(positions))]
            for a in picks:
                for b in picks:
                    if b <= a:
                        continue
                    parts = (a, b - a, pipe.length - b)
                    if max(parts) > max_stock:
                        continue
                    if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
                        pats.add(parts)
        if not pats:
            raise ValueError(f"no legal weld pattern for pipe {pipe.pipe_id} (L={pipe.length})")
        cands[i] = sorted(pats, key=lambda p: (len(p), p))
        for p in cands[i]:
            alphabet.update(p)
    return cands, alphabet


# ---------------------------------------------------------------------------
# Step 2: enumerate cutting-pattern columns over the shared alphabet.
# ---------------------------------------------------------------------------


def enumerate_cut_columns(
    seg_lengths: list[int],
    stock_lengths: list[int],
    kerf: int,
    col_cap: int,
    max_trim: int,
    max_pieces: int,
) -> list[dict[str, Any]]:
    """Each column = one way to cut a single stock bar into alphabet segments.

    Physical budget matches the verifier: ``sum(parts) + kerf*(#pieces-1) <= L``.
    Only DENSE columns (trailing remnant <= ``max_trim``) with at most
    ``max_pieces`` segments are kept -- legacy cut patterns are 2-3 segments with
    <=32 mm trim, so this mirrors the real structure and keeps the pool small."""
    seg_lengths = sorted(set(seg_lengths), reverse=True)
    smallest = seg_lengths[-1] if seg_lengths else 0
    columns: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[tuple[int, int], ...]]] = set()

    for L in sorted(set(stock_lengths)):
        counts: dict[int, int] = {}
        budget = [col_cap]

        def dfs(idx: int, used: int, n_pieces: int) -> None:
            if budget[0] <= 0:
                return
            remnant = L - used
            if n_pieces >= 1 and remnant <= max_trim:
                key = (L, tuple(sorted(counts.items())))
                if key not in seen:
                    seen.add(key)
                    columns.append(
                        {"stock": L, "counts": dict(counts), "used": used, "remnant": remnant}
                    )
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return
            if remnant < smallest or n_pieces >= max_pieces:
                return
            for j in range(idx, len(seg_lengths)):
                seg = seg_lengths[j]
                extra = seg + (kerf if n_pieces >= 1 else 0)
                if used + extra > L:
                    continue
                counts[seg] = counts.get(seg, 0) + 1
                dfs(j, used + extra, n_pieces + 1)
                counts[seg] -= 1
                if counts[seg] == 0:
                    del counts[seg]
                if budget[0] <= 0:
                    return

        dfs(0, 0, 0)
    return columns


def ensure_coverage(
    columns: list[dict[str, Any]],
    seg_lengths: list[int],
    stock_lengths: list[int],
    kerf: int,
    max_pieces: int,
) -> list[dict[str, Any]]:
    """Guarantee every alphabet segment has at least one producing column.

    Segment balance (produced >= consumed) is INFEASIBLE if some segment a weld
    pattern needs is never produced by any cut column -- which happens when the
    only way to cut it leaves a trailing remnant above ``max_trim``.  For each
    uncovered segment we greedily build one dense-as-possible column (the segment
    itself, then fill with the largest segments that still fit), ignoring the
    ``max_trim`` filter.  These are just feasibility anchors; the ILP will avoid
    them unless truly needed."""
    produced = {seg for col in columns for seg in col["counts"]}
    missing = [s for s in seg_lengths if s not in produced]
    if not missing:
        return columns
    segs_desc = sorted(set(seg_lengths), reverse=True)
    max_stock = max(stock_lengths)
    existing = {(c["stock"], tuple(sorted(c["counts"].items()))) for c in columns}
    added = 0
    for seg in missing:
        # smallest stock bar that can hold this segment
        L = min((s for s in sorted(set(stock_lengths)) if s >= seg), default=max_stock)
        counts: dict[int, int] = {seg: 1}
        used = seg
        n_pieces = 1
        for s in segs_desc:
            while n_pieces < max_pieces and used + s + kerf <= L:
                counts[s] = counts.get(s, 0) + 1
                used += s + kerf
                n_pieces += 1
        key = (L, tuple(sorted(counts.items())))
        if key in existing:
            continue
        existing.add(key)
        columns.append(
            {"stock": L, "counts": dict(counts), "used": used, "remnant": L - used}
        )
        added += 1
    if added:
        print(f"[step2b] added {added} coverage-anchor columns for otherwise-unproducible segments", flush=True)
    return columns


# ---------------------------------------------------------------------------
# Step 3: joint cut+weld set-covering ILP.
# ---------------------------------------------------------------------------


def solve_joint(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
    time_limit: float,
    target_len: int | None = None,
    minimise: str = "length",
    strict_balance: bool = False,
) -> dict[str, Any]:
    """Joint cut+weld set-covering ILP.

    ``minimise``:
      * ``length``: minimise total stock length (utilisation-optimal).
      * ``cut_types``: minimise number of DISTINCT cut patterns used (the shop's
        real pain point) subject to a utilisation floor.  Requires ``target_len``
        -- total stock length is hard-capped at ``target_len`` so the solution can
        never be worse than that (e.g. legacy's used length => "not worse than
        legacy").  This is the lexicographic "lock utilisation, then cut variety".
    """
    from pyscipopt import Model, quicksum

    m = Model("setcover_v2")
    m.setParam("limits/time", time_limit)
    m.hideOutput()

    total_bars = sum(s.quantity for s in group.stocks)
    bars_by_len: dict[int, int] = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity

    n = len(cut_cols)

    # weld decision vars: u[i][wi]
    u: dict[tuple[int, int], Any] = {}
    for i, pats in weld_cands.items():
        for wi in range(len(pats)):
            u[(i, wi)] = m.addVar(vtype="I", lb=0, name=f"u_{i}_{wi}")
        m.addCons(quicksum(u[(i, wi)] for wi in range(len(pats))) == group.pipes[i].demand)

    # cut decision vars: x[p]
    x = {p: m.addVar(vtype="I", lb=0, name=f"x{p}") for p in range(n)}

    # per-stock-length bar budget
    cols_by_len: dict[int, list[int]] = defaultdict(list)
    for p, col in enumerate(cut_cols):
        cols_by_len[col["stock"]].append(p)
    for L, plist in cols_by_len.items():
        m.addCons(quicksum(x[p] for p in plist) <= bars_by_len[L])

    # segment balance: produced >= consumed, per segment length
    produced: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for p, col in enumerate(cut_cols):
        for seg, c in col["counts"].items():
            produced[seg].append((c, x[p]))
    consumed: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for i, pats in weld_cands.items():
        for wi, parts in enumerate(pats):
            cnt: dict[int, int] = defaultdict(int)
            for seg in parts:
                cnt[seg] += 1
            for seg, c in cnt.items():
                consumed[seg].append((c, u[(i, wi)]))

    for seg in set(produced) | set(consumed):
        lhs = quicksum(c * var for c, var in produced.get(seg, []))
        rhs = quicksum(c * var for c, var in consumed.get(seg, []))
        if strict_balance:
            m.addCons(lhs == rhs)
        else:
            m.addCons(lhs >= rhs)

    length_term = quicksum(cut_cols[p]["stock"] * x[p] for p in range(n))

    if minimise == "cut_types":
        if target_len is None:
            raise ValueError("cut_types objective requires target_len (utilisation floor)")
        # hard cap on used length => never worse than the target (e.g. legacy).
        m.addCons(length_term <= target_len)
        # y_p = cut pattern p used; z_t = weld-type t used (weld TYPE = parts tuple,
        # pool-global per verifier).  Minimise a weighted sum of distinct cut and
        # weld pattern types -- both drive shop-floor difficulty.  Cut variety is
        # weighted a bit higher (user: "one extra cut type ~ 3x cutting effort").
        y = {p: m.addVar(vtype="B", name=f"y{p}") for p in range(n)}
        for p in range(n):
            m.addCons(x[p] <= bars_by_len[cut_cols[p]["stock"]] * y[p])

        # map weld TYPE (parts tuple) -> the u-vars that realise it
        wtype_vars: dict[tuple[int, ...], list[Any]] = defaultdict(list)
        for i, pats in weld_cands.items():
            for wi, parts in enumerate(pats):
                wtype_vars[tuple(parts)].append(u[(i, wi)])
        z = {t: m.addVar(vtype="B", name=f"z{k}") for k, t in enumerate(wtype_vars)}
        for t, vars_ in wtype_vars.items():
            # z_t must be 1 if any u realising type t is used; big-M = total demand
            bigM = sum(group.pipes[i].demand for i in range(len(group.pipes)))
            m.addCons(quicksum(vars_) <= bigM * z[t])

        # true lexicographic: cut types are the primary pain point, so make one
        # extra cut type strictly worse than eliminating EVERY weld type.  A cut
        # weight of (|weld types| + 1) guarantees weld count can never buy back a
        # cut type -- weld minimisation is a pure tie-break under fixed cut count.
        lex_cut_w = len(wtype_vars) + 1
        m.setObjective(
            lex_cut_w * quicksum(y[p] for p in range(n))
            + quicksum(z[t] for t in wtype_vars),
            "minimize",
        )
    else:
        if target_len is not None:
            m.addCons(length_term <= target_len)
        m.setObjective(length_term, "minimize")

    started = time.monotonic()
    m.optimize()
    elapsed = time.monotonic() - started
    status = m.getStatus()
    if m.getNSols() == 0:
        return {"status": status, "elapsed": elapsed, "feasible": False}

    used_cuts = []
    active_cut_idx: list[int] = []
    total_x = 0
    used_len = 0
    for p in range(n):
        xv = round(m.getVal(x[p]))
        if xv > 0:
            used_cuts.append({**cut_cols[p], "count": xv})
            active_cut_idx.append(p)
            total_x += xv
            used_len += cut_cols[p]["stock"] * xv
    used_welds = []
    for (i, wi), var in u.items():
        uv = round(m.getVal(var))
        if uv > 0:
            used_welds.append({"pipe": i, "parts": weld_cands[i][wi], "count": uv})
    weld_types = len({tuple(w["parts"]) for w in used_welds})
    return {
        "status": status,
        "elapsed": elapsed,
        "feasible": True,
        "cut_types": len(used_cuts),
        "weld_types": weld_types,
        "total_bars": total_x,
        "used_len": used_len,
        "cuts": used_cuts,
        "welds": used_welds,
        "active_cut_idx": active_cut_idx,
    }


# ---------------------------------------------------------------------------
# Two-phase lexicographic: phase 1 minimise length (fast, no big-M) to get the
# utilisation floor + an active column set; phase 2 minimise cut-pattern types on
# that (much smaller) active set under a length cap == phase-1 length (so it can
# never get worse).  This scales where a single big-M model over the full pool
# cannot even find a feasible solution.
# ---------------------------------------------------------------------------


def solve_two_phase(
    group: MaterialGroup,
    weld_cands: dict[int, list[tuple[int, ...]]],
    cut_cols: list[dict[str, Any]],
    time_p1: float,
    time_p2: float,
    slack: float,
) -> dict[str, Any]:
    p1 = solve_joint(group, weld_cands, cut_cols, time_p1, minimise="length")
    if not p1["feasible"]:
        return p1
    cap = int(p1["used_len"] * (1 + slack))
    # phase 2: minimise cut TYPES then weld TYPES (lexicographic) under the phase-1
    # length cap.  Pool = phase-1 active columns: keeps the big-M type-count MIP
    # small enough to solve to optimality.  (A wider pool lets cut types drop a
    # little further in principle, but the big-M model then times out and the
    # fallback is worse -- active-only is the robust choice.)
    active = [cut_cols[i] for i in p1["active_cut_idx"]]
    p2 = solve_joint(
        group, weld_cands, active, time_p2, target_len=cap, minimise="cut_types"
    )
    if not p2["feasible"]:
        # phase 2 could not improve within its pool/time -> keep phase 1.
        p1["phase"] = 1
        p1["p1_used_len"] = p1["used_len"]
        return p1
    p2["phase"] = 2
    p2["p1_used_len"] = p1["used_len"]
    p2["p1_cut_types"] = p1["cut_types"]
    p2["elapsed"] = p1["elapsed"] + p2["elapsed"]
    return p2


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _load(args) -> tuple[MaterialGroup, dict[str, Any] | None]:
    if args.sample_file:
        payload = json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
        legacy = None
    elif args.sample_id:
        samples = json.loads(
            (REPO_ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
        )
        rec = next(s for s in samples["samples"] if s["id"] == args.sample_id)
        payload, legacy = rec["problem"], rec.get("legacy")
    else:
        raise SystemExit("provide --sample-file or --sample-id")
    if args.blade_margin is not None:
        payload = {**payload, "NestParam": {**payload.get("NestParam", {}), "BladeMargin": args.blade_margin}}
    problem = parse_problem(payload)
    if not problem.groups:
        raise SystemExit("no group parsed")
    return problem.groups[0], legacy


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sample-file")
    p.add_argument("--sample-id")
    p.add_argument("--blade-margin", type=float, default=None)
    p.add_argument("--splits", type=int, default=6, help="single-joint split candidates per pipe")
    p.add_argument("--col-cap", type=int, default=200000, help="max cut columns per stock length")
    p.add_argument("--max-trim", type=int, default=400, help="keep only cut columns with trailing remnant <= this")
    p.add_argument("--max-pieces", type=int, default=3, help="max segments per cut column (legacy is 2-3)")
    p.add_argument("--time", type=float, default=120.0, help="ILP time limit (s); for two_phase this is split across the two phases")
    p.add_argument("--minimise", choices=["length", "cut_types", "two_phase"], default="length", help="objective: utilisation-optimal length, fewest cut patterns under a floor, or two-phase lexicographic (recommended)")
    p.add_argument("--target-len", type=int, default=None, help="used-length hard cap; if omitted with --minimise cut_types, uses legacy used length (not-worse-than-legacy)")
    p.add_argument("--slack", type=float, default=0.0, help="relax the length cap by this fraction (e.g. 0.001) to trade a little material for fewer cut types")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    group, legacy = _load(args)
    kerf = group.blade_margin

    print(
        f"Group {group.material}/{group.specifications}: "
        f"{len(group.pipes)} pipe types, demand {sum(p.demand for p in group.pipes)} pipes, "
        f"{sum(s.quantity for s in group.stocks)} bars, kerf={kerf}, "
        f"min_weld={group.min_weld_distance}, min_cut={group.min_cut_length}\n"
    )

    t0 = time.monotonic()
    weld_cands, alphabet = build_weld_candidates(group, args.splits)
    n_wc = sum(len(v) for v in weld_cands.values())
    print(
        f"[step1] weld candidates: {n_wc} patterns over {len(group.pipes)} pipe types "
        f"-> shared alphabet {len(alphabet)} distinct segment lengths",
        flush=True,
    )

    stock_lengths = [s.length for s in group.stocks]
    cut_cols = enumerate_cut_columns(
        sorted(alphabet), stock_lengths, kerf, args.col_cap, args.max_trim, args.max_pieces
    )
    cut_cols = ensure_coverage(cut_cols, sorted(alphabet), stock_lengths, kerf, args.max_pieces)
    print(
        f"[step2] enumerated {len(cut_cols)} cut columns "
        f"({len(set(stock_lengths))} stock lengths, cap {args.col_cap}, "
        f"max_trim {args.max_trim}, max_pieces {args.max_pieces}) "
        f"in {time.monotonic()-t0:.2f}s\n",
        flush=True,
    )

    # Determine the used-length hard cap for the cut-types objective.
    target_len = args.target_len
    legacy_used_len = None
    if legacy:
        lcp0 = ((legacy.get("Result") or {}).get("CuttingPattern") or {}).get("CuttingPipe", []) or []
        legacy_used_len = sum(
            int(float(c.get("Length", 0))) * int(float(c.get("Number", 0))) for c in lcp0
        )

    if args.minimise == "two_phase":
        res = solve_two_phase(
            group, weld_cands, cut_cols, args.time / 2, args.time / 2, args.slack
        )
        if res.get("feasible"):
            print(
                f"[two_phase] phase={res.get('phase')} "
                f"p1_used_len={res.get('p1_used_len')} p1_cut_types={res.get('p1_cut_types','-')}",
                flush=True,
            )
    else:
        if args.minimise == "cut_types" and target_len is None:
            if legacy_used_len is None:
                raise SystemExit("--minimise cut_types needs --target-len (no legacy to derive it)")
            target_len = int(legacy_used_len * (1 + args.slack))
        res = solve_joint(
            group, weld_cands, cut_cols, args.time, target_len=target_len, minimise=args.minimise
        )
        if args.minimise == "cut_types":
            print(f"[phase2] minimise cut types, used-length cap = {target_len} "
                  f"(legacy used {legacy_used_len}, slack {args.slack})", flush=True)

    print(f"[step3] joint ILP status={res['status']} in {res['elapsed']:.2f}s")
    if not res["feasible"]:
        print("  INFEASIBLE / no solution within time limit")
        return 1

    demand_len = group.demand_length
    util = demand_len / res["used_len"] if res["used_len"] else 0.0
    print(f"  cut pattern types  = {res['cut_types']}")
    print(f"  weld pattern types = {res['weld_types']}")
    print(f"  stock bars used    = {res['total_bars']}")
    print(f"  utilisation        = {util:.6f}  (demand {demand_len} / used {res['used_len']})")

    if legacy:
        gi = legacy.get("GeneralInfo") or {}
        lg = legacy.get("Result") or {}
        lcp = (lg.get("CuttingPattern") or {}).get("CuttingPipe", []) or []
        lwp = (lg.get("WeldingPattern") or {}).get("WeldingPipe", []) or []
        legacy_bars = sum(int(float(c.get("Number", 0))) for c in lcp)
        print("\n  --- vs legacy ---")
        print(f"  {'metric':<20}{'v2':>14}{'legacy':>14}")
        print(f"  {'cut pattern types':<20}{res['cut_types']:>14}{len(lcp):>14}")
        print(f"  {'weld pattern types':<20}{res['weld_types']:>14}{len(lwp):>14}")
        print(f"  {'stock bars':<20}{res['total_bars']:>14}{legacy_bars:>14}")
        print(f"  {'utilisation':<20}{util:>14.6f}{float(gi.get('UtilRate', 0)):>14.6f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
