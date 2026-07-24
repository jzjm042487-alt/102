"""看一条中等难度成功记录的 Result 明细结构，确定如何统计老软件拼法/切法种类。"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
CORPUS = r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"
rows = json.loads(Path("scripts/_corpus_success.json").read_text(encoding="utf-8"))
mids = [r for r in rows if r["diff"] == "中"]
print(f"中等难度: {len(mids)} 条, 取第一条 idx={mids[0]['idx']}")
data = json.loads(Path(CORPUS).read_text(encoding="utf-8"))
rec = data["RECORDS"][mids[0]["idx"]]
res = json.loads(rec["MOMRESULTJSON"])
result = res["Result"]
print("Result 类型:", type(result).__name__)
if isinstance(result, dict):
    print("Result 键:", list(result.keys()))
    for k, v in result.items():
        print(f"  {k}: {type(v).__name__}", end="")
        if isinstance(v, list) and v:
            print(f" len={len(v)} 首元素键={list(v[0].keys()) if isinstance(v[0], dict) else v[0]}")
        else:
            print()
elif isinstance(result, list):
    print("Result 长度:", len(result), "首元素:", json.dumps(result[0], ensure_ascii=False)[:500])

for pk in ("WeldingPattern", "CuttingPattern"):
    v = result.get(pk)
    print(f"\n=== {pk} ({type(v).__name__}) ===")
    if isinstance(v, dict):
        print("键:", list(v.keys())[:5], "... 共", len(v), "键")
        fk = next(iter(v))
        print("首键:", repr(fk), "首值:", json.dumps(v[fk], ensure_ascii=False)[:400])
    elif isinstance(v, list):
        print("长度:", len(v), "首元素:", json.dumps(v[0], ensure_ascii=False)[:400])
