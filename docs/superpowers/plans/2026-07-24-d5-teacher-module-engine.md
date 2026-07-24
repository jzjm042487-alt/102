# D5 教师模块引擎 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 D5 从“海量单列组合后压缩”改为“由教师语料学习少量可复用切—焊模块，再由受限尾项补齐”的可验证求解路径。

**Architecture:** 离线层把教师每个成功案例规范成模块训练样本：一个模块同时描述切法供给、焊法消耗、覆盖的管型、复用件数与尾项比例。模块挖掘只使用输入中可重建的几何特征，不保存题号或订单标识。在线/探针层先选择少量主模块、再用严格受限的例外列完成余额；所有结果仍经独立验证器与留出集对标。授权教授软件只作为同一训练语料格式的增量来源，绝不接入生产求解路径。

**Tech Stack:** Python 3.13、PySCIPOpt、现有 `legacy-cases`、`legacy-fingerprints-all`、pytest、现有 D5 HTML 报告。

---

## Completion Definition

“彻底解决”不以单题或有解率定义，而以以下连续门禁定义：

1. D5 留出集上，模块引擎的 `human_non_worse_rate` 达到并稳定保持至少 20%，再以 50% 为下一阶段门槛。
2. 自动 `lex_non_worse_rate` 相对当前 0% 有可复现实质提升；每个 `mixed` 必须能在 HTML 解释为主模块或尾项代价。
3. 不得以题号、历史订单 ID 或教师输出中的具体段长作为规则输入；规则只能由当前案例库存、需求、切缝与工艺约束重建。
4. 方案必须通过独立验证器；D1/D2 生产主路径不变；线上教师响应永不直接作为生产排料。

## File Structure

- Create: `scripts/teacher_module_miner.py` — 教师布局标准化、训练/验证/留出切分、模块候选与统计。
- Create: `scripts/d5_module_factory.py` — 将当前问题的几何特征与已验证模块原型匹配，生成主模块与受限尾项列。
- Create: `scripts/solve_d5_modules.py` — 模块级主问题；先选主模块，再允许有限尾项补齐。
- Create: `scripts/probe_d5_modules.py` — 可恢复的分层评估、机器数据和 HTML 详审输出。
- Create: `scripts/teacher_api_adapter.py` — 仅授权测试环境使用的响应标准化接口；从环境变量读取配置。
- Create: `backend/tests/test_teacher_module_miner.py` — 语料切分、模块签名、无题号泄漏测试。
- Create: `backend/tests/test_d5_module_factory.py` — 当前输入可重建模块与例外预算测试。
- Create: `backend/tests/test_solve_d5_modules.py` — 模块主问题、独立验证器、非回归测试。
- Modify: `scripts/probe_d5_motif.py` — 仅保留为 v1 基线，不再追加局部权重实验。
- Modify: `scripts/_gen_report.py` or add a D5 report renderer — 展示主模块覆盖、尾项比例、四维对标与教师布局。
- Create: `data/acceptance/d5_teacher_modules/` — 运行时生成，包含 split、模块词典、训练统计、留出报告；不将真实线上响应或密钥纳入 Git。

## Task 1: Freeze Baseline and Create Leakage-Safe Corpus Split

**Files:**
- Create: `backend/tests/test_teacher_module_miner.py`
- Create: `scripts/teacher_module_miner.py`

- [ ] Write a failing test proving that deterministic splits are disjoint, exhaustive, and stable for a fixed case ID list.
- [ ] Run `pytest backend/tests/test_teacher_module_miner.py -q`; expected failure because the miner does not exist.
- [ ] Implement `split_case_ids(case_ids, seed=20260724)` using a stable hash of the numeric ID only for dataset partitioning, yielding 60% train, 20% validation, 20% holdout.
- [ ] Implement `write_split_manifest()` with only IDs, split name, source corpus revision hash and generation timestamp.
- [ ] Run the test again; expected pass.

## Task 2: Normalize Teacher Layout into Reusable Module Samples

**Files:**
- Modify: `backend/tests/test_teacher_module_miner.py`
- Modify: `scripts/teacher_module_miner.py`

- [ ] Write failing tests for `normalize_teacher_case()` using synthetic cuts/welds. Assert that output contains canonical unordered cut signatures, ordered weld signatures, produced/consumed segment counters, pipe demand coverage, and quantities.
- [ ] Implement canonical signatures:
  - cut: `(stock_length, sorted(segment_lengths))`;
  - weld: `(pipe_length, ordered_segment_lengths)`;
  - module: sorted collection of cut and weld signatures plus segment-flow balance.
- [ ] Reject a sample when segment supply cannot cover its weld consumption or when its output cannot be independently reconciled.
- [ ] Preserve source case only in a private audit field outside the exported module key; never use it for matching.
- [ ] Run targeted tests.

## Task 3: Learn Main Recipes, Tail Recipes, and Closed-Loop Modules

**Files:**
- Modify: `backend/tests/test_teacher_module_miner.py`
- Modify: `scripts/teacher_module_miner.py`

- [ ] Write failing tests showing that a 90%-coverage pattern is classified as a main recipe and a low-quantity pattern as a tail recipe.
- [ ] Implement per-pipe coverage concentration, per-cut reuse concentration, and bipartite cut-to-weld segment-flow grouping.
- [ ] Emit module statistics: support cases, total pieces, main coverage, tail coverage, segment count, cut-type count, weld-type count, anchor inventory role and near-fill role.
- [ ] Select module prototypes using train data only, with configurable minimum support and minimum coverage; validation data may select thresholds but may not add prototypes.
- [ ] Write `module_dictionary.json`, `train_summary.json`, and `validation_summary.json` under `data/acceptance/d5_teacher_modules/`.
- [ ] Run module-miner tests and inspect a sample report.

