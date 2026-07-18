"""外层 GA：进化"段长向量"，内层 ILP 评估。不喂答案，自己生成段。

依据 §11.38 八 + 子代理 f0d342ce 结构3（GA/SA 搜段长集合，老软件传闻所用）。
  - 染色体 = 每管型一个合法拆分（段元组），段集 = 所有拆分的并。
  - 适应度（车间口径，词典序）= 总焊口数少 → 拼种类少 → 切种类少 → 段种类少
    → 利用率高。内层 ILP 评估。

用法: python scripts/_exp_ga.py <samples.json> <id前缀>
        [--pop 30] [--gen 25] [--tl 20] [--seed 0]
"""
from __future__ import annotations

import functools
import json
import math
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.domain import parse_problem  # noqa: E402
from app.solver import _legal_pattern  # noqa: E402
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics  # noqa: E402
from _exp_ksplit import enum_cuts  # noqa: E402


# ───────────────────────── 内层评估 ─────────────────────────
def can_compose(pipe, segs, min_wd, min_cut):
    """管型能否用 segs 里的段合法拼出（存在性）。返回一个合法拆分或 None。"""
    L = pipe.length
    maxseg = pipe.max_joints + 1
    segs = sorted(set(segs), reverse=True)

    def dfs(remain, nseg, path):
        if remain == 0 and nseg >= 1:
            import itertools
            for perm in set(itertools.permutations(path)):
                if _legal_pattern(pipe, list(perm), min_wd, min_cut):
                    return perm
            return None
        if nseg >= maxseg:
            return None
        for s in segs:
            if s <= remain and (not path or s <= path[-1]):
                r = dfs(remain - s, nseg + 1, path + (s,))
                if r:
                    return r
        return None

    return dfs(L, 0, ())


def _find_legal_order(pipe, multiset, min_wd, min_cut, budget=2000):
    """给定段的多重集，回溯搜一个合法排列（累积焊点均在允许区且满足间距）。
    找到返回该排列(tuple)，否则 None。用节点预算防止 k! 爆炸。"""
    n = len(multiset)
    if n == 1:
        return tuple(multiset) if _legal_pattern(pipe, list(multiset), min_wd, min_cut) else None
    from collections import Counter
    avail = Counter(multiset)
    order = []
    nodes = [0]

    def bt(pos, placed):
        if nodes[0] > budget:
            return None
        nodes[0] += 1
        if placed == n:
            return tuple(order)
        for s in sorted(avail, reverse=True):
            if avail[s] <= 0:
                continue
            npos = pos + s
            is_last = (placed == n - 1)
            if not is_last:
                # 该段末尾是一个焊点：须允许 + 内部间距（非首段）
                if not pipe.weld_allowed(npos):
                    continue
                if placed >= 1 and s < min_wd:
                    continue
                if placed >= 1 and min_cut > 0 and s < min_cut:
                    continue
                if placed == 0 and min_cut > 0 and s < min_cut:
                    continue
            else:
                # 末段：≥ min_cut（整体 ≥2 段时）
                if min_cut > 0 and s < min_cut:
                    continue
            avail[s] -= 1
            order.append(s)
            r = bt(npos, placed + 1)
            if r is not None:
                return r
            order.pop()
            avail[s] += 1
        return None

    return bt(0, 0)


def enum_pipe_pats(pipe, segs, min_wd, min_cut, cap=3000, node_budget=200000,
                   seed_splits=None):
    """枚举管型用 segs 的所有合法拼法（按多重集去重，每个多重集找一个合法序）。

    node_budget: DFS 访问节点硬上限，防止大段字母表下搜索树指数爆炸→卡死。
    seed_splits: 必须包含的已知合法拆分（如该管型自身在个体里的拆分），保证
    即使 DFS 因预算截断也不会漏掉个体自带的可行拼法→避免误判不可行。
    """
    L = pipe.length
    maxseg = pipe.max_joints + 1
    segs = sorted(set(segs), reverse=True)
    pats = set()
    if seed_splits:
        for sp in seed_splits:
            order = _find_legal_order(pipe, tuple(sp), min_wd, min_cut)
            if order is not None:
                pats.add(order)
    nodes = [0]

    def dfs(remain, nseg, path):
        if len(pats) >= cap or nodes[0] >= node_budget:
            return
        nodes[0] += 1
        if remain == 0 and nseg >= 1:
            order = _find_legal_order(pipe, path, min_wd, min_cut)
            if order is not None:
                pats.add(order)
            return
        if nseg >= maxseg:
            return
        for s in segs:
            if s <= remain and (not path or s <= path[-1]):
                dfs(remain - s, nseg + 1, path + (s,))

    dfs(L, 0, ())
    return sorted(pats)


