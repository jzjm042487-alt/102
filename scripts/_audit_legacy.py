"""全量核查 _picked20_full.json 20 样本的 legacy 真伪。

对每档: 用 record_idx 取语料记录, 判断 MOMRESULTJSON 是否真实 JSON 结果(status=1),
还是 "running"/空 (status=0, 伪基准)。 输出真伪表。
"""
import json
from pathlib import Path

CORPUS = Path(r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")
d = json.loads(CORPUS.read_text(encoding="utf-8"))["RECORDS"]
picked = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))

print(f"{'Lv':>3} {'idx':>5} {'status':>6} {'reslen':>7} {'real?':>5}  legacy")
fake = []
for s in sorted(picked, key=lambda x: x["level"]):
    lv = s["level"]; idx = s.get("record_idx")
    r = d[idx] if idx is not None and 0 <= idx < len(d) else None
    res = str(r.get("MOMRESULTJSON") or "") if r else ""
    st = r.get("MOMCALCULATESTATUS") if r else None
    real = res.strip()[:1] in ("{", "[") and len(res) > 200
    if not real:
        fake.append(lv)
    print(f"{lv:>3} {str(idx):>5} {str(st):>6} {len(res):>7} {('YES' if real else 'NO'):>5}  {s.get('legacy')}")
print(f"\n伪基准(老软件无真实解)档: {fake}")
Path("scripts/_fake_levels.json").write_text(json.dumps(fake), encoding="utf-8")
