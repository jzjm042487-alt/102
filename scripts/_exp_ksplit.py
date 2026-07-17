"""实证Ⅴ（视角修正后）：段长作变量 + 段种类数最小化。

不逆向老软件的段。目标：在 利用率≥老软件 前提下，最小化解中用到的不同段长数。

核心思想（区别于之前失败的算术候选）：
  候选段 = 管的合法拆点（拆分自动满足"能组成整管"），拆点在整数格点上按步长
  step 离散化（步长小→段长近乎连续）。段能否密排母料由切侧 ILP 决定。
  联合 ILP：选拼法(定义段) + 切法(密排母料) + 段平衡，t_ℓ 标记段是否启用，
  目标 min Σt_ℓ（段种类最少）在 用料≤target 约束下求解。

用法: python scripts/_exp_ksplit.py <samples.json> <id前缀> [--step 4] [--tl 120] [--slack 0.0]
"""
from __future__ import annotations

import functools
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain import MaterialGroup, PipeDemand, parse_problem  # noqa: E402
from app.solver import _legal_pattern  # noqa: E402

# 复用主实验里的合并管型 + 老软件指标
sys.path.insert(0, str(ROOT / "scripts"))
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics  # noqa: E402


def enum_splits(pipe: PipeDemand, group: MaterialGroup, max_stock: int,
                step: int, max_pats_per_pipe: int = 4000):
    """枚举管的合法拆分（段长=拆点差）。拆点在 [step..L] 上按 step 网格。

    段长即变量：step 越小越接近连续。每段 ≤ max_stock；中间焊缝避禁区、
    满足 min_weld_distance；段数 ≤ max_joints+1；min_cut_length。
    """
    L = pipe.length
    maxseg = pipe.max_joints + 1
    min_wd = group.min_weld_distance
    min_cut = group.min_cut_length
    results: set[tuple[int, ...]] = set()

    def dfs(pos: int, nseg: int, path: tuple[int, ...]):
        if len(results) >= max_pats_per_pipe:
            return
        if pos == L and nseg >= 1:
            if _legal_pattern(pipe, list(path), min_wd, min_cut):
                results.add(path)
            return
        if nseg >= maxseg:
            return
        # 下一个拆点/终点
        # 末段直接到 L；中间段按 step 网格
        remain = L - pos
        # 直接收尾（整段到 L）
        seg_last = remain
        if seg_last <= max_stock and (nseg == 0 or seg_last >= min_cut):
            dfs(L, nseg + 1, path + (seg_last,))
        if nseg + 1 >= maxseg:
            return
        # 中间拆：段长 s 从 max(min_cut,min_wd,step) 起，步长 step
        lo = max(min_cut, min_wd if nseg >= 1 else 1, step)
        s = ((lo + step - 1) // step) * step
        while s <= min(max_stock, remain - 1):
            nc = pos + s
            is_first = (nseg == 0)
            # 内部段(非首非末)约束
            if not is_first and s < min_wd:
                s += step
                continue
            if not pipe.weld_allowed(nc):
                s += step
                continue
            dfs(nc, nseg + 1, path + (s,))
            s += step

    dfs(0, 0, ())
    return sorted(results)


def enum_cuts(group, sigma, kerf, max_pieces, max_trim, cap=60000):
    """在段集 sigma 上枚举密排切法（尾料≤max_trim）。

    配额按定尺**平均分配**：避免 DFS 在第一种定尺就耗尽全局 cap、导致其余
    定尺一个切法都生成不出（料紧且定尺多时会直接不可行）。
    """
    stock_lens = sorted({s.length for s in group.stocks})
    segs = sorted((s for s in sigma if s > 0), reverse=True)
    cols: dict[tuple[int, tuple], int] = {}
    per_len_cap = max(200, cap // max(1, len(stock_lens)))

    def rec(L, remain, pieces, start, counts, local):
        if local[0] >= per_len_cap:
            return
        used = L - remain
        if pieces >= 1 and (L - used) <= max_trim:
            key = (L, tuple(sorted(counts.items())))
            if key not in cols:
                cols[key] = 1
                local[0] += 1
        if pieces >= max_pieces:
            return
        for idx in range(start, len(segs)):
            s = segs[idx]
            need = s + (kerf if pieces >= 1 else 0)
            if need > remain:
                continue
            counts[s] = counts.get(s, 0) + 1
            rec(L, remain - need, pieces + 1, idx, counts, local)
            counts[s] -= 1
            if counts[s] == 0:
                del counts[s]

    for L in stock_lens:
        rec(L, L, 0, 0, {}, [0])
    return [(L, dict(c)) for (L, c) in cols]


def solve(group, step, kerf, target_len, slack, time_limit, verbose=True):
    from pyscipopt import Model, quicksum
    max_stock = max(s.length for s in group.stocks)
    bars_by_len = defaultdict(int)
    for s in group.stocks:
        bars_by_len[s.length] += s.quantity

    # 1) 每管型枚举拆分，收集候选段
    pipe_pats = []
    all_segs: set[int] = set()
    for pipe in group.pipes:
        pats = enum_splits(pipe, group, max_stock, step)
        if not pats:
            print(f"  管型 len={pipe.length} 无合法拆分")
            return None
        pipe_pats.append(pats)
        for p in pats:
            all_segs.update(p)
    if verbose:
        print(f"  拆分候选/管型: {[len(p) for p in pipe_pats]} 候选段总数={len(all_segs)}")

    # 2) 枚举切法（段集=all_segs）
    max_pieces = min(200, max_stock // max(1, min(all_segs)) + 2)
    cut_cols = enum_cuts(group, all_segs, kerf, max_pieces,
                         max_trim=max(600, group.min_cut_length))
    if verbose:
        print(f"  切法候选 {len(cut_cols)}（max_pieces={max_pieces}）")
    if not cut_cols:
        return None

    # 3) 联合 ILP：min 段种类，用料≤target*(1+slack)
    m = Model("ksplit")
    m.hideOutput()
    m.setParam("limits/time", time_limit)
    # 拼法选择 u[(i,j)] 整数（该管型用第 j 种拆法多少根）
    u = {}
    for i, pats in enumerate(pipe_pats):
        for j in range(len(pats)):
            u[(i, j)] = m.addVar(vtype="I", lb=0, name=f"u{i}_{j}")
    x = {ci: m.addVar(vtype="I", lb=0, name=f"x{ci}") for ci in range(len(cut_cols))}
    t = {s: m.addVar(vtype="B", name=f"t{s}") for s in all_segs}
    # 需求
    for i, pipe in enumerate(group.pipes):
        m.addCons(quicksum(u[(i, j)] for j in range(len(pipe_pats[i]))) == pipe.demand)
    # 段平衡：产(切) >= 耗(拼)
    prod = defaultdict(list)
    cons = defaultdict(list)
    for ci, (L, counts) in enumerate(cut_cols):
        for s, c in counts.items():
            prod[s].append((c, x[ci]))
    for i, pats in enumerate(pipe_pats):
        for j, p in enumerate(pats):
            cnt = defaultdict(int)
            for s in p:
                cnt[s] += 1
            for s, c in cnt.items():
                cons[s].append((c, u[(i, j)]))
    for s in all_segs:
        m.addCons(quicksum(c * v for c, v in prod.get(s, []))
                  - quicksum(c * v for c, v in cons.get(s, [])) >= 0)
    # 段启用：切法/拼法用到 s → t[s]=1（用大 M 关联 x/u）
    #   Σ_p(产s) ≤ BIG * t[s]；Σ(耗s) ≤ BIG*t[s]
    total_bars = sum(bars_by_len.values())
    BIG = total_bars * max_pieces + sum(p.demand for p in group.pipes) * (max(pp.max_joints for pp in group.pipes) + 1)
    for s in all_segs:
        m.addCons(quicksum(c * v for c, v in prod.get(s, [])) <= BIG * t[s])
    # 母料预算
    cbl = defaultdict(list)
    for ci, (L, _) in enumerate(cut_cols):
        cbl[L].append(ci)
    for L, pl in cbl.items():
        m.addCons(quicksum(x[ci] for ci in pl) <= bars_by_len[L])
    # 用料上界
    m.addCons(quicksum(cut_cols[ci][0] * x[ci] for ci in range(len(cut_cols)))
              <= int(target_len * (1 + slack)))
    # 目标：min 段种类
    m.setObjective(quicksum(t[s] for s in all_segs), "minimize")
    t0 = time.monotonic()
    m.optimize()
    if m.getNSols() == 0:
        print(f"  无解/超时（{time.monotonic()-t0:.1f}s, status={m.getStatus()}）")
        return None
    used_segs = {s for s in all_segs if m.getVal(t[s]) > 0.5}
    used_cut = [ci for ci in range(len(cut_cols)) if m.getVal(x[ci]) > 0.5]
    used_weld = [(i, j) for (i, j) in u if m.getVal(u[(i, j)]) > 0.5]
    used_len = sum(cut_cols[ci][0] * round(m.getVal(x[ci])) for ci in used_cut)
    cut_types = len({(cut_cols[ci][0], tuple(sorted(cut_cols[ci][1].items()))) for ci in used_cut})
    weld_types = len({pipe_pats[i][j] for (i, j) in used_weld})
    return {
        "seg_types": len(used_segs), "segs": sorted(used_segs),
        "cut_types": cut_types, "weld_types": weld_types,
        "used_len": used_len, "util": group.demand_length / used_len if used_len else 0,
        "time": time.monotonic() - t0, "status": str(m.getStatus()),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    path, pref = sys.argv[1], sys.argv[2]
    step = int(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 4
    tl = float(sys.argv[sys.argv.index("--tl") + 1]) if "--tl" in sys.argv else 120.0
    slack = float(sys.argv[sys.argv.index("--slack") + 1]) if "--slack" in sys.argv else 0.0
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
    for s in recs:
        sid = s.get("id") or s.get("ID") or ""
        if not sid.startswith(pref):
            continue
        prob = json.loads(s["MOMPROBLEMJSON"]) if "MOMPROBLEMJSON" in s else s.get("problem")
        group = merge_equivalent_pipes(parse_problem(prob).groups[0])
        _oracle, lm = legacy_alpha_and_metrics(s)
        print(f"id={sid[:12]} 管长={sorted({p.length for p in group.pipes})} "
              f"定尺={sorted({st.length for st in group.stocks})}")
        print(f"  老软件: util={lm['util']:.4f} cut_types={lm['cut_types']} weld_types={lm['weld_types']}")
        # target = 老软件用料（利用率持平口径）
        target = group.demand_length / lm["util"]
        kerf = group.blade_margin
        r = solve(group, step, kerf, target, slack, tl)
        if r is None:
            return
        def cmp(new, old, low=True):
            if abs(new - old) < 1e-9:
                return "[=]"
            return "[BETTER]" if ((new < old) == low) else "[WORSE]"
        print("  ── 段长作变量 + min 段种类 vs 老软件 ──")
        print(f"    利用率:  {r['util']:.4f} vs {lm['util']:.4f} {cmp(r['util'], lm['util'], low=False)}")
        print(f"    切法种类: {r['cut_types']} vs {lm['cut_types']} {cmp(r['cut_types'], lm['cut_types'])}")
        print(f"    拼法种类: {r['weld_types']} vs {lm['weld_types']} {cmp(r['weld_types'], lm['weld_types'])}")
        print(f"    段种类:  {r['seg_types']}  段={r['segs']}")
        print(f"    耗时 {r['time']:.1f}s status={r['status']}")
        return
    print("未找到样本")


if __name__ == "__main__":
    main()
