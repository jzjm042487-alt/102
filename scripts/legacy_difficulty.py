"""可计算语料的难度分类（输入规模 + 管长离散 + 输出段长离散 + 几何/算法理解）。"""

from __future__ import annotations

import math
from typing import Any, Mapping

from scripts.legacy_corpus_lib import _parts_from_field

DIFFICULTY_TAXONOMY_VERSION = 1

# 五档中文标签（测试抽样 / SLA 用）
TIER_LABELS = ("D1-入门", "D2-常规", "D3-进阶", "D4-困难", "D5-极难")


def _weighted_histogram_stats(length_qty: Mapping[int, int]) -> dict[str, Any]:
    """按根数加权的管长/段长分布统计。"""
    if not length_qty:
        return {
            "unique_lengths": 0,
            "total_pieces": 0,
            "mean_mm": None,
            "std_mm": None,
            "cv": None,
            "spread_ratio": None,
            "entropy_norm": None,
        }
    total = sum(int(q) for q in length_qty.values())
    if total <= 0:
        return {
            "unique_lengths": len(length_qty),
            "total_pieces": 0,
            "mean_mm": None,
            "std_mm": None,
            "cv": None,
            "spread_ratio": None,
            "entropy_norm": None,
        }
    mean = sum(int(L) * int(q) for L, q in length_qty.items()) / total
    var = sum(int(q) * (int(L) - mean) ** 2 for L, q in length_qty.items()) / total
    std = math.sqrt(var)
    cv = std / mean if mean > 1e-9 else 0.0
    lengths = [int(L) for L in length_qty]
    spread = (max(lengths) - min(lengths)) / mean if mean > 1e-9 else 0.0
    ent = 0.0
    for q in length_qty.values():
        p = int(q) / total
        if p > 0:
            ent -= p * math.log(p)
    n = len(length_qty)
    ent_norm = ent / math.log(n) if n > 1 else 0.0
    return {
        "unique_lengths": n,
        "total_pieces": total,
        "mean_mm": round(mean, 2),
        "std_mm": round(std, 2),
        "cv": round(cv, 4),
        "spread_ratio": round(spread, 4),
        "entropy_norm": round(ent_norm, 4),
    }


def input_pipe_dispersion(per_group: list[Mapping[str, Any]]) -> dict[str, Any]:
    """合并各组的管长直方图，算输入离散度。"""
    merged: dict[int, int] = {}
    for g in per_group:
        hist = g.get("pipe_length_histogram") or {}
        for L, q in hist.items():
            merged[int(L)] = merged.get(int(L), 0) + int(q)
    stats = _weighted_histogram_stats(merged)
    stats["histogram"] = {str(k): v for k, v in sorted(merged.items())}
    return stats


def output_segment_dispersion(result: dict[str, Any] | None) -> dict[str, Any]:
    """从老软件拼/切 Pattern 统计「产出段长」离散度（材料段种类与分布）。"""
    if not result:
        return {
            "observed": False,
            "segment_histogram": {},
            "unique_segments": 0,
            "mean_mm": None,
            "cv": None,
            "entropy_norm": None,
            "pattern_rows": 0,
        }
    inner = result.get("Result") or result
    if "WeldingPattern" in result and "Result" not in result:
        inner = result
    weld_rows = (inner.get("WeldingPattern") or {}).get("WeldingPipe") or []
    cut_rows = (inner.get("CuttingPattern") or {}).get("CuttingPipe") or []
    seg_qty: dict[int, int] = {}
    rows = 0
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
            try:
                qty = int(round(float(pat.get("Number", 1) or 1)))
            except (TypeError, ValueError):
                qty = 1
            rows += 1
            for seg in parts:
                seg_qty[seg] = seg_qty.get(seg, 0) + qty
    for cp in cut_rows:
        if not isinstance(cp, dict):
            continue
        parts = _parts_from_field(cp.get("Part"))
        try:
            qty = int(round(float(cp.get("Number", 1) or 1)))
        except (TypeError, ValueError):
            qty = 1
        rows += 1
        for seg in parts:
            seg_qty[seg] = seg_qty.get(seg, 0) + qty
    stats = _weighted_histogram_stats(seg_qty)
    return {
        "observed": bool(seg_qty),
        "segment_histogram": {str(k): v for k, v in sorted(seg_qty.items())},
        "unique_segments": stats["unique_lengths"],
        "total_segment_slots": stats["total_pieces"],
        "mean_mm": stats["mean_mm"],
        "cv": stats["cv"],
        "entropy_norm": stats["entropy_norm"],
        "spread_ratio": stats["spread_ratio"],
        "pattern_rows": rows,
    }


