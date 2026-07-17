"""实证Ⅱ：不碰老软件答案，从"管长 ⊖ 定尺组合"派生共享字母表，跑 route3 ILP。

依据（§11.32 四）：Ágoston 2019 等价 CSP —— 拼接分割点落在"母料边界导出位置"，
即 段长 ∈ { 定尺组合 } ∪ { 管长 − 定尺组合 } ∪ { 管长本身 }。任意毫米、有限集、切拼共用。

与 _exp_alphabet.py（oracle）唯一的区别：字母表由 derive_alphabet() 生成，不读 legacy。
其余 ILP / verifier / 指标口径完全一致，可直接对比 oracle 与老软件。

用法: python scripts/_exp_derived_alphabet.py <samples.json> [--n 8] [--tl 30] [--cap N]
"""

from __future__ import annotations

import functools
import json
import sys
from collections import defaultdict
from itertools import combinations_with_replacement
from pathlib import Path

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain import parse_problem  # noqa: E402
from app import route3_setcover as R3  # noqa: E402

# 复用 oracle 脚本里已验证的 ILP 包装 + 指标口径，只换字母表来源。
from _exp_alphabet import (  # noqa: E402
    build_weld_cands_from_alphabet,
    legacy_alphabet,
    legacy_metrics,
)


def derive_alphabet(group, max_stock_terms: int = 2, cap: int = 400) -> set[int]:
    """从输入本身派生共享字母表（不含任何解信息）。

    元素来源（全为任意毫米整数，非网格）：
      S    = 各库存定尺
      SC   = 至多 max_stock_terms 根定尺之和（等价母料长度）
      pipe = 各成品管长（整根成品也是一种段）
      P⊖SC = 管长 − 定尺组合（拼接残段：管子比某个定尺/等价母料长时的补段）

    仅保留 0 < x <= 最长定尺 的值（单段不可能超过一根母料）。
    cap：字母表硬上界（对齐老软件 p50≈18~几十），超了按"出现价值"截断。
    """
    stock_lens = sorted({s.length for s in group.stocks})
    max_stock = max(stock_lens)
    pipe_lens = sorted({p.length for p in group.pipes})

    # 定尺组合（等价母料候选长度）
    stock_combos: set[int] = set(stock_lens)
    for k in range(2, max_stock_terms + 1):
        for combo in combinations_with_replacement(stock_lens, k):
            stock_combos.add(sum(combo))

    alpha: set[int] = set()
    # 单根定尺本身可作为一段
    alpha.update(s for s in stock_lens if 0 < s <= max_stock)
    for pl in pipe_lens:
        # 整根成品
        if 0 < pl <= max_stock:
            alpha.add(pl)
        # 管长 − 定尺组合 = 拼接残段
        for sc in stock_combos:
            r = pl - sc
            if 0 < r <= max_stock:
                alpha.add(r)
            # 对称：定尺组合 − 管长（切掉一根管后母料残段）
            r2 = sc - pl
            if 0 < r2 <= max_stock:
                alpha.add(r2)

    if len(alpha) <= cap:
        return alpha
    # 超上界：优先保留"离散频次高"的值——用 与管长/定尺的接近度 打分，取前 cap 个
    scored = sorted(alpha, key=lambda x: min(abs(x - pl) for pl in pipe_lens + stock_lens))
    return set(scored[:cap])


