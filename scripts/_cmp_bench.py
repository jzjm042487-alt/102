import json
import sys
from pathlib import Path

rows = []
for f in sys.argv[1:]:
    for line in Path(f).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))

ok = 0
bad = []
for r in rows:
    tag = f"{r['spec']}/{r['problem_no']}"
    st = r.get("ours_status", "")
    if not r.get("ours_verified") or st in ("UNSOLVED", "") or not r.get("ours_util_rate"):
        bad.append((tag, "UNSOLVED", st))
        continue
    su, ou = r.get("sw_util_rate") or 0, r.get("ours_util_rate") or 0
    sj = r.get("sw_weld_joints_recomputed") or r.get("sw_welding_joints") or 0
    oj = r.get("ours_welding_joints") or 0
    swt, owt = r.get("sw_weld_pattern_types") or 0, r.get("ours_weld_pattern_types") or 0
    sct, oct = r.get("sw_cut_pattern_types") or 0, r.get("ours_cut_pattern_types") or 0
    issues = []
    if ou < su - 1e-4:
        issues.append(f"util {ou:.4f}<{su:.4f}")
    if oj > sj:
        issues.append(f"joints {oj}>{sj}")
    if owt > swt:
        issues.append(f"weldT {owt}>{swt}")
    if oct > sct:
        issues.append(f"cutT {oct}>{sct}")
    if issues:
        bad.append((tag, "; ".join(issues), st))
    else:
        ok += 1

print(f"=== {len(rows)} groups: 全面不劣于软件 {ok}, 劣势 {len(bad)} ===")
for t, why, st in bad:
    print(f"  [{st}] {t}: {why}")