def _bucket(value: float | int, cuts: tuple[float, ...]) -> int:
    """cuts 升序，返回 0..len(cuts)。"""
    v = float(value)
    for i, c in enumerate(cuts):
        if v <= c:
            return i
    return len(cuts)


def _dispersion_score(unique: int, cv: float | None, ent: float | None) -> int:
    u = unique
    if u <= 1:
        s_u = 0
    elif u <= 3:
        s_u = 1
    elif u <= 8:
        s_u = 2
    elif u <= 16:
        s_u = 3
    else:
        s_u = 4
    cv = cv if cv is not None else 0.0
    ent = ent if ent is not None else 0.0
    if cv < 0.08 and ent < 0.35:
        s_d = 0
    elif cv < 0.18 or ent < 0.55:
        s_d = 1
    elif cv < 0.35 or ent < 0.75:
        s_d = 2
    elif cv < 0.55:
        s_d = 3
    else:
        s_d = 4
    return min(4, round(0.55 * s_u + 0.45 * s_d))


def is_computable_for_solver(
    input_error: str | None,
    geom_class: str | None,
    pipe_pieces: int | None,
) -> tuple[bool, str | None]:
    if input_error:
        return False, "input_parse_error"
    if not pipe_pieces or pipe_pieces <= 0:
        return False, "zero_demand"
    if geom_class == "infeasible_len":
        return False, "stock_shorter_than_demand"
    return True, None


