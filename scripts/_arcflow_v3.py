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


def _diagnose(group, result, pure_fallback, util_floor=None, note=None):
    """S6 诊断透传: 给结果附一段结构化诊断, 说明解的成色与可能的欠优原因。

    不静默 fallback: 当发生'焊接无解回退纯切'、'利用率未达门槛'、'焊口远高于理论
    下界'等情况时, 用结构化 code 标注, 让调用方(CLI/回归/报告)能显式暴露, 而非
    看到一个数字就当成正常最优。
    """
    import math
    codes = []
    uf = _util_floor(group, util_floor)
    max_stock = max(s.length for s in group.stocks)
    # 理论最小焊口下界: 每管至少 ⌈L/max_stock⌉ 段 -> ⌈L/max_stock⌉-1 焊口。
    lb_joints = sum((math.ceil(p.length / max_stock) - 1) * p.demand for p in group.pipes)
    if result is None:
        codes.append(("NO_SOLUTION", "焊接与纯切均无可行解"))
        return {"codes": codes, "lb_joints": lb_joints, "util_floor": uf}
    if note:
        codes.append(note)
    util = result.get("util", 0.0)
    if util + 1e-9 < uf:
        codes.append(("UTIL_BELOW_FLOOR",
                      f"利用率 {util:.4f} < 硬底线 {uf:.4f}(库存物理受限或无更优解)"))
    if pure_fallback is not None and result is pure_fallback:
        codes.append(("FELL_BACK_TO_PURECUT",
                      "焊接未能给出不劣于纯切的解 -> 采用纯切兜底(0 焊口)"))
    j = result.get("joints", 0)
    if lb_joints > 0 and j > lb_joints:
        gap = (j - lb_joints) / lb_joints
        if gap > 0.15:
            codes.append(("JOINTS_ABOVE_LB",
                          f"焊口 {j} 高于理论下界 {lb_joints}(+{gap:.0%}); "
                          f"可能段集不足/段集受库存约束导致未达最优分段"))
    return {"codes": codes, "lb_joints": lb_joints, "util_floor": uf}


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


def _util_floor(group, util_floor=None):
    """利用率硬底线 = max(0.95, legacy_util - 1e-3)（用户拍板）。

    solve_arcflow 内拿不到 legacy_util, 由调用方(CLI/回归)传入 util_floor;
    未传时退回 group.target_rate(输入目标利用率), 再退回 0.95。
    底线只作可行性约束, 绝不作优化目标(util 在词典序末档被动最大化)。
    """
    if util_floor is not None:
        return max(0.95, util_floor)
    if group.target_rate is not None:
        return max(0.95, group.target_rate)
    return 0.95


