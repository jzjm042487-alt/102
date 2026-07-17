"""拆解老软件解，核对其是否满足我方硬约束，并统计基线四元组。

用途：方案评审放行前置①/②。对指定样本：
  - 逐管把 legacy WeldingPattern 的分段还原成焊口位置；
  - 核对每个焊口是否落在该管禁焊区(Unweldable_Area)内、每段是否 >= Min_Welding_Length、焊口数是否 <= Max_Weldingjoint_Number；
  - 核对 CuttingPattern 每段是否 >= Min_Welding_Length（下料段最小长度）；
  - 统计基线四元组：利用率 / 焊口总数 / 切法种类数 / 拼法种类数。

不改动任何生产代码，纯只读分析。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent.parent / "frontend-next" / "public" / "samples.json"

TARGETS = [
    "9f618d9f-5dd5-4a2d-9348-91597d4f0d03",  # A 规模+种类双高
    "6a4fecf7-d070-4629-8bcc-a0ffa5c3b091",  # B 切拼种类最多
    "3040bd13-9108-40bf-9900-c5cdeeec51f0",  # C 规模最大
    "350f2dbb-0858-4398-b00f-b2015be43e58",  # D 管型多+最紧
]


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _parse_parts(part_str: str) -> list[float]:
    return [_f(t) for t in str(part_str).split() if t.strip()]


def _unweld_intervals(pipe: dict) -> list[tuple[float, float]]:
    ua = pipe.get("Unweldable_Area")
    out: list[tuple[float, float]] = []
    if isinstance(ua, list):
        for seg in ua:
            if isinstance(seg, (list, tuple)) and len(seg) >= 2:
                out.append((_f(seg[0]), _f(seg[1])))
    return out


def _in_forbidden(pos: float, intervals: list[tuple[float, float]], tol: float = 1e-6) -> bool:
    for a, b in intervals:
        if a - tol <= pos <= b + tol:
            return True
    return False


def analyze(sample: dict) -> dict:
    pr = sample["problem"]
    lg = sample.get("legacy") or {}
    gi = lg.get("GeneralInfo") or {}
    res = lg.get("Result") or {}

    min_weld = _f(pr.get("Min_Welding_Length"))

    # 按 (figure_number, jlxh) 建管索引，取禁焊区与焊口上限
    pipe_by_fig_jlxh: dict[tuple[str, str], dict] = {}
    pipe_by_fig: dict[str, list[dict]] = {}
    for p in pr.get("Pipe", []):
        fig = str(p.get("figure_number", ""))
        jlxh = str(p.get("jlxh", ""))
        pipe_by_fig_jlxh[(fig, jlxh)] = p
        pipe_by_fig.setdefault(fig, []).append(p)

    wp = res.get("WeldingPattern") or {}
    weld_pipes = wp.get("WeldingPipe", []) if isinstance(wp, dict) else []
    cp = res.get("CuttingPattern") or {}
    cut_pipes = cp.get("CuttingPipe", []) if isinstance(cp, dict) else []

    # ---- 核对拼法(焊位/段长/焊口数) ----
    weld_types = len(weld_pipes)
    total_welds = 0
    violations = {
        "seg_too_short": 0,      # 焊接残段 < min_weld
        "weld_in_forbidden": 0,  # 焊口落禁焊区
        "over_maxjoint": 0,      # 焊口数超上限
        "fig_jlxh_not_found": 0, # 匹配不到管定义
        "length_mismatch": 0,    # 分段总长与管长不符
    }
    checked_welds = 0
    matched = 0

    for w in weld_pipes:
        fig = str(w.get("FigureNumber", ""))
        jlxh = str(w.get("jlxh", ""))
        pipe_len = _f(w.get("Length"))
        in_num = int(_f(w.get("InPipeNumber")))
        patterns = w.get("Pattern", []) or []

        pdef = pipe_by_fig_jlxh.get((fig, jlxh))
        if pdef is None:
            cands = pipe_by_fig.get(fig, [])
            pdef = next((c for c in cands if abs(_f(c.get("pipe_length")) - pipe_len) < 1.0), None)
        if pdef is None:
            violations["fig_jlxh_not_found"] += 1
            forb: list[tuple[float, float]] = []
            max_joint = 10**9
        else:
            forb = _unweld_intervals(pdef)
            max_joint = int(_f(pdef.get("Max_Weldingjoint_Number")) or 10**9)

        for pat in patterns:
            segs = _parse_parts(pat.get("Part", ""))
            cnt = int(_f(pat.get("Number")))
            if not segs:
                continue
            joints = len(segs) - 1  # 焊口数 = 段数 - 1
            total_welds += joints * cnt
            checked_welds += cnt
            matched += cnt if pdef is not None else 0

            # 段长核对（焊接残段受 min_weld）
            if joints >= 1:
                for sl in segs:
                    if sl + 1e-6 < min_weld:
                        violations["seg_too_short"] += cnt
                        break

            # 焊口位置核对
            if joints >= 1 and forb:
                pos = 0.0
                bad = False
                for sl in segs[:-1]:
                    pos += sl
                    if _in_forbidden(pos, forb):
                        bad = True
                        break
                if bad:
                    violations["weld_in_forbidden"] += cnt

            # 焊口数上限
            if joints > max_joint:
                violations["over_maxjoint"] += cnt

            # 总长核对
            if pipe_len > 0 and abs(sum(segs) - pipe_len) > 2.0:
                violations["length_mismatch"] += cnt

    # ---- 核对切法(下料段最小长度) ----
    # 注意：切法里出现的"整根成品短管"(如 150mm 过渡短管/附件)本身是成品件，
    # 不是焊接残段，不受 min_weld 约束。只有当某段既非任何成品管的完整长度、
    # 又短于 min_weld 时，才是可疑的下料残段。
    pipe_lengths = {round(_f(p.get("pipe_length"))) for p in pr.get("Pipe", [])}
    cut_types = len(cut_pipes)
    cut_seg_too_short = 0
    total_cut_bars = 0
    for c in cut_pipes:
        segs = _parse_parts(c.get("Part", ""))
        cnt = int(_f(c.get("Number")))
        total_cut_bars += cnt
        for sl in segs:
            if sl + 1e-6 < min_weld and round(sl) not in pipe_lengths:
                cut_seg_too_short += cnt
                break

    return {
        "id": sample["id"],
        "material": sample.get("material"),
        "spec": sample.get("spec"),
        "min_weld": min_weld,
        "util": _f(gi.get("UtilRate")),
        "joints_reported": int(_f(gi.get("WeldingJointQuantity"))),
        "joints_recomputed": total_welds,
        "cut_types": cut_types,
        "weld_types": weld_types,
        "total_cut_bars": total_cut_bars,
        "checked_welds": checked_welds,
        "matched_pipe_defs": matched,
        "violations": violations,
        "cut_seg_too_short": cut_seg_too_short,
        "pipe_types": len(pr.get("Pipe", [])),
        "stock_bars": sum(int(_f(x.get("stock_demand"))) for x in pr.get("Stock", [])),
    }


def main() -> None:
    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in data["samples"]}
    ids = sys.argv[1:] or TARGETS
    for i, sid in enumerate(ids):
        s = by_id.get(sid)
        if s is None:
            print(f"[{sid}] NOT FOUND")
            continue
        r = analyze(s)
        tag = "ABCD"[i] if i < 4 else str(i)
        print(f"\n===== [{tag}] {r['id']}  {r['material']} {r['spec']} =====")
        print(f"  规模: {r['stock_bars']} 母材 / {r['pipe_types']} 管型 | min_weld={r['min_weld']}")
        print(f"  基线四元组: 利用率={r['util']:.5f} | 焊口(报告)={r['joints_reported']} 焊口(重算)={r['joints_recomputed']} "
              f"| 切法种类={r['cut_types']} | 拼法种类={r['weld_types']}")
        print(f"  下料母材总张数={r['total_cut_bars']} | 核对拼法条目(按张)={r['checked_welds']} 匹配到管定义={r['matched_pipe_defs']}")
        v = r["violations"]
        print(f"  约束核对(违规张数):")
        print(f"    焊接残段 < min_weld : {v['seg_too_short']}")
        print(f"    焊口落禁焊区        : {v['weld_in_forbidden']}")
        print(f"    焊口数超上限        : {v['over_maxjoint']}")
        print(f"    管长与分段总长不符  : {v['length_mismatch']}")
        print(f"    匹配不到管定义      : {v['fig_jlxh_not_found']}")
        print(f"    切法段 < min_weld   : {r['cut_seg_too_short']}")
        ok = (v['seg_too_short']==0 and v['weld_in_forbidden']==0 and v['over_maxjoint']==0)
        print(f"  >> 约束口径{'一致(老软件解满足我方硬约束)' if ok else '不一致(存在违反我方硬约束的解，需澄清!)'}")


if __name__ == "__main__":
    main()
