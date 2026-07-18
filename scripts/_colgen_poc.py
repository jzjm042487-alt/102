"""列生成 POC (Column Generation) —— 治本方案验证, 独立模块, 不碰生效代码。

对应用户思路三步:
  ① 整排优先        -> 初始列含"整管"切法/拼法
  ② 按合法窗口切分   -> 拼法定价的 RCSP 在合法焊口窗口放焊口(复用 _legal_pattern)
  ③ 拼接段按需生成   -> 不穷举, 用对偶价格 pricing 定向生成最值钱的切法/拼法列

结构:
  主问题(LP松弛): 变量 = 切法列 x[c] + 拼法列 y[p]
    约束(段供需): sum_c a[s,c]*x[c] - sum_p b[s,p]*y[p] >= 0   (对偶 pi[s]>=0)
         (需求) : sum_{p of pipe i} y[p] = demand_i             (对偶 mu[i] 自由)
         (库存) : sum_{c of stock L} x[c] <= stock_qty[L]        (对偶 <=0)
    目标: min 用料总长 (先把利用率顶到最优, 达标是准入门槛)
  切法定价: 对每种定尺 L, 背包 max sum_s pi[s]*n_s  s.t. sum n_s*s <= L
            reduced cost = L(用料成本) - sum pi[s]*n_s ; <0 则加列
    (说明: 目标是 min 用料, 切法列贡献用料成本=L, 段值 pi[s])
  拼法定价: 对每个管型 i, RCSP 在管身合法窗口放焊口, 找段序列 seq(sum=Li,
            合法), 最大化 sum_s pi[s]*cnt_s ; reduced cost = -sum pi[s]*cnt + (-mu_i)
            <0 则加列。 (拼法不耗用料, 只消耗段)

用法: python scripts/_colgen_poc.py <level> [--max_iter 60 --tl 30]
"""
import json
import sys
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


def derive_seg_alphabet(group):
    """派生结构化候选段长(Agoston2019: 母料边界导出位置 = 管长/定尺的整数组合)。
    不靠猜: 段长来自'定尺 - 管长的若干倍'与'管长 - 定尺余料的若干倍'等组合。
    这给 pricing 提供'有米下锅'的候选, 新段由此进入模型。返回 sorted set。
    """
    stock_lens = sorted({s.length for s in group.stocks})
    pipe_lens = sorted({p.length for p in group.pipes})
    # 段长下限: 焊接段必须 >= min_weld_distance(否则不是合法可焊段), 避免 1mm 碎段污染
    min_seg = max(group.min_cut_length, group.min_weld_distance, 1)
    max_seg = min(max(stock_lens), max(pipe_lens))
    segs = set()
    # ① 管长本身(整排, 仅当能被单根定尺容纳)
    for pl in pipe_lens:
        if pl <= max(stock_lens):
            segs.add(pl)
    # ①b 管>料(必焊场景): 段候选含"定尺整段"及"管长对定尺取余的收尾段"
    for pl in pipe_lens:
        for L in stock_lens:
            if pl > L:
                if min_seg <= L <= max_seg:
                    segs.add(L)              # 整根定尺用满作为一段
                rem = pl % L
                if min_seg <= rem <= max_seg:
                    segs.add(rem)            # 收尾余段
                # 用 k 根定尺后剩余 (pl - k*L)
                k = 1
                while k * L < pl:
                    r = pl - k * L
                    if min_seg <= r <= max_seg:
                        segs.add(r)
                    k += 1
    # ② 定尺切若干整管后的余料: L - k*pl (管<料)
    for L in stock_lens:
        for pl in pipe_lens:
            k = 1
            while k * pl <= L:
                r = L - k * pl
                if min_seg <= r <= max_seg:
                    segs.add(r)
                k += 1
    # ③ 管长减若干余料段 = 拼接补段: pl - r (让余料能配对拼回管)
    base = set(segs)
    for pl in pipe_lens:
        for r in base:
            d = pl - r
            if min_seg <= d <= max_seg:
                segs.add(d)
    # ④ 余料的整数倍(多段余料拼一段): r*k
    for r in list(segs):
        for k in (2, 3):
            if min_seg <= r * k <= max_seg:
                segs.add(r * k)
    return sorted(s for s in segs if s >= min_seg)


