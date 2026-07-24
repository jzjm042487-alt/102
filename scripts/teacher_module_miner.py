"""Leakage-safe normalization primitives for D5 teacher module mining.

This module intentionally separates corpus identities from exported reusable
structure. Case IDs select a train/validation/holdout split only; they are
never included in a module key or used when matching a future order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEACHER_ROOT = ROOT / "data" / "acceptance" / "legacy-fingerprints-all" / "cases"
DEFAULT_OUT = ROOT / "data" / "acceptance" / "d5_teacher_modules"


def split_case_ids(case_ids: Iterable[int], seed: int = 20260724) -> dict[str, list[int]]:
    """Return a stable 60/20/20 split independent of source ordering."""
    unique_ids = sorted({int(case_id) for case_id in case_ids})
    result: dict[str, list[int]] = {"train": [], "validation": [], "holdout": []}
    for case_id in unique_ids:
        digest = hashlib.sha256(f"{seed}:{case_id}".encode("ascii")).digest()
        bucket = int.from_bytes(digest[:8], "big") % 10
        split = "train" if bucket < 6 else "validation" if bucket < 8 else "holdout"
        result[split].append(case_id)
    return result


def _positive_int(value: object) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError("module lengths and quantities must be positive")
    return number


def _signature_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    length_key: str,
    sort_segments: bool,
) -> tuple[list[dict[str, object]], Counter[int]]:
    counts: Counter[tuple[int, tuple[int, ...]]] = Counter()
    flow: Counter[int] = Counter()
    for row in rows:
        length = _positive_int(row[length_key])
        segs = tuple(_positive_int(value) for value in row["segs"])
        if sort_segments:
            segs = tuple(sorted(segs))
        if (not sort_segments and sum(segs) != length) or (sort_segments and sum(segs) > length):
            raise ValueError("weld segments must equal pipe length and cuts cannot exceed stock")
        quantity = _positive_int(row.get("number", row.get("num", 1)))
        counts[(length, segs)] += quantity
        for segment in segs:
            flow[segment] += quantity
    signatures = [
        {"signature": [length, list(segs)], "number": quantity}
        for (length, segs), quantity in sorted(counts.items())
    ]
    return signatures, flow


def classify_recipe_rows(
    rows: Iterable[Mapping[str, Any]], *, main_coverage: float = 0.8
) -> list[dict[str, object]]:
    """Classify the highest-reuse recipe prefix as main and the rest as tail."""
    if not 0 < main_coverage <= 1:
        raise ValueError("main_coverage must be within (0, 1]")
    prepared = [
        {"signature": row["signature"], "number": _positive_int(row["number"])}
        for row in rows
    ]
    prepared.sort(key=lambda row: (-int(row["number"]), repr(row["signature"])))
    total = sum(int(row["number"]) for row in prepared)
    cumulative = 0
    for row in prepared:
        row["role"] = "main" if cumulative / total < main_coverage else "tail"
        cumulative += int(row["number"])
    return prepared


def build_recipe_catalog(normalized_samples: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, object]]]:
    """Count transferable recipe roles without retaining absolute dimensions."""
    cut_support: Counter[tuple[int, str]] = Counter()
    weld_support: Counter[tuple[int, str]] = Counter()
    for sample in normalized_samples:
        if not sample:
            continue
        seen_cut: set[tuple[int, str]] = set()
        seen_weld: set[tuple[int, str]] = set()
        for row in sample["cut_signatures"]:
            stock_len, segments = row["signature"]
            arity = len(segments)
            role = "near_fill" if sum(segments) < stock_len else "exact_fill"
            seen_cut.add((arity, role))
        for row in sample["weld_signatures"]:
            _, segments = row["signature"]
            arity = len(segments)
            role = "one_segment" if arity == 1 else "two_segment" if arity == 2 else "multi_segment"
            seen_weld.add((arity, role))
        cut_support.update(seen_cut)
        weld_support.update(seen_weld)
    return {
        "cut_roles": [
            {"arity": arity, "role": role, "support_cases": support}
            for (arity, role), support in sorted(cut_support.items())
        ],
        "weld_roles": [
            {"arity": arity, "role": role, "support_cases": support}
            for (arity, role), support in sorted(weld_support.items())
        ],
    }


def build_module_dictionary(
    normalized_samples: Iterable[Mapping[str, Any]], *, min_support: int = 2
) -> dict[str, object]:
    """Aggregate closed-loop module shapes from training samples only."""
    if min_support < 1:
        raise ValueError("min_support must be at least one")
    counts: Counter[str] = Counter()
    examples: dict[str, dict[str, object]] = {}
    n_samples = 0
    for sample in normalized_samples:
        if not sample:
            continue
        key = repr((sample["cut_signatures"], sample["weld_signatures"]))
        counts[key] += 1
        examples.setdefault(
            key,
            {
                "cut_signatures": sample["cut_signatures"],
                "weld_signatures": sample["weld_signatures"],
                "segment_flow": sample["segment_flow"],
            },
        )
        n_samples += 1
    prototypes = []
    for key, support in counts.most_common():
        if support < min_support:
            continue
        prototype = dict(examples[key])
        prototype["module_id"] = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        prototype["support_cases"] = support
        prototypes.append(prototype)
    return {"n_samples": n_samples, "min_support": min_support, "prototypes": prototypes}


def normalize_teacher_case(sample: Mapping[str, Any]) -> dict[str, object] | None:
    """Normalize a reconciled teacher layout into a reusable, ID-free module.

    Cuts are canonicalized as unordered segment multisets. Weld sequences keep
    their order because order can represent a process recipe. A layout is a
    closed loop only if its cuts supply every welded segment exactly.
    """
    try:
        cuts, supplied = _signature_rows(
            sample.get("cuts", []), length_key="stock_len", sort_segments=True
        )
        welds, consumed = _signature_rows(
            sample.get("welds", []), length_key="pipe_len", sort_segments=False
        )
    except (KeyError, TypeError, ValueError):
        return None

    if not cuts or not welds or any(supplied[segment] < need for segment, need in consumed.items()):
        return None
    flow = {
        str(segment): supplied[segment] - consumed[segment]
        for segment in sorted(set(supplied) | set(consumed))
    }
    return {
        "cut_signatures": cuts,
        "weld_signatures": welds,
        "segment_flow": flow,
        "closed_loop": all(balance == 0 for balance in flow.values()),
    }


def _sample_from_teacher_columns(teacher: Mapping[str, Any]) -> dict[str, object]:
    """Convert existing extraction output to the ID-free normalizer input."""
    return {
        "cuts": teacher.get("cut_columns", []),
        "welds": teacher.get("weld_columns", []),
    }


def _case_id_from_path(path: Path) -> int:
    return int(path.parent.name.split("_", 1)[0])


def filter_paths_for_case_ids(paths: Iterable[Path], case_ids: set[int]) -> list[Path]:
    """Filter extracted teacher layouts by an explicit case set."""
    return [path for path in paths if _case_id_from_path(path) in case_ids]


def mine_teacher_modules(
    teacher_root: Path = DEFAULT_TEACHER_ROOT,
    *,
    min_support: int = 2,
    case_ids: set[int] | None = None,
) -> tuple[dict[str, object], dict[str, list[int]], dict[str, int]]:
    """Build prototypes solely from the training partition of extracted layouts."""
    paths = sorted(teacher_root.glob("*/teacher_columns.json"))
    if case_ids is not None:
        paths = filter_paths_for_case_ids(paths, case_ids)
    split = split_case_ids(_case_id_from_path(path) for path in paths)
    train_ids = set(split["train"])
    normalized_train: list[dict[str, object]] = []
    counts = {"seen": 0, "normalized": 0, "rejected": 0, "train": 0}
    for path in paths:
        counts["seen"] += 1
        try:
            teacher = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["rejected"] += 1
            continue
        normalized = normalize_teacher_case(_sample_from_teacher_columns(teacher))
        if normalized is None:
            counts["rejected"] += 1
            continue
        counts["normalized"] += 1
        if _case_id_from_path(path) in train_ids:
            normalized_train.append(normalized)
            counts["train"] += 1
    dictionary = build_module_dictionary(normalized_train, min_support=min_support)
    dictionary["recipe_catalog"] = build_recipe_catalog(normalized_train)
    return dictionary, split, counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Mine leakage-safe teacher module prototypes")
    parser.add_argument("--teacher-root", type=Path, default=DEFAULT_TEACHER_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--min-support", type=int, default=2)
    parser.add_argument("--tier", default="", help="optional difficulty tier, e.g. D5-极难")
    args = parser.parse_args()

    selected_ids: set[int] | None = None
    if args.tier:
        pools = json.loads(
            (ROOT / "data" / "legacy-cases" / "difficulty_pools.json").read_text(encoding="utf-8")
        )
        try:
            selected_ids = {int(case_id) for case_id in pools["pools"][args.tier]}
        except KeyError as exc:
            raise SystemExit(f"unknown tier: {args.tier}") from exc
    dictionary, split, counts = mine_teacher_modules(
        args.teacher_root, min_support=args.min_support, case_ids=selected_ids
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "split_manifest.json").write_text(
        json.dumps(split, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "module_dictionary.json").write_text(
        json.dumps(dictionary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "mining_summary.json").write_text(
        json.dumps(
            {"tier": args.tier or "all", "counts": counts, "prototypes": len(dictionary["prototypes"])},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"mined seen={counts['seen']} normalized={counts['normalized']} "
        f"train={counts['train']} prototypes={len(dictionary['prototypes'])}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
