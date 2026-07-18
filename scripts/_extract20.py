"""预抽取: 把选中的 20 个样本的 problem + 老软件指标 dump 成小文件, 避免每个 worker 重复读 62MB。
纯 Python。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from scripts._exp_colgen import legacy_alpha_and_metrics

ROOT = Path(__file__).resolve().parents[1]
picked = json.loads((ROOT / "scripts" / "_picked20.json").read_text(encoding="utf-8"))
REC = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"

print("加载 62MB records...")
d = json.loads(REC.read_text(encoding="utf-8"))
recs = d["RECORDS"]

out = []
for f in picked:
    rec = recs[f["record_idx"]]
    try:
        _o, lm = legacy_alpha_and_metrics(rec)
        legacy = {
            "joints": lm.get("joints"),
            "cut_types": lm.get("cut_types"),
            "weld_types": lm.get("weld_types"),
            "util": round(lm.get("util", 0), 4),
        }
    except Exception:
        # 老软件解缺失/非法, 用 CSV 里的 sw_* 指标兜底
        legacy = {
            "joints": int(f.get("sw_joints") or 0),
            "cut_types": int(f.get("sw_cut") or 0),
            "weld_types": int(f.get("sw_weld") or 0),
            "util": round(f.get("sw_util") or 0, 4),
        }
    out.append({
        "level": f["level"],
        "spec": f["spec"],
        "problem_no": f["problem_no"],
        "record_idx": f["record_idx"],
        "problem": json.loads(rec["MOMPROBLEMJSON"]),
        "legacy": legacy,
    })
    print(f"  L{f['level']:>2} {f['spec']} 老软件焊口={legacy['joints']} "
          f"切={legacy['cut_types']} 拼={legacy['weld_types']}")

Path("scripts/_picked20_full.json").write_text(
    json.dumps(out, ensure_ascii=False), encoding="utf-8")
print(f"\n已存 scripts/_picked20_full.json ({len(out)} 样本)")
