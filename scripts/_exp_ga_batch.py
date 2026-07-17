"""批量泛化验证：新版禁焊区感知 GA 在一批代表性样本上 vs 老软件。

选样策略：含禁焊区优先、管型数分档、老软件有解且 0.9<util<1.0。
用法: python scripts/_exp_ga_batch.py <samples.json> [--n 12] [--pop 20]
        [--gen 15] [--tl 10] [--seed 1] [--maxpipes 8]
"""
from __future__ import annotations

import functools
import json
import sys
import time
from pathlib import Path

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import random  # noqa: E402

from app.domain import parse_problem  # noqa: E402
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics  # noqa: E402
from _exp_ga import ga_run  # noqa: E402


def argf(name, default, cast=float):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    path = sys.argv[1]
    n = argf("--n", 12, int)
    pop = argf("--pop", 20, int)
    gen = argf("--gen", 15, int)
    tl = argf("--tl", 10.0, float)
    seed = argf("--seed", 1, int)
    maxpipes = argf("--maxpipes", 8, int)

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")

    # 选样：按难度分层，轻量为主 + 少量中等，避开极端重组（定尺种类过多会
    # 让内层 enum_cuts 爆列、单组耗时数分钟）。难度≈禁焊数+定尺种类+管型数。
    cands = []
    for s in recs:
        sid = str(s.get("id") or s.get("ID") or "")
        if not sid:
            continue
        try:
            prob = json.loads(s["MOMPROBLEMJSON"]) if "MOMPROBLEMJSON" in s else s.get("problem")
            g0 = parse_problem(prob).groups[0]
            g = merge_equivalent_pipes(g0)
        except Exception:
            continue
        npipes = len(g.pipes)
        if npipes < 1 or npipes > maxpipes:
            continue
        nfb = sum(len(p.forbidden) for p in g.pipes)
        nstock = len({st.length for st in g.stocks})
        try:
            _o, lm = legacy_alpha_and_metrics(s)
        except Exception:
            continue
        if not (0.90 < lm["util"] < 1.0) or lm["cut_types"] <= 0:
            continue
        # 极端重组（定尺>8种 且 禁焊>25）单组太慢，批量里剔除
        if nstock > 8 and nfb > 25:
            continue
        diff = nfb + nstock * 2 + npipes * 3
        cands.append((sid, s, g, npipes, nfb, lm, nstock, diff))

    # 分三档：易(diff小) 中 难，各取一部分，保证覆盖含禁焊区
    cands.sort(key=lambda c: c[7])
    fb_cands = [c for c in cands if c[4] > 0]
    nofb_cands = [c for c in cands if c[4] == 0]
    picked = []
    # 70% 含禁焊区（分布在难度谱上），30% 无禁焊区对照
    n_fb = int(n * 0.7)
    if fb_cands:
        step = max(1, len(fb_cands) // max(1, n_fb))
        picked.extend(fb_cands[::step][:n_fb])
    for c in nofb_cands[:n - len(picked)]:
        picked.append(c)
    if len(picked) < n:
        for c in cands:
            if c not in picked:
                picked.append(c)
            if len(picked) >= n:
                break
    picked = picked[:n]

    print(f"批量泛化：{len(picked)} 组（含禁焊区优先，管型≤{maxpipes}）")
    print(f"参数 pop={pop} gen={gen} tl={tl} seed={seed}\n")
    header = f"{'id':13} {'管型':>3} {'禁焊':>4} | {'老u':>7} {'老切':>4} {'老拼':>4} | {'新u':>7} {'新切':>4} {'新拼':>4} | {'评':>4} {'s':>5}"
    print(header)
    print("-" * len(header))

    win = tie = lose = fail = 0
    rows = []
    for sid, s, g, npipes, nfb, lm, nstock, diff in picked:
        rng = random.Random(seed)
        t0 = time.monotonic()
        try:
            res, segs = ga_run(g, lm, pop, gen, tl, rng, verbose=False)
        except Exception as e:
            res = None
            print(f"{sid[:13]:13} 异常 {e}")
        dt = time.monotonic() - t0
        if res is None:
            fail += 1
            print(f"{sid[:13]:13} {npipes:>3} {nfb:>4} | {lm['util']:.5f} {lm['cut_types']:>4} {lm['weld_types']:>4} | {'FAIL':>7}")
            continue
        # 评级：利用率不劣 且 (切≤ 且 拼≤) 为 WIN；利用率劣为 LOSE
        util_ok = res["util"] >= lm["util"] - 1e-6
        cut_ok = res["cut_types"] <= lm["cut_types"]
        weld_ok = res["weld_types"] <= lm["weld_types"]
        if not util_ok:
            grade = "LOSE"
            lose += 1
        elif cut_ok and weld_ok and (res["cut_types"] < lm["cut_types"] or res["weld_types"] < lm["weld_types"]):
            grade = "WIN"
            win += 1
        elif cut_ok and weld_ok:
            grade = "TIE"
            tie += 1
        else:
            grade = "MIX"
            lose += 1
        print(f"{sid[:13]:13} {npipes:>3} {nfb:>4} | {lm['util']:.5f} {lm['cut_types']:>4} {lm['weld_types']:>4} | "
              f"{res['util']:.5f} {res['cut_types']:>4} {res['weld_types']:>4} | {grade:>4} {dt:>5.0f}")
        rows.append((sid, lm, res, grade))

    print("-" * len(header))
    print(f"WIN={win} TIE={tie} MIX/LOSE={lose} FAIL={fail}  (WIN=利用率不劣且切拼更少; TIE=不劣且不多; MIX=有一项变多)")


if __name__ == "__main__":
    main()
