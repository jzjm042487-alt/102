"""Compare the 1521 round-5 plans with legacy plans. Does not solve or change the solver.

Metrics follow ``legacy_lex_metrics`` from commit a7e7c43 (0-weld sequences
count as a weld type; cut types are stock length plus the sorted part
multiset; used length is GeneralInfo). A single WeldingPipe / Pattern object
is treated as a one-element list so those rows are not dropped.

Pipe identity uses the verifier key, then ``figure_number|jlxh|cube_no``.
A weld row that omits ``jlxh`` matches ``figure_number|cube_no`` when that
pair identifies one pipe. Compliance is ``verify_solution`` on the stable
payload after that stamp. Trim-loss and unused-stock label mismatches are
not treated as an illegal plan.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.engine_stable import prepare_stable_payload
from app.verifier import _extract_problem_contexts, _key_scalar, _pipe_key, verify_solution

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
FULL = ROOT / "docs/stable-round5-full.csv"
OUT = ROOT / "docs/stable-round5-vs-legacy.csv"
LEXCG_COMMIT = "a7e7c43"

LABEL_CODES = {
    "TRIM_LOSS_MISMATCH",
    "UNUSED_STOCK_MISMATCH",
    "KERF_LOSS_MISMATCH",
    "REMAINDER_MISMATCH",
    "USED_LENGTH_MISMATCH",
    "REMNANT_DETAIL_MISMATCH",
    "REMNANT_REUSABLE_FLAG_MISMATCH",
    "JOINT_COUNT_MISMATCH",
    "WELD_POSITION_MISMATCH",
    "REPORTED_PIPE_LENGTH_MISMATCH",
}
WELD_ZONE = re.compile(r"焊口位置 (\d+) 落入禁焊区 \[(\d+), (\d+)\]")
PIPE_DELTA = re.compile(r"拼法总长 (\d+) 与管段长度 (\d+)")

COLUMNS = [
    "case_id",
    "record_index",
    "集合",
    "策略来源",
    "用料",
    "焊口",
    "拼法种类",
    "切法种类",
    "母料根数",
    "老软件用料",
    "老软件焊口",
    "老软件拼法种类",
    "老软件切法种类",
    "老软件母料根数",
    "用料差",
    "焊口差",
    "拼法差",
    "切法差",
    "母料根数差",
    "用料对照",
    "焊口对照",
    "拼法对照",
    "切法对照",
    "母料根数对照",
    "词典序对照",
    "老软件合规",
    "不合规代码",
    "仅标签差异",
    "禁焊区仅可外移",
    "管长差毫米",
    "身份未对上",
    "母料根数可复现",
    "baseline覆盖",
    "baseline词典序",
    "lexcg覆盖",
    "lexcg词典序",
]


def _parts(value: Any) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        tokens = value
    else:
        tokens = str(value or "").replace("+", " ").split()
    out: list[int] = []
    for item in tokens:
        try:
            out.append(int(round(float(item))))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _pipe_rows(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        raw = node.get("WeldingPipe")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            return [raw]
    return []


def _cut_rows(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        raw = node.get("CuttingPipe")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            return [raw]
    return []


def legacy_lex_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Same four counts as lexcg, plus mother-bar quantity."""

    gi = result.get("GeneralInfo") or {}
    inner = result.get("Result") or {}
    weld_sigs: set[tuple[int, ...]] = set()
    joints = 0
    for pipe in _pipe_rows(inner.get("WeldingPattern")):
        patterns = pipe.get("Pattern") or []
        if isinstance(patterns, dict):
            patterns = [patterns]
        for pattern in patterns:
            if not isinstance(pattern, dict):
                continue
            parts = _parts(pattern.get("Part"))
            if not parts:
                continue
            try:
                quantity = int(round(float(pattern.get("Number", 1) or 1)))
            except (TypeError, ValueError):
                quantity = 1
            weld_sigs.add(parts)
            joints += max(0, len(parts) - 1) * quantity
    cut_sigs: set[tuple[int, tuple[int, ...]]] = set()
    bars = 0
    for row in _cut_rows(inner.get("CuttingPattern")):
        try:
            stock = int(round(float(row.get("Length", 0) or 0)))
        except (TypeError, ValueError):
            stock = 0
        parts = tuple(sorted(_parts(row.get("Part"))))
        if stock <= 0 or not parts:
            continue
        cut_sigs.add((stock, parts))
        try:
            bars += int(round(float(row.get("Number", 0) or 0)))
        except (TypeError, ValueError):
            pass
    try:
        used = int(round(float(gi.get("StockLength_Of_SingleMaterialSpecification") or 0)))
    except (TypeError, ValueError):
        used = None
    return {
        "used_stock_length": used,
        "welding_joint_quantity": joints,
        "welding_pattern_type_quantity": len(weld_sigs),
        "cutting_pattern_type_quantity": len(cut_sigs),
        "used_stock_quantity": bars,
    }


