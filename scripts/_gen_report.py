"""生成 V4 排料方案单页验收报告(自包含 HTML, 双击即开)。

跑指定档位的 solve_arcflow, 把结果(指标对比 + 每档切法/拼法明细)内联进
一个纯静态 HTML(SVG 可视化), 供人工验收。

用法: python scripts/_gen_report.py [levels...] [--tl 60] [--out docs/xxx.html]
默认: levels = 7 8 10 12; tl=60; out=docs/v4-acceptance-report.html
"""
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts._arcflow_v3 import solve_arcflow
from scripts._check_v3 import check
from scripts._export_mock import legacy_detail


# 老软件完整语料(含 MOMRESULTJSON 逐条排料布局)。首选此文件, 按 record_idx 索引。
CORPUS_PATH = Path("backend/data/samples/software-io/batch-DGMOM/software_records.json")
# 备用: 工作区样例库(仅 100 条, 按 problem.id 匹配)。
LIB_PATH = Path("frontend-next/public/samples.json")


def _load_corpus():
    if not CORPUS_PATH.exists():
        return None
    try:
        return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["RECORDS"]
    except Exception:
        return None


def _load_lib_by_id():
    """样例库按 problem.id -> legacy(含 Result) 索引。语料缺失时兜底用。"""
    if not LIB_PATH.exists():
        return {}
    try:
        lib = json.loads(LIB_PATH.read_text(encoding="utf-8")).get("samples", [])
    except Exception:
        return {}
    return {s.get("problem", {}).get("id"): s["legacy"]
            for s in lib if s.get("problem", {}).get("id") and s.get("legacy")}


_CORPUS = None
_LIB_BY_ID = None


def _rows_to_layout(cut_rows, weld_rows):
    cuts = [{"L": c["L"], "segs": c["segs"], "waste": c["trim"], "count": c["num"]}
            for c in cut_rows]
    welds = [{"pipe_len": w["len"], "seq": w["segs"],
              "joints": max(0, len(w["segs"]) - 1), "count": w["num"]}
             for w in weld_rows]
    return cuts, welds


def _legacy_layout_for(sample):
    """取某样例的老软件排料明细 (legacy_cuts, legacy_welds) 或 (None, None)。
    优先从完整语料(record_idx)解析真实排料; 语料缺失时回退样例库(problem.id)。
    老软件未产出布局(仅 GeneralInfo / 运行中占位串 / 无 Result)时返回 (None, None)。"""
    global _CORPUS, _LIB_BY_ID
    if _CORPUS is None:
        _CORPUS = _load_corpus() or []
    if _LIB_BY_ID is None:
        _LIB_BY_ID = _load_lib_by_id()

    # 1) 完整语料按 record_idx。
    ri = sample.get("record_idx")
    if _CORPUS and ri is not None and 0 <= ri < len(_CORPUS):
        try:
            cut_rows, weld_rows = legacy_detail(_CORPUS[ri])
            if cut_rows or weld_rows:
                return _rows_to_layout(cut_rows, weld_rows)
        except Exception:
            pass
    # 2) 样例库按 problem.id 兜底。
    pid = sample.get("problem", {}).get("id")
    legacy_result = _LIB_BY_ID.get(pid)
    if legacy_result is not None:
        try:
            cut_rows, weld_rows = legacy_detail({"MOMRESULTJSON": legacy_result})
            if cut_rows or weld_rows:
                return _rows_to_layout(cut_rows, weld_rows)
        except Exception:
            pass
    return None, None


