"""从 CSV 100 样本中按难度分 20 级, 每级选 1 个, 并关联回 software_records 的完整输入。
难度综合分 = 归一化(老软件焊口数, 需求总长, 切种类, 拼种类) 的加权和。
输出选中 20 个样本的 record 索引 + 关联键, 存到 _picked20.json 供后续逐个跑。
纯 Python。
"""
import csv
import json
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
base = Path("backend/data/samples/software-io/batch-DGMOM")
csv_path = base / "stress_s100_t20.csv"
rec_path = base / "software_records.json"

rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
print(f"CSV 样本 = {len(rows)}")


def fnum(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


# 难度特征: 老软件焊口数 / 需求总长 / 切种类 / 拼种类
feats = []
for r in rows:
    joints = fnum(r.get("sw_welding_joints"))
    dem = fnum(r.get("sw_demand_length"))
    ct = fnum(r.get("sw_cut_pattern_types"))
    wt = fnum(r.get("sw_weld_pattern_types"))
    feats.append({
        "problem_no": r["problem_no"],
        "spec": r["spec"],
        "sw_joints": joints,
        "sw_demand": dem,
        "sw_cut": ct,
        "sw_weld": wt,
        "sw_util": fnum(r.get("sw_util_rate")),
    })

# 归一化(对数缩放需求长, 线性其余), 综合难度分
def lg(x):
    return math.log10(x + 1)

mx = {
    "j": max(f["sw_joints"] for f in feats) or 1,
    "d": max(lg(f["sw_demand"]) for f in feats) or 1,
    "c": max(f["sw_cut"] for f in feats) or 1,
    "w": max(f["sw_weld"] for f in feats) or 1,
}
for f in feats:
    f["difficulty"] = (
        0.45 * f["sw_joints"] / mx["j"]
        + 0.25 * lg(f["sw_demand"]) / mx["d"]
        + 0.15 * f["sw_cut"] / mx["c"]
        + 0.15 * f["sw_weld"] / mx["w"]
    )

feats.sort(key=lambda z: z["difficulty"])

# 20 级: 把排序后的 100 个切成 20 段, 每段取中位那个
picked = []
n = len(feats)
levels = 20
for lv in range(levels):
    lo = lv * n // levels
    hi = (lv + 1) * n // levels
    seg = feats[lo:hi]
    if not seg:
        continue
    mid = seg[len(seg) // 2]
    mid["level"] = lv + 1
    picked.append(mid)

print(f"\n分 {levels} 级, 选出 {len(picked)} 个:")
print(f"{'级':>3} {'难度':>6} {'焊口':>6} {'需求长':>10} {'切':>4} {'拼':>4} {'利用率':>8}  spec / problem_no")
for f in picked:
    print(f"{f['level']:>3} {f['difficulty']:>6.3f} {int(f['sw_joints']):>6} "
          f"{int(f['sw_demand']):>10} {int(f['sw_cut']):>4} {int(f['sw_weld']):>4} "
          f"{f['sw_util']:>8.4f}  {f['spec']} / {f['problem_no']}")

# 关联回 records: 用 (spec, problem_no) 匹配
d = json.loads(rec_path.read_text(encoding="utf-8"))
recs = d["RECORDS"]

def spec_of(rec):
    mp = json.loads(rec["MOMPROBLEMJSON"])
    return f"{mp.get('material','')}/{mp.get('specifications','')}".replace("φ", "").replace("×", "x").replace("Φ", "")

# 建索引: (normalized spec, problem_no) -> record idx
def norm(s):
    return s.replace("φ", "").replace("Φ", "").replace("×", "x").replace(" ", "").lower()

idx = {}
for i, rec in enumerate(recs):
    key = (norm(spec_of(rec)), rec.get("MOMPROBLEMNO", ""))
    idx.setdefault(key, i)

out = []
miss = 0
for f in picked:
    key = (norm(f["spec"]), f["problem_no"])
    ri = idx.get(key)
    if ri is None:
        # 退化: 只按 spec 匹配
        for i, rec in enumerate(recs):
            if norm(spec_of(rec)) == norm(f["spec"]) and rec.get("MOMPROBLEMNO") == f["problem_no"]:
                ri = i
                break
    if ri is None:
        miss += 1
        print(f"  !! 未匹配: {f['spec']} / {f['problem_no']}")
        continue
    out.append({**f, "record_idx": ri, "record_id": recs[ri]["ID"]})

print(f"\n匹配成功 {len(out)}, 未匹配 {miss}")
Path("scripts/_picked20.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("已存 scripts/_picked20.json")
