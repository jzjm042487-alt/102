"""泛化测试: 在 L1-L20(或指定档)上跑 V4, 与老软件全指标对比。
输出焊口/拼法/切法/段/利用率的 V4 vs 老软件, 及词典序胜负判定。
用法: python scripts/_gen_test.py [--tl 60] [--levels 1,2,3]
"""
import json, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow
from scripts._check_v3 import check


def argf(name, d, cast):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d


def lex_winner(v, l):
    """词典序 焊口->拼法->切法->段->(利用率). 返回 'V4优' / '老软件优' / '持平'。
    利用率作为末级仅当前四项全等时比较(容差1e-4)。"""
    for key in ("joints", "weld_types", "cut_types", "seg_types"):
        vv, lv = v.get(key), l.get(key)
        if vv is None or lv is None:
            continue
        if vv < lv:
            return f"V4优({key})"
        if vv > lv:
            return f"老软件优({key})"
    du = (v.get("util") or 0) - (l.get("util") or 0)
    if du > 1e-4:
        return "V4优(util)"
    if du < -1e-4:
        return "老软件优(util)"
    return "持平"


def main():
    tl = argf("--tl", 60.0, float)
    only = argf("--levels", None, str)
    levels = [int(x) for x in only.split(",")] if only else list(range(1, 21))
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    by_lv = {s["level"]: s for s in samples}

    hdr = f"{'档':<5}{'规格':<22}{'指标':<6}{'焊口':>7}{'拼法':>6}{'切法':>6}{'段':>6}{'利用率':>9}"
    rows = []
    for lv in levels:
        s = by_lv.get(lv)
        if s is None:
            continue
        try:
            g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
        except Exception as e:  # noqa: BLE001
            print("-" * 78, flush=True)
            print(f"L{lv:<4}{str(s.get('spec','')):<22}  解析失败跳过: {e}", flush=True)
            continue
        t0 = time.time()
        lm = s.get("legacy") or None
        # 利用率硬底线(用户拍板)= max(0.95, legacy_util-1e-3); 无 legacy 时退回 target_rate。
        uf = None
        if lm and lm.get("util"):
            uf = lm["util"] - 1e-3
        try:
            res = solve_arcflow(g, tl=tl, verbose=False, util_floor=uf)
        except Exception as e:  # noqa: BLE001
            res = None
            print(f"L{lv} 异常: {e}", flush=True)
        dt = time.time() - t0
        v = None
        if res:
            errs = check(g, res)
            v = {"joints": res["joints"], "weld_types": res["weld_types"],
                 "cut_types": res["cut_types"], "seg_types": res["seg_types"],
                 "util": res["util"], "check": "OK" if not errs else "BAD",
                 "solver": res.get("solver")}
        rows.append((lv, s["spec"], v, lm, dt))
        # 打印
        print("-" * 78, flush=True)
        print(f"L{lv:<4}{str(s['spec']):<22}  ({dt:.0f}s solver={v['solver'] if v else '无解'} "
              f"自检={v['check'] if v else '—'})", flush=True)
        if v:
            print(f"  V4    焊口{v['joints']:>6} 拼法{v['weld_types']:>4} 切法{v['cut_types']:>4} "
                  f"段{v['seg_types']:>4} 利用率{v['util']*100:>7.2f}%", flush=True)
        if lm:
            lj = lm.get('joints'); lj = lj if lj is not None else '—'
            print(f"  老软件 焊口{str(lj):>6} 拼法{lm['weld_types']:>4} 切法{lm['cut_types']:>4} "
                  f"段{'—':>4} 利用率{lm['util']*100:>7.2f}%", flush=True)
        if v and lm:
            print(f"  => {lex_winner(v, lm)}", flush=True)

    # 汇总
    print("\n" + "=" * 78, flush=True)
    win = defaultdict(int)
    bad = 0
    for lv, spec, v, lm, dt in rows:
        if not v:
            win["无解"] += 1; continue
        if v["check"] == "BAD":
            bad += 1
        if lm:
            w = lex_winner(v, lm)
            key = "V4优" if w.startswith("V4") else ("老软件优" if w.startswith("老软件") else "持平")
            win[key] += 1
    print(f"词典序胜负(焊口>拼法>切法>段>利用率): V4优 {win['V4优']}  持平 {win['持平']}  "
          f"老软件优 {win['老软件优']}  无解 {win['无解']}  自检异常 {bad}  共 {len(rows)} 档", flush=True)


if __name__ == "__main__":
    main()
