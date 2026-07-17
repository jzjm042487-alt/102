"""全量批跑 driver：遍历全部样本，每个样本用子进程 worker 求解（进程级硬超时 + 崩溃隔离）。
- 断点续跑：已在 results.jsonl 里的 id 自动跳过。
- 日志：run.log（人读），results.jsonl（每行一个样本结果，机读，用于和原始输入对比）。
用法：
  python scripts/_batch_all.py [--pop 20 --gen 15 --tl 30 --seed 3 --timeout 120 --limit N --only-legacy]
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(r'd:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json')
OUTDIR = ROOT / 'data' / 'batch_run'


def log(msg, fh):
    line = f'[{datetime.now():%H:%M:%S}] {msg}'
    print(line, flush=True)
    fh.write(line + '\n'); fh.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pop', type=int, default=20)
    ap.add_argument('--gen', type=int, default=15)
    ap.add_argument('--tl', type=float, default=30)
    ap.add_argument('--seed', type=int, default=3)
    ap.add_argument('--timeout', type=float, default=180, help='每样本进程硬超时(秒)')
    ap.add_argument('--limit', type=int, default=0, help='只跑前N个(0=全部)')
    ap.add_argument('--only-legacy', action='store_true', help='仅跑有老软件结果的样本')
    args = ap.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    results_path = OUTDIR / 'results.jsonl'
    log_path = OUTDIR / 'run.log'

    data = json.loads(SRC.read_text(encoding='utf-8'))
    recs = data if isinstance(data, list) else data.get('RECORDS') or data.get('samples')

    # 断点续跑：读已完成 id
    done = set()
    if results_path.exists():
        for ln in results_path.read_text(encoding='utf-8').splitlines():
            try:
                done.add(json.loads(ln)['id'])
            except Exception:
                pass

    fh_log = log_path.open('a', encoding='utf-8')
    fh_res = results_path.open('a', encoding='utf-8')
    log(f'=== 批跑启动 total={len(recs)} 已完成={len(done)} pop={args.pop} gen={args.gen} tl={args.tl} timeout={args.timeout}s ===', fh_log)

    stats = {}
    n_run = 0
    t_start = time.time()
    for idx, rec in enumerate(recs):
        sid = str(rec.get('id') or rec.get('ID') or '')
        if not sid or sid in done:
            continue
        if args.only_legacy and not rec.get('MOMRESULTJSON'):
            continue
        if args.limit and n_run >= args.limit:
            break
        n_run += 1
        payload = json.dumps({'rec': rec, 'pop': args.pop, 'gen': args.gen,
                              'tl': args.tl, 'seed': args.seed}, ensure_ascii=False)
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'scripts' / '_batch_worker.py')],
                input=payload.encode('utf-8'),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(ROOT), timeout=args.timeout,
            )
            out = None
            for ln in proc.stdout.decode('utf-8', 'replace').splitlines():
                if ln.startswith('__RESULT__'):
                    out = json.loads(ln[len('__RESULT__'):])
                    break
            if out is None:
                err = proc.stderr.decode('utf-8', 'replace')[-800:]
                out = {'id': sid, 'verdict': 'ERROR', 'error': 'no_result_line', 'stderr': err}
        except subprocess.TimeoutExpired:
            out = {'id': sid, 'verdict': 'TIMEOUT', 'timeout_s': args.timeout}

        out['idx'] = idx
        out.setdefault('elapsed', round(time.time()-t0, 1))
        fh_res.write(json.dumps(out, ensure_ascii=False) + '\n'); fh_res.flush()
        v = out.get('verdict', '?')
        stats[v] = stats.get(v, 0) + 1

        extra = ''
        if 'ours' in out and 'legacy' in out:
            lg, ou = out['legacy'], out['ours']
            extra = (f' util {ou["util"]:.4f}/{lg["util"]:.4f}'
                     f' cut {ou["cut_types"]}/{lg["cut_types"]}'
                     f' weld {ou["weld_types"]}/{lg["weld_types"]}')
        log(f'#{idx} {sid[:8]} {v}{extra}  ({out["elapsed"]}s)  [{n_run} done, {dict(sorted(stats.items()))}]', fh_log)

    dur = time.time() - t_start
    log(f'=== 完成本轮 {n_run} 个, 耗时 {dur/60:.1f} 分钟, 汇总 {dict(sorted(stats.items()))} ===', fh_log)
    fh_log.close(); fh_res.close()


if __name__ == '__main__':
    main()
