# Algorithm Study Diagnostic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first in-repository learning unit so Codex can explain the concepts and inputs, the learner can personally implement three core Python functions, and both can test and assess the result.

**Architecture:** Create an isolated `learning/00_diagnostic/` directory that production code never imports. Codex owns the lesson, inputs, tests, assessment, and project mapping; the learner owns `study.py`, whose initial version contains contracts and `NotImplementedError` stubs only.

**Tech Stack:** Python 3.12, pytest, Markdown, existing repository virtual environment

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `learning/00_diagnostic/lesson.md` | First explanation, hand-worked examples, coding order, and run commands |
| `learning/00_diagnostic/inputs.py` | Named public examples and expected outputs |
| `learning/00_diagnostic/study.py` | Learner-owned function stubs and later learner implementations |
| `learning/00_diagnostic/test_study.py` | Public contract tests followed later by unseen transfer tests |
| `learning/00_diagnostic/assessment.md` | Evidence-based assessment template |
| `learning/00_diagnostic/project_map.md` | Mapping from greedy fill to the production candidate generator |
| `learning/00_diagnostic/study_rewrite.py` | Learner-owned, uncommitted closed-reference rewrite created only at final assessment |

No `__init__.py` is required. Tests add the diagnostic directory to `sys.path` implicitly because pytest places the test directory on the import path.

### Task 1: Create the concept lesson and public inputs

**Files:**
- Create: `learning/00_diagnostic/lesson.md`
- Create: `learning/00_diagnostic/inputs.py`

- [ ] **Step 1: Create the isolated lesson directory**

Run:

```powershell
New-Item -ItemType Directory -Force -Path 'learning/00_diagnostic'
```

Expected: the directory exists. This command must not remove or replace existing files.

- [ ] **Step 2: Write the first lesson**

Create `lesson.md` with these sections:

1. What an algorithm is: a finite and unambiguous procedure mapping inputs to outputs.
2. Feasible solution versus optimal solution.
3. The pipe analogy: capacity is stock length, items are candidate pipe segments, and a selected list is one cutting pattern.
4. The Python building blocks used in the diagnostic: function parameters, `for`, `if`, `dict.get`, `sorted`, `append`, `sum`, copies, and `raise ValueError`.
5. Greedy choice: select the currently longest fitting segment.
6. Why greedy is not generally optimal, using capacity `10` and lengths `[6, 5, 5]`.
7. Brute-force intuition: compare complete feasible combinations instead of committing to the first local choice.
8. Exact function contracts copied from design section 6.1.
9. The required order: hand-calculate, run the stub, implement one function, run only that function's tests, then continue.

Do not include complete implementations of `aggregate_demands`, `greedy_fill`, or `best_fill`.

- [ ] **Step 3: Create named input cases**

Create `inputs.py` with plain constants:

```python
AGGREGATE_NORMAL_INPUT = [(6000, 2), (3000, 1), (6000, 3)]
AGGREGATE_NORMAL_EXPECTED = {3000: 1, 6000: 5}
AGGREGATE_EMPTY_INPUT = []
AGGREGATE_EMPTY_EXPECTED = {}

GREEDY_NORMAL_CAPACITY = 10
GREEDY_NORMAL_LENGTHS = [6, 5, 4]
GREEDY_NORMAL_EXPECTED = [6, 4]

GREEDY_COUNTEREXAMPLE_CAPACITY = 10
GREEDY_COUNTEREXAMPLE_LENGTHS = [6, 5, 5]
GREEDY_COUNTEREXAMPLE_EXPECTED = [6]
BEST_COUNTEREXAMPLE_EXPECTED = [5, 5]

PROJECT_SHAPED_CAPACITY = 12000
PROJECT_SHAPED_LENGTHS = [7000, 6000, 6000, 5000, 3000]
PROJECT_SHAPED_GREEDY_EXPECTED = [7000, 5000]
PROJECT_SHAPED_BEST_EXPECTED = [7000, 5000]
```

- [ ] **Step 4: Verify syntax without creating cache files**

Run:

```powershell
.\.venv\Scripts\python.exe -c "import ast, pathlib; ast.parse(pathlib.Path('learning/00_diagnostic/inputs.py').read_text(encoding='utf-8'))"
```

Expected: exit code `0` and no output.

- [ ] **Step 5: Commit the lesson and inputs**

```powershell
git add -- learning/00_diagnostic/lesson.md learning/00_diagnostic/inputs.py
git commit -m "docs: add algorithm diagnostic lesson"
```

### Task 2: Create the learner-owned scaffold

**Files:**
- Create only if absent: `learning/00_diagnostic/study.py`

- [ ] **Step 1: Check ownership before writing**

Run:

```powershell
Test-Path 'learning/00_diagnostic/study.py'
```

Expected before first creation: `False`.

If it returns `True`, stop and read the file. Do not replace or edit it without the learner's explicit instruction.

- [ ] **Step 2: Create contract-only stubs**

Create:

```python
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
```

- [ ] **Step 3: Verify syntax**

Run:

```powershell
.\.venv\Scripts\python.exe -c "import ast, pathlib; ast.parse(pathlib.Path('learning/00_diagnostic/study.py').read_text(encoding='utf-8'))"
```

Expected: exit code `0` and no output.

- [ ] **Step 4: Commit the untouched scaffold**

```powershell
git add -- learning/00_diagnostic/study.py
git commit -m "chore: add learner-owned diagnostic scaffold"
```

### Task 3: Create public tests and supporting templates

**Files:**
- Create: `learning/00_diagnostic/test_study.py`
- Create: `learning/00_diagnostic/assessment.md`
- Create: `learning/00_diagnostic/project_map.md`

- [ ] **Step 1: Write public tests**

Create tests that import the constants from `inputs.py` and load the learner module selected by `DIAGNOSTIC_STUDY_MODULE`, defaulting to `study`:

```python
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
    assert aggregate_demands(AGGREGATE_NORMAL_INPUT.copy()) == AGGREGATE_NORMAL_EXPECTED


def test_aggregate_demands_empty() -> None:
    assert aggregate_demands(AGGREGATE_EMPTY_INPUT.copy()) == AGGREGATE_EMPTY_EXPECTED


def test_greedy_fill_normal() -> None:
    assert greedy_fill(GREEDY_NORMAL_CAPACITY, GREEDY_NORMAL_LENGTHS.copy()) == GREEDY_NORMAL_EXPECTED


def test_greedy_fill_counterexample() -> None:
    assert (
        greedy_fill(
            GREEDY_COUNTEREXAMPLE_CAPACITY,
            GREEDY_COUNTEREXAMPLE_LENGTHS.copy(),
        )
        == GREEDY_COUNTEREXAMPLE_EXPECTED
    )


def test_best_fill_beats_greedy_counterexample() -> None:
    assert (
        best_fill(
            GREEDY_COUNTEREXAMPLE_CAPACITY,
            GREEDY_COUNTEREXAMPLE_LENGTHS.copy(),
        )
        == BEST_COUNTEREXAMPLE_EXPECTED
    )
```

