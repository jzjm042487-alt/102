"""从原始语料抽"老程序有真实成功输出 + 中等难度 + 单母料组"的案例，跑 V4 做四项对比。

难度(中): 单 group、总需求 20~150 根、管型 2~12、且实际需要焊接(老软件焊口>0 或管>母料)。
对比: 焊口 / 拼法种类 / 切法种类 / 利用率，词典序判定(焊口第一，util 是硬底线)。

用法: python scripts/_corpus_cmp.py [--n 15] [--tl 90] [--seed 0]
"""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow

CORPUS = r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"


def argf(name, d, cast):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d


def legacy_metrics(res):
    """从老软件结果 JSON 提取四项: 焊口/拼法种类/切法种类/利用率。"""
    gi = res.get("GeneralInfo", {})
    result = res.get("Result", {})
    joints = int(float(gi.get("WeldingJointQuantity", 0)))
    util = float(gi.get("UtilRate", 0))
    # 拼法种类 = 不同 Part 序列数(跨所有 WeldingPipe 的 Pattern)
    weld_seqs = set()
    for wp in (result.get("WeldingPattern", {}) or {}).get("WeldingPipe", []) or []:
        for pat in wp.get("Pattern", []) or []:
            parts = tuple(int(float(x)) for x in pat.get("Part", "").split())
            if len(parts) >= 2:  # 只有多段才算拼法(单段=纯切)
                weld_seqs.add(parts)
    # 切法种类 = 不同 (母料长, 切段序列) 数
    cut_seqs = set()
    for cp in (result.get("CuttingPattern", {}) or {}).get("CuttingPipe", []) or []:
        L = int(float(cp.get("Length", 0)))
        parts = tuple(sorted(int(float(x)) for x in cp.get("Part", "").split()))
        cut_seqs.add((L, parts))
    return {"joints": joints, "util": util,
            "weld_types": len(weld_seqs), "cut_types": len(cut_seqs)}


def main():
    n = argf("--n", 15, int)
    tl = argf("--tl", 90.0, float)
    seed = argf("--seed", 0, int)

    data = json.loads(Path(CORPUS).read_text(encoding="utf-8"))
    records = data["RECORDS"]
    cands = []
    for idx, rec in enumerate(records):
        rj = rec.get("MOMRESULTJSON")
        if not rj or not isinstance(rj, str):
            continue
        try:
            res = json.loads(rj)
        except (TypeError, ValueError):
            continue
        gi = res.get("GeneralInfo", {})
        if not str(gi.get("Result", "")).startswith("Success"):
            continue
        try:
            prob = parse_problem(json.loads(rec["MOMPROBLEMJSON"]))
        except Exception:  # noqa: BLE001
            continue
        if len(prob.groups) != 1:
            continue
        g = prob.groups[0]
        n_types = len(g.pipes)
        total = sum(p.demand for p in g.pipes)
        if not (20 <= total <= 150 and 2 <= n_types <= 12):
            continue
        lg = legacy_metrics(res)
        # 中等难度且需要焊接的案例(排除纯切平凡档)
        max_stock = max(s.length for s in g.stocks)
        needs_weld = lg["joints"] > 0 or any(p.length > max_stock for p in g.pipes)
        if not needs_weld:
            continue
        cands.append((idx, rec.get("ID"), rec.get("MOMMATERIAL"), g, lg, n_types, total))

    print(f"符合(中难+需焊+单组+老软件成功)的候选: {len(cands)}")
    rng = random.Random(seed)
    rng.shuffle(cands)
    picked = cands[:n]

    out = []
    print(f"\n{'#idx':>7} {'材料':<18} {'型/根':>7} "
          f"{'焊口 我/老':>12} {'拼 我/老':>9} {'切 我/老':>9} {'util 我/老':>16} {'判定':>6} {'耗时':>6}")
    print("-" * 110)
    for (idx, rid, mat, group, lg, n_types, total) in picked:
        g = merge_equivalent_pipes(group)
        uf = lg["util"] if lg["util"] else None
        t0 = time.time()
        try:
            res = solve_arcflow(g, tl=tl, verbose=False, util_floor=uf)
        except Exception as e:  # noqa: BLE001
            res = None
            err = str(e)[:80]
        dt = time.time() - t0
        if res is None:
            print(f"{idx:>7} {str(mat)[:18]:<18} {n_types}/{total:<5} 无解/异常 ({dt:.0f}s)")
            out.append({"idx": idx, "id": rid, "material": mat, "ours": None, "legacy": lg})
            continue
        jv, wv, cv, uv = res["joints"], res["weld_types"], res["cut_types"], res["util"]
        # 词典序判定
        if uv < lg["util"] - 5e-3:
            verdict = "利用率低"
        elif jv > lg["joints"]:
            verdict = "焊口劣"
        elif jv < lg["joints"]:
            verdict = "优于"
        elif wv > lg["weld_types"]:
            verdict = "拼法劣"
        elif wv < lg["weld_types"]:
            verdict = "优于"
        elif cv > lg["cut_types"]:
            verdict = "切法劣"
        elif cv < lg["cut_types"]:
            verdict = "优于"
        else:
            verdict = "打平"
        print(f"{idx:>7} {str(mat)[:18]:<18} {n_types}/{total:<5} "
              f"{jv}/{lg['joints']:<8} {wv}/{lg['weld_types']:<6} {cv}/{lg['cut_types']:<6} "
              f"{uv:.4f}/{lg['util']:.4f} {verdict:>6} {dt:>5.0f}s")
        out.append({"idx": idx, "id": rid, "material": mat,
                    "ours": {"joints": jv, "weld_types": wv, "cut_types": cv, "util": uv,
                             "solver": res.get("solver")},
                    "legacy": lg, "verdict": verdict, "elapsed": round(dt, 1)})
        Path("scripts/_corpus_cmp_out.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 汇总
    from collections import Counter
    c = Counter(r.get("verdict") for r in out if r.get("ours"))
    print("-" * 110)
    print("汇总:", dict(c), f"| 无解: {sum(1 for r in out if not r.get('ours'))}")
    print("已存 scripts/_corpus_cmp_out.json")


if __name__ == "__main__":
    main()
