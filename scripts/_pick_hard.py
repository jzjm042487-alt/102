"""扫描全部样本，按真实难度打分，选出最重难点的 N 个（默认10），输出 id 清单。
难度打分综合：管型数、定尺种类、禁焊区总数、需求量、母材根数。
只在有老软件对比基准的样本里选。不求解，纯扫描，很快。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, 'backend')
sys.path.insert(0, 'scripts')
from app.domain import parse_problem
from _exp_colgen import merge_equivalent_pipes
from _batch_worker import legacy_metrics

SRC = Path(r'd:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json')


def main():
    n = int(sys.argv[sys.argv.index('--n')+1]) if '--n' in sys.argv else 10
    data = json.loads(SRC.read_text(encoding='utf-8'))
    recs = data if isinstance(data, list) else data.get('RECORDS') or data.get('samples')
    cands = []
    for rec in recs:
        sid = str(rec.get('id') or rec.get('ID') or '')
        if not rec.get('MOMRESULTJSON'):
            continue
        try:
            lm = legacy_metrics(rec)
            if lm is None:
                continue
            prob = parse_problem(json.loads(rec['MOMPROBLEMJSON']))
            if len(prob.groups) != 1:
                continue
            g = merge_equivalent_pipes(prob.groups[0])
        except Exception:
            continue
        npipes = len(g.pipes)
        nstock = len({st.length for st in g.stocks})
        nbars = sum(st.quantity for st in g.stocks)
        nfb = sum(len(p.forbidden) for p in g.pipes)
        demand = sum(p.demand for p in g.pipes)
        # 难度分：管型/定尺/禁焊区权重高，母材根数/需求量作规模项
        diff = npipes*5 + nstock*3 + nfb*2 + nbars*0.02 + demand*0.05
        cands.append({
            'id': sid, 'diff': round(diff, 1),
            'npipes': npipes, 'nstock': nstock, 'nbars': nbars,
            'nfb': nfb, 'demand': demand,
            'legacy': {'util': round(lm['util'], 5), 'cut': lm['cut_types'], 'weld': lm['weld_types']},
        })
    cands.sort(key=lambda c: -c['diff'])
    top = cands[:n]
    print(f'有对比基准的单组样本共 {len(cands)} 个，选出难度 Top{n}:')
    print(f'{"id":10} {"diff":>7} {"管型":>4} {"定尺":>4} {"母材":>5} {"禁焊":>4} {"需求":>5}  老(util/cut/weld)')
    for c in top:
        lg = c['legacy']
        print(f'{c["id"][:8]:10} {c["diff"]:>7} {c["npipes"]:>4} {c["nstock"]:>4} {c["nbars"]:>5} '
              f'{c["nfb"]:>4} {c["demand"]:>5}  {lg["util"]}/{lg["cut"]}/{lg["weld"]}')
    Path('scripts/_hard10.json').write_text(json.dumps([c['id'] for c in top], ensure_ascii=False), encoding='utf-8')
    Path('scripts/_hard10_detail.json').write_text(json.dumps(top, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\nwritten scripts/_hard10.json')


if __name__ == '__main__':
    main()
