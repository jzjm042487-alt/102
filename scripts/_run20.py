"""主控: 逐级(20)子进程跑 _run20_worker, 汇总 我们 vs 老软件, 判定"不劣化"。
词典序: 焊口 -> 拼种类 -> 切种类 -> (利用率>=老软件-1e-3)。
子进程隔离 + 硬超时; 逐行 flush 并写日志文件, 无缓冲。
用法: python scripts/_run20.py [--pop 30 --gen 25 --tl 20 --seed 0 --proc_timeout 240]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
samples = json.loads((ROOT / "scripts" / "_picked20_full.json").read_text(encoding="utf-8"))
samples.sort(key=lambda z: z["level"])

LOG = (ROOT / "scripts" / "_run20_console.txt").open("w", encoding="utf-8")


def emit(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.write(line + "\n"); LOG.flush()


def argf(name, default, cast):
    a = sys.argv
    return cast(a[a.index(name) + 1]) if name in a else default


pop = argf("--pop", 30, int)
gen = argf("--gen", 25, int)
tl = argf("--tl", 20.0, float)
seed = argf("--seed", 0, int)
proc_to = argf("--proc_timeout", 240, int)

emit(f"配置: pop={pop} gen={gen} tl={tl}s seed={seed} 子进程超时={proc_to}s")
emit(f"{'级':>3} {'焊口(我/老)':>14} {'拼(我/老)':>12} {'切(我/老)':>12} "
     f"{'利用率(我/老)':>18} {'判定':>10} {'耗时':>7}  spec")
emit("-" * 118)

results = []
for s in samples:
    lv = s["level"]
    t_start = time.monotonic()
    try:
        cp = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "_run20_worker.py"),
             str(lv), str(pop), str(gen), str(tl), str(seed)],
            capture_output=True, timeout=proc_to, cwd=str(ROOT),
        )
        stdout = cp.stdout.decode("utf-8", errors="replace")
        stderr = cp.stderr.decode("utf-8", errors="replace")
        line = next((l for l in stdout.splitlines() if l.startswith("RESULT ")), None)
        if line is None:
            data = {"ours": None, "legacy": s["legacy"], "elapsed": round(time.monotonic() - t_start, 1),
                    "_err": (stderr or stdout)[-400:]}
        else:
            data = json.loads(line[len("RESULT "):])
    except subprocess.TimeoutExpired:
        data = {"ours": None, "legacy": s["legacy"], "elapsed": proc_to, "_timeout": True}

    lg = data.get("legacy", {})
    ou = data.get("ours")
    data["level"] = lv
    data["spec"] = s["spec"]
    results.append(data)

    def pair(a, b):
        return f"{'-' if a is None else a}/{'-' if b is None else b}"

    if ou is None:
        verdict = "TIMEOUT" if data.get("_timeout") else "FAIL"
        jp = wp = cp_ = up = "-/-"
    else:
        jp = pair(ou["joints"], lg.get("joints"))
        wp = pair(ou["weld_types"], lg.get("weld_types"))
        cp_ = pair(ou["cut_types"], lg.get("cut_types"))
        up = f"{ou['util']:.4f}/{(lg.get('util') or 0):.4f}"
        lj, lw = lg.get("joints"), lg.get("weld_types")
        lc, lu = lg.get("cut_types"), (lg.get("util") or 0)
        verdict = "OK"
        if lj is not None and ou["joints"] > lj:
            verdict = "LOSE_J"
        elif lw is not None and ou["weld_types"] > lw:
            verdict = "LOSE_W"
        elif lc is not None and ou["cut_types"] > lc:
            verdict = "LOSE_C"
        elif ou["util"] < lu - 1e-3:
            verdict = "LOSE_U"
        if verdict == "OK" and (
                (lj is not None and ou["joints"] < lj)
                or (lw is not None and ou["weld_types"] < lw)
                or (lc is not None and ou["cut_types"] < lc)):
            verdict = "WIN"

    data["verdict"] = verdict
    emit(f"{lv:>3} {jp:>14} {wp:>12} {cp_:>12} {up:>18} {verdict:>10} "
         f"{data.get('elapsed', -1):>6}s  {s['spec']}")

(ROOT / "scripts" / "_run20_results.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

emit("-" * 118)
ok = [r for r in results if r.get("verdict") in ("OK", "WIN")]
consec = 0
for r in sorted(results, key=lambda z: z["level"]):
    if r.get("verdict") in ("OK", "WIN"):
        consec = r["level"]
    else:
        break
emit(f"不劣化(OK/WIN): {len(ok)}/20  连续通过到 L{consec}  "
     f"最高通过 L{max((r['level'] for r in ok), default=0)}")
emit("结果已存 scripts/_run20_results.json")
LOG.close()
