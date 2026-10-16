"""Measure two material lower bounds on the frozen hard 50.

LB1 fixes the segment lengths and quantities of the 60-minute weld plan and
reopens the cuts. LB2 is the inventory volume bound. Neither path changes the
solver. The blind split is refused. Each case gets 120 seconds for both
bounds, including the time spent building the model.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import signal
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "backend"))

RECORDS_PATH = ROOT / "backend/data/samples/software-io/batch-DGMOM/software_records.json"
SPLIT_PATH = ROOT / "docs/opt-pilot-split.json"
BLIND_PATH = ROOT / "docs/stable-round3-split.json"
TIMESCALE_CSV = ROOT / "docs/opt-timescale-60min.csv"
DUMP_DIR = ROOT / "docs/opt-bounds-demand"
OUT = ROOT / "docs/opt-bounds.csv"
SEED = 1
THREADS = 1
BOUND_SECONDS = 120.0
LAYERS = (1800.0, 900.0, 900.0)
CHAIN = 15.0
CPSAT = 15.0
ONDEMAND = 60.0
SOLVE_ALARM = CHAIN + CPSAT + ONDEMAND + sum(LAYERS) + 180.0
WORKERS = 3

COLUMNS = [
    "case_id",
    "record_index",
    "老软件用料",
    "我们60分钟用料",
    "重解用料",
    "用料一致",
    "重解校验",
    "LB1_LP",
    "LB1_LP来源",
    "LB1",
    "LB1整数解",
    "LB1已证明最优",
    "LB2",
    "锯缝",
    "锯缝来源",
    "锯缝模式",
    "段长种类",
    "段件数",
    "管总长",
    "LB1列数",
    "LB1轮数",
    "LB1用时",
    "界用时",
    "重解用时",
    "备注",
]

_RECORDS: list[dict] | None = None


def _init() -> None:
    global _RECORDS
    _RECORDS = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))["RECORDS"]


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _blind() -> set[int]:
    payload = json.loads(BLIND_PATH.read_text(encoding="utf-8"))
    return {int(index) for index in payload["blind_record_indexes"]}


def pilot_indexes() -> list[int]:
    payload = json.loads(SPLIT_PATH.read_text(encoding="utf-8"))
    chosen = [int(index) for index in payload["record_indexes"]]
    if len(chosen) != 50 or len(chosen) != len(set(chosen)):
        raise SystemExit(f"{SPLIT_PATH.name} is not a frozen 50-index split")
    overlap = set(chosen) & _blind()
    if overlap:
        raise SystemExit(f"split overlaps the blind set: {sorted(overlap)}")
    return chosen


def _as_int(value: object) -> int:
    return int(round(float(value)))  # type: ignore[arg-type]


def _alarm(seconds: float, message: str) -> None:
    def _handler(_signum, _frame):
        raise TimeoutError(message)

    signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, max(1.0, seconds))


def _clear_alarm() -> None:
    signal.setitimer(signal.ITIMER_REAL, 0)


def _explicit_blade(node: object) -> bool:
    if not isinstance(node, dict):
        return False
    nest = node.get("NestParam")
    if isinstance(nest, dict) and "BladeMargin" in nest:
        return True
    return "BladeMargin" in node


def _raw_groups(payload: dict) -> tuple[dict, list[dict]]:
    from app.engine_stable import _stable_nodes

    root, groups = _stable_nodes(payload)
    return root, groups


def _dump_path(index: int) -> Path:
    return DUMP_DIR / f"{index}.json"


def _segment_demand(solved: dict) -> dict[str, int]:
    demand: dict[int, int] = {}
    for row in solved.get("welding_patterns") or []:
        quantity = _as_int(row.get("quantity") or 0)
        if quantity <= 0:
            continue
        for part in row.get("parts") or []:
            length = _as_int(part)
            demand[length] = demand.get(length, 0) + quantity
    return {str(length): count for length, count in sorted(demand.items())}


def _incumbent(solved: dict) -> list[dict[str, object]]:
    merged: dict[tuple[int, tuple[int, ...]], int] = {}
    for row in solved.get("cutting_patterns") or []:
        quantity = _as_int(row.get("quantity") or 0)
        stock = _as_int(row.get("stock_length") or 0)
        parts = tuple(sorted(_as_int(part) for part in row.get("parts") or []))
        if quantity <= 0 or stock <= 0 or not parts:
            continue
        key = (stock, parts)
        merged[key] = merged.get(key, 0) + quantity
    rows = []
    for (stock, parts), quantity in sorted(merged.items()):
        rows.append({"stock": stock, "parts": list(parts), "quantity": quantity})
    return rows


def _solve_dump(task: dict[str, str]) -> dict:
    from app.domain import parse_problem
    from app.engine_stable_opt import apply_optimization, lex_key
    from app.solver import solve_payload
    from app.verifier import verify_solution

    index = int(task["record_index"])
    path = _dump_path(index)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    records = _RECORDS or []
    record = records[index]
    raw_payload = record["MOMPROBLEMJSON"]
    problem = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    raw_root, raw_groups = _raw_groups(problem)
    started = time.monotonic()
    _alarm(SOLVE_ALARM, "60分钟重解到达硬时限")
    try:
        result = solve_payload(
            problem,
            time_limit_seconds=CHAIN,
            engine="stable",
            cpsat_time_limit=CPSAT,
            ondemand_time_limit=ONDEMAND,
        )
        prepared = result.pop("_stable_prepared_payload")
        optimized, _logs = apply_optimization(prepared, result, LAYERS)
        report = verify_solution(prepared, optimized)
    finally:
        _clear_alarm()
    parsed = parse_problem(prepared)
    if len(parsed.groups) != len(optimized.get("groups") or []) or len(parsed.groups) != len(raw_groups):
        raise RuntimeError("组数与求解结果不一致")
    groups = []
    for group, solved, raw_group in zip(parsed.groups, optimized["groups"], raw_groups):
        explicit = _explicit_blade(raw_group) or _explicit_blade(raw_root)
        demand = _segment_demand(solved)
        piece_length = sum(int(length) * count for length, count in ((int(k), v) for k, v in demand.items()))
        groups.append(
            {
                "kerf": int(group.blade_margin),
                "kerf_source": "explicit" if explicit else "default-5mm",
                "kerf_mode": group.kerf_mode,
                "pipe_length": int(group.demand_length),
                "piece_length": piece_length,
                "demand": demand,
                "stocks": [
                    {
                        "length": int(stock.length),
                        "quantity": int(stock.quantity),
                        "must_use": int(stock.must_use_quantity),
                    }
                    for stock in group.stocks
                ],
                "incumbent": _incumbent(solved),
                "used_length": _as_int((solved.get("metrics") or {}).get("used_stock_length") or 0),
            }
        )
    key = lex_key(optimized["summary"])
    payload = {
        "record_index": index,
        "case_id": task["case_id"],
        "used_length": int(key[0]),
        "joints": int(key[1]),
        "weld_types": int(key[2]),
        "cut_types": int(key[3]),
        "verified": bool(report.get("passed") is True),
        "solve_seconds": time.monotonic() - started,
        "groups": groups,
    }
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return payload


def _load_group(group: dict) -> tuple[dict[int, int], list[tuple[int, int]], list[tuple[int, tuple[int, ...], int]]]:
    demand = {int(length): int(count) for length, count in group["demand"].items()}
    stocks = [(int(row["length"]), int(row["quantity"])) for row in group["stocks"]]
    incumbent = [
        (int(row["stock"]), tuple(int(part) for part in row["parts"]), int(row["quantity"]))
        for row in group["incumbent"]
    ]
    return demand, stocks, incumbent


def _bound_dump(dump: dict, deadline: float) -> dict[str, object]:
    from app.cut_pricing import cutting_stock_bound, minimum_inventory_length

    lp_sum = 0.0
    integer_sum = 0
    ip_sum = 0
    ip_complete = True
    proved = True
    sources: list[str] = []
    columns = 0
    rounds = 0
    lb1_seconds = 0.0
    notes: list[str] = []
    lb2 = 0
    prepared = []
    for index, group in enumerate(dump["groups"]):
        demand, stocks, incumbent = _load_group(group)
        cover = minimum_inventory_length(stocks, int(group["pipe_length"]))
        if cover is None:
            raise RuntimeError(f"组 {index} 的库存盖不住管长")
        lb2 += cover
        if int(group["piece_length"]) != int(group["pipe_length"]):
            notes.append(f"组{index}段长合计不等于管长")
        prepared.append((group, demand, stocks, incumbent))
    for index, (group, demand, stocks, incumbent) in enumerate(prepared):
        result = cutting_stock_bound(
            demand,
            stocks,
            int(group["kerf"]),
            str(group["kerf_mode"]),
            incumbent,
            deadline=deadline,
            seed=SEED,
            threads=THREADS,
        )
        lb1_seconds += float(result["seconds"])
        columns += int(result["columns"])
        rounds += int(result["rounds"])
        if result.get("note"):
            notes.append(f"组{index}:{result['note']}")
        if result.get("lp_bound") is None or result.get("integer_lb") is None:
            return {
                "lb2": lb2,
                "lp": None,
                "lb1": None,
                "ip": None,
                "proved": False,
                "sources": sources,
                "columns": columns,
                "rounds": rounds,
                "lb1_seconds": lb1_seconds,
                "notes": notes + [f"组{index}没有有效下界"],
            }
        lp_sum += float(result["lp_bound"])
        integer_sum += int(result["integer_lb"])
        sources.append(str(result["lp_source"]))
        if result.get("integer_solution") is None:
            ip_complete = False
            proved = False
        else:
            ip_sum += int(result["integer_solution"])
            proved = proved and bool(result["integer_proved"])
        if time.monotonic() >= deadline:
            notes.append("时限")
            break
    if len(sources) != len(dump["groups"]):
        return {
            "lb2": lb2,
            "lp": None,
            "lb1": None,
            "ip": None,
            "proved": False,
            "sources": sources,
            "columns": columns,
            "rounds": rounds,
            "lb1_seconds": lb1_seconds,
            "notes": notes + ["未完成全部分组"],
        }
    from math import ceil

    snapped = round(lp_sum)
    if abs(lp_sum - snapped) <= 1e-6 * max(1.0, abs(lp_sum)):
        ceil_lp = int(snapped)
    else:
        ceil_lp = int(ceil(lp_sum - 1e-9))
    lb1 = max(integer_sum, ceil_lp)
    ip = ip_sum if ip_complete else None
    if ip is not None and lb1 > ip:
        notes.append("合计下界高于整数解，已丢弃 LB1")
        return {
            "lb2": lb2,
            "lp": None,
            "lb1": None,
            "ip": ip,
            "proved": False,
            "sources": sources,
            "columns": columns,
            "rounds": rounds,
            "lb1_seconds": lb1_seconds,
            "notes": notes,
        }
    if ip is not None and ip == lb1:
        proved = True
    elif ip is None:
        proved = False
    return {
        "lb2": lb2,
        "lp": lp_sum,
        "lb1": lb1,
        "ip": ip,
        "proved": proved,
        "sources": sources,
        "columns": columns,
        "rounds": rounds,
        "lb1_seconds": lb1_seconds,
        "notes": notes,
    }


def _blank(task: dict[str, str]) -> dict[str, str]:
    row = {column: "" for column in COLUMNS}
    row["case_id"] = task["case_id"]
    row["record_index"] = task["record_index"]
    row["老软件用料"] = task.get("老软件用料", "")
    row["我们60分钟用料"] = task.get("我们60分钟用料", "")
    return row


def _fmt(value: object, digits: int = 4) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def solve_case(task: dict[str, str]) -> dict[str, str]:
    row = _blank(task)
    try:
        dump = _solve_dump(task)
        row["重解用料"] = str(dump["used_length"])
        row["重解用时"] = f"{float(dump.get('solve_seconds') or 0.0):.3f}"
        row["重解校验"] = "是" if dump.get("verified") else "否"
        row["用料一致"] = "是" if str(dump["used_length"]) == task.get("我们60分钟用料", "") else "否"
        if not dump.get("verified"):
            row["备注"] = "重解未通过校验，不算 LB1"
            return row
        if row["用料一致"] != "是":
            row["备注"] = "重解用料与 60 分钟档不一致，段长需求不能当作那一档的拼法"
            return row
        started = time.monotonic()
        deadline = started + BOUND_SECONDS
        _alarm(BOUND_SECONDS + 30.0, "下界计算到达硬时限")
        try:
            bound = _bound_dump(dump, deadline)
        finally:
            _clear_alarm()
        row["界用时"] = f"{time.monotonic() - started:.3f}"
        row["LB2"] = _fmt(bound.get("lb2"))
        row["LB1_LP"] = _fmt(bound.get("lp"))
        sources = bound.get("sources") or []
        row["LB1_LP来源"] = "+".join(str(item) for item in sources)
        row["LB1"] = _fmt(bound.get("lb1"))
        row["LB1整数解"] = _fmt(bound.get("ip"))
        if bound.get("ip") is None:
            row["LB1已证明最优"] = ""
        else:
            row["LB1已证明最优"] = "是" if bound.get("proved") else "否"
        kerfs = [str(group["kerf"]) for group in dump["groups"]]
        sources_k = [str(group["kerf_source"]) for group in dump["groups"]]
        modes = [str(group["kerf_mode"]) for group in dump["groups"]]
        row["锯缝"] = "+".join(kerfs)
        row["锯缝来源"] = "+".join(sources_k)
        row["锯缝模式"] = "+".join(modes)
        demand_lengths = {
            length
            for group in dump["groups"]
            for length in group["demand"]
        }
        row["段长种类"] = str(len(demand_lengths))
        row["段件数"] = str(sum(sum(group["demand"].values()) for group in dump["groups"]))
        row["管总长"] = str(sum(int(group["pipe_length"]) for group in dump["groups"]))
        row["LB1列数"] = str(bound.get("columns") or "")
        row["LB1轮数"] = str(bound.get("rounds") or "")
        row["LB1用时"] = f"{float(bound.get('lb1_seconds') or 0.0):.3f}"
        extra = "；".join(str(item) for item in (bound.get("notes") or []) if item)
        if extra:
            row["备注"] = (row["备注"] + "；" if row["备注"] else "") + extra
    except Exception as exc:  # noqa: BLE001
        _clear_alarm()
        row["备注"] = f"{type(exc).__name__}: {exc}"
        detail = traceback.format_exc(limit=2).strip().replace("\n", " | ")
        row["备注"] = f"{row['备注']} | {detail[-400:]}"
    return row


def _tasks(only: set[int] | None) -> list[dict[str, str]]:
    indexes = pilot_indexes()
    if only:
        unknown = only - set(indexes)
        if unknown:
            raise SystemExit(f"--only is outside the frozen hard 50: {sorted(unknown)}")
        indexes = [index for index in indexes if index in only]
    timescale = {int(row["record_index"]): row for row in _rows(TIMESCALE_CSV)}
    tasks = []
    for index in indexes:
        source = timescale.get(index)
        if source is None:
            raise SystemExit(f"record {index} is missing from the 60-minute table")
        tasks.append(
            {
                "record_index": str(index),
                "case_id": source["case_id"],
                "老软件用料": source.get("老软件用料", ""),
                "我们60分钟用料": source.get("最终用料", ""),
                "已知耗时": source.get("总耗时", "0"),
            }
        )
    return tasks


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    for row in _rows(path):
        # A killed worker writes nothing. A row with a re-solved length, or an
        # explicit mismatch / verifier failure, is finished.
        if row.get("重解用料") or row.get("用料一致") in {"是", "否"}:
            if row.get("case_id"):
                done.add(row["case_id"])
    return done


def summarize(path: Path) -> dict[str, object]:
    rows = _rows(path) if path.exists() else []
    usable = []
    for row in rows:
        if row.get("用料一致") != "是" or row.get("重解校验") != "是":
            continue
        if row.get("LB1") == "" or row.get("LB2") == "":
            continue
        usable.append(row)

    def num(row: dict[str, str], column: str) -> int:
        return int(round(float(row[column])))

    recoverable = 0
    must_change = 0
    legacy_above_lb2 = 0
    lb1_le_legacy = 0
    our_above_lb1 = 0
    proved = 0
    for row in usable:
        ours = num(row, "我们60分钟用料")
        legacy = num(row, "老软件用料")
        lb1 = num(row, "LB1")
        lb2 = num(row, "LB2")
        ip = row.get("LB1整数解") or ""
        baseline = num(row, "LB1整数解") if ip else lb1
        recoverable += ours - baseline
        must_change += max(0, lb1 - legacy)
        legacy_above_lb2 += legacy - lb2
        if lb1 <= legacy:
            lb1_le_legacy += 1
        our_above_lb1 += ours - lb1
        if row.get("LB1已证明最优") == "是":
            proved += 1
    summary = {
        "rows": len(rows),
        "usable": len(usable),
        "recoverable_by_cuts": recoverable,
        "must_change_segments": must_change,
        "legacy_above_lb2": legacy_above_lb2,
        "lb1_le_legacy": lb1_le_legacy,
        "our_minus_lb1": our_above_lb1,
        "proved": proved,
        "mismatch": sum(1 for row in rows if row.get("用料一致") == "否"),
        "unverified": sum(1 for row in rows if row.get("重解校验") == "否"),
        "missing_lb1": sum(1 for row in rows if row.get("LB1") == ""),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--only", type=int, action="append", default=[])
    args = parser.parse_args()
    out = args.out if args.out.is_absolute() else ROOT / args.out
    tasks = _tasks(set(args.only))
    tasks.sort(key=lambda task: float(task.get("已知耗时") or 0.0), reverse=True)
    done = _done(out)
    queue = [task for task in tasks if task["case_id"] not in done]
    print(
        f"bounds {len(tasks)} done {len(done)} pending {len(queue)} "
        f"workers {args.workers} bound {BOUND_SECONDS:.0f}s seed {SEED} threads {THREADS}",
        flush=True,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    new_file = not out.exists()
    handle = out.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
    if new_file:
        writer.writeheader()
        handle.flush()
    if queue:
        context = multiprocessing.get_context("spawn")
        wrote = 0
        with ProcessPoolExecutor(
            max_workers=max(1, args.workers),
            mp_context=context,
            initializer=_init,
        ) as pool:
            futures = {pool.submit(solve_case, task): task for task in queue}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"retry later {task['record_index']} "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    continue
                writer.writerow(row)
                handle.flush()
                wrote += 1
                print(
                    f"wrote {wrote}/{len(queue)} {row['record_index']} "
                    f"match {row['用料一致']} lb1 {row['LB1']} ip {row['LB1整数解']} "
                    f"lb2 {row['LB2']} wall {row['界用时']} {row['备注']}",
                    flush=True,
                )
    handle.close()
    summarize(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
