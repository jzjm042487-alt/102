"""Provider interface and the always-available CPU no-op backend.

The interface is intentionally free of any ``solver`` import: a provider yields
plain ``(pipe_index, parts)`` tuples of **integer millimetres**, and the solver
adapts them into ``_WeldCandidate`` objects.  This keeps the acceleration layer
decoupled (no circular import) and makes every provider unit-testable without
constructing solver internals.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..domain import MaterialGroup

# A welding column: which pipe (by index in ``group.pipes``) and the ordered
# integer-millimetre part lengths it is welded from.  Order is significant --
# reversing a tuple is a distinct welding pattern.
WeldColumn = tuple[int, tuple[int, ...]]


@runtime_checkable
class CandidateProvider(Protocol):
    """Appends extra legal welding columns to the CPU baseline pool.

    Contract (all points are load-bearing red lines):

    * **Append-only**: the returned columns are *added* to the pool; the pool is
      never replaced or pruned by a provider.  A provider can only widen the
      feasible region.
    * **Legal & integer**: every returned column must already satisfy the
      group's process rules (min-cut length, min weld distance, forbidden zones,
      max joints, stock length) and be integer millimetres.  The solver still
      re-checks, but a provider must not lean on that.
    * **Deterministic**: identical inputs must yield an identical, ordered,
      de-duplicated result so the whole solve stays reproducible.
    * **Never fatal**: a provider must not raise for environmental reasons; it
      returns ``[]`` and lets the caller fall back to the baseline.
    """

    name: str

    def augment_weld_candidates(
        self,
        group: MaterialGroup,
        tier: str,
        existing: Sequence[WeldColumn],
        *,
        deadline: float | None = None,
        cap: int = 0,
    ) -> list[WeldColumn]:
        """Return **new** legal welding columns (excluding ``existing``).

        Parameters
        ----------
        group:
            The material group being solved (stocks, pipes, process rules).
        tier:
            The current graded-relaxation tier; a provider may scale how much it
            generates by tier.
        existing:
            Columns already in the pool, so the provider can skip duplicates.
        deadline:
            Optional wall-clock ``time.monotonic`` bound; the provider must stop
            before it and return whatever it has.
        cap:
            Upper bound on how many columns to return (``0`` disables the cap).
        """
        ...


class CpuNoopProvider:
    """Adds nothing -- the pool stays exactly the CPU baseline.

    This is the default whenever no accelerator is available, and it makes the
    accelerated path a strict superset of historical behaviour: with this
    provider selected the solver is byte-for-byte the pre-accel solver.
    """

    name = "cpu-noop"

    def augment_weld_candidates(
        self,
        group: MaterialGroup,
        tier: str,
        existing: Sequence[WeldColumn],
        *,
        deadline: float | None = None,
        cap: int = 0,
    ) -> list[WeldColumn]:
        return []
