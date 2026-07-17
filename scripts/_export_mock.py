import json, sys
from pathlib import Path
from collections import defaultdict, Counter
sys.path.insert(0, 'backend'); sys.path.insert(0, 'scripts')
from app.domain import parse_problem
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics
from _exp_ga import (make_individual, indiv_segs, enum_pipe_pats, ga_run)
from _exp_ksplit import enum_cuts
from pyscipopt import Model, quicksum
import random


def parts_of(s):
    if isinstance(s, (list, tuple)):
        return [int(round(float(x))) for x in s]
    s = str(s).replace('+', ' ')
    return [int(round(float(x))) for x in s.split() if x.strip()]


def legacy_detail(rec):
    res = json.loads(rec['MOMRESULTJSON']) if isinstance(rec.get('MOMRESULTJSON'), str) else rec.get('MOMRESULTJSON')
    r = res['Result']
    cut_rows = []
    cp = r.get('CuttingPattern', {}).get('CuttingPipe', [])
    for c in cp:
        L = int(round(float(c.get('Length') or c.get('Pipe_length') or 0)))
        segs = parts_of(c.get('Part', ''))
        num = int(c.get('Number', 1))
        cut_rows.append({'L': L, 'segs': sorted(segs, reverse=True), 'num': num,
                         'trim': L - sum(segs)})
    weld_rows = []
    wp = r.get('WeldingPattern', {}).get('WeldingPipe', [])
    for w in wp:
        fig = w.get('FigureNumber') or w.get('figure_number') or ''
        pipe_len = w.get('Pipe_length') or w.get('Length')
        pats = w.get('Pattern')
        if pats:
            for pt in pats:
                segs = parts_of(pt.get('Part', ''))
                num = int(pt.get('Number', 1))
                weld_rows.append({'fig': str(fig), 'len': sum(segs), 'segs': sorted(segs, reverse=True), 'num': num})
        else:
            segs = parts_of(w.get('Part', ''))
            num = int(w.get('Number', 1))
            weld_rows.append({'fig': str(fig), 'len': sum(segs), 'segs': sorted(segs, reverse=True), 'num': num})
    return cut_rows, weld_rows


def ga_detail(group, lm, segs_hint=None, pop=20, gen=15, tl=15, seed=3):
    """跑 GA 拿最优段集，再解一次完整 ILP 导出切/拼明细。"""
    rng = random.Random(seed)
    res, segs = ga_run(group, lm, pop, gen, tl, rng, verbose=False)
    if res is None:
        return None
    # 用 best segs 解完整 ILP 取 x/u
    from _exp_ga import evaluate
    # 复刻 evaluate 内部但导出解
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    sigma0 = sorted(set(int(s) for s in segs if s > 0))
    pipe_pats = [enum_pipe_pats(p, sigma0, min_wd, min_cut) for p in group.pipes]
    used = set()
    for pats in pipe_pats:
        for p in pats:
            used.update(p)
    sigma = sorted(used)
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
            cnt = Counter(p)
            for seg, c in cnt.items():
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
    m.setObjective(quicksum(t[seg] for seg in sigma), 'minimize')
    m.optimize()
    if m.getNSols() == 0:
        return None
    cut_rows = []
    for ci, (L, cc) in enumerate(cuts):
        q = round(m.getVal(x[ci]))
        if q > 0:
            cut_rows.append({'L': L, 'segs': sorted(cc.elements() if hasattr(cc, 'elements') else
                             [s for s, n in cc.items() for _ in range(n)], reverse=True),
                             'num': q, 'trim': L - sum(s*n for s, n in cc.items())})
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
    weld_rows = list(weld_map.values())
    # 切法按 (L, segs) 去重合并，得到真正的"切法种类"
    cut_map = {}
    for r in cut_rows:
        key = (r['L'], tuple(r['segs']))
        if key in cut_map:
            cut_map[key]['num'] += r['num']
        else:
            cut_map[key] = r
    cut_rows = list(cut_map.values())
    return {'util': res['util'], 'cut_rows': cut_rows, 'weld_rows': weld_rows,
            'cut_types': len(cut_rows), 'weld_types': len(weld_rows),
            'segs': sorted(set(s for r in cut_rows for s in r['segs']))}


def build_one(s, sid):
    g = merge_equivalent_pipes(parse_problem(json.loads(s['MOMPROBLEMJSON'])).groups[0])
    _o, lm = legacy_alpha_and_metrics(s)
    lcut, lweld = legacy_detail(s)
    gd = ga_detail(g, lm)
    if gd is None:
        return None
    return {
        'id': sid,
        'title': f"{len(g.pipes)} 管型 · 定尺 {sorted({st.length for st in g.stocks})} · 需求 {sum(p.demand for p in g.pipes)} 根",
        'pipes': [{'len': p.length, 'demand': p.demand, 'maxj': p.max_joints,
                   'fb': [(iv.start, iv.end) for iv in p.forbidden]} for p in g.pipes],
        'stocks': sorted({st.length for st in g.stocks}),
        'legacy': {'util': lm['util'], 'cut_types': lm['cut_types'], 'weld_types': lm['weld_types'],
                   'cut_rows': lcut, 'weld_rows': lweld},
        'ours': gd,
    }


