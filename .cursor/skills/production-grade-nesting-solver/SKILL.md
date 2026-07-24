---
name: production-grade-nesting-solver
description: Governs all design, implementation, debugging, and acceptance work on the snake-pipe 1D cut-and-weld nesting solver. Use whenever modifying or evaluating solver algorithms, R&CG, weld-then-cut, D5, pattern generation, objectives, acceptance reports, or professor/legacy comparisons.
---

# Production-Grade Nesting Solver

## Mission

Deliver one genuinely usable algorithm for finite-stock 1D cutting with welding. The deliverable is not a solution for a few cases, a better weighted score, a feasible solution, or a patchwork of special cases. It must produce verifier-valid, auditable plans that reduce workshop setup complexity across unseen orders.

## Non-Negotiable Objective

Use this lexicographic order unless the user explicitly changes it:

1. minimize `weld_types` — distinct weld recipes;
2. lock that optimum and minimize total `joints`;
3. lock both optima and minimize `cut_types` — distinct cut recipes;
4. lock all above and minimize `used_len` / maximize `util`.

Do not replace this with weighted sums. Do not let utilization, a score, or feasibility silently outrank a prior objective. A later phase may run only after the earlier optimum is recorded and constrained.

Canonical recipe identities:

- weld recipe: finished-pipe identity plus normalized ordered segment sequence and required process attributes;
- cut recipe: stock length plus normalized segment multiset and kerf semantics;
- reverse-order sequences are the same only if the production process explicitly treats them as equivalent.

## Required Architecture: One Coupled Algorithm

The solver has one logical algorithm, even if implemented with a master problem and pricing subproblems:

```text
weld recipe activation → segment demand
cut recipe activation  → segment supply
segment supply >= segment demand
finite inventory + process constraints
```

It may use R&CG / branch-and-price or a restricted master with certified expansion. It must not be implemented as disconnected short-, medium-, and long-term routes, nor as several independent engines competing without provenance.

Required decision variables in the integer master:

```text
x[p] = repetitions of weld recipe p
y[q] = repetitions of cut recipe q
zw[p] = weld recipe p is active
zc[q] = cut recipe q is active
```

Required linking constraints:

```text
x[p] <= pipe-demand-upper-bound[p] * zw[p]
y[q] <= stock-quantity[stock(q)] * zc[q]
for every segment s:
    sum(q produces s) y[q] >= sum(p consumes s) x[p]
```

The model must also enforce pipe demand, finite stock, must-use stock, kerf rules, segment bounds, maximum joints, minimum weld distance, forbidden weld regions, and all verifier invariants.

## Candidate Generation and Expansion

1. Seed legal weld and cut recipes from the current order only: pipe demand, stock inventory, kerf, and process constraints.
2. Solve the current restricted master through the four objective passes.
3. Expand recipes only with an auditable reason:
   - a negative/reduced-cost recipe under the currently locked objective layer; or
   - a documented infeasibility witness identifying an unsupplied segment or inventory bottleneck.
4. Every expansion record must include the trigger, recipe, originating constraint/dual or witness, and the objective phase.
5. Do not add broad candidate floods, arbitrary top-N templates, random weights, hard-coded case IDs, legacy dimensions, or one-off rules.
6. Historical professor/legacy solutions may be used only as offline acceptance data or optional warm-start seeds clearly marked as such. They must never be copied into production candidate generation.

## Forbidden Work Patterns

Stop and redesign instead of doing any of these:

- tuning one case, one threshold, one penalty, or one cap to improve a report;
- adding a fallback that returns a lower-priority-worse plan as success;
- using a weighted objective in place of lexicographic locks;
- claiming R&CG when rows/columns are only heuristically appended without evidence;
- using `legacy_seed`, case ID, historical segment lengths, or professor output to create a production solution;
- measuring success by `has_solution`, wall-clock time alone, or one small handpicked sample;
- keeping multiple conflicting production routes without reporting exactly which route produced the result.

If three attempted fixes expose different structural failures, stop patching. Write the root cause, redesign the model boundary or pricing formulation, and only then resume implementation.

## Mandatory Evidence Before Claiming Progress

Every algorithm change must provide all of the following:

1. **Unit tests** for objective locks, activation variables, segment balance, legality, and the triggering defect.
2. **Independent verifier** pass for every reported solution.
3. **Fixed split evaluation**: train data only develops candidate/pricing rules; validation selects configuration; holdout is reported once for acceptance. No leakage across splits.
4. **Four-dimensional comparison** to legacy on the same cases: `weld_types`, `joints`, `cut_types`, `util`.
5. **Per-case artifact** showing active weld recipes, active cut recipes, quantities, segment balance, stock use, objective values per phase, generation provenance, and elapsed time.
6. **Aggregate artifact** with solved count, verifier-failure count, lex-non-worse count, mixed count, worse count, timeout count, and result source (`native`, `warm-start-assisted`, or fallback).

Never report aggregate superiority when any result source is mixed without separating it.

## Acceptance Gate

A candidate becomes usable only when all are true on the untouched holdout set:

- every accepted result passes the independent verifier;
- no recipe derives from case ID or legacy output;
- its exact four-pass objective values are persisted;
- output is inspectable by workshop users in HTML/JSON;
- it improves the agreed non-worse/human-review metric against the current frozen baseline;
- no result labelled as native was actually created by a fallback or warm-start path.

Until these conditions hold, call the work an experiment, not a production algorithm.

## Existing Code: Read Before Editing

- `docs/SPEC.md` — domain contract and hard constraints. If its objective order conflicts with this skill, stop and ask the user; do not silently choose one.
- `backend/app/route_b_rcg.py` — existing coupled R&CG prototype; preserve only validated components, not its utilization-first objective.
- `scripts/weld_then_cut.py` — existing decoupled prototype; preserve inventory feasibility mechanics only if they conform to this skill.
- `backend/app/verifier.py` — independent correctness gate.
- `scripts/solve_d5_motif.py` and `scripts/d5_column_factory.py` — frozen comparative baseline; do not add local tuning.

## Working Protocol

1. State the exact model/price defect in terms of recipe activation, supply-demand balance, objective ordering, or generation completeness.
2. Add a failing test that demonstrates it.
3. Implement the smallest model-level correction.
4. Run targeted tests, then regression tests, then the fixed validation/holdout harness.
5. If the result misses the acceptance gate, record the evidence and continue from the diagnosed model defect; do not create a parallel shortcut.
6. Keep one canonical solver result schema and include `solver_path` with every output.
