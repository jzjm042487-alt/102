"""Pluggable candidate-generation acceleration backends.

The solver's restricted MILP is fed a *pool* of welding candidates
(``_WeldCandidate``).  A :class:`CandidateProvider` may **append** extra,
already-legal welding columns to that pool -- it can only widen the feasible
region, never shrink it, so a provider can never carve away a group's only
legal decomposition (see ``docs/research/GPU可插拔预解器设计方案.md`` §五).

Backends:

* :class:`CpuNoopProvider` -- returns nothing; the pool is exactly the CPU
  baseline.  Selected when no accelerator is available or ``NESTING_ACCEL=cpu``.
* GPU provider -- CuPy-vectorised generation (added in stage 2); selected only
  when ``NESTING_ACCEL`` permits *and* a CUDA device with CuPy is present.

Selection is driven by the ``NESTING_ACCEL`` environment variable
(``auto`` | ``gpu`` | ``cpu``) and is cached for the process.  Any failure to
initialise or run a GPU backend degrades to the CPU baseline; a provider must
never raise out of :func:`select_provider` or crash a solve.
"""

from __future__ import annotations

from .base import CandidateProvider, CpuNoopProvider
from .registry import accel_mode, select_provider

__all__ = [
    "CandidateProvider",
    "CpuNoopProvider",
    "accel_mode",
    "select_provider",
]