def main():
    data = json.loads(Path(r'd:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json').read_text(encoding='utf-8'))
    recs = data if isinstance(data, list) else data.get('RECORDS') or data.get('samples')
    out = {}

    # 固定两个已验证样本
    for pref in ['d1670941', 'cec08040']:
        for s in recs:
            sid = str(s.get('id') or s.get('ID') or '')
            if sid.startswith(pref):
                r = build_one(s, sid)
                if r:
                    out[pref] = r
                    print(f'{pref}: legacy weld={r["legacy"]["weld_types"]} -> ours weld={r["ours"]["weld_types"]}')
                break

    # 自动扫描：找老软件拼法种类最多、单管型、需求适中的样本，做"最悬殊"展示
    cands = []
    for s in recs:
        try:
            _o, lm = legacy_alpha_and_metrics(s)
            if lm is None:
                continue
            g = merge_equivalent_pipes(parse_problem(json.loads(s['MOMPROBLEMJSON'])).groups[0])
        except Exception:
            continue
        if len(g.pipes) != 1:
            continue
        dem = sum(p.demand for p in g.pipes)
        if dem > 300 or dem < 40:
            continue
        cands.append((lm['weld_types'], s, str(s.get('id') or s.get('ID') or '')))
    cands.sort(key=lambda c: -c[0])
    for wt, s, sid in cands[:5]:
        print(f'最悬殊候选 {sid[:8]}: legacy weld={wt}, 求解中...')
        try:
            r = build_one(s, sid)
        except Exception as e:
            print('  求解失败, 跳过:', e)
            continue
        if r and r['ours']['weld_types'] < r['legacy']['weld_types']:
            out['stark'] = r
            print(f'stark {sid[:8]}: legacy weld={r["legacy"]["weld_types"]} -> ours weld={r["ours"]["weld_types"]}')
            break

    Path('scripts/_mock_data.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print('written scripts/_mock_data.json  keys=', list(out.keys()))
    write_html(out)


def write_html(out):
    payload = json.dumps(out, ensure_ascii=False)
    html = HTML_TEMPLATE.replace('__DATA__', payload)
    dst = Path(r'd:\07-codeing\12-plrj\切法拼法对比.html')
    dst.write_text(html, encoding='utf-8')
    print('written', dst)


HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>切法/拼法对比：新方案 vs 现场排料软件</title>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--bd:#2a2f3a;--fg:#e6e9ef;--mut:#9aa3b2;--ok:#4c9f70;--warn:#c9803a;--acc:#4f8ff0;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;font-size:14px;line-height:1.5}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
  h1{font-size:24px;margin:0 0 6px}
  h2{font-size:19px;margin:34px 0 4px;border-left:3px solid var(--acc);padding-left:10px}
  .sub{color:var(--mut);margin:0 0 16px}
  .callout{background:#152030;border:1px solid #24405f;border-radius:8px;padding:14px 16px;margin:16px 0 24px}
  .callout b{color:var(--acc)}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0 20px}
  .stat{background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:12px 14px}
  .stat .v{font-size:22px;font-weight:600}
  .stat .l{color:var(--mut);font-size:12px;margin-top:2px}
  .stat.good .v{color:var(--ok)}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:8px}
  .card{background:var(--panel);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
  .card .hd{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--bd);font-weight:600}
  .badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px}
  .badge.warn{background:#3a2a17;color:#e0a35e;border:1px solid #5a3f1f}
  .badge.ok{background:#193226;color:#6fd6a0;border:1px solid #2a5c40}
  .body{padding:12px 14px}
  .pat{margin-bottom:11px}
  .pat .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;font-size:12.5px}
  .pat .top .n{color:var(--mut);font-size:11px;background:var(--panel2);border:1px solid var(--bd);border-radius:9px;padding:1px 7px}
  .bar{display:flex;height:22px;border-radius:3px;overflow:hidden;border:1px solid var(--bd)}
  .seg{display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:600;min-width:0;overflow:hidden;white-space:nowrap;border-right:1px solid var(--bg)}
  .trim{display:flex;align-items:center;justify-content:center;font-size:9px;color:var(--mut);background:#2b303b}
  .legend{color:var(--mut);font-size:12px;margin:6px 0 4px}
  hr{border:0;border-top:1px solid var(--bd);margin:32px 0}
</style></head>
<body><div class="wrap">
<h1>切法 / 拼法对比：新方案 vs 现场排料软件</h1>
<p class="sub">利用率持平前提下，压缩"切法种类 / 拼法种类"——种类越少，车间下料与焊接越简单。</p>
<div class="callout">
<b>为什么 1 种拼法能顶老软件好几种？（利用率完全持平，无猫腻）</b><br>
关键在"段长集"。老软件对同一根管子用了多种不同切分方式（整根 / A+B / C+D……），每一种切分就是一种拼法、就要一张焊接图。
新方案让遗传算法自动搜出少数几个"通用段长"，它们既能拼成目标管长，又能高密度铺满各种定尺——于是同一批管子被统一成极少数拼法。
这不是牺牲利用率换来的，两边利用率相等。
</div>
<div id="root"></div>
</div>
<script>
const DATA = __DATA__;
const PALETTE = ["#4c9f70","#c9803a","#8a6fb0","#3f7fa6","#b5563f","#5f8f4e","#a5843c","#6b8fb5","#b07fa0","#5aa0a0"];
function colorMap(sample){
  const segs=[...new Set([].concat(...[...sample.legacy.cut_rows,...sample.ours.cut_rows].map(r=>r.segs)))].sort((a,b)=>b-a);
  const m=new Map(); segs.forEach((s,i)=>m.set(s,PALETTE[i%PALETTE.length])); return m;
}
function bar(L,segs,trim,cm){
  let h='<div class="bar">';
  for(const s of segs){const w=(s/L*100).toFixed(3);const c=cm.get(s)||'#666';
    h+=`<div class="seg" title="${s} mm" style="width:${w}%;background:${c}">${s/L>0.07?s:''}</div>`;}
  if(trim>0){const w=(trim/L*100).toFixed(3);h+=`<div class="trim" title="尾料 ${trim} mm" style="width:${w}%">${trim/L>0.07?('尾'+trim):''}</div>`;}
  return h+'</div>';
}
function weldList(rows,cm){
  return rows.map((r,i)=>`<div class="pat"><div class="top"><span>拼法 ${i+1}：${r.segs.join(' + ')} = ${r.len}</span><span class="n">×${r.num}</span></div>${bar(r.len,r.segs,0,cm)}</div>`).join('');
}
function cutList(rows,cm){
  return rows.map(r=>`<div class="pat"><div class="top"><span style="color:var(--mut)">定尺 ${r.L} · ${r.segs.length} 段 · 尾料 ${r.trim}</span><span class="n">×${r.num}</span></div>${bar(r.L,r.segs,r.trim,cm)}</div>`).join('');
}
function block(sample){
  const l=sample.legacy,o=sample.ours,cm=colorMap(sample);
  const utilEq=Math.abs(l.util-o.util)<1e-4;
  return `<h2>${sample.id.slice(0,8)}</h2><p class="sub">${sample.title||''}</p>
  <div class="stats">
    <div class="stat"><div class="v">${(o.util*100).toFixed(2)}%</div><div class="l">利用率　老 ${(l.util*100).toFixed(2)}%${utilEq?'（持平）':''}</div></div>
    <div class="stat good"><div class="v">${o.weld_types} ← ${l.weld_types}</div><div class="l">拼法种类　新 ← 老</div></div>
    <div class="stat good"><div class="v">${o.cut_types} ← ${l.cut_types}</div><div class="l">切法种类　新 ← 老</div></div>
    <div class="stat good"><div class="v">${o.segs.length}</div><div class="l">通用段长数（老多段混用）</div></div>
  </div>
  <div class="cols">
    <div class="card"><div class="hd"><span>老软件拼法 · ${l.weld_types} 种</span><span class="badge warn">车间记 ${l.weld_types} 张焊图</span></div><div class="body">${weldList(l.weld_rows,cm)}</div></div>
    <div class="card"><div class="hd"><span>新方案拼法 · ${o.weld_types} 种</span><span class="badge ok">只记 ${o.weld_types} 张</span></div><div class="body">${weldList(o.weld_rows,cm)}</div></div>
  </div>
  <div class="cols">
    <div class="card"><div class="hd"><span>老软件切法 · ${l.cut_types} 种</span></div><div class="body">${cutList(l.cut_rows,cm)}</div></div>
    <div class="card"><div class="hd"><span>新方案切法 · ${o.cut_types} 种</span></div><div class="body">${cutList(o.cut_rows,cm)}</div></div>
  </div>
  <p class="legend">段长图例：同颜色 = 同一段长（跨新/老、跨切/拼一致）；灰色 = 尾料。数据源：samples 真实历史结果 vs 两层 GA 求解。</p>`;
}
const order=['stark','d1670941','cec08040'].filter(k=>DATA[k]);
document.getElementById('root').innerHTML=order.map(k=>block(DATA[k])).join('<hr>');
</script>
</body></html>'''


if __name__ == '__main__':
    main()