def enum_legal_seqs(pipe, alphabet, min_wd, min_cut, max_stock, budget=50000):
    """枚举管型 pipe 用字母表段拼成的所有合法序列(sum=length, 满足焊口约束)。
    RCSP风格 DFS 带节点预算。合法性用 _legal_pattern 复核。返回去重序列列表。
    """
    L = pipe.length
    cand = sorted({s for s in alphabet if 0 < s <= min(L, max_stock)}, reverse=True)
    out = set()
    nodes = [0]

    def dfs(pos, seq):
        nodes[0] += 1
        if nodes[0] > budget:
            return
        if pos == L:
            if _legal_pattern(pipe, tuple(seq), min_wd, min_cut):
                out.add(tuple(seq))
            return
        if len(seq) >= pipe.max_joints + 1:
            return
        remain = L - pos
        for s in cand:
            if s <= remain:
                seq.append(s)
                dfs(pos + s, seq)
                seq.pop()

    dfs(0, [])
    # 整管兜底(仅当整管能被单根定尺容纳)
    if L <= max_stock and _legal_pattern(pipe, (L,), min_wd, min_cut):
        out.add((L,))
    return list(out)


# ───────────────────── RCSP 拼法定价: 沿合法焊点动态生成段序列 ─────────────────────
def rcsp_price_welding(pipe, pi, min_wd, min_cut, max_stock, max_new_seg=6, w_j=0.0,
                       extra_segs=None, full_positions=False):
    """RCSP: 为管型 pipe 找 reduced cost 最负的合法段序列。
    不依赖预设字母表 —— 段长由'合法焊点位置差'动态产生, 天然感知禁焊区。
    打分 = sum(pi[seg]) - w_j*(段数-1); w_j>0 时偏好少焊口拼法。
    extra_segs: 切法侧当前能产出的段集合, 让拼法能消费这些段(闭合切拼环)。

    DP 沿管身: state=(pos, nseg), 只在 weld_allowed(pos) 或 pos∈{0,length} 处切分。
    f[pos] = 到达 pos 的最大累计段价 sum(pi[seg]); 段=pos-prev, 需满足:
      段 <= max_stock (单段必须能从一根定尺切出),
      段 >= min_cut (若被焊接), 相邻内焊距 >= min_wd, 段数 <= max_joints+1。
    返回 (best_value, best_seq)。合法性最终用 _legal_pattern 复核。

    关键: 位置集合 = {0, length} ∪ 合法焊点。为控规模, 合法焊点做'定尺边界锚定'采样:
      候选切点 pos 使 pos 落在某根定尺的整数倍附近 (pos ≈ k*max_stock 的合法邻近点),
      以及 pi 中已有段能拼出的位置。这样 DP 既发现新段又不爆炸。
    """
    L = pipe.length
    # ── 生成候选切点(位置) ──
    cut_positions = {0, L}
    # ① 已有段(pi中)能累加到的位置(让已知有价段进入)
    known = sorted(s for s in pi if 0 < s <= max_stock)
    # ①b 切法侧可产段: 让拼法能消费这些段, 闭合"切→拼"环(紧料关键)
    if extra_segs:
        known = sorted(set(known) | {s for s in extra_segs if 0 < s <= max_stock})
    # ①c 紧料治本: 放开到"所有合法焊点"(不只锚点), 让DP发现任意毫米级最优段。
    #    最优分割点由全局LP对偶价决定, 无法算术预派生(见L12/L13联合ILP用离散字母表infeasible)。
    if full_positions:
        for pos in range(1, L):
            if pipe.weld_allowed(pos):
                cut_positions.add(pos)
    # ② 定尺边界锚点: 每根定尺切满后的位置 ~ k*max_stock, 取其合法邻近焊点
    k = 1
    while k * max_stock < L:
        anchor = k * max_stock
        # 向下找最近合法焊点(定尺装满后必须在此之前焊)
        p = _nearest_legal_below(pipe, anchor, max(1, anchor - max_stock + 1))
        if p is not None:
            cut_positions.add(p)
        k += 1
    # ③ 已有段的组合位置(前向传播)
    reach = {0}
    for _ in range(pipe.max_joints + 1):
        newr = set()
        for r in reach:
            for s in known:
                np_ = r + s
                if np_ <= L and (np_ == L or pipe.weld_allowed(np_)):
                    newr.add(np_)
        reach |= newr
    cut_positions |= {r for r in reach if r <= L}
    positions = sorted(p for p in cut_positions if p == 0 or p == L or pipe.weld_allowed(p))
    pos_idx = {p: i for i, p in enumerate(positions)}

    # ── DP: f[(pos, nseg)] = 最大累计段价 ──
    NEG = float("-inf")
    # dp[i] over positions, track best value and predecessor seq
    best = [NEG, None]
    # 用 (pos)->list of (value, seq) 但控制 seg 数; 简化为一维最优 + 回溯
    f = {0: (0.0, ())}
    for pos in positions:
        if pos == 0:
            continue
        bestv, bestseq = NEG, None
        for prev in positions:
            if prev >= pos:
                break
            if prev not in f:
                continue
            seg = pos - prev
            if seg > max_stock or seg <= 0:
                continue
            pv, pseq = f[prev]
            nseg = len(pseq) + 1
            if nseg > pipe.max_joints + 1:
                continue
            # 内焊距约束(除首段外相邻段>=min_wd)由 _legal_pattern 最终校验; 这里粗筛
            val = pv + pi.get(seg, 0.0) - (w_j if len(pseq) >= 1 else 0.0)
            if val > bestv:
                bestv, bestseq = val, pseq + (seg,)
        if bestseq is not None:
            f[pos] = (bestv, bestseq)
            if pos == L and bestv > best[0]:
                if _legal_pattern(pipe, bestseq, min_wd, min_cut):
                    best[0], best[1] = bestv, bestseq
    # 若一维最优在L处非法, 退回枚举L处所有候选(小规模)
    if best[1] is None and L in f:
        # 重新在到达L的所有前驱里挑合法的
        for prev in positions:
            if prev >= L or prev not in f:
                continue
            seg = L - prev
            if seg > max_stock or seg <= 0:
                continue
            pv, pseq = f[prev]
            cand = pseq + (seg,)
            if _legal_pattern(pipe, cand, min_wd, min_cut):
                v = pv + pi.get(seg, 0.0)
                if v > best[0]:
                    best[0], best[1] = v, cand
    return (best[0] if best[1] else 0.0), best[1]


