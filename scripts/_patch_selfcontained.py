"""彻底改造 _exp_ga.py: 去掉'偷看老软件答案'。
1) evaluate: 利用率上限约束改为可选(默认不限制, 仅受库存约束)。
2) ga_run: 移除 lm 依赖, target/baseline_util 删除; 停止条件改为收敛判据(连续无改进)。
纯 Python 编辑 + 语法校验。"""
import ast
from pathlib import Path

ga = Path(__file__).resolve().parents[1] / "scripts" / "_exp_ga.py"
s = ga.read_text(encoding="utf-8")

# ---- 补丁1: evaluate 的利用率上限约束改为可选 ----
old1 = '    m.addCons(quicksum(cuts[ci][0] * x[ci] for ci in range(len(cuts))) <= int(target_len * (1 + slack)))'
new1 = (
    '    # 利用率不是优化目标, 也不是硬约束(用户认知: 利用率是好结果的自然奖励)。\n'
    '    # 仅当显式传入 target_len 时才加用料上限; 默认只受库存约束。\n'
    '    if target_len is not None:\n'
    '        m.addCons(quicksum(cuts[ci][0] * x[ci] for ci in range(len(cuts))) <= int(target_len * (1 + slack)))'
)
assert s.count(old1) == 1, f"补丁1目标出现 {s.count(old1)} 次"
s = s.replace(old1, new1)

# evaluate 签名: target_len 默认 None
old_sig = 'def evaluate(group, segs, target_len, slack, time_limit, exact=True, indiv=None):'
new_sig = 'def evaluate(group, segs, target_len=None, slack=0.0, time_limit=20, exact=True, indiv=None):'
assert s.count(old_sig) == 1
s = s.replace(old_sig, new_sig)

# ---- 补丁2: 完整替换 ga_run(去 lm 依赖, 收敛停止) ----
old_ga_start = 'def ga_run(group, lm, pop_size, gens, tl, rng, verbose=True):'
i = s.find(old_ga_start)
assert i != -1
j = s.find('\n    return best_res, best_segs\n', i)
assert j != -1
j_end = j + len('\n    return best_res, best_segs\n')

new_ga = '''def ga_run(group, pop_size, gens, tl, rng, verbose=True, patience=8):
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
            scored.append((fitness_key(res), indiv, res))
        scored.sort(key=lambda z: z[0], reverse=True)
        improved = False
        if scored[0][2] is not None:
            if best_res is None or fitness_key(scored[0][2]) > fitness_key(best_res):
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
'''
s = s[:i] + new_ga + s[j_end:]

# ---- 补丁3: main() 里 ga_run 调用去掉 lm ----
old_call = '        res, segs = ga_run(group, lm, pop, gen, tl, rng)'
new_call = '        res, segs = ga_run(group, pop, gen, tl, rng)'
assert s.count(old_call) == 1, f"补丁3目标出现 {s.count(old_call)} 次"
s = s.replace(old_call, new_call)

ga.write_text(s, encoding="utf-8")
ast.parse(ga.read_text(encoding="utf-8"))
print("OK: 3个补丁全部应用, 语法校验通过")