def _fig_key(row: dict) -> str:
    figure = row.get("figure_number", row.get("FigureNumber"))
    jlxh = row.get("jlxh", row.get("Jlxh"))
    cube = row.get("cube_no", row.get("InPipeNumber", row.get("in_pipe_number")))
    return "|".join((_key_scalar(figure), _key_scalar(jlxh), _key_scalar(cube)))


def _fig_cube(row: dict) -> tuple[str, str]:
    figure = row.get("figure_number", row.get("FigureNumber"))
    cube = row.get("cube_no", row.get("InPipeNumber", row.get("in_pipe_number")))
    return _key_scalar(figure), _key_scalar(cube)


def _group_pipes(prepared: dict) -> list[dict]:
    pipes: list[dict] = []
    for context in _extract_problem_contexts(prepared, []):
        group = context["group"]
        raw = None
        for name in ("Pipe", "pipe", "pipes"):
            if isinstance(group.get(name), list):
                raw = group[name]
                break
        for pipe in raw or []:
            if isinstance(pipe, dict):
                pipes.append(pipe)
    return pipes


def _normalize_solution(result: dict) -> dict:
    solution = deepcopy(result)
    solution.pop("GeneralInfo", None)
    inner = solution.get("Result")
    if not isinstance(inner, dict):
        return solution
    weld = inner.get("WeldingPattern")
    if isinstance(weld, list):
        inner["WeldingPattern"] = {"WeldingPipe": weld}
        weld = inner["WeldingPattern"]
    if isinstance(weld, dict):
        pipes = weld.get("WeldingPipe")
        if isinstance(pipes, dict):
            pipes = [pipes]
            weld["WeldingPipe"] = pipes
        if isinstance(pipes, list):
            for row in pipes:
                if isinstance(row, dict) and isinstance(row.get("Pattern"), dict):
                    row["Pattern"] = [row["Pattern"]]
    cut = inner.get("CuttingPattern")
    if isinstance(cut, dict) and isinstance(cut.get("CuttingPipe"), dict):
        cut["CuttingPipe"] = [cut["CuttingPipe"]]
    if not isinstance(inner.get("UnusedMaterials"), list):
        inner.pop("UnusedMaterials", None)
    return solution


def _stamp(prepared: dict, solution: dict) -> int:
    pipes = _group_pipes(prepared)
    by_solver = {_pipe_key(pipe): pipe for pipe in pipes}
    by_figure: dict[str, list[str]] = {}
    by_cube: dict[tuple[str, str], list[str]] = {}
    for pipe in pipes:
        key = _pipe_key(pipe)
        by_figure.setdefault(_fig_key(pipe), []).append(key)
        by_cube.setdefault(_fig_cube(pipe), []).append(key)
    missing = 0
    weld = ((solution.get("Result") or {}).get("WeldingPattern") or {}).get("WeldingPipe") or []
    for row in weld:
        if not isinstance(row, dict):
            continue
        if _pipe_key(row) in by_solver:
            continue
        hits = list(dict.fromkeys(by_figure.get(_fig_key(row), [])))
        if len(hits) == 1:
            row["pipe_id"] = hits[0]
            continue
        jlxh = _key_scalar(row.get("jlxh", row.get("Jlxh")))
        cube_hits = list(dict.fromkeys(by_cube.get(_fig_cube(row), [])))
        if jlxh == "" and len(cube_hits) == 1:
            row["pipe_id"] = cube_hits[0]
            continue
        missing += 1
    return missing


