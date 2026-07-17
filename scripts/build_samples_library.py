"""Build a compact frontend sample library from a MOM export.

The MOM export (``RECORDS`` array, tens of MB) is far too large to ship to the
browser.  This script distills each record into the minimum the frontend needs
to offer a four-level cascading picker -- 部件图号 (Com_draw_number) -> 图号
(figure_number) -> 材质 (material) -> 规格 (specifications) -> a concrete record
-- and to load both:

  * the solver input (the record's ``MOMPROBLEMJSON``; the backend already
    accepts a bare ``{Pipe, Stock, ...}`` group), and
  * the legacy nesting result (the record's ``MOMRESULTJSON``; the frontend's
    ``normalizeLegacyGroup`` already understands ``GeneralInfo`` / ``Result``).

Usage:
    python scripts/build_samples_library.py <source.json> [-o frontend/samples.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _legacy_is_solved(result: dict[str, Any]) -> bool:
    """A record counts as solved when the legacy engine returned a real plan."""

    general = result.get("GeneralInfo") or {}
    if not str(general.get("Result", "")).strip().lower().startswith("success"):
        return False
    try:
        if float(general.get("UtilRate") or 0) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    cutting = (result.get("Result") or {}).get("CuttingPattern") or {}
    return bool(cutting.get("CuttingPipe"))


def _round_robin_by_com(samples: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Pick up to ``limit`` samples, spreading them across 部件图号 (Com_draw_number)
    so the picker shows a diverse tree instead of one dominant component."""

    by_com: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_com[sample["com"]].append(sample)
    for bucket in by_com.values():
        bucket.sort(key=lambda s: (s["material"], s["spec"], s["id"]))

    picked: list[dict[str, Any]] = []
    coms = sorted(by_com)
    cursor = {com: 0 for com in coms}
    while len(picked) < limit:
        progressed = False
        for com in coms:
            index = cursor[com]
            if index < len(by_com[com]):
                picked.append(by_com[com][index])
                cursor[com] = index + 1
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
    return picked


def _slim_legacy(result: dict[str, Any]) -> dict[str, Any]:
    """Keep only the parts of MOMRESULTJSON the comparison view consumes."""

    general = result.get("GeneralInfo") or {}
    inner = result.get("Result") or {}
    return {
        "GeneralInfo": {
            "Material": general.get("Material"),
            "Specification": general.get("Specification"),
            "UtilRate": general.get("UtilRate"),
            "WeldingJointQuantity": general.get("WeldingJointQuantity"),
            "PipeLength_Of_SingleMaterialSpecification": general.get(
                "PipeLength_Of_SingleMaterialSpecification"
            ),
            "StockLength_Of_SingleMaterialSpecification": general.get(
                "StockLength_Of_SingleMaterialSpecification"
            ),
        },
        "Result": {
            "WeldingPattern": inner.get("WeldingPattern") or {},
            "CuttingPattern": inner.get("CuttingPattern") or {},
            "UnusedMaterials": inner.get("UnusedMaterials") or [],
        },
    }


def build(source: Path, *, only_solved: bool = False, limit: int | None = None) -> dict[str, Any]:
    with source.open(encoding="utf-8") as fh:
        data = json.load(fh)
    records = data.get("RECORDS") or []

    samples: list[dict[str, Any]] = []
    for record in records:
        problem_raw = record.get("MOMPROBLEMJSON")
        result_raw = record.get("MOMRESULTJSON")
        if not problem_raw or not result_raw:
            continue
        try:
            problem = json.loads(problem_raw)
            result = json.loads(result_raw)
        except (json.JSONDecodeError, TypeError):
            continue

        if only_solved and not _legacy_is_solved(result):
            continue

        pipes = problem.get("Pipe") or []
        if not pipes:
            continue

        material = _clean(problem.get("material") or record.get("MOMMATERIAL"))
        spec = _clean(problem.get("specifications"))
        com = _clean(pipes[0].get("Com_draw_number"))
        figures = sorted(
            {_clean(pipe.get("figure_number")) for pipe in pipes if pipe.get("figure_number")}
        )
        if not (material and spec and com and figures):
            continue

        samples.append(
            {
                "id": _clean(record.get("ID")),
                "com": com,
                "figures": figures,
                "material": material,
                "spec": spec,
                "pipe_count": len(pipes),
                "pipe_demand_total": sum(
                    int(_clean(p.get("pipe_demand")) or 0)
                    for p in pipes
                    if str(p.get("pipe_demand", "")).strip().isdigit()
                ),
                # The backend accepts a bare {Pipe, Stock, ...} group directly.
                "problem": problem,
                "legacy": _slim_legacy(result),
            }
        )

    samples.sort(key=lambda s: (s["com"], s["material"], s["spec"], s["id"]))
    if limit is not None and len(samples) > limit:
        samples = _round_robin_by_com(samples, limit)
        samples.sort(key=lambda s: (s["com"], s["material"], s["spec"], s["id"]))
    return {"version": 1, "count": len(samples), "samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="MOM export JSON with a RECORDS array")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "frontend-next" / "public" / "samples.json",
        help="destination sample library JSON",
    )
    parser.add_argument(
        "--only-solved",
        action="store_true",
        help="keep only records the legacy engine actually solved",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of samples, spread across 部件图号",
    )
    args = parser.parse_args()

    library = build(args.source, only_solved=args.only_solved, limit=args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        json.dump(library, fh, ensure_ascii=False, separators=(",", ":"))
    size_mb = args.output.stat().st_size / 1024 / 1024
    print(f"wrote {library['count']} samples -> {args.output} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
