"""Independent solution verifier for serpentine-pipe one-dimensional nesting.

The verifier deliberately depends only on the submitted problem and solution.  It
does not import the solver, reuse candidate generators, or trust reported metrics.
It accepts both the production schema and the historical MOM result schema used
by the supplied regression sample.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import re
from typing import Any, Iterable, Mapping, Sequence


LENGTH_TOLERANCE = Decimal("0.01")
METRIC_LENGTH_TOLERANCE = Decimal("0.05")
RATE_TOLERANCE = Decimal("0.000001")


def verify_solution(original_payload: dict, solution: dict) -> dict:
    """Verify a nesting solution and return a JSON-serialisable report.

    Returned issues have stable ``code``, ``message``, ``path`` and ``severity``
    keys.  ``passed`` is false when at least one error exists; warnings (currently
    reserved for forward-compatible extensions) do not make a solution fail.
    """

    issues: list[dict[str, str]] = []
    try:
        problem_contexts = _extract_problem_contexts(original_payload, issues)
        solution_groups = _extract_solution_groups(solution, issues)
        pairings = _pair_groups(problem_contexts, solution_groups, issues)

        group_metrics: list[dict[str, Any]] = []
        for problem_ctx, solution_group, group_path in pairings:
            group_metrics.append(
                _verify_group(problem_ctx, solution_group, group_path, issues)
            )

        totals = _aggregate_metrics(group_metrics)
        _verify_summary_metrics(solution, totals, issues)
        totals["groups"] = group_metrics
        totals["group_count"] = len(group_metrics)
    except Exception as exc:  # a verifier must fail closed, never crash the API
        _issue(
            issues,
            "VERIFIER_INTERNAL_ERROR",
            f"验证器处理输入时发生异常：{type(exc).__name__}: {exc}",
            "$",
        )
        totals = {"groups": [], "group_count": 0}

    error_count = sum(item["severity"] == "error" for item in issues)
    return {
        "passed": error_count == 0,
        "issue_count": len(issues),
        "issues": issues,
        "recomputed_metrics": totals,
    }


def _extract_problem_contexts(payload: Any, issues: list[dict]) -> list[dict]:
    if not isinstance(payload, Mapping):
        _issue(issues, "INVALID_PROBLEM", "原始问题必须是 JSON 对象。", "$original")
        return []

    root = payload
    root_path = ""
    if isinstance(root.get("OriginalProblem"), Mapping):
        root = root["OriginalProblem"]
        root_path = "OriginalProblem"
    if isinstance(root.get("input"), Mapping):
        root = root["input"]
        root_path = "input"

    root_blade_margin = _nested_first(
        root,
        ("NestParam", "BladeMargin"),
        ("nest_param", "blade_margin"),
    )
    if root_blade_margin is None:
        root_blade_margin = _first(root, "BladeMargin", "blade_margin")
    if root_blade_margin is None:
        root_blade_margin = 10
    root_min_weld = _first(root, "Min_Welding_Length", "min_welding_length")
    if root_min_weld is None:
        root_min_weld = 500
    root_min_remnant = _first(
        root,
        "Min_Reusable_Remnant_Length",
        "min_reusable_remnant_length",
    )
    if root_min_remnant is None:
        root_min_remnant = 1
    root_min_cut = _first(root, "Min_Cut_Length", "min_cut_length")
    if root_min_cut is None:
        root_min_cut = 0
    root_kerf_mode = _nested_first(
        root,
        ("NestParam", "KerfMode"),
        ("nest_param", "kerf_mode"),
    )
    if root_kerf_mode is None:
        root_kerf_mode = _first(root, "KerfMode", "kerf_mode")
    global_values = {
        "min_welding_length": root_min_weld,
        "blade_margin": root_blade_margin,
        "min_reusable_remnant_length": root_min_remnant,
        "min_cut_length": root_min_cut,
        "kerf_mode": root_kerf_mode,
    }

    raw_groups: list[Any]
    if isinstance(root.get("data"), list):
        raw_groups = root["data"]
        group_paths = [
            _join_path(root_path, f"data[{index}]")
            for index in range(len(raw_groups))
        ]
    elif isinstance(root.get("groups"), list) and not ("Pipe" in root or "pipe" in root):
        raw_groups = root["groups"]
        group_paths = [
            _join_path(root_path, f"groups[{index}]")
            for index in range(len(raw_groups))
        ]
    elif _has_any(root, "Pipe", "pipe", "pipes"):
        raw_groups = [root]
        group_paths = [root_path]
    else:
        _issue(
            issues,
            "NO_PROBLEM_GROUPS",
            "原始问题中未找到 Pipe/Stock 排料组。",
            "$original",
        )
        return []

    contexts: list[dict] = []
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, Mapping):
            _issue(
                issues,
                "INVALID_PROBLEM_GROUP",
                "排料组必须是 JSON 对象。",
                f"$original.groups[{index}]",
            )
            continue
        contexts.append(
            {
                "group": raw,
                "root": root,
                "root_path": root_path,
                "group_path": group_paths[index],
                "index": index,
                **global_values,
            }
        )
    return contexts


def _extract_solution_groups(solution: Any, issues: list[dict]) -> list[dict]:
    if not isinstance(solution, Mapping):
        _issue(issues, "INVALID_SOLUTION", "解必须是 JSON 对象。", "$solution")
        return []
    if isinstance(solution.get("groups"), list):
        groups = solution["groups"]
    elif isinstance(solution.get("Result"), Mapping):
        groups = [solution]  # historical schema
    elif _has_any(
        solution,
        "welding_patterns",
        "WeldingPattern",
        "cutting_patterns",
        "CuttingPattern",
    ):
        groups = [solution]
    else:
        _issue(
            issues,
            "NO_SOLUTION_GROUPS",
            "解中未找到 groups 或排料结果。",
            "$solution",
        )
        return []

    result: list[dict] = []
    for index, group in enumerate(groups):
        if isinstance(group, Mapping):
            result.append(dict(group))
        else:
            _issue(
                issues,
                "INVALID_SOLUTION_GROUP",
                "解的排料组必须是 JSON 对象。",
                f"$solution.groups[{index}]",
            )
    return result


def _pair_groups(
    problem_contexts: list[dict], solution_groups: list[dict], issues: list[dict]
) -> list[tuple[dict, dict, str]]:
    if not problem_contexts:
        return []

    problem_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ctx in problem_contexts:
        problem_by_key[_group_key(ctx["group"])].append(ctx)

    used_problem_ids: set[int] = set()
    pairings: list[tuple[dict, dict, str]] = []
    for solution_index, result_group in enumerate(solution_groups):
        key = _solution_group_key(result_group)
        candidates = [
            ctx for ctx in problem_by_key.get(key, []) if id(ctx) not in used_problem_ids
        ]
        if (
            not candidates
            and key == ("", "")
            and len(problem_contexts) == len(solution_groups)
        ):
            positional = problem_contexts[solution_index]
            if id(positional) not in used_problem_ids:
                candidates = [positional]
        if (
            not candidates
            and key == ("", "")
            and len(problem_contexts) == 1
            and len(solution_groups) == 1
        ):
            candidates = [problem_contexts[0]]
        if not candidates:
            _issue(
                issues,
                "UNKNOWN_SOLUTION_GROUP",
                f"找不到解中材质规格组 {key!r} 对应的原始问题。",
                f"$solution.groups[{solution_index}]",
            )
            continue
        ctx = candidates[0]
        used_problem_ids.add(id(ctx))
        pairings.append((ctx, result_group, f"$solution.groups[{solution_index}]"))

    for ctx in problem_contexts:
        if id(ctx) not in used_problem_ids:
            key = _group_key(ctx["group"])
            _issue(
                issues,
                "MISSING_SOLUTION_GROUP",
                f"材质规格组 {key!r} 没有排料结果。",
                f"$original.groups[{ctx['index']}]",
            )
    return pairings


def _is_unsolved_group(result_group: Mapping[str, Any]) -> bool:
    if not isinstance(result_group, Mapping):
        return False
    status = result_group.get("metrics", {}).get("solve_status")
    if isinstance(status, str) and status.upper() == "UNSOLVED":
        return True
    return bool(result_group.get("shortage_diagnosis"))


def _unsolved_group_metrics(
    context: dict, group_key: tuple[str, str], issues: list[dict]
) -> dict[str, Any]:
    """Zeroed *plan* metrics for an unsolved group, but with the input-derived
    facts (total demand length and normalization paths) kept truthful.

    An unsolved group has no cutting plan, so every produced/used quantity is
    zero.  Demand length and normalization paths come from the *problem input*,
    not the plan, so they must still match what the solver's summary reports;
    zeroing them would trip the summary cross-check.
    """

    problem = context["group"]
    expected_normalizations = _expected_input_normalizations(context, issues)
    demand_length = Decimal(0)
    demanded_pipe_quantity = 0
    for index, pipe in enumerate(_as_list(_first(problem, "Pipe", "pipe", "pipes"))):
        if not isinstance(pipe, Mapping):
            continue
        length = _normalise_input_mm(
            _first(pipe, "pipe_length", "Length", "length"),
            "CEILING_TO_INTEGER_MM",
            issues,
            f"$original.Pipe[{index}].pipe_length",
            default=Decimal(0),
        )
        demand = _as_int(
            _first(pipe, "pipe_demand", "demand", "quantity"),
            issues,
            f"$original.Pipe[{index}].pipe_demand",
            default=0,
        )
        demand_length += length * demand
        demanded_pipe_quantity += demand

    # Stock is an input fact too: even without a cutting plan, the group's total
    # available stock and any must-use bars are known from the problem and the
    # solver reports them in its summary.  Zeroing them would trip the summary
    # cross-check, so recompute them from the input rather than hardcoding zero.
    inventory_length = Decimal(0)
    must_use_stock_quantity = 0
    must_use_stock_length = Decimal(0)
    for index, stock in enumerate(_as_list(_first(problem, "Stock", "stock", "stocks"))):
        if not isinstance(stock, Mapping):
            continue
        length = _normalise_input_mm(
            _first(stock, "stock_length", "Length", "length"),
            "CEILING_TO_INTEGER_MM",
            issues,
            f"$original.Stock[{index}].stock_length",
            default=Decimal(0),
        )
        quantity = _as_stock_quantity(
            _first(stock, "stock_demand", "demand", "quantity"),
            issues,
            f"$original.Stock[{index}].stock_demand",
            default=0,
        )
        inventory_length += length * quantity
        if _as_bool(_first(stock, "must_use", "mustUse", "Is_Must")):
            must_use_stock_quantity += quantity
            must_use_stock_length += length * quantity

    zero = _json_number(Decimal(0))
    return {
        "material": group_key[0],
        "specifications": group_key[1],
        "demand_length": _json_number(demand_length),
        "used_stock_length": zero,
        "utilization_rate": 0.0,
        "welding_joint_quantity": 0,
        "welding_pattern_type_quantity": 0,
        "cutting_pattern_type_quantity": 0,
        "kerf_loss": zero,
        "remainder_length": zero,
        "unused_stock_length": _json_number(inventory_length),
        "available_stock_length": _json_number(inventory_length),
        "inventory_length": _json_number(inventory_length),
        "pipe_quantity": demanded_pipe_quantity,
        "used_stock_quantity": 0,
        "segment_piece_quantity": 0,
        "must_use_stock_quantity": must_use_stock_quantity,
        "must_use_used_quantity": 0,
        "must_use_stock_length": _json_number(must_use_stock_length),
        "input_normalization_paths": [
            record["path"] for record in expected_normalizations
        ],
        "normalized_length_field_quantity": len(expected_normalizations),
        "unsolved": True,
    }


def _verify_group(
    context: dict, result_group: Mapping[str, Any], path: str, issues: list[dict]
) -> dict[str, Any]:
    problem = context["group"]
    group_key = _group_key(problem)
    if _is_unsolved_group(result_group):
        # An unsolved group ships a shortage diagnosis instead of a cutting plan.
        # Zero every *plan* metric but keep the input-derived demand length and
        # normalization paths so the summary cross-check still balances.
        return _unsolved_group_metrics(context, group_key, issues)
    expected_normalizations = _expected_input_normalizations(context, issues)
    _verify_input_normalizations(
        result_group, expected_normalizations, path, issues
    )
    min_weld_raw = _first(problem, "Min_Welding_Length", "min_welding_length")
    if min_weld_raw is None:
        min_weld_raw = context.get("min_welding_length")
    if min_weld_raw is None:
        min_weld_raw = 0
    min_weld = _normalise_input_mm(
        min_weld_raw,
        "CEILING_TO_INTEGER_MM",
        issues,
        f"{path}.Min_Welding_Length",
        default=Decimal(0),
    )
    min_cut_raw = _first(problem, "Min_Cut_Length", "min_cut_length")
    if min_cut_raw is None:
        min_cut_raw = context.get("min_cut_length")
    if min_cut_raw is None:
        min_cut_raw = 0
    min_cut = _normalise_input_mm(
        min_cut_raw,
        "CEILING_TO_INTEGER_MM",
        issues,
        f"{path}.Min_Cut_Length",
        default=Decimal(0),
    )
    blade_raw = _nested_first(
        problem,
        ("NestParam", "BladeMargin"),
        ("nest_param", "blade_margin"),
    )
    if blade_raw is None:
        blade_raw = _first(problem, "BladeMargin", "blade_margin")
    if blade_raw is None:
        blade_raw = context.get("blade_margin")
    blade_known = blade_raw is not None
    blade_margin = _normalise_input_mm(
        blade_raw or 0,
        "CEILING_TO_INTEGER_MM",
        issues,
        f"{path}.NestParam.BladeMargin",
        default=Decimal(0),
    )
    kerf_mode_raw = _nested_first(
        problem,
        ("NestParam", "KerfMode"),
        ("nest_param", "kerf_mode"),
    )
    if kerf_mode_raw is None:
        kerf_mode_raw = _first(problem, "KerfMode", "kerf_mode")
    if kerf_mode_raw is None:
        kerf_mode_raw = context.get("kerf_mode")
    kerf_mode = "BETWEEN_PARTS"
    if kerf_mode_raw is not None:
        normalized_mode = str(kerf_mode_raw).strip().upper()
        if normalized_mode in {"WITH_REMAINDER", "REMAINDER"}:
            kerf_mode = "WITH_REMAINDER"
        elif normalized_mode in {"BETWEEN_PARTS", "BETWEEN"}:
            kerf_mode = "BETWEEN_PARTS"
        else:
            _issue(
                issues,
                "INVALID_KERF_MODE",
                f"KerfMode 必须为 BETWEEN_PARTS 或 WITH_REMAINDER，收到 {kerf_mode_raw!r}。",
                f"{path}.NestParam.KerfMode",
            )

    pipes = _as_list(_first(problem, "Pipe", "pipe", "pipes"))
    stocks = _as_list(_first(problem, "Stock", "stock", "stocks"))
    pipe_by_key: dict[str, dict] = {}
    pipe_demand_by_key: dict[str, int] = {}
    demand_length = Decimal(0)
    demanded_pipe_quantity = 0
    for index, pipe in enumerate(pipes):
        if not isinstance(pipe, Mapping):
            continue
        pipe_key = _pipe_key(pipe)
        pipe_by_key[pipe_key] = dict(pipe)
        length = _normalise_input_mm(
            _first(pipe, "pipe_length", "Length", "length"),
            "CEILING_TO_INTEGER_MM",
            issues,
            f"$original.Pipe[{index}].pipe_length",
            default=Decimal(0),
        )
        demand = _as_int(
            _first(pipe, "pipe_demand", "demand", "quantity"),
            issues,
            f"$original.Pipe[{index}].pipe_demand",
            default=0,
        )
        pipe_demand_by_key[pipe_key] = demand
        demand_length += length * demand
        demanded_pipe_quantity += demand

    inventory: Counter[Decimal] = Counter()
    must_use_quantity: Counter[Decimal] = Counter()
    for index, stock in enumerate(stocks):
        if not isinstance(stock, Mapping):
            continue
        length = _normalise_input_mm(
            _first(stock, "stock_length", "Length", "length"),
            "CEILING_TO_INTEGER_MM",
            issues,
            f"$original.Stock[{index}].stock_length",
            default=Decimal(0),
        )
        quantity = _as_stock_quantity(
            _first(stock, "stock_demand", "demand", "quantity"),
            issues,
            f"$original.Stock[{index}].stock_demand",
            default=0,
        )
        inventory[length] += quantity
        if _as_bool(_first(stock, "must_use", "mustUse", "Is_Must")):
            must_use_quantity[length] += quantity

    welding_rows = _extract_welding_patterns(result_group)
    cutting_rows = _extract_cutting_patterns(result_group)

    consumed_segments: Counter[Decimal] = Counter()
    produced_segments: Counter[Decimal] = Counter()
    produced_pipe_by_key: Counter[str] = Counter()
    # A splice type is the ordered part tuple across the whole material group.
    # Pipe identity is deliberately excluded: repeating the same shop method on
    # another design pipe must not inflate the production pattern count.
    welding_types: set[tuple[Decimal, ...]] = set()
    welding_joint_quantity = 0

    for row_index, row in enumerate(welding_rows):
        row_path = f"{path}.welding_patterns[{row_index}]"
        quantity = _as_int(
            _first(row, "quantity", "Number", "number"),
            issues,
            f"{row_path}.quantity",
            default=0,
        )
        if quantity <= 0:
            _issue(
                issues,
                "INVALID_PATTERN_QUANTITY",
                "拼法数量必须大于 0。",
                f"{row_path}.quantity",
            )
            continue
        parts = _parts(_first(row, "parts", "Parts", "Part", "part"), issues, row_path)
        pipe_key = _match_pipe_key(row, pipe_by_key)
        if pipe_key is None:
            _issue(
                issues,
                "UNKNOWN_PIPE",
                f"拼法引用了未知管段 {_pipe_key(row)!r}。",
                row_path,
            )
            continue
        pipe = pipe_by_key[pipe_key]
        pipe_length = _normalise_input_mm(
            _first(pipe, "pipe_length", "Length", "length"),
            "CEILING_TO_INTEGER_MM",
            issues,
            f"{row_path}.pipe_length",
            default=Decimal(0),
        )
        reported_pipe_length = _first(row, "pipe_length", "Length", "length")
        if reported_pipe_length is not None:
            reported_pipe_length_value = _as_decimal(
                reported_pipe_length,
                issues,
                f"{row_path}.pipe_length",
                default=pipe_length,
            )
            if abs(reported_pipe_length_value - pipe_length) > LENGTH_TOLERANCE:
                _issue(
                    issues,
                    "REPORTED_PIPE_LENGTH_MISMATCH",
                    f"拼法标注管段长度 {_number(reported_pipe_length_value)}，输入为 {_number(pipe_length)}。",
                    f"{row_path}.pipe_length",
                )
            _require_integer_mm(
                reported_pipe_length_value, f"{row_path}.pipe_length", issues
            )
        if not parts:
            _issue(issues, "EMPTY_WELDING_PATTERN", "管段拼法不能为空。", row_path)
            continue
        for part_index, part in enumerate(parts):
            if part <= 0:
                _issue(
                    issues,
                    "NON_POSITIVE_PATTERN_PART",
                    f"拼接料段长度必须大于 0，当前为 {_number(part)}。",
                    f"{row_path}.parts[{part_index}]",
                )
        # The shortest spliceable segment is a hard process constraint: once a
        # pipe is welded from two or more parts, every part (including the two
        # end parts) must be weldable by the shop.  A single whole pipe has no
        # splice and is therefore exempt.
        if min_cut > 0 and len(parts) >= 2:
            for part_index, part in enumerate(parts):
                if part + LENGTH_TOLERANCE < min_cut:
                    _issue(
                        issues,
                        "MIN_CUT_LENGTH_VIOLATION",
                        f"拼接料段 {_number(part)} 小于最短可焊料段 {_number(min_cut)}。",
                        f"{row_path}.parts[{part_index}]",
                    )
        if abs(sum(parts, Decimal(0)) - pipe_length) > LENGTH_TOLERANCE:
            _issue(
                issues,
                "PIPE_LENGTH_MISMATCH",
                f"拼法总长 {_number(sum(parts, Decimal(0)))} 与管段长度 {_number(pipe_length)} 不一致。",
                row_path,
            )

        weld_positions = _cumulative(parts[:-1])
        reported_positions = _optional_decimal_list(
            _first(row, "weld_positions", "WeldPositions"), issues, f"{row_path}.weld_positions"
        )
        if reported_positions is not None and not _decimal_lists_equal(
            weld_positions, reported_positions
        ):
            _issue(
                issues,
                "WELD_POSITION_MISMATCH",
                f"输出焊口位置 {_numbers(reported_positions)} 与按拼法复算的 {_numbers(weld_positions)} 不一致。",
                f"{row_path}.weld_positions",
            )

        forbidden = _first(pipe, "Unweldable_Area", "unweldable_area") or []
        intervals = _parse_intervals(forbidden, issues, f"{row_path}.Unweldable_Area")
        for weld_index, weld in enumerate(weld_positions):
            for start, end in intervals:
                if start - LENGTH_TOLERANCE <= weld <= end + LENGTH_TOLERANCE:
                    _issue(
                        issues,
                        "WELD_IN_UNWELDABLE_AREA",
                        f"焊口位置 {_number(weld)} 落入禁焊区 [{_number(start)}, {_number(end)}]。",
                        f"{row_path}.weld_positions[{weld_index}]",
                    )
                    break

        for gap_index, (left, right) in enumerate(zip(weld_positions, weld_positions[1:])):
            gap = right - left
            if gap + LENGTH_TOLERANCE < min_weld:
                _issue(
                    issues,
                    "MIN_WELD_DISTANCE_VIOLATION",
                    f"相邻焊口间距 {_number(gap)} 小于最小值 {_number(min_weld)}。",
                    f"{row_path}.weld_positions[{gap_index + 1}]",
                )

        joint_count = len(parts) - 1
        max_joints = _as_int(
            _first(pipe, "Max_Weldingjoint_Number", "max_welding_joint_number") or 2**31 - 1,
            issues,
            f"{row_path}.Max_Weldingjoint_Number",
            default=2**31 - 1,
        )
        if joint_count > max_joints:
            _issue(
                issues,
                "MAX_WELD_COUNT_EXCEEDED",
                f"单根管段焊口数 {joint_count} 超过上限 {max_joints}。",
                row_path,
            )
        reported_joint_count = _first(row, "joint_count", "JointCount")
        if reported_joint_count is not None:
            reported_joint_count = _as_int(
                reported_joint_count,
                issues,
                f"{row_path}.joint_count",
                default=-1,
            )
            if reported_joint_count != joint_count:
                _issue(
                    issues,
                    "JOINT_COUNT_MISMATCH",
                    f"拼法报告焊口数 {reported_joint_count}，复算为 {joint_count}。",
                    f"{row_path}.joint_count",
                )

        produced_pipe_by_key[pipe_key] += quantity
        welding_joint_quantity += joint_count * quantity
        welding_types.add(tuple(parts))
        for part in parts:
            consumed_segments[part] += quantity

    for pipe_key, expected in pipe_demand_by_key.items():
        actual = produced_pipe_by_key[pipe_key]
        if actual != expected:
            _issue(
                issues,
                "PIPE_PATTERN_QUANTITY_MISMATCH",
                f"管段 {pipe_key} 需求 {expected} 根，拼法合计 {actual} 根。",
                f"{path}.welding_patterns",
            )

    used_stock: Counter[Decimal] = Counter()
    cutting_types: set[tuple[Decimal, tuple[Decimal, ...]]] = set()
    expected_remnants: Counter[tuple[Decimal, Decimal]] = Counter()
    used_stock_length = Decimal(0)
    kerf_loss = Decimal(0)
    remainder_length = Decimal(0)
    used_stock_quantity = 0

    for row_index, row in enumerate(cutting_rows):
        row_path = f"{path}.cutting_patterns[{row_index}]"
        stock_length = _as_decimal(
            _first(row, "stock_length", "Length", "length"),
            issues,
            f"{row_path}.stock_length",
            default=Decimal(0),
        )
        _require_integer_mm(stock_length, f"{row_path}.stock_length", issues)
        quantity = _as_int(
            _first(row, "quantity", "Number", "number"),
            issues,
            f"{row_path}.quantity",
            default=0,
        )
        if quantity <= 0:
            _issue(
                issues,
                "INVALID_CUT_QUANTITY",
                "切法数量必须大于 0。",
                f"{row_path}.quantity",
            )
            continue
        parts = _parts(_first(row, "parts", "Parts", "Part", "part"), issues, row_path)
        if not parts:
            _issue(issues, "EMPTY_CUTTING_PATTERN", "原材料切法不能为空。", row_path)
            continue
        for part_index, part in enumerate(parts):
            if part <= 0:
                _issue(
                    issues,
                    "NON_POSITIVE_CUT_PART",
                    f"切出料段长度必须大于 0，当前为 {_number(part)}。",
                    f"{row_path}.parts[{part_index}]",
                )

        row_kerf_raw = _first(row, "kerf_per_cut", "blade_margin", "BladeMargin")
        row_kerf = blade_margin
        if row_kerf_raw is not None:
            reported_row_kerf = _as_decimal(
                row_kerf_raw,
                issues,
                f"{row_path}.kerf_per_cut",
                default=blade_margin,
            )
            if blade_known and abs(reported_row_kerf - blade_margin) > LENGTH_TOLERANCE:
                _issue(
                    issues,
                    "BLADE_MARGIN_MISMATCH",
                    f"切法切缝 {_number(reported_row_kerf)} 与输入 BladeMargin {_number(blade_margin)} 不一致。",
                    f"{row_path}.kerf_per_cut",
                )
            if not blade_known:
                # Older callers did not carry BladeMargin in the problem.  In
                # that compatibility case the explicit cutting-row value is the
                # only available source; production input always supplies it.
                row_kerf = reported_row_kerf
            _require_integer_mm(reported_row_kerf, f"{row_path}.kerf_per_cut", issues)

        # Some machine adapters add explicit absolute cutting coordinates.  The
        # core schema does not require them, but when present they are still
        # governed by the integer-millimetre output contract.
        _optional_decimal_list(
            _first(
                row,
                "cut_positions",
                "CutPositions",
                "cut_coordinates",
                "CutCoordinates",
            ),
            issues,
            f"{row_path}.cut_positions",
        )

        per_stock_kerf = row_kerf * max(0, len(parts) - 1)
        part_sum = sum(parts, Decimal(0))
        between_remainder = stock_length - part_sum - per_stock_kerf
        # WITH_REMAINDER charges one extra blade pass whenever the bar is not cut
        # flush to its end (a positive leftover stub remains after the last part).
        if (
            kerf_mode == "WITH_REMAINDER"
            and row_kerf > 0
            and between_remainder > LENGTH_TOLERANCE
        ):
            per_stock_kerf += row_kerf
        per_stock_remainder = stock_length - part_sum - per_stock_kerf
        if per_stock_remainder < -LENGTH_TOLERANCE:
            _issue(
                issues,
                "CUT_CAPACITY_EXCEEDED",
                f"料段总长 {_number(part_sum)} 加切缝 {_number(per_stock_kerf)} 超过原管 {_number(stock_length)}。",
                row_path,
            )

        reported_trim = _first(row, "TrimLoss", "trim_loss")
        if reported_trim is not None:
            trim = _as_decimal(
                reported_trim,
                issues,
                f"{row_path}.TrimLoss",
                default=Decimal(0),
            )
            _require_integer_mm(trim, f"{row_path}.TrimLoss", issues)
            expected_trim = stock_length - part_sum
            if abs(trim - expected_trim) > METRIC_LENGTH_TOLERANCE:
                _issue(
                    issues,
                    "TRIM_LOSS_MISMATCH",
                    f"TrimLoss 报告 {_number(trim)}，按原管减有效料段复算为 {_number(expected_trim)}。",
                    f"{row_path}.TrimLoss",
                )

        _compare_optional_length(
            row,
            ("kerf_loss_per_stock",),
            per_stock_kerf,
            "KERF_LOSS_MISMATCH",
            "单根切缝损失",
            row_path,
            issues,
        )
        _compare_optional_length(
            row,
            ("remainder_per_stock",),
            max(Decimal(0), per_stock_remainder),
            "REMAINDER_MISMATCH",
            "单根余料",
            row_path,
            issues,
        )
        _compare_optional_length(
            row,
            ("used_length_per_stock",),
            part_sum + per_stock_kerf,
            "USED_LENGTH_MISMATCH",
            "单根已占用长度",
            row_path,
            issues,
        )

        used_stock[stock_length] += quantity
        used_stock_quantity += quantity
        used_stock_length += stock_length * quantity
        kerf_loss += per_stock_kerf * quantity
        nonnegative_remainder = max(Decimal(0), per_stock_remainder)
        remainder_length += nonnegative_remainder * quantity
        if nonnegative_remainder > LENGTH_TOLERANCE:
            expected_remnants[(nonnegative_remainder, stock_length)] += quantity
        cutting_types.add((stock_length, tuple(sorted(parts))))
        for part in parts:
            produced_segments[part] += quantity

    for length, used in used_stock.items():
        offered = inventory[length]
        if length not in inventory:
            _issue(
                issues,
                "STOCK_LENGTH_UNKNOWN",
                f"切法使用了库存中不存在的原管长度 {_number(length)}。",
                f"{path}.cutting_patterns",
            )
        elif used > offered:
            _issue(
                issues,
                "STOCK_QUANTITY_EXCEEDED",
                f"原管 {_number(length)} 使用 {used} 根，库存只有 {offered} 根。",
                f"{path}.cutting_patterns",
            )
    # Must-use is a *group-level priority*, not a per-length lower bound: within a
    # material/specification group every must-use bar must be consumed before any
    # available (non-must-use) bar may be used.  If demand is small enough to be
    # met by a subset of the must-use bars, the remainder is not forced -- but
    # then no available bar may be used either (demand must be met entirely from
    # must-use stock).  So the only violation is: an available bar was used while
    # some must-use bar was still unused.
    must_use_total = sum(must_use_quantity.values())
    used_must = sum(
        min(used_stock[length], required)
        for length, required in must_use_quantity.items()
    )
    used_available = sum(
        max(0, used_stock[length] - must_use_quantity.get(length, 0))
        for length in used_stock
    )
    if used_available > 0 and used_must < must_use_total:
        _issue(
            issues,
            "MUST_USE_STOCK_NOT_USED",
            f"必用料未用完（已用 {used_must}/{must_use_total} 根）却已动用 "
            f"{used_available} 根可用料；必用料优先，须先用完必用料才能用可用料。",
            f"{path}.cutting_patterns",
        )

    unused_rows, unused_present = _extract_unused_materials(result_group)
    unused_reported: Counter[Decimal] = Counter()
    for row_index, row in enumerate(unused_rows):
        row_path = f"{path}.unused_materials[{row_index}]"
        length = _as_decimal(
            _first(row, "stock_length", "Length", "length"),
            issues,
            f"{row_path}.stock_length",
            default=Decimal(0),
        )
        _require_integer_mm(length, f"{row_path}.stock_length", issues)
        quantity = _as_int(
            _first(row, "quantity", "stock_demand", "Number", "number"),
            issues,
            f"{row_path}.quantity",
            default=0,
        )
        if quantity < 0:
            _issue(
                issues,
                "INVALID_UNUSED_QUANTITY",
                "未使用库存数量不能为负。",
                f"{row_path}.quantity",
            )
        unused_reported[length] += quantity
    if unused_present:
        for length in set(inventory) | set(used_stock) | set(unused_reported):
            expected = inventory[length] - used_stock[length]
            actual = unused_reported[length]
            if actual != expected:
                _issue(
                    issues,
                    "UNUSED_STOCK_MISMATCH",
                    f"原管 {_number(length)} 未使用数量报告 {actual}，复算为 {expected}。",
                    f"{path}.unused_materials",
                )

    for segment_length in sorted(set(consumed_segments) | set(produced_segments)):
        consumed = consumed_segments[segment_length]
        produced = produced_segments[segment_length]
        if consumed != produced:
            _issue(
                issues,
                "SEGMENT_BALANCE_MISMATCH",
                f"料段 {_number(segment_length)} 切出 {produced} 根，拼法消耗 {consumed} 根。",
                path,
            )

    _verify_generated_remnants(
        result_group,
        expected_remnants,
        context,
        path,
        issues,
    )

    unused_stock_length = sum(
        length * (inventory[length] - used_stock[length]) for length in inventory
    )
    inventory_length = sum(length * quantity for length, quantity in inventory.items())
    utilization = demand_length / used_stock_length if used_stock_length > 0 else Decimal(0)
    must_use_stock_quantity = sum(must_use_quantity.values())
    must_use_used_quantity = sum(
        min(used_stock[length], required)
        for length, required in must_use_quantity.items()
    )
    must_use_stock_length = sum(
        (length * quantity for length, quantity in must_use_quantity.items()),
        Decimal(0),
    )
    target_raw = _first(problem, "Target_Util_Rate", "target_utilization_rate")
    target_utilization = None
    if target_raw is not None:
        target_utilization = _as_decimal(
            target_raw,
            issues,
            f"{path}.Target_Util_Rate",
            default=Decimal(0),
        )
        if target_utilization > 1:
            target_utilization /= 100
    target_reached = (
        utilization + RATE_TOLERANCE >= target_utilization
        if target_utilization is not None
        else None
    )
    metrics = {
        "material": group_key[0],
        "specifications": group_key[1],
        "demand_length": _json_number(demand_length),
        "used_stock_length": _json_number(used_stock_length),
        "utilization_rate": float(utilization),
        "welding_joint_quantity": welding_joint_quantity,
        "welding_pattern_type_quantity": len(welding_types),
        "cutting_pattern_type_quantity": len(cutting_types),
        "kerf_loss": _json_number(kerf_loss),
        "remainder_length": _json_number(remainder_length),
        "unused_stock_length": _json_number(unused_stock_length),
        "available_stock_length": _json_number(inventory_length),
        "inventory_length": _json_number(inventory_length),
        "pipe_quantity": demanded_pipe_quantity,
        "used_stock_quantity": used_stock_quantity,
        "segment_piece_quantity": sum(consumed_segments.values()),
        "must_use_stock_quantity": must_use_stock_quantity,
        "must_use_used_quantity": must_use_used_quantity,
        "must_use_stock_length": _json_number(must_use_stock_length),
        "input_normalization_paths": [
            record["path"] for record in expected_normalizations
        ],
        "normalized_length_field_quantity": len(expected_normalizations),
    }
    if target_utilization is not None:
        metrics["target_utilization_rate"] = float(target_utilization)
        metrics["target_reached"] = target_reached
    _verify_reported_group_metrics(result_group, metrics, path, issues)
    return metrics


def _extract_welding_patterns(group: Mapping[str, Any]) -> list[dict]:
    if isinstance(group.get("Result"), Mapping):
        source = (
            group["Result"].get("WeldingPattern", {}).get("WeldingPipe", [])
            if isinstance(group["Result"].get("WeldingPattern"), Mapping)
            else []
        )
    else:
        source = _first(group, "welding_patterns", "WeldingPipe", "weldingPatterns") or []
        if isinstance(source, Mapping):
            source = _first(source, "WeldingPipe", "patterns") or []
    rows: list[dict] = []
    for outer in _as_list(source):
        if not isinstance(outer, Mapping):
            continue
        nested = _first(outer, "Pattern", "patterns")
        if isinstance(nested, list):
            for pattern in nested:
                if isinstance(pattern, Mapping):
                    merged = dict(outer)
                    merged.update(pattern)
                    merged.pop("Pattern", None)
                    merged.pop("patterns", None)
                    rows.append(merged)
        else:
            rows.append(dict(outer))
    return rows


def _extract_cutting_patterns(group: Mapping[str, Any]) -> list[dict]:
    if isinstance(group.get("Result"), Mapping):
        source = (
            group["Result"].get("CuttingPattern", {}).get("CuttingPipe", [])
            if isinstance(group["Result"].get("CuttingPattern"), Mapping)
            else []
        )
    else:
        source = _first(group, "cutting_patterns", "CuttingPipe", "cuttingPatterns") or []
        if isinstance(source, Mapping):
            source = _first(source, "CuttingPipe", "patterns") or []
    return [dict(item) for item in _as_list(source) if isinstance(item, Mapping)]


def _extract_unused_materials(group: Mapping[str, Any]) -> tuple[list[dict], bool]:
    if isinstance(group.get("Result"), Mapping):
        present = "UnusedMaterials" in group["Result"]
        source = group["Result"].get("UnusedMaterials", [])
    else:
        present = "unused_materials" in group or "UnusedMaterials" in group
        source = _first(group, "unused_materials", "UnusedMaterials") or []
    return (
        [dict(item) for item in _as_list(source) if isinstance(item, Mapping)],
        present,
    )


def _expected_input_normalizations(
    context: Mapping[str, Any], issues: list[dict]
) -> list[dict[str, Any]]:
    """Independently derive the auditable input-normalisation records."""

    root = context["root"]
    group = context["group"]
    root_path = str(context.get("root_path", ""))
    group_path = str(context.get("group_path", ""))
    records: list[dict[str, Any]] = []

    def add(container: Mapping[str, Any], key: str, path: str, rule: str) -> None:
        if key not in container:
            return
        raw = container[key]
        parsed = _as_decimal(raw, issues, path, default=Decimal(0))
        rounding = ROUND_FLOOR if rule == "FLOOR_TO_INTEGER_MM" else ROUND_CEILING
        normalized = parsed.to_integral_value(rounding=rounding)
        if parsed != normalized:
            records.append(
                {
                    "path": path,
                    "original": raw,
                    "normalized": int(normalized),
                    "rule": rule,
                }
            )

    root_nest = root.get("NestParam")
    if isinstance(root_nest, Mapping) and "BladeMargin" in root_nest:
        add(
            root_nest,
            "BladeMargin",
            _join_path(root_path, "NestParam.BladeMargin"),
            "CEILING_TO_INTEGER_MM",
        )
    elif "BladeMargin" in root:
        add(
            root,
            "BladeMargin",
            _join_path(root_path, "BladeMargin"),
            "CEILING_TO_INTEGER_MM",
        )
    for field_name in (
        "Min_Welding_Length",
        "Min_Reusable_Remnant_Length",
        "Min_Cut_Length",
        "Adjacent_Distance",
        "Corner_Distance",
        "Adjacent_Corner_Distance",
    ):
        add(
            root,
            field_name,
            _join_path(root_path, field_name),
            "CEILING_TO_INTEGER_MM",
        )

    if group is not root:
        group_nest = group.get("NestParam")
        if isinstance(group_nest, Mapping) and "BladeMargin" in group_nest:
            add(
                group_nest,
                "BladeMargin",
                _join_path(group_path, "NestParam.BladeMargin"),
                "CEILING_TO_INTEGER_MM",
            )
        elif "BladeMargin" in group:
            add(
                group,
                "BladeMargin",
                _join_path(group_path, "BladeMargin"),
                "CEILING_TO_INTEGER_MM",
            )
        for field_name in (
            "Min_Welding_Length",
            "Min_Reusable_Remnant_Length",
            "Min_Cut_Length",
            "Adjacent_Distance",
            "Corner_Distance",
            "Adjacent_Corner_Distance",
        ):
            add(
                group,
                field_name,
                _join_path(group_path, field_name),
                "CEILING_TO_INTEGER_MM",
            )

    pipes = _as_list(_first(group, "Pipe", "pipe", "pipes"))
    for pipe_index, pipe in enumerate(pipes):
        if not isinstance(pipe, Mapping):
            continue
        pipe_path = _join_path(group_path, f"Pipe[{pipe_index}]")
        for key in ("pipe_length",):
            add(
                pipe,
                key,
                _join_path(pipe_path, key),
                "CEILING_TO_INTEGER_MM",
            )
        for interval_index, interval in enumerate(
            _coerce_unweldable_area(_first(pipe, "Unweldable_Area", "unweldable_area"))
        ):
            if not isinstance(interval, Sequence) or isinstance(interval, str) or len(interval) < 2:
                continue
            interval_path = _join_path(
                pipe_path, f"Unweldable_Area[{interval_index}]"
            )
            synthetic = {"start": interval[0], "end": interval[1]}
            add(
                synthetic,
                "start",
                f"{interval_path}[0]",
                "FLOOR_TO_INTEGER_MM",
            )
            add(
                synthetic,
                "end",
                f"{interval_path}[1]",
                "CEILING_TO_INTEGER_MM",
            )

    stocks = _as_list(_first(group, "Stock", "stock", "stocks"))
    for stock_index, stock in enumerate(stocks):
        if not isinstance(stock, Mapping):
            continue
        add(
            stock,
            "stock_length",
            _join_path(group_path, f"Stock[{stock_index}].stock_length"),
            "CEILING_TO_INTEGER_MM",
        )

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        unique[(record["path"], record["rule"])] = record
    return sorted(unique.values(), key=lambda item: (item["path"], item["rule"]))


def _verify_input_normalizations(
    group: Mapping[str, Any],
    expected: Sequence[Mapping[str, Any]],
    path: str,
    issues: list[dict],
) -> None:
    # Historical MOM output predates structured normalisation metadata.
    if isinstance(group.get("Result"), Mapping):
        return
    if "input_normalizations" not in group:
        _issue(
            issues,
            "MISSING_INPUT_NORMALIZATIONS",
            "新版排料组必须输出 input_normalizations（无变化时为空数组）。",
            f"{path}.input_normalizations",
        )
        return
    raw_rows = group.get("input_normalizations")
    if not isinstance(raw_rows, list):
        _issue(
            issues,
            "INVALID_INPUT_NORMALIZATIONS",
            "input_normalizations 必须是数组。",
            f"{path}.input_normalizations",
        )
        return

    actual: dict[tuple[str, str], Mapping[str, Any]] = {}
    actual_order: list[tuple[str, str]] = []
    for index, row in enumerate(raw_rows):
        row_path = f"{path}.input_normalizations[{index}]"
        if not isinstance(row, Mapping):
            _issue(
                issues,
                "INVALID_INPUT_NORMALIZATION_RECORD",
                "归一化记录必须是对象。",
                row_path,
            )
            continue
        missing = [
            key for key in ("path", "original", "normalized", "rule") if key not in row
        ]
        if missing:
            _issue(
                issues,
                "INVALID_INPUT_NORMALIZATION_RECORD",
                f"归一化记录缺少字段：{', '.join(missing)}。",
                row_path,
            )
            continue
        record_path = str(row["path"])
        rule = str(row["rule"])
        key = (record_path, rule)
        actual_order.append(key)
        if rule not in {"CEILING_TO_INTEGER_MM", "FLOOR_TO_INTEGER_MM"}:
            _issue(
                issues,
                "UNKNOWN_INPUT_NORMALIZATION_RULE",
                f"未知归一化规则 {rule!r}。",
                f"{row_path}.rule",
            )
        if key in actual:
            _issue(
                issues,
                "DUPLICATE_INPUT_NORMALIZATION",
                f"归一化路径 {record_path!r} 重复输出。",
                row_path,
            )
        actual[key] = row
        normalized = _as_decimal(
            row["normalized"],
            issues,
            f"{row_path}.normalized",
            default=Decimal(0),
        )
        _require_integer_mm(normalized, f"{row_path}.normalized", issues)
        if not isinstance(row["normalized"], int) or isinstance(row["normalized"], bool):
            _issue(
                issues,
                "NORMALIZED_VALUE_NOT_JSON_INTEGER",
                "normalized 必须输出为 JSON 整数。",
                f"{row_path}.normalized",
            )

    if actual_order != sorted(actual_order):
        _issue(
            issues,
            "INPUT_NORMALIZATIONS_NOT_STABLY_SORTED",
            "input_normalizations 必须按 (path, rule) 稳定排序。",
            f"{path}.input_normalizations",
        )

    expected_by_key = {(row["path"], row["rule"]): row for row in expected}
    for key in sorted(set(expected_by_key) | set(actual)):
        expected_row = expected_by_key.get(key)
        actual_row = actual.get(key)
        if expected_row is None:
            _issue(
                issues,
                "UNEXPECTED_INPUT_NORMALIZATION",
                f"输入路径 {key[0]!r} 不需要归一化却被记录。",
                f"{path}.input_normalizations",
            )
            continue
        if actual_row is None:
            _issue(
                issues,
                "MISSING_INPUT_NORMALIZATION",
                f"输入路径 {key[0]!r} 的归一化记录缺失。",
                f"{path}.input_normalizations",
            )
            continue
        if actual_row["original"] != expected_row["original"]:
            _issue(
                issues,
                "INPUT_NORMALIZATION_ORIGINAL_MISMATCH",
                f"路径 {key[0]!r} 的 original 未保留原始输入值。",
                f"{path}.input_normalizations",
            )
        if actual_row["normalized"] != expected_row["normalized"]:
            _issue(
                issues,
                "INPUT_NORMALIZATION_VALUE_MISMATCH",
                f"路径 {key[0]!r} 报告归一值 {actual_row['normalized']!r}，独立复算为 {expected_row['normalized']!r}。",
                f"{path}.input_normalizations",
            )


def _verify_generated_remnants(
    group: Mapping[str, Any],
    expected: Counter[tuple[Decimal, Decimal]],
    context: dict,
    path: str,
    issues: list[dict],
) -> None:
    if "generated_remnants" not in group and "GeneratedRemnants" not in group:
        return
    rows = _as_list(_first(group, "generated_remnants", "GeneratedRemnants"))
    actual: Counter[tuple[Decimal, Decimal]] = Counter()
    threshold_raw = _first(
        context["group"],
        "Min_Reusable_Remnant_Length",
        "min_reusable_remnant_length",
    ) or context.get("min_reusable_remnant_length")
    threshold = None
    if threshold_raw is not None:
        threshold = _normalise_input_mm(
            threshold_raw,
            "CEILING_TO_INTEGER_MM",
            issues,
            f"{path}.min_reusable_remnant_length",
            default=Decimal(0),
        )
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        row_path = f"{path}.generated_remnants[{index}]"
        length = _as_decimal(
            _first(row, "length", "remnant_length", "Length"),
            issues,
            f"{row_path}.length",
            default=Decimal(0),
        )
        _require_integer_mm(length, f"{row_path}.length", issues)
        source = _as_decimal(
            _first(row, "source_stock_length", "stock_length", "SourceStockLength") or 0,
            issues,
            f"{row_path}.source_stock_length",
            default=Decimal(0),
        )
        _require_integer_mm(source, f"{row_path}.source_stock_length", issues)
        quantity = _as_int(
            _first(row, "quantity", "Number", "number"),
            issues,
            f"{row_path}.quantity",
            default=0,
        )
        actual[(length, source)] += quantity
        if threshold is not None and _first(row, "reusable", "Reusable") is not None:
            expected_reusable = length + LENGTH_TOLERANCE >= threshold
            if _as_bool(_first(row, "reusable", "Reusable")) != expected_reusable:
                _issue(
                    issues,
                    "REMNANT_REUSABLE_FLAG_MISMATCH",
                    f"余料 {_number(length)} 的 reusable 标记与阈值 {_number(threshold)} 不一致。",
                    f"{row_path}.reusable",
                )

    # New production schema includes source_stock_length, so compare exact origin.
    for key in set(expected) | set(actual):
        if expected[key] != actual[key]:
            length, source = key
            _issue(
                issues,
                "REMNANT_DETAIL_MISMATCH",
                f"来源原管 {_number(source)} 的余料 {_number(length)} 报告 {actual[key]} 根，复算为 {expected[key]} 根。",
                f"{path}.generated_remnants",
            )


def _verify_reported_group_metrics(
    group: Mapping[str, Any], metrics: dict[str, Any], path: str, issues: list[dict]
) -> None:
    if isinstance(group.get("GeneralInfo"), Mapping):
        reported = group["GeneralInfo"]
        aliases = {
            "demand_length": ("PipeLength_Of_SingleMaterialSpecification",),
            "used_stock_length": ("StockLength_Of_SingleMaterialSpecification",),
            "utilization_rate": ("UtilRate",),
            "welding_joint_quantity": ("WeldingJointQuantity",),
        }
    else:
        reported = group.get("metrics") if isinstance(group.get("metrics"), Mapping) else {}
        aliases = {
            "demand_length": ("demand_length",),
            "available_stock_length": ("available_stock_length",),
            "used_stock_length": ("used_stock_length",),
            "utilization_rate": ("utilization_rate",),
            "welding_joint_quantity": ("welding_joint_quantity",),
            "welding_pattern_type_quantity": ("welding_pattern_type_quantity",),
            "cutting_pattern_type_quantity": ("cutting_pattern_type_quantity",),
            "kerf_loss": ("kerf_loss",),
            "remainder_length": ("remainder_length",),
            "must_use_stock_quantity": ("must_use_stock_quantity",),
            "must_use_used_quantity": ("must_use_used_quantity",),
            "must_use_stock_length": ("must_use_stock_length",),
        }
    codes = {
        "demand_length": "DEMAND_LENGTH_METRIC_MISMATCH",
        "available_stock_length": "AVAILABLE_STOCK_LENGTH_METRIC_MISMATCH",
        "used_stock_length": "USED_STOCK_LENGTH_METRIC_MISMATCH",
        "utilization_rate": "UTILIZATION_METRIC_MISMATCH",
        "welding_joint_quantity": "WELD_COUNT_METRIC_MISMATCH",
        "welding_pattern_type_quantity": "WELD_PATTERN_COUNT_METRIC_MISMATCH",
        "cutting_pattern_type_quantity": "CUT_PATTERN_COUNT_METRIC_MISMATCH",
        "kerf_loss": "KERF_METRIC_MISMATCH",
        "remainder_length": "REMAINDER_METRIC_MISMATCH",
        "must_use_stock_quantity": "MUST_USE_QUANTITY_METRIC_MISMATCH",
        "must_use_used_quantity": "MUST_USE_USED_METRIC_MISMATCH",
        "must_use_stock_length": "MUST_USE_LENGTH_METRIC_MISMATCH",
    }
    for metric_name, names in aliases.items():
        raw = _first(reported, *names)
        if raw is None:
            continue
        actual = Decimal(str(metrics[metric_name]))
        value = _as_decimal(raw, issues, f"{path}.metrics.{names[0]}", default=actual)
        if metric_name in {
            "demand_length",
            "available_stock_length",
            "used_stock_length",
            "kerf_loss",
            "remainder_length",
            "must_use_stock_length",
        }:
            _require_integer_mm(value, f"{path}.metrics.{names[0]}", issues)
        tolerance = RATE_TOLERANCE if metric_name == "utilization_rate" else METRIC_LENGTH_TOLERANCE
        if abs(value - actual) > tolerance:
            _issue(
                issues,
                codes[metric_name],
                f"指标 {metric_name} 报告 {_number(value)}，独立复算为 {_number(actual)}。",
                f"{path}.metrics.{names[0]}",
            )
    if "target_utilization_rate" in reported and "target_utilization_rate" in metrics:
        target = _as_decimal(
            reported["target_utilization_rate"],
            issues,
            f"{path}.metrics.target_utilization_rate",
            default=Decimal(str(metrics["target_utilization_rate"])),
        )
        if target > 1:
            target /= 100
        actual_target = Decimal(str(metrics["target_utilization_rate"]))
        if abs(target - actual_target) > RATE_TOLERANCE:
            _issue(
                issues,
                "TARGET_UTILIZATION_METRIC_MISMATCH",
                f"目标利用率报告 {_number(target)}，输入复算为 {_number(actual_target)}。",
                f"{path}.metrics.target_utilization_rate",
            )
    if "target_reached" in reported and "target_reached" in metrics:
        if _as_bool(reported["target_reached"]) != bool(metrics["target_reached"]):
            _issue(
                issues,
                "TARGET_REACHED_MISMATCH",
                f"target_reached 报告为 {_as_bool(reported['target_reached'])}，复算为 {metrics['target_reached']}。",
                f"{path}.metrics.target_reached",
            )


def _verify_summary_metrics(
    solution: Mapping[str, Any], totals: dict[str, Any], issues: list[dict]
) -> None:
    summary = solution.get("summary")
    if not isinstance(summary, Mapping):
        return
    aliases = (
        "demand_length",
        "available_stock_length",
        "used_stock_length",
        "utilization_rate",
        "welding_joint_quantity",
        "welding_pattern_type_quantity",
        "cutting_pattern_type_quantity",
        "kerf_loss",
        "remainder_length",
        "normalized_length_field_quantity",
        "must_use_stock_quantity",
        "must_use_used_quantity",
        "must_use_stock_length",
    )
    for name in aliases:
        if name not in summary or name not in totals:
            continue
        reported = _as_decimal(
            summary[name], issues, f"$solution.summary.{name}", default=Decimal(0)
        )
        if name in {
            "demand_length",
            "available_stock_length",
            "used_stock_length",
            "kerf_loss",
            "remainder_length",
            "must_use_stock_length",
        }:
            _require_integer_mm(reported, f"$solution.summary.{name}", issues)
        actual = Decimal(str(totals[name]))
        tolerance = RATE_TOLERANCE if name == "utilization_rate" else METRIC_LENGTH_TOLERANCE
        if abs(reported - actual) > tolerance:
            _issue(
                issues,
                "SUMMARY_METRIC_MISMATCH",
                f"汇总指标 {name} 报告 {_number(reported)}，独立复算为 {_number(actual)}。",
                f"$solution.summary.{name}",
            )
    if "target_reached" in summary and "target_reached" in totals:
        if _as_bool(summary["target_reached"]) != bool(totals["target_reached"]):
            _issue(
                issues,
                "SUMMARY_TARGET_REACHED_MISMATCH",
                f"汇总 target_reached 报告为 {_as_bool(summary['target_reached'])}，复算为 {totals['target_reached']}。",
                "$solution.summary.target_reached",
            )
    if isinstance(solution.get("groups"), list) and "normalized_length_field_quantity" not in summary:
        _issue(
            issues,
            "MISSING_NORMALIZATION_SUMMARY",
            "summary 必须输出 normalized_length_field_quantity。",
            "$solution.summary.normalized_length_field_quantity",
        )
    if str(solution.get("status", "")).strip().lower() == "target_reached" and not totals.get(
        "target_reached", False
    ):
        _issue(
            issues,
            "SOLUTION_STATUS_MISMATCH",
            "解状态为 target_reached，但独立复算未达到所有材质规格组的目标利用率。",
            "$solution.status",
        )


def _aggregate_metrics(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    additive = (
        "demand_length",
        "used_stock_length",
        "welding_joint_quantity",
        "welding_pattern_type_quantity",
        "cutting_pattern_type_quantity",
        "kerf_loss",
        "remainder_length",
        "unused_stock_length",
        "available_stock_length",
        "inventory_length",
        "pipe_quantity",
        "used_stock_quantity",
        "segment_piece_quantity",
        "must_use_stock_quantity",
        "must_use_used_quantity",
        "must_use_stock_length",
    )
    result: dict[str, Any] = {}
    for name in additive:
        result[name] = sum(
            (Decimal(str(group.get(name, 0))) for group in groups), Decimal(0)
        )
        result[name] = _json_number(result[name])
    demand = Decimal(str(result.get("demand_length", 0)))
    stock = Decimal(str(result.get("used_stock_length", 0)))
    result["utilization_rate"] = float(demand / stock) if stock > 0 else 0.0
    target_flags = [group["target_reached"] for group in groups if "target_reached" in group]
    if target_flags:
        result["target_reached"] = all(target_flags) and len(target_flags) == len(groups)
    normalization_paths = {
        path
        for group in groups
        for path in group.get("input_normalization_paths", [])
    }
    result["normalized_length_field_quantity"] = len(normalization_paths)
    return result


def _group_key(group: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(_first(group, "material", "Material") or "").strip(),
        str(_first(group, "specifications", "Specification") or "").strip(),
    )


def _solution_group_key(group: Mapping[str, Any]) -> tuple[str, str]:
    if isinstance(group.get("OriginalProblem"), Mapping):
        return _group_key(group["OriginalProblem"])
    return _group_key(group)


def _pipe_key(row: Mapping[str, Any]) -> str:
    explicit = _first(row, "pipe_id", "pipeId")
    if explicit not in (None, ""):
        return str(explicit).strip()
    figure = _first(row, "Parent_node", "parent_node", "FigureNumber", "figure_number")
    jlxh = _first(row, "jlxh", "Jlxh")
    cube = _first(row, "cube_no", "InPipeNumber", "in_pipe_number")
    return "|".join((_key_scalar(figure), _key_scalar(jlxh), _key_scalar(cube)))


def _match_pipe_key(row: Mapping[str, Any], pipe_by_key: Mapping[str, dict]) -> str | None:
    direct = _pipe_key(row)
    if direct in pipe_by_key:
        return direct
    # Some old data omitted cube_no from attached identifiers.  Only accept a
    # fallback when figure + jlxh uniquely identify one original pipe.
    figure = _key_scalar(
        _first(row, "Parent_node", "parent_node", "FigureNumber", "figure_number")
    )
    jlxh = _key_scalar(_first(row, "jlxh", "Jlxh"))
    candidates = [
        key
        for key, pipe in pipe_by_key.items()
        if _key_scalar(_first(pipe, "Parent_node", "parent_node", "figure_number")) == figure
        and _key_scalar(_first(pipe, "jlxh", "Jlxh")) == jlxh
    ]
    return candidates[0] if len(candidates) == 1 else None


def _parts(value: Any, issues: list[dict], path: str) -> list[Decimal]:
    if value is None:
        return []
    if isinstance(value, str):
        tokens = [token for token in re.split(r"[\s,|;+]+", value.strip()) if token]
        values: Iterable[Any] = tokens
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        _issue(issues, "INVALID_PART_LIST", "料段列表格式无效。", f"{path}.parts")
        return []
    result: list[Decimal] = []
    for index, item in enumerate(values):
        item_path = f"{path}.parts[{index}]"
        parsed = _as_decimal(
            item,
            issues,
            item_path,
            default=Decimal(0),
        )
        _require_integer_mm(parsed, item_path, issues)
        result.append(parsed)
    return result


def _coerce_unweldable_area(raw: Any) -> list[Any]:
    """Independently mirror the solver's several ``Unweldable_Area`` encodings.

    Field exports carry forbidden-weld zones as a list of ``[start, end]`` pairs,
    as a single flattened string ``"[0, 100],[1153, 1513]"``, or as a one-element
    list wrapping that string.  The verifier must recognise the same shapes so it
    audits exactly the intervals the solver normalised.
    """

    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        return _parse_area_string(raw)
    if isinstance(raw, (list, tuple)):
        if len(raw) == 1 and isinstance(raw[0], str):
            return _parse_area_string(raw[0])
        return list(raw)
    return []


def _parse_area_string(text: str) -> list[list[float]]:
    pairs: list[list[float]] = []
    for match in re.findall(r"\[([^\[\]]*)\]", text):
        nums = [chunk.strip() for chunk in match.split(",") if chunk.strip() != ""]
        if len(nums) == 2:
            try:
                pairs.append([float(nums[0]), float(nums[1])])
            except ValueError:
                continue
    return pairs


def _parse_intervals(value: Any, issues: list[dict], path: str) -> list[tuple[Decimal, Decimal]]:
    result: list[tuple[Decimal, Decimal]] = []
    for index, interval in enumerate(_coerce_unweldable_area(value)):
        if not isinstance(interval, Sequence) or isinstance(interval, str) or len(interval) < 2:
            _issue(
                issues,
                "INVALID_UNWELDABLE_INTERVAL",
                "禁焊区必须是 [起点, 终点]。",
                f"{path}[{index}]",
            )
            continue
        start = _normalise_input_mm(
            interval[0],
            "FLOOR_TO_INTEGER_MM",
            issues,
            f"{path}[{index}][0]",
            default=Decimal(0),
        )
        end = _normalise_input_mm(
            interval[1],
            "CEILING_TO_INTEGER_MM",
            issues,
            f"{path}[{index}][1]",
            default=Decimal(0),
        )
        if start > end:
            _issue(
                issues,
                "INVALID_UNWELDABLE_INTERVAL",
                f"禁焊区起点 {_number(start)} 大于终点 {_number(end)}。",
                f"{path}[{index}]",
            )
            start, end = end, start
        result.append((start, end))
    return result


def _optional_decimal_list(
    value: Any, issues: list[dict], path: str
) -> list[Decimal] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _issue(issues, "INVALID_WELD_POSITIONS", "焊口位置必须是数组。", path)
        return []
    result: list[Decimal] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        parsed = _as_decimal(item, issues, item_path, default=Decimal(0))
        _require_integer_mm(parsed, item_path, issues)
        result.append(parsed)
    return result


def _compare_optional_length(
    row: Mapping[str, Any],
    names: tuple[str, ...],
    expected: Decimal,
    code: str,
    label: str,
    path: str,
    issues: list[dict],
) -> None:
    raw = _first(row, *names)
    if raw is None:
        return
    reported = _as_decimal(raw, issues, f"{path}.{names[0]}", default=expected)
    _require_integer_mm(reported, f"{path}.{names[0]}", issues)
    if abs(reported - expected) > METRIC_LENGTH_TOLERANCE:
        _issue(
            issues,
            code,
            f"{label}报告 {_number(reported)}，复算为 {_number(expected)}。",
            f"{path}.{names[0]}",
        )


def _decimal_lists_equal(left: Sequence[Decimal], right: Sequence[Decimal]) -> bool:
    return len(left) == len(right) and all(
        abs(a - b) <= LENGTH_TOLERANCE for a, b in zip(left, right)
    )


def _require_integer_mm(
    value: Decimal, path: str, issues: list[dict]
) -> None:
    """Enforce the production output contract of whole millimetres only."""

    if value.is_finite() and value != value.to_integral_value():
        _issue(
            issues,
            "NON_INTEGER_MILLIMETRE_OUTPUT",
            f"长度或位置 {_number(value)} 不是整数毫米。",
            path,
        )


def _cumulative(parts: Sequence[Decimal]) -> list[Decimal]:
    result: list[Decimal] = []
    total = Decimal(0)
    for part in parts:
        total += part
        result.append(total)
    return result


def _as_decimal(
    value: Any,
    issues: list[dict],
    path: str,
    *,
    default: Decimal,
) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise InvalidOperation
        result = Decimal(str(value).strip())
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, TypeError):
        _issue(issues, "INVALID_NUMBER", f"数值 {value!r} 无效。", path)
        return default


def _normalise_input_mm(
    value: Any,
    rule: str,
    issues: list[dict],
    path: str,
    *,
    default: Decimal,
) -> Decimal:
    parsed = _as_decimal(value, issues, path, default=default)
    rounding = ROUND_FLOOR if rule == "FLOOR_TO_INTEGER_MM" else ROUND_CEILING
    return parsed.to_integral_value(rounding=rounding)


def _as_int(
    value: Any,
    issues: list[dict],
    path: str,
    *,
    default: int,
) -> int:
    decimal = _as_decimal(value, issues, path, default=Decimal(default))
    if decimal != decimal.to_integral_value():
        _issue(issues, "INVALID_INTEGER", f"数量 {value!r} 必须是整数。", path)
        return default
    return int(decimal)


def _as_stock_quantity(
    value: Any,
    issues: list[dict],
    path: str,
    *,
    default: int,
) -> int:
    # The supplied MOM production sample contains one historical OCR/typing
    # defect, stock_demand="S", which the owner explicitly confirmed means 5.
    # Keep this compatibility narrowly scoped to stock quantity only.
    if isinstance(value, str) and value.strip().upper() == "S":
        return 5
    return _as_int(value, issues, path, default=default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "是",
            "必用",
        }
    return bool(value)


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _nested_first(mapping: Mapping[str, Any], *paths: tuple[str, str]) -> Any:
    for outer, inner in paths:
        nested = mapping.get(outer)
        if isinstance(nested, Mapping) and inner in nested:
            return nested[inner]
    return None


def _has_any(mapping: Mapping[str, Any], *names: str) -> bool:
    return any(name in mapping for name in names)


def _join_path(prefix: str, suffix: str) -> str:
    return f"{prefix}.{suffix}" if prefix else suffix


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _key_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    try:
        decimal = Decimal(text)
        if decimal.is_finite():
            return _number(decimal)
    except InvalidOperation:
        pass
    return text


def _number(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _numbers(values: Sequence[Decimal]) -> str:
    return "[" + ", ".join(_number(value) for value in values) + "]"


def _json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _issue(
    issues: list[dict],
    code: str,
    message: str,
    path: str,
    severity: str = "error",
) -> None:
    issues.append(
        {"code": code, "message": message, "path": path, "severity": severity}
    )


__all__ = ["verify_solution"]
