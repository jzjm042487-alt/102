"""主控: 从语料抽"中难+需焊+单组+老软件成功"案例, 子进程隔离逐档跑 V4(硬超时 kill), 四项对比。
词典序判定: util 硬底线(低于老软件-0.5% 判"利用率低") -> 焊口 -> 拼法 -> 切法。
用法: python scripts/_corpus_run.py [--n 20] [--tl 60] [--proc_timeout 200] [--seed 0]
"""
import json
import random
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem

ROOT = Path(__file__).resolve().parents[1]
CORPUS = r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"


def argf(name, d, cast):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d


def legacy_metrics(res):
    gi = res.get("GeneralInfo", {})
    result = res.get("Result", {})
    joints = int(float(gi.get("WeldingJointQuantity", 0)))
    util = float(gi.get("UtilRate", 0))
    weld_seqs = set()
    for wp in (result.get("WeldingPattern", {}) or {}).get("WeldingPipe", []) or []:
        for pat in wp.get("Pattern", []) or []:
            parts = tuple(int(float(x)) for x in pat.get("Part", "").split())
            if len(parts) >= 2:
                weld_seqs.add(parts)
    cut_seqs = set()
    for cp in (result.get("CuttingPattern", {}) or {}).get("CuttingPipe", []) or []:
        L = int(float(cp.get("Length", 0)))
        parts = tuple(sorted(int(float(x)) for x in cp.get("Part", "").split()))
        cut_seqs.add((L, parts))
    return {"joints": joints, "util": util,
            "weld_types": len(weld_seqs), "cut_types": len(cut_seqs)}


def verdict_of(ours, lg):
    if ours is None:
        return "无解"
    if ours["util"] < lg["util"] - 5e-3:
        return "利用率低"
    if ours["joints"] > lg["joints"]:
        return "焊口劣"
    if ours["joints"] < lg["joints"]:
        return "优于"
    if ours["weld_types"] > lg["weld_types"]:
        return "拼法劣"
    if ours["weld_types"] < lg["weld_types"]:
        return "优于"
    if ours["cut_types"] > lg["cut_types"]:
        return "切法劣"
    if ours["cut_types"] < lg["cut_types"]:
        return "优于"
    return "打平"


def main():
    n = argf("--n", 20, int)
    tl = argf("--tl", 60.0, float)
    proc_to = argf("--proc_timeout", 200, int)
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
        if not str(res.get("GeneralInfo", {}).get("Result", "")).startswith("Success"):
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
        max_stock = max(s.length for s in g.stocks)
        needs_weld = lg["joints"] > 0 or any(p.length > max_stock for p in g.pipes)
        if not needs_weld:
            continue
        cands.append((idx, n_types, total, lg))

    print(f"候选(中难+需焊+单组+老软件成功): {len(cands)}", flush=True)
    rng = random.Random(seed)
    rng.shuffle(cands)
    picked = cands[:n]

    print(f"\n{'#idx':>6} {'型/根':>7} {'焊口 我/老':>12} {'拼 我/老':>9} "
          f"{'切 我/老':>9} {'util 我/老':>16} {'判定':>7} {'耗时':>6}", flush=True)
    print("-" * 92, flush=True)
    out = []
    for (idx, n_types, total, lg) in picked:
        t0 = time.monotonic()
        ours = None
        try:
            cp = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "_corpus_worker.py"), str(idx), str(tl)],
                capture_output=True, timeout=proc_to, cwd=str(ROOT))
            so = cp.stdout.decode("utf-8", errors="replace")
            line = next((l for l in so.splitlines() if l.startswith("RESULT ")), None)
            if line:
                ours = json.loads(line[len("RESULT "):]).get("ours")
        except subprocess.TimeoutExpired:
            ours = None
        dt = time.monotonic() - t0
        v = verdict_of(ours, lg)
        if ours is None:
            print(f"{idx:>6} {n_types}/{total:<5} {'无解/超时':>50} {v:>7} {dt:>5.0f}s", flush=True)
        else:
            print(f"{idx:>6} {n_types}/{total:<5} "
                  f"{ours['joints']}/{lg['joints']:<8} {ours['weld_types']}/{lg['weld_types']:<6} "
                  f"{ours['cut_types']}/{lg['cut_types']:<6} {ours['util']:.4f}/{lg['util']:.4f} "
                  f"{v:>7} {dt:>5.0f}s", flush=True)
        out.append({"idx": idx, "n_types": n_types, "total": total,
                    "ours": ours, "legacy": lg, "verdict": v, "elapsed": round(dt, 1)})
        (ROOT / "scripts" / "_corpus_run_out.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    c = Counter(r["verdict"] for r in out)
    print("-" * 92, flush=True)
    print("汇总:", dict(c), flush=True)
    good = sum(c.get(k, 0) for k in ("优于", "打平"))
    print(f"不劣化(优于+打平): {good}/{len(out)}", flush=True)
    print("已存 scripts/_corpus_run_out.json", flush=True)


if __name__ == "__main__":
    main()