def _nearest_legal_below(pipe, high, low):
    high = min(pipe.length - 1, high)
    low = max(1, low)
    for pos in range(high, low - 1, -1):
        if pipe.weld_allowed(pos):
            return pos
    return None


def rcsp_bnp_price(pipe, pi, min_wd, min_cut, max_stock, w_j=0.0, cand_extra=None,
                   max_states=20000, top_k=40):
    """高效毫米级 RCSP(候选段驱动无界-序列 DP), 带状态数上限防爆炸。
    候选段 K = 对偶价最高 top_k 段 ∪ cand_extra ∪ '收尾到L的补段'。
    f[pos] = 到达管身位置 pos 的最优 (Σpi[seg] − w_j×焊口数) + 段序列;
    只在 pipe.weld_allowed(pos)(或 pos∈{0,L}) 处允许分割。段长离散但由
    '全局对偶价 + 收尾补段' 驱动, 覆盖离散字母表漏掉的余料回收段。
    """
    L = pipe.length
    # 候选段: 取对偶价 top_k(降序) + cand_extra
    priced = sorted((s for s in pi if 0 < s <= max_stock and pi[s] > 1e-9),
                    key=lambda s: -pi[s])[:top_k]
    cand = set(priced)
    if cand_extra:
        cand |= {s for s in cand_extra if 0 < s <= max_stock}
    # 收尾补段: 让 DP 从候选段可达的合法位置精确落到 L
    base = set(cand)
    reach = {0}
    for _ in range(pipe.max_joints + 1):
        nr = set()
        for r in reach:
            for s in base:
                q = r + s
                if q < L and pipe.weld_allowed(q):
                    nr.add(q)
        reach |= nr
        if len(reach) > max_states:
            break
    for r in reach:
        tail = L - r
        if 0 < tail <= max_stock:
            cand.add(tail)
    if L <= max_stock:
        cand.add(L)
    cand = sorted(cand)
    if not cand:
        return 0.0, None

    NEG = float("-inf")
    f = {0: (0.0, ())}
    order = [0]
    seen = {0}
    best = (NEG, None)
    idx = 0
    while idx < len(order):
        if len(order) > max_states:
            break
        prev = order[idx]; idx += 1
        pv, pseq = f[prev]
        if len(pseq) > pipe.max_joints + 1:
            continue
        for s in cand:
            pos = prev + s
            if pos > L:
                break  # cand 升序
            if pos != L and not pipe.weld_allowed(pos):
                continue
            nseg = len(pseq) + 1
            if nseg > pipe.max_joints + 1:
                continue
            val = pv + pi.get(s, 0.0) - (w_j if len(pseq) >= 1 else 0.0)
            cur = f.get(pos)
            if cur is None or val > cur[0]:
                f[pos] = (val, pseq + (s,))
                if pos not in seen:
                    seen.add(pos); order.append(pos)
            if pos == L:
                v2, sq2 = f[L]
                if v2 > best[0] and _legal_pattern(pipe, sq2, min_wd, min_cut):
                    best = (v2, sq2)
    if best[1] is None and L in f:
        for prev in order:
            if prev >= L:
                continue
            s = L - prev
            if not (0 < s <= max_stock):
                continue
            pv, pseq = f[prev]
            cand_seq = pseq + (s,)
            if len(cand_seq) <= pipe.max_joints + 1 and _legal_pattern(pipe, cand_seq, min_wd, min_cut):
                v = pv + pi.get(s, 0.0) - (w_j * (len(cand_seq) - 1))
                if v > best[0]:
                    best = (v, cand_seq)
    return (best[0] if best[1] else 0.0), best[1]