def build_case(lv, tl):
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    s = next((x for x in samples if x["level"] == lv), None)
    if s is None:
        return None
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    t0 = time.time()
    res = solve_arcflow(g, tl=tl, verbose=False)
    dt = time.time() - t0
    lg = s["legacy"]
    if res is None:
        return {"level": lv, "spec": s["spec"], "legacy": lg, "solved": False, "dt": dt}
    errs = check(g, res)
    # 切法明细: [{L, segs, waste, count}]
    cuts = []
    for (L, segs), cnt in sorted(res["cut_patterns"].items(),
                                 key=lambda kv: (-kv[0][0], kv[1])):
        used = sum(segs) + (len(segs) - 1) * g.blade_margin if segs else 0
        cuts.append({"L": L, "segs": list(segs), "waste": L - used, "count": cnt})
    # 拼法明细: [{pipe_len, seq, joints, count}]
    welds = []
    for (i, seq), cnt in sorted(res["weld_patterns"].items(),
                                key=lambda kv: (kv[0][0], -len(kv[0][1]))):
        welds.append({"pipe_len": g.pipes[i].length, "seq": list(seq),
                      "joints": len(seq) - 1, "count": cnt})
    legacy_cuts, legacy_welds = _legacy_layout_for(s)
    return {
        "level": lv, "spec": s["spec"], "solved": True, "dt": round(dt, 1),
        "legacy": lg, "self_check_ok": not errs, "self_check_errs": errs,
        "target_rate": round(g.target_rate, 4),
        "demand_length": g.demand_length, "stock_length": g.stock_length,
        "pipes": [{"length": p.length, "demand": p.demand} for p in g.pipes],
        "stocks": [{"length": st.length, "quantity": st.quantity} for st in g.stocks],
        "metrics": {
            "joints": res["joints"], "weld_types": res["weld_types"],
            "cut_types": res["cut_types"], "seg_types": res["seg_types"],
            "util": round(res["util"], 4), "used_len": res["used_len"],
        },
        "cuts": cuts, "welds": welds,
        "legacy_cuts": legacy_cuts, "legacy_welds": legacy_welds,
        "has_legacy_layout": legacy_cuts is not None,
    }


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V4 排料方案验收报告</title>
<style>
  :root {
    --bg:#0f1420; --card:#1a2332; --line:#2a3648; --fg:#e6ecf5; --muted:#8b9bb4;
    --good:#3fb950; --bad:#f85149; --warn:#d29922; --seg:#4493f8; --waste:#3a4658;
    --joint:#f0883e;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; padding:24px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); margin-bottom:20px; font-size:13px; }
  .case { background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:20px; margin-bottom:20px; }
  .case h2 { font-size:17px; margin:0 0 12px; display:flex; align-items:center; gap:10px; }
  .badge { font-size:12px; padding:2px 10px; border-radius:20px; font-weight:600; }
  .badge.ok { background:rgba(63,185,80,.15); color:var(--good); }
  .badge.err { background:rgba(248,81,73,.15); color:var(--bad); }
  table.metrics { border-collapse:collapse; width:100%; margin-bottom:16px; font-size:13px; }
  table.metrics th, table.metrics td { border:1px solid var(--line); padding:6px 12px; text-align:center; }
  table.metrics th { background:#141c29; color:var(--muted); font-weight:600; }
  .win { color:var(--good); font-weight:700; }
  .lose { color:var(--bad); font-weight:700; }
  .tie { color:var(--fg); }
  .meta { color:var(--muted); font-size:12px; margin-bottom:16px; }
  .section-title { font-size:14px; font-weight:600; margin:14px 0 8px; color:var(--fg); }
  .bar-row { display:flex; align-items:center; gap:10px; margin-bottom:6px; }
  .bar-label { width:104px; flex:none; font-size:11px; color:var(--muted); text-align:right; }
  .bar-count { width:44px; flex:none; font-size:11px; color:var(--muted); }
  svg { display:block; }
  .seg-text { font:10px sans-serif; fill:#fff; }
  .legend { display:flex; gap:16px; font-size:12px; color:var(--muted); margin:8px 0 14px; }
  .legend i { display:inline-block; width:12px; height:12px; border-radius:2px;
    vertical-align:-1px; margin-right:5px; }
  .cmp { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
  .cmp .col { min-width:0; }
  .col-title { font-size:13px; font-weight:700; margin:0 0 8px; padding:4px 10px;
    border-radius:6px; display:inline-block; }
  .col-title.ours { background:rgba(68,147,248,.15); color:var(--seg); }
  .col-title.legacy { background:rgba(210,153,34,.15); color:var(--warn); }
  .nolayout { color:var(--muted); font-size:12px; padding:8px 0; }
  .bar-scroll { max-height:360px; overflow-y:auto; padding-right:4px; }
  @media (max-width:900px){ .cmp { grid-template-columns:1fr; } }
</style>
</head>
<body>
<h1>V4 排料方案验收报告</h1>
<div class="sub">词典序目标: 焊口 &rarr; 拼法种类 &rarr; 切法种类 &rarr; 段种类 &rarr; 利用率 &nbsp;|&nbsp; 生成于 __TS__</div>
<div id="app"></div>
<script>
const CASES = __DATA__;

function fmt(n){ return n==null ? "&mdash;" : n.toLocaleString(); }
function cmp(ours, lg, lowerBetter){
  if(lg==null || ours==null) return "tie";
  if(ours===lg) return "tie";
  const better = lowerBetter ? ours<lg : ours>lg;
  return better ? "win" : "lose";
}

function segBar(items, total, opts){
  // items: [{len, kind}] kind: 'seg'|'waste'; 焊口在相邻 seg 之间
  const W = 420, H = 24, scale = W/total;
  let x = 0, parts = [], joints = [];
  const colors = {seg:'var(--seg)', waste:'var(--waste)'};
  items.forEach((it,idx)=>{
    const w = Math.max(1, it.len*scale);
    const showTxt = w > 30;
    parts.push(`<rect x="${x.toFixed(1)}" y="0" width="${w.toFixed(1)}" height="${H}" fill="${colors[it.kind]}" stroke="#0f1420" stroke-width="0.5"/>`
      + (showTxt ? `<text class="seg-text" x="${(x+w/2).toFixed(1)}" y="16" text-anchor="middle">${it.len}</text>` : ``));
    x += w;
    // 焊口标记(拼法: 两个 seg 之间)
    if(opts.weldJoints && idx < items.length-1 && it.kind==='seg' && items[idx+1].kind==='seg'){
      joints.push(`<line x1="${x.toFixed(1)}" y1="-2" x2="${x.toFixed(1)}" y2="${H+2}" stroke="var(--joint)" stroke-width="2.5"/>`);
    }
  });
  return `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${parts.join('')}${joints.join('')}</svg>`;
}

function renderCase(c){
  if(!c.solved){
    return `<div class="case"><h2>L${c.level} &middot; ${c.spec} <span class="badge err">无解</span></h2></div>`;
  }
  const m=c.metrics, lg=c.legacy;
  const rows = [
    ["总焊口", m.joints, lg.joints, true],
    ["拼法种类", m.weld_types, lg.weld_types, true],
    ["切法种类", m.cut_types, lg.cut_types, true],
    ["段种类", m.seg_types, null, true],
    ["利用率", (m.util*100).toFixed(2)+"%", lg.util!=null?(lg.util*100).toFixed(2)+"%":null, false],
  ];
  const metricRows = rows.map(([name,ours,lgv,lb])=>{
    const cls = (name==="段种类") ? "tie" : cmp(
      typeof ours==='string'?parseFloat(ours):ours,
      typeof lgv==='string'?parseFloat(lgv):lgv, lb);
    return `<tr><td style="color:var(--muted)">${name}</td>`
      + `<td class="${cls}">${ours}</td>`
      + `<td class="tie">${lgv==null?'&mdash;':lgv}</td></tr>`;
  }).join('');

  const badge = c.self_check_ok
    ? `<span class="badge ok">自检通过</span>`
    : `<span class="badge err">自检不一致</span>`;

  // 统一比例: 切法以两侧最大定尺为准, 拼法以两侧最大管长为准, 便于横向对比。
  const allCuts = (c.cuts||[]).concat(c.legacy_cuts||[]);
  const cutScale = Math.max(1, ...allCuts.map(x=>x.L));
  const allWelds = (c.welds||[]).concat(c.legacy_welds||[]);
  const weldScale = Math.max(1, ...allWelds.map(w=> (w.seq||[]).reduce((a,b)=>a+b,0) ));

  function cutBarsOf(cuts){
    if(!cuts || !cuts.length) return `<div class="nolayout">无切法数据</div>`;
    return cuts.map(cut=>{
      const items = cut.segs.map(s=>({len:s,kind:'seg'}));
      if(cut.waste>0) items.push({len:cut.waste,kind:'waste'});
      return `<div class="bar-row">`
        + `<div class="bar-label">定尺 ${fmt(cut.L)}</div>`
        + segBar(items, cutScale, {weldJoints:false})
        + `<div class="bar-count">&times;${cut.count}</div></div>`;
    }).join('');
  }
  function weldBarsOf(welds){
    if(!welds || !welds.length) return `<div class="nolayout">无焊口(纯切)或无拼法数据</div>`;
    return welds.map(w=>{
      const items = w.seq.map(s=>({len:s,kind:'seg'}));
      return `<div class="bar-row">`
        + `<div class="bar-label">管 ${fmt(w.pipe_len)} (${w.joints}焊口)</div>`
        + segBar(items, weldScale, {weldJoints:true})
        + `<div class="bar-count">&times;${w.count}</div></div>`;
    }).join('');
  }

  const legacyAvail = c.has_legacy_layout;
  const legacyNote = legacyAvail ? `` :
    `<div class="nolayout">该档老软件逐条排料布局不在工作区样例库中(仅有汇总指标: 焊口 ${fmt(lg.joints)} / 切法 ${fmt(lg.cut_types)} / 拼法 ${fmt(lg.weld_types)} / 利用率 ${lg.util!=null?(lg.util*100).toFixed(2)+'%':'—'})。</div>`;

  const cutLegend = `<div class="legend"><span><i style="background:var(--seg)"></i>切段</span><span><i style="background:var(--waste)"></i>废料</span></div>`;
  const weldLegend = `<div class="legend"><span><i style="background:var(--seg)"></i>段</span><span><i style="background:var(--joint);width:3px"></i>焊口</span></div>`;

  const cutCmp = `<div class="section-title">切法排料对比</div>${cutLegend}
    <div class="cmp">
      <div class="col"><div class="col-title ours">V4 (我们) · ${c.cuts.length} 种</div>
        <div class="bar-scroll">${cutBarsOf(c.cuts)}</div></div>
      <div class="col"><div class="col-title legacy">老软件 · ${legacyAvail?c.legacy_cuts.length+' 种':'—'}</div>
        ${legacyAvail?`<div class="bar-scroll">${cutBarsOf(c.legacy_cuts)}</div>`:legacyNote}</div>
    </div>`;

  const weldCmp = `<div class="section-title">拼法排料对比 (竖橙线=焊口)</div>${weldLegend}
    <div class="cmp">
      <div class="col"><div class="col-title ours">V4 (我们) · ${c.welds.length} 种</div>
        <div class="bar-scroll">${weldBarsOf(c.welds)}</div></div>
      <div class="col"><div class="col-title legacy">老软件 · ${legacyAvail?c.legacy_welds.length+' 种':'—'}</div>
        ${legacyAvail?`<div class="bar-scroll">${weldBarsOf(c.legacy_welds)}</div>`:legacyNote}</div>
    </div>`;

  return `<div class="case">
    <h2>L${c.level} &middot; ${c.spec} ${badge}
      <span style="font-size:12px;color:var(--muted);font-weight:400">求解 ${c.dt}s</span></h2>
    <table class="metrics">
      <tr><th>指标</th><th>V4 (我们)</th><th>老软件</th></tr>
      ${metricRows}
    </table>
    <div class="meta">需求总长 ${fmt(c.demand_length)} &nbsp;|&nbsp; 用料 ${fmt(m.used_len)} &nbsp;|&nbsp; 目标利用率 ${(c.target_rate*100).toFixed(2)}% &nbsp;|&nbsp; 管型 ${c.pipes.length} 种, 库存 ${c.stocks.length} 种</div>
    ${cutCmp}
    ${weldCmp}
  </div>`;
}

document.getElementById('app').innerHTML = CASES.map(renderCase).join('');
</script>
</body>
</html>
"""


def _read_existing_cases(out_path):
    """从已有报告 HTML 中解析出内嵌的 CASES(增量合并用)。失败返回 []。"""
    if not out_path.exists():
        return []
    try:
        h = out_path.read_text(encoding="utf-8")
        mt = re.search(r"const CASES = (\[.*?\]);\n", h, re.S)
        return json.loads(mt.group(1)) if mt else []
    except Exception:
        return []


def _write_report(cases, out_path):
    """把 cases 按档位排序后写出 HTML(每算完一档即时落盘, 防中途卡死丢结果)。"""
    cases = sorted(cases, key=lambda c: c["level"])
    html = (HTML_TMPL
            .replace("__DATA__", json.dumps(cases, ensure_ascii=False))
            .replace("__TS__", time.strftime("%Y-%m-%d %H:%M")))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main():
    args = sys.argv[1:]
    tl = 60.0
    out = "docs/v4-acceptance-report.html"
    append = False
    levels = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tl":
            tl = float(args[i + 1]); i += 2
        elif a == "--out":
            out = args[i + 1]; i += 2
        elif a == "--append":
            append = True; i += 1
        else:
            levels.append(int(a)); i += 1
    if not levels:
        levels = [8, 12, 11, 9]
    out_path = Path(out)
    # 增量: 读入已有档, 新档覆盖同 level 旧档。
    by_level = {c["level"]: c for c in (_read_existing_cases(out_path) if append else [])}
    for lv in levels:
        print(f"求解 L{lv} (tl={tl}s) ...", flush=True)
        c = build_case(lv, tl)
        if c is not None:
            by_level[lv] = c
            if c.get("solved"):
                m = c["metrics"]
                print(f"  L{lv}: 焊口={m['joints']} 拼法={m['weld_types']} "
                      f"切法={m['cut_types']} 段={m['seg_types']} "
                      f"util={m['util']:.4f} 自检={'OK' if c['self_check_ok'] else '不一致'}",
                      flush=True)
            # 每档即时落盘, 中途卡死也保住已完成结果。
            _write_report(list(by_level.values()), out_path)
            print(f"  已写入 {out_path.name} (当前 {len(by_level)} 档)", flush=True)
    print(f"\n报告已生成: {out_path.resolve()}", flush=True)


if __name__ == "__main__":
    main()
