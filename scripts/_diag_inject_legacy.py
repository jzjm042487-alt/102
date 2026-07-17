"""验证假设：D 样本 infeasible 是否因"拼法候选段长与母材对不齐"。

做法：把老软件实际用过的拼法段长注入候选，再跑 LP 松弛。
若注入后 LP 可行 => 证明是候选生成粒度问题，需要 pricing/列生成动态补段。
若仍 infeasible => 是我们的约束建模与老软件语义不一致。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _poc_setcover_ilp_v2 as v2  # noqa: E402


def _parts(s: str) -> tuple[int, ...]:
    return tuple(int(round(float(t))) for t in str(s).split() if t.strip())


def main() -> int:
    class A:
        sample_id = sys.argv[1] if len(sys.argv) > 1 else "350f2dbb-0858-4398-b00f-b2015be43e58"
        sample_file = None
        splits = 4
        max_trim = 400
        max_pieces = 3
        col_cap = 200000
        blade_margin = None

    group, legacy = v2._load(A)
    kerf = group.blade_margin
    weld_cands, alphabet = v2.build_weld_candidates(group, A.splits)

    # --- 注入老软件真实拼法 ---
    wp = ((legacy.get("Result") or {}).get("WeldingPattern") or {}).get("WeldingPipe", []) or []
    # 按 (figure, jlxh) 建管长->pipe index 映射（近似：用管长匹配）
    len_to_idx: dict[int, list[int]] = defaultdict(list)
    for i, p in enumerate(group.pipes):
        len_to_idx[p.length].append(i)

    injected = 0
    for w in wp:
        for pat in (w.get("Pattern") or []):
            segs = _parts(pat.get("Part", ""))
            if not segs:
                continue
            total = sum(segs)
            for i in len_to_idx.get(total, []):
                cur = weld_cands.get(i, [])
                if tuple(segs) not in {tuple(x) for x in cur}:
                    weld_cands[i] = list(cur) + [tuple(segs)]
                    alphabet.update(segs)
                    injected += 1
    print(f"注入老软件拼法段组: {injected} 条, 字母表 -> {len(alphabet)} 段长")

    stock_lengths = [s.length for s in group.stocks]
    cut_cols = v2.enumerate_cut_columns(
        sorted(alphabet), stock_lengths, kerf, A.col_cap, A.max_trim, A.max_pieces
    )
    cut_cols = v2.ensure_coverage(cut_cols, sorted(alphabet), stock_lengths, kerf, A.max_pieces)
    print(f"切法列: {len(cut_cols)}")

    from pyscipopt import Model, quicksum

    m = Model("lp2")
    m.setParam("limits/time", 90)
    m.hideOutput()
    bars_by_len: dict[int, int] = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity
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
    prod = defaultdict(list)
    for p, col in enumerate(cut_cols):
        for seg, c in col["counts"].items():
            prod[seg].append((c, x[p]))
    cons = defaultdict(list)
    for i, pats in weld_cands.items():
        for wi, parts in enumerate(pats):
            cnt = defaultdict(int)
            for seg in parts:
                cnt[seg] += 1
            for seg, c in cnt.items():
                cons[seg].append((c, u[(i, wi)]))
    for seg in set(prod) | set(cons):
        m.addCons(
            quicksum(c * v for c, v in prod.get(seg, []))
            >= quicksum(c * v for c, v in cons.get(seg, []))
        )
    m.setObjective(quicksum(cut_cols[p]["stock"] * x[p] for p in range(n)), "minimize")
    m.optimize()
    st = m.getStatus()
    print(f"\n注入老软件段后 LP 松弛: {st}")
    if st == "optimal":
        print(f"  最优用料={m.getObjVal():.0f}  => 证明是候选生成粒度问题(需 pricing 动态补段)")
    else:
        print("  仍不可行 => 约束建模与老软件语义不一致，需重查约束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
