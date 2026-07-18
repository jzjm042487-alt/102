"""V3 arc-flow 全局整数模型(治本, 弃列生成打补丁路线) —— 独立模块, 不碰生效代码。

思想(见设计文档 §11.45):
  段长不再预派生字母表, 而是'位置图上的弧'(段长=两端位置差, 天然毫米级连续)。
  两张图 + 一个耦合:
    切层(母料 arc-flow): 每种定尺 L, 节点=0..L, 弧(a,b)=切出段 b-a; 流守恒;
                         源汇流量=用的该定尺根数 <= qty_L。
    拼层(管身 arc-flow): 每种管型 i, 节点={0,Li}∪合法焊点(排除禁焊区),
                         弧(a,b)=[a,b]由一段长 b-a 的料充当(b-a<=max_stock);
                         源汇流量=demand_i; 路径弧数-1=焊口, 受 max_joints 限。
    耦合(段供需): 每段长 ℓ, 切层产出(ℓ) >= 拼层消耗(ℓ)。
  目标(字典序): Pass1 min 用料; Pass2 固定用料<=U*, min 总焊口。

关键: 段长集合 = 拼层管身图弧长的并集(合法焊点两两差, 几何完备, 非算术派生)。
      切层只需为这些段长在母料上建弧 -> 两层共享同一段长弧集, 天然闭环。

用法: python scripts/_arcflow_v3.py <level> [--tl 120]
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
from backend.app.solver import _legal_pattern
from scripts._exp_colgen import merge_equivalent_pipes

# 切层弧数超此阈值时, 自动改用切层列生成(避免 SCIP 被百万级对称弧压垮)。
# 松料/中等档远低于此值 -> 走原 arc-flow, 零回归; 仅极限档(L12/L13)触发。
CUTCG_ARC_THRESHOLD = 200_000


def _weld_points(pipe, step=1, stock_lens=()):
    """管身候选焊点(升序): 排除禁焊区。含端点 0 与 length。
    step>1 时按 step 网格取候选点(落到最近合法焊点), 大幅缩减 O(L²) 弧数。
    治本依据: 相邻焊口必须间隔>=min_wd, 故 step<=min_wd 时不丢任何合法路径。

    stock_lens: 母料长度集合。为让段能'恰好填满母料'(利用率治本),
      额外并入母料边界诱导切点: {k*S} 与 {L-k*S}(落到最近合法焊点)。
      这些是几何诱导位置(母料铺砌), 非算术字母表爆炸 —— 每个 (S,k) 仅一点。
    """
    L = pipe.length

    def snap(pos):
        """把 pos 落到最近合法焊点(半径 step 内), 失败返回 None。"""
        if 0 < pos < L and pipe.weld_allowed(pos):
            return pos
        for d in range(1, max(2, step)):
            for c in (pos + d, pos - d):
                if 0 < c < L and pipe.weld_allowed(c):
                    return c
        return None

    if step <= 1:
        pts = {0, L}
        for pos in range(1, L):
            if pipe.weld_allowed(pos):
                pts.add(pos)
    else:
        pts = {0, L}
        pos = step
        while pos < L:
            s = snap(pos)
            if s is not None:
                pts.add(s)
            pos += step
    # 母料边界诱导切点(利用率治本: 让段能恰好填满母料)。
    # 关键: 段拼齐母料边界的位置是'母料长在管身坐标上的位置'。
    # 对每根管 L 与母料 S: 加入 {k*S}(第 k 根母料切满处) 与 {L-k*S}(倒数补齐处),
    # 以及模位置 {k*S mod L}(母料边界落在下一管身何处)。
    # k 上界 = 管跨越的母料根数 + 1(不是无脑扫 200, 避免碎段污染 -> 段种类爆炸)。
    for S in stock_lens:
        kmax = L // S + 2
        for k in range(1, kmax + 1):
            cands = []
            if k * S < L:
                cands.append(k * S)
                cands.append(L - k * S)
            r = (k * S) % L
            if 0 < r < L:
                cands.append(r)
                cands.append(L - r)
            for raw in cands:
                sp = snap(raw)
                if sp is not None:
                    pts.add(sp)
    return sorted(pts)


def build_pipe_arcs(pipe, max_stock, min_cut, min_wd, step=1, stock_lens=()):
    """拼层管身图弧集: 弧(a,b), a<b 均为候选焊点(或端点), 段长 b-a<=max_stock。
    额外约束: 段长 >= max(min_cut,1)(可切段); 两内部焊口之间段 >= min_wd。
    首段(a=0)和尾段(b=L)不受 min_wd 下界限制(端点不是焊口)。
    step: 焊点网格粒度(见 _weld_points), <=min_wd 时不丢合法性。
    stock_lens: 母料长度集合, 用于并入母料边界诱导切点(利用率治本)。
    返回 (nodes, arcs) —— nodes: 升序位置列表; arcs: list[(a,b,seglen)]。
    """
    L = pipe.length
    pts = _weld_points(pipe, step, stock_lens)
    min_seg = max(min_cut, 1)
    arcs = []
    n = len(pts)
    for ai in range(n):
        a = pts[ai]
        for bi in range(ai + 1, n):
            b = pts[bi]
            seg = b - a
            if seg > max_stock:
                break  # pts 升序, 再往后只会更长
            if seg < min_seg:
                continue
            # 内焊段(两端都是内部焊口)需满足最小焊距
            if a > 0 and b < L and seg < min_wd:
                continue
            arcs.append((a, b, seg))
    return pts, arcs


def _cut_arc_count(stock_qty, seg_sorted):
    """估算规范化切层总弧数(不建变量, 仅计数), 用于决定是否切换列生成。
    复用与 solve_arcflow 相同的非增序规范可达枚举。"""
    if not seg_sorted:
        return 0
    INF = max(seg_sorted)
    total = 0
    for L in stock_qty:
        maxseg = {0: INF}
        order = [0]
        idx = 0
        while idx < len(order):
            a = order[idx]; idx += 1
            cap = maxseg[a]
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + seg
                if b > L:
                    continue
                if b not in maxseg:
                    maxseg[b] = seg
                    order.append(b)
                elif seg > maxseg[b]:
                    maxseg[b] = seg
        node_set = set(maxseg)
        for a, cap in maxseg.items():
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + seg
                if b <= L and b in node_set:
                    total += 1
            if total > CUTCG_ARC_THRESHOLD * 4:
                return total  # 早停: 已远超阈值, 无需精确计数
    return total


def classify_group(group, tl=15.0, verbose=False):
    """前置分类器: 决定该组走纯切快路还是焊接求解器。

    分类不能只看几何(见 docs/corpus-profile.md): 即便几何可纯切, 纯切利用率
    也常达不到目标, 老软件靠焊接顶利用率。故先做几何预筛, 再用一次纯切装箱
    判定'纯切能否达标'。

    返回 dict:
      kind          : "infeasible" | "pure_cut" | "needs_weld"
      reason        : 人类可读判据。
      geom          : 几何子类(all_stock_fit / some_stock_fit / all_stock_short / infeasible_len)。
      u_cut         : 纯切最优利用率(若跑了纯切装箱), 否则 None。
      pure_result   : 纯切解(kind=="pure_cut" 时直接复用), 否则 None。
    """
    max_pipe = max(p.length for p in group.pipes)
    min_stock = min(s.length for s in group.stocks)
    max_stock = max(s.length for s in group.stocks)
    if group.stock_length < group.demand_length:
        return {"kind": "infeasible", "geom": "infeasible_len",
                "reason": f"料总长 {group.stock_length} < 需求总长 {group.demand_length}",
                "u_cut": None, "pure_result": None}
    if max_stock < max_pipe:
        return {"kind": "needs_weld", "geom": "all_stock_short",
                "reason": f"全部库存 < 最长管 {max_pipe}, 必须切分+焊接",
                "u_cut": None, "pure_result": None}
    geom = "all_stock_fit" if min_stock >= max_pipe else "some_stock_fit"
    # 几何可纯切: 跑一次纯切装箱, 看利用率能否达标。
    pure = solve_pure_cut(group, tl=tl, verbose=verbose)
    if pure is None:
        # 纯切装箱无解(受限纯切下短库存装不下长管等) -> 交给焊接求解器。
        return {"kind": "needs_weld", "geom": geom,
                "reason": "纯切装箱无可行解 -> 焊接求解器", "u_cut": None,
                "pure_result": None}
    u_cut = pure["util"]
    if u_cut >= group.target_rate - 1e-9:
        return {"kind": "pure_cut", "geom": geom,
                "reason": f"纯切利用率 {u_cut:.4f} >= 目标 {group.target_rate:.4f}",
                "u_cut": u_cut, "pure_result": pure}
    return {"kind": "needs_weld", "geom": geom,
            "reason": f"纯切利用率 {u_cut:.4f} < 目标 {group.target_rate:.4f} -> 焊接顶利用率",
            "u_cut": u_cut, "pure_result": pure}


def solve_pure_cut(group, tl=30.0, verbose=True):
    """纯切装箱(无焊口): 每根需求管整根切下, 只解一维 cutting-stock。
    段集 = 各管型整根长度; 切层 arc-flow(规范非增序) min 用料。
    不建拼层焊口变量 -> 极快。返回与 solve_arcflow 同构的解 dict, 或 None(不可行)。
    """
    from pyscipopt import Model, quicksum, SCIP_PARAMEMPHASIS
    pipes = group.pipes
    max_stock = max(s.length for s in group.stocks)
    stock_qty = defaultdict(int)
    for st in group.stocks:
        stock_qty[st.length] += st.quantity
    # 段 = 管整根长(能被至少一种库存装下的才有意义)。
    seg_demand = defaultdict(int)  # seg_len -> 需求根数
    for p in pipes:
        if p.length > max_stock:
            return None  # 有管比最长库存还长, 纯切不可能
        seg_demand[p.length] += p.demand
    seg_sorted = sorted(seg_demand)
    INF = max(seg_sorted)

    m = Model("purecut")
    m.hideOutput()
    m.setParam("limits/time", tl)
    kerf = group.blade_margin
    f = {}
    fseg = {}
    used_vars = {}
    waste = {}
    seg_prod = defaultdict(list)  # seg_len -> [切层弧变量]
    for L in stock_qty:
        f[L] = {}
        fseg[L] = {}
        maxseg = {0: INF}
        order = [0]
        idx = 0
        while idx < len(order):
            a = order[idx]; idx += 1
            cap = maxseg[a]
            step_kerf = 0 if a == 0 else kerf
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + step_kerf + seg
                if b > L:
                    continue
                if b not in maxseg:
                    maxseg[b] = seg
                    order.append(b)
                elif seg > maxseg[b]:
                    maxseg[b] = seg
        nodes = sorted(maxseg)
        node_set = set(nodes)
        for a in nodes:
            cap = maxseg[a]
            step_kerf = 0 if a == 0 else kerf
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + step_kerf + seg
                if b <= L and b in node_set:
                    var = m.addVar(vtype="I", lb=0, name=f"f_{L}_{a}_{b}")
                    f[L][(a, b)] = var
                    fseg[L][(a, b)] = seg
                    seg_prod[seg].append(var)
        waste[L] = {}
        for a in nodes:
            if a < L:
                waste[L][a] = m.addVar(vtype="I", lb=0, name=f"w_{L}_{a}")
        outflow = defaultdict(list)
        inflow = defaultdict(list)
        for (a, b) in f[L]:
            outflow[a].append(f[L][(a, b)])
            inflow[b].append(f[L][(a, b)])
        for a, wv in waste[L].items():
            outflow[a].append(wv)
            inflow[L].append(wv)
        used = m.addVar(vtype="I", lb=0, ub=stock_qty[L], name=f"used_{L}")
        for pos in nodes:
            if pos == 0:
                m.addCons(quicksum(outflow[pos]) == used)
            elif pos == L:
                m.addCons(quicksum(inflow[pos]) == used)
            else:
                m.addCons(quicksum(inflow[pos]) == quicksum(outflow[pos]))
        used_vars[L] = used
    # 需求满足: 每种段(管长)产出 == 需求根数。
    for seg, demand in seg_demand.items():
        if not seg_prod[seg]:
            return None  # 该管长无任何库存能切出 -> 不可行
        m.addCons(quicksum(seg_prod[seg]) == demand)

    usedlen = quicksum(used_vars[L] * L for L in stock_qty)
    m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
    m.setObjective(usedlen, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  纯切装箱无解 status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)
        return None
    used_len = round(m.getVal(usedlen))
    util = group.demand_length / used_len if used_len else 0
    if verbose:
        print(f"  纯切装箱: 用料={used_len} util={util:.4f} "
              f"status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)
    # 抽取切法(段多重集), 无焊口。
    cut_patterns = defaultdict(int)
    seg_used = set()
    for L in stock_qty:
        flow = {}
        for (a, b), var in f[L].items():
            v = round(m.getVal(var))
            if v > 0:
                flow[(a, b)] = v
        used = round(m.getVal(used_vars[L]))
        for _ in range(used):
            segs = []
            pos = 0
            while pos < L:
                nxt = None
                for (a, b), v in flow.items():
                    if a == pos and v > 0:
                        nxt = (a, b)
                        break
                if nxt is None:
                    break
                segs.append(fseg[L][nxt])
                flow[nxt] -= 1
                pos = nxt[1]
            cut_patterns[(L, tuple(sorted(segs)))] += 1
            seg_used.update(segs)
    return {
        "joints": 0, "cut_types": len(cut_patterns), "weld_types": 0,
        "seg_types": len(seg_used), "used_len": used_len, "util": util,
        "cut_patterns": dict(cut_patterns), "weld_patterns": {},
        "solver": "pure_cut",
    }


def _better_of(weld, pure, group, verbose=False):
    """在焊接解与纯切兜底解之间取优。

    字典序判据(与 §7 一致): 利用率是**准入门槛**, 其次才是焊口。
      1. 达标者(util>=target)优先于不达标者。
      2. 同为达标: 焊口少者优先(达标区内利用率已够, 焊口是首要目标)。
      3. 同为不达标: 利用率高者优先——低利用率+少焊口是"废解"(如 L7 纯切 0.7435
         远劣于焊接 0.9742), 不能因焊口少就选它。仅当利用率接近(<=0.5%)时才比焊口。
    纯切解焊口恒为 0。L14 类(纯切/焊接均不达标但纯切 util 与焊接相当或更高)-> 取纯切 0 焊口。
    """
    if pure is None:
        return weld
    if weld is None:
        if verbose:
            print("  焊接无解 -> 回退纯切兜底解(0 焊口)", flush=True)
        return pure
    tr = group.target_rate
    w_ok = tr is None or weld["util"] >= tr - 0.005
    p_ok = tr is None or pure["util"] >= tr - 0.005
    if w_ok != p_ok:
        pick_pure = p_ok  # 达标者胜。
    elif w_ok and p_ok:
        pick_pure = pure["joints"] < weld["joints"]  # 均达标: 焊口少者胜。
    else:
        # 均不达标: 利用率为准入门槛, 高者胜; 接近时(<=0.5%)才比焊口。
        du = pure["util"] - weld["util"]
        if abs(du) > 0.005:
            pick_pure = du > 0
        else:
            pick_pure = pure["joints"] < weld["joints"]
    if pick_pure and verbose:
        print(f"  纯切兜底更优(焊口 {pure['joints']} util {pure['util']:.4f} "
              f"vs 焊接 {weld['joints']}/{weld['util']:.4f}) -> 取纯切", flush=True)
    return pure if pick_pure else weld


def solve_arcflow(group, tl=120.0, step=None, verbose=True):
    from pyscipopt import Model, quicksum
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    kerf = group.blade_margin
    max_stock = max(s.length for s in group.stocks)
    stock_qty = defaultdict(int)
    for st in group.stocks:
        stock_qty[st.length] += st.quantity
    pipes = group.pipes
    # 焊点网格: 默认 = min_wd(治本, 不丢合法路径; 相邻焊口本就须>=min_wd)。
    if step is None:
        step = max(1, min_wd)

    # ── 前置分类器: 纯切可达标 -> 纯切快路(0 焊口, 跳过焊接建模) ──
    cls = classify_group(group, verbose=verbose)
    if verbose:
        print(f"  分类: {cls['kind']} [{cls['geom']}] — {cls['reason']}", flush=True)
    if cls["kind"] == "infeasible":
        return None
    if cls["kind"] == "pure_cut":
        return cls["pure_result"]
    # 纯切不达标但仍可行(利用率被库存物理顶死, 焊接也顶不上去, 如 L14):
    # 纯切 0 焊口解是天然兜底——若焊接求解器无解或反而更差(焊口更多/利用率更低),
    # 就回退到纯切解(0 焊口, 利用率不低于焊接解)。
    pure_fallback = cls.get("pure_result")

    # ── 拼层: 每管型建管身图弧集, 收集全局段长集合 ──
    stock_lens = sorted(stock_qty.keys())
    pipe_graphs = {}
    seg_lengths = set()
    for i, p in enumerate(pipes):
        pts, arcs = build_pipe_arcs(p, max_stock, min_cut, min_wd, step, stock_lens)
        pipe_graphs[i] = (pts, arcs)
        for (_, _, seg) in arcs:
            seg_lengths.add(seg)
    if verbose:
        na = sum(len(a) for (_, a) in pipe_graphs.values())
        print(f"  拼层(step={step}): 管型={len(pipes)} 弧总数={na} 段长种类={len(seg_lengths)}", flush=True)

    # ── 切层: 每种定尺建母料 arc-flow 图, 弧长 ∈ seg_lengths(共享) ──
    seg_sorted = sorted(seg_lengths)
    if verbose:
        print(f"  段长范围: [{seg_sorted[0] if seg_sorted else 0}, "
              f"{seg_sorted[-1] if seg_sorted else 0}]", flush=True)

    # 切层规模决策: 弧数超阈值(极限档 L12/L13 段长爆炸)时改用切层列生成,
    # 不预建百万级对称弧 -> 从小列池起步, 用对偶价定价出少数关键切法。
    n_cut_arcs = _cut_arc_count(stock_qty, seg_sorted)
    if n_cut_arcs > CUTCG_ARC_THRESHOLD:
        if verbose:
            print(f"  切层弧数≈{n_cut_arcs} > {CUTCG_ARC_THRESHOLD} -> 切换切层列生成", flush=True)
        weld = solve_arcflow_cutcg(
            group, pipe_graphs, seg_sorted, stock_qty, max_stock,
            min_wd, min_cut, kerf, tl=tl, verbose=verbose,
            pure_incumbent=pure_fallback)
        return _better_of(weld, pure_fallback, group, verbose)
    elif verbose:
        print(f"  切层弧数≈{n_cut_arcs} -> 原 arc-flow", flush=True)

    m = Model("arcflow")
    m.hideOutput()
    m.setParam("limits/time", tl)

    # 拼层流变量: g[i][(a,b)] >=0 整数
    g = {}
    for i, (pts, arcs) in pipe_graphs.items():
        g[i] = {}
        for (a, b, seg) in arcs:
            g[i][(a, b)] = m.addVar(vtype="I", lb=0, name=f"g_{i}_{a}_{b}")

    # 拼层流守恒 + 源汇=需求
    for i, (pts, arcs) in pipe_graphs.items():
        Li = pipes[i].length
        outflow = defaultdict(list)
        inflow = defaultdict(list)
        for (a, b, seg) in arcs:
            outflow[a].append(g[i][(a, b)])
            inflow[b].append(g[i][(a, b)])
        for pos in pts:
            if pos == 0:
                m.addCons(quicksum(outflow[pos]) == pipes[i].demand)
            elif pos == Li:
                m.addCons(quicksum(inflow[pos]) == pipes[i].demand)
            else:
                m.addCons(quicksum(inflow[pos]) == quicksum(outflow[pos]))
        # 焊口上限: 总弧数 <= demand*(max_joints+1)
        m.addCons(quicksum(g[i][(a, b)] for (a, b, seg) in arcs)
                  <= pipes[i].demand * (pipes[i].max_joints + 1))

    # 切层流变量: f[L][(a,b)] 段弧 + w[L][a] 废料弧(a->L)
    # 规范化 arc-flow(Valério de Carvalho): 段按'非增序'放置(每段<=上一段),
    # 打破排列对称, 把切层节点/弧数从组合级降到近线性。maxseg[a]=到达 a 时
    # 下一段允许的最大长度; a=0 时为 +inf。这不丢任何可行切法(任意多重集都有
    # 唯一的非增排列), 但等价切法只保留一条规范路径 -> 弧数骤降。
    f = {}
    fseg = {}  # (L,a,b) -> 该弧对应的实际段长(扣 kerf 后), 供耦合与还原用
    waste = {}
    used_vars = {}
    INF = max(seg_sorted) if seg_sorted else 0
    for L in stock_qty:
        f[L] = {}
        fseg[L] = {}
        # 节点 = 母料上已消耗的物理长度(含内部切缝)。首段从 0 出发不含前置切缝,
        # 其后每段额外占 kerf(BETWEEN_PARTS)。故弧: 首段 b=a+seg; 内部段 b=a+kerf+seg。
        # 规范非增序打破排列对称: maxseg[a]=到达 a 时下一段允许的最大长度。
        maxseg = {0: INF}
        order = [0]
        idx = 0
        while idx < len(order):
            a = order[idx]; idx += 1
            cap = maxseg[a]
            step_kerf = 0 if a == 0 else kerf
            for seg in seg_sorted:
                if seg > cap:
                    break  # seg_sorted 升序, 超过 cap 的都不允许(非增序)
                b = a + step_kerf + seg
                if b > L:
                    continue
                if b not in maxseg:
                    maxseg[b] = seg
                    order.append(b)
                elif seg > maxseg[b]:
                    maxseg[b] = seg  # 放宽(允许更大的后继段)
        nodes = sorted(maxseg)
        node_set = set(nodes)
        for a in nodes:
            cap = maxseg[a]
            step_kerf = 0 if a == 0 else kerf
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + step_kerf + seg
                if b <= L and b in node_set:
                    f[L][(a, b)] = m.addVar(vtype="I", lb=0, name=f"f_{L}_{a}_{b}")
                    fseg[L][(a, b)] = seg
        # 废料弧: 可达位置 a(a<L) -> L(尾料 L-a, 允许 0)
        waste[L] = {}
        for a in nodes:
            if a < L:
                waste[L][a] = m.addVar(vtype="I", lb=0, name=f"w_{L}_{a}")
        # 切层流守恒: 源 0 出流 = 用的根数 = 汇 L 入流(段弧入 + 废料弧入)
        outflow = defaultdict(list)
        inflow = defaultdict(list)
        for (a, b) in f[L]:
            outflow[a].append(f[L][(a, b)])
            inflow[b].append(f[L][(a, b)])
        for a, wv in waste[L].items():
            outflow[a].append(wv)  # 废料弧从 a 出
            inflow[L].append(wv)   # 到 L
        used = m.addVar(vtype="I", lb=0, ub=stock_qty[L], name=f"used_{L}")
        for pos in nodes:
            if pos == 0:
                m.addCons(quicksum(outflow[pos]) == used)
            elif pos == L:
                m.addCons(quicksum(inflow[pos]) == used)
            else:
                m.addCons(quicksum(inflow[pos]) == quicksum(outflow[pos]))
        used_vars[L] = used

    # ── 耦合: 每段长 ℓ, 切层产出 >= 拼层消耗 ──
    for seg in seg_sorted:
        prod_terms = []
        for L in stock_qty:
            for (a, b), var in f[L].items():
                if fseg[L][(a, b)] == seg:
                    prod_terms.append(var)
        cons_terms = []
        for i, (pts, arcs) in pipe_graphs.items():
            for (a, b, s2) in arcs:
                if s2 == seg:
                    cons_terms.append(g[i][(a, b)])
        if prod_terms or cons_terms:
            m.addCons(quicksum(prod_terms) - quicksum(cons_terms) >= 0)

    # ── 目标 Pass1: min 用料总长 ──
    usedlen = quicksum(used_vars[L] * L for L in stock_qty)
    # arc-flow LP 松弛极紧(实测 L7 达 0.9999), 整数最优接近之。
    # 慢在闭合整数 gap -> 开激进可行性 heuristics 尽快拿好 incumbent。
    from pyscipopt import SCIP_PARAMEMPHASIS
    m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
    m.setObjective(usedlen, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  Pass1 无解 status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)
        return _better_of(None, pure_fallback, group, verbose)
    u_star = m.getObjVal()
    if verbose:
        util1 = group.demand_length / u_star if u_star else 0
        print(f"  Pass1: 用料={u_star:.0f} util={util1:.4f} "
              f"status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)

    # ── Pass2: 固定用料<=U*, min 总焊口 ──
    m.freeTransform()
    m.setParam("limits/time", tl)
    m.addCons(usedlen <= u_star + 1e-6)
    joints = quicksum(
        (quicksum(g[i][(a, b)] for (a, b, s2) in pipe_graphs[i][1]) - pipes[i].demand)
        for i in range(len(pipes))
    )
    m.setObjective(joints, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  Pass2 无解 status={m.getStatus()}", flush=True)
        return None

    # ── 抽取解 ──
    weld = _extract(group, pipes, pipe_graphs, g, f, used_vars, stock_qty, m, verbose, fseg=fseg)
    return _better_of(weld, pure_fallback, group, verbose)


def _grid_seg_set(pipes, max_stock, min_wd, min_cut, step):
    """极限档段集: 每根管长按 step 网格的所有切分点段长(两侧), 限 [min_seg, max_stock]。
    治本 L13 根因: CG 只出少数密排短段, 拿不到 3 段分割所需的中段, 焊口暴涨。
    完整网格段集(L13 约 70 种)让耦合整数模型能自由 2/3 段分割 -> 库存内 min 焊口。

    注意: 段下界是 max(min_cut,1), 不含 min_wd。min_wd 只约束'两内部焊口间'的段
    (见 build_pipe_arcs), 首/尾段及整管段(纯切档)可短于 min_wd, 不能在此滤掉,
    否则纯切档/短管档段集被滤空 -> 无解(L3/L14/L15/L16 曾如此)。"""
    min_seg = max(min_cut, 1)
    segs = set()
    for p in pipes:
        L = p.length
        # 整管本身也是合法段(纯切/短管档: 一根管一段, 无焊口)
        if min_seg <= L <= max_stock:
            segs.add(L)
        a = step
        while a < L:
            if min_seg <= a <= max_stock:
                segs.add(a)
            rem = L - a
            if min_seg <= rem <= max_stock:
                segs.add(rem)
            a += step
    return sorted(segs)


def solve_arcflow_cutcg(group, pipe_graphs, seg_sorted, stock_qty, max_stock,
                        min_wd, min_cut, kerf, tl=120.0, verbose=True,
                        max_iter=80, step=None, pure_incumbent=None):
    """极限档(切层弧爆炸)焊接求解: coarse-to-fine 网格逐步细化 + 完整段集兜底。

    治本原经验: 原始切层用全部段长在每根母料上组合 -> 百万弧 SCIP 压垮。
    焊口只由拼层产生, 拼层段集本身不大(数十~数百种); 切层只需按'拼层实际会用的段'
    建规范图(段种类可控 -> 弧数可控)。

    coarse-to-fine(用户思路): 不必一步到位。先用粗网格段集快速拿'达标利用率'的
    可行解与焊口上界; 若达标, 再用完整段集(最细一档)尝试把焊口压到更低。这样:
      - 粗档快速给保底解(避免完整段集直接超时);
      - 细档在时限内优化焊口(完整段集不丢任何合法分段, 已验证 L13/L9)。
    每档都带 floor_cap(利用率软下界)与递减的焊口上界(joint_cap), 逐档收紧。
    """
    pipes = group.pipes
    if step is None:
        step = max(1, min_wd)
    # 用料上界: 取'总库存长'(最松, 永不因用料上界而 infeasible)。
    # 不用 demand/target: 那会强制 util>=target, 但很多档(如 L9)连老软件都达不到
    # target(库存物理上限), 逼 util>=target 会让模型 infeasible -> SCIP 空耗时限。
    # 库存越紧, 满足需求本身就把 util 顶得越高(L9 库存≈需求, min用料解 util 自然近满),
    # 故无需 target 硬约束; min 焊口目标下再靠段供需自然收敛。
    floor_cap = group.stock_length if group.stock_length else None

    # 网格档: 从密(段多, 更易可行)到疏(段少, 更快)再到完整段集兜底。
    # 用 _grid_seg_set 的均匀步长段(小而快); 逐档尝试, 命中达标即停。
    # 完整段集作最细兜底(不丢解, 但切层弧可能爆炸 -> 仅在前档全废时用)。
    full_segs = list(seg_sorted)
    grids = []
    # 步长梯度: 由细到粗探路。含更细档(step/2, step/3)保证像 L9 这类
    # step 相对管长偏大的档也能拿到足够密的可行探路网格(不至于只有 2~3 段而 infeasible),
    # 再含更粗档(step*2, step*3)快速试更省焊口的稀疏解。
    for stp in (step // 3, step // 2, step, step * 2, step * 3):
        if stp < 1:
            continue
        gs = _grid_seg_set(pipes, max_stock, min_wd, min_cut, stp)
        gs = sorted(set(gs) & set(full_segs))
        if gs:
            grids.append(gs)
    # 由段多(密)到段少(疏)排序探路: 密档更易可行, 先拿保底解。
    grids.sort(key=len, reverse=True)
    grids.append(full_segs)  # 最细兜底: 完整段集, 保证不丢解(可能在循环内被动态跳过)。
    # 去重(保序): 粗档若与完整档相同则跳过。
    seen = set()
    uniq_grids = []
    for gs in grids:
        key = tuple(gs)
        if key not in seen:
            seen.add(key)
            uniq_grids.append(gs)
    grids = uniq_grids

    if verbose:
        na = sum(len(a) for (_, a) in pipe_graphs.values())
        print(f"  极限档焊接: 完整段集={len(full_segs)} 拼层弧={na} "
              f"coarse-to-fine 档位段数={[len(gs) for gs in grids]}", flush=True)

    best = None
    joint_cap = None  # 上一档得到的焊口 -> 下一档的严格上界(逐档降焊口)。
    remaining = tl
    n_grid = len(grids)
    # 时间策略: 未拿到达标解前, 每档是'可行性探路', 密档小额封顶、完整档兜底给足;
    # 一旦某档拿到达标解, 后续档(含完整档)只是'改进尝试'(压更少焊口), 小额封顶——
    # 不值得花全部预算去证明'更少焊口不可行'(那是最优性证明, 完整档常直接超时空耗)。
    PROBE_CAP = 10.0
    IMPROVE_CAP = 30.0
    pure_has = pure_incumbent is not None and pure_incumbent.get("joints", 1) == 0
    for gi, seg_grid in enumerate(grids):
        if remaining <= 1:
            break
        is_final = gi == n_grid - 1
        # 动态跳过完整段集兜底档: 若已有 0 焊口纯切兜底, 且到目前为止焊接连一个
        # 达标解都没拿到(target 很可能被库存物理顶死, 完整档也补不上), 而完整档
        # 切层弧巨大(光建模就极慢, 如 L14 的 356 段)——则跳过, 直接回退纯切兜底。
        # 焊接必 >0 焊口, 无法在焊口上超越纯切; 唯一价值是达标, 而这里已判定难达标。
        if is_final and pure_has:
            best_达标_now = best is not None and (
                group.target_rate is None or best["util"] >= group.target_rate - 0.005)
            n_full_arcs = _cut_arc_count(stock_qty, seg_grid)
            if not best_达标_now and n_full_arcs > CUTCG_ARC_THRESHOLD:
                if verbose:
                    print(f"    档{gi}(段{len(seg_grid)}) 切层弧≈{n_full_arcs} 巨大且"
                          f"目标难达标, 已有 0 焊口纯切兜底 -> 跳过完整档", flush=True)
                break
        best_达标 = best is not None and (
            group.target_rate is None or best["util"] >= group.target_rate - 0.005)
        if best_达标:
            share = min(remaining, IMPROVE_CAP)  # 已达标: 后续仅改进, 封顶。
        elif is_final:
            share = remaining  # 未达标且到完整档: 兜底给足全部剩余预算。
        else:
            share = min(remaining, PROBE_CAP)  # 探路档: 快速探路。
        # 拼层弧须限制在本档段集内, 耦合才闭环。
        sub_graphs = _restrict_pipe_graphs(pipe_graphs, set(seg_grid))
        t0 = time.time()
        res = _solve_int_restricted(
            group, pipes, sub_graphs, seg_grid, stock_qty, max_stock,
            floor_cap, tl=share, verbose=verbose, joint_cap=joint_cap)
        remaining -= time.time() - t0
        if res is None:
            if verbose:
                print(f"    档{gi}(段{len(seg_grid)}) 无更优解, 尝试下一档", flush=True)
            continue
        达标 = group.target_rate is None or res["util"] >= group.target_rate - 0.005
        # 采纳优先级: 达标解优先于非达标; 同达标性下焊口少者优先。
        if best is None:
            take = True
        elif 达标 and not best_达标:
            take = True  # 达标击败非达标(哪怕焊口多)。
        elif 达标 == best_达标:
            take = res["joints"] < best["joints"]
        else:
            take = False  # 非达标不替换达标。
        if take:
            best = res
            if 达标:
                joint_cap = res["joints"] - 1  # 下一档只找更少焊口的达标解。
        if verbose:
            print(f"    档{gi}(段{len(seg_grid)}): 焊口={res['joints']} "
                  f"util={res['util']:.4f} 达标={达标}", flush=True)
    return best


def _restrict_pipe_graphs(pipe_graphs, seg_set):
    """把拼层弧集限制到给定段集内, 并重算节点集(耦合闭环需拼/切层段集一致)。"""
    out = {}
    for i, (pts, arcs) in pipe_graphs.items():
        sub_arcs = [(a, b, seg) for (a, b, seg) in arcs if seg in seg_set]
        node_set = {0}
        for (a, b, seg) in sub_arcs:
            node_set.add(a)
            node_set.add(b)
        # 保留原端点(0 与各管 length 由 arcs 端点自然涵盖)。
        for pos in pts:
            if pos == 0 or pos == (pts[-1] if pts else 0):
                node_set.add(pos)
        out[i] = (sorted(node_set), sub_arcs)
    return out



def _solve_int_restricted(group, pipes, pipe_graphs, seg_prod, stock_qty, max_stock,
                          floor_cap, tl=120.0, verbose=True, joint_cap=None):
    """限定段整数模型: 拼层 arc-flow(已按 producible 过滤) + 切层 arc-flow(仅 seg_prod)。
    两阶段: Pass1 min 用料(达门槛) -> Pass2 固定用料 min 焊口。切层弧数极小(段仅 ~49)。"""
    from pyscipopt import Model, quicksum, SCIP_PARAMEMPHASIS
    min_wd = group.min_weld_distance
    seg_sorted = sorted(seg_prod)
    if not seg_sorted:
        return None
    INF = max(seg_sorted)

    m = Model("cutcg_int")
    m.hideOutput()
    m.setParam("limits/time", tl)
    # 拼层整数流
    g = {}
    for i, (pts, arcs) in pipe_graphs.items():
        g[i] = {}
        for (a, b, seg) in arcs:
            g[i][(a, b)] = m.addVar(vtype="I", lb=0, name=f"g_{i}_{a}_{b}")
    for i, (pts, arcs) in pipe_graphs.items():
        Li = pipes[i].length
        outflow = defaultdict(list)
        inflow = defaultdict(list)
        for (a, b, seg) in arcs:
            outflow[a].append(g[i][(a, b)])
            inflow[b].append(g[i][(a, b)])
        for pos in pts:
            if pos == 0:
                m.addCons(quicksum(outflow[pos]) == pipes[i].demand)
            elif pos == Li:
                m.addCons(quicksum(inflow[pos]) == pipes[i].demand)
            else:
                m.addCons(quicksum(inflow[pos]) == quicksum(outflow[pos]))
        m.addCons(quicksum(g[i][(a, b)] for (a, b, seg) in arcs)
                  <= pipes[i].demand * (pipes[i].max_joints + 1))

    # 切层 arc-flow(规范化非增序), 段仅 seg_sorted -> 弧数极小
    f = {}
    fseg = {}
    waste = {}
    used_vars = {}
    kerf = group.blade_margin
    for L in stock_qty:
        f[L] = {}
        fseg[L] = {}
        maxseg = {0: INF}
        order = [0]
        idx = 0
        while idx < len(order):
            a = order[idx]; idx += 1
            cap = maxseg[a]
            step_kerf = 0 if a == 0 else kerf
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + step_kerf + seg
                if b > L:
                    continue
                if b not in maxseg:
                    maxseg[b] = seg
                    order.append(b)
                elif seg > maxseg[b]:
                    maxseg[b] = seg
        nodes = sorted(maxseg)
        node_set = set(nodes)
        for a in nodes:
            cap = maxseg[a]
            step_kerf = 0 if a == 0 else kerf
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + step_kerf + seg
                if b <= L and b in node_set:
                    f[L][(a, b)] = m.addVar(vtype="I", lb=0, name=f"f_{L}_{a}_{b}")
                    fseg[L][(a, b)] = seg
        waste[L] = {}
        for a in nodes:
            if a < L:
                waste[L][a] = m.addVar(vtype="I", lb=0, name=f"w_{L}_{a}")
        outflow = defaultdict(list)
        inflow = defaultdict(list)
        for (a, b) in f[L]:
            outflow[a].append(f[L][(a, b)])
            inflow[b].append(f[L][(a, b)])
        for a, wv in waste[L].items():
            outflow[a].append(wv)
            inflow[L].append(wv)
        used = m.addVar(vtype="I", lb=0, ub=stock_qty[L], name=f"used_{L}")
        for pos in nodes:
            if pos == 0:
                m.addCons(quicksum(outflow[pos]) == used)
            elif pos == L:
                m.addCons(quicksum(inflow[pos]) == used)
            else:
                m.addCons(quicksum(inflow[pos]) == quicksum(outflow[pos]))
        used_vars[L] = used

    # 段供需耦合: 切层产出(ℓ) >= 拼层消耗(ℓ)
    for seg in seg_sorted:
        prod_terms = []
        for L in stock_qty:
            for (a, b), var in f[L].items():
                if fseg[L][(a, b)] == seg:
                    prod_terms.append(var)
        cons_terms = []
        for i, (pts, arcs) in pipe_graphs.items():
            for (a, b, s2) in arcs:
                if s2 == seg:
                    cons_terms.append(g[i][(a, b)])
        if prod_terms or cons_terms:
            m.addCons(quicksum(prod_terms) - quicksum(cons_terms) >= 0)

    usedlen = quicksum(used_vars[L] * L for L in stock_qty)
    if floor_cap is not None:
        m.addCons(usedlen <= floor_cap + 1e-6)
    # 单趟: min 焊口 s.t. 用料<=利用率门槛(0.95)。焊口是真目标, 利用率只需达门槛。
    # 不做 min用料 前置(那会逼密排短段 -> 焊口暴涨, 且在极限档求不动)。
    joints = quicksum(
        (quicksum(g[i][(a, b)] for (a, b, s2) in pipe_graphs[i][1]) - pipes[i].demand)
        for i in range(len(pipes)))
    if joint_cap is not None and joint_cap >= 0:
        # coarse-to-fine 逐档收紧: 只找比上一档更少焊口的解。
        m.addCons(joints <= joint_cap)
    m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
    m.setObjective(joints, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  整数(限定段)无解 status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)
        return None
    if verbose:
        uu = m.getVal(usedlen)
        print(f"  整数(限定段): 焊口={m.getObjVal():.0f} 用料={uu:.0f} "
              f"util={group.demand_length/uu:.4f} status={m.getStatus()} "
              f"({time.time()-t0:.1f}s)", flush=True)
    return _extract(group, pipes, pipe_graphs, g, f, used_vars, stock_qty, m, verbose, fseg=fseg)


def _extract(group, pipes, pipe_graphs, g, f, used_vars, stock_qty, m, verbose, fseg=None):
    # 拼法: 每管型分解流为路径(段序列)
    weld_patterns = defaultdict(int)   # (pipe_i, seq) -> count
    total_joints = 0
    for i, (pts, arcs) in pipe_graphs.items():
        # 建弧流字典
        flow = {}
        for (a, b, seg) in arcs:
            v = round(m.getVal(g[i][(a, b)]))
            if v > 0:
                flow[(a, b)] = v
        # 流分解为路径(贪心): 反复从 0 走到 Li
        Li = pipes[i].length
        while any(v > 0 for v in flow.values()):
            seq = []
            pos = 0
            ok = True
            while pos < Li:
                nxt = None
                for (a, b), v in flow.items():
                    if a == pos and v > 0:
                        nxt = (a, b)
                        break
                if nxt is None:
                    ok = False
                    break
                seq.append(nxt[1] - nxt[0])
                flow[nxt] -= 1
                pos = nxt[1]
            if ok and pos == Li:
                weld_patterns[(i, tuple(seq))] += 1
                total_joints += len(seq) - 1
            else:
                break
    # 切法
    cut_patterns = defaultdict(int)
    used_len = 0
    seg_used = set()
    for L in stock_qty:
        # 分解母料流为切法(段多重集)
        flow = {}
        for (a, b), var in f[L].items():
            v = round(m.getVal(var))
            if v > 0:
                flow[(a, b)] = v
        # 每根定尺 = 从 0 到 L 的一条路径(段弧+末尾废料弧)
        # 用 used 根数拆: 逐根走
        used = round(m.getVal(used_vars[L]))
        for _ in range(used):
            segs = []
            pos = 0
            while pos < L:
                nxt = None
                for (a, b), v in flow.items():
                    if a == pos and v > 0:
                        nxt = (a, b)
                        break
                if nxt is None:
                    break  # 剩余到 L 是废料
                real_seg = fseg[L][nxt] if fseg is not None else (nxt[1] - nxt[0])
                segs.append(real_seg)
                flow[nxt] -= 1
                pos = nxt[1]
            cut_patterns[(L, tuple(sorted(segs)))] += 1
            used_len += L
            seg_used.update(segs)

    cut_types = len(cut_patterns)
    weld_types = len({seq for (i, seq) in weld_patterns if len(seq) >= 2})
    seg_types = len(seg_used)
    util = group.demand_length / used_len if used_len else 0
    return {
        "joints": total_joints, "cut_types": cut_types, "weld_types": weld_types,
        "seg_types": seg_types, "used_len": used_len, "util": util,
        "cut_patterns": dict(cut_patterns), "weld_patterns": dict(weld_patterns),
    }


def main():
    lv = int(sys.argv[1])

    def argf(name, d, cast):
        return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d

    tl = argf("--tl", 120.0, float)
    step = argf("--step", None, int)
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    s = next(x for x in samples if x["level"] == lv)
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    print(f"L{lv} {s['spec']}  老软件: {s['legacy']}  target_rate={g.target_rate:.4f}", flush=True)
    res = solve_arcflow(g, tl=tl, step=step)
    if res is None:
        print("  arc-flow 无解")
        return
    lg = s["legacy"]
    print("  ══ arc-flow V3 vs 老软件 ══")
    print(f"    利用率:   {res['util']:.4f} vs {lg.get('util'):.4f}")
    print(f"    总焊口:   {res['joints']} vs {lg.get('joints')}")
    print(f"    拼法种类: {res['weld_types']} vs {lg.get('weld_types')}")
    print(f"    切法种类: {res['cut_types']} vs {lg.get('cut_types')}")
    print(f"    段种类:   {res['seg_types']}")


if __name__ == "__main__":
    main()
