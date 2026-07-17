"""单样本 worker：从 stdin 读一条样本记录 JSON，跑 GA，把结果打到 stdout（单行 JSON）。
由 batch driver 以子进程方式调用，实现进程级超时与崩溃隔离——单样本卡死/崩溃不影响整批。
"""
import json
import sys
import time
import random
from collections import defaultdict, Counter

sys.path.insert(0, 'backend')
sys.path.insert(0, 'scripts')

from app.domain import parse_problem
from _exp_colgen import merge_equivalent_pipes
from _exp_ga import enum_pipe_pats, indiv_segs, ga_run
from _exp_ksplit import enum_cuts
from pyscipopt import Model, quicksum


def _parts(s):
    if isinstance(s, (list, tuple)):
        return [int(round(float(x))) for x in s]
    s = str(s).replace('+', ' ')
    return [int(round(float(t))) for t in s.split() if t.strip()]


def legacy_metrics(rec):
    """健壮版老软件指标解析：容忍 Pattern 为字符串/缺失、Result 非法、util=0。
    返回 (lm dict | None)。util=0 时从切法反算。"""
    res = rec.get('MOMRESULTJSON') or rec.get('legacy')
    if isinstance(res, str):
        res = res.strip()
        if not res:
            return None
        try:
            res = json.loads(res)
        except Exception:
            return None
    if not isinstance(res, dict):
        return None
    R = res.get('Result') or {}
    gi = res.get('GeneralInfo') or {}
    cp = ((R.get('CuttingPattern') or {}).get('CuttingPipe')) or []
    wp = ((R.get('WeldingPattern') or {}).get('WeldingPipe')) or []
    if not cp:
        return None

    cut_keys = set()
    used_len = 0
    for c in cp:
        if not isinstance(c, dict):
            continue
        L = c.get('Length') or c.get('Pipe_length') or 0
        try:
            L = int(round(float(L)))
        except Exception:
            L = 0
        pr = _parts(c.get('Part', ''))
        num = int(c.get('Number', 1) or 1)
        cut_keys.add((L, tuple(sorted(pr))))
        used_len += L * num

    weld_keys = set()
    total_joints = 0
    for w in wp:
        if not isinstance(w, dict):
            continue
        wnum = int(w.get('Number', 1) or 1)
        pats = w.get('Pattern')
        if isinstance(pats, list):
            for p in pats:
                if isinstance(p, dict):
                    pr = _parts(p.get('Part', ''))
                    pnum = int(p.get('Number', 1) or 1)
                else:
                    pr = _parts(p)
                    pnum = 1
                weld_keys.add(tuple(sorted(pr)))
                total_joints += max(0, len(pr) - 1) * pnum
        else:
            pr = _parts(w.get('Part', ''))
            weld_keys.add(tuple(sorted(pr)))
            total_joints += max(0, len(pr) - 1) * wnum

    try:
        util = float(gi.get('UtilRate', 0) or 0)
    except Exception:
        util = 0.0
    return {'util': util, 'cut_types': len(cut_keys), 'weld_types': len(weld_keys),
            'joints': total_joints, 'used_len': used_len}