def classify_difficulty(
    *,
    aggregate: Mapping[str, Any],
    per_group: list[Mapping[str, Any]],
    geom_class: str | None,
    input_error: str | None,
    legacy_result: dict[str, Any] | None,
    legacy_status: str | None,
) -> dict[str, Any]:
    """对单案例打难度分（可计算池内有效）。"""
    pipe_pieces = int(aggregate.get("pipe_pieces") or 0)
    stock_pieces = int(aggregate.get("stock_pieces") or 0)
    pipe_types = int(aggregate.get("pipe_types") or 0)
    stock_types = int(aggregate.get("stock_types") or 0)
    groups = int(aggregate.get("groups") or 1)

    computable, block_reason = is_computable_for_solver(
        input_error, geom_class, pipe_pieces
    )

    in_disp = input_pipe_dispersion(list(per_group))
    out_disp = output_segment_dispersion(legacy_result if legacy_status == "success" else None)

    # --- 四维 0–4 分（基于语料分位数校准）---
    s_pipe = _bucket(pipe_pieces, (30, 80, 180, 500))
    s_stock = _bucket(stock_pieces, (20, 77, 200, 800))
    s_in = _dispersion_score(
        in_disp["unique_lengths"],
        in_disp.get("cv"),
        in_disp.get("entropy_norm"),
    )

    if out_disp["observed"]:
        s_out = _dispersion_score(
            out_disp["unique_segments"],
            out_disp.get("cv"),
            out_disp.get("entropy_norm"),
        )
        out_source = "legacy_patterns"
    else:
        # 无老解：用几何 + 输入离散推断「预期输出复杂度」
        if geom_class == "all_stock_fit":
            s_out = 0 if s_in <= 1 else 1
        elif geom_class == "some_stock_fit":
            s_out = min(4, 2 + (1 if s_in >= 2 else 0))
        elif geom_class == "all_stock_short":
            s_out = min(4, 3 + (1 if pipe_types >= 8 else 0))
        else:
            s_out = 2
        out_source = "inferred_no_legacy"

    # --- 算法理解附加分 0–4 ---
    algo = 0
    if geom_class == "some_stock_fit":
        algo += 1
    elif geom_class == "all_stock_short":
        algo += 2
    if pipe_types >= 12:
        algo += 1
    if stock_types >= 4:
        algo += 1
    if groups > 1:
        algo += 1
    algo = min(4, algo)

    composite = s_pipe + s_stock + s_in + s_out + algo  # 0–20
    if composite <= 4:
        tier = TIER_LABELS[0]
        tier_id = 1
    elif composite <= 8:
        tier = TIER_LABELS[1]
        tier_id = 2
    elif composite <= 12:
        tier = TIER_LABELS[2]
        tier_id = 3
    elif composite <= 16:
        tier = TIER_LABELS[3]
        tier_id = 4
    else:
        tier = TIER_LABELS[4]
        tier_id = 5

    reasons: list[str] = []
    if s_pipe >= 3:
        reasons.append(f"需求根数大({pipe_pieces})")
    if s_stock >= 3:
        reasons.append(f"库存根数大({stock_pieces})")
    if s_in >= 3:
        reasons.append(
            f"输入管长离散高({in_disp['unique_lengths']}种,cv={in_disp.get('cv')})"
        )
    if s_out >= 3:
        if out_disp["observed"]:
            reasons.append(
                f"产出段长离散高({out_disp['unique_segments']}段,cv={out_disp.get('cv')})"
            )
        else:
            reasons.append(f"几何约束导致高拼切复杂度({geom_class})")
    if algo >= 2:
        reasons.append(f"几何/规模叠加({geom_class},管型{pipe_types})")

    return {
        "taxonomy_version": DIFFICULTY_TAXONOMY_VERSION,
        "computable": computable,
        "block_reason": block_reason,
        "tier_id": tier_id if computable else None,
        "tier": tier if computable else "不可计算",
        "composite_score": composite if computable else None,
        "axes": {
            "pipe_pieces": {"value": pipe_pieces, "score_0_4": s_pipe},
            "stock_pieces": {"value": stock_pieces, "score_0_4": s_stock},
            "input_length_dispersion": {
                "score_0_4": s_in,
                **{k: in_disp[k] for k in ("unique_lengths", "cv", "entropy_norm", "spread_ratio")},
            },
            "output_segment_dispersion": {
                "score_0_4": s_out,
                "source": out_source,
                "observed": out_disp["observed"],
                "unique_segments": out_disp.get("unique_segments"),
                "cv": out_disp.get("cv"),
                "entropy_norm": out_disp.get("entropy_norm"),
                "spread_ratio": out_disp.get("spread_ratio"),
            },
            "algorithmic_load": {
                "score_0_4": algo,
                "geom_class": geom_class,
                "pipe_types": pipe_types,
                "stock_types": stock_types,
                "groups": groups,
            },
        },
        "reasons": reasons[:6],
    }


def classify_from_metrics_file(metrics_doc: dict[str, Any], legacy_result: Any) -> dict[str, Any]:
    """从落盘 metrics.json + output 对象分类。"""
    agg = metrics_doc.get("aggregate") or {}
    per_group: list[Mapping[str, Any]] = []
    # metrics.json 未存 per_group 时需从 input 侧补；classify 脚本会传完整 input
    met = metrics_doc.get("metrics") or {}
    return classify_difficulty(
        aggregate=agg,
        per_group=per_group,
        geom_class=met.get("geom_class"),
        input_error=metrics_doc.get("input_error"),
        legacy_result=legacy_result if isinstance(legacy_result, dict) else None,
        legacy_status=met.get("legacy_status"),
    )
