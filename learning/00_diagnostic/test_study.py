"""Public tests for the recovery diagnostic."""

import importlib
import os

from inputs import (
    AGGREGATE_EMPTY_EXPECTED,
    AGGREGATE_EMPTY_INPUT,
    AGGREGATE_NORMAL_EXPECTED,
    AGGREGATE_NORMAL_INPUT,
    BEST_COUNTEREXAMPLE_EXPECTED,
    GREEDY_COUNTEREXAMPLE_CAPACITY,
    GREEDY_COUNTEREXAMPLE_EXPECTED,
    GREEDY_COUNTEREXAMPLE_LENGTHS,
    GREEDY_NORMAL_CAPACITY,
    GREEDY_NORMAL_EXPECTED,
    GREEDY_NORMAL_LENGTHS,
)

study = importlib.import_module(os.environ.get("DIAGNOSTIC_STUDY_MODULE", "study"))
aggregate_demands = study.aggregate_demands
best_fill = study.best_fill
greedy_fill = study.greedy_fill


def test_aggregate_demands_normal() -> None:
    actual = aggregate_demands(AGGREGATE_NORMAL_INPUT.copy())
    assert actual == AGGREGATE_NORMAL_EXPECTED


def test_aggregate_demands_empty() -> None:
    actual = aggregate_demands(AGGREGATE_EMPTY_INPUT.copy())
    assert actual == AGGREGATE_EMPTY_EXPECTED


def test_greedy_fill_normal() -> None:
    actual = greedy_fill(GREEDY_NORMAL_CAPACITY, GREEDY_NORMAL_LENGTHS.copy())
    assert actual == GREEDY_NORMAL_EXPECTED


def test_greedy_fill_counterexample() -> None:
    actual = greedy_fill(
        GREEDY_COUNTEREXAMPLE_CAPACITY,
        GREEDY_COUNTEREXAMPLE_LENGTHS.copy(),
    )
    assert actual == GREEDY_COUNTEREXAMPLE_EXPECTED


def test_best_fill_beats_greedy_counterexample() -> None:
    actual = best_fill(
        GREEDY_COUNTEREXAMPLE_CAPACITY,
        GREEDY_COUNTEREXAMPLE_LENGTHS.copy(),
    )
    assert actual == BEST_COUNTEREXAMPLE_EXPECTED
