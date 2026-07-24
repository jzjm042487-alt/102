# D5 Motif Setcover Implementation Plan

> **For agentic workers:** Execute task-by-task. HTML **reuses** `scripts/_gen_report.py`.

**Goal:** D5-only motif column factory + SPEC-lex setcover; probe emits reusable SVG HTML for human review of weld/cut patterns.

**Architecture:** `d5_column_factory` enriches teacher construct with all-stock 2/3/4-seg anchors → `solve_d5_motif` SCIP lex phases + layout export → `probe_d5_motif` writes JSON + `_gen_report._write_report` HTML. V4 production untouched.

**Tech Stack:** Python, PySCIPOpt, existing `teacher_inspired_construct_v0` helpers, `_gen_report` HTML_TMPL, legacy corpus.

---

### Task 1: Column factory + solver
- Create `scripts/d5_column_factory.py`, `scripts/solve_d5_motif.py`
- Smoke: idx 1756 / 390 with `--tl 120`

### Task 2: Probe + HTML reuse
- Create `scripts/probe_d5_motif.py`
- Output `data/acceptance/d5_motif_probe/report.html` via `_gen_report._write_report`

### Task 3: Smoke then expand
- Small set first, then full D5 success (budget via `--tl`)