def run(group, alphabet, tl):
    weld_cands = build_weld_cands_from_alphabet(group, alphabet)
    if weld_cands is None:
        return {"stage": "weld_cands", "reason": "派生字母表拼不出某根管"}
    used_alpha = set()
    for pats in weld_cands.values():
        for p in pats:
            used_alpha.update(p)
    stock_lengths = [s.length for s in group.stocks]
    kerf = group.blade_margin
    smallest = min(used_alpha) if used_alpha else 1
    max_stock = max(stock_lengths)
    adaptive_mp = max(R3.DEFAULT_MAX_PIECES, min(30, max_stock // max(smallest, 1) + 1))
    cols = R3._enumerate_cut_columns(
        sorted(used_alpha), stock_lengths, kerf, R3.DEFAULT_COL_CAP, 500, adaptive_mp,
    )
    cols = R3._ensure_coverage(cols, sorted(used_alpha), stock_lengths, kerf, adaptive_mp)
    if not cols:
        return {"stage": "cut_cols", "reason": "无切法列", "alpha": len(used_alpha)}
    res = R3._solve_two_phase(group, weld_cands, cols, tl / 2, tl / 2)
    if res is None:
        return {"stage": "ilp", "reason": "ILP 无解/超时", "alpha": len(used_alpha), "ncols": len(cols)}
    weld_counts, cut_counts = R3._reconcile_to_counts(group, res)
    if not weld_counts or not cut_counts:
        return {"stage": "reconcile", "reason": "reconcile 空", "alpha": len(used_alpha)}
    produced = defaultdict(int)
    for cand, qty in weld_counts:
        produced[cand.pipe_index] += qty
    incomplete = [i for i, pipe in enumerate(group.pipes) if produced.get(i, 0) != pipe.demand]
    cut_types = len({(c.stock_length, c.parts) for c, _ in cut_counts})
    weld_types = len({c.parts for c, _ in weld_counts})
    used_len = sum(c.stock_length * q for c, q in cut_counts)
    util = group.demand_length / used_len if used_len else 0
    return {"stage": "done", "complete": not incomplete, "n_incomplete": len(incomplete),
            "cut_types": cut_types, "weld_types": weld_types,
            "util": util, "alpha": len(used_alpha), "ncols": len(cols)}


def main():
    path = sys.argv[1]
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 8
    tl = float(sys.argv[sys.argv.index("--tl") + 1]) if "--tl" in sys.argv else 30.0
    cap = int(sys.argv[sys.argv.index("--cap") + 1]) if "--cap" in sys.argv else 400
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    samples = data if isinstance(data, list) else data.get("samples") or data.get("RECORDS")

    picked = 0
    for s in samples:
        if picked >= n:
            break
        prob = s.get("problem")
        if prob is None and "MOMPROBLEMJSON" in s:
            prob = json.loads(s["MOMPROBLEMJSON"])
        if not prob:
            continue
        try:
            lm = legacy_metrics(s)
        except Exception:
            continue
        if not (0.5 < lm["util"] < 1.05):
            continue
        try:
            problem = parse_problem(prob)
        except Exception:
            continue
        if len(problem.groups) != 1:
            continue
        group = problem.groups[0]
        bars = sum(st.quantity for st in group.stocks)
        if not (100 <= bars <= 2000):
            continue
        stock_len = sum(st.length * st.quantity for st in group.stocks)
        tight = group.demand_length / stock_len if stock_len else 0
        if not (0.99 <= tight < 0.999):
            continue
        # 只挑与 oracle 同批（有 legacy 字母表可对照）
        oracle_alpha = legacy_alphabet(s)
        if not oracle_alpha:
            continue
        picked += 1
        sid = (s.get("id") or s.get("ID") or "?")[:10]
        derived = derive_alphabet(group, cap=cap)
        print(f"[{picked}/{n}] {sid} bars={bars} | oracle_alpha={len(oracle_alpha)} "
              f"derived_alpha={len(derived)} solving...", flush=True)
        try:
            out = run(group, derived, tl)
        except Exception as e:
            import traceback
            traceback.print_exc()
            out = {"stage": "exc", "err": str(e)[:80]}
        if out.get("stage") == "done":
            print(f"  -> DONE complete={out['complete']} (miss={out['n_incomplete']}) "
                  f"alpha={out['alpha']} ncols={out['ncols']} | "
                  f"cut {lm['cut_types']}->{out['cut_types']}  weld {lm['weld_types']}->{out['weld_types']} | "
                  f"util {lm['util']:.4f}->{out['util']:.4f}")
        else:
            print(f"  -> FAIL stage={out.get('stage')} {out.get('reason') or out.get('err')} "
                  f"| legacy cut/weld {lm['cut_types']}/{lm['weld_types']} util {lm['util']:.4f}")


if __name__ == "__main__":
    main()