# ───────────────────── 拼法定价: RCSP(合法窗口段序列) ─────────────────────
def price_welding(pipe, pi, min_wd, min_cut, max_stock, alphabet, node_budget=200000):
    """给定段对偶价 pi 和候选字母表 alphabet, 为管型找'最值钱'的合法段序列。
    候选段 = alphabet(结构化派生) 中 <=min(L,max_stock) 的段。用对偶价(含0)评分,
    使新段(pi=0)也能被选入 -> 打破'段驱动循环依赖'。合法性用 _legal_pattern 复核。
    """
    L = pipe.length
    cand = sorted({s for s in alphabet if 0 < s <= min(L, max_stock)}, reverse=True)
    best = [0.0, None]
    nodes = [0]

    def dfs(pos, seq, val):
        nodes[0] += 1
        if nodes[0] > node_budget:
            return
        if pos == L:
            if _legal_pattern(pipe, tuple(seq), min_wd, min_cut) and val > best[0]:
                best[0], best[1] = val, tuple(seq)
            return
        nseg = len(seq)
        if nseg >= pipe.max_joints + 1:  # 段数超限
            return
        remain = L - pos
        # 尝试每个候选段长(不超过剩余)
        for s in cand:
            if s > remain:
                continue
            # 最后一段必须正好收尾: 若放 s 后 remain-s>0 还得能再放至少一段
            newpos = pos + s
            if newpos < L and (L - newpos) < 1:
                continue
            seq.append(s)
            dfs(newpos, seq, val + pi.get(s, 0.0))
            seq.pop()
        # 也允许"直接用剩余整段收尾"(即使它没对偶价), 保证能拼满
        if remain not in cand and remain > 0:
            seq.append(remain)
            if pos + remain == L and _legal_pattern(pipe, tuple(seq), min_wd, min_cut):
                v = val + pi.get(remain, 0.0)
                if v > best[0]:
                    best[0], best[1] = v, tuple(seq)
            seq.pop()

    dfs(0, [], 0.0)
    return best[0], best[1]