def _nudge_only(issues: list[dict]) -> bool:
    zones = [item for item in issues if item["code"] == "WELD_IN_UNWELDABLE_AREA"]
    if not zones:
        return False
    for item in zones:
        match = WELD_ZONE.search(item["message"])
        if match is None:
            return False
        weld, start, end = (int(match.group(index)) for index in (1, 2, 3))
        # A closed zone rejects its endpoints. The stable rule allows a 1 mm
        # outward move: start -> start-1, and end or end-1 -> end+1.
        if weld not in {start, end, end - 1}:
            return False
    return True


def _pipe_deltas(issues: list[dict]) -> list[int]:
    deltas = []
    for item in issues:
        if item["code"] != "PIPE_LENGTH_MISMATCH":
            continue
        match = PIPE_DELTA.search(item["message"])
        if match:
            deltas.append(int(match.group(1)) - int(match.group(2)))
    return deltas


def _side(ours: int | None, legacy: int | None) -> str:
    if ours is None or legacy is None:
        return ""
    if ours < legacy:
        return "胜"
    if ours > legacy:
        return "负"
    return "平"


def _lex(ours: tuple[int, ...], legacy: tuple[int, ...]) -> str:
    if ours == legacy:
        return "平"
    return "胜" if ours < legacy else "负"


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _load_lexcg() -> dict[str, dict[str, dict[str, str]]]:
    text = subprocess.check_output(
        ["git", "show", f"{LEXCG_COMMIT}:docs/lexcg-corpus-results.csv"],
        cwd=ROOT,
        text=True,
    )
    grouped: dict[str, dict[str, dict[str, str]]] = {}
    for row in csv.DictReader(text.splitlines()):
        grouped.setdefault(row["case_id"], {})[row["engine"]] = row
    return grouped


def _blade_missing(problem: dict) -> bool:
    if not isinstance(problem, dict):
        return True
    nest = problem.get("NestParam")
    if isinstance(nest, dict) and nest.get("BladeMargin") not in (None, ""):
        return False
    if problem.get("BladeMargin") not in (None, ""):
        return False
    data = problem.get("data")
    if isinstance(data, list):
        return all(_blade_missing(item) if isinstance(item, dict) else True for item in data)
    return True


