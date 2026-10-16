"""Exact cut pricing, column-generation bounds, and the volume lower bound."""

from __future__ import annotations

import random
from pathlib import Path

from app.cut_pricing import (
    cutting_stock_bound,
    fits,
    max_copies,
    minimum_inventory_length,
    price_pattern,
)
from app.lexcg import _fits

ROOT = Path(__file__).resolve().parents[2]


def test_fits_matches_solver_geometry() -> None:
    rng = random.Random(1)
    for kerf in (0, 1, 5):
        for mode in ("BETWEEN_PARTS", "WITH_REMAINDER"):
            for _ in range(40):
                stock = rng.randint(10, 80)
                parts = tuple(rng.randint(1, 30) for _ in range(rng.randint(1, 6)))
                assert fits(stock, parts, kerf, mode) is _fits(stock, parts, kerf, mode)


def test_price_pattern_finds_the_best_pack() -> None:
    value, parts = price_pattern(
        10,
        ((6, 6.0), (4, 4.0)),
        0,
        "BETWEEN_PARTS",
        native=False,
    )
    assert parts == (4, 6)
    assert value == 10.0
    native_value, native_parts = price_pattern(
        10, ((6, 6.0), (4, 4.0)), 0, "BETWEEN_PARTS", native=True
    )
    assert native_parts == parts
    assert native_value == value


def test_kerf_blocks_the_pair() -> None:
    assert fits(10, (4, 6), 0, "BETWEEN_PARTS")
    assert not fits(10, (4, 6), 1, "BETWEEN_PARTS")
    value, parts = price_pattern(10, ((6, 5.0), (4, 5.0)), 1, "BETWEEN_PARTS")
    assert parts == (4, 4)
    assert value == 10.0


def test_with_remainder_charges_a_trailing_kerf() -> None:
    assert fits(10, (9,), 2, "BETWEEN_PARTS")
    assert not fits(10, (9,), 2, "WITH_REMAINDER")
    assert fits(10, (10,), 2, "WITH_REMAINDER")
    assert fits(10, (8,), 2, "WITH_REMAINDER")
    value, parts = price_pattern(10, ((9, 3.0), (10, 1.0)), 2, "WITH_REMAINDER")
    assert parts == (10,)
    assert value == 1.0


def test_price_matches_enumeration() -> None:
    stock = 30
    kerf = 1
    segments = ((5, 2.0), (7, 4.0), (11, 5.0))
    best_value = 0.0
    best_parts: tuple[int, ...] = ()
    lengths = [length for length, _dual in segments]
    dual = dict(segments)

    def walk(parts: list[int]) -> None:
        nonlocal best_value, best_parts
        if parts and fits(stock, parts, kerf, "BETWEEN_PARTS"):
            value = sum(dual[part] for part in parts)
            if value > best_value:
                best_value = value
                best_parts = tuple(sorted(parts))
        if len(parts) >= 6:
            return
        for length in lengths:
            walk(parts + [length])

    walk([])
    value, parts = price_pattern(stock, segments, kerf, "BETWEEN_PARTS", native=False)
    assert abs(value - best_value) < 1e-9
    assert sum(dual[part] for part in parts) == best_value
    native_value, native_parts = price_pattern(
        stock, segments, kerf, "BETWEEN_PARTS", native=True
    )
    assert abs(native_value - value) < 1e-9
    assert sum(dual[part] for part in native_parts) == best_value


def test_cutting_stock_certificates_a_known_optimum() -> None:
    result = cutting_stock_bound(
        {6: 2, 4: 2},
        ((10, 10),),
        0,
        "BETWEEN_PARTS",
        incumbent=((10, (6, 4), 2),),
        seed=1,
        threads=1,
    )
    assert result["certified"] is True
    assert result["integer_lb"] == 20
    assert result["integer_solution"] == 20
    assert result["integer_proved"] is True
    assert result["lp_bound"] == 20


def test_finite_inventory_is_tighter_than_an_unlimited_bar() -> None:
    # A 10 mm bar holds two 4 mm pieces and only one such bar exists.
    # Two more pieces each need a 6 mm bar: 10 + 6 + 6 = 22.
    result = cutting_stock_bound(
        {4: 4},
        ((10, 1), (6, 10)),
        0,
        "BETWEEN_PARTS",
        seed=1,
        threads=1,
    )
    assert result["lp_bound"] is not None
    assert abs(float(result["lp_bound"]) - 22.0) < 1e-6
    assert result["integer_lb"] == 22
    assert result["integer_solution"] == 22
    assert result["integer_proved"] is True


def test_volume_bound_packs_across_pipes() -> None:
    # Two 6000 mm pipes can share one 12000 mm bar. Summing per pipe is not a bound.
    assert minimum_inventory_length(((12000, 1), (6000, 2)), 12000) == 12000
    assert minimum_inventory_length(((12000, 1), (6000, 2)), 18000) == 18000
    assert minimum_inventory_length(((12000, 1), (6000, 2)), 20000) == 24000
    assert minimum_inventory_length(((12000, 1),), 12001) is None
    assert minimum_inventory_length(((5000, 3),), 12000) == 15000
    assert max_copies(10, 4, 1, "BETWEEN_PARTS") == 2


def test_solver_does_not_import_the_pricing_module() -> None:
    for name in ("engine_stable.py", "engine_stable_opt.py", "solver.py"):
        text = (ROOT / "backend" / "app" / name).read_text(encoding="utf-8")
        assert "cut_pricing" not in text
