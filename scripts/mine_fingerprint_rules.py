"""Mine fingerprint-derived construction rules from remaining fail vs ok cases.

Case-agnostic: aggregates motifs across teacher columns, no idx special-cases.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
from scripts.teacher_inspired_construct_v0 import construct_columns, _kerf_candidates

FP = ROOT / "data" / "acceptance" / "legacy-fingerprints"
CASES = ROOT / "data" / "legacy-cases"
PROBE = FP / "teacher_inspired_v0_probe.json"


def load_teacher(idx: int) -> dict | None:
    dirs = list((FP / "cases").glob(f"{idx}_*"))
    if not dirs:
        return None
    return {
        "fp": json.loads((dirs[0] / "fingerprint.json").read_text(encoding="utf-8")),
        "tc": json.loads((dirs[0] / "teacher_columns.json").read_text(encoding="utf-8")),
        "dir": dirs[0],
    }


def motif_weld(segs: list[int], stocks: list[int], kerfs: list[int]) -> list[str]:
    """Label a weld pattern with structural motifs (no case id)."""
    tags: list[str] = []
    L = sum(segs)
    s_max = max(stocks) if stocks else 0
    if len(segs) == 1:
        tags.append("1seg")
        return tags
    if len(segs) == 2:
        a, b = segs
        if s_max in segs:
            tags.append("2seg_stock_anchor")
            R = L - s_max
            tags.append("2seg_R_plus_S")
        else:
            tags.append("2seg_no_full_stock")
            # gap = Sp - kerf - L ?
            for Sp in stocks:
                for k in kerfs:
                    if Sp - k - L in (a, b) and L - (Sp - k - L) in (a, b):
                        tags.append("2seg_stock_gap")
                        break
            # packing leftover of L on Sp
            for Sp in stocks:
                for k in kerfs:
                    room = Sp - k
                    if room <= 0:
                        continue
                    kk = room // L if L else 0
                    if kk >= 1:
                        lo = room - kk * L
                        if lo in (a, b) and L - lo in (a, b):
                            tags.append("2seg_pack_L_leftover")
                    # k*kerf mode
                    for n in range(1, 8):
                        lo = Sp - n * k - n * L
                        if lo in (a, b) and L - lo in (a, b):
                            tags.append("2seg_kkerf_L")
        return tags
    if len(segs) >= 3:
        S = max(segs)
        if S in stocks or S == s_max:
            tags.append(f"{len(segs)}seg_stock_anchor")
            rem = [x for x in segs if x != S]
            R = sum(rem)
            if len(rem) == 2:
                a, b = rem
                # leftover split of R
                for Sp in stocks:
                    for k in kerfs:
                        room = Sp - k
                        if room >= R and R > 0:
                            kk = room // R
                            if kk >= 1:
                                lo = room - kk * R
                                if lo in (a, b) and R - lo in (a, b):
                                    tags.append("3seg_leftover_split_R")
                        for n in range(1, 6):
                            lo = Sp - n * k - n * R
                            if lo in (a, b) and R - lo in (a, b):
                                tags.append("3seg_kkerf_split_R")
                half = (S - 5) // 2
                if half in (a, b) or any(abs((S - k) // 2 - x) <= 1 for k in kerfs for x in (a, b)):
                    tags.append("3seg_half_split_R")
                # complement: c = Sp-kerf-seg
                for Sp in stocks:
                    for k in kerfs:
                        if Sp - k - a == b or Sp - k - b == a:
                            tags.append("3seg_stock_complement_pair")
            if len(rem) == 3:
                tags.append("4seg_R_triple")
                # half in rem?
                for k in kerfs:
                    half = (S - k) // 2
                    if half in rem:
                        tags.append("4seg_contains_half")
        else:
            tags.append(f"{len(segs)}seg_no_stock_anchor")
            # short pipe multi-split
            tags.append("short_multi_split")
    return tags or ["unknown"]


def coverage(our_sigma: set[int], teach_sigma: list[int]) -> dict:
    t = set(teach_sigma)
    return {
        "teach_n": len(t),
        "our_n": len(our_sigma),
        "hit": len(t & our_sigma),
        "miss": sorted(t - our_sigma),
        "extra_n": len(our_sigma - t),
        "hit_rate": len(t & our_sigma) / len(t) if t else 0.0,
    }


def main() -> int:
    probe = json.loads(PROBE.read_text(encoding="utf-8"))
    fail_idx = [int(r["idx"]) for r in probe["rows"] if not r.get("ok")]
    ok_idx = [int(r["idx"]) for r in probe["rows"] if r.get("ok")]

    motif_fail: Counter[str] = Counter()
    motif_ok_teacher: Counter[str] = Counter()
    cut_motif_fail: Counter[str] = Counter()
    cov_fail = []
    cov_ok = []
    geom_fail: Counter[str] = Counter()
    miss_seg_role: Counter[str] = Counter()

    def analyze(idxs: list[int], is_fail: bool) -> None:
        for idx in idxs:
            pack = load_teacher(idx)
            if not pack:
                continue
            fp = pack["fp"]
            tc = pack["tc"]
            geom = fp.get("geom_class") or "?"
            if is_fail:
                geom_fail[geom] += 1
            case = load_legacy_case(CASES, idx)
            g = merge_equivalent_pipes(
                parse_problem(clean_mom_problem(case["problem"])).groups[0]
            )
            stocks = sorted({int(s.length) for s in g.stocks})
            kerfs = _kerf_candidates(g)
            cons = construct_columns(g)
            our_sigma = set(cons.get("sigma") or [])
            teach_sigma = tc.get("sigma") or []
            cov = coverage(our_sigma, teach_sigma)
            cov["idx"] = idx
            cov["geom"] = geom
            if is_fail:
                cov_fail.append(cov)
            else:
                cov_ok.append(cov)

            for w in tc.get("weld_columns") or []:
                tags = motif_weld(list(w["segs"]), stocks, kerfs)
                for t in tags:
                    (motif_fail if is_fail else motif_ok_teacher)[t] += int(w.get("number") or 1)

            for c in tc.get("cut_columns") or []:
                segs = list(c["segs"])
                Sp = max(stocks)
                # classify cut
                fill = sum(segs)
                slack = min(abs(Sp - fill), min(abs(Sp - k - fill) for k in kerfs))
                if len(segs) == 1:
                    cut_motif_fail["cut_1seg"] += 1 if is_fail else 0
                elif slack <= 20:
                    cut_motif_fail["cut_near_full"] += 1 if is_fail else 0
                else:
                    cut_motif_fail["cut_partial"] += 1 if is_fail else 0
                # k identical
                if len(set(segs)) == 1 and len(segs) >= 2:
                    cut_motif_fail["cut_k_copies"] += 1 if is_fail else 0
                if len(segs) >= 5:
                    cut_motif_fail["cut_ge5_parts"] += 1 if is_fail else 0

            # role of missed segs
            if is_fail and cov["miss"]:
                teach_welds = [tuple(w["segs"]) for w in tc.get("weld_columns") or []]
                for m in cov["miss"]:
                    roles = []
                    for segs in teach_welds:
                        if m not in segs:
                            continue
                        if max(segs) in stocks and m != max(segs):
                            roles.append("in_stock_anchor_weld")
                        if len(segs) == 2 and max(segs) not in stocks:
                            roles.append("in_short_2seg")
                        if len(segs) >= 3:
                            roles.append(f"in_{len(segs)}seg")
                    if not roles:
                        roles = ["only_in_cuts?"]
                        for c in tc.get("cut_columns") or []:
                            if m in c["segs"]:
                                roles = ["cut_only_seg"]
                                break
                    for r in set(roles):
                        miss_seg_role[r] += 1

    analyze(fail_idx, True)
    analyze(ok_idx[:20], False)  # sample ok for contrast

    # aggregate miss hit rates
    def avg(rows, key):
        return sum(r[key] for r in rows) / len(rows) if rows else 0.0

    summary = {
        "n_fail": len(fail_idx),
        "n_ok_sampled": min(20, len(ok_idx)),
        "fail_geom": dict(geom_fail),
        "fail_teacher_sigma_hit_rate_mean": avg(cov_fail, "hit_rate"),
        "ok_teacher_sigma_hit_rate_mean": avg(cov_ok, "hit_rate"),
        "fail_miss_seg_count_mean": avg(cov_fail, "teach_n") - avg(
            [{"x": r["hit"]} for r in cov_fail], "x"
        )
        if cov_fail
        else 0,
        "fail_our_extra_sigma_mean": avg(cov_fail, "extra_n"),
        "ok_our_extra_sigma_mean": avg(cov_ok, "extra_n"),
        "teacher_weld_motifs_on_fails": motif_fail.most_common(30),
        "teacher_weld_motifs_on_ok_sample": motif_ok_teacher.most_common(20),
        "cut_motifs_on_fails": cut_motif_fail.most_common(20),
        "missed_seg_roles_on_fails": miss_seg_role.most_common(20),
        "fail_coverage_rows": sorted(cov_fail, key=lambda r: r["hit_rate"]),
    }
    # fix miss count mean
    if cov_fail:
        summary["fail_miss_seg_count_mean"] = sum(len(r["miss"]) for r in cov_fail) / len(
            cov_fail
        )

    out = FP / "fingerprint_rule_mine.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "fail_coverage_rows"}, indent=2))
    print("\nworst coverage fails:")
    for r in summary["fail_coverage_rows"][:12]:
        print(
            f"  idx={r['idx']} geom={r['geom']} hit={r['hit']}/{r['teach_n']} "
            f"miss={r['miss'][:8]} extra={r['extra_n']}"
        )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
