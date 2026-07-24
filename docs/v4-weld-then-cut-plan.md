# V4 先拼后切（Weld-then-Cut）历史方案

> **版本**：2026-07-23  
> **状态更新（2026-07-24）**：不再是默认生产路径。先拼后切 + 启发式反馈不满足当前“一个耦合模型、配方激活、四阶段锁定”的权威要求；只保留为历史失败模式与可迁移库存校验机制的参考。
> **当前权威**：`docs/SPEC.md` 与项目 Skill `production-grade-nesting-solver`。

---

## 1. 结论（为何改主路）

| 旧主路（Recipe → LexMaster） | 问题 |
|---|---|
| Recipe 保证「单根管可达」 | 不保证多根管在**紧库存**下的共切可行 |
| Lex 纯压焊口 | Cr 等题选出 pack-hostile 段需求 → Pass A 不可行 → FFD 兜底 |
| 继续堆 arc-flow 补丁 | 用户明确拒绝 |

| 新主路（S1 → S2 → 反馈） | 作用 |
|---|---|
| **S1** 库存感知混拼 | 先定合法焊拼，且段需求对母料可切 |
| **S2** 经典一维 CSP | 给定段需求做下料（SCIP） |
| **反馈** | S2 失败则换种子/主互补段再试 |

**不做**：把老软件题解写死当算法；不做无库存意识的「纯最小焊口」。

---

## 2. 算法骨架

```text
classify / pure_cut 快路
        │
        ▼
S1: 互补连锁混拼（complement-linked）
  1. Σ 闭包 + 合法焊拼枚举（已有）
  2. 过滤 pack-hostile 段（近母料假段 / 巨大段无足够 mate）
  3. 种子：某管型的纯二段互补 (a,b)，且 a、b 均可在最长母料上配对
  4. core = {mate(a), mate(b)} on max stock
  5. 短管强制整段（1-seg）
  6. 紧缺母料整段嵌入：优先含 primary∈core 的三段拼；可留 1 根紧缺不耗尽
  7. 其余长管：优先 core + 最长母料 填充
        │
        ▼
S2: cut_csp_ilp（段需求 × 有限库存 CSP，min used_len）
        │
   失败 → 换种子 / 换 primary / 少耗紧缺 → 再 S1
        │
        ▼
达标则返回；否则 Recipe+Lex → ladder（旧路径保底）
```

### 2.1 目标词典序（不变）

焊口 → 拼法种类 → 切法种类 → 段种类 → util（util 硬 floor ≥ max(0.95, target)）。

### 2.2 泛化性

- 种子与 primary 由几何/库存导出，**无 113146 / 老软件常数**。
- 验收可用老软件指标对比，但求解器不得依赖其拼法表。

---

## 3. 文件

| 文件 | 职责 |
|---|---|
| `scripts/weld_then_cut.py` | S1+S2 主实现：`solve()` |
| `scripts/solve_v4_production.py` | L0：needs_weld 先 `weld_then_cut`，再 Lex/ladder |
| `docs/v4-production-ultimate-plan.md` | 战略文档改指向本主路 |

---

## 4. 验收门禁（113146）

| 题 | 墙钟 | util | 焊口参考（老软件） |
|---|---|---|---|
| T91 | ≤4s 目标 / ≤15s 门禁 | ≥ floor | 对比 baseline |
| Cr | ≤9s 目标 / ≤15s 门禁 | ≥ floor | ~115 焊口、util≈0.997 |

Verifier 必须通过；禁止 `LEGACY_*` 硬编码求解路径。