- [ ] **Step 2: Run tests and confirm the expected learning failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest learning/00_diagnostic/test_study.py -q
```

Expected: five failures caused by the three intentional `NotImplementedError` stubs. Import or collection errors are not acceptable.

- [ ] **Step 3: Create the assessment template**

Create `assessment.md` with blank evidence fields for concept understanding, hand calculation, Python expression, correctness, boundaries, complexity, debugging, independence, highest hint level, greedy counterexample explanation, and recommended next lesson.

- [ ] **Step 4: Create the initial project map**

Document the first production relationship:

- `greedy_fill` corresponds conceptually to the seeded best-fill loop in `backend/app/solver.py::_generate_cut_candidates`.
- Both repeatedly choose the largest currently fitting segment.
- Production code additionally accounts for blade margin, candidate caps, multiple seeds, stable de-duplication, and later MILP selection.
- The lesson's counterexample does not by itself prove a production bug because production enumerates multiple seeds and alternatives; it identifies a heuristic boundary to test.

- [ ] **Step 5: Commit public tests and templates**

```powershell
git add -- learning/00_diagnostic/test_study.py learning/00_diagnostic/assessment.md learning/00_diagnostic/project_map.md
git commit -m "test: add algorithm diagnostic checks"
```

### Task 4: Learner implementation checkpoints

**Files:**
- Learner modifies: `learning/00_diagnostic/study.py`
- Codex reads but does not modify: `learning/00_diagnostic/study.py`

- [ ] **Step 1: Explain Task 1 and review the public input**

Walk through aggregation by hand. Confirm that the learner understands why two `6000` rows become `{6000: 5}` before asking for code.

- [ ] **Step 2: Learner implements `aggregate_demands`**

Codex waits for the learner to save the file and say `check` or `检查`.

- [ ] **Step 3: Run only aggregation tests**

```powershell
.\.venv\Scripts\python.exe -m pytest learning/00_diagnostic/test_study.py -q -k aggregate
```

Expected after a correct implementation: `2 passed`.

- [ ] **Step 4: Explain greedy choice and review both greedy inputs**

Hand-simulate `[6, 5, 4]` and `[6, 5, 5]`. Explain local choice versus global objective without supplying the function body.

- [ ] **Step 5: Learner implements `greedy_fill`**

Codex waits for save and `check` or `检查`.

- [ ] **Step 6: Run only greedy tests**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  learning/00_diagnostic/test_study.py::test_greedy_fill_normal `
  learning/00_diagnostic/test_study.py::test_greedy_fill_counterexample -q
```

Expected after a correct implementation: `2 passed`.

- [ ] **Step 7: Explain exhaustive search and optimal subset selection**

Draw the include/exclude decision for the three elements `[6, 5, 5]`. Do not give complete Python code.

- [ ] **Step 8: Learner implements `best_fill`**

Codex waits for save and `check` or `检查`.

- [ ] **Step 9: Run all public tests**

```powershell
.\.venv\Scripts\python.exe -m pytest learning/00_diagnostic/test_study.py -q
```

Expected after correct implementations: `5 passed`.

- [ ] **Step 10: Run the project-shaped input manually**

Run:

```powershell
Push-Location 'learning/00_diagnostic'
try {
    @'
from inputs import PROJECT_SHAPED_CAPACITY, PROJECT_SHAPED_LENGTHS
from study import best_fill, greedy_fill

print(greedy_fill(PROJECT_SHAPED_CAPACITY, PROJECT_SHAPED_LENGTHS.copy()))
print(best_fill(PROJECT_SHAPED_CAPACITY, PROJECT_SHAPED_LENGTHS.copy()))
'@ | ..\..\.venv\Scripts\python.exe
} finally {
    Pop-Location
}
```

Expected:

```text
[7000, 5000]
[7000, 5000]
```

### Task 5: Add transfer tests after the learner's first implementation

**Files:**
- Modify: `learning/00_diagnostic/test_study.py`

- [ ] **Step 1: Add validation and non-mutation tests**

Add tests for:

- zero or negative capacity raises `ValueError`;
- zero or negative segment lengths raise `ValueError`;
- invalid order length or quantity raises `ValueError`;
- non-integer capacities, lengths, order lengths, and quantities raise `ValueError`;
- all three functions leave their list input unchanged;
- oversized positive lengths are ignored by fill functions.
- empty fill inputs return `[]`;
- `aggregate_demands` returns keys in ascending insertion order.

Use explicit checks such as:

```python
import pytest


@pytest.mark.parametrize("bad_capacity", [0, -1, 10.5, "10"])
def test_fill_rejects_invalid_capacity(bad_capacity: object) -> None:
    with pytest.raises(ValueError):
        greedy_fill(bad_capacity, [6, 4])  # type: ignore[arg-type]


def test_aggregate_demands_returns_sorted_key_order() -> None:
    result = aggregate_demands([(6000, 1), (3000, 1), (5000, 1)])
    assert list(result) == [3000, 5000, 6000]


def test_fill_empty_input() -> None:
    assert greedy_fill(10, []) == []
    assert best_fill(10, []) == []
