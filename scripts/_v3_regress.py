"""V3 回归验证: 两种模式。

1) 固定档模式(默认): 跑 scripts/_picked20_full.json 的 L1-L20, 对齐老软件焊口/利用率。
   python scripts/_v3_regress.py [--tl 180] [--levels 8,9,11]

2) 分层抽样模式: 从 3177 案例库按几何分类分层抽样, 每类抽 N 个, 对齐老软件。
   python scripts/_v3_regress.py --corpus [--n 30] [--tl 60] [--data <path>]

判定: util 不低于 legacy-0.5% 且 焊口不高于 legacy 视为 OK(焊口是真目标);
      焊口更少记 '优于'; 无解/异常单列。
"""
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow, classify_group

DEFAULT_DATA = "backend/data/samples/software-io/software-records-3177.json"


def argf(name, d, cast):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d


def verdict(j_v, u_v, j_l, u_l):
    if j_v is None:
        return "无解/异常"
    joint_ok = j_v <= (j_l or 0)
    util_ok = u_v >= (u_l or 0) - 0.005
    if joint_ok and util_ok:
        return "优于" if j_v < (j_l or 0) else "OK"
    return "焊口回归" if not joint_ok else "利用率回归"


def _legacy_from_record(rec):
    try:
        res = json.loads(rec["MOMRESULTJSON"])
    except (TypeError, ValueError):
        return None
    gi = res.get("GeneralInfo", {})
    if not str(gi.get("Result", "")).startswith("Success"):
        return None
    try:
        return {"joints": int(float(gi.get("WeldingJointQuantity", 0))),
                "util": float(gi.get("UtilRate", 0))}
    except (TypeError, ValueError):
        return None


def run_fixed(tl, levels):
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    by_lv = {s["level"]: s for s in samples}
    rows = []
    for lv in levels:
        s = by_lv.get(lv)
        if s is None:
            continue
        g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
        lg = s["legacy"]
        t0 = time.time()
        try:
            res = solve_arcflow(g, tl=tl, verbose=False)
        except Exception:  # noqa: BLE001
            res = None
        dt = time.time() - t0
        j_v = res["joints"] if res else None
        u_v = res["util"] if res else None
        tag = verdict(j_v, u_v, lg.get("joints"), lg.get("util"))
        rows.append((f"L{lv}", s["spec"], j_v, u_v, lg.get("joints"), lg.get("util"), dt, tag))
        _print_row(rows[-1])
    _summary(rows)


def run_corpus(tl, n, data_path, seed=0):
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    records = data["RECORDS"] if isinstance(data, dict) and "RECORDS" in data else data
    # 按几何分类分桶(只收有老解且解析成功的)。
    buckets = defaultdict(list)
    for idx, rec in enumerate(records):
        try:
            prob = parse_problem(json.loads(rec["MOMPROBLEMJSON"]))
        except Exception:  # noqa: BLE001
            continue
        lg = _legacy_from_record(rec)
        if lg is None:
            continue
        for gi_idx, group in enumerate(prob.groups):
            g = merge_equivalent_pipes(group)
            cls = classify_group(g, tl=5.0, verbose=False)
            geom = cls["geom"]
            if geom == "infeasible_len":
                continue
            buckets[geom].append((idx, gi_idx, g, lg))
    rng = random.Random(seed)
    rows = []
    for geom in ("all_stock_fit", "some_stock_fit", "all_stock_short"):
        pool = buckets.get(geom, [])
        rng.shuffle(pool)
        picked = pool[:n]
        print(f"\n#### 分类 {geom}: 抽 {len(picked)}/{len(pool)}", flush=True)
        for (ridx, gidx, g, lg) in picked:
            t0 = time.time()
            try:
                res = solve_arcflow(g, tl=tl, verbose=False)
            except Exception:  # noqa: BLE001
                res = None
            dt = time.time() - t0
            j_v = res["joints"] if res else None
            u_v = res["util"] if res else None
            tag = verdict(j_v, u_v, lg["joints"], lg["util"])
            rows.append((f"#{ridx}.{gidx}", geom, j_v, u_v, lg["joints"], lg["util"], dt, tag))
            _print_row(rows[-1])
    _summary(rows)


def _print_row(row):
    name, spec, j_v, u_v, j_l, u_l, dt, tag = row
    jvs = f"{j_v}" if j_v is not None else "—"
    uvs = f"{u_v:.4f}" if u_v is not None else "—"
    print(f"{name:<10} {str(spec):<22} 焊口 {jvs} vs {j_l}  "
          f"util {uvs} vs {(u_l or 0):.4f}  [{tag}] ({dt:.1f}s)", flush=True)


def _summary(rows):
    print("\n" + "=" * 78)
    n_ok = n_better = n_reg = 0
    for *_, tag in rows:
        if tag == "优于":
            n_better += 1
        elif tag == "OK":
            n_ok += 1
        else:
            n_reg += 1
    print(f"优于老软件: {n_better}   打平: {n_ok}   回归/异常: {n_reg}   共 {len(rows)} 例")


def main():
    tl = argf("--tl", 180.0, float)
    if "--corpus" in sys.argv:
        n = argf("--n", 30, int)
        data_path = argf("--data", DEFAULT_DATA, str)
        run_corpus(tl, n, data_path)
    else:
        only = argf("--levels", None, str)
        levels = [int(x) for x in only.split(",")] if only else list(range(1, 21))
        run_fixed(tl, levels)


if __name__ == "__main__":
    main()