## Task 4: Define Input-Only Module Matching

**Files:**
- Create: `backend/tests/test_d5_module_factory.py`
- Create: `scripts/d5_module_factory.py`

- [ ] Write failing tests showing that a matched module is determined only from current pipe lengths, stock lengths, kerf, weld restrictions and quantities.
- [ ] Implement geometric feature extraction: stock set, stock ratios, pipe-to-stock relation, valid anchors across all stock lengths, feasible complements and near-fill relations.
- [ ] Implement `match_modules(group, dictionary)` that reconstructs candidates from current input; reject any prototype whose exact lengths cannot be generated from the current input.
- [ ] Generate at most one or two main weld templates per pipe type before adding any tail templates.
- [ ] Generate cut templates only when their segment output feeds a selected weld template; remove independent cut-column floods.
- [ ] Run factory tests.

## Task 5: Build Module-First Master Problem

**Files:**
- Create: `backend/tests/test_solve_d5_modules.py`
- Create: `scripts/solve_d5_modules.py`

- [ ] Write failing tests for a synthetic instance where a high-reuse module is selected before an exception column.
- [ ] Build SCIP variables for module activation, module repetitions, and explicitly bounded exception cut/weld columns.
- [ ] Enforce demand, stock capacities, segment-flow balance, forbidden-weld constraints and utilization floor.
- [ ] Lexicographic objectives: minimize active main modules; minimize active tail modules; minimize weld types; minimize cut types; minimize joints; minimize used material. Persist every phase optimum.
- [ ] Export `main_module_coverage`, `tail_piece_ratio`, selected module IDs, cuts and welds.
- [ ] Require independent verification before returning a production-shaped result.
- [ ] Run solver tests.

## Task 6: Comparative Evaluation Harness and Human Review

**Files:**
- Create: `scripts/probe_d5_modules.py`
- Modify: or extend the D5 HTML renderer

- [ ] Build a resumable probe that evaluates train, validation and holdout separately; all metrics must identify the split.
- [ ] For every case write four-dimensional comparison, module coverage, tail ratio, individual layouts and solver phase status.
- [ ] Add aggregate metrics: lex-non-worse rate, human verdict counts, median module coverage, median tail ratio, timeout rate and verifier failure count.
- [ ] Test a small fixed smoke subset first; then run the full 120 D5 legacy-success corpus with an explicit per-case budget.
- [ ] Do not compare a training-case result as evidence of generalization; promotion is based on holdout only.

## Task 7: Promotion Gate and Regression Protection

**Files:**
- Modify: `backend/tests/test_solve_d5_modules.py`
- Create: `scripts/check_d5_promotion.py`

- [ ] Define machine-readable promotion thresholds: no verifier failure, no ID-leakage, nonzero holdout improvement over D5 motif baseline, and documented human review sample.
- [ ] Fail the gate if a new module version reduces the validated holdout non-worse rate or increases tail ratio without a verified four-dimensional compensating improvement.
- [ ] Add a regression snapshot for selected D5 cases and one D1/D2 smoke check proving V4 is untouched.
- [ ] Produce `promotion_report.json` and a concise HTML/Markdown summary under `data/acceptance/d5_teacher_modules/`.

## Task 8: Add Authorized Online Teacher Adapter After Offline Gate Passes

**Files:**
- Create: `scripts/teacher_api_adapter.py`
- Create: `backend/tests/test_teacher_api_adapter.py`
- Modify: `scripts/teacher_module_miner.py`

- [ ] Write failing tests using a fake transport for request normalization, response normalization, request IDs, redacted logs and environment-only credentials.
- [ ] Implement a transport interface with no hard-coded URL, token or secret. Read endpoint and credentials from environment variables such as `TEACHER_API_BASE_URL` and `TEACHER_API_TOKEN`.
- [ ] Require explicit `--allow-online` for outbound calls, default to offline mode, rate limit requests, save response hash/version/status and normalize responses into the same schema as local cases.
- [ ] Run independent verifier on every normalized teacher response before it can enter the training corpus.
- [ ] Keep online samples in a quarantined dataset split until they pass the same leakage, validation and holdout policy.

## Task 9: Replace or Retire D5 Motif v1 Only After Evidence

**Files:**
- Modify: `scripts/solve_d5_motif.py` only as an optional baseline dispatcher
- Modify: project docs under `docs/`

- [ ] Keep `d5_motif_v1` intact as benchmark baseline until module engine clears the promotion gate.
- [ ] Expose module engine only through explicit `engine=d5-modules`/probe mode at first.
- [ ] After holdout and human-review gates pass, make the module engine the D5 candidate default; do not alter V4 production fallback behavior.
- [ ] Archive failed exploratory reports rather than using them for acceptance claims.

## Execution Order and Stop Rules

1. Execute Tasks 1–3 now: they are offline, deterministic, and create evidence rather than another solver tweak.
2. Execute Task 4 only after the module dictionary shows concentrated main recipes and closed-loop support.
3. Execute Tasks 5–6 only after factory tests prove candidates reconstruct solely from current input.
4. Do not touch Task 8 until offline holdout evaluation is complete and authorized API documentation is available.
5. If Task 3 shows no reusable structure on the holdout distribution, stop module-engine implementation and report that the available offline corpus cannot support a general rule learner; then use the authorized teacher API only to collect diverse, authorized training examples.