# ───────────────────── 切法定价: 有界背包 ─────────────────────
def price_cutting(stock_len, pi, kerf, min_seg, alphabet, fill_bonus=0.0, need_segs=None):
    """在一根定尺 stock_len 上选一组段最大化 sum pi[s]*n_s + fill_bonus*已用长度。
    候选段 = alphabet 中有正对偶价的段; fill_bonus>0 时纳入全部 need_segs 并奖励填满
    (零浪费混切, 治紧料: 把余料变成另一管所需段)。无界背包 DP(按毫米)。
    """
    if fill_bonus > 0 and need_segs:
        # 紧料模式: 候选 = 所有需求段, 目标 = 对偶价 + 填充长度奖励(逼近零余料)
        cand = sorted(s for s in set(alphabet) | set(need_segs) if 0 < s <= stock_len)
        weight = {s: pi.get(s, 0.0) + fill_bonus * s for s in cand}
    else:
        cand = sorted(s for s in alphabet if 0 < s <= stock_len and pi.get(s, 0) > 1e-9)
        weight = {s: pi.get(s, 0.0) for s in cand}
    if not cand:
        return 0.0, {}
    dp = [0.0] * (stock_len + 1)
    pick = [-1] * (stock_len + 1)
    for cap in range(1, stock_len + 1):
        best_v, best_s = dp[cap - 1], -2
        for s in cand:
            if s <= cap:
                v = weight[s] + dp[cap - s]
                if v > best_v:
                    best_v, best_s = v, s
        dp[cap] = best_v
        pick[cap] = best_s
    counts = defaultdict(int)
    cap = stock_len
    while cap > 0:
        s = pick[cap]
        if s is None or s < 0:
            cap -= 1
            continue
        counts[s] += 1
        cap -= s
    # 返回值只算对偶价部分(用于 reduced cost 判断), 与 fill_bonus 无关
    val = sum(pi.get(s, 0.0) * n for s, n in counts.items())
    return val, dict(counts)


