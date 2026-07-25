"""Learner-owned implementations for the recovery diagnostic.

Codex must not fill or overwrite these functions unless the learner explicitly
asks for a reference implementation.
"""


def aggregate_demands(order_lines: list[tuple[int, int]]) -> dict[int, int]:
    """Aggregate positive (length, quantity) rows by length."""
    raise NotImplementedError("Implement aggregate_demands")


def greedy_fill(capacity: int, lengths: list[int]) -> list[int]:
    """Select each fitting segment once, checking lengths from largest to smallest."""
    raise NotImplementedError("Implement greedy_fill")


def best_fill(capacity: int, lengths: list[int]) -> list[int]:
    """Return the deterministic best subset under the agreed tie-breaking rules."""
    raise NotImplementedError("Implement best_fill")
