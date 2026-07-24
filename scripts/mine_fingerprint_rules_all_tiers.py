"""全档指纹规则挖掘：只读理解，不改求解器。

对 legacy-fingerprints-all 的每案教师列打 motif，按档位/几何汇总。

用法:
  .venv\\Scripts\\python.exe scripts\\mine_fingerprint_rules_all_tiers.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes
from scripts.legacy_corpus_lib import clean_mom_problem, load_legacy_case
from scripts.mine_fingerprint_rules import motif_weld

FP_ALL = ROOT / "data" / "acceptance" / "legacy-fingerprints-all"
CASES = ROOT / "data" / "legacy-cases"
OUT = FP_ALL / "rule_mine_by_tier.json"
DOC = ROOT / "docs" / "legacy-fingerprint-rules-understood.md"


def motif_cut(segs: list[int], stock_len: int, kerfs: list[int]) -> list[str]:
    tags: list[str] = []
    n = len(segs)
    fill = sum(segs)
    slack = stock_len - fill
    if n == 1:
        tags.append("cut_1seg")
        if abs(slack) < 1:
            tags.append("cut_full_bar")
        return tags
    uniq = len(set(segs))
    if uniq == 1:
        tags.append(f"cut_kcopies_k{n}")
    else:
        tags.append(f"cut_mixed_parts{n}")
    if abs(slack) < 1:
        tags.append("cut_exact")
    elif slack <= 50:
        tags.append("cut_nearfill_le50")
    else:
        tags.append("cut_partial")
    # near-full with kerf: fill ≈ stock - kerf
    for k in kerfs:
        if abs(fill - (stock_len - k)) <= 1:
            tags.append(f"cut_kerf_fill_{k}")
            break
    if n >= 5:
        tags.append("cut_many_parts_ge5")
    return tags or ["cut_unknown"]


def kerfs_for(group) -> list[int]:
    blade = int(getattr(group, "blade_margin", 0) or 0)
    out = []
    for x in ([blade] if blade > 0 else []) + [5, 6, 10, 0, 15, 20]:
        if x >= 0 and x not in out:
            out.append(x)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    rows = [
        json.loads(l)
        for l in (FP_ALL / "fingerprints.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    print(f"mine rules n={len(rows)} from {FP_ALL}", flush=True)

    # (tier, geom) -> weld motif counts (weighted by number)
    weld_by: dict[tuple[str, str], Counter] = defaultdict(Counter)
    cut_by: dict[tuple[str, str], Counter] = defaultdict(Counter)
    case_profile: dict[tuple[str, str], Counter] = defaultdict(Counter)
    # pipe-level: one weld template per pipe? 
    templates_per_pipe: dict[str, list[int]] = defaultdict(list)
    anchor_stock: Counter = Counter()  # for long 2seg: is max seg == s_max?
    sample_errs = 0

    for n, r in enumerate(rows, 1):
        idx = int(r["idx"])
        tier = str(r.get("tier") or "?")
        geom = str(r.get("geom_class") or "?")
        key = (tier, geom)
        case_dir = FP_ALL / "cases" / r["dir"]
        tc_path = case_dir / "teacher_columns.json"
        if not tc_path.exists():
            sample_errs += 1
            continue
        tc = json.loads(tc_path.read_text(encoding="utf-8"))

        try:
            case = load_legacy_case(CASES, idx)
            g = merge_equivalent_pipes(
                parse_problem(clean_mom_problem(case["problem"])).groups[0]
            )
            stocks = sorted({int(s.length) for s in g.stocks})
            kerfs = kerfs_for(g)
            s_max = max(stocks) if stocks else 0
        except Exception:
            sample_errs += 1
            continue

        # case-level profile
        j = int(r.get("joints_gi") or 0)
        if j == 0:
            case_profile[key]["case_zero_joint"] += 1
        else:
            case_profile[key]["case_has_weld"] += 1
        case_profile[key]["cases"] += 1

        # weld motifs
        by_pipe: dict[str, set[tuple[int, ...]]] = defaultdict(set)
        for w in tc.get("weld_columns") or []:
            segs = list(w.get("segs") or [])
            if not segs:
                continue
            qty = int(w.get("number") or 1)
            pipe_len = int(w.get("pipe_len") or sum(segs))
            by_pipe[str(w.get("pipe_key") or pipe_len)].add(tuple(segs))
            for tag in motif_weld(segs, stocks, kerfs):
                weld_by[key][tag] += qty
            # long vs short
            if pipe_len > s_max:
                weld_by[key]["pipe_long"] += qty
                mx = max(segs)
                if mx == s_max:
                    anchor_stock["long_max_is_smax"] += qty
                elif mx in stocks:
                    anchor_stock["long_max_other_stock"] += qty
                else:
                    anchor_stock["long_max_not_stock"] += qty
            else:
                weld_by[key]["pipe_short"] += qty

        for pk, pats in by_pipe.items():
            templates_per_pipe[tier].append(len(pats))

        # cut motifs
        for c in tc.get("cut_columns") or []:
            segs = list(c.get("segs") or [])
            if not segs:
                continue
            qty = int(c.get("number") or 1)
            Sp = int(c.get("stock_len") or 0)
            for tag in motif_cut(segs, Sp, kerfs):
                cut_by[key][tag] += qty

        if n % 200 == 0:
            print(f"  [{n}/{len(rows)}]", flush=True)

    def top(counter: Counter, k: int = 15):
        return counter.most_common(k)

    # build report structure
    tiers = sorted({t for t, _ in case_profile.keys()})
    report = {
        "n_cases": len(rows),
        "load_errors": sample_errs,
        "long_pipe_anchor": dict(anchor_stock),
        "by_tier_geom": {},
        "templates_per_pipe_mean_by_tier": {},
    }
    for tier in tiers:
        xs = templates_per_pipe.get(tier) or []
        report["templates_per_pipe_mean_by_tier"][tier] = (
            sum(xs) / len(xs) if xs else None
        )

    for key in sorted(case_profile.keys()):
        tier, geom = key
        cp = case_profile[key]
        report["by_tier_geom"][f"{tier}|{geom}"] = {
            "cases": cp["cases"],
            "zero_joint_cases": cp["case_zero_joint"],
            "weld_cases": cp["case_has_weld"],
            "weld_motifs_top": top(weld_by[key], 20),
            "cut_motifs_top": top(cut_by[key], 20),
        }

    # also roll up by tier only
    weld_tier: dict[str, Counter] = defaultdict(Counter)
    cut_tier: dict[str, Counter] = defaultdict(Counter)
    case_tier: dict[str, Counter] = defaultdict(Counter)
    for (tier, geom), cp in case_profile.items():
        for k, v in cp.items():
            case_tier[tier][k] += v
        for k, v in weld_by[(tier, geom)].items():
            weld_tier[tier][k] += v
        for k, v in cut_by[(tier, geom)].items():
            cut_tier[tier][k] += v

    report["by_tier"] = {
        t: {
            "cases": case_tier[t]["cases"],
            "zero_joint_cases": case_tier[t]["case_zero_joint"],
            "weld_motifs_top": top(weld_tier[t], 25),
            "cut_motifs_top": top(cut_tier[t], 25),
        }
        for t in tiers
    }

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)

    # markdown understanding doc
    lines = [
        "# 老软件指纹规则理解（全档，先读懂再改引擎）",
        "",
        "> 数据：`legacy-fingerprints-all`（1586 success）+ 教师列 motif 挖掘。",
        "> 本文件只描述「老软件解里反复出现什么」，**不写实现补丁**。",
        "",
        "## 0. 一句话",
        "",
        "老软件解看起来像：**先定每根成品管怎么拼（焊列），再定母料怎么切出这些段（切列），"
        "全局选少数切法并大量复用**。难度升高时，焊段变碎、Σ 变大、0 焊口消失——"
        "不是换了一套路由，而是同一套「拼+切」解变得更复杂。",
        "",
        "## 1. 分档：在解什么题",
        "",
        "| 档位 | 题数 | 0焊口题 | 含义 |",
        "|------|-----:|--------:|------|",
    ]
    for t in tiers:
        b = report["by_tier"][t]
        z = b["zero_joint_cases"]
        n = b["cases"]
        lines.append(
            f"| {t} | {n} | {z} ({100*z/n:.0f}%) | "
            + (
                "以纯切为主"
                if z / n > 0.7
                else ("几乎必焊" if z / n < 0.1 else "焊/切混合")
            )
            + " |"
        )

    lines += [
        "",
        "### 几何约束（硬规则）",
        "",
        "- `all_stock_short`（成品长于所有母料）：**100% 有焊接**，没有 0 焊口解。",
        "- `all_stock_fit`：可以 0 焊，也可以焊（全档约 58% 为 0 焊；D1 更高）。",
        "- `some_stock_fit`：多数仍焊。",
        "",
        "## 2. 焊接规则（从教师焊列统计）",
        "",
        "### 2.1 长管（成品 > S_max）",
        "",
        f"锚点统计（按焊件件数加权）：`{report['long_pipe_anchor']}`",
        "",
        "- **绝大多数**长管焊法的最长段 = `S_max`（整根最长母料当锚段）。",
        "- 少数用其它母料长度当最长段；极少数最长段不是任何库存规格。",
        "- 典型形状：`(R, S_max)` 两段；或把 `R` 再拆成 2～3 段 → 3/4 段焊。",
        "",
        "### 2.2 短管（成品 ≤ S_max）",
        "",
        "- **1 段**（整管当一段，等于纯切用料）在各档都很多。",
        "- 需要吃料头/拼利用率时出现 **2 段**：常见 motif 为",
        "  - `2seg_stock_gap`：用 `母料 - kerf - 管长` 一类缺口当一段；",
        "  - `2seg_pack_L_leftover` / `2seg_kkerf_L`：把管长在母料上整段码放后的余料当一段；",
        "  - `2seg_no_full_stock`：两段都不是整根母料规格。",
        "- 更碎的短管 3 段相对少，但 D3+ 会增多。",
        "",
        "### 2.3 每管型用几种拼法？",
        "",
        "各档「同一管键下不同拼法条数」均值：",
        "",
    ]
    for t, v in report["templates_per_pipe_mean_by_tier"].items():
        lines.append(f"- {t}: {v:.2f}" if v is not None else f"- {t}: n/a")

    lines += [
        "",
        "多数情况接近 **每管型一种主拼法**；D2 多拼法比例更高（与早期 fingerprint 的 multi_pattern 一致）。",
        "",
        "### 2.4 分档焊 motif 榜（件数加权 Top）",
        "",
    ]
    for t in tiers:
        lines.append(f"#### {t}")
        lines.append("")
        for tag, cnt in report["by_tier"][t]["weld_motifs_top"][:12]:
            lines.append(f"- `{tag}`: {cnt}")
        lines.append("")

    lines += [
        "## 3. 切割规则（从教师切列统计）",
        "",
        "共同特征：",
        "",
        "1. **少类型、高复用**：档位越高，切/棒比越低（少数切法开很多张）。",
        "2. **k 拷贝**很常见：同一段规格在一根母料上切多件（`cut_kcopies_k*`）。",
        "3. **近满切**常见：`cut_nearfill_le50` / `cut_kerf_fill_*`（留刀缝或很小余料）。",
        "4. **混合多件**在难档增多：`cut_mixed_parts*`、`cut_many_parts_ge5`。",
        "5. D1 大量 `cut_1seg` / 近满或整棒——就是纯切。",
        "",
        "### 分档切 motif 榜",
        "",
    ]
    for t in tiers:
        lines.append(f"#### {t}")
        lines.append("")
        for tag, cnt in report["by_tier"][t]["cut_motifs_top"][:12]:
            lines.append(f"- `{tag}`: {cnt}")
        lines.append("")

    lines += [
        "## 4. 拼起来：老软件在遵循的「工作流」（推断）",
        "",
        "```",
        "对每类成品管:",
        "  if 管长 <= 某根母料:",
        "      优先 1 段（整管）",
        "      若要抬利用率 / 吃余料 → 用 gap / pack leftover / k·kerf 拆成 2 段",
        "  else:  # 长于所有母料",
        "      必焊；优先 (余段 R, 最长母料 S_max)",
        "      若 R 仍难切满 → 按母料 leftover / k·kerf / 半料 再拆 R",
        "",
        "全局:",
        "  枚举/生成能生产这些段的切法列",
        "  用集合覆盖式优化：选很少的切法类型，决定各开几张",
        "  目标侧表现为：焊口优先，其次利用率（语料观感）",
        "```",
        "",
        "没有证据表明存在「D1 走纯切引擎、D5 走另一引擎」的显式路由；",
        "更像是 **同一套拼+切列 + 全局优化**，随实例变难自动用上更碎的列。",
        "",
        "## 5. 对我们造引擎的含义（只理解，不实施）",
        "",
        "| 场景 | 老软件在做什么 | 我们若只学 D2 失败簇会怎样 |",
        "|------|----------------|---------------------------|",
        "| D1 纯切 | Σ≈1，切法很少，几乎不焊 | 硬造焊缝/扩 Σ → 污染、util 差、甚至无解 |",
        "| D2 混合 | 1 段 + (R,S) + leftover | 大致对口，但仍要干净 Σ |",
        "| D4/D5 | 大 Σ、多段焊、多混合切 | 规则不够会缺列；乱扩又会超时 |",
        "",
        "**理解结论**：规则要按「几何 + 是否需要焊」分层描述，而不是一套焊缝启发式打天下。",
        "",
        "## 6. 机器可读明细",
        "",
        f"- `{OUT.relative_to(ROOT).as_posix()}`",
        "",
    ]

    DOC.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {DOC}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
