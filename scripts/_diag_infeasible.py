"""诊断 v2 设置覆盖 ILP 为何 infeasible。

纯只读，复用 v2 的建模函数。逐项检查：
  1. 每种拼法候选是否都能被切法列产出（段覆盖）。
  2. 是否存在"必须整料/无法拼接"的长管，其对应母材长度供给是否足够。
  3. LP 松弛是否可行（若 LP 都不可行，说明是结构性缺料/缺列，而非整数难解）。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _poc_setcover_ilp_v2 as v2  # noqa: E402


def main() -> int:
    class A:
        sample_id = sys.argv[1] if len(sys.argv) > 1 else "350f2dbb-0858-4398-b00f-b2015be43e58"
        sample_file = None
        splits = 4
        max_trim = 150
        max_pieces = 3
        col_cap = 200000
        blade_margin = None

    group, legacy = v2._load(A)
    kerf = group.blade_margin
    weld_cands, alphabet = v2.build_weld_candidates(group, A.splits)
    stock_lengths = [s.length for s in group.stocks]
    cut_cols = v2.enumerate_cut_columns(
        sorted(alphabet), stock_lengths, kerf, A.col_cap, A.max_trim, A.max_pieces
    )
    cut_cols = v2.ensure_coverage(cut_cols, sorted(alphabet), stock_lengths, kerf, A.max_pieces)

    bars_by_len: dict[int, int] = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity

    print(f"母材供给: {dict(bars_by_len)}  总 {sum(bars_by_len.values())} 根")
    print(f"字母表段长 {len(alphabet)} 种, 切法列 {len(cut_cols)}")

    # 1. 段覆盖
    produced = {seg for c in cut_cols for seg in c["counts"]}
    needed = set()
    for pats in weld_cands.values():
        for p in pats:
            needed.update(p)
    uncovered = needed - produced
    print(f"\n[1] 拼法需要 {len(needed)} 种段长, 未被任何切法产出: {len(uncovered)} 种")
    if uncovered:
        print(f"    未覆盖段长(前20): {sorted(uncovered, reverse=True)[:20]}")

    # 2. 每种母材长度的产出上界 vs 需求下界（LP 层面粗算）
    #    最小总消耗段长 = 每根管选"总段长最小"的拼法（含 kerf 内部损耗），累加。
    min_consume = 0
    for i, pats in weld_cands.items():
        best = min(sum(p) for p in pats)
        min_consume += best * group.pipes[i].demand
    total_stock_len = sum(s.length * s.quantity for s in group.stocks)
    print(f"\n[2] 需求最小总段长={min_consume}  母材总长={total_stock_len}  "
          f"余量={total_stock_len - min_consume} ({'OK' if total_stock_len>=min_consume else '缺料!'})")

    # 3. 直接跑 LP 松弛（把整数变量放松），看是否可行
    from pyscipopt import Model, quicksum

    m = Model("lp")
    m.setParam("limits/time", 60)
    m.hideOutput()
    u = {}
    for i, pats in weld_cands.items():
        for wi in range(len(pats)):
            u[(i, wi)] = m.addVar(vtype="C", lb=0)
        m.addCons(quicksum(u[(i, wi)] for wi in range(len(pats))) == group.pipes[i].demand)
    n = len(cut_cols)
    x = {p: m.addVar(vtype="C", lb=0) for p in range(n)}
    cols_by_len = defaultdict(list)
    for p, col in enumerate(cut_cols):
        cols_by_len[col["stock"]].append(p)
    for L, plist in cols_by_len.items():
        m.addCons(quicksum(x[p] for p in plist) <= bars_by_len[L])
    produced_map = defaultdict(list)
    for p, col in enumerate(cut_cols):
        for seg, c in col["counts"].items():
            produced_map[seg].append((c, x[p]))
    consumed_map = defaultdict(list)
    for i, pats in weld_cands.items():
        for wi, parts in enumerate(pats):
            cnt = defaultdict(int)
            for seg in parts:
                cnt[seg] += 1
            for seg, c in cnt.items():
                consumed_map[seg].append((c, u[(i, wi)]))
    for seg in set(produced_map) | set(consumed_map):
        m.addCons(
            quicksum(c * v for c, v in produced_map.get(seg, []))
            >= quicksum(c * v for c, v in consumed_map.get(seg, []))
        )
    m.setObjective(quicksum(cut_cols[p]["stock"] * x[p] for p in range(n)), "minimize")
    m.optimize()
    print(f"\n[3] LP 松弛状态: {m.getStatus()}  "
          f"{'(整数难，但 LP 可行)' if m.getStatus()=='optimal' else '(结构性不可行!)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
