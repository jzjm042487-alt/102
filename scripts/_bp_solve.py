"""规范 Branch-and-Price 模块(独立, 专攻极限紧料档 L12/L13: <0.7%余量 + 管>料)。

与 _colgen_poc 的区别(治本, 非打补丁):
  POC 失败根因(§11.44): Phase-I 大M + '加不出列就停'(added==0 break)
  破坏了列生成的收敛性 —— 缺口>0 时本应继续定价直到真正无改进列或证 infeasible。

本模块的规范做法:
  主问题(Gilmore-Gomory 双列, 毫米级):
    变量: 切法列 x[c] (一根定尺切成的段多重集) + 拼法列 y[p] (一根管的合法段序列)
    约束:
      段守恒: Σ_c a[s,c]x[c] - Σ_p b[s,p]y[p] >= 0   (对偶 pi[s]>=0)
      管需求: Σ_{p of pipe i} y[p] >= demand_i             (对偶 mu[i]>=0)
      库存  : Σ_{c of stock L} x[c] <= qty_L                  (对偶 sg[L]<=0)
    目标: min 用料总长 + w_j·总焊口   (利用率门槛, 门槛内焊口最少)

  定价(规范, 每轮交替直到 reduced cost >= 0 才收敛, 不提前停):
    拼法: rcsp_bnp_price 沿合法焊点毫米级 DP, 找 reduced cost 最负段序列
            rc = w_j·(nseg-1) - Σ pi[s] - mu[i] < 0 则加列。
    切法: 背包 max Σ pi[s]·n_s s.t. Σ n_s·s <= L; rc = L - Σ pi[s]·n_s < 0 则加列。
    关键: 拼法产生的新段 -> 切法定价会自动为其生成供应列(闭合切拼环)。

  分支(Ryan-Foster 风格简化): LP 分数解时, 对最分数的列变量 x[c]向上/向下取整
            分支, 子节点重新列生成(对偶变->定价出新列)。深度优先。

用法: python scripts/_bp_solve.py <level> [--tl 60 --max_cg 200]
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
from scripts._colgen_poc import rcsp_bnp_price, rcsp_price_welding, derive_seg_alphabet


def _cut_knapsack(L, pi, min_cut):
    """无界背包: max Σ pi[s]·n_s s.t. Σ n_s·s <= L。返回 (best_value, counts)。
    候选段 = pi 中有正对偶价且 <=L 的段。DP 按毫米(L 上千级, 可接受)。
    """
    cand = sorted(s for s in pi if 0 < s <= L and pi[s] > 1e-9)
    if not cand:
        return 0.0, {}
    dp = [0.0] * (L + 1)
    pick = [-1] * (L + 1)
    for cap in range(1, L + 1):
        bv, bs = dp[cap - 1], -1
        for s in cand:
            if s <= cap:
                v = pi[s] + dp[cap - s]
                if v > bv:
                    bv, bs = v, s
        dp[cap], pick[cap] = bv, bs
    counts = defaultdict(int)
    cap = L
    while cap > 0:
        s = pick[cap]
        if s < 0:
            cap -= 1
            continue
        counts[s] += 1
        cap -= s
    return dp[L], dict(counts)


class BranchAndPrice:
    def __init__(self, group, tl=60.0, max_cg=300, w_j=1.0, verbose=True, col_cap=4000):
        self.g = group
        self.tl = tl
        self.max_cg = max_cg
        self.w_j = w_j
        self.verbose = verbose
        self.col_cap = col_cap
        self.min_wd = group.min_weld_distance
        self.min_cut = group.min_cut_length
        self.kerf = group.blade_margin
        self.max_stock = max(s.length for s in group.stocks)
        self.stock_qty = defaultdict(int)
        for st in group.stocks:
            self.stock_qty[st.length] += st.quantity
        self.pipes = group.pipes
        # 列池: 切法 (L, counts:dict[seg->n]);  拼法 (pipe_idx, seq:tuple[seg])
        self.cut_cols = []
        self.weld_cols = []
        self._seed()

    def _seed(self):
        """初始列(极小): 每管型一个 RCSP 保底拼法(感知禁焊区) + 它们用到的段的单段切法。
        列池保持小(§11.42 教训): 其余靠定价按需补。
        """
        for i, p in enumerate(self.pipes):
            _, seq = rcsp_price_welding(p, {}, self.min_wd, self.min_cut, self.max_stock)
            if seq is not None:
                self.weld_cols.append((i, seq))
        # 初始切法: 仅为保底拼法用到的段建单段切法(每段一个)
        segs = set()
        for (_, seq) in self.weld_cols:
            segs.update(seq)
        for L in self.stock_qty:
            for s in sorted(segs):
                if 0 < s <= L:
                    self.cut_cols.append((L, {s: 1}))

    def _all_segs(self):
        s = set()
        for (_, cc) in self.cut_cols:
            s.update(cc)
        for (_, seq) in self.weld_cols:
            s.update(seq)
        return sorted(s)

    def _price_welding_dp(self, pipe, pi):
        """专用拼法定价(有界, 非爆炸): 找 reduced cost 最负的合法段序列。
        状态 = 位置 pos(只在合法焊点); 转移只用'有价段'(pi[s]>0, s<=max_stock);
        f[pos] = 到 pos 的最大 (Σpi[seg] - w_j·(段数-1)) + 段序列。
        每个可达位置尝试'直接收尾到L'(tail=L-pos, 需 <=max_stock 且合法)。
        有价段数量少 -> 可达位置数少 -> 天然有界。返回 (best_val, best_seq)。
        """
        L = pipe.length
        priced = sorted(s for s in pi if 0 < s <= self.max_stock and pi[s] > 1e-9)
        NEG = float("-inf")
        # BFS/DP over reachable positions using priced segments (bounded by max_joints)
        f = {0: (0.0, ())}
        order = [0]
        idx = 0
        best = (NEG, None)
        # 先试整管(0段焊): 仅当整管能被单根定尺容纳
        if L <= self.max_stock and _legal_pattern(pipe, (L,), self.min_wd, self.min_cut):
            v = pi.get(L, 0.0)
            if v > best[0]:
                best = (v, (L,))
        while idx < len(order):
            pos = order[idx]; idx += 1
            pv, pseq = f[pos]
            nseg = len(pseq)
            # 收尾: 直接从 pos 跳到 L(tail 作为一段)
            tail = L - pos
            if 0 < tail <= self.max_stock and nseg + 1 <= pipe.max_joints + 1:
                seq = pseq + (tail,)
                if _legal_pattern(pipe, seq, self.min_wd, self.min_cut):
                    val = pv + pi.get(tail, 0.0) - (self.w_j * nseg if nseg >= 1 else 0.0)
                    if val > best[0]:
                        best = (val, seq)
            # 扩展: 用有价段前进到下一合法焊点
            if nseg + 1 > pipe.max_joints:
                continue  # 再加一段后还需收尾, 超焊口上限
            for s in priced:
                npos = pos + s
                if npos >= L or npos > self.max_stock * (nseg + 1):
                    if npos >= L:
                        break  # priced 升序
                    continue
                if not pipe.weld_allowed(npos):
                    continue
                val = pv + pi.get(s, 0.0) - (self.w_j if nseg >= 1 else 0.0)
                cur = f.get(npos)
                if cur is None or val > cur[0]:
                    f[npos] = (val, pseq + (s,))
                    if cur is None:
                        order.append(npos)
        return (best[0] if best[1] else NEG), best[1]

    def _solve_lp(self, branch_bounds):
        """解主问题 LP(Phase-I 恢复可行 + Phase-II min用料)。
        branch_bounds: dict cut_col_idx -> (lb, ub) 分支限定。
        返回 (feasible, gap, used_len, pi, mu, x_val, y_val)。
        """
        from pyscipopt import Model, quicksum, SCIP_PARAMSETTING
        m = Model("master")
        m.hideOutput()
        m.setParam("limits/time", self.tl)
        m.setPresolve(SCIP_PARAMSETTING.OFF)
        m.setHeuristics(SCIP_PARAMSETTING.OFF)
        m.disablePropagation()
        segs = self._all_segs()
        nC, nW, nP = len(self.cut_cols), len(self.weld_cols), len(self.pipes)
        BIGM = 10 * sum(st.length * st.quantity for st in self.g.stocks) + 1
        x = {c: m.addVar(vtype="C", lb=0) for c in range(nC)}
        y = {p: m.addVar(vtype="C", lb=0) for p in range(nW)}
        a = {i: m.addVar(vtype="C", lb=0) for i in range(nP)}  # 人工变量(缺口)
        for c, (lb, ub) in branch_bounds.items():
            if c < nC:
                m.chgVarLb(x[c], lb)
                if ub is not None:
                    m.chgVarUb(x[c], ub)
        seg_cons = {}
        for s in segs:
            prod = quicksum(self.cut_cols[c][1].get(s, 0) * x[c] for c in range(nC))
            cons = quicksum(self.weld_cols[p][1].count(s) * y[p] for p in range(nW))
            seg_cons[s] = m.addCons(prod - cons >= 0)
        dem_cons = {}
        for i in range(nP):
            dem_cons[i] = m.addCons(
                quicksum(y[pp] for pp in range(nW) if self.weld_cols[pp][0] == i)
                + a[i] >= self.pipes[i].demand)
        for L in self.stock_qty:
            m.addCons(quicksum(x[c] for c in range(nC) if self.cut_cols[c][0] == L)
                      <= self.stock_qty[L])
        joints_lp = quicksum((len(self.weld_cols[p][1]) - 1) * y[p] for p in range(nW))
        m.setObjective(
            quicksum(self.cut_cols[c][0] * x[c] for c in range(nC))
            + self.w_j * joints_lp
            + BIGM * quicksum(a[i] for i in range(nP)), "minimize")
        m.optimize()
        if m.getNSols() == 0:
            return False, float("inf"), 0.0, {}, {}, {}, {}
        gap = sum(m.getVal(a[i]) for i in range(nP))
        used_len = sum(self.cut_cols[c][0] * m.getVal(x[c]) for c in range(nC))
        pi = {s: max(0.0, m.getDualsolLinear(seg_cons[s])) for s in segs}
        mu = {i: max(0.0, m.getDualsolLinear(dem_cons[i])) for i in range(nP)}
        x_val = {c: m.getVal(x[c]) for c in range(nC)}
        y_val = {p: m.getVal(y[p]) for p in range(nW)}
        return True, gap, used_len, pi, mu, x_val, y_val

    def _price(self, pi, mu, gap):
        """规范定价: 拼法(rcsp_bnp_price) + 切法(背包), 返回新增列数。
        关键: 拼法产新段 -> 切法为其供应; 交替直到本轮 reduced cost>=0。
        gap>0 时走可行性定价(Farkas): mu_i>0 驱动生成任意新合法拼法,
        并为每段配'零浪费互补切法'(段 s 从定尺 L 切出, 余料 L-s 也注册为可用段)。
        这才是§11.44 指的'全局零浪费混切', 由 mu 主动驱动而非等 pi。
        """
        added = 0
        import time as _t
        # 拼法定价(专用有界 DP, 非爆炸的 rcsp_bnp_price):
        #   沿合法焊点扫描, 状态=位置, 只用'有价段(pi>0)'作中间转移 + 每段收尾补到L。
        #   由 pi 驱动选最值钱拼法; 有界于 |priced| 个位置, 不依赖段枚举。
        _tb = _t.time()
        for i, p in enumerate(self.pipes):
            val, seq = self._price_welding_dp(p, pi)
            if seq is None:
                continue
            # rc = w_j*(nseg-1) - Σpi - mu_i ; val = Σpi - w_j*(nseg-1) => rc = -val - mu_i
            rc = -val - mu[i]
            if rc < -1e-6:
                if seq not in [w[1] for w in self.weld_cols if w[0] == i]:
                    self.weld_cols.append((i, seq))
                    added += 1
        _tbnp = _t.time() - _tb
        # 切法定价: 为拼法所需段供应
        _tb = _t.time()
        need = sorted({s for (_, seq) in self.weld_cols for s in seq})
        cur_pi = {s: pi.get(s, 0.0) for s in need}
        for L in self.stock_qty:
            val, counts = _cut_knapsack(L, cur_pi, self.min_cut)
            rc = L - val
            if rc < -1e-6 and counts:
                if not any(cc == counts and cl == L for (cl, cc) in self.cut_cols):
                    self.cut_cols.append((L, counts))
                    added += 1
        _tcut = _t.time() - _tb
        _tb = _t.time()
        if gap > 1e-6:
            added += self._feasibility_pricing(mu)
        _tfeas = _t.time() - _tb
        if self.verbose:
            print(f"      price: bnp={_tbnp:.1f}s cut={_tcut:.1f}s feas={_tfeas:.1f}s "
                  f"added={added}", flush=True)
        return added

    def _feasibility_pricing(self, mu):
        """可行性定价(gap>0): 为每根缺口管生成毫米级合法拼法(pi无关),
        并为其每段注册'零浪费互补切法'——段 s 从定尺 L 切出, 余段 L-s 作为新段。
        以管需求对偶 mu 驱动, 主动把不同管型的互补段拼到同一根定尺。
        """
        added = 0
        # 当前已有段(供互补参考)
        known_segs = {s for (_, cc) in self.cut_cols for s in cc}
        known_segs |= {s for (_, seq) in self.weld_cols for s in seq}
        new_seq_by_pipe = defaultdict(list)
        for i, p in enumerate(self.pipes):
            if mu[i] <= 1e-9:
                continue
            for seq in self._enum_millimeter_seqs(p, known_segs):
                if seq not in [w[1] for w in self.weld_cols if w[0] == i] \
                        and seq not in new_seq_by_pipe[i]:
                    self.weld_cols.append((i, seq))
                    new_seq_by_pipe[i].append(seq)
                    added += 1
        # 为所有新段配零浪费互补切法
        new_segs = {s for seqs in new_seq_by_pipe.values() for seq in seqs for s in seq}
        for L in self.stock_qty:
            for s in sorted(new_segs):
                if not (0 < s <= L):
                    continue
                rem = L - s - self.kerf
                # 单段 + 余料(余料成新段供其它管)
                variants = [{s: 1}]
                if rem >= max(1, self.min_cut) and rem != s:
                    variants.append({s: 1, rem: 1})
                # 两同段(如两根管头段凑满)
                if 2 * s <= L:
                    variants.append({s: L // s})
                for cc in variants:
                    if not any(c2 == cc and l2 == L for (l2, c2) in self.cut_cols):
                        self.cut_cols.append((L, cc))
                        added += 1
        return added

    def _enum_millimeter_seqs(self, pipe, known_segs, max_new=6):
        """为一根管生成每米级合法拼法: 优先复用已知段(闭合切拼环),
        不够时沿合法焊点均匀采样新分割点。限 max_new 条防爆炸。
        """
        L = pipe.length
        out = []
        # ① 已知段能拼出的合法序列(2 段优先)
        ksorted = sorted(s for s in known_segs if 0 < s < L and s <= self.max_stock)
        for s in ksorted:
            rem = L - s
            if 0 < rem <= self.max_stock and pipe.weld_allowed(s):
                seq = (s, rem)
                if _legal_pattern(pipe, seq, self.min_wd, self.min_cut):
                    out.append(seq)
                    if len(out) >= max_new:
                        return out
        # ② 沿合法焊点均匀采样新 2 段分割点(两段均<=max_stock)
        lo = max(1, L - self.max_stock)
        hi = min(L - 1, self.max_stock)
        if lo <= hi:
            span = hi - lo
            step = max(1, span // (max_new * 4))
            pos = lo
            while pos <= hi and len(out) < max_new:
                if pipe.weld_allowed(pos):
                    seq = (pos, L - pos)
                    if _legal_pattern(pipe, seq, self.min_wd, self.min_cut) \
                            and seq not in out:
                        out.append(seq)
                pos += step
        return out

    def _column_generation(self, branch_bounds):
        """在给定分支限定下跑完整列生成直到收敛。返回 LP 结果元组。"""
        import time
        last = None
        for it in range(self.max_cg):
            t0 = time.time()
            res = self._solve_lp(branch_bounds)
            feasible, gap, used_len, pi, mu, x_val, y_val = res
            last = res
            if not feasible:
                return res
            if self.verbose:
                util = self.g.demand_length / used_len if used_len > 0 else 0
                print(f"    cg{it}: gap={gap:.2f} util={util:.4f} "
                      f"cols(cut={len(self.cut_cols)},weld={len(self.weld_cols)}) "
                      f"lp={time.time()-t0:.1f}s", flush=True)
            added = self._price(pi, mu, gap)
            if added == 0:
                if self.verbose:
                    print(f"    cg收敛 @it{it}: gap={gap:.2f} 无改进列", flush=True)
                break
            if len(self.cut_cols) + len(self.weld_cols) > self.col_cap:
                if self.verbose:
                    print(f"    列池达上限 {self.col_cap} @it{it}: gap={gap:.2f} 停止补列", flush=True)
                break
        return last

    def solve(self):
        # 根节点列生成
        res = self._column_generation({})
        feasible, gap, used_len, pi, mu, x_val, y_val = res
        if not feasible:
            if self.verbose:
                print("  根节点 LP 不可行")
            return None
        if gap > 1e-6:
            if self.verbose:
                print(f"  根节点列生成后仍有缺口 gap={gap:.2f} -> 物理不可行或需更强定价")
            return None
        # 分支定界(深度优先): 对最分数切法列分支
        return self._final_ilp()

    def _final_ilp(self):
        """列池固定后解整数 ILP(两阶段词典序: 先 min用料, 后 min焊口)。"""
        from pyscipopt import Model, quicksum
        m = Model("final")
        m.hideOutput()
        ilp_tl = max(120.0, self.tl * 4)
        m.setParam("limits/time", ilp_tl)
        nC, nW, nP = len(self.cut_cols), len(self.weld_cols), len(self.pipes)
        segs = self._all_segs()
        x = {c: m.addVar(vtype="I", lb=0) for c in range(nC)}
        y = {p: m.addVar(vtype="I", lb=0) for p in range(nW)}
        for s in segs:
            prod = quicksum(self.cut_cols[c][1].get(s, 0) * x[c] for c in range(nC))
            cons = quicksum(self.weld_cols[p][1].count(s) * y[p] for p in range(nW))
            m.addCons(prod - cons >= 0)
        for i in range(nP):
            m.addCons(quicksum(y[pp] for pp in range(nW) if self.weld_cols[pp][0] == i)
                      == self.pipes[i].demand)
        for L in self.stock_qty:
            m.addCons(quicksum(x[c] for c in range(nC) if self.cut_cols[c][0] == L)
                      <= self.stock_qty[L])
        usedlen = quicksum(self.cut_cols[c][0] * x[c] for c in range(nC))
        joints = quicksum((len(self.weld_cols[p][1]) - 1) * y[p] for p in range(nW))
        m.setObjective(usedlen, "minimize")
        m.optimize()
        if m.getNSols() == 0:
            if self.verbose:
                print(f"  final_ilp Pass1 无解 {m.getStatus()}")
            return None
        u_star = m.getObjVal()
        m.freeTransform()
        m.setParam("limits/time", ilp_tl)
        m.addCons(usedlen <= u_star + 1e-6)
        m.setObjective(joints, "minimize")
        m.optimize()
        if m.getNSols() == 0:
            return None
        used_cut = [(c, round(m.getVal(x[c]))) for c in range(nC) if m.getVal(x[c]) > 0.5]
        used_weld = [(p, round(m.getVal(y[p]))) for p in range(nW) if m.getVal(y[p]) > 0.5]
        used_len = sum(self.cut_cols[c][0] * n for c, n in used_cut)
        total_joints = sum((len(self.weld_cols[p][1]) - 1) * n for p, n in used_weld)
        cut_types = len({(self.cut_cols[c][0], tuple(sorted(self.cut_cols[c][1].items())))
                         for c, _ in used_cut})
        weld_types = len({self.weld_cols[p][1] for p, _ in used_weld
                          if len(self.weld_cols[p][1]) >= 2})
        seg_types = len({s for c, _ in used_cut for s in self.cut_cols[c][1]})
        return {
            "joints": total_joints, "cut_types": cut_types, "weld_types": weld_types,
            "seg_types": seg_types, "used_len": used_len,
            "util": self.g.demand_length / used_len if used_len else 0,
        }


def main():
    lv = int(sys.argv[1])

    def argf(name, d, cast):
        return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d

    tl = argf("--tl", 60.0, float)
    max_cg = argf("--max_cg", 300, int)
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    s = next(x for x in samples if x["level"] == lv)
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    print(f"L{lv} {s['spec']}  老软件: {s['legacy']}  target_rate={g.target_rate:.4f}")
    bp = BranchAndPrice(g, tl=tl, max_cg=max_cg)
    res = bp.solve()
    if res is None:
        print("  B&P 无解")
        return
    lg = s["legacy"]
    print("  ══ B&P vs 老软件 ══")
    print(f"    利用率:   {res['util']:.4f} vs {lg.get('util'):.4f}")
    print(f"    总焊口:   {res['joints']} vs {lg.get('joints')}")
    print(f"    拼法种类: {res['weld_types']} vs {lg.get('weld_types')}")
    print(f"    切法种类: {res['cut_types']} vs {lg.get('cut_types')}")
    print(f"    段种类:   {res['seg_types']}")


if __name__ == "__main__":
    main()