```

- [ ] **Step 2: Add deterministic tie-breaking tests**

Use cases that prove:

- equal fill prefers fewer segments;
- equal fill and equal count prefer the lexicographically larger descending result;
- duplicate values represent separate usable items.

Use these exact inputs and expected outputs:

```python
def test_best_fill_prefers_fewer_segments() -> None:
    assert best_fill(10, [10, 6, 4]) == [10]


def test_best_fill_uses_lexicographic_tie_break() -> None:
    assert best_fill(10, [7, 3, 6, 4]) == [7, 3]


def test_best_fill_treats_duplicates_as_separate_items() -> None:
    assert best_fill(10, [6, 5, 5]) == [5, 5]
```

- [ ] **Step 3: Run transfer tests**

```powershell
.\.venv\Scripts\python.exe -m pytest learning/00_diagnostic/test_study.py -q
```

Expected: failures are possible and form part of the assessment. Import and collection must still succeed.

- [ ] **Step 4: Learner repairs any failed behavior**

Codex explains the first failing state and uses the agreed hint ladder. The learner edits `study.py`.

- [ ] **Step 5: Re-run tests**

Expected when complete: all diagnostic tests pass.

### Task 6: Assess and connect to the production project

**Files:**
- Modify: `learning/00_diagnostic/assessment.md`
- Modify: `learning/00_diagnostic/project_map.md`
- Learner creates temporarily: `learning/00_diagnostic/study_rewrite.py`

- [ ] **Step 1: Record evidence**

Complete every assessment dimension with observed evidence, not personality labels or an unsupported numeric score.

- [ ] **Step 2: Learner explains state and branches**

Without Codex paraphrasing first, the learner explains:

- what each function input and output represents;
- what mutable state changes during each loop or recursive call;
- why every `if` branch exists;
- which invariant keeps the partial result feasible;
- why the chosen `best_fill` method eventually finds the optimum.

Record the explanation and any prompts required in `assessment.md`.

- [ ] **Step 3: Perform a closed-reference rewrite**

The learner closes `study.py`, the lesson, and chat reference code, then creates `study_rewrite.py` and rewrites all three diagnostic functions from memory. Codex must not create or edit this file.

Run the same public and transfer tests against the rewrite:

```powershell
$env:DIAGNOSTIC_STUDY_MODULE = "study_rewrite"
try {
    .\.venv\Scripts\python.exe -m pytest learning/00_diagnostic/test_study.py -q
} finally {
    Remove-Item Env:\DIAGNOSTIC_STUDY_MODULE -ErrorAction SilentlyContinue
}
```

Expected: all diagnostic tests pass. Record the result in `assessment.md`.

Do not commit `study_rewrite.py`; it is temporary learner evidence.

- [ ] **Step 4: Review complexity**

Record the learner's own complexity explanation for aggregation, greedy fill, and the chosen `best_fill` implementation.

- [ ] **Step 5: Audit the production analogy**

Compare the learner's greedy code with `backend/app/solver.py::_generate_cut_candidates`, especially the loop beginning with `for seed in fitting`. Record what the production heuristic adds and what it still may miss.

- [ ] **Step 6: Select the next lesson**

Choose one:

- Python refresher if function/data-structure expression blocked progress;
- explicit enumeration if the learner solved `best_fill` informally but cannot describe the search space;
- greedy invariants and counterexamples if greedy reasoning is weak;
- 0/1 knapsack state modeling if the learner is ready to move from enumeration to dynamic programming.

- [ ] **Step 7: Run repository regression tests**

The learning directory must not affect production:

```powershell
$env:PYTHONPATH="$PWD\backend"
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Expected: the existing backend suite passes with the same known skip and no new failures.

- [ ] **Step 8: Commit assessment artifacts**

Do not commit learner code without the learner's explicit approval.

```powershell
git add -- learning/00_diagnostic/test_study.py learning/00_diagnostic/assessment.md learning/00_diagnostic/project_map.md
git commit -m "docs: assess algorithm diagnostic"
```
