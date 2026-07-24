"""从 D5 legacy-success 抽取几何 motif 字典（无题号特判）。

产物: data/acceptance/d5_motif_dict/motif_dict.json
- remnant_alphabet: 教师焊段中出现的非库存段长（跨案复用）
- stock_anchor_freq: 焊段等于库存规格的频率
- cut_nearfill_templates: 近满切对 (a,b) 在母料 L 上 trim≈kerf
- long_seg_n: 长管段数分布

用法:
  .venv\\Scripts\\python.exe scripts/build_d5_motif_dict.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

OUT = ROOT / "data" / "acceptance" / "d5_motif_dict"


def _parts(s) -> list[int]:
    if isinstance(s, (list, tuple)):
        return [int(round(float(x))) for x in s]
    s = str(s).replace("+", " ")
    return [int(round(float(x))) for x in s.split() if x.strip()]


def _d5_success_idxs() -> list[int]:
    cases_root = ROOT / "data" / "legacy-cases"
    pools = json.loads((cases_root / "difficulty_pools.json").read_text(encoding="utf-8"))
    by = {
        c["idx"]: c
        for c in json.loads((cases_root / "index.json").read_text(encoding="utf-8"))["cases"]
    }
    return [
        int(i)
        for i in pools["pools"]["D5-极难"]
        if by.get(i, {}).get("legacy_status") == "success"
    ]


def main() -> int:
    from scripts.legacy_corpus_lib import load_legacy_case
    from scripts._export_mock import legacy_detail

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    idxs = _d5_success_idxs()
    remnant_ctr: Counter[int] = Counter()
    stock_hit = 0
    weld_seg_n: Counter[int] = Counter()
    weld_templates: Counter[tuple] = Counter()
    cut_nearfill: Counter[tuple] = Counter()
    cut_templates: Counter[tuple] = Counter()
    long_seg_n: Counter[int] = Counter()
    n_ok = 0

    for n, idx in enumerate(idxs, 1):
        case = load_legacy_case(ROOT / "data" / "legacy-cases", idx)
        lr = case.get("legacy_result")
        if not isinstance(lr, dict) or "Result" not in lr:
            continue
        try:
            cut_rows, weld_rows = legacy_detail({"MOMRESULTJSON": lr})
        except Exception:
            continue
        stocks = set()
        for st in (case.get("problem") or {}).get("Material") or []:
            # problem shape varies; also from stocks in metrics
            pass
        # stocks from cut L's and problem
        from backend.app.domain import parse_problem
        from scripts.legacy_corpus_lib import clean_mom_problem

        try:
            g = parse_problem(clean_mom_problem(case["problem"])).groups[0]
            stocks = {int(s.length) for s in g.stocks}
            s_max = max(stocks) if stocks else 0
            kerf = int(g.blade_margin or 0) or 5
        except Exception:
            stocks, s_max, kerf = set(), 0, 5

        n_ok += 1
        for w in weld_rows:
            segs = tuple(_parts(w.get("segs") or w.get("Part") or []))
            if not segs:
                segs = tuple(w["segs"]) if isinstance(w.get("segs"), list) else ()
            if not segs:
                continue
            segs = tuple(int(x) for x in segs)
            weld_seg_n[len(segs)] += int(w.get("num") or w.get("count") or 1)
            plen = int(w.get("len") or sum(segs))
            if len(segs) >= 2 and sum(segs) == plen:
                weld_templates[segs] += int(w.get("num") or w.get("count") or 1)
            if s_max and plen > s_max:
                long_seg_n[len(segs)] += int(w.get("num") or 1)
            for s in segs:
                if s in stocks:
                    stock_hit += 1
                else:
                    remnant_ctr[s] += 1

        for c in cut_rows:
            L = int(c["L"])
            segs = tuple(sorted(int(x) for x in c["segs"]))
            num = int(c.get("num") or c.get("count") or 1)
            cut_templates[segs] += num
            trim = int(c.get("trim") or c.get("waste") or (L - sum(segs)))
            if len(segs) == 2 and abs(trim - kerf) <= 15:
                cut_nearfill[(segs[0], segs[1], L)] += num
            elif len(segs) == 2 and trim <= max(kerf * 3, 50):
                cut_nearfill[(segs[0], segs[1], L)] += num

        if n % 30 == 0:
            print(f"[{n}/{len(idxs)}] mined ok={n_ok}", flush=True)

    # keep top remnants / templates
    remnant_alphabet = [s for s, _ in remnant_ctr.most_common(800)]
    nearfill_list = [
        {"a": a, "b": b, "L": L, "n": n}
        for (a, b, L), n in cut_nearfill.most_common(2000)
    ]
    # templates without stock L — just piece multisets for transfer
    cut_piece_templates = [
        {"segs": list(segs), "n": n} for segs, n in cut_templates.most_common(1500)
    ]
    weld_pattern_templates = [
        {"segs": list(segs), "sum": int(sum(segs)), "n": n}
        for segs, n in weld_templates.most_common(3000)
    ]

    out = {
        "n_cases": n_ok,
        "remnant_alphabet": remnant_alphabet,
        "weld_seg_n": dict(weld_seg_n),
        "long_seg_n": dict(long_seg_n),
        "stock_hit_pieces": stock_hit,
        "cut_nearfill": nearfill_list,
        "cut_piece_templates": cut_piece_templates,
        "weld_pattern_templates": weld_pattern_templates,
        "notes": [
            "geometry-transferable motifs from D5 legacy-success; no case idx keys",
            "apply: weld templates where sum==pipe_len; cuts nearfill + templates ⊆ sigma",
            "long pipes: always keep stock-anchor 2/3/4-seg",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "motif_dict.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"wrote {path} remnants={len(remnant_alphabet)} nearfill={len(nearfill_list)} "
        f"cut_tmpl={len(cut_piece_templates)} weld_tmpl={len(weld_pattern_templates)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
