"""S2/S3 内部一致性自检: 直接在 solve_arcflow 返回的 patterns 上核对硬约束,
镜像 verifier 的核心不变式(段供需平衡/需求满足/焊口计数/利用率)。
用法: python scripts/_check_v3.py <level> [--tl 40]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow


def check(group, res):
    errs = []
    pipes = group.pipes
    is_pure = res.get("solver") == "pure_cut" or not res["weld_patterns"]
    if is_pure:
        # 纯切: 无焊层, 每根需求管 = 一个整管段被切出。校验 cut 产出覆盖需求。
        need = defaultdict(int)
        for p in pipes:
            need[p.length] += p.demand
        got = defaultdict(int)
        used_len = 0
        for (L, segs), cnt in res["cut_patterns"].items():
            assert sum(segs) <= L
            used_len += L * cnt
            for s in segs:
                got[s] += cnt
        for ln, q in need.items():
            if got[ln] < q:
                errs.append(f"纯切: 管长 {ln} 产出 {got[ln]} < 需求 {q}")
        if used_len != res["used_len"]:
            errs.append(f"用料 {used_len} != res {res['used_len']}")
        return errs
    # 1) 需求满足 + 焊口计数(按拼法)
    dem_got = defaultdict(int)
    joints = 0
    weld_consume = defaultdict(int)
    for (i, seq), cnt in res["weld_patterns"].items():
        dem_got[i] += cnt
        joints += (len(seq) - 1) * cnt
        assert sum(seq) == pipes[i].length, f"拼法 {seq} 段和 != 管长 {pipes[i].length}"
        for s in seq:
            weld_consume[s] += cnt
    for i, p in enumerate(pipes):
        if dem_got[i] != p.demand:
            errs.append(f"管型{i} 需求 {dem_got[i]} != {p.demand}")
    if joints != res["joints"]:
        errs.append(f"焊口计数 {joints} != res {res['joints']}")
    # 2) 切法段产出
    cut_produce = defaultdict(int)
    used_len = 0
    for (L, segs), cnt in res["cut_patterns"].items():
        assert sum(segs) <= L, f"切法 {segs} 段和 > 定尺 {L}"
        used_len += L * cnt
        for s in segs:
            cut_produce[s] += cnt
    if used_len != res["used_len"]:
        errs.append(f"用料 {used_len} != res {res['used_len']}")
    # 3) 段供需精确平衡(verifier SEGMENT_BALANCE)
    all_segs = set(weld_consume) | set(cut_produce)
    for s in all_segs:
        if cut_produce[s] != weld_consume[s]:
            errs.append(f"段 {s} 产出 {cut_produce[s]} != 消耗 {weld_consume[s]}")
    # 4) 利用率
    util = group.demand_length / used_len if used_len else 0
    if abs(util - res["util"]) > 1e-6:
        errs.append(f"利用率 {util:.6f} != res {res['util']:.6f}")
    return errs


def main():
    lv = int(sys.argv[1])
    tl = float(sys.argv[sys.argv.index("--tl") + 1]) if "--tl" in sys.argv else 40.0
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    s = next(x for x in samples if x["level"] == lv)
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    res = solve_arcflow(g, tl=tl, verbose=False)
    if res is None:
        print(f"L{lv}: 无解")
        return
    errs = check(g, res)
    lg = s["legacy"]
    print(f"L{lv} {s['spec']}")
    print(f"  焊口={res['joints']}(lg {lg.get('joints')}) 拼法={res['weld_types']}"
          f"(lg {lg.get('weld_types')}) 切法={res['cut_types']}(lg {lg.get('cut_types')}) "
          f"段种类={res['seg_types']} util={res['util']:.4f}(lg {lg.get('util')})")
    if errs:
        print("  [不一致]")
        for e in errs:
            print(f"    - {e}")
    else:
        print("  [自检通过] 段供需平衡/需求/焊口/用料 全部自洽")


if __name__ == "__main__":
    main()
