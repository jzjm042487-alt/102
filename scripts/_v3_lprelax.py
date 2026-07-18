"""快速探针: 解 arc-flow 主模型的 LP 松弛(所有变量连续), 看能否秒达理论最优利用率。
若 LP 达 0.99 而 MILP 慢 -> 模型对, 是整数搜索慢(需 warmstart/对称破除)。
若 LP 也达不到 -> 段候选缺失(建图问题)。

用法: python scripts/_v3_lprelax.py <level> [--step N]
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import build_pipe_arcs


def lp_relax(group, step=None, verbose=True):
    from pyscipopt import Model, quicksum
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    max_stock = max(s.length for s in group.stocks)
    stock_qty = defaultdict(int)
    for st in group.stocks:
        stock_qty[st.length] += st.quantity
    pipes = group.pipes
    if step is None:
        step = max(1, min_wd)
    stock_lens = sorted(stock_qty.keys())
    pipe_graphs = {}
    seg_lengths = set()
    for i, p in enumerate(pipes):
        pts, arcs = build_pipe_arcs(p, max_stock, min_cut, min_wd, step, stock_lens)
        pipe_graphs[i] = (pts, arcs)
        for (_, _, seg) in arcs:
            seg_lengths.add(seg)
    seg_sorted = sorted(seg_lengths)
    na = sum(len(a) for (_, a) in pipe_graphs.values())
    print(f"  拼层(step={step}): 弧={na} 段长种类={len(seg_lengths)}", flush=True)

    m = Model("lp")
    m.hideOutput()
    g = {}
    for i, (pts, arcs) in pipe_graphs.items():
        g[i] = {(a, b): m.addVar(vtype="C", lb=0) for (a, b, seg) in arcs}
    for i, (pts, arcs) in pipe_graphs.items():
        Li = pipes[i].length
        of, inf = defaultdict(list), defaultdict(list)
        for (a, b, seg) in arcs:
            of[a].append(g[i][(a, b)]); inf[b].append(g[i][(a, b)])
        for pos in pts:
            if pos == 0:
                m.addCons(quicksum(of[pos]) == pipes[i].demand)
            elif pos == Li:
                m.addCons(quicksum(inf[pos]) == pipes[i].demand)
            else:
                m.addCons(quicksum(inf[pos]) == quicksum(of[pos]))
    f = {}; used_vars = {}
    for L in stock_qty:
        f[L] = {}
        reach = {0}; fr = {0}
        while fr:
            nf = set()
            for a in fr:
                for seg in seg_sorted:
                    nb = a + seg
                    if nb <= L and nb not in reach:
                        nf.add(nb)
            reach |= nf; fr = nf
        nodes = sorted(reach)
        for a in nodes:
            for seg in seg_sorted:
                b = a + seg
                if b <= L and b in reach:
                    f[L][(a, b)] = m.addVar(vtype="C", lb=0)
        waste = {a: m.addVar(vtype="C", lb=0) for a in nodes if a < L}
        of, inf = defaultdict(list), defaultdict(list)
        for (a, b) in f[L]:
            of[a].append(f[L][(a, b)]); inf[b].append(f[L][(a, b)])
        for a, wv in waste.items():
            of[a].append(wv); inf[L].append(wv)
        used = m.addVar(vtype="C", lb=0, ub=stock_qty[L])
        for pos in nodes:
            if pos == 0:
                m.addCons(quicksum(of[pos]) == used)
            elif pos == L:
                m.addCons(quicksum(inf[pos]) == used)
            else:
                m.addCons(quicksum(inf[pos]) == quicksum(of[pos]))
        used_vars[L] = used
    for seg in seg_sorted:
        pt = [f[L][(a, b)] for L in stock_qty for (a, b) in f[L] if b - a == seg]
        ct = [g[i][(a, b)] for i, (_, arcs) in pipe_graphs.items()
              for (a, b, s2) in arcs if s2 == seg]
        if pt or ct:
            m.addCons(quicksum(pt) - quicksum(ct) >= 0)
    usedlen = quicksum(used_vars[L] * L for L in stock_qty)
    m.setObjective(usedlen, "minimize")
    t0 = time.time()
    m.optimize()
    u = m.getObjVal()
    util = group.demand_length / u if u else 0
    print(f"  LP松弛: 用料={u:.0f} util={util:.4f} ({time.time()-t0:.1f}s) {m.getStatus()}",
          flush=True)


def main():
    lv = int(sys.argv[1])
    step = int(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else None
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    s = next(x for x in samples if x["level"] == lv)
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    print(f"L{lv} {s['spec']} 老:{s['legacy']}", flush=True)
    lp_relax(g, step=step)


if __name__ == "__main__":
    main()
