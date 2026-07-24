"""修正 _picked20_full.json: 8 个伪基准档的 legacy 置为无解标记, 保留原值到 legacy_claimed 备查。

伪基准(老软件语料库无真实解): [2,5,7,9,10,11,18,19]
真实基准保留不动。
"""
import json
from pathlib import Path

fp = Path("scripts/_picked20_full.json")
picked = json.loads(fp.read_text(encoding="utf-8"))
fake = set(json.loads(Path("scripts/_fake_levels.json").read_text()))

changed = []
for s in picked:
    if s["level"] in fake:
        s["legacy_claimed"] = s.get("legacy")  # 备查: 原样本臆造/错配值
        s["legacy"] = None                      # 老软件无真实解
        s["legacy_status"] = "no_result"        # 语料库 status=0 / running
        changed.append(s["level"])

fp.write_text(json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已修正 {len(changed)} 档 legacy=null: {sorted(changed)}")
print("真实基准档(不变):", sorted(s["level"] for s in picked if s.get("legacy") is not None))