def evaluate(group, segs, target_len=None, slack=0.0, time_limit=20, exact=True, indiv=None):
    """给定段集，跑内层 ILP：min 总焊口数，用料≤target*(1+slack)。
    indiv: 可选的个体(每管型自身拆分)，用作各管型枚举的必含种子，防止大段
    字母表下 DFS 预算截断误判不可行。返回 dict 或 None（不可行/拼不出）。"""
    from pyscipopt import Model, quicksum
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    segs = sorted(set(int(s) for s in segs if s > 0))
    if not segs:
        return None
    # 各管型拼法
    pipe_pats = []
    for idx, pipe in enumerate(group.pipes):
        seed = [indiv[idx]] if indiv is not None else None
        pats = enum_pipe_pats(pipe, segs, min_wd, min_cut, seed_splits=seed)
        if not pats:
            return None
        pipe_pats.append(pats)
    used = set()
    for pats in pipe_pats:
        for p in pats:
            used.update(p)
    sigma = sorted(used)
    max_stock = max(s.length for s in group.stocks)
    bars = defaultdict(int)
    for st in group.stocks:
        bars[st.length] += st.quantity
    # 上限须足够切满一根定尺：短管/小段（如 200mm 段切 9600 定尺需 48 片）时
    # 固定 40 会截断所有合法切法 → 无解。取"最小段能填满最长定尺"所需片数。
    max_pieces = min(200, max_stock // max(1, min(sigma)) + 2)
    cuts = enum_cuts(group, sigma, group.blade_margin, max_pieces,
                     max_trim=max(600, min_cut))
    if not cuts:
        return None

    m = Model("eval")
    m.hideOutput()
    m.setParam("limits/time", time_limit)
    if not exact:
        m.setParam("limits/solutions", 1)
    u = {}
    for i, pats in enumerate(pipe_pats):
        for j in range(len(pats)):
            u[(i, j)] = m.addVar(vtype="I", lb=0)
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
    # 利用率不是优化目标, 也不是硬约束(用户认知: 利用率是好结果的自然奖励)。
    # 仅当显式传入 target_len 时才加用料上限; 默认只受库存约束。
    if target_len is not None:
        m.addCons(quicksum(cuts[ci][0] * x[ci] for ci in range(len(cuts))) <= int(target_len * (1 + slack)))
    # 车间真实目标（词典序）：① 总焊口数最少 ② 同焊口数下用料最省(利用率最高)。
    # 实测教训(20级): 只 min 焊口时 ILP 会"单管单料、剩料浪费"→ 利用率崩到74%
    # (段供需是 产出≥消耗, 多切的段可白白扔掉)。故加"用料"为次级目标。
    # SCIP 无原生词典序: 用加权 min joints*W + used_len, W 大到"减1焊口 > 任何
    # 用料差"即可保证焊口绝对优先, 同焊口下再挤利用率。
    joints_expr = quicksum((len(pipe_pats[i][j]) - 1) * u[(i, j)]
                           for (i, j) in u)
    usedlen_expr = quicksum(cuts[ci][0] * x[ci] for ci in range(len(cuts)))
    total_stock_len = sum(st.length * st.quantity for st in group.stocks)
    W = total_stock_len + 1  # 任一用料方案的 used_len < W, 故 1 焊口 > 全部用料差
    m.setObjective(joints_expr * W + usedlen_expr, "minimize")
    m.optimize()
    if m.getNSols() == 0:
        return None
    used_segs = {seg for seg in sigma if m.getVal(t[seg]) > 0.5}
    used_cut = [ci for ci in range(len(cuts)) if m.getVal(x[ci]) > 0.5]
    used_weld = [(i, j) for (i, j) in u if m.getVal(u[(i, j)]) > 0.5]
    total_joints = sum((len(pipe_pats[i][j]) - 1) * round(m.getVal(u[(i, j)]))
                       for (i, j) in used_weld)
    used_len = sum(cuts[ci][0] * round(m.getVal(x[ci])) for ci in used_cut)
    if used_len <= 0:
        return None
    return {
        "seg_types": len(used_segs), "segs": sorted(used_segs),
        "cut_types": len({(cuts[ci][0], tuple(sorted(cuts[ci][1].items()))) for ci in used_cut}),
        "weld_types": len({pipe_pats[i][j] for (i, j) in used_weld if len(pipe_pats[i][j]) >= 2}),
        "joints": total_joints,
        "used_len": used_len, "util": group.demand_length / used_len,
    }


# ───────────────────────── 合法焊点窗口 ─────────────────────────
def _weld_windows(pipe, min_cut, hi):
    """管型内所有合法焊点位置（从起点量）落在的区间列表 [(lo,hi),...]。

    焊点是沿管的绝对位置，可到 L-min_cut；不能用 hi 截断上界（hi 只约束单段
    长度，由 random_split 的 floor/ceil 逐段保证）。此前误用 hi 截断，导致
    长管(>hi)的靠后焊点无窗口可采，初始种群整体不可行 → 秒 FAIL。
    """
    L = pipe.length
    lo = max(min_cut, 1)
    high = L - max(min_cut, 1)
    if lo > high:
        return []
    # 从 [lo, high] 中挖掉禁焊区
    windows = [(lo, high)]
    for iv in pipe.forbidden:
        s, e = iv.start, iv.end
        nxt = []
        for wlo, whi in windows:
            if e < wlo or s > whi:
                nxt.append((wlo, whi))
                continue
            if s - 1 >= wlo:
                nxt.append((wlo, s - 1))
            if e + 1 <= whi:
                nxt.append((e + 1, whi))
        windows = nxt
    return windows


def _sample_pos(windows, rng):
    """按窗口总长加权，随机取一个合法焊点位置。"""
    if not windows:
        return None
    tot = sum(whi - wlo + 1 for wlo, whi in windows)
    r = rng.randrange(tot)
    for wlo, whi in windows:
        span = whi - wlo + 1
        if r < span:
            return wlo + r
        r -= span
    return None


def random_split(pipe, group, rng, hi):
    """随机生成管型的一个合法拆分（段和=管长，段数≤max_joints+1）。

    禁焊区感知：拆点只在合法焊点窗口（禁焊区之间的空隙）内采样。
    """
    L = pipe.length
    maxseg = pipe.max_joints + 1
    min_wd = group.min_weld_distance
    min_cut = group.min_cut_length

    # 整管（不焊）优先作为一个候选
    if L <= hi and _legal_pattern(pipe, [L], min_wd, min_cut):
        whole_ok = True
    else:
        whole_ok = False

    windows = _weld_windows(pipe, min_cut, hi)
    for _ in range(60):
        nseg = rng.randint(1, maxseg)
        if nseg == 1:
            if whole_ok:
                return (L,)
            continue
        if not windows:
            continue
        # 采 nseg-1 个升序焊点，相邻间距 ≥ min_wd(内部)/min_cut(端段)，
        # 且每个焊点落在合法窗口内。
        need = nseg - 1
        # 长管：nseg 段每段 ≤ hi 才可能拼成，先确保段数够
        if nseg * hi < L:
            continue
        positions = []
        ok = True
        prev = 0
        for k in range(need):
            # 该焊点最小值：受上一焊点 + 段下限约束（首段端段用 min_cut，内部用 min_wd）；
            # 且必须保证"剩余长度能被后续段(含末段)以每段≤hi容纳"。
            gap = min_cut if k == 0 else min_wd
            remain_welds = need - 1 - k          # 本焊点之后还有几个焊点
            remain_segs_after = remain_welds + 1  # 本焊点之后还有几段(含末段)
            floor = max(prev + gap, L - remain_segs_after * hi)
            # 该焊点最大值：本段(prev→p) ≤ hi，且给后续焊点+末段留够 min 空间
            ceil = min(prev + hi, L - max(min_cut, 1) - remain_welds * min_wd)
            cands = []
            for wlo, whi in windows:
                a, b = max(wlo, floor), min(whi, ceil)
                if a <= b:
                    cands.append((a, b))
            p = _sample_pos(cands, rng)
            if p is None:
                ok = False
                break
            positions.append(p)
            prev = p
        if not ok:
            continue
        # 末段 = L - 最后焊点，须 ≥ min_cut 且 ≤ hi
        last_seg = L - positions[-1]
        if not (max(min_cut, 1) <= last_seg <= hi):
            continue
        segs = []
        prev = 0
        for p in positions:
            segs.append(p - prev)
            prev = p
        segs.append(last_seg)
        if any(s > hi or s <= 0 for s in segs):
            continue
        if _legal_pattern(pipe, segs, min_wd, min_cut):
            return tuple(sorted(segs, reverse=True))
    # 兜底：整管
    return (L,) if whole_ok else None


def make_individual(group, rng, hi, shared_hint=None):
    """个体 = 每管型一个合法拆分。shared_hint: 优先复用的段集（促进少段）。"""
    indiv = []
    for pipe in group.pipes:
        sp = None
        if shared_hint:
            sp = compose_from(pipe, shared_hint, group, rng)
        if sp is None:
            sp = random_split(pipe, group, rng, hi)
        if sp is None:
            return None
        indiv.append(sp)
    return indiv


def compose_from(pipe, seg_pool, group, rng):
    """尝试仅用 seg_pool 里的段拼成管型（促进段复用）。"""
    r = can_compose(pipe, seg_pool, group.min_weld_distance, group.min_cut_length)
    return tuple(sorted(r, reverse=True)) if r else None


def indiv_segs(indiv):
    s = set()
    for sp in indiv:
        s.update(sp)
    return sorted(s)


def fitness_key(res, target_rate=0.0):
    """词典序(车间口径): 利用率软下界作准入门槛 -> 焊口第一 -> 拼 -> 切 -> 段 -> 利用率。

    实测教训(20级): 若利用率不参与主排序, GA 会选"0焊口但利用率74%"的垃圾解
    (料浪费一半, 车间绝不接受)。修法: 把"是否达到车间下界 target_rate"作为最外层
    门槛 tier(达标=1 优于 跌破=0)。含义:
      - 只要存在达标解, 绝不选跌破下界的解(哪怕它焊口更少)——废料解不是真优势。
      - 达标区内部: 焊口最少优先(用户口径"焊口第一"完全保留), 再拼->切->段->利用率。
      - 全部跌破时(极端): 按利用率缺口大小排(越接近下界越好), 仍焊口次之。
    target_rate 来自输入 Target_Util_Rate(默认99.25%), 非老软件答案。
    """
    if res is None:
        return (-1, -(10**9), -999, -999, -999, 0.0)
    meets = 1 if res["util"] >= target_rate - 1e-9 else 0
    return (meets, -res["joints"], -res["weld_types"], -res["cut_types"],
            -res["seg_types"], round(res["util"], 5))


def mutate_indiv(indiv, group, rng, hi):
    """变异：重切某管型（引入新段）或改用复用现有段。"""
    new = list(indiv)
    i = rng.randrange(len(new))
    pipe = group.pipes[i]
    if rng.random() < 0.5:
        sp = random_split(pipe, group, rng, hi)
    else:
        pool = set()
        for j, sp2 in enumerate(new):
            if j != i:
                pool.update(sp2)
        sp = compose_from(pipe, sorted(pool), group, rng) if pool else None
        if sp is None:
            sp = random_split(pipe, group, rng, hi)
    if sp is not None:
        new[i] = sp
    return new


def crossover_indiv(a, b, rng, group):
    """交叉：用合并段池复用促进少段，否则取一父本。"""
    pool = set()
    for x, y in zip(a, b):
        pool.update(x)
        pool.update(y)
    child = []
    for i, (x, y) in enumerate(zip(a, b)):
        pipe = group.pipes[i]
        sp = compose_from(pipe, sorted(pool), group, rng)
        if sp is None:
            sp = rng.choice([x, y])
        child.append(sp)
    return child


def ga_run(group, pop_size, gens, tl, rng, verbose=True, patience=8):
    """纯自约束求解: 只优化 焊口->拼->切->段(利用率是自然结果)。
    不接收任何老软件指标。停止条件=收敛(连续 patience 代无改进)或跑满。"""
    max_stock = max(s.length for s in group.stocks)
    hi = min(max_stock, max(p.length for p in group.pipes))

    best_res = None
    best_segs = None

    pop = []
    tries = 0
    while len(pop) < pop_size and tries < pop_size * 20:
        tries += 1
        indiv = make_individual(group, rng, hi)
        if indiv is not None:
            pop.append(indiv)
    if not pop:
        if verbose:
            print("  no feasible initial individual")
        return None, None

    no_improve = 0
    for gen in range(gens):
        scored = []
        for indiv in pop:
            segs = indiv_segs(indiv)
            # target_len=None: 不加利用率上限, 只受库存约束
            res = evaluate(group, segs, None, 0.0, tl, exact=True, indiv=indiv)
            scored.append((fitness_key(res, group.target_rate), indiv, res))
        scored.sort(key=lambda z: z[0], reverse=True)
        improved = False
        if scored[0][2] is not None:
            if best_res is None or fitness_key(scored[0][2], group.target_rate) > fitness_key(best_res, group.target_rate):
                best_res, best_segs = scored[0][2], indiv_segs(scored[0][1])
                improved = True
        no_improve = 0 if improved else no_improve + 1
        if verbose:
            r = scored[0][2]
            tag = (f"joints={r['joints']} weld={r['weld_types']} cut={r['cut_types']} "
                   f"seg={r['seg_types']} util={r['util']:.4f}"
                   if r else "infeasible")
            print(f"  gen {gen}: best {tag}", flush=True)
        # 收敛停止(自约束, 不看老软件): 连续 patience 代无改进
        if best_res is not None and no_improve >= patience:
            if verbose:
                print(f"  -> gen {gen} converged (no improve {no_improve} gens), stop", flush=True)
            break
        elite = [z[1] for z in scored[:max(2, pop_size // 4)]]
        newpop = list(elite)
        guard = 0
        while len(newpop) < pop_size and guard < pop_size * 20:
            guard += 1
            pa, pb = rng.choice(elite), rng.choice(elite)
            child = crossover_indiv(pa, pb, rng, group)
            if rng.random() < 0.9:
                child = mutate_indiv(child, group, rng, hi)
            if all(sp for sp in child):
                newpop.append(child)
        pop = newpop
    return best_res, best_segs


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    path, pref = sys.argv[1], sys.argv[2]

    def argf(name, default, cast=float):
        return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default

    pop = argf("--pop", 30, int)
    gen = argf("--gen", 25, int)
    tl = argf("--tl", 20.0, float)
    seed = argf("--seed", 0, int)
    rng = random.Random(seed)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
    for s in recs:
        sid = s.get("id") or s.get("ID") or ""
        if not sid.startswith(pref):
            continue
        prob = json.loads(s["MOMPROBLEMJSON"]) if "MOMPROBLEMJSON" in s else s.get("problem")
        group = merge_equivalent_pipes(parse_problem(prob).groups[0])
        _o, lm = legacy_alpha_and_metrics(s)
        print(f"id={sid[:12]} 管长={sorted({p.length for p in group.pipes})} "
              f"定尺={sorted({st.length for st in group.stocks})}")
        print(f"  老软件: util={lm['util']:.4f} cut_types={lm['cut_types']} weld_types={lm['weld_types']}")
        t0 = time.monotonic()
        res, segs = ga_run(group, pop, gen, tl, rng)
        dt = time.monotonic() - t0
        if res is None:
            print(f"  GA 未找到可行解（{dt:.1f}s）")
            return

        def cmp(new, old, low=True):
            if abs(new - old) < 1e-9:
                return "[=]"
            return "[BETTER]" if ((new < old) == low) else "[WORSE]"

        print("  ══ GA 自生成段（不喂答案） vs 老软件 ══")
        print(f"    利用率:   {res['util']:.4f} vs {lm['util']:.4f} {cmp(res['util'], lm['util'], low=False)}")
        print(f"    切法种类: {res['cut_types']} vs {lm['cut_types']} {cmp(res['cut_types'], lm['cut_types'])}")
        print(f"    拼法种类: {res['weld_types']} vs {lm['weld_types']} {cmp(res['weld_types'], lm['weld_types'])}")
        print(f"    段种类:   {res['seg_types']}  → {res['segs']}")
        print(f"    GA 总耗时 {dt:.1f}s")
        return
    print("未找到样本")


if __name__ == "__main__":
    main()
