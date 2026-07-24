# V4 分层接入指纹集合覆盖（实施计划）

**Goal:** 以 V4 为主干；指纹 construct 仅作焊接无解/弱解兜底；pure_cut 不改；全档 D1–D5 复测。

**Architecture:**
1. `classify → pure_cut` 早退：不动。
2. `weld_then_cut → lex → ladder` 之后若仍无解（或 best 为空），调用 `construct_columns + solve_setcover`。
3. setcover 解补齐 `weld_types/cut_types/seg_types/used_len`，纳入 `_lex_key` 比较。

**不做什么:** 不用 construct 替换整条 V4；不为题号特判。

---

## Tasks

- [x] 增强 `solve_setcover` 产出 V4 兼容字段
- [x] `solve_v4_production` 挂接 `fingerprint_setcover` 兜底
- [x] `scripts/probe_v4_all_tiers.py` 全档 success 复测
- [x] 跑完并写 `data/acceptance/v4_all_tiers_probe/summary.json`

## 全档复测结果（2026-07-24，tl=12，workers=4，n=1586）

| 档 | n | 有解率 | 无解 | util劣 | fp兜底命中 | 非劣率 |
|---|---:|---:|---:|---:|---:|---:|
| D1 | 347 | 98.8% | 4 | 21 | 7 | 76.9% |
| D2 | 581 | 94.0% | 35 | 43 | 47 | 52.7% |
| D3 | 325 | 80.9% | 62 | 83 | 23 | 26.2% |
| D4 | 213 | 63.4% | 78 | 56 | 6 | 13.6% |
| D5 | 120 | 36.7% | 76 | 22 | 0 | 2.5% |
| 全 | 1586 | 83.9% | 255 | 225 | 83 | 43.5% |

对照改前基线（D1/D2 `*-full-compare`，tl=15）：

- **D1**：无解 7→4（有解↑）；util劣 11→21、劣于(焊口) 3→9（质量略退）
- **D2**：无解类 53+TIMEOUT1→35（有解明显↑）；非劣率约 58%→53%（多解出的案里 util劣/焊口劣增多）
- **D5**：fp 兜底 0 命中，列生成仍盖不住极难案

明细：`data/acceptance/v4_all_tiers_probe/summary_brief.json`
