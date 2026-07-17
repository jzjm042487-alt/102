"""实证Ⅰ：拿老软件解里的真实段长字母表回灌 route3，看切法/拼法能否压到 <= 老软件。

不改生产代码，直接复用 route3_setcover 的 ILP，只把"候选段长的来源"换成：
  (a) route3 原生 per-pipe 枚举字母表  (baseline of route3)
  (b) 老软件解里出现的真实段长字母表    (oracle：给它答案的段，看它能不能少切少拼)

对每个样本打印：字母表大小、切法种类、拼法种类、利用率、verifier 通过否。
若 (b) 明显把切法/拼法压到 <= 老软件且利用率保住 => 病根确认在"候选段来源"。

用法: python scripts/_exp_alphabet.py <samples.json> [--n 8]
"""

from __future__ import annotations

import functools
import json
import sys
from collections import defaultdict
from pathlib import Path

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain import parse_problem  # noqa: E402
from app import route3_setcover as R3  # noqa: E402
from app.service import solve_and_verify  # noqa: E402


def _parts(s):
    return [int(float(t)) for t in str(s).split() if t.strip()]


def legacy_alphabet(sample) -> set[int]:
    """老软件解 CuttingPattern 里出现的所有段长（乘以长度缩放前的整数 mm）。"""
    res = sample.get("legacy") or sample.get("MOMRESULTJSON")
    if isinstance(res, str):
        res = json.loads(res)
    R = res.get("Result", {})
    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    seg = set()
    for c in cp:
        seg.update(_parts(c.get("Part", "")))
    return seg


def legacy_metrics(sample):
    res = sample.get("legacy") or sample.get("MOMRESULTJSON")
    if isinstance(res, str):
        res = json.loads(res)
    gi = res.get("GeneralInfo", {})
    R = res.get("Result", {})
    cp = (R.get("CuttingPattern") or {}).get("CuttingPipe") or []
    wp = (R.get("WeldingPattern") or {}).get("WeldingPipe") or []
    cut_types = len({(int(float(c.get("Length"))), tuple(_parts(c.get("Part", "")))) for c in cp})
    weld_types = len({(w.get("FigureNumber"), tuple(_parts(p.get("Part", ""))))
                      for w in wp for p in w.get("Pattern", [])})
    return {"util": float(gi.get("UtilRate", 0)), "cut_types": cut_types, "weld_types": weld_types}


def build_weld_cands_from_alphabet(group, alphabet: set[int]):
    """把每根管的拼法候选限制为：只用 alphabet 里的段拼出管长(<=3段)，外加整根(若<=最长母料)。

    与 R3._build_weld_candidates 同样走 _legal_pattern 合法性校验，
    但分割点必须落在给定字母表上（这正是'共享小字母表'假设的核心）。
    """
    from app.solver import _legal_pattern
    max_stock = max(s.length for s in group.stocks)
    A = sorted(a for a in alphabet if 0 < a <= max_stock)
    Aset = set(A)
    cands = {}
    for i, pipe in enumerate(group.pipes):
        pats = set()
        if pipe.length <= max_stock and pipe.length in Aset:
            pats.add((pipe.length,))
        elif pipe.length <= max_stock:
            # 整根成品也允许（老软件里整管本就是一种"段"）
            pats.add((pipe.length,))
        # 2 段：两段都必须在字母表内
        if pipe.max_joints >= 1:
            for a in A:
                b = pipe.length - a
                if b <= 0 or b > max_stock or b not in Aset:
                    continue
                parts = (a, b)
                if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
                    pats.add(tuple(parts))
        # 3 段：三段都必须在字母表内
        if pipe.max_joints >= 2 and len(A) <= 200:
            for a in A:
                for bmid in A:
                    c = pipe.length - a - bmid
                    if c <= 0 or c > max_stock or c not in Aset:
                        continue
                    parts = (a, bmid, c)
                    if max(parts) > max_stock:
                        continue
                    if _legal_pattern(pipe, parts, group.min_weld_distance, group.min_cut_length):
                        pats.add(tuple(parts))
        if not pats:
            return None  # 字母表拼不出这根管 -> oracle 失败信号
        cands[i] = sorted(pats, key=lambda p: (len(p), p))
    return cands


def run_route3_with_alphabet(group, alphabet, time_limit):
    """复用 R3 的 ILP，但用给定字母表构造拼法候选 + 切法列。返回诊断 dict。"""
    weld_cands = build_weld_cands_from_alphabet(group, alphabet)
    if weld_cands is None:
        return {"stage": "weld_cands", "reason": "字母表拼不出某根管"}
    used_alpha = set()
    for pats in weld_cands.values():
        for p in pats:
            used_alpha.update(p)
    stock_lengths = [s.length for s in group.stocks]
    kerf = group.blade_margin
    # 自适应 max_pieces：短段多的组需要每根母料放很多段（老软件实测可达23段）
    smallest = min(used_alpha) if used_alpha else 1
    max_stock = max(stock_lengths)
    adaptive_mp = max(R3.DEFAULT_MAX_PIECES, min(30, max_stock // max(smallest, 1) + 1))
    cols = R3._enumerate_cut_columns(
        sorted(used_alpha), stock_lengths, kerf,
        R3.DEFAULT_COL_CAP, 500, adaptive_mp,
    )
    cols = R3._ensure_coverage(cols, sorted(used_alpha), stock_lengths, kerf, adaptive_mp)
    if not cols:
        return {"stage": "cut_cols", "reason": "无切法列", "alpha": len(used_alpha)}
    res = R3._solve_two_phase(group, weld_cands, cols, time_limit / 2, time_limit / 2)
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
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    samples = data if isinstance(data, list) else data.get("samples") or data.get("RECORDS")

    picked = 0
    print(f"{'id':10s} | {'alpha':>5} | {'L cut/weld':>10} | {'R3o cut/weld':>12} | {'util L/R3o':>16} | done")
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
        alpha = legacy_alphabet(s)
        if not alpha:
            continue
        picked += 1
        sid = (s.get("id") or s.get("ID") or "?")[:10]
        print(f"[{picked}/{n}] {sid} bars={bars} alpha_raw={len(alpha)} solving...", flush=True)
        try:
            out = run_route3_with_alphabet(group, alpha, tl)
        except Exception as e:
            import traceback
            out = {"stage": "exc", "err": str(e)[:60]}
            traceback.print_exc()
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
