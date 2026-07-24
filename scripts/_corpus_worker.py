"""单档 worker: 从语料按 idx 取一条, 跑 V4, 输出 RESULT <json> 行。
用法: python scripts/_corpus_worker.py <idx> <tl>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow

CORPUS = r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json"


def legacy_metrics(res):
    gi = res.get("GeneralInfo", {})
    result = res.get("Result", {})
    joints = int(float(gi.get("WeldingJointQuantity", 0)))
    util = float(gi.get("UtilRate", 0))
    weld_seqs = set()
    for wp in (result.get("WeldingPattern", {}) or {}).get("WeldingPipe", []) or []:
        for pat in wp.get("Pattern", []) or []:
            parts = tuple(int(float(x)) for x in pat.get("Part", "").split())
            if len(parts) >= 2:
                weld_seqs.add(parts)
    cut_seqs = set()
    for cp in (result.get("CuttingPattern", {}) or {}).get("CuttingPipe", []) or []:
        L = int(float(cp.get("Length", 0)))
        parts = tuple(sorted(int(float(x)) for x in cp.get("Part", "").split()))
        cut_seqs.add((L, parts))
    return {"joints": joints, "util": util,
            "weld_types": len(weld_seqs), "cut_types": len(cut_seqs)}


def main():
    idx = int(sys.argv[1])
    tl = float(sys.argv[2])
    data = json.loads(Path(CORPUS).read_text(encoding="utf-8"))
    rec = data["RECORDS"][idx]
    res = json.loads(rec["MOMRESULTJSON"])
    lg = legacy_metrics(res)
    g = merge_equivalent_pipes(parse_problem(json.loads(rec["MOMPROBLEMJSON"])).groups[0])
    uf = lg["util"] if lg["util"] else None
    out = {"idx": idx, "id": rec.get("ID"), "material": rec.get("MOMMATERIAL"), "legacy": lg}
    try:
        r = solve_arcflow(g, tl=tl, verbose=False, util_floor=uf)
        out["ours"] = None if r is None else {
            "joints": r["joints"], "weld_types": r["weld_types"],
            "cut_types": r["cut_types"], "util": r["util"], "solver": r.get("solver")}
    except Exception as e:  # noqa: BLE001
        out["ours"] = None
        out["err"] = str(e)[:120]
    print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