def _summarize(rows: list[dict[str, str]]) -> dict[str, Any]:
    def take(pred) -> list[dict[str, str]]:
        return [row for row in rows if pred(row)]

    comparable = take(lambda row: row["词典序对照"] in {"胜", "平", "负"})

    def tally(subset: list[dict[str, str]], column: str) -> dict[str, int]:
        counts = Counter(row[column] for row in subset if row[column])
        return {name: counts.get(name, 0) for name in ("胜", "平", "负")}

    def total(subset: list[dict[str, str]], column: str) -> int | None:
        values = [row[column] for row in subset if row[column] != ""]
        if len(values) != len(subset):
            return None
        return sum(int(value) for value in values)

    def block(subset: list[dict[str, str]]) -> dict[str, Any]:
        metrics = {
            "用料": ("用料对照", "用料差"),
            "焊口": ("焊口对照", "焊口差"),
            "拼法种类": ("拼法对照", "拼法差"),
            "切法种类": ("切法对照", "切法差"),
            "母料根数": ("母料根数对照", "母料根数差"),
        }
        out: dict[str, Any] = {"条数": len(subset), "词典序": tally(subset, "词典序对照")}
        for name, (side, delta) in metrics.items():
            out[name] = {"对照": tally(subset, side), "总量差": total(subset, delta)}
        return out

    compliant = take(lambda row: row["老软件合规"] == "是" and row["词典序对照"] in {"胜", "平", "负"})
    losses = [row for row in comparable if row["词典序对照"] == "负"]
    losses.sort(
        key=lambda row: (
            int(row["用料差"]),
            int(row["焊口差"]),
            int(row["拼法差"]),
            int(row["切法差"]),
        ),
        reverse=True,
    )
    compliant_losses = [row for row in losses if row["老软件合规"] == "是"]
    codes = Counter()
    for row in rows:
        if row["老软件合规"] != "否":
            continue
        for code in row["不合规代码"].split(";"):
            if code:
                codes[code] += 1
    engines: dict[str, Any] = {}
    for engine, covered_col, versus_col in (
        ("baseline", "baseline覆盖", "baseline词典序"),
        ("lexcg", "lexcg覆盖", "lexcg词典序"),
    ):
        covered = take(lambda row, col=covered_col: row[col] == "是")
        engines[engine] = {
            "覆盖": len(covered),
            "词典序": tally(covered, versus_col),
            "不可比": sum(1 for row in rows if row[covered_col] != "是"),
        }
    return {
        "条数": len(rows),
        "全部": block(comparable),
        "几何合规": block(compliant),
        "不合规条数": sum(row["老软件合规"] == "否" for row in rows),
        "仅可外移条数": sum(row["禁焊区仅可外移"] == "是" for row in rows),
        "不合规代码": dict(codes),
        "劣化前10": [
            {
                "record_index": row["record_index"],
                "case_id": row["case_id"],
                "集合": row["集合"],
                "用料差": row["用料差"],
                "焊口差": row["焊口差"],
                "拼法差": row["拼法差"],
                "切法差": row["切法差"],
                "老软件合规": row["老软件合规"],
                "不合规代码": row["不合规代码"],
            }
            for row in losses[:10]
        ],
        "合规劣化前10": [
            {
                "record_index": row["record_index"],
                "case_id": row["case_id"],
                "用料差": row["用料差"],
                "焊口差": row["焊口差"],
                "拼法差": row["拼法差"],
                "切法差": row["切法差"],
            }
            for row in compliant_losses[:10]
        ],
        "历史引擎": engines,
        "缺省锯缝": sum(row.get("_blade_missing") == "是" for row in rows),
    }