def solve_colgen(group, max_iter=60, tl=30, verbose=True):
    from pyscipopt import Model, quicksum
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    kerf = group.blade_margin
    max_stock = max(s.length for s in group.stocks)
    stock_qty = defaultdict(int)
    for st in group.stocks:
        stock_qty[st.length] += st.quantity
    pipes = group.pipes
    alphabet = derive_seg_alphabet(group)
    if not alphabet:
        if verbose:
            print("  字母表为空(无法派生任何合法段) -> 跳过")
        return None
    if verbose:
        print(f"  派生候选段字母表: n={len(alphabet)} {alphabet[:20]}{'...' if len(alphabet)>20 else ''}")

    # ── 初始列: 字母表静态枚举 + RCSP 保底(每管型至少一个合法拼法) ──
    # 拼法列: 字母表段的合法序列; 再用 RCSP 沿合法焊点补一个保底列(感知禁焊区)
    weld_cols = []
    for i, p in enumerate(pipes):
        seqs = enum_legal_seqs(p, alphabet, min_wd, min_cut, max_stock)
        for seq in seqs:
            weld_cols.append((i, seq))
        if not seqs:
            # 字母表拼不出(如管>料+禁焊区): RCSP 沿合法焊点动态生成一个合法拼法
            _, seq = rcsp_price_welding(p, {}, min_wd, min_cut, max_stock)
            if seq is not None:
                weld_cols.append((i, seq))
    # 切法列: 初始只播种少量'密排字母表'切法(不做60000级全枚举);
    # 迭代循环靠 price_cutting/gap驱动按需补列, 保持列池小 -> final_ilp 可解。
    from scripts._exp_ksplit import enum_cuts
    seed_segs = set(alphabet)
    for (_, seq) in weld_cols:
        seed_segs.update(seq)
    sigma = sorted(s for s in seed_segs if 0 < s <= max_stock)
    max_pieces = min(200, max_stock // max(1, min(sigma)) + 2) if sigma else 2
    # 初始只取每定尺的密排切法(cap 小); 其余靠 pricing 生成
    raw_cuts = enum_cuts(group, sigma, kerf, max_pieces, max_trim=max_stock, cap=3000)
    cut_cols = [(L, cc) for (L, cc) in raw_cuts]
    if verbose:
        print(f"  列池(初始): 拼法={len(weld_cols)} 切法={len(cut_cols)}")
    # 松料样本静态列池已足够; 紧料/禁焊区样本继续 RCSP+Farkas 迭代补列(下方循环)

    def all_segs():
        s = set(alphabet)  # 全字母表都建约束, 保证每段都有对偶价(打破循环依赖)
        for (_, cc) in cut_cols:
            s.update(cc)
        for (_, seq) in weld_cols:
            s.update(seq)
        return sorted(s)

    stall = 0
    for it in range(max_iter):
        # ── 主问题 LP: min 用料 + 大M×人工变量(Phase-I, 保证永不infeasible) ──
        from pyscipopt import SCIP_PARAMSETTING
        m = Model("master")
        m.hideOutput()
        m.setParam("limits/time", tl)
        m.setPresolve(SCIP_PARAMSETTING.OFF)
        m.setHeuristics(SCIP_PARAMSETTING.OFF)
        m.disablePropagation()
        segs = all_segs()
        BIGM = 10 * sum(st.length * st.quantity for st in group.stocks) + 1
        x = {c: m.addVar(vtype="C", lb=0) for c in range(len(cut_cols))}
        y = {p: m.addVar(vtype="C", lb=0) for p in range(len(weld_cols))}
        # 人工变量: 需求缺口 a[i](未满足的管需求, 罚 BIGM)
        a = {i: m.addVar(vtype="C", lb=0) for i in range(len(pipes))}
        # 段供需 >= 0
        seg_cons = {}
        for s in segs:
            prod = quicksum(cut_cols[c][1].get(s, 0) * x[c] for c in range(len(cut_cols)))
            consume = quicksum(weld_cols[p][1].count(s) * y[p] for p in range(len(weld_cols)))
            seg_cons[s] = m.addCons(prod - consume >= 0)
        # 需求 == demand (人工变量 a[i] 补缺口, 使LP恒可行)
        dem_cons = {}
        for i, p in enumerate(pipes):
            dem_cons[i] = m.addCons(
                quicksum(y[pp] for pp in range(len(weld_cols)) if weld_cols[pp][0] == i)
                + a[i] == p.demand)
        # 库存 <= qty
        for L in stock_qty:
            m.addCons(quicksum(x[c] for c in range(len(cut_cols)) if cut_cols[c][0] == L)
                      <= stock_qty[L])
        # 目标: min 用料 + 小焊口惩罚(引导定价出少焊口拼法) + 大M人工变量
        # w_j 远小于用料尺度(不改利用率最优), 但足以在平局时偏好少焊口。
        w_j = 1.0
        joints_lp = quicksum((len(weld_cols[p][1]) - 1) * y[p] for p in range(len(weld_cols)))
        m.setObjective(
            quicksum(cut_cols[c][0] * x[c] for c in range(len(cut_cols)))
            + w_j * joints_lp
            + BIGM * quicksum(a[i] for i in range(len(pipes))), "minimize")
        m.optimize()
        if m.getNSols() == 0:
            if verbose:
                print(f"  it{it}: 主问题无解({m.getStatus()})")
            return None
        # 未满足缺口(人工变量>0 说明还差合法拼法列)
        gap = sum(m.getVal(a[i]) for i in range(len(pipes)))
        used_len = sum(cut_cols[c][0] * m.getVal(x[c]) for c in range(len(cut_cols)))
        pi = {s: max(0.0, m.getDualsolLinear(seg_cons[s])) for s in segs}
        mu = {i: m.getDualsolLinear(dem_cons[i]) for i in range(len(pipes))}

        if verbose:
            util = group.demand_length / used_len if used_len > 0 else 0
            print(f"  it{it}: 用料={used_len:.0f} util={util:.4f} 缺口={gap:.1f} "
                  f"cols(cut={len(cut_cols)},weld={len(weld_cols)}) segs={len(segs)}")

        # ── 定价 ──
        added = 0
        # 拼法(RCSP沿合法焊点, 感知禁焊区): 缺口>0时优先补合法拼法列
        # extra_segs = 切法侧当前能产出的所有段, 让拼法消费它们(闭合切拼环, 治紧料)
        producible = {s for (_, cc) in cut_cols for s in cc}
        for i, p in enumerate(pipes):
            val, seq = rcsp_price_welding(p, pi, min_wd, min_cut, max_stock, w_j=w_j,
                                          extra_segs=producible)
            if seq is None:
                continue
            # reduced cost = w_j*(nseg-1) - sum pi*cnt - mu_i < 0  <=>  val - w_j*0... 
            # val 已含 -w_j*(nseg-1); reduced cost<0 <=> val > -mu_i
            if val > (-mu[i]) + 1e-6 or (gap > 1e-6 and seq not in [w[1] for w in weld_cols if w[0] == i]):
                if seq not in [w[1] for w in weld_cols if w[0] == i]:
                    weld_cols.append((i, seq))
                    added += 1
            # 缺口存在: 用切法可产段多样化补拼法列(闭合切拼环, 治紧料 L12/L13)
            if gap > 1e-6:
                for sq in enum_legal_seqs(p, sorted(producible), min_wd, min_cut, max_stock, budget=8000):
                    if sq not in [w[1] for w in weld_cols if w[0] == i]:
                        weld_cols.append((i, sq))
                        added += 1
        # 切法: 消耗新出现的段。用当前所有列涉及的段做背包
        cur_alpha = sorted({s for (_, seq) in weld_cols for s in seq} | set(alphabet))
        need_segs = sorted({s for (_, seq) in weld_cols for s in seq})
        for L in stock_qty:
            val, counts = price_cutting(L, {s: pi.get(s, 0.0) for s in cur_alpha}, kerf, min_cut, cur_alpha)
            if val > L + 1e-6 and counts:
                if not any(cc == counts and cl == L for (cl, cc) in cut_cols):
                    cut_cols.append((L, counts))
                    added += 1
            # 缺口存在: 零浪费混切背包(填满定尺, 余料变成另一管所需段, 治L12/L13)
            if gap > 1e-6:
                fb = 1.0 / max(1, L)  # 填充奖励远小于对偶价尺度, 仅打破平局逼近零余料
                _, fc = price_cutting(L, {s: pi.get(s, 0.0) for s in cur_alpha}, kerf,
                                      min_cut, cur_alpha, fill_bonus=fb, need_segs=need_segs)
                if fc and not any(cc == fc and cl == L for (cl, cc) in cut_cols):
                    cut_cols.append((L, fc))
                    added += 1
        # 缺口存在: 逐段生成"专门产出该段"的切法, 保证拼法所需段能被供应(限量防爆炸)
        if gap > 1e-6:
            need_segs = sorted({s for (_, seq) in weld_cols for s in seq})
            budget = 200  # 每轮最多补这么多切法列, 防止列池爆炸拖垮 final_ilp
            for L in stock_qty:
                for s in need_segs:
                    if s > L or budget <= 0:
                        continue
                    for cc in _cuts_producing(L, s, cur_alpha, max_stock, kerf, min_cut):
                        if not any(c2 == cc and l2 == L for (l2, c2) in cut_cols):
                            cut_cols.append((L, cc))
                            added += 1
                            budget -= 1

        if added == 0:
            if verbose:
                print(f"  it{it}: 无改进列 -> 停止(缺口={gap:.1f})")
            break
        stall = stall + 1 if added <= 0 else 0

    # ── 取整: ILP 最终解 ──
    return final_ilp(group, cut_cols, weld_cols, stock_qty, tl, verbose)


def _cuts_producing(stock_len, target_seg, alphabet, max_stock, kerf=0, min_cut=1):
    """生成'至少产出1个 target_seg'的切法(几种密排变体), 供缺口闭合。
    ① 只切 target_seg 若干 + 余料丢弃; ② target_seg + 其余大段密排;
    ③ target_seg + 余料整块作为'新段'(供另一管型消费, 治紧料零浪费混切)。
    """
    out = []
    n = stock_len // target_seg
    if n >= 1:
        out.append({target_seg: n})  # 尽量多切该段
    # target_seg 1个 + 剩余空间用其它段密排(降序贪心)
    remain = stock_len - target_seg
    cc = {target_seg: 1}
    for s in sorted((a for a in alphabet if 0 < a <= remain and a != target_seg), reverse=True):
        while s <= remain:
            cc[s] = cc.get(s, 0) + 1
            remain -= s
    if len(cc) >= 1:
        out.append(dict(cc))
    # ③ 零浪费混切: target_seg + 整段余料(kerf后), 余料成新段供别的管拼接
    rem2 = stock_len - target_seg - kerf
    if rem2 >= min_cut and rem2 != target_seg:
        out.append({target_seg: 1, rem2: 1})
    return out


def final_ilp(group, cut_cols, weld_cols, stock_qty, tl, verbose):
    from pyscipopt import Model, quicksum
    pipes = group.pipes
    m = Model("final")
    m.hideOutput()
    # 最终取整给足时间(用户: 质量优先, 速度次要); 每阶段至少 60s 或 3×tl
    ilp_tl = max(60.0, tl * 3)
    m.setParam("limits/time", ilp_tl)
    segs = set()
    for (_, cc) in cut_cols:
        segs.update(cc)
    for (_, seq) in weld_cols:
        segs.update(seq)
    segs = sorted(segs)
    x = {c: m.addVar(vtype="I", lb=0) for c in range(len(cut_cols))}
    y = {p: m.addVar(vtype="I", lb=0) for p in range(len(weld_cols))}
    for s in segs:
        prod = quicksum(cut_cols[c][1].get(s, 0) * x[c] for c in range(len(cut_cols)))
        consume = quicksum(weld_cols[p][1].count(s) * y[p] for p in range(len(weld_cols)))
        m.addCons(prod - consume >= 0)
    for i, p in enumerate(pipes):
        m.addCons(quicksum(y[pp] for pp in range(len(weld_cols)) if weld_cols[pp][0] == i)
                  == p.demand)
    for L in stock_qty:
        m.addCons(quicksum(x[c] for c in range(len(cut_cols)) if cut_cols[c][0] == L)
                  <= stock_qty[L])
    # 优先级(用户拍板): 利用率是准入门槛(不能为了少焊口而浪费整根母料),
    # 门槛内焊口最少。两阶段词典序:
    #   Pass1: min 用料(得到本列池能达到的最省用料 U*)
    #   Pass2: 固定 用料<=U**(1+eps) 作为门槛, 再 min 焊口
    # 这样 target_rate 达不到时也不会退化成74%的浪费解(见L7)。
    usedlen = quicksum(cut_cols[c][0] * x[c] for c in range(len(cut_cols)))
    joints = quicksum((len(weld_cols[p][1]) - 1) * y[p] for p in range(len(weld_cols)))
    m.setObjective(usedlen, "minimize")
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  final_ilp: Pass1 无解 status={m.getStatus()}")
        return None
    u_star = m.getObjVal()
    # 门槛: 严格保持最省用料(利用率最优), 在此前提下 min 焊口。
    # 不容许为省焊口而多耗母料(利用率是准入门槛, 不能浪费)。
    m.freeTransform()
    m.setParam("limits/time", ilp_tl)
    m.addCons(usedlen <= u_star + 1e-6)
    m.setObjective(joints, "minimize")
    m.optimize()
    if verbose:
        print(f"  final_ilp: status={m.getStatus()} nsols={m.getNSols()} "
              f"vars={len(x)+len(y)} segcons={len(segs)}")
    if m.getNSols() == 0:
        return None
    used_cut = [(c, round(m.getVal(x[c]))) for c in range(len(cut_cols)) if m.getVal(x[c]) > 0.5]
    used_weld = [(p, round(m.getVal(y[p]))) for p in range(len(weld_cols)) if m.getVal(y[p]) > 0.5]
    used_len = sum(cut_cols[c][0] * n for c, n in used_cut)
    total_joints = sum((len(weld_cols[p][1]) - 1) * n for p, n in used_weld)
    cut_types = len({(cut_cols[c][0], tuple(sorted(cut_cols[c][1].items()))) for c, _ in used_cut})
    weld_types = len({weld_cols[p][1] for p, _ in used_weld if len(weld_cols[p][1]) >= 2})
    seg_types = len({s for c, _ in used_cut for s in cut_cols[c][1]})
    return {
        "joints": total_joints, "cut_types": cut_types, "weld_types": weld_types,
        "seg_types": seg_types, "used_len": used_len,
        "util": group.demand_length / used_len if used_len else 0,
    }


def main():
    lv = int(sys.argv[1])

    def argf(name, d, cast):
        return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d

    max_iter = argf("--max_iter", 60, int)
    tl = argf("--tl", 30, float)

    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    s = next(x for x in samples if x["level"] == lv)
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    print(f"L{lv} {s['spec']}  老软件: {s['legacy']}  target_rate={g.target_rate:.4f}")
    res = solve_colgen(g, max_iter=max_iter, tl=tl)
    if res is None:
        print("  列生成无解")
        return
    lg = s["legacy"]
    print("  ══ 列生成 vs 老软件 ══")
    print(f"    利用率:   {res['util']:.4f} vs {lg.get('util'):.4f}")
    print(f"    总焊口:   {res['joints']} vs {lg.get('joints')}")
    print(f"    拼法种类: {res['weld_types']} vs {lg.get('weld_types')}")
    print(f"    切法种类: {res['cut_types']} vs {lg.get('cut_types')}")
    print(f"    段种类:   {res['seg_types']}")


if __name__ == "__main__":
    main()
