"""单样本 worker: 从 _picked20_full.json 按 level 取, 跑新 GA, 打印 RESULT JSON 行。
只读小文件, 秒级加载。被 _run20.py 子进程调用。
用法: python scripts/_run20_worker.py <level> <pop> <gen> <tl> <seed>
"""
import json
import random
import sys
import time
from pathlib import Path

sys.setrecursionlimit(50000)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from scripts._exp_ga import ga_run
from scripts._exp_colgen import merge_equivalent_pipes

FULL = Path(__file__).resolve().parents[1] / "scripts" / "_picked20_full.json"


def main():
    level = int(sys.argv[1])
    pop = int(sys.argv[2]); gen = int(sys.argv[3])
    tl = float(sys.argv[4]); seed = int(sys.argv[5])

    samples = json.loads(FULL.read_text(encoding="utf-8"))
    s = next(x for x in samples if x["level"] == level)
    group = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])

    rng = random.Random(seed)
    t0 = time.monotonic()
    res, segs = ga_run(group, pop, gen, tl, rng, verbose=False)
    dt = time.monotonic() - t0

    out = {
        "level": level,
        "spec": s["spec"],
        "elapsed": round(dt, 1),
        "target_rate": round(group.target_rate, 4),
        "legacy": s["legacy"],
    }
    if res is None:
        out["ours"] = None
    else:
        out["ours"] = {
            "joints": res["joints"], "cut_types": res["cut_types"],
            "weld_types": res["weld_types"], "seg_types": res["seg_types"],
            "util": round(res["util"], 4),
        }
    print("RESULT " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
