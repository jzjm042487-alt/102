"""Probe real records: do must_use and available bars ever share a length?"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmark_against_software import _clean_payload, _load_records  # noqa: E402


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "是", "真"}
    return False


def _iter_groups(payload):
    root = payload.get("input", payload)
    data = root.get("data")
    if isinstance(data, list):
        yield from data
    elif "Stock" in payload:
        yield payload


def main() -> int:
    records = _load_records()
    cands = [r for r in records if r.get("MOMCALCULATESTATUS") == "1"]
    groups_with_mustuse = 0
    mixed_length_groups = 0   # a length that has BOTH must_use and non-must_use
    total_groups = 0
    examples = []
    for r in cands:
        try:
            pl = _clean_payload(json.loads(r["MOMPROBLEMJSON"]))
        except Exception:
            continue
        for g in _iter_groups(pl):
            stocks = g.get("Stock")
            if not isinstance(stocks, list):
                continue
            total_groups += 1
            must_by_len: dict = {}
            avail_by_len: dict = {}
            has_must = False
            for s in stocks:
                if not isinstance(s, dict):
                    continue
                length = s.get("stock_length")
                key = "must_use" if "must_use" in s else "Is_Must"
                mu = _as_bool(s.get(key, "0"))
                if mu:
                    has_must = True
                    must_by_len[length] = must_by_len.get(length, 0) + 1
                else:
                    avail_by_len[length] = avail_by_len.get(length, 0) + 1
            if has_must:
                groups_with_mustuse += 1
                mixed = set(must_by_len) & set(avail_by_len)
                if mixed:
                    mixed_length_groups += 1
                    if len(examples) < 5:
                        examples.append((sorted(mixed), must_by_len, avail_by_len))

    print(f"total groups: {total_groups}")
    print(f"groups with any must_use: {groups_with_mustuse}")
    print(f"groups where a length is BOTH must_use and available: {mixed_length_groups}")
    for ex in examples:
        print("  mixed-length example:", ex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
