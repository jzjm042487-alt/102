"""独立跑单个裸问题 JSON(无 MOMRESULTJSON), 只跑 GA 自约束求解, 打印焊口/种类/利用率。
用法: python scripts/_try_one.py data/input_a598e024.json [--pop 30 --gen 25 --tl 20 --seed 0]
"""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from scripts._exp_ga import ga_run
from scripts._exp_colgen import merge_equivalent_pipes


def argf(name, default, cast=float):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main():
    path = sys.argv[1]
    pop = argf("--pop", 30, int)
    gen = argf("--gen", 25, int)
    tl = argf("--tl", 20.0, float)
    seed = argf("--seed", 0, int)
    rng = random.Random(seed)

    prob = json.loads(Path(path).read_text(encoding="utf-8"))
    group = merge_equivalent_pipes(parse_problem(prob).groups[0])

    print(f"文件={path}")
    print(f"  管长(需求): {[(p.length, p.demand) for p in group.pipes]}")
    print(f"  定尺(库存): {[(st.length, st.quantity) for st in group.stocks]}")
    print(f"  利用率软下界 target_rate={group.target_rate:.4f}")
    print(f"  需求总长={group.demand_length}  库存总长={group.stock_length}")

    t0 = time.monotonic()
    res, segs = ga_run(group, pop, gen, tl, rng, verbose=True)
    dt = time.monotonic() - t0

    if res is None:
        print(f"  GA 未找到可行解 ({dt:.1f}s)")
        return
    print("  == GA 自约束结果 ==")
    print(f"    总焊口数: {res['joints']}")
    print(f"    拼法种类: {res['weld_types']}")
    print(f"    切法种类: {res['cut_types']}")
    print(f"    段种类:   {res['seg_types']}  -> {res.get('segs')}")
    print(f"    利用率:   {res['util']:.4f}  (下界 {group.target_rate:.4f} "
          f"{'达标' if res['util'] >= group.target_rate - 1e-9 else '未达标(不强制)'})")
    print(f"    耗时 {dt:.1f}s")


if __name__ == "__main__":
    main()
