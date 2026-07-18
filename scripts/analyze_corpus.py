"""语料画像: 扫描 3177 案例库, 按'纯切可达性'给每组分类, 对齐老软件焊口/利用率。

背景(见 docs/V3_HANDOFF.md): 分类不能只看几何。即使 min库存>=max管长(几何可纯切),
老软件仍会为顶利用率而焊接。真正判据是——纯切能否达标目标利用率。

分类轴(几何):
  infeasible_len : 料总长 < 需求总长, 必不可行。
  all_stock_short: 全部库存 < max管长, 必须切分+焊接(L12/L13 属此)。
  some_stock_fit : 部分库存 >= max管长, 纯切几何可行但受限。
  all_stock_fit  : 最短库存 >= max管长, 纯切几何完全自由。

对每类再看老软件解的焊口分布 -> 验证'几何可切 != 无焊口'。

用法:
  python scripts/analyze_corpus.py            # 全量扫描, 打印画像
  python scripts/analyze_corpus.py --md       # 额外写 docs/corpus-profile.md
  python scripts/analyze_corpus.py --data <path>  # 指定数据文件
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from scripts._exp_colgen import merge_equivalent_pipes

DEFAULT_DATA = "backend/data/samples/software-io/software-records-3177.json"


def geom_class(group):
    """纯几何分类(零成本, 不解模型)。"""
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


def legacy_metrics(record):
    """从 MOMRESULTJSON.GeneralInfo 取老软件焊口/利用率; 失败返回 None。"""
    try:
        res = json.loads(record["MOMRESULTJSON"])
    except (TypeError, ValueError):
        return None
    gi = res.get("GeneralInfo", {})
    if not str(gi.get("Result", "")).startswith("Success"):
        return None
    try:
        joints = int(float(gi.get("WeldingJointQuantity", 0)))
        util = float(gi.get("UtilRate", 0))
    except (TypeError, ValueError):
        return None
    return {"joints": joints, "util": util}


def analyze(data_path):
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    records = data["RECORDS"] if isinstance(data, dict) and "RECORDS" in data else data
    n_total = len(records)
    n_parse_ok = n_parse_err = 0
    parse_errs = defaultdict(int)
    # cat -> {n, n_legacy, n_legacy_joint, sum_joints, sum_util}
    cat = defaultdict(lambda: {"n": 0, "n_legacy": 0, "n_joint": 0,
                               "sum_joints": 0, "sum_util": 0.0})
    for rec in records:
        try:
            prob = parse_problem(json.loads(rec["MOMPROBLEMJSON"]))
        except Exception as exc:  # noqa: BLE001 - 数据质量画像需吃掉解析异常
            n_parse_err += 1
            parse_errs[str(exc)[:48]] += 1
            continue
        n_parse_ok += 1
        lg = legacy_metrics(rec)
        for group in prob.groups:
            group = merge_equivalent_pipes(group)
            kind = geom_class(group)
            bucket = cat[kind]
            bucket["n"] += 1
            if lg is not None:
                bucket["n_legacy"] += 1
                bucket["sum_util"] += lg["util"]
                if lg["joints"] > 0:
                    bucket["n_joint"] += 1
                    bucket["sum_joints"] += lg["joints"]
    return {
        "n_total": n_total,
        "n_parse_ok": n_parse_ok,
        "n_parse_err": n_parse_err,
        "parse_errs": dict(sorted(parse_errs.items(), key=lambda kv: -kv[1])),
        "cat": {k: dict(v) for k, v in cat.items()},
    }


ORDER = ["all_stock_fit", "some_stock_fit", "all_stock_short", "infeasible_len"]
LABEL = {
    "all_stock_fit": "最短库存>=max管长 (几何全自由纯切)",
    "some_stock_fit": "部分库存>=max管长 (受限纯切)",
    "all_stock_short": "全库存<max管长 (必须切分+焊接)",
    "infeasible_len": "料长<需求 (必不可行)",
}


def _rows(prof):
    rows = []
    for kind in ORDER:
        b = prof["cat"].get(kind)
        if not b:
            continue
        n_legacy = b["n_legacy"] or 0
        n_joint = b["n_joint"]
        avg_j = b["sum_joints"] / n_joint if n_joint else 0.0
        avg_u = b["sum_util"] / n_legacy if n_legacy else 0.0
        pct_j = 100 * n_joint / n_legacy if n_legacy else 0.0
        rows.append((kind, b["n"], n_legacy, n_joint, pct_j, avg_j, avg_u))
    return rows


def print_profile(prof):
    print(f"总记录 {prof['n_total']}  解析成功 {prof['n_parse_ok']}  "
          f"解析失败 {prof['n_parse_err']}")
    print()
    print(f"{'几何分类':<32}{'组数':>6}{'有老解':>7}{'带焊口':>7}"
          f"{'焊口占比':>9}{'均焊口':>8}{'均利用率':>10}")
    print("-" * 84)
    for kind, n, n_lg, n_j, pct_j, avg_j, avg_u in _rows(prof):
        print(f"{LABEL[kind]:<32}{n:>6}{n_lg:>7}{n_j:>7}"
              f"{pct_j:>8.1f}%{avg_j:>8.1f}{avg_u:>10.4f}")
    print()
    print("关键结论: '几何可纯切'的组仍有大量带焊口案例 -> 老软件靠焊接顶利用率,"
          " 故分类需按'纯切可达性'而非纯几何。")
    if prof["parse_errs"]:
        print("\n解析失败 Top(数据质量):")
        for msg, cnt in list(prof["parse_errs"].items())[:8]:
            print(f"  {cnt:>4}  {msg}")


def write_md(prof, path):
    rows = _rows(prof)
    lines = [
        "# 语料画像 (3177 案例库)",
        "",
        "> 由 `scripts/analyze_corpus.py` 自动生成, 勿手改。",
        "",
        f"- 总记录: **{prof['n_total']}**",
        f"- 解析成功: **{prof['n_parse_ok']}**",
        f"- 解析失败(数据质量): **{prof['n_parse_err']}**",
        "",
        "## 几何分类分布 + 老软件焊口画像",
        "",
        "| 几何分类 | 组数 | 有老解 | 带焊口 | 焊口占比 | 均焊口 | 均利用率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for kind, n, n_lg, n_j, pct_j, avg_j, avg_u in rows:
        lines.append(
            f"| {LABEL[kind]} | {n} | {n_lg} | {n_j} | "
            f"{pct_j:.1f}% | {avg_j:.1f} | {avg_u:.4f} |"
        )
    lines += [
        "",
        "## 关键结论",
        "",
        "即便几何上可纯切(最短库存 ≥ 最长管长), 老软件仍在大量案例里焊接——",
        "因为纯切利用率达不到目标(常见 99.25%), 需靠焊接吃掉料头把利用率顶上去。",
        "因此分类器不能只看几何, 真正判据是 **纯切能否达标目标利用率**:",
        "",
        "- 纯切可达标 → 纯切快路(0 焊口, 跳过焊接建模)。",
        "- 纯切不达标 / 几何不可纯切 → 焊接 arc-flow(coarse-to-fine 网格)。",
        "",
        "## 数据质量",
        "",
        f"解析失败 {prof['n_parse_err']} 条, 主要为 pipe_demand=0、重复管型、空行等录入问题。",
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写 {path}")


def main():
    argv = sys.argv[1:]
    data_path = DEFAULT_DATA
    if "--data" in argv:
        data_path = argv[argv.index("--data") + 1]
    prof = analyze(data_path)
    print_profile(prof)
    if "--md" in argv:
        write_md(prof, "docs/corpus-profile.md")


if __name__ == "__main__":
    main()
