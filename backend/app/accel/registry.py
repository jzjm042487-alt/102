"""Runtime backend selection with safe degradation.

``NESTING_ACCEL`` chooses the acceleration mode:

* ``auto`` (default): use the GPU provider iff CuPy imports and a CUDA device is
  present; otherwise the CPU baseline.
* ``gpu``: force the GPU provider; if it is unavailable, log a warning and fall
  back to CPU (never fail the process).
* ``cpu``: force the CPU baseline (the explicit switch for GPU-less sites).

Selection is cached for the process.  Probing is wrapped so that a missing
library, a driver mismatch, or a CUDA runtime error can never escape -- the
worst case is a warning and the CPU baseline.
"""

from __future__ import annotations

import functools
import logging
import os

from .base import CandidateProvider, CpuNoopProvider

log = logging.getLogger(__name__)

_VALID_MODES = ("auto", "gpu", "cpu", "gpu-cpu")


def accel_mode() -> str:
    """The requested acceleration mode, normalised; unknown values -> ``auto``."""

    mode = os.getenv("NESTING_ACCEL", "auto").strip().lower()
    if mode not in _VALID_MODES:
        log.warning("NESTING_ACCEL=%r is not one of %s; using 'auto'", mode, _VALID_MODES)
        return "auto"
    return mode


def _gpu_available() -> bool:
    """True iff CuPy imports and reports at least one CUDA device.

    Every failure mode (no CuPy, no driver, no device, runtime error) is treated
    as "not available" -- never raised.
    """

    try:
        import cupy  # type: ignore

        return int(cupy.cuda.runtime.getDeviceCount()) > 0
    except Exception as exc:  # noqa: BLE001 - environmental, must not propagate
        log.debug("GPU probe failed, staying on CPU: %s", exc)
        return False


def _build_gpu_provider(force_numpy: bool = False) -> CandidateProvider | None:
    """Construct the GPU provider, or ``None`` if it cannot be built.

    The GPU provider module is imported lazily so that a CPU-only deployment
    never pays for (or fails on) a missing CuPy import at module load.
    ``force_numpy`` pins the NumPy array backend even on a CUDA host.
    """

    try:
        from .gpu import GpuCandidateProvider  # type: ignore
    except Exception as exc:  # noqa: BLE001 - GPU provider not present/importable
        log.debug("GPU provider unavailable: %s", exc)
        return None
    try:
        return GpuCandidateProvider(force_numpy=force_numpy)
    except Exception as exc:  # noqa: BLE001 - construction must not be fatal
        log.warning("GPU provider failed to initialise, using CPU: %s", exc)
        return None


@functools.lru_cache(maxsize=1)
def select_provider() -> CandidateProvider:
    """Return the process-wide candidate provider for the current environment."""

    mode = accel_mode()
    if mode == "cpu":
        return CpuNoopProvider()
    # ``gpu-cpu`` forces the vectorised GPU provider onto its NumPy backend even
    # on a CUDA host -- used to unit-test and benchmark the generation logic
    # (identical to the CuPy path bar the array backend) deterministically.
    if mode == "gpu-cpu":
        provider = _build_gpu_provider(force_numpy=True)
        return provider if provider is not None else CpuNoopProvider()
    if mode in ("auto", "gpu"):
        if _gpu_available():
            provider = _build_gpu_provider()
            if provider is not None:
                log.info("acceleration: using %s provider", provider.name)
                return provider
        if mode == "gpu":
            log.warning(
                "NESTING_ACCEL=gpu requested but no usable GPU backend; "
                "falling back to CPU baseline"
            )
    return CpuNoopProvider()
