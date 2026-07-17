"""集合覆盖 ILP —— 最小 POC（docs §11.24 逆向结论的验证一跳）。

逆向老软件指纹显示它是"列生成 / 集合覆盖 ILP"：切法种类远少于母材根数、
同管固定单一拼法、TrimLoss 被精确集中。本脚本用最朴素的方式复刻这条主引擎，
只回答一个问题：

    在 B 样本（老软件全排完、baseline 求不动）上，
    "固定拼法模板 → 枚举切法列 → ILP 选列" 能否同时做到
        (1) 全部管子排完（每个段长需求被覆盖）
        (2) 切法种类 ≤ 老软件
    ？

若能，就证明"先证明再重工程"的方向正确，值得把它做成生产 route。
若不能，就说明列池/定价还得加强（列生成而非静态枚举）。

方法（三步，全部朴素实现，不追求最优，只追求可行性证据）：

  1) 拼法模板（每管一种）: 用 solver._baseline_weld_patterns 取该管型
     "关节最少 + 字典序最小"的一种合法拼法，得到它需要的"段长清单"。
     把所有管型的段长按 (段长) 聚合成"段长需求" demand_s。
     —— 这一步与逆向发现的"同管固定单一拼法"对齐。

  2) 切法列枚举: 对每种母材长度，DFS 枚举"把 1 根母材切成若干需求段"的方案
     （kerf 感知、按段长非增序去重、尾料 >= 0），每个方案就是一列 pattern，
     记录它产出的各段长数量向量 a_p[s]。列数用 cap 限制，保证 POC 秒级。

  3) 集合覆盖 ILP (PySCIPOpt):
        变量  x_p >= 0 整数  = 用切法 p 的母材根数
              y_p in {0,1}    = 切法 p 是否被启用（用于数"种类"）
        约束  Σ_p a_p[s] * x_p >= demand_s        每个段长需求被覆盖
              x_p <= M * y_p                       启用后才能用
              Σ_p x_p <= 可用母材总根数            母材上限
        目标  按词典序: 先最小化 Σ y_p（切法种类），再最小化 Σ x_p（母材根数）
              用大权重把种类放首位: minimize  W*Σy_p + Σx_p

用法:
    python scripts/_poc_setcover_ilp.py --sample-id 6a4fecf7-d070-4629-8bcc-a0ffa5c3b091
    python scripts/_poc_setcover_ilp.py --sample-file some.json --col-cap 20000 --time 60
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
from app.solver import _baseline_weld_patterns, _legal_pattern  # noqa: E402


# ---------------------------------------------------------------------------
# Step 1: fixed weld template per pipe type -> aggregated segment demand.
# ---------------------------------------------------------------------------


def _weld_template(pipe: PipeDemand, group: MaterialGroup, max_stock: int) -> tuple[int, ...]:
    """Pick ONE legal weld template for this pipe: fewest joints, then lexi-min.

    Mirrors the legacy fingerprint (each pipe type welded exactly one way)."""
    patterns = _baseline_weld_patterns(pipe, group, max_stock)
    legal = [
        p
        for p in patterns
        if _legal_pattern(pipe, p, group.min_weld_distance, group.min_cut_length)
    ]
    if not legal:
        raise ValueError(f"no legal weld template for pipe {pipe.pipe_id} (L={pipe.length})")
    # fewest parts (= fewest joints), then lexicographically smallest.
    legal.sort(key=lambda p: (len(p), tuple(sorted(p, reverse=True))))
    return tuple(legal[0])


def build_segment_demand(group: MaterialGroup) -> tuple[dict[int, int], dict[str, tuple[int, ...]]]:
    """Return (demand_by_segment_length, template_by_pipe_id)."""
    max_stock = max(s.length for s in group.stocks)
    seg_demand: dict[int, int] = defaultdict(int)
    templates: dict[str, tuple[int, ...]] = {}
    for pipe in group.pipes:
        tpl = _weld_template(pipe, group, max_stock)
        templates[pipe.pipe_id] = tpl
        for seg in tpl:
            seg_demand[seg] += pipe.demand
    return dict(seg_demand), templates


# ---------------------------------------------------------------------------
# Step 2: enumerate cutting-pattern columns per stock length.
# ---------------------------------------------------------------------------


def enumerate_cut_columns(
    seg_lengths: list[int],
    stock_lengths: list[int],
    kerf: int,
    min_remnant: int,
    col_cap: int,
) -> list[dict[str, Any]]:
    """Each column = one way to cut a single stock bar into segment pieces.

    A stock bar of length ``L`` holds pieces whose lengths sum, together with one
    kerf per internal cut, to <= L.  We model the physical budget the same way the
    verifier does: ``sum(parts) + kerf*(#pieces-1) <= L`` when parts are all cut
    from one bar (the leftover, if reusable, is not a cut-loss)."""
    seg_lengths = sorted(set(seg_lengths), reverse=True)
    columns: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[tuple[int, int], ...]]] = set()

    for L in sorted(set(stock_lengths)):
        # DFS: choose counts of each segment length in non-increasing order.
        counts: dict[int, int] = {}
        budget = [col_cap]

        def dfs(idx: int, used: int, n_pieces: int) -> None:
            if budget[0] <= 0:
                return
            # record current combination (>=1 piece) as a column
            if n_pieces >= 1:
                remnant = L - used  # physical leftover before final-kerf bookkeeping
                key = (L, tuple(sorted(counts.items())))
                if key not in seen:
                    seen.add(key)
                    columns.append(
                        {
                            "stock": L,
                            "counts": dict(counts),
                            "used": used,
                            "n_pieces": n_pieces,
                            "remnant": remnant,
                            "reusable": remnant >= min_remnant,
                        }
                    )
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return
            for j in range(idx, len(seg_lengths)):
                seg = seg_lengths[j]
                # adding one more piece costs seg + (kerf if not first piece)
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


# ---------------------------------------------------------------------------
# Step 3: set-covering ILP.
# ---------------------------------------------------------------------------


def solve_set_cover(
    columns: list[dict[str, Any]],
    seg_demand: dict[int, int],
    total_bars: int,
    time_limit: float,
    type_weight: float,
    count_types: bool,
    objective: str = "length",
) -> dict[str, Any]:
    """Set-covering MIP.

    ``objective``:
      * ``length`` (default): minimise total stock LENGTH consumed
        (``Σ L_p·x_p``) -- this directly maximises utilisation because it
        penalises both extra bars AND large offcuts, so SCIP prefers dense,
        low-trim columns.  This is the correct objective for our tight instances.
      * ``bars``: minimise bar COUNT only -- fast but indifferent to trim, so it
        can leave large offcuts on long bars (lower utilisation).

    ``count_types`` adds the big-M ``y_p`` layer to *explicitly* minimise pattern
    types first -- correct in theory but heavy for SCIP on large static pools."""
    from pyscipopt import Model, quicksum

    m = Model("setcover_poc")
    m.setParam("limits/time", time_limit)
    m.hideOutput()

    n = len(columns)
    x = {p: m.addVar(vtype="I", lb=0, name=f"x{p}") for p in range(n)}

    # coverage: produced >= demand for each segment length
    for seg, dem in seg_demand.items():
        m.addCons(
            quicksum(columns[p]["counts"].get(seg, 0) * x[p] for p in range(n)) >= dem
        )

    # bar budget
    m.addCons(quicksum(x[p] for p in range(n)) <= total_bars)

    bars_term = quicksum(x[p] for p in range(n))
    length_term = quicksum(columns[p]["stock"] * x[p] for p in range(n))
    primary = length_term if objective == "length" else bars_term

    if count_types:
        y = {p: m.addVar(vtype="B", name=f"y{p}") for p in range(n)}
        for p in range(n):
            m.addCons(x[p] <= total_bars * y[p])
        m.setObjective(type_weight * quicksum(y[p] for p in range(n)) + primary, "minimize")
    else:
        m.setObjective(primary, "minimize")

    started = time.monotonic()
    m.optimize()
    elapsed = time.monotonic() - started
    status = m.getStatus()

    if m.getNSols() == 0:
        return {"status": status, "elapsed": elapsed, "feasible": False}

    used_cols = []
    total_x = 0
    for p in range(len(columns)):
        xv = round(m.getVal(x[p]))
        if xv > 0:
            used_cols.append({**columns[p], "count": xv})
            total_x += xv
    return {
        "status": status,
        "elapsed": elapsed,
        "feasible": True,
        "cut_types": len(used_cols),
        "total_bars": total_x,
        "columns": used_cols,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _load_group(args) -> tuple[MaterialGroup, dict[str, Any] | None]:
    if args.sample_file:
        payload = json.loads(Path(args.sample_file).read_text(encoding="utf-8"))
        legacy = None
    elif args.sample_id:
        samples = json.loads(
            (REPO_ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8")
        )
        rec = next(s for s in samples["samples"] if s["id"] == args.sample_id)
        payload = rec["problem"]
        legacy = rec.get("legacy")
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
    p.add_argument("--col-cap", type=int, default=40000, help="max columns per stock length")
    p.add_argument("--time", type=float, default=60.0, help="ILP time limit (s)")
    p.add_argument("--type-weight", type=float, default=None, help="objective weight on pattern types")
    p.add_argument("--count-types", action="store_true", help="add big-M y_p layer to explicitly minimise pattern types (heavy)")
    p.add_argument("--objective", choices=["length", "bars"], default="length", help="minimise total stock length (util-optimal) or bar count")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    group, legacy = _load_group(args)
    kerf = group.blade_margin
    min_remnant = group.min_reusable_remnant

    print(
        f"Group {group.material}/{group.specifications}: "
        f"{len(group.pipes)} pipe types, demand {sum(p.demand for p in group.pipes)} pipes, "
        f"{sum(s.quantity for s in group.stocks)} bars, kerf={kerf}, "
        f"min_weld={group.min_weld_distance}, min_cut={group.min_cut_length}, "
        f"min_remnant={min_remnant}\n"
    )

    t0 = time.monotonic()
    seg_demand, templates = build_segment_demand(group)
    print(
        f"[step1] fixed weld templates: {len(templates)} pipe types -> "
        f"{len(seg_demand)} distinct segment lengths, "
        f"total {sum(seg_demand.values())} segment pieces demanded"
    )

    stock_lengths = [s.length for s in group.stocks]
    total_bars = sum(s.quantity for s in group.stocks)
    columns = enumerate_cut_columns(
        list(seg_demand.keys()), stock_lengths, kerf, min_remnant, args.col_cap
    )
    print(
        f"[step2] enumerated {len(columns)} cutting-pattern columns "
        f"({len(set(stock_lengths))} stock lengths, cap {args.col_cap}) "
        f"in {time.monotonic()-t0:.2f}s\n"
    )

    # type_weight must dominate the bar term: > total_bars so one extra type never
    # pays for itself in bar savings.
    type_weight = args.type_weight if args.type_weight is not None else float(total_bars + 1)
    res = solve_set_cover(
        columns, seg_demand, total_bars, args.time, type_weight, args.count_types, args.objective
    )

    print(f"[step3] ILP status={res['status']} in {res['elapsed']:.2f}s")
    if not res["feasible"]:
        print("  INFEASIBLE / no solution within time limit")
        return 1

    # utilisation from covered demand: sum(pipe.length*demand) / (bars*avg? ) --
    # we compute it exactly as demand_length / used_stock_length.
    demand_len = group.demand_length
    # used stock length = sum over chosen columns of stock*count MINUS reusable
    # leftovers that survive (not consumed). For a pure covering POC we report the
    # gross used length (bars actually opened) as the denominator, matching how the
    # verifier counts used bars.
    used_len = sum(c["stock"] * c["count"] for c in res["columns"])
    util = demand_len / used_len if used_len else 0.0

    print(f"  cut pattern types = {res['cut_types']}")
    print(f"  stock bars used   = {res['total_bars']} / {total_bars} available")
    print(f"  utilisation       = {util:.6f}  (demand_len {demand_len} / used_len {used_len})")

    if legacy:
        gi = legacy.get("GeneralInfo") or {}
        lg = legacy.get("Result") or {}
        lcp = (lg.get("CuttingPattern") or {}).get("CuttingPipe", []) or []
        lwp = (lg.get("WeldingPattern") or {}).get("WeldingPipe", []) or []
        legacy_bars = sum(int(float(c.get("Number", 0))) for c in lcp)
        print("\n  --- vs legacy ---")
        print(f"  {'metric':<20}{'POC':>14}{'legacy':>14}")
        print(f"  {'cut pattern types':<20}{res['cut_types']:>14}{len(lcp):>14}")
        print(f"  {'weld pattern types':<20}{len(templates):>14}{len(lwp):>14}")
        print(f"  {'stock bars':<20}{res['total_bars']:>14}{legacy_bars:>14}")
        print(f"  {'utilisation':<20}{util:>14.6f}{float(gi.get('UtilRate', 0)):>14.6f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