def solve_arcflow(group, tl=120.0, step=None, verbose=True, util_floor=None):
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
        if verbose:
            print(f"  [诊断] INFEASIBLE: {cls['reason']}", flush=True)
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
        result = _better_of(weld, pure_fallback, group, verbose)
        if result is not None:
            result["diagnosis"] = _diagnose(group, result, pure_fallback, util_floor)
            if verbose and result["diagnosis"]["codes"]:
                for code, msg in result["diagnosis"]["codes"]:
                    print(f"  [诊断] {code}: {msg}", flush=True)
        return result
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

    # ── 耦合: 每段长 ℓ, 切层产出 == 拼层消耗(精确段平衡) ──
    # 用 == 而非 >=: 禁止切层过产无人消耗的"幽灵段"(否则利用率虚高且 verifier 段平衡报错)。
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
            m.addCons(quicksum(prod_terms) - quicksum(cons_terms) == 0)

    # ── 词典序求解(SPEC §二): 焊口第一, 利用率是底线约束+末档 tiebreak ──
    usedlen = quicksum(used_vars[L] * L for L in stock_qty)
    joints = quicksum(
        (quicksum(g[i][(a, b)] for (a, b, s2) in pipe_graphs[i][1]) - pipes[i].demand)
        for i in range(len(pipes))
    )
    # 利用率硬底线: used_len <= demand_length / util_floor(等价 util>=floor)。
    uf = _util_floor(group, util_floor)
    cap = group.demand_length / uf if uf > 0 else None
    floor_cons = None
    if cap is not None:
        floor_cons = m.addCons(usedlen <= cap + 1e-6, name="util_floor")

    # ── Pass1: min 总焊口(s.t. util>=floor) ── 焊口是 SPEC 第一优先级。
    # arc-flow LP 松弛极紧(实测 L7 达 0.9999); 开激进可行性 heuristics 尽快拿好 incumbent。
    from pyscipopt import SCIP_PARAMEMPHASIS
    m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
    m.setObjective(joints, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0 and floor_cons is not None and m.getStatus() == "infeasible":
        # 底线过紧(库存物理顶死, 如 L7 达不到 target)。不能直接删底线后 min 焊口
        # ——那会退化成纯切 0 焊口低利用率废解(如 L7 0.7435)。正确做法: 先 min 用料
        # 探明'可达到的最高利用率'U_min, 再把底线替换为 U_min(可达最优), 在其内 min 焊口。
        if verbose:
            print(f"  Pass1 底线 floor={uf:.4f} 不可达 -> 探最高可达利用率", flush=True)
        m.freeTransform()
        m.delCons(floor_cons)
        floor_cons = None
        m.setParam("limits/time", tl)
        m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
        m.setObjective(usedlen, "minimize")
        m.optimize()
        if m.getNSols() > 0:
            u_min = m.getObjVal()
            if verbose:
                print(f"    可达最高利用率={group.demand_length/u_min:.4f} "
                      f"(用料={u_min:.0f}) -> 以此为底线 min 焊口", flush=True)
            m.freeTransform()
            m.setParam("limits/time", tl)
            floor_cons = m.addCons(usedlen <= u_min + 1e-6, name="util_floor_achievable")
            m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
            m.setObjective(joints, "minimize")
            m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  Pass1(min焊口) 无解 status={m.getStatus()} ({time.time()-t0:.1f}s)",
                  flush=True)
        return _better_of(None, pure_fallback, group, verbose)
    j_star = m.getObjVal()
    if verbose:
        print(f"  Pass1(min焊口): 焊口={j_star:.0f} floor={uf:.4f} "
              f"status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)

    # ── Pass2: 固定焊口<=J*, min 用料(末档 max util 的等价) ──
    # 在焊口最优的前提下再收紧用料 -> 利用率被动最大化(SPEC 第 5 档)。
    m.freeTransform()
    m.setParam("limits/time", tl)
    m.addCons(joints <= j_star + 1e-6)
    m.setObjective(usedlen, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  Pass2(min用料) 无解 status={m.getStatus()}", flush=True)
        return _better_of(None, pure_fallback, group, verbose)
    if verbose:
        u_star = m.getObjVal()
        util2 = group.demand_length / u_star if u_star else 0
        print(f"  Pass2(min用料): 用料={u_star:.0f} util={util2:.4f} "
              f"status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)

    # ── 抽取 Pass2 解(基线: 焊口最优 + 用料最优) ──
    weld2 = _extract(group, pipes, pipe_graphs, g, f, used_vars, stock_qty, m, verbose, fseg=fseg)
    j_final = weld2["joints"]
    u_final = weld2["used_len"]

    # ── Pass3/4/5: 种类压缩(拼法→切法→段) —— 在'实际产出段集'上重解, 段集极小(通常≤8)
    # -> 路径枚举可控(实测 L7=1, L8=5)。保持焊口<=J*、用料<=U* 不劣化。 ──
    produced_segs = _produced_seg_set(f, fseg, used_vars, stock_qty, m)
    weld3 = _compress_types(
        group, pipes, produced_segs, stock_qty, j_final, u_final,
        tl=tl, verbose=verbose)
    weld = weld3 if weld3 is not None else weld2
    result = _better_of(weld, pure_fallback, group, verbose)
    if result is not None:
        result["diagnosis"] = _diagnose(group, result, pure_fallback, util_floor)
        if verbose and result["diagnosis"]["codes"]:
            for code, msg in result["diagnosis"]["codes"]:
                print(f"  [诊断] {code}: {msg}", flush=True)
    return result


def _produced_seg_set(f, fseg, used_vars, stock_qty, m):
    """从 Pass2 解读出'实际被切层产出的段长集合'(去重)。
    种类压缩只需在此小段集上重解(段种类通常≤8), 路径枚举可控。"""
    segs = set()
    for L in stock_qty:
        for (a, b), var in f[L].items():
            if round(m.getVal(var)) > 0:
                segs.add(fseg[L][(a, b)])
    return sorted(segs)


def _enum_pipe_patterns(pipe, seg_set, min_wd, min_cut, max_stock, budget=50000):
    """枚举管型 pipe 在给定段集内的全部合法拼法(0->L 路径, 段序列)。
    合法性: 段∈seg_set 且<=max_stock; 内部段(两端都是内部焊口)>=min_wd;
    焊口绝对位置(每段末端, 除最后到 L)须 weld_allowed(禁焊区/间距由 build 时保证的
    位置合法性在此重判)。段数<=max_joints+1。返回 list[tuple(段序列)]。"""
    L = pipe.length
    segs = sorted(s for s in seg_set if s <= max_stock)
    max_parts = pipe.max_joints + 1
    out = []
    cnt = [0]

    def dfs(pos, seq):
        if cnt[0] > budget or len(seq) > max_parts:
            return
        if pos == L:
            out.append(tuple(seq))
            return
        for s in segs:
            nb = pos + s
            if nb > L:
                continue
            is_first = pos == 0
            is_last = nb == L
            # 内部段(两端皆内部焊口)须>=min_wd
            if not is_first and not is_last and s < min_wd:
                continue
            # 焊口绝对位置(段末端, 非管尾)须合法
            if not is_last and not pipe.weld_allowed(nb):
                continue
            cnt[0] += 1
            seq.append(s)
            dfs(nb, seq)
            seq.pop()

    dfs(0, [])
    return out


def _enum_cut_patterns(L, seg_sorted, kerf, budget=200000):
    """枚举定尺 L 在 seg_sorted 内的所有切法(段多重集, 计 kerf 锯缝)。
    切法 = 若干段填入 L, 满足 Σseg + (段数-1)*kerf <= L。返回 list[tuple(sorted segs)]。
    段集小(通常≤8) -> 组合可控。用非降序段枚举避免多重集重复。"""
    out = []
    n = len(seg_sorted)
    cnt = [0]

    def dfs(start, used, segs):
        if cnt[0] > budget:
            return
        # 记录当前(非空)切法
        if segs:
            out.append(tuple(segs))
        for j in range(start, n):
            s = seg_sorted[j]
            add = s + (kerf if segs else 0)
            if used + add > L:
                continue
            cnt[0] += 1
            segs.append(s)
            dfs(j, used + add, segs)
            segs.pop()

    dfs(0, 0, [])
    return out


def _compress_types(group, pipes, seg_set, stock_qty, j_cap, u_cap,
                    tl=60.0, verbose=True, col_cap=20000, cut_tl=None):
    """在'实际产出段集'上重解, 词典序压缩种类(SPEC §二 第2/3/4/5档):
       Pass3 min 拼法种类; Pass4 min 切法种类; Pass5 min 段种类 + min 用料。
    约束: 焊口<=j_cap、用料<=u_cap 全程不劣化; 每档冻结上一档最优。
    cut_tl: Pass4(min切法种类)单独时限(默认=tl); 切法种类收敛慢, 可给更长时间。

    模型(全列式, 精确计种类):
      拼层: 路径变量 xp[(i,k)](段集小 -> 路径少, 可枚举); zW[type] 计拼法种类。
      切层: 切法列变量 yc[(L,k)]; zC[type] 计切法种类。
      段: zS[seg] 计段种类。
      段供需精确平衡: 切层产出(ℓ) == 拼层消耗(ℓ)(满足 verifier SEGMENT_BALANCE)。
    """
    from pyscipopt import Model, quicksum, SCIP_PARAMEMPHASIS
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    kerf = group.blade_margin
    max_stock = max(s.length for s in group.stocks)
    seg_sorted = sorted(seg_set)
    if not seg_sorted:
        return None

    # ── 每管型枚举拼法(段集内) ──
    pipe_pats = {}
    total_pats = 0
    for i, p in enumerate(pipes):
        pats = _enum_pipe_patterns(p, seg_set, min_wd, min_cut, max_stock)
        if not pats:
            if verbose:
                print(f"  种类压缩: 管型 {i}(L={p.length}) 在产出段集内无合法拼法 -> 跳过压缩",
                      flush=True)
            return None
        pipe_pats[i] = pats
        total_pats += len(pats)
    # ── 每定尺枚举切法列 ──
    cut_pats = {}
    total_cuts = 0
    for L in stock_qty:
        cps = _enum_cut_patterns(L, seg_sorted, kerf)
        cut_pats[L] = cps
        total_cuts += len(cps)
    if verbose:
        print(f"  种类压缩: 段集={len(seg_sorted)} 拼法列={total_pats} 切法列={total_cuts}",
              flush=True)
    # 列数爆炸保护: 枚举列过多会把压缩 MILP 压垮(得不偿失), 跳过压缩保留原解。
    if total_pats + total_cuts > col_cap:
        if verbose:
            print(f"  种类压缩: 列数={total_pats + total_cuts} 过多 -> 跳过(保留原解)",
                  flush=True)
        return None

    m = Model("compress")
    m.hideOutput()
    m.setParam("limits/time", tl)

    # 拼层路径变量 + 需求满足
    xp = {}
    for i, pats in pipe_pats.items():
        for k in range(len(pats)):
            xp[(i, k)] = m.addVar(vtype="I", lb=0, name=f"xp_{i}_{k}")
        m.addCons(quicksum(xp[(i, k)] for k in range(len(pats))) == pipes[i].demand)

    # 拼法种类 setup(去管型身份, 与 verifier weld_types 口径一致: 仅计 >=2 段)
    weld_type_ids = {}
    for pats in pipe_pats.values():
        for seq in pats:
            if len(seq) >= 2:
                weld_type_ids.setdefault(seq, len(weld_type_ids))
    zW = {tid: m.addVar(vtype="B", name=f"zW_{tid}") for tid in weld_type_ids.values()}
    for i, pats in pipe_pats.items():
        for k, seq in enumerate(pats):
            if len(seq) >= 2:
                m.addCons(xp[(i, k)] <= pipes[i].demand * zW[weld_type_ids[seq]])

    # 切层切法列变量 + 切法种类 setup(去定尺? verifier cut_types 按(定尺,段多重集)计,
    # 这里 type = (L, 段多重集) 保守区分定尺)
    yc = {}
    cut_type_ids = {}
    for L, cps in cut_pats.items():
        for k, segs in enumerate(cps):
            yc[(L, k)] = m.addVar(vtype="I", lb=0, name=f"yc_{L}_{k}")
            cut_type_ids.setdefault((L, segs), len(cut_type_ids))
    zC = {tid: m.addVar(vtype="B", name=f"zC_{tid}") for tid in cut_type_ids.values()}
    # 定尺根数上界: 单条切法用量 <= 该定尺可用根数
    for L, cps in cut_pats.items():
        m.addCons(quicksum(yc[(L, k)] for k in range(len(cps))) <= stock_qty[L])
        for k, segs in enumerate(cps):
            m.addCons(yc[(L, k)] <= stock_qty[L] * zC[cut_type_ids[(L, segs)]])

    # 段种类 setup: zS[seg]=1 若该段被产出
    zS = {seg: m.addVar(vtype="B", name=f"zS_{seg}") for seg in seg_sorted}

    def weld_consume(seg):
        return [sum(1 for s in seq if s == seg) * xp[(i, k)]
                for i, pats in pipe_pats.items() for k, seq in enumerate(pats)
                if seg in seq]

    def cut_produce(seg):
        return [segs.count(seg) * yc[(L, k)]
                for L, cps in cut_pats.items() for k, segs in enumerate(cps)
                if seg in segs]

    # 段供需精确平衡 + 段种类点亮
    for seg in seg_sorted:
        prod = cut_produce(seg)
        cons = weld_consume(seg)
        m.addCons(quicksum(prod) == quicksum(cons))
        # zS[seg]=1 当该段被产出(cons<=BigM*zS)
        bigm = sum(p.demand * (p.max_joints + 1) for p in pipes)
        m.addCons(quicksum(cons) <= bigm * zS[seg])

    # 焊口约束(不劣化)
    joints = quicksum((len(seq) - 1) * xp[(i, k)]
                      for i, pats in pipe_pats.items() for k, seq in enumerate(pats))
    m.addCons(joints <= j_cap + 1e-6)
    # 用料(不劣化)
    usedlen = quicksum(L * yc[(L, k)] for L, cps in cut_pats.items()
                       for k in range(len(cps)))
    m.addCons(usedlen <= u_cap + 1e-6)

    m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)

    def solve_pass(obj_expr, name, freeze_prev=None, pass_tl=None):
        """求一档并返回目标值; freeze_prev: (expr, val) 冻结上一档最优。"""
        m.freeTransform()
        m.setParam("limits/time", pass_tl if pass_tl is not None else tl)
        if freeze_prev is not None:
            expr, val = freeze_prev
            m.addCons(expr <= val + 1e-6)
        m.setObjective(obj_expr, "minimize")
        t0 = time.time()
        m.optimize()
        if m.getNSols() == 0:
            if verbose:
                print(f"  {name} 无解 status={m.getStatus()} ({time.time()-t0:.1f}s)",
                      flush=True)
            return None, time.time() - t0
        return round(m.getObjVal()), time.time() - t0

    zW_sum = quicksum(zW.values()) if zW else None
    zC_sum = quicksum(zC.values()) if zC else None
    zS_sum = quicksum(zS.values())

    # Pass3: min 拼法种类
    if zW_sum is not None:
        wt_star, dt = solve_pass(zW_sum, "Pass3(min拼法种类)")
        if wt_star is None:
            return None
        if verbose:
            print(f"  Pass3(min拼法种类): {wt_star} ({dt:.1f}s)", flush=True)
    else:
        wt_star = 0

    # Pass4: min 切法种类(冻结拼法种类) —— 切法收敛慢, 用 cut_tl 给更长时间
    ct_star, dt = solve_pass(
        zC_sum, "Pass4(min切法种类)",
        freeze_prev=(zW_sum, wt_star) if zW_sum is not None else None,
        pass_tl=cut_tl)
    if ct_star is None:
        return None
    if verbose:
        print(f"  Pass4(min切法种类): {ct_star} ({dt:.1f}s)", flush=True)

    # Pass5: min 段种类(冻结切法种类) —— 用料已由 u_cap 约束封顶(末档不再单列)
    st_star, dt = solve_pass(zS_sum, "Pass5(min段种类)",
                             freeze_prev=(zC_sum, ct_star) if zC_sum is not None else None)
    if st_star is None:
        return None
    if verbose:
        print(f"  Pass5(min段种类): {st_star} ({dt:.1f}s)", flush=True)

    return _extract_compress2(group, pipes, pipe_pats, xp, cut_pats, yc, stock_qty, m)


def _extract_compress2(group, pipes, pipe_pats, xp, cut_pats, yc, stock_qty, m):
    """从全列式压缩模型抽取解(拼法路径列 + 切法列)。"""
    weld_patterns = defaultdict(int)
    total_joints = 0
    for i, pats in pipe_pats.items():
        for k, seq in enumerate(pats):
            v = round(m.getVal(xp[(i, k)]))
            if v > 0:
                weld_patterns[(i, seq)] += v
                total_joints += (len(seq) - 1) * v
    cut_patterns = defaultdict(int)
    used_len = 0
    seg_used = set()
    for L, cps in cut_pats.items():
        for k, segs in enumerate(cps):
            v = round(m.getVal(yc[(L, k)]))
            if v > 0:
                cut_patterns[(L, tuple(sorted(segs)))] += v
                used_len += L * v
                seg_used.update(segs)
    weld_types = len({seq for (i, seq) in weld_patterns if len(seq) >= 2})
    util = group.demand_length / used_len if used_len else 0
    return {
        "joints": total_joints, "cut_types": len(cut_patterns), "weld_types": weld_types,
        "seg_types": len(seg_used), "used_len": used_len, "util": util,
        "cut_patterns": dict(cut_patterns), "weld_patterns": dict(weld_patterns),
        "solver": "compress",
    }


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


def _build_std_seg_set(group, stock_qty, S):
    """混合段集(整根管 + 标准段拆分片): 给定标准段 S, 段集包含
      (1) 每种管长本身(整根段, 0 焊口, 利于低焊口打包);
      (2) 标准段 S 及每管的拆分片 (L 用 S 分割后的剩余, 利于填满母料空隙)。
    整根段压焊口/拼法, 拆分片提供切割灵活性 -> 焊口/拼法/切法同步压低。
    返回排序段列表, 若某管无法在段集内合法拼出则返回 None。"""
    min_wd = group.min_weld_distance
    min_cut = max(group.min_cut_length, 1)
    max_stock = max(s.length for s in group.stocks)
    if S < max(min_wd, min_cut) or S > max_stock:
        return None
    segs = set()
    for p in group.pipes:
        L = p.length
        # (1) 整根段: 可整根切出(<=母料)则加入, 使该管可 0 焊口
        if min_cut <= L <= max_stock:
            segs.add(L)
        # (2) 标准段拆分片: 管可焊且比 S 长时, 拆成 S + (L-S)
        if p.max_joints >= 1 and L > S:
            tail = L - S
            if tail >= min_wd:  # 拆分片须 >= 最小焊距(作内部/端段)
                segs.add(S)
                segs.add(tail)
        # 校验: 该管至少有一种合法拼法(整根 或 拆分)
        if L > max_stock and (p.max_joints < 1 or L - S < min_wd or S > max_stock):
            return None
    return sorted(s for s in segs if min_cut <= s <= max_stock)


def _std_seg_candidates(group, stock_qty):
    """候选标准段 S: 各母料的 1/2, 1/3(优先能整除母料/使母料近乎无废者)。
    S 决定切层能否近乎无废填满母料 -> 决定标准段压缩是否可行, 故尝试多个候选。"""
    max_stock = max(s.length for s in group.stocks)
    lens = sorted(stock_qty)
    cands = []
    for L in lens:
        for d in (2, 3):
            s = L // d
            if s not in cands:
                cands.append(s)
    # 也试最短/最长母料的一半
    cands = [c for c in dict.fromkeys(cands) if 1 <= c <= max_stock]
    return cands


def _try_std_compress(group, pipes, stock_qty, ffd, tl=120.0, verbose=True):
    """FFD 兜底档的标准段种类压缩: 逐个候选标准段构造段集 -> _compress_types 压
    拼法/切法/段种类。可行性取决于段能否近乎无废填满母料(与母料是否规整强相关),
    直接试解判定; 找到第一个成功且不劣于 FFD 焊口 3 倍的解即采纳。"""
    stock_len = sum(L * q for L, q in stock_qty.items())
    best = None
    for S in _std_seg_candidates(group, stock_qty):
        seg_set = _build_std_seg_set(group, stock_qty, S)
        if not seg_set:
            continue
        ncut = 0
        skip = False
        for L in stock_qty:
            ncut += len(_enum_cut_patterns(L, seg_set, group.blade_margin))
            if ncut > 60000:
                skip = True
                break
        if skip:
            if verbose:
                print(f"  标准段压缩(S={S}): 切法列>{ncut} 过多 -> 试下一候选", flush=True)
            continue
        if verbose:
            print(f"  标准段压缩(S={S}): 段集={len(seg_set)} 切法列≈{ncut}", flush=True)
        comp = _compress_types(group, pipes, seg_set, stock_qty,
                               j_cap=ffd["joints"] * 3, u_cap=stock_len,
                               tl=min(tl, 60.0), verbose=verbose, col_cap=60000,
                               cut_tl=200.0)
        if comp is not None:
            best = comp
            break  # 采纳首个可行解(候选按母料规整度排序, 越前越优)
    return best


def _ffd_weld_incumbent(group):
    """带焊 FFD 保底可行解: 需求管按长降序首尾相接成链, 按母料逐根切满,
    管被母料边界截断处产生焊口。几何上总能构造(总长匹配), 利用率天然接近满。

    专治 L16/L20 这类'纯切不可行(库存根数<需求根数, 必须焊接省料)'的极限档:
    coarse-to-fine 每档 MILP 在 util 硬门槛下连一个可行整数解都构造不出而 timelimit,
    此 FFD 解作为兜底返回(可行且高利用率), 至少给出可对比的排料方案。
    返回与其它解同构的 dict, 或 None(理论上不会)。"""
    min_cut = max(group.min_cut_length, 1)
    kerf = group.blade_margin
    sq = defaultdict(int)
    for st in group.stocks:
        sq[st.length] += st.quantity
    stock = [[L, sq[L]] for L in sorted(sq, reverse=True)]
    pipes = group.pipes
    queue = []  # [type_idx, 剩余待填]
    for i, p in enumerate(pipes):
        for _ in range(p.demand):
            queue.append([i, p.length])
    queue.sort(key=lambda x: -x[1])
    weld_patterns = defaultdict(int)
    cut_patterns = defaultdict(int)
    joints = 0
    used_len = 0
    pi = 0
    cur_segs = []  # 当前管已焊上的段序列
    cur_type = None
    for L, qty in stock:
        for _ in range(qty):
            if pi >= len(queue):
                break
            pos = 0
            segs = []
            first = True
            while pi < len(queue) and pos < L:
                rem_bar = L - pos - (0 if first else kerf)
                if rem_bar < min_cut:
                    break
                if cur_type is None:
                    cur_type = queue[pi][0]
                need = queue[pi][1]
                cut = min(need, rem_bar)
                if cut < min_cut:
                    break
                if not first:
                    pos += kerf
                segs.append(cut)
                pos += cut
                first = False
                cur_segs.append(cut)
                queue[pi][1] -= cut
                if queue[pi][1] <= 0:
                    weld_patterns[(cur_type, tuple(cur_segs))] += 1
                    joints += len(cur_segs) - 1
                    cur_segs = []
                    cur_type = None
                    pi += 1
                else:
                    break  # 管被母料边界截断, 续接在下一根母料(产生焊口)
            if segs:
                cut_patterns[(L, tuple(sorted(segs)))] += 1
                used_len += L
    if pi < len(queue) or cur_segs:
        return None  # 库存不足以排下所有管(理论上前置分类已挡住 infeasible)
    seg_used = set()
    for (i, seq) in weld_patterns:
        seg_used.update(seq)
    util = group.demand_length / used_len if used_len else 0
    return {
        "joints": joints, "cut_types": len(cut_patterns),
        "weld_types": len(weld_patterns), "seg_types": len(seg_used),
        "used_len": used_len, "util": util,
        "cut_patterns": dict(cut_patterns), "weld_patterns": dict(weld_patterns),
        "solver": "ffd_weld",
    }


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
    # FFD/纯切初始可行解: 每根管=1整段(0 焊口), 段长=管长(必在每档段集内, 见
    # _grid_seg_set)。极限档(L16/L20)coarse 档 MILP 连一个可行整数解都构造不出
    # (SCIP 档内 timelimit 无解), 注入这个保底可行解作 warm start, 让每档从'已知
    # 可行点'出发改进, 而非从零空搜。纯切解本身 weld_patterns 为空, 这里补出显式整段拼法。
    ffd_warm = None
    if pure_incumbent is not None and pure_incumbent.get("joints", 1) == 0:
        wp = {}
        for i, p in enumerate(pipes):
            wp[(i, (p.length,))] = p.demand
        ffd_warm = {"joints": 0, "weld_patterns": wp,
                    "cut_patterns": pure_incumbent.get("cut_patterns", {})}
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
        # 热启动: 完整兜底档(段集是所有档超集)用当前 best 的拼法作初始解,
        # 让 SCIP 从已知焊口出发改进(而非 30s 从零空搜, 见 L11)。
        # 未拿到 best 前, 用 FFD/纯切保底可行解(0 焊口整段拼法)作 warm start,
        # 让极限档(L16/L20)每档至少有可行点出发, 而非档内空搜 timelimit。
        # 注意: 热启动时 joint_cap 放宽到 warm 焊口本身(而非 -1), 否则 warm 解
        # 违反上界被判不可行 -> 热启动失效。目标仍是 min 焊口, 会自动压到更低。
        if is_final and best is not None:
            warm = best
        elif best is None and ffd_warm is not None:
            warm = ffd_warm
        else:
            warm = None
        jcap = joint_cap
        if warm is not None and joint_cap is not None:
            jcap = warm["joints"]
        t0 = time.time()
        res = _solve_int_restricted(
            group, pipes, sub_graphs, seg_grid, stock_qty, max_stock,
            floor_cap, tl=share, verbose=verbose, joint_cap=jcap, warm=warm)
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
    if best is None:
        # 所有 grid 档 MILP 均无可行整数解(L16/L20: util 硬门槛下 SCIP 档内空搜
        # timelimit)。回退到带焊 FFD 保底可行解(几何构造, 高利用率), 至少给出方案。
        ffd = _ffd_weld_incumbent(group)
        if ffd is None:
            return None
        if verbose:
            print(f"  所有档 MILP 无解 -> 带焊 FFD 兜底: 焊口={ffd['joints']} "
                  f"util={ffd['util']:.4f} 拼法={ffd['weld_types']} 切法={ffd['cut_types']}",
                  flush=True)
        # FFD 段过散 -> 标准段量化 + 种类压缩(词典序压 拼法→切法→段), 修正 FFD 的
        # 海量花样。段能否近乎无废填满母料决定可行性(L16 母料规整可行, L20 混合母料不可行)。
        std_tl = 150.0
        comp = _try_std_compress(group, pipes, stock_qty, ffd, tl=std_tl,
                                 verbose=verbose)
        if comp is not None and comp["joints"] <= ffd["joints"] * 3:
            if verbose:
                print(f"  标准段压缩成功: 焊口={comp['joints']} 拼法={comp['weld_types']} "
                      f"切法={comp['cut_types']} 段={comp['seg_types']} util={comp['util']:.4f}",
                      flush=True)
            return comp
        return ffd
    # ── 种类压缩(拼法→切法→段): 在 best 实际产出的段集上重解, 精确压种类且修复
    # cutcg '>=' 耦合的段过产(verifier SEGMENT_BALANCE)。段集大/枚举爆炸时自动跳过。 ──
    comp_tl = max(15.0, min(remaining + 30.0, 60.0))  # remaining 可能<=0, 保证正下限
    best = _maybe_compress(group, pipes, best, stock_qty, tl=comp_tl, verbose=verbose)
    return best


def _maybe_compress(group, pipes, best, stock_qty, tl=60.0, verbose=True, seg_cap=16):
    """在 best 解产出的段集上跑种类压缩(_compress_types)。段集过大或无改进则返回原解。
    同时修复段平衡(压缩模型用 == 耦合, 切层产出恰等拼层消耗)。"""
    if best is None or not best.get("weld_patterns"):
        return best  # 纯切/无焊: 段本就整管, 无压缩空间
    seg_set = set()
    for (L, segs) in best["cut_patterns"]:
        seg_set.update(segs)
    for (i, seq) in best["weld_patterns"]:
        seg_set.update(seq)
    if len(seg_set) > seg_cap:
        if verbose:
            print(f"  种类压缩: 段集={len(seg_set)}>{seg_cap} 过大 -> 跳过(保留原解)", flush=True)
        return best
    comp = _compress_types(group, pipes, sorted(seg_set), stock_qty,
                           best["joints"], best["used_len"], tl=tl, verbose=verbose)
    if comp is None:
        return best
    # 只在词典序(拼法→切法→段)不劣时采纳(焊口/用料已由 cap 约束保证不劣)。
    better = (comp["weld_types"], comp["cut_types"], comp["seg_types"]) <= (
        best["weld_types"], best["cut_types"], best["seg_types"])
    if verbose:
        print(f"  种类压缩结果: 拼法 {best['weld_types']}->{comp['weld_types']} "
              f"切法 {best['cut_types']}->{comp['cut_types']} "
              f"段 {best['seg_types']}->{comp['seg_types']} 采纳={better}", flush=True)
    return comp if better else best


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
                          floor_cap, tl=120.0, verbose=True, joint_cap=None, warm=None):
    """限定段整数模型: 拼层 arc-flow(已按 producible 过滤) + 切层 arc-flow(仅 seg_prod)。
    两阶段: Pass1 min 用料(达门槛) -> Pass2 固定用料 min 焊口。切层弧数极小(段仅 ~49)。

    warm: 上一档得到的解(dict, 含 weld_patterns/cut_patterns)。若其所有段/焊点弧
    都存在于当前(更细)段集的弧集里, 则据此构造 SCIP 初始可行解注入(热启动),
    让求解器从'已知 J 焊口'出发改进, 而非从零搜索(见 L11: 完整档 30s 空搜)。"""
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
    # ── 热启动: 把上一档解映射到当前(更细)段集的弧流, 注入为初始可行解 ──
    if warm is not None:
        _try_warm_start(m, warm, g, f, fseg, used_vars, pipe_graphs, stock_qty, verbose)
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


def _try_warm_start(m, warm, g, f, fseg, used_vars, pipe_graphs, stock_qty, verbose):
    """把上一档解(weld_patterns)映射为拼层弧流, 作为部分初始解注入 SCIP。

    只设拼层弧流(焊口结构所在)与 used_vars 上界内的粗估; 切层弧由 SCIP 的
    completesol 启发式补全(放宽 maxunknownrate=1.0)。映射失败(弧不在当前图)则
    整体放弃热启动(不影响正确性, 仅退化为冷启动)。"""
    # 预建每管弧集查找: (a,b)->存在。
    arc_ok = {}
    for i, (pts, arcs) in pipe_graphs.items():
        arc_ok[i] = {(a, b) for (a, b, seg) in arcs}
    gflow = defaultdict(int)  # (i,(a,b)) -> count
    for (i, seq), cnt in warm.get("weld_patterns", {}).items():
        pos = 0
        ok = True
        for seg in seq:
            nb = pos + seg
            if (pos, nb) not in arc_ok.get(i, ()):  # 焊点弧须存在于当前图
                ok = False
                break
            pos = nb
        if not ok:
            if verbose:
                print("    热启动: 拼法弧不在当前段集图内 -> 放弃热启动", flush=True)
            return False
        pos = 0
        for seg in seq:
            gflow[(i, (pos, pos + seg))] += cnt
            pos += seg
    try:
        m.setParam("heuristics/completesol/maxunknownrate", 1.0)
    except Exception:
        pass
    sol = m.createPartialSol()
    for (i, ab), v in gflow.items():
        m.setSolVal(sol, g[i][ab], float(v))
    m.addSol(sol)
    if verbose:
        print(f"    热启动: 注入拼层弧流 {len(gflow)} 条(上一档 {warm.get('joints')} 焊口)",
              flush=True)
    return True


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
    diag = res.get("diagnosis")
    if diag and diag.get("codes"):
        print("  ── 诊断 ──")
        for code, msg in diag["codes"]:
            print(f"    [{code}] {msg}")


if __name__ == "__main__":
    main()
