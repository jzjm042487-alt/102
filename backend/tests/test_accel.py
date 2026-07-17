from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.accel import CpuNoopProvider, accel_mode, select_provider
from app.accel.registry import _gpu_available
from app.domain import parse_problem
from app import solver


FIXTURES = Path(__file__).parent / "fixtures"


def _plan_signature(result: dict) -> list[tuple]:
    """Cut/weld plan per group, excluding wall-clock fields, for equality checks."""

    return [
        (
            group["metrics"]["used_stock_length"],
            group["metrics"]["welding_joint_quantity"],
            [
                (row["pipe_id"], tuple(row["parts"]), row["quantity"])
                for row in group.get("welding_patterns", [])
            ],
            [
                (row["stock_length"], tuple(row["parts"]), row["quantity"])
                for row in group.get("cutting_patterns", [])
            ],
        )
        for group in result["groups"]
    ]


def _first_group():
    payload = json.loads((FIXTURES / "valid_case.json").read_text(encoding="utf-8"))[
        "problem"
    ]
    return parse_problem(payload).groups[0]


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    # The selector is process-cached; reset it around every test so env-var
    # overrides take effect and do not leak between cases.
    select_provider.cache_clear()
    yield
    select_provider.cache_clear()


def test_noop_provider_appends_nothing() -> None:
    group = _first_group()
    provider = CpuNoopProvider()
    assert provider.augment_weld_candidates(group, "full", []) == []


def test_cpu_mode_selects_noop(monkeypatch) -> None:
    monkeypatch.setenv("NESTING_ACCEL", "cpu")
    assert accel_mode() == "cpu"
    assert select_provider().name == "cpu-noop"


def test_unknown_mode_falls_back_to_auto(monkeypatch) -> None:
    monkeypatch.setenv("NESTING_ACCEL", "banana")
    assert accel_mode() == "auto"


def test_gpu_mode_without_device_degrades_to_cpu(monkeypatch) -> None:
    # A GPU-less site that explicitly asked for GPU must still get a usable
    # (CPU) provider rather than crashing -- the core "pluggable" guarantee.
    monkeypatch.setenv("NESTING_ACCEL", "gpu")
    monkeypatch.setattr("app.accel.registry._gpu_available", lambda: False)
    assert select_provider().name == "cpu-noop"


def test_gpu_probe_never_raises() -> None:
    # Whatever the host looks like, probing must return a bool, never raise.
    assert isinstance(_gpu_available(), bool)


def test_default_solver_pool_is_baseline_when_no_accel(monkeypatch) -> None:
    # With the no-op provider selected, the accel hook contributes nothing, so
    # the tiered pool equals the historical baseline pool.  Force CPU so this
    # holds on hosts that do have a usable GPU.
    monkeypatch.setenv("NESTING_ACCEL", "cpu")
    select_provider.cache_clear()
    group = _first_group()
    assert solver._accel_weld_candidates(group, "full", []) == []


class _FakeProvider:
    """Returns a mix of legal, illegal, duplicate and malformed columns."""

    name = "fake"

    def __init__(self, columns):
        self._columns = columns

    def augment_weld_candidates(self, group, tier, existing, *, deadline=None, cap=0):
        return list(self._columns)


def test_accel_columns_are_validated_deduped_and_ordered(monkeypatch) -> None:
    group = _first_group()
    pipe = group.pipes[0]
    whole = (pipe.length,)  # the always-legal whole-pipe column

    fake = _FakeProvider(
        [
            (0, whole),  # legal
            (0, whole),  # duplicate of the above -> dropped
            (0, (10**9,)),  # exceeds any stock -> dropped
            (99, whole),  # pipe index out of range -> dropped
            ("x", whole),  # malformed -> dropped
        ]
    )
    monkeypatch.setattr(solver, "select_provider", lambda: fake)

    out = solver._accel_weld_candidates(group, "full", [])

    assert [(c.pipe_index, c.parts) for c in out] == [(0, whole)]


def test_accel_failure_falls_back_to_baseline(monkeypatch) -> None:
    class _Boom:
        name = "boom"

        def augment_weld_candidates(self, *a, **k):
            raise RuntimeError("gpu exploded")

    group = _first_group()
    monkeypatch.setattr(solver, "select_provider", lambda: _Boom())

    # A provider blowing up must never break candidate generation.
    assert solver._accel_weld_candidates(group, "full", []) == []


# --- GPU provider (CuPy on a GPU host, NumPy backend otherwise) --------------


def test_gpu_provider_builds_and_is_consistent() -> None:
    from app.accel.gpu import GpuCandidateProvider

    provider = GpuCandidateProvider()
    # It must build on any host without raising.  When a CUDA device is present
    # it uses CuPy ("gpu-cupy"); otherwise it degrades to NumPy ("gpu-numpy").
    # The two flags must always agree, whichever backend was selected.
    assert provider.is_gpu is (provider.name == "gpu-cupy")
    assert provider.name in ("gpu-cupy", "gpu-numpy")


def test_gpu_provider_emits_legal_deduped_columns() -> None:
    from app.accel.gpu import GpuCandidateProvider

    group = _first_group()
    provider = GpuCandidateProvider()
    columns = provider.augment_weld_candidates(group, "full", [], cap=50)

    keys = [tuple(c) for c in columns]
    assert len(keys) == len(set(keys))  # de-duplicated
    max_stock = max(stock.length for stock in group.stocks)
    for pipe_index, parts in columns:
        assert 0 <= pipe_index < len(group.pipes)
        pipe = group.pipes[pipe_index]
        assert sum(parts) == pipe.length
        assert max(parts) <= max_stock
        assert solver._legal_pattern(
            pipe, parts, group.min_weld_distance, group.min_cut_length
        )


def test_gpu_provider_is_deterministic() -> None:
    from app.accel.gpu import GpuCandidateProvider

    group = _first_group()
    a = GpuCandidateProvider().augment_weld_candidates(group, "full", [], cap=50)
    b = GpuCandidateProvider().augment_weld_candidates(group, "full", [], cap=50)
    assert [tuple(c) for c in a] == [tuple(c) for c in b]


def test_gpu_provider_respects_cap() -> None:
    from app.accel.gpu import GpuCandidateProvider

    group = _first_group()
    columns = GpuCandidateProvider().augment_weld_candidates(group, "full", [], cap=2)
    assert len(columns) <= 2


def test_gpu_cpu_mode_solves_and_is_deterministic(monkeypatch) -> None:
    # End-to-end: forcing the GPU provider onto its NumPy backend must still
    # solve a known-good case, pass the verifier, and be reproducible.
    import json

    monkeypatch.setenv("NESTING_ACCEL", "gpu-cpu")
    select_provider.cache_clear()

    payload = json.loads(
        (FIXTURES / "valid_case.json").read_text(encoding="utf-8")
    )["problem"]
    first = solver.solve_payload(payload, time_limit_seconds=10)
    second = solver.solve_payload(payload, time_limit_seconds=10)

    from app.verifier import verify_solution

    assert verify_solution(payload, first)["passed"] is True
    assert first["status"] == "TARGET_REACHED"
    # Compare the production plan (elapsed_seconds is a wall-clock field and is
    # expected to differ run to run); the cut/weld plan must be identical.
    assert _plan_signature(first) == _plan_signature(second)