def _detail(group, lm, segs, res):
    """用 best segs 解一次完整 ILP，导出去重后的切/拼明细与种类数。"""
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    sigma0 = sorted(set(int(s) for s in segs if s > 0))
    pipe_pats = [enum_pipe_pats(p, sigma0, min_wd, min_cut) for p in group.pipes]
    used = set()
    for pats in pipe_pats:
        for p in pats:
            used.update(p)
    sigma = sorted(used)
    if not sigma:
        return None
    bars = defaultdict(int)
    for st in group.stocks:
        bars[st.length] += st.quantity
    max_stock = max(s.length for s in group.stocks)
    mp = min(200, max_stock // max(1, min(sigma)) + 2)
    cuts = enum_cuts(group, sigma, group.blade_margin, mp, max_trim=max(600, min_cut))
    target = group.demand_length / lm['util']
    m = Model(); m.hideOutput(); m.setParam('limits/time', 30)
    u = {}
    for i, pats in enumerate(pipe_pats):
        for j in range(len(pats)):
            u[(i, j)] = m.addVar(vtype='I', lb=0)
    x = {ci: m.addVar(vtype='I', lb=0) for ci in range(len(cuts))}
    t = {seg: m.addVar(vtype='B') for seg in sigma}
    for i, pipe in enumerate(group.pipes):
        m.addCons(quicksum(u[(i, j)] for j in range(len(pipe_pats[i]))) == pipe.demand)
    prod = defaultdict(list); cons = defaultdict(list)
    for ci, (L, cc) in enumerate(cuts):
        for seg, c in cc.items():
            prod[seg].append((c, x[ci]))
    for i, pats in enumerate(pipe_pats):
        for j, p in enumerate(pats):
            for seg, c in Counter(p).items():
                cons[seg].append((c, u[(i, j)]))
    for seg in sigma:
        m.addCons(quicksum(c*v for c, v in prod.get(seg, [])) - quicksum(c*v for c, v in cons.get(seg, [])) >= 0)
    total_bars = sum(bars.values())
    BIG = total_bars*mp + sum(p.demand for p in group.pipes)*(max(pp.max_joints for pp in group.pipes)+1)
    for seg in sigma:
        m.addCons(quicksum(c*v for c, v in prod.get(seg, [])) <= BIG*t[seg])
    cbl = defaultdict(list)
    for ci, (L, _) in enumerate(cuts):
        cbl[L].append(ci)
    for L, pl in cbl.items():
        m.addCons(quicksum(x[ci] for ci in pl) <= bars[L])
    m.addCons(quicksum(cuts[ci][0]*x[ci] for ci in range(len(cuts))) <= int(target*1.003))
    m.setObjective(quicksum((len(pipe_pats[i][j]) - 1) * u[(i, j)] for (i, j) in u), 'minimize')
    m.optimize()
    if m.getNSols() == 0:
        return None
    cut_map = {}
    for ci, (L, cc) in enumerate(cuts):
        q = round(m.getVal(x[ci]))
        if q > 0:
            segl = sorted([s for s, n in cc.items() for _ in range(n)], reverse=True)
            key = (L, tuple(segl))
            if key in cut_map:
                cut_map[key]['num'] += q
            else:
                cut_map[key] = {'L': L, 'segs': segl, 'num': q, 'trim': L - sum(segl)}
    weld_map = {}
    for (i, j), var in u.items():
        q = round(m.getVal(var))
        if q > 0:
            p = pipe_pats[i][j]
            key = (group.pipes[i].length, tuple(sorted(p, reverse=True)))
            if key in weld_map:
                weld_map[key]['num'] += q
            else:
                weld_map[key] = {'fig': f'pipe{group.pipes[i].length}', 'len': sum(p),
                                 'segs': sorted(p, reverse=True), 'num': q}
    cut_rows = list(cut_map.values())
    weld_rows = list(weld_map.values())
    used_len = sum(r['L']*r['num'] for r in cut_rows)
    total_joints = sum(max(0, len(r['segs']) - 1) * r['num'] for r in weld_rows)
    return {
        'util': group.demand_length / used_len if used_len else 0.0,
        'cut_types': len(cut_rows), 'weld_types': len(weld_rows),
        'joints': total_joints,
        'segs': sorted(set(s for r in cut_rows for s in r['segs'])),
        'cut_rows': cut_rows, 'weld_rows': weld_rows,
    }


def run(rec, pop, gen, tl, seed, verbose=False):
    t0 = time.time()
    sid = str(rec.get('id') or rec.get('ID') or '')
    prob = parse_problem(json.loads(rec['MOMPROBLEMJSON']))
    if len(prob.groups) != 1:
        return {'id': sid, 'verdict': 'SKIP_MULTIGROUP', 'ngroups': len(prob.groups)}
    g = merge_equivalent_pipes(prob.groups[0])
    lm = legacy_metrics(rec)
    if lm is None:
        return {'id': sid, 'verdict': 'SKIP_NO_LEGACY'}
    if lm['util'] <= 0:
        if lm.get('used_len'):
            lm['util'] = g.demand_length / lm['used_len']
        else:
            return {'id': sid, 'verdict': 'SKIP_NO_LEGACY'}
    if lm['util'] <= 0 or lm['util'] > 1.5:
        return {'id': sid, 'verdict': 'SKIP_BAD_UTIL', 'legacy_util': lm['util']}
    rng = random.Random(seed)
    res, segs = ga_run(g, lm, pop, gen, tl, rng, verbose=verbose)
    if res is None or segs is None:
        return {'id': sid, 'verdict': 'FAIL_NO_SOLUTION',
                'legacy': {'util': lm['util'], 'cut_types': lm['cut_types'], 'weld_types': lm['weld_types']},
                'elapsed': round(time.time()-t0, 1),
                'meta': {'npipes': len(g.pipes), 'demand': sum(p.demand for p in g.pipes)}}
    det = _detail(g, lm, segs, res)
    if det is None:
        return {'id': sid, 'verdict': 'FAIL_DETAIL',
                'legacy': {'util': lm['util'], 'cut_types': lm['cut_types'], 'weld_types': lm['weld_types']},
                'ours_metric': {'util': res['util'], 'cut_types': res.get('cut_types'), 'weld_types': res.get('weld_types')},
                'elapsed': round(time.time()-t0, 1)}
    lc, lw = lm['cut_types'], lm['weld_types']
    oc, ow = det['cut_types'], det['weld_types']
    lj, oj = lm.get('joints', 0), det.get('joints', 0)
    util_ok = det['util'] >= lm['util'] - 1e-4
    # 验收口径（车间）：焊口数为首要，其次拼/切种类，利用率作下限(不劣化)。
    if not util_ok:
        verdict = 'LOSE_UTIL'
    elif oj < lj and ow <= lw and oc <= lc:
        verdict = 'WIN'          # 焊口更少且种类不多 → 全面胜
    elif oj == lj and (ow < lw or oc < lc) and ow <= lw and oc <= lc:
        verdict = 'WIN'          # 焊口持平但种类更少
    elif oj == lj and ow == lw and oc == lc:
        verdict = 'TIE'
    elif oj <= lj and ow <= lw and oc <= lc:
        verdict = 'TIE'          # 各项均不劣化
    elif oj > lj:
        verdict = 'LOSE_JOINTS'  # 焊口更多 → 车间不认
    else:
        verdict = 'MIXED'        # 焊口不劣但某类种类更多
    return {
        'id': sid, 'verdict': verdict, 'elapsed': round(time.time()-t0, 1),
        'meta': {'npipes': len(g.pipes), 'demand': sum(p.demand for p in g.pipes),
                 'stocks': sorted({st.length for st in g.stocks}),
                 'nstock_bars': sum(st.quantity for st in g.stocks)},
        'legacy': {'util': lm['util'], 'cut_types': lc, 'weld_types': lw, 'joints': lj},
        'ours': det,
    }


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw)
    rec = payload['rec']
    try:
        out = run(rec, payload['pop'], payload['gen'], payload['tl'], payload['seed'])
    except Exception as e:
        import traceback
        out = {'id': str(rec.get('id') or rec.get('ID') or ''), 'verdict': 'ERROR',
               'error': f'{type(e).__name__}: {e}', 'trace': traceback.format_exc()[-1500:]}
    sys.stdout.write('__RESULT__' + json.dumps(out, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
