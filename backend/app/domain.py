"""Domain model and lossless input normalisation for pipe nesting.

The optimiser never calculates with binary floating point lengths.  MOM decimal
lengths are normalised with exact Decimal floor/ceiling rules and an audit trail;
the solver and every public length output remain integer millimetres.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


LENGTH_SCALE = 1  # Production coordinates are integer millimetres.
LENGTH_QUANTUM_MM = Decimal(1)
# Process defaults (JB/T 6509 family): at most one splice weld per 3000 mm of
# overall tube length, and never more than 13 welds on a single tube.  The 3000 mm
# interval matches the shop's ``pipe_length / 3000~4000mm`` rule of thumb at its
# tighter end, so long serpentine tubes retain enough splice points to pack to
# target utilisation.  Both are configurable per payload (global) and per group.
DEFAULT_WELD_INTERVAL_MM = 3000
DEFAULT_MAX_JOINTS_CAP = 13
# Cutting kerf (saw width) defaults to zero because the field MOM export and the
# incumbent nesting software both plan cut lengths kerf-free -- any real blade
# loss is already folded into the part lengths.  Callers that must model a
# physical kerf pass ``BladeMargin`` explicitly.
DEFAULT_BLADE_MARGIN_MM = 0


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be numeric, got {value!r}")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric, got {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class InputNormalization:
    path: str
    original: Any
    normalized: int
    rule: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "original": self.original,
            "normalized": self.normalized,
            "rule": self.rule,
        }


def _json_original(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _record_normalization(
    records: list[InputNormalization] | None,
    *,
    path: str,
    original: Any,
    normalized: int,
    rule: str,
) -> None:
    if records is None:
        return
    record = InputNormalization(path, _json_original(original), normalized, rule)
    if record not in records:
        records.append(record)


def to_units(
    value: Any,
    *,
    field_name: str,
    rounding: str = "ceiling",
    normalizations: list[InputNormalization] | None = None,
) -> int:
    """Normalize a length to an auditable integer millimetre coordinate.

    Scalar lengths and forbidden-zone ends use mathematical ceiling.  A caller
    may request floor only for a forbidden-zone start so the resulting interval
    expands conservatively.
    """

    number = _decimal(value, field_name=field_name)
    if rounding == "ceiling":
        mode = ROUND_CEILING
        rule = "CEILING_TO_INTEGER_MM"
    elif rounding == "floor":
        mode = ROUND_FLOOR
        rule = "FLOOR_TO_INTEGER_MM"
    else:
        raise ValueError(f"unknown length normalization rule {rounding!r}")
    normalized = int(number.to_integral_value(rounding=mode))
    if number != Decimal(normalized):
        _record_normalization(
            normalizations,
            path=field_name,
            original=value,
            normalized=normalized,
            rule=rule,
        )
    return normalized


def from_units(value: int) -> int:
    """Return an integer JSON number in millimetres."""

    if isinstance(value, bool) or int(value) != value:
        raise ValueError(f"solver length must be an integer millimetre value, got {value!r}")
    return int(value)


def normalise_rate(value: Any, *, field_name: str = "Target_Util_Rate") -> float:
    rate = _decimal(value, field_name=field_name)
    if rate > 1:
        rate /= 100
    if rate <= 0 or rate > 1:
        raise ValueError(f"{field_name} must be in (0, 1] or (0, 100], got {value!r}")
    return float(rate)


def _positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    number = _decimal(value, field_name=field_name)
    integral = number.to_integral_value()
    if number != integral or integral <= 0:
        raise ValueError(f"{field_name} must be a positive integer, got {value!r}")
    return int(integral)


def _nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    number = _decimal(value, field_name=field_name)
    integral = number.to_integral_value()
    if number != integral or integral < 0:
        raise ValueError(
            f"{field_name} must be a non-negative integer, got {value!r}"
        )
    return int(integral)


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    start: int
    end: int

    def contains(self, position: int) -> bool:
        return self.start <= position <= self.end


@dataclass(frozen=True, slots=True)
class PipeDemand:
    pipe_id: str
    figure_number: str
    jlxh: str
    cube_no: str
    length: int
    demand: int
    max_joints: int
    forbidden: tuple[Interval, ...]
    parent_node: str = ""
    com_draw_number: str = ""
    scr_draw_number: str = ""

    def weld_allowed(self, position: int) -> bool:
        return 0 < position < self.length and not any(
            interval.contains(position) for interval in self.forbidden
        )


@dataclass(frozen=True, slots=True)
class StockSupply:
    length: int
    quantity: int
    must_use_quantity: int = 0


@dataclass(frozen=True, slots=True)
class MaterialGroup:
    material: str
    specifications: str
    target_rate: float
    pipes: tuple[PipeDemand, ...]
    stocks: tuple[StockSupply, ...]
    blade_margin: int
    min_weld_distance: int
    min_reusable_remnant: int
    min_cut_length: int = 0
    kerf_mode: str = "BETWEEN_PARTS"
    input_normalizations: tuple[InputNormalization, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def demand_length(self) -> int:
        return sum(pipe.length * pipe.demand for pipe in self.pipes)

    @property
    def stock_length(self) -> int:
        return sum(stock.length * stock.quantity for stock in self.stocks)


@dataclass(frozen=True, slots=True)
class NestingProblem:
    task_id: str
    groups: tuple[MaterialGroup, ...]
    source: Mapping[str, Any]


def _merge_intervals(intervals: Iterable[Interval], pipe_length: int) -> tuple[Interval, ...]:
    clipped = sorted(
        (
            Interval(max(0, interval.start), min(pipe_length, interval.end))
            for interval in intervals
            if interval.end >= 0 and interval.start <= pipe_length
        ),
        key=lambda item: (item.start, item.end),
    )
    merged: list[Interval] = []
    for interval in clipped:
        if interval.end < interval.start:
            continue
        if merged and interval.start <= merged[-1].end + 1:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, interval.end))
        else:
            merged.append(interval)
    return tuple(merged)


def _normalize_unweldable_area(raw: Any) -> list[Any]:
    """Coerce the several real-world encodings of ``Unweldable_Area`` into a
    uniform list of ``[start, end]`` pairs.

    Field exports carry the forbidden-weld zones in four shapes:
      * empty / missing -> no zones;
      * list of pairs ``[[0, 100], ...]`` -> already canonical;
      * a single string ``"[0, 100],[1153, 1513]"`` -> the JSON brackets were
        flattened into text and must be re-parsed;
      * a one-element list wrapping that same string.
    """

    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        return _parse_area_string(raw)
    if isinstance(raw, (list, tuple)):
        if len(raw) == 1 and isinstance(raw[0], str):
            return _parse_area_string(raw[0])
        return list(raw)
    return list(raw)


def _parse_area_string(text: str) -> list[list[float]]:
    """Parse ``"[0, 100],[1153, 1513]"`` into ``[[0, 100], [1153, 1513]]``."""

    pairs: list[list[float]] = []
    for match in re.findall(r"\[([^\[\]]*)\]", text):
        nums = [chunk.strip() for chunk in match.split(",") if chunk.strip() != ""]
        if len(nums) == 2:
            pairs.append([float(nums[0]), float(nums[1])])
    return pairs


def _parse_pipe(
    raw: Mapping[str, Any],
    index: int,
    group_path: str,
    normalizations: list[InputNormalization],
    *,
    weld_interval: int,
    max_joints_cap: int,
    pipe_warnings: list[str],
) -> PipeDemand:
    prefix = f"{group_path}.Pipe[{index}]" if group_path else f"Pipe[{index}]"
    length = to_units(
        raw.get("pipe_length"),
        field_name=f"{prefix}.pipe_length",
        normalizations=normalizations,
    )
    if length <= 0:
        raise ValueError(f"{prefix}.pipe_length must be positive")
    demand = _positive_int(raw.get("pipe_demand"), field_name=f"{prefix}.pipe_demand")
    max_joints_raw = raw.get("Max_Weldingjoint_Number", 2_000)
    # Field exports frequently leave this blank; an empty/None value means "no
    # explicit limit" and defers entirely to the process rule below.
    if max_joints_raw is None or (
        isinstance(max_joints_raw, str) and not max_joints_raw.strip()
    ):
        max_joints_raw = 2_000
    input_joints = _nonnegative_int(
        max_joints_raw, field_name=f"{prefix}.Max_Weldingjoint_Number"
    )
    # A splice weld is only permitted on a straight section, at most one per
    # weld_interval of overall length, and never above the hard cap.  The input
    # value is treated as an upper bound only: a placeholder such as 2000 is
    # silently clamped by the process rule instead of driving the model.
    process_limit = min(length // weld_interval, max_joints_cap)
    max_joints = min(input_joints, process_limit)
    if input_joints > process_limit:
        pipe_warnings.append(
            f"{prefix}.Max_Weldingjoint_Number={input_joints} exceeds the process "
            f"limit {process_limit} (length {from_units(length)}mm / interval "
            f"{from_units(weld_interval)}mm, cap {max_joints_cap}); using {max_joints}"
        )

    intervals: list[Interval] = []
    for area_index, area in enumerate(_normalize_unweldable_area(raw.get("Unweldable_Area"))):
        if not isinstance(area, (list, tuple)) or len(area) != 2:
            raise ValueError(f"{prefix}.Unweldable_Area[{area_index}] must be [start, end]")
        start = to_units(
            area[0],
            field_name=f"{prefix}.Unweldable_Area[{area_index}][0]",
            rounding="floor",
            normalizations=normalizations,
        )
        end = to_units(
            area[1],
            field_name=f"{prefix}.Unweldable_Area[{area_index}][1]",
            rounding="ceiling",
            normalizations=normalizations,
        )
        if end < start:
            # Field exports occasionally list a forbidden-weld zone with its
            # endpoints reversed.  Treat it as an ordering slip rather than
            # failing the whole group, and record why.
            pipe_warnings.append(
                f"{prefix}.Unweldable_Area[{area_index}] had end before start "
                f"({from_units(start)}>{from_units(end)}); swapped"
            )
            start, end = end, start
        if end == start:
            continue
        intervals.append(Interval(start, end))

    figure = str(raw.get("figure_number", "")).strip()
    parent = str(raw.get("Parent_node", figure)).strip()
    jlxh = str(raw.get("jlxh", "")).strip()
    cube = str(raw.get("cube_no", "")).strip()
    pipe_id = "|".join((parent or figure or f"pipe-{index}", jlxh, cube))
    return PipeDemand(
        pipe_id=pipe_id,
        figure_number=figure,
        jlxh=jlxh,
        cube_no=cube,
        length=length,
        demand=demand,
        max_joints=max_joints,
        forbidden=_merge_intervals(intervals, length),
        parent_node=parent,
        com_draw_number=str(raw.get("Com_draw_number", "")).strip(),
        scr_draw_number=str(raw.get("Scr_draw_number", "")).strip(),
    )


def _must_use_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "是", "真"}:
            return True
        if normalized in {"0", "false", "no", "n", "否", "假", ""}:
            return False
    raise ValueError(
        f"{field_name} must be 是/否, true/false, or 1/0, got {value!r}"
    )


def _parse_kerf_mode(value: Any, *, field_name: str) -> str:
    """Resolve the KerfMode process flag.

    ``BETWEEN_PARTS`` charges a blade kerf only between two adjacent parts
    (``n`` parts -> ``n-1`` cuts).  ``WITH_REMAINDER`` additionally charges the
    final cut that separates the last part from a positive remainder, so a bar
    that is not cut to the very end loses one more kerf.  The mode is a process
    definition and must never be hard-coded.
    """

    if value is None:
        return "BETWEEN_PARTS"
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be BETWEEN_PARTS or WITH_REMAINDER, got {value!r}"
        )
    normalized = value.strip().upper()
    aliases = {
        "BETWEEN_PARTS": "BETWEEN_PARTS",
        "BETWEEN": "BETWEEN_PARTS",
        "WITH_REMAINDER": "WITH_REMAINDER",
        "REMAINDER": "WITH_REMAINDER",
    }
    if normalized not in aliases:
        raise ValueError(
            f"{field_name} must be BETWEEN_PARTS or WITH_REMAINDER, got {value!r}"
        )
    return aliases[normalized]


def _parse_stocks(
    raw_stocks: Any,
    warnings: list[str],
    group_path: str,
    normalizations: list[InputNormalization],
) -> tuple[StockSupply, ...]:
    if not isinstance(raw_stocks, list) or not raw_stocks:
        raise ValueError("Stock must be a non-empty list")
    quantities: dict[int, int] = {}
    required: dict[int, int] = {}
    for index, raw in enumerate(raw_stocks):
        prefix = f"{group_path}.Stock[{index}]" if group_path else f"Stock[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{prefix} must be an object")
        length = to_units(
            raw.get("stock_length"),
            field_name=f"{prefix}.stock_length",
            normalizations=normalizations,
        )
        if length <= 0:
            raise ValueError(f"{prefix}.stock_length must be positive")
        demand_raw = raw.get("stock_demand")
        if isinstance(demand_raw, str) and demand_raw.strip().upper() == "S":
            # Confirmed legacy MOM typo in the supplied production data.
            quantity = 5
            warnings.append(
                f"STOCK_DEMAND_S_NORMALIZED: {prefix}.stock_demand 'S' was interpreted as 5"
            )
        else:
            quantity = _positive_int(
                demand_raw, field_name=f"{prefix}.stock_demand"
            )
        must_use_key = "must_use" if "must_use" in raw else "Is_Must"
        must_use = _must_use_bool(
            raw.get(must_use_key, "0"), field_name=f"{prefix}.{must_use_key}"
        )
        quantities[length] = quantities.get(length, 0) + quantity
        if must_use:
            required[length] = required.get(length, 0) + quantity
    return tuple(
        StockSupply(length, quantity, required.get(length, 0))
        for length, quantity in sorted(quantities.items())
    )


def _payload_root(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]], str, list[str]]:
    if "input" in payload:
        root = payload["input"]
        if not isinstance(root, Mapping):
            raise ValueError("input must be an object")
        data = root.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError("input.data must be a non-empty list")
        return root, data, "input", [f"input.data[{index}]" for index in range(len(data))]
    if "OriginalProblem" in payload:
        root = payload["OriginalProblem"]
        if not isinstance(root, Mapping):
            raise ValueError("OriginalProblem must be an object")
        return root, [root], "OriginalProblem", ["OriginalProblem"]
    data = payload.get("data")
    if isinstance(data, list) and data:
        return payload, data, "", [f"data[{index}]" for index in range(len(data))]
    if "Pipe" in payload and "Stock" in payload:
        return payload, [payload], "", [""]
    raise ValueError("expected input.data, data, OriginalProblem, or a single material group")


def _stable_task_id(payload: Mapping[str, Any], root: Mapping[str, Any]) -> str:
    supplied = root.get("id") or payload.get("id")
    if supplied:
        return str(supplied)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"nest-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


def parse_problem(payload: dict[str, Any]) -> NestingProblem:
    """Validate and normalise all accepted MOM input envelopes."""

    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    root, raw_groups, root_path, group_paths = _payload_root(payload)
    path = lambda name: f"{root_path}.{name}" if root_path else name
    global_normalizations: list[InputNormalization] = []
    nest_param = root.get("NestParam") if isinstance(root.get("NestParam"), Mapping) else {}
    blade_margin_raw = nest_param.get(
        "BladeMargin", root.get("BladeMargin", DEFAULT_BLADE_MARGIN_MM)
    )
    blade_margin_path = (
        path("NestParam.BladeMargin")
        if "BladeMargin" in nest_param
        else path("BladeMargin")
    )
    blade_margin = to_units(
        blade_margin_raw,
        field_name=blade_margin_path,
        normalizations=global_normalizations,
    )
    if blade_margin < 0:
        raise ValueError(f"{blade_margin_path} must be non-negative")
    min_weld_distance = to_units(
        root.get("Min_Welding_Length", 500),
        field_name=path("Min_Welding_Length"),
        normalizations=global_normalizations,
    )
    if min_weld_distance < 0:
        raise ValueError(f"{path('Min_Welding_Length')} must be non-negative")
    min_remnant = to_units(
        root.get("Min_Reusable_Remnant_Length", LENGTH_QUANTUM_MM),
        field_name=path("Min_Reusable_Remnant_Length"),
        normalizations=global_normalizations,
    )
    if min_remnant < 0:
        raise ValueError(f"{path('Min_Reusable_Remnant_Length')} must be non-negative")

    min_cut_length = to_units(
        root.get("Min_Cut_Length", 0),
        field_name=path("Min_Cut_Length"),
        normalizations=global_normalizations,
    )
    if min_cut_length < 0:
        raise ValueError(f"{path('Min_Cut_Length')} must be non-negative")

    # Process rule (JB/T 6509 family): a serpentine tube may carry at most one
    # splice weld per Weld_Interval_mm of overall length, and never more than
    # Max_Joints_Cap regardless of length.  These bound the joint count so that
    # candidate generation cannot explode on placeholder inputs such as 2000.
    weld_interval = to_units(
        root.get("Weld_Interval_mm", DEFAULT_WELD_INTERVAL_MM),
        field_name=path("Weld_Interval_mm"),
        normalizations=global_normalizations,
    )
    if weld_interval <= 0:
        raise ValueError(f"{path('Weld_Interval_mm')} must be positive")
    max_joints_cap = _nonnegative_int(
        root.get("Max_Joints_Cap", DEFAULT_MAX_JOINTS_CAP),
        field_name=path("Max_Joints_Cap"),
    )
    kerf_mode = _parse_kerf_mode(
        nest_param.get("KerfMode", root.get("KerfMode")),
        field_name=(
            path("NestParam.KerfMode")
            if "KerfMode" in nest_param
            else path("KerfMode")
        ),
    )
    # These legacy process-distance inputs are currently not active constraints,
    # but they are still lengths and must not smuggle fractional millimetres into
    # a future ruleset.
    for field_name in (
        "Adjacent_Distance",
        "Corner_Distance",
        "Adjacent_Corner_Distance",
    ):
        if field_name in root:
            to_units(
                root[field_name],
                field_name=path(field_name),
                normalizations=global_normalizations,
            )

    groups: list[MaterialGroup] = []
    for group_index, raw_group in enumerate(raw_groups):
        group_path = group_paths[group_index]
        if not isinstance(raw_group, Mapping):
            raise ValueError(f"data[{group_index}] must be an object")
        warnings: list[str] = []
        group_normalizations = list(global_normalizations)
        for field_name in (
            "Adjacent_Distance",
            "Corner_Distance",
            "Adjacent_Corner_Distance",
        ):
            if field_name in raw_group:
                field_path = (
                    f"{group_path}.{field_name}" if group_path else field_name
                )
                to_units(
                    raw_group[field_name],
                    field_name=field_path,
                    normalizations=group_normalizations,
                )
        raw_pipes = raw_group.get("Pipe")
        if not isinstance(raw_pipes, list) or not raw_pipes:
            raise ValueError(f"data[{group_index}].Pipe must be a non-empty list")
        stocks = _parse_stocks(
            raw_group.get("Stock"), warnings, group_path, group_normalizations
        )
        target_raw = raw_group.get("Target_Util_Rate", root.get("Target_Util_Rate", 99.25))
        group_nest_param = (
            raw_group.get("NestParam")
            if isinstance(raw_group.get("NestParam"), Mapping)
            else {}
        )
        group_blade_raw = group_nest_param.get(
            "BladeMargin", raw_group.get("BladeMargin", blade_margin_raw)
        )
        group_blade_path = (
            f"{group_path}.NestParam.BladeMargin"
            if "BladeMargin" in group_nest_param
            else (
                f"{group_path}.BladeMargin"
                if "BladeMargin" in raw_group
                else blade_margin_path
            )
        )
        group_blade = to_units(
            group_blade_raw,
            field_name=group_blade_path,
            normalizations=group_normalizations,
        )
        group_min_weld = to_units(
            raw_group.get("Min_Welding_Length", root.get("Min_Welding_Length", 500)),
            field_name=(
                f"{group_path}.Min_Welding_Length"
                if "Min_Welding_Length" in raw_group
                else path("Min_Welding_Length")
            ),
            normalizations=group_normalizations,
        )
        group_min_remnant = to_units(
            raw_group.get(
                "Min_Reusable_Remnant_Length",
                root.get("Min_Reusable_Remnant_Length", LENGTH_QUANTUM_MM),
            ),
            field_name=(
                f"{group_path}.Min_Reusable_Remnant_Length"
                if "Min_Reusable_Remnant_Length" in raw_group
                else path("Min_Reusable_Remnant_Length")
            ),
            normalizations=group_normalizations,
        )
        group_min_cut = to_units(
            raw_group.get("Min_Cut_Length", root.get("Min_Cut_Length", 0)),
            field_name=(
                f"{group_path}.Min_Cut_Length"
                if "Min_Cut_Length" in raw_group
                else path("Min_Cut_Length")
            ),
            normalizations=group_normalizations,
        )
        group_weld_interval = to_units(
            raw_group.get(
                "Weld_Interval_mm",
                root.get("Weld_Interval_mm", DEFAULT_WELD_INTERVAL_MM),
            ),
            field_name=(
                f"{group_path}.Weld_Interval_mm"
                if "Weld_Interval_mm" in raw_group
                else path("Weld_Interval_mm")
            ),
            normalizations=group_normalizations,
        )
        if group_weld_interval <= 0:
            raise ValueError(f"{group_path}.Weld_Interval_mm must be positive")
        group_max_joints_cap = _nonnegative_int(
            raw_group.get(
                "Max_Joints_Cap", root.get("Max_Joints_Cap", DEFAULT_MAX_JOINTS_CAP)
            ),
            field_name=(
                f"{group_path}.Max_Joints_Cap"
                if "Max_Joints_Cap" in raw_group
                else path("Max_Joints_Cap")
            ),
        )
        pipe_warnings: list[str] = []
        pipes = tuple(
            _parse_pipe(
                raw,
                index,
                group_path,
                group_normalizations,
                weld_interval=group_weld_interval,
                max_joints_cap=group_max_joints_cap,
                pipe_warnings=pipe_warnings,
            )
            for index, raw in enumerate(raw_pipes)
        )
        if len({pipe.pipe_id for pipe in pipes}) != len(pipes):
            raise ValueError(
                f"data[{group_index}] contains duplicate pipe identity Parent_node|jlxh|cube_no"
            )
        warnings.extend(pipe_warnings)
        if group_blade < 0:
            raise ValueError(f"{group_blade_path} must be non-negative")
        if group_min_weld < 0:
            raise ValueError(
                f"{group_path}.Min_Welding_Length must be non-negative"
            )
        if group_min_remnant < 0:
            raise ValueError(
                f"{group_path}.Min_Reusable_Remnant_Length must be non-negative"
            )
        if group_min_cut < 0:
            raise ValueError(f"{group_path}.Min_Cut_Length must be non-negative")
        group_kerf_mode = _parse_kerf_mode(
            group_nest_param.get(
                "KerfMode",
                raw_group.get("KerfMode", nest_param.get("KerfMode", root.get("KerfMode"))),
            ),
            field_name=(
                f"{group_path}.NestParam.KerfMode"
                if "KerfMode" in group_nest_param
                else (
                    f"{group_path}.KerfMode"
                    if "KerfMode" in raw_group
                    else (
                        path("NestParam.KerfMode")
                        if "KerfMode" in nest_param
                        else path("KerfMode")
                    )
                )
            ),
        )
        group_normalizations.sort(key=lambda item: (item.path, item.rule))
        if group_normalizations:
            warnings.append(
                "INPUT_LENGTH_NORMALIZATION_SUMMARY: "
                f"{len(group_normalizations)} field(s) normalized; "
                "see input_normalizations"
            )
        must_use_quantity = sum(stock.must_use_quantity for stock in stocks)
        if must_use_quantity:
            warnings.append(
                "MUST_USE_INVENTORY_SUMMARY: "
                f"{must_use_quantity} stock bar(s) constrained as mandatory"
            )
        group = MaterialGroup(
            material=str(raw_group.get("material", root.get("material", ""))).strip(),
            specifications=str(
                raw_group.get("specifications", root.get("specifications", ""))
            ).strip(),
            target_rate=normalise_rate(target_raw),
            pipes=pipes,
            stocks=stocks,
            blade_margin=group_blade,
            min_weld_distance=group_min_weld,
            min_reusable_remnant=group_min_remnant,
            min_cut_length=group_min_cut,
            kerf_mode=group_kerf_mode,
            input_normalizations=tuple(group_normalizations),
            warnings=tuple(warnings),
        )
        if group.stock_length < group.demand_length:
            # Total stock is provably too short for total demand.  This used to
            # abort the whole request, but a single short group must not sink
            # every other (solvable) group.  Record it as a warning; the solver's
            # length-shortfall prefilter turns it into a per-group INFEASIBLE
            # verdict with an actionable material-top-up recommendation.
            group = replace(
                group,
                warnings=group.warnings
                + (
                    "LENGTH_SHORTFALL: stock shorter than demand before kerf "
                    f"({from_units(group.stock_length)} < {from_units(group.demand_length)} mm)",
                ),
            )
        groups.append(group)

    return NestingProblem(
        task_id=_stable_task_id(payload, root),
        groups=tuple(groups),
        source=payload,
    )


__all__ = [
    "InputNormalization",
    "Interval",
    "LENGTH_QUANTUM_MM",
    "LENGTH_SCALE",
    "MaterialGroup",
    "NestingProblem",
    "PipeDemand",
    "StockSupply",
    "from_units",
    "normalise_rate",
    "parse_problem",
    "to_units",
]
