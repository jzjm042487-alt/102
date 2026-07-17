"""无时间限制难样本 runner：对指定 id 列表串行跑 GA（大预算，无外层硬超时），
每个出结果立即落盘到 data/hard_run/。用于让代码充分搜索出具体结果。
用法：
  python scripts/_run_hard.py --ids 1e71bfd5           # 跑单个(前缀匹配)
  python scripts/_run_hard.py --from-file scripts/_hard10.json   # 跑清单
  可选：--pop 40 --gen 40 --tl 60 --seed 3
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, 'backend')
sys.path.insert(0, 'scripts')
from _batch_worker import run as worker_run

SRC = Path(r'd:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json')
OUTDIR = Path('data/hard_run')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ids', nargs='*', default=[])
    ap.add_argument('--from-file', default='')
    ap.add_argument('--pop', type=int, default=40)
    ap.add_argument('--gen', type=int, default=40)
    ap.add_argument('--tl', type=float, default=60)
    ap.add_argument('--seed', type=int, default=3)
    args = ap.parse_args()

    ids = list(args.ids)
    if args.from_file:
        ids += json.loads(Path(args.from_file).read_text(encoding='utf-8'))
    if not ids:
        print('需要 --ids 或 --from-file'); return

    OUTDIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTDIR / 'hard_run.log'
    res_path = OUTDIR / 'hard_results.jsonl'
    fh = log_path.open('a', encoding='utf-8')

    def log(m):
        line = f'[{datetime.now():%H:%M:%S}] {m}'
        print(line, flush=True); fh.write(line+'\n'); fh.flush()

    data = json.loads(SRC.read_text(encoding='utf-8'))
    recs = data if isinstance(data, list) else data.get('RECORDS') or data.get('samples')
    by_id = {}
    for r in recs:
        sid = str(r.get('id') or r.get('ID') or '')
        by_id[sid] = r

    log(f'=== 无限时难样本跑 pop={args.pop} gen={args.gen} tl={args.tl} 共{len(ids)}个 ===')
    fh_res = res_path.open('a', encoding='utf-8')
    for i, wanted in enumerate(ids):
        rec = None
        for sid, r in by_id.items():
            if sid.startswith(wanted):
                rec = r; break
        if rec is None:
            log(f'#{i} {wanted} 未找到样本'); continue
        sid = str(rec.get('id') or rec.get('ID') or '')
        log(f'#{i} {sid[:8]} 开始求解...')
        t0 = time.time()
        try:
            out = worker_run(rec, args.pop, args.gen, args.tl, args.seed, verbose=True)
        except Exception as e:
            import traceback
            out = {'id': sid, 'verdict': 'ERROR', 'error': f'{type(e).__name__}: {e}',
                   'trace': traceback.format_exc()[-1500:]}
        out['elapsed'] = round(time.time()-t0, 1)
        fh_res.write(json.dumps(out, ensure_ascii=False)+'\n'); fh_res.flush()
        v = out.get('verdict', '?')
        extra = ''
        if 'ours' in out and 'legacy' in out:
            lg, ou = out['legacy'], out['ours']
            extra = (f' util {ou["util"]:.4f}/{lg["util"]:.4f}'
                     f' cut {ou["cut_types"]}/{lg["cut_types"]}'
                     f' weld {ou["weld_types"]}/{lg["weld_types"]}')
        log(f'#{i} {sid[:8]} {v}{extra}  ({out["elapsed"]}s / {out["elapsed"]/60:.1f}min)')
    fh_res.close()
    log('=== 完成 ===')
    fh.close()


if __name__ == '__main__':
    main()
