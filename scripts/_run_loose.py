"""批量跑松料有焊口样本的列生成 POC, 输出 vs 老软件对比表。
松料判据: 库存总长 / 需求总长 - 1 >= slack_thr (默认1%冗余), 且老软件有焊口。
"""
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes

samples = json.loads((ROOT / "scripts" / "_picked20_full.json").read_text(encoding="utf-8"))
samples.sort(key=lambda z: z["level"])

SLACK_THR = float(sys.argv[sys.argv.index("--slack") + 1]) if "--slack" in sys.argv else 0.01
TL = sys.argv[sys.argv.index("--tl") + 1] if "--tl" in sys.argv else "30"

picked = []
for s in samples:
    try:
        g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    except Exception as e:
        s["_slack"] = None
        continue
    slack = g.stock_length / g.demand_length - 1 if g.demand_length else 0
    has_weld = (s["legacy"].get("joints") or 0) > 0
    s["_slack"] = slack
    if has_weld and slack >= SLACK_THR:
        picked.append(s)

tags = ", ".join(f"L{s['level']}" for s in picked)
LOG = (ROOT / "scripts" / "_loose_log.txt").open("w", encoding="utf-8")


def emit(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.write(line + "\n"); LOG.flush()


emit(f"松料有焊口样本(冗余>={SLACK_THR:.0%}): {tags}")
emit(f"{'级':>3} {'冗余':>7} {'利用率(我/老)':>18} {'焊口(我/老)':>14} {'拼(我/老)':>12} {'切(我/老)':>12} {'判定':>8}")
emit("-" * 90)

for s in picked:
    lv = s["level"]
    emit(f"  ... 跑 L{lv} (slack={s['_slack']:.1%}) ...")
    try:
        cp = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "_colgen_poc.py"), str(lv), "--tl", TL, "--max_iter", "40"],
            capture_output=True, timeout=int(TL) * 4 + 60, cwd=str(ROOT))
        out = cp.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        emit(f"{lv:>3} {s['_slack']:>6.1%}  {'子进程超时':>16}")
        continue
    lg = s["legacy"]
    if "列生成无解" in out or "infeasible" in out:
        emit(f"{lv:>3} {s['_slack']:>6.1%}  {'无解/紧料':>16}")
        continue
    import re
    def num(pat):
        m = re.search(pat, out)
        return m.group(1) if m else "?"
    util = num(r"利用率:\s*([\d.]+)")
    joints = num(r"总焊口:\s*(\d+)")
    weld = num(r"拼法种类:\s*(\d+)")
    cut = num(r"切法种类:\s*(\d+)")
    lu, lj, lw, lc = lg.get("util"), lg.get("joints"), lg.get("weld_types"), lg.get("cut_types")
    try:
        verdict = "WIN" if int(joints) < (lj or 1e9) else "OK"
    except Exception:
        verdict = "?"
    emit(f"{lv:>3} {s['_slack']:>6.1%}  {util}/{lu:.4f}      {joints}/{lj}        "
         f"{weld}/{lw}        {cut}/{lc}     {verdict}")
emit("-" * 90)
LOG.close()
