"""老软件 MOM 导出语料：输入/输出指标与单案例结构（供回归与落盘复用）。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes

# 与 _corpus_worker / benchmark 对齐的默认语料路径
DEFAULT_CORPUS_PATH = Path(r"C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json")

CASE_FORMAT_VERSION = 1


def clean_mom_problem(payload: dict[str, Any]) -> dict[str, Any]:
    """合并重复管型、去掉 demand=0，与 benchmark_against_software 一致。"""
    pipes = payload.get("Pipe")
    if not isinstance(pipes, list):
        return payload
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for pipe in pipes:
        if not isinstance(pipe, dict):
            continue
        try:
            demand = int(float(pipe.get("pipe_demand", 0)))
        except (TypeError, ValueError):
            demand = 0
        if demand <= 0:
            continue
        key = (
            str(pipe.get("Parent_node", pipe.get("figure_number", ""))).strip(),
            str(pipe.get("jlxh", "")).strip(),
            str(pipe.get("cube_no", "")).strip(),
        )
        if key in merged:
            existing = merged[key]
            existing["pipe_demand"] = int(float(existing.get("pipe_demand", 0))) + demand
        else:
            copy = dict(pipe)
            copy["pipe_demand"] = demand
            merged[key] = copy
            order.append(key)
    cleaned = dict(payload)
    cleaned["Pipe"] = [merged[k] for k in order]
    return cleaned


def _parts_from_field(value: Any) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(round(float(item))))
            except (TypeError, ValueError):
                pass
        return tuple(out)
    text = str(value or "").replace("+", " ")
    out = []
    for token in text.split():
        try:
            out.append(int(round(float(token))))
        except (TypeError, ValueError):
            pass
    return tuple(out)


def legacy_result_status(result: dict[str, Any] | None) -> str:
    if not result:
        return "empty"
    gi = result.get("GeneralInfo") or {}
    msg = str(gi.get("Result", "")).strip()
    if msg.lower().startswith("success"):
        try:
            if float(gi.get("UtilRate") or 0) <= 0:
                return "success_zero_util"
        except (TypeError, ValueError):
            return "success_bad_util"
        inner = result.get("Result") or {}
        cutting = (inner.get("CuttingPattern") or {}).get("CuttingPipe") or []
        if not cutting:
            return "success_no_cutting"
        return "success"
    if not msg:
        return "no_result"
    return f"failed:{msg[:80]}"


def geom_class_for_group(group) -> str:
    max_pipe = max(p.length for p in group.pipes)
    min_stock = min(s.length for s in group.stocks)
    max_stock = max(s.length for s in group.stocks)
    if group.stock_length < group.demand_length:
        return "infeasible_len"
    if max_stock < max_pipe:
        return "all_stock_short"
    if min_stock < max_pipe:
        return "some_stock_fit"
    return "all_stock_fit"


def extract_output_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """从 MOMRESULTJSON 解析对象提取输出指标（拼法/切法从 Pattern 重算）。"""
    gi = result.get("GeneralInfo") or {}
    inner = result.get("Result") or {}
    weld_rows = (inner.get("WeldingPattern") or {}).get("WeldingPipe") or []
    cut_rows = (inner.get("CuttingPattern") or {}).get("CuttingPipe") or []

    weld_sigs: set[tuple[int, ...]] = set()
    joints_from_patterns = 0
    for wp in weld_rows:
        if not isinstance(wp, dict):
            continue
        patterns = wp.get("Pattern") or []
        if isinstance(patterns, dict):
            patterns = [patterns]
        for pat in patterns:
            if not isinstance(pat, dict):
                continue
            parts = _parts_from_field(pat.get("Part"))
            if not parts:
                continue
            try:
                qty = int(round(float(pat.get("Number", 1) or 1)))
            except (TypeError, ValueError):
                qty = 1
            if len(parts) >= 2:
                weld_sigs.add(parts)
            joints_from_patterns += max(0, len(parts) - 1) * qty

    cut_sigs: set[tuple[int, tuple[int, ...]]] = set()
    cut_bars_used = 0
    for cp in cut_rows:
        if not isinstance(cp, dict):
            continue
        try:
            stock_len = int(round(float(cp.get("Length", 0) or 0)))
        except (TypeError, ValueError):
            stock_len = 0
        parts = tuple(sorted(_parts_from_field(cp.get("Part"))))
        if not parts or not stock_len:
            continue
        cut_sigs.add((stock_len, parts))
        try:
            cut_bars_used += int(round(float(cp.get("Number", 0) or 0)))
        except (TypeError, ValueError):
            pass

    try:
        joints_gi = int(round(float(gi.get("WeldingJointQuantity", 0) or 0)))
    except (TypeError, ValueError):
        joints_gi = None
    try:
        util = float(gi.get("UtilRate") or 0)
    except (TypeError, ValueError):
        util = None
    try:
        demand_len_gi = int(round(float(gi.get("PipeLength_Of_SingleMaterialSpecification") or 0)))
    except (TypeError, ValueError):
        demand_len_gi = None
    try:
        used_len_gi = int(round(float(gi.get("StockLength_Of_SingleMaterialSpecification") or 0)))
    except (TypeError, ValueError):
        used_len_gi = None

    unused = inner.get("UnusedMaterials") or []
    unused_bars = 0
    for u in unused:
        if isinstance(u, dict):
            try:
                unused_bars += int(round(float(u.get("stock_demand", 0) or 0)))
            except (TypeError, ValueError):
                pass

    return {
        "legacy_status": legacy_result_status(result),
        "welding_joints": joints_gi,
        "welding_joints_recomputed": joints_from_patterns,
        "weld_pattern_types": len(weld_sigs),
        "cut_pattern_types": len(cut_sigs),
        "utilization_rate": util,
        "demand_length_reported": demand_len_gi,
        "used_stock_length_reported": used_len_gi,
        "cutting_pattern_rows": len(cut_rows),
        "cut_bars_in_patterns": cut_bars_used,
        "unused_stock_bars": unused_bars,
        "material_reported": gi.get("Material"),
        "specification_reported": gi.get("Specification"),
        "result_message": gi.get("Result"),
    }


def extract_input_metrics(
    problem: dict[str, Any],
    *,
    merge_pipes: bool = True,
) -> dict[str, Any]:
    """从 MOMPROBLEMJSON 解析对象提取输入规模指标。"""
    cleaned = clean_mom_problem(problem)
    parsed = parse_problem(cleaned)
    groups_out = []
    for g in parsed.groups:
        g2 = merge_equivalent_pipes(g) if merge_pipes else g
        pipe_lengths = Counter({p.length: p.demand for p in g2.pipes})
        stock_lengths = Counter({s.length: s.quantity for s in g2.stocks})
        groups_out.append(
            {
                "geom_class": geom_class_for_group(g2),
                "pipe_types": len(g2.pipes),
                "pipe_pieces": sum(p.demand for p in g2.pipes),
                "stock_types": len(g2.stocks),
                "stock_pieces": sum(s.quantity for s in g2.stocks),
                "demand_length_mm": g2.demand_length,
                "stock_length_mm": g2.stock_length,
                "target_util_rate": g2.target_rate,
                "max_pipe_length_mm": max(p.length for p in g2.pipes),
                "min_pipe_length_mm": min(p.length for p in g2.pipes),
                "max_stock_length_mm": max(s.length for s in g2.stocks),
                "min_stock_length_mm": min(s.length for s in g2.stocks),
                "pipe_length_histogram": dict(sorted(pipe_lengths.items())),
                "stock_length_histogram": dict(sorted(stock_lengths.items())),
            }
        )
    agg = {
        "groups": len(groups_out),
        "pipe_types": sum(x["pipe_types"] for x in groups_out),
        "pipe_pieces": sum(x["pipe_pieces"] for x in groups_out),
        "stock_types": sum(x["stock_types"] for x in groups_out),
        "stock_pieces": sum(x["stock_pieces"] for x in groups_out),
        "demand_length_mm": sum(x["demand_length_mm"] for x in groups_out),
        "stock_length_mm": sum(x["stock_length_mm"] for x in groups_out),
    }
    if agg["demand_length_mm"]:
        agg["stock_to_demand_ratio"] = round(
            agg["stock_length_mm"] / agg["demand_length_mm"], 6
        )
    else:
        agg["stock_to_demand_ratio"] = None
    return {
        "aggregate": agg,
        "per_group": groups_out,
        "material": cleaned.get("material") or problem.get("material"),
        "specifications": cleaned.get("specifications") or problem.get("specifications"),
        "blade_margin_mm": cleaned.get("BladeMargin"),
        "min_welding_length_mm": cleaned.get("Min_Welding_Length"),
    }


def difficulty_tag(agg: Mapping[str, Any]) -> str:
    d = int(agg.get("pipe_pieces") or 0)
    t = int(agg.get("pipe_types") or 0)
    if d <= 30 and t <= 3:
        return "易"
    if d <= 150 and t <= 12:
        return "中"
    return "难"


def load_corpus_records(source: Path | None = None) -> list[dict[str, Any]]:
    path = source or DEFAULT_CORPUS_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("RECORDS") or []


def analyze_record(
    record: dict[str, Any],
    idx: int,
) -> dict[str, Any]:
    """单条 RECORD 完整分析（不落盘）。"""
    meta = {
        "idx": idx,
        "id": record.get("ID"),
        "problem_no": record.get("MOMPROBLEMNO"),
        "material_row": record.get("MOMMATERIAL"),
        "calculate_status": record.get("MOMCALCULATESTATUS"),
        "material_rate_row": record.get("MOMMATERIALRATE"),
        "outside_diameter": record.get("MOMOUTSIDEDIAMETER"),
        "wall_thickness": record.get("MOMWALLTHICKNESS"),
    }
    out: dict[str, Any] = {
        "format_version": CASE_FORMAT_VERSION,
        "meta": meta,
        "input": None,
        "input_error": None,
        "output": None,
        "output_error": None,
        "metrics": {},
    }
    prob_raw = record.get("MOMPROBLEMJSON")
    res_raw = record.get("MOMRESULTJSON")
    try:
        if not prob_raw:
            raise ValueError("missing MOMPROBLEMJSON")
        problem = json.loads(prob_raw) if isinstance(prob_raw, str) else prob_raw
        out["input"] = extract_input_metrics(problem)
        out["input"]["difficulty"] = difficulty_tag(out["input"]["aggregate"])
    except Exception as exc:  # noqa: BLE001
        out["input_error"] = f"{type(exc).__name__}: {exc}"

    try:
        if not res_raw or not str(res_raw).strip():
            out["output_error"] = "missing MOMRESULTJSON"
        elif str(res_raw).strip()[:1] not in ("{", "["):
            out["output_error"] = f"not_json:{str(res_raw)[:120]}"
        else:
            result = json.loads(res_raw) if isinstance(res_raw, str) else res_raw
            out["output"] = extract_output_metrics(result)
    except Exception as exc:  # noqa: BLE001
        out["output_error"] = f"{type(exc).__name__}: {exc}"

    inp = out.get("input") or {}
    agg = inp.get("aggregate") or {}
    outp = out.get("output") or {}
    out["metrics"] = {
        "pipe_types": agg.get("pipe_types"),
        "pipe_pieces": agg.get("pipe_pieces"),
        "stock_types": agg.get("stock_types"),
        "stock_pieces": agg.get("stock_pieces"),
        "welding_joints": outp.get("welding_joints"),
        "weld_pattern_types": outp.get("weld_pattern_types"),
        "cut_pattern_types": outp.get("cut_pattern_types"),
        "utilization_rate": outp.get("utilization_rate"),
        "legacy_status": outp.get("legacy_status") if outp else "no_parse",
        "difficulty": inp.get("difficulty"),
        "geom_class": (
            inp.get("per_group", [{}])[0].get("geom_class") if inp.get("per_group") else None
        ),
    }
    return out


def render_case_analysis_md(analysis: dict[str, Any]) -> str:
    """生成单案例中文分析 Markdown。"""
    m = analysis.get("meta") or {}
    met = analysis.get("metrics") or {}
    lines = [
        f"# 语料案例 idx={m.get('idx')}",
        "",
        "## 元数据",
        "",
        f"| 字段 | 值 |",
        f"|------|-----|",
        f"| 记录 ID | `{m.get('id')}` |",
        f"| 子问题号 | {m.get('problem_no')} |",
        f"| 材质(行) | {m.get('material_row')} |",
        f"| 计算状态(MOMCALCULATESTATUS) | {m.get('calculate_status')} |",
        f"| 外径 / 壁厚 | {m.get('outside_diameter')} / {m.get('wall_thickness')} |",
        "",
        "## 关键指标（回归对标用）",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 管子类型数 | {met.get('pipe_types')} |",
        f"| 管子根数(需求) | {met.get('pipe_pieces')} |",
        f"| 原材料类型数 | {met.get('stock_types')} |",
        f"| 原材料根数(库存) | {met.get('stock_pieces')} |",
        f"| 焊口数 | {met.get('welding_joints')} |",
        f"| 拼法数 | {met.get('weld_pattern_types')} |",
        f"| 切法数 | {met.get('cut_pattern_types')} |",
        f"| 利用率 | {met.get('utilization_rate')} |",
        f"| 老软件结果状态 | {met.get('legacy_status')} |",
        f"| 难度(粗分) | {met.get('difficulty')} |",
        f"| 几何分类(首组) | {met.get('geom_class')} |",
        "",
    ]
    if analysis.get("input_error"):
        lines.extend(["## 输入解析", "", f"**失败**: {analysis['input_error']}", ""])
    elif analysis.get("input"):
        inp = analysis["input"]
        agg = inp.get("aggregate") or {}
        lines.extend(
            [
                "## 输入详解",
                "",
                f"- 材质/规格: {inp.get('material')} / {inp.get('specifications')}",
                f"- 组数: {agg.get('groups')}",
                f"- 需求总长 mm: {agg.get('demand_length_mm')}",
                f"- 库存总长 mm: {agg.get('stock_length_mm')}",
                f"- 库存/需求比: {agg.get('stock_to_demand_ratio')}",
                f"- 目标利用率: {inp.get('per_group', [{}])[0].get('target_util_rate') if inp.get('per_group') else '—'}",
                "",
            ]
        )
        for gi, g in enumerate(inp.get("per_group") or []):
            lines.append(f"### 组 {gi}")
            lines.append(f"- 几何: `{g.get('geom_class')}`")
            lines.append(f"- 管型 {g.get('pipe_types')} 种 / {g.get('pipe_pieces')} 根")
            lines.append(
                f"- 管长范围: {g.get('min_pipe_length_mm')}–{g.get('max_pipe_length_mm')} mm"
            )
            lines.append(f"- 原料 {g.get('stock_types')} 种 / {g.get('stock_pieces')} 根")
            lines.append(
                f"- 原料长度范围: {g.get('min_stock_length_mm')}–{g.get('max_stock_length_mm')} mm"
            )
            hist_p = g.get("pipe_length_histogram") or {}
            if hist_p:
                top_p = sorted(hist_p.items(), key=lambda kv: -kv[1])[:8]
                lines.append(
                    "- 管长 Top: "
                    + ", ".join(f"{L}mm×{n}" for L, n in top_p)
                    + (" …" if len(hist_p) > 8 else "")
                )
            hist_s = g.get("stock_length_histogram") or {}
            if hist_s:
                lines.append(
                    "- 库存: " + ", ".join(f"{L}mm×{n}" for L, n in sorted(hist_s.items()))
                )
            lines.append("")

    if analysis.get("output_error"):
        lines.extend(["## 输出解析", "", f"**失败**: {analysis['output_error']}", ""])
    elif analysis.get("output"):
        o = analysis["output"]
        lines.extend(
            [
                "## 输出详解（老软件）",
                "",
                f"- 状态: `{o.get('legacy_status')}`",
                f"- GeneralInfo 焊口: {o.get('welding_joints')}",
                f"- Pattern 重算焊口: {o.get('welding_joints_recomputed')}",
                f"- 拼法种类: {o.get('weld_pattern_types')}",
                f"- 切法种类: {o.get('cut_pattern_types')}",
                f"- 切法行数: {o.get('cutting_pattern_rows')}",
                f"- 利用率: {o.get('utilization_rate')}",
                f"- 报告需求长/用料长: {o.get('demand_length_reported')} / {o.get('used_stock_length_reported')}",
                f"- 余料根数(UnusedMaterials): {o.get('unused_stock_bars')}",
                "",
            ]
        )
        if o.get("welding_joints") is not None and o.get("welding_joints_recomputed") is not None:
            j1, j2 = o["welding_joints"], o["welding_joints_recomputed"]
            if j1 != j2:
                lines.append(
                    f"> 注意: GeneralInfo 焊口({j1})与 Pattern 重算({j2})不一致，"
                    "回归默认以 **GeneralInfo.WeldingJointQuantity** 为焊口数，"
                    "拼法/切法数以 Pattern 去重为准。"
                )
                lines.append("")

    lines.append("## 文件说明")
    lines.append("")
    lines.append("- `input.json` — 清洗后的 MOMPROBLEMJSON（可直接喂 `parse_problem`）")
    lines.append("- `output.json` — 完整 MOMRESULTJSON（无解则为 null）")
    lines.append("- `metrics.json` — 本页指标 + meta，供脚本加载")
    lines.append("")
    return "\n".join(lines)


def case_dir_name(idx: int, record_id: str | None) -> str:
    short = (str(record_id or "noid").split("-")[0])[:8]
    return f"{idx:04d}_{short}"


def load_legacy_case(
    root: Path,
    idx: int,
) -> dict[str, Any]:
    """从落盘目录按 idx 加载（读 metrics + input + output）。"""
    index_path = root / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        entry = next((e for e in index.get("cases", []) if e.get("idx") == idx), None)
        if entry and entry.get("dir"):
            case_path = root / entry["dir"]
        else:
            case_path = None
    else:
        case_path = None
    if case_path is None or not case_path.is_dir():
        matches = list(root.glob(f"{idx:04d}_*"))
        if not matches:
            raise FileNotFoundError(f"no case dir for idx={idx} under {root}")
        case_path = matches[0]
    metrics = json.loads((case_path / "metrics.json").read_text(encoding="utf-8"))
    inp = json.loads((case_path / "input.json").read_text(encoding="utf-8"))
    out_path = case_path / "output.json"
    output = None
    if out_path.exists():
        raw = out_path.read_text(encoding="utf-8").strip()
        if raw and raw != "null":
            output = json.loads(raw)
    return {
        "dir": str(case_path),
        "metrics": metrics,
        "problem": inp,
        "legacy_result": output,
    }