def evaluate(bars_path: Path | None = None) -> list[dict[str, str]]:
    feas = [
        row
        for row in csv.DictReader(FULL.open(encoding="utf-8"))
        if row["是否可行"] == "是" and row["校验通过"] == "是"
    ]
    records = json.loads(RECORDS.read_text(encoding="utf-8"))["RECORDS"]
    historical = _load_lexcg()
    bars: dict[str, dict[str, str]] = {}
    if bars_path is not None and bars_path.exists():
        bars = {row["case_id"]: row for row in csv.DictReader(bars_path.open(encoding="utf-8"))}
    written: list[dict[str, str]] = []
    legacy_mismatch = 0
    for row in feas:
        index = int(row["record_index"])
        record = records[index]
        raw_problem = record["MOMPROBLEMJSON"]
        problem = json.loads(raw_problem) if isinstance(raw_problem, str) else raw_problem
        raw_result = record["MOMRESULTJSON"]
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        legacy = legacy_lex_metrics(result if isinstance(result, dict) else {})
        prepared = prepare_stable_payload(problem)
        solution = _normalize_solution(result if isinstance(result, dict) else {})
        missing = _stamp(prepared, solution)
        report = verify_solution(prepared, solution)
        issues = report["issues"]
        found = {item["code"] for item in issues}
        geometric = found - LABEL_CODES
        nudge = bool(geometric) and geometric <= {"WELD_IN_UNWELDABLE_AREA"} and _nudge_only(issues)
        if nudge:
            geometric = set()
        deltas = [item for item in _pipe_deltas(issues) if abs(item) < 1_000_000]
        ours = (
            _int(row["用料"]),
            _int(row["焊口"]),
            _int(row["拼法种类"]),
            _int(row["切法种类"]),
        )
        theirs = (
            legacy["used_stock_length"],
            legacy["welding_joint_quantity"],
            legacy["welding_pattern_type_quantity"],
            legacy["cutting_pattern_type_quantity"],
        )
        complete = None not in ours and None not in theirs
        our_bars = None
        reproduced = ""
        cached = bars.get(row["case_id"])
        if cached and cached.get("四项一致") == "是":
            our_bars = _int(cached.get("母料根数"))
            reproduced = "是"
        elif cached:
            reproduced = "否"
        legacy_bars = legacy["used_stock_quantity"]
        out = {column: "" for column in COLUMNS}
        out.update(
            {
                "case_id": row["case_id"],
                "record_index": row["record_index"],
                "集合": row["集合"],
                "策略来源": row["策略来源"],
                "用料": "" if ours[0] is None else str(ours[0]),
                "焊口": "" if ours[1] is None else str(ours[1]),
                "拼法种类": "" if ours[2] is None else str(ours[2]),
                "切法种类": "" if ours[3] is None else str(ours[3]),
                "母料根数": "" if our_bars is None else str(our_bars),
                "老软件用料": "" if theirs[0] is None else str(theirs[0]),
                "老软件焊口": str(theirs[1]),
                "老软件拼法种类": str(theirs[2]),
                "老软件切法种类": str(theirs[3]),
                "老软件母料根数": str(legacy_bars),
                "用料对照": _side(ours[0], theirs[0]) if complete else "",
                "焊口对照": _side(ours[1], theirs[1]) if complete else "",
                "拼法对照": _side(ours[2], theirs[2]) if complete else "",
                "切法对照": _side(ours[3], theirs[3]) if complete else "",
                "母料根数对照": _side(our_bars, legacy_bars),
                "词典序对照": _lex(ours, theirs) if complete else "不可比",
                "老软件合规": "否" if geometric else "是",
                "不合规代码": ";".join(sorted(geometric)),
                "仅标签差异": ";".join(sorted(found & LABEL_CODES)),
                "禁焊区仅可外移": "是" if nudge else "否",
                "管长差毫米": "" if not deltas else str(max(deltas, key=abs)),
                "身份未对上": str(missing),
                "母料根数可复现": reproduced,
            }
        )
        if complete:
            out["用料差"] = str(ours[0] - theirs[0])
            out["焊口差"] = str(ours[1] - theirs[1])
            out["拼法差"] = str(ours[2] - theirs[2])
            out["切法差"] = str(ours[3] - theirs[3])
        if our_bars is not None:
            out["母料根数差"] = str(our_bars - legacy_bars)
        hist = historical.get(row["case_id"], {})
        for engine, covered_col, versus_col in (
            ("baseline", "baseline覆盖", "baseline词典序"),
            ("lexcg", "lexcg覆盖", "lexcg词典序"),
        ):
            previous = hist.get(engine)
            if previous is None:
                out[covered_col] = "不在1589"
                continue
            if previous.get("failure_category") == "solved_verified":
                out[covered_col] = "是"
                label = {"better": "胜", "tie": "平", "worse": "负"}.get(previous.get("versus_legacy", ""), "")
                out[versus_col] = label
                legacy_key = (
                    _int(previous.get("legacy_used_stock_length")),
                    _int(previous.get("legacy_welding_joint_quantity")),
                    _int(previous.get("legacy_welding_pattern_type_quantity")),
                    _int(previous.get("legacy_cutting_pattern_type_quantity")),
                )
                if legacy_key != theirs:
                    legacy_mismatch += 1
            else:
                out[covered_col] = "否"
        out["_blade_missing"] = "是" if _blade_missing(problem) else "否"
        written.append(out)
    written.sort(key=lambda item: int(item["record_index"]))
    summary = _summarize(written)
    summary["老软件四项与lexcg列不一致的历史行"] = legacy_mismatch
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(written)
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=Path, default=None)
    args = parser.parse_args()
    evaluate(args.bars)


if __name__ == "__main__":
    main()
