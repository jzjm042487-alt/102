"""摸清语料结果结构 + 统计"老软件成功输出"的难度分布，为中等难度抽样做准备。
用法: python scripts/_corpus_scan.py
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem

CORPUS = r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"

data = json.loads(Path(CORPUS).read_text(encoding="utf-8"))
records = data["RECORDS"]
print(f"总记录数: {len(records)}")

# 先看一条成功结果的键结构
sample_keys = None
success = 0
by_diff = Counter()
rows = []
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
    success += 1
    if sample_keys is None:
        sample_keys = list(gi.keys())
        print("GeneralInfo 键:", sample_keys)
        print("结果顶层键:", list(res.keys()))
    # 解析问题规模(管型数、总需求根数)
    try:
        prob = parse_problem(json.loads(rec["MOMPROBLEMJSON"]))
    except Exception:  # noqa: BLE001
        continue
    n_pipe_types = sum(len(g.pipes) for g in prob.groups)
    total_demand = sum(p.demand for g in prob.groups for p in g.pipes)
    n_groups = len(prob.groups)
    joints = gi.get("WeldingJointQuantity")
    util = gi.get("UtilRate")
    rows.append({
        "idx": idx, "id": rec.get("ID"), "material": rec.get("MOMMATERIAL"),
        "n_groups": n_groups, "n_pipe_types": n_pipe_types,
        "total_demand": total_demand, "joints": joints, "util": util,
    })

print(f"\n老软件成功输出记录: {success}")
print(f"可解析问题的成功记录: {len(rows)}")

# 难度分档: 用 (管型数, 总需求) 粗分
def diff_tag(r):
    d = r["total_demand"]
    t = r["n_pipe_types"]
    if d <= 30 and t <= 3:
        return "易"
    if d <= 150 and t <= 12:
        return "中"
    return "难"

for r in rows:
    r["diff"] = diff_tag(r)
    by_diff[r["diff"]] += 1
print("难度分布:", dict(by_diff))

Path("scripts/_corpus_success.json").write_text(
    json.dumps(rows, ensure_ascii=False), encoding="utf-8")
print("已存 scripts/_corpus_success.json")
