"""预扫描样本库, 建立轻量索引(管型数/需求/老软件指标), 落盘 data/sample_index.json。
带进度打印, 定位是否有样本卡住。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from app.domain import parse_problem
from _exp_colgen import merge_equivalent_pipes, legacy_alpha_and_metrics

SRC = Path(r"d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")
data = json.loads(SRC.read_text(encoding="utf-8"))
recs = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
print(f"样本总数: {len(recs)}", flush=True)

index = []
t0 = time.monotonic()
for k, rec in enumerate(recs):
    if k % 100 == 0:
        print(f"  进度 {k}/{len(recs)}  已收录 {len(index)}  用时 {time.monotonic()-t0:.0f}s", flush=True)
    if not rec.get("MOMRESULTJSON") or not rec.get("MOMPROBLEMJSON"):
        continue
    sid = str(rec.get("id") or rec.get("ID") or "")
    try:
        _o, lm = legacy_alpha_and_metrics(rec)
        if not lm or lm.get("util", 0) <= 0:
            continue
        ts = time.monotonic()
        prob = parse_problem(json.loads(rec["MOMPROBLEMJSON"]))
        if len(prob.groups) != 1:
            continue
        g = merge_equivalent_pipes(prob.groups[0])
        parse_dt = time.monotonic() - ts
        if parse_dt > 3:
            print(f"    [慢] {sid[:12]} parse+merge 用时 {parse_dt:.1f}s (管型{len(g.pipes)})", flush=True)
        index.append({
            "id": sid,
            "n_pipes": len(g.pipes),
            "demand": sum(p.demand for p in g.pipes),
            "pipe_lens": sorted({p.length for p in g.pipes}),
            "stock_lens": sorted({s.length for s in g.stocks}),
            "legacy_joints": lm.get("joints"),
            "legacy_weld": lm.get("weld_types"),
            "legacy_cut": lm.get("cut_types"),
            "legacy_util": round(lm.get("util", 0), 4),
        })
    except Exception as e:
        continue

out = ROOT / "data" / "sample_index.json"
out.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n完成: 收录 {len(index)} 个样本 → {out}  总用时 {time.monotonic()-t0:.0f}s", flush=True)
