"""外层段长搜索：候选段来自"母料密排组合"（精确值，非算术整除）。

依据 §11.38 六/七 + 子代理 f0d342ce：
  - 段长可变问题=NP-hard MINLP，工业解法=外层选段长 + 内层精确密排。
  - 候选段来源：能"高密度平铺母料"的段组合（尾料≤tol），这些段自动满足密排。
  - 再用 min-段种类 ILP（_exp_ksplit.solve）让内层自选最少段，拼成各管型。

与之前失败的区别：候选段不是 S/n 整除值，而是"母料密排组合"的联合解
（例如 12000 内 a+b+c 尾料<200 → 自然产出 3960/3964/5620 这类非整除段）。

用法: python scripts/_exp_seggen.py <samples.json> <id前缀> [--tol 200] [--tl 120] [--slack 0.003]
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
sys.path.insert(0, str(ROOT / "scripts"))

from app.domain import parse_problem  # noqa: E402
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics  # noqa: E402
from _exp_ksplit import enum_cuts, solve  # noqa: E402


def candidate_segments(group, tol, grid, cap_seglen=None):
    """候选段池 = 能密排各定尺母料（尾料≤tol）的段长值集合。

    对每种母料 S，DFS 枚举段组合(段长∈grid 网格, 段数≤maxpieces)使尾料≤tol，
    收集其中出现的所有段长。段长上界 = min(max_stock, 最长管长)。这些段
    天然满足"密排"，同时受 min_cut/min_weld_distance/grid 约束。
    """
    stock_lens = sorted({s.length for s in group.stocks}, reverse=True)
    max_pipe = max(p.length for p in group.pipes)
    max_stock = stock_lens[0]
    hi = min(max_stock, cap_seglen or max_pipe)
    lo = max(group.min_cut_length, group.min_weld_distance, grid)
    lo = ((lo + grid - 1) // grid) * grid
    seg_vals: set[int] = set()

    # 限制搜索：段长网格值 g，DFS 尾料<=tol
    def dfs(S, remain, pieces, start, path, maxpieces):
        if len(seg_vals) > 4000:
            return
        if pieces >= 1 and remain <= tol:
            for s in path:
                seg_vals.add(s)
            # 不 return：继续放更多段也可能有效
        if pieces >= maxpieces:
            return
        # 段长从大到小，避免重复用 start 索引
        g = ((start) // grid) * grid
        g = max(g, lo)
        while g <= min(hi, remain):
            dfs(S, remain - g, pieces + 1, g, path + (g,), maxpieces)
            g += grid

    for S in stock_lens:
        maxpieces = min(30, S // lo)
        # start 从 hi 往下：用递减枚举控制组合爆炸
        s = (hi // grid) * grid
        while s >= lo:
            if s <= S:
                dfs(S, S - s, 1, s, (s,), maxpieces)
            s -= grid
    return seg_vals


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    path, pref = sys.argv[1], sys.argv[2]
    tol = int(sys.argv[sys.argv.index("--tol") + 1]) if "--tol" in sys.argv else 200
    tl = float(sys.argv[sys.argv.index("--tl") + 1]) if "--tl" in sys.argv else 120.0
    slack = float(sys.argv[sys.argv.index("--slack") + 1]) if "--slack" in sys.argv else 0.003

    import math
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
    for s in recs:
        sid = s.get("id") or s.get("ID") or ""
        if not sid.startswith(pref):
            continue
        prob = json.loads(s["MOMPROBLEMJSON"]) if "MOMPROBLEMJSON" in s else s.get("problem")
        group = merge_equivalent_pipes(parse_problem(prob).groups[0])
        _o, lm = legacy_alpha_and_metrics(s)
        # gcd 网格
        vals = [st.length for st in group.stocks] + [p.length for p in group.pipes]
        grid = math.gcd(*vals) if len(vals) > 1 else vals[0]
        grid = max(grid, 1)
        print(f"id={sid[:12]} 管长={sorted({p.length for p in group.pipes})} "
              f"定尺={sorted({st.length for st in group.stocks})} gcd网格={grid}")
        print(f"  老软件: util={lm['util']:.4f} cut_types={lm['cut_types']} weld_types={lm['weld_types']}")

        t0 = time.monotonic()
        cand = candidate_segments(group, tol, grid)
        print(f"  候选段池 |Σ_cand|={len(cand)}（tol={tol}, grid={grid}） 用时{time.monotonic()-t0:.1f}s")

        # 内层：在候选段池上 min 段种类 + 拼成各管 + 密排母料
        # 复用 _exp_ksplit.solve 但替换其候选段来源为 cand。
        # solve 内部按管型枚举拆分——这里改成：拆分只用 cand 里的段。
        r = solve_with_pool(group, cand, grid, group.demand_length / lm["util"], slack, tl)
        if r is None:
            print("  无解")
            return
        def cmp(new, old, low=True):
            if abs(new - old) < 1e-9:
                return "[=]"
            return "[BETTER]" if ((new < old) == low) else "[WORSE]"
        print("  ── 外层自生成段 + min 段种类 vs 老软件 ──")
        print(f"    利用率:  {r['util']:.4f} vs {lm['util']:.4f} {cmp(r['util'], lm['util'], low=False)}")
        print(f"    切法种类: {r['cut_types']} vs {lm['cut_types']} {cmp(r['cut_types'], lm['cut_types'])}")
        print(f"    拼法种类: {r['weld_types']} vs {lm['weld_types']} {cmp(r['weld_types'], lm['weld_types'])}")
        print(f"    段种类:  {r['seg_types']}  段={r['segs']}")
        print(f"    耗时 {r['time']:.1f}s status={r['status']}")
        return
    print("未找到样本")


def solve_with_pool(group, cand, grid, target_len, slack, time_limit, verbose=True):
    """在给定候选段池 cand 上：拼法=用cand段拼成各管；切法=cand段密排母料；
    目标 min 段种类；约束 用料≤target*(1+slack)。"""
    from pyscipopt import Model, quicksum
    from app.solver import _legal_pattern
    import itertools

    max_stock = max(s.length for s in group.stocks)
    bars = defaultdict(int)
    for st in group.stocks:
        bars[st.length] += st.quantity
    cand = sorted(cand)

    # 每管型：用 cand 段拼成（Σ=L，段数≤maxseg，合法）
    pipe_pats = []
    for pipe in group.pipes:
        L = pipe.length
        maxseg = pipe.max_joints + 1
        pats = set()
        # DFS 用 cand 段组合到 L
        def dfs(remain, nseg, path):
            if len(pats) > 20000:
                return
            if remain == 0 and nseg >= 1:
                for perm in set(itertools.permutations(path)):
                    if _legal_pattern(pipe, list(perm), group.min_weld_distance, group.min_cut_length):
                        pats.add(perm)
                        break
                return
            if nseg >= maxseg:
                return
            for seg in cand:
                if seg <= remain and (not path or seg <= path[-1]):
                    dfs(remain - seg, nseg + 1, path + (seg,))
        dfs(L, 0, ())
        if not pats:
            print(f"  管型 len={pipe.length} 用候选段拼不出")
            return None
        pipe_pats.append(sorted(pats))
    used_segs_in_pats = set()
    for pats in pipe_pats:
        for p in pats:
            used_segs_in_pats.update(p)
    if verbose:
        print(f"  各管型拼法数={[len(p) for p in pipe_pats]}  拼法涉及段={len(used_segs_in_pats)}")

    # 切法：只用拼法涉及到的段（否则切出来没法拼）
    sigma = sorted(used_segs_in_pats)
    max_pieces = min(40, max_stock // max(1, min(sigma)) + 2)
    cuts = enum_cuts(group, sigma, group.blade_margin, max_pieces,
                     max_trim=max(600, group.min_cut_length))
    if verbose:
        print(f"  切法候选={len(cuts)}")
    if not cuts:
        return None

    m = Model("seggen")
    m.hideOutput()
    m.setParam("limits/time", time_limit)
    u = {}
    for i, pats in enumerate(pipe_pats):
        for j in range(len(pats)):
            u[(i, j)] = m.addVar(vtype="I", lb=0, name=f"u{i}_{j}")
    x = {ci: m.addVar(vtype="I", lb=0) for ci in range(len(cuts))}
    t = {seg: m.addVar(vtype="B") for seg in sigma}
    for i, pipe in enumerate(group.pipes):
        m.addCons(quicksum(u[(i, j)] for j in range(len(pipe_pats[i]))) == pipe.demand)
    prod = defaultdict(list)
    cons = defaultdict(list)
    for ci, (L, cc) in enumerate(cuts):
        for seg, c in cc.items():
            prod[seg].append((c, x[ci]))
    for i, pats in enumerate(pipe_pats):
        for j, p in enumerate(pats):
            cnt = defaultdict(int)
            for seg in p:
                cnt[seg] += 1
            for seg, c in cnt.items():
                cons[seg].append((c, u[(i, j)]))
    for seg in sigma:
        m.addCons(quicksum(c * v for c, v in prod.get(seg, [])) - quicksum(c * v for c, v in cons.get(seg, [])) >= 0)
    total_bars = sum(bars.values())
    BIG = total_bars * max_pieces + sum(p.demand for p in group.pipes) * (max(pp.max_joints for pp in group.pipes) + 1)
    for seg in sigma:
        m.addCons(quicksum(c * v for c, v in prod.get(seg, [])) <= BIG * t[seg])
    cbl = defaultdict(list)
    for ci, (L, _) in enumerate(cuts):
        cbl[L].append(ci)
    for L, pl in cbl.items():
        m.addCons(quicksum(x[ci] for ci in pl) <= bars[L])
    m.addCons(quicksum(cuts[ci][0] * x[ci] for ci in range(len(cuts))) <= int(target_len * (1 + slack)))
    m.setObjective(quicksum(t[seg] for seg in sigma), "minimize")
    t0 = time.monotonic()
    m.optimize()
    if m.getNSols() == 0:
        print(f"  无解/超时（{time.monotonic()-t0:.1f}s, status={m.getStatus()}）")
        return None
    used_segs = {seg for seg in sigma if m.getVal(t[seg]) > 0.5}
    used_cut = [ci for ci in range(len(cuts)) if m.getVal(x[ci]) > 0.5]
    used_weld = [(i, j) for (i, j) in u if m.getVal(u[(i, j)]) > 0.5]
    used_len = sum(cuts[ci][0] * round(m.getVal(x[ci])) for ci in used_cut)
    cut_types = len({(cuts[ci][0], tuple(sorted(cuts[ci][1].items()))) for ci in used_cut})
    weld_types = len({pipe_pats[i][j] for (i, j) in used_weld})
    return {
        "seg_types": len(used_segs), "segs": sorted(used_segs),
        "cut_types": cut_types, "weld_types": weld_types,
        "used_len": used_len, "util": group.demand_length / used_len if used_len else 0,
        "time": time.monotonic() - t0, "status": str(m.getStatus()),
    }


if __name__ == "__main__":
    main()
