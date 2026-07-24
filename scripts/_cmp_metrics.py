"""对指定档跑 solve_arcflow, 提取全四项指标(焊口/拼法/切法/利用率)与老软件基准对比。
用法: python scripts/_cmp_metrics.py 3,4,6,12 [--tl 120]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow


def argf(name, d, cast):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d


levels = [int(x) for x in sys.argv[1].split(",")]
tl = argf("--tl", 120.0, float)
samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
by_lv = {s["level"]: s for s in samples}

out_rows = []
print(f"{'档':>4} {'指标':<8} {'V4':>10} {'老软件':>10} {'判定':>8}")
print("-" * 48)
for lv in levels:
    s = by_lv[lv]
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    lg = s["legacy"] or {}
    # 验收口径: util 硬底线 = max(0.95, legacy_util - 1e-3)(用户拍板)。
    # 必须传入 solve_arcflow, 否则退回 group.target_rate(常偏高), 会把老软件的
    # 低利用率纯切解误判为"不达标"而选高 util 焊接解, 违反焊口第一的词典序(如 L4)。
    lg_util = lg.get("util")
    uf = lg_util if lg_util else None
    t0 = time.time()
    res = solve_arcflow(g, tl=tl, verbose=False, util_floor=uf)
    dt = time.time() - t0
    if res is None:
        print(f"L{lv:<3} 无解/异常 ({dt:.1f}s)")
        out_rows.append({"level": lv, "spec": s["spec"], "ours": None, "legacy": s["legacy"]})
        continue
    ours = {"joints": res.get("joints"), "weld_types": res.get("weld_types"),
            "cut_types": res.get("cut_types"), "util": res.get("util"),
            "solver": res.get("solver")}
    out_rows.append({"level": lv, "spec": s["spec"], "ours": ours,
                     "legacy": s["legacy"], "elapsed": round(dt, 1)})
    Path("scripts/_cmp_metrics_out.json").write_text(
        json.dumps(out_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = [
        ("焊口", res.get("joints"), lg.get("joints"), True),
        ("拼法种类", res.get("weld_types"), lg.get("weld_types"), True),
        ("切法种类", res.get("cut_types"), lg.get("cut_types"), True),
        ("利用率", res.get("util"), lg.get("util"), False),
    ]
    print(f"L{lv}  spec={s['spec']}  solver={res.get('solver')}  ({dt:.1f}s)")
    for name, v, l, lower_better in rows:
        if l is None:
            verdict = "无基准"
        elif name == "利用率":
            verdict = "达标" if v >= l - 1e-3 else "低"
        else:
            verdict = "优于" if v < l else ("打平" if v == l else "劣于")
        vs = f"{v:.4f}" if name == "利用率" and v is not None else str(v)
        ls = f"{l:.4f}" if name == "利用率" and l is not None else str(l)
        print(f"     {name:<8} {vs:>10} {ls:>10} {verdict:>8}")
    print()

Path("scripts/_cmp_metrics_out.json").write_text(
    json.dumps(out_rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("已存 scripts/_cmp_metrics_out.json")
