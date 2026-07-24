# V4 生产终极方案（Ultimate Plan）

> **版本**：2026-07-23（历史方案）  
> **状态更新（2026-07-24）**：本方案的“唯一生产路线”定位已废止。它采用焊口优先、解耦/ladder 与多个 fallback，和当前权威的耦合四阶段模型不一致。  
> **当前权威**：`docs/SPEC.md` 与项目 Skill `production-grade-nesting-solver`；本文件只保留为历史证据，不能据此扩展生产路径。  
> **详细施工步骤**（函数/文件/日计划/门禁）：见 [`v4-production-implementation-plan.md`](v4-production-implementation-plan.md)（**2026-07-23 评审修订版**）

---

## 0. 北极星（生产定义）

满足以下全部条件，才称 V4 生产就绪：

| # | 标准 | 量化建议 |
|---|------|----------|
| P1 | **可行必出解** | 物理可行组（stock≥demand 且工艺合法）API 返回合法方案，**无解率 < 1%**（语料固定 50 例 seed） |
| P2 | **词典序** | 焊口 → 拼法 → 切法 → 段 → util；util = `max(0.95, legacy-1e-3)` **硬 floor**，非主目标 |
| P3 | **时限** | 用户 `time_limit` 硬切；单组不超分配预算；无「tl=120 实际跑 600s」无解释 |
| P4 | **可观测** | 每解带 `solver_path`、`diagnosis[]`、各阶段耗时；无解必说明阶段与原因 |
| P5 | **校验** | 100% 过 production verifier（段平衡、禁焊、库存、需求覆盖） |
| P6 | **回归** | 真 legacy 档 + 语料固定集；**不劣化** = 词典序逐项 ≤ 老（或 legacy=null 仅报 V4 指标） |

**默认引擎**：`engine=v4` → 内部只调 **`solve_v4_production`**（见 §2），不再让调用方理解 arc-flow/cutcg/CG 分裂路径。

---

## 1. 战略结论（基于既往讨论，不再辩论）

1. **配方段（需求对齐 Σ）是缺的那一层**，不是 SCIP 不够快。  
2. **编排重构 + 复用组件**，不是推倒重写，也不是只调 `_arcflow_v3` 参数。  
3. **baseline 的 Tier/标准段调研有效，但必须迁入 V4 主路径**（事前 Σ，不是仅 baseline fallback）。  
4. **arc-flow / cutcg / CG / FFD / pure_cut** 全部是 **Fallback 组件**，不是默认世界观。  
5. **伪 legacy 已清理**；验收只用语料真实 Success + `legacy_claimed` 备查。  
6. **L20/超大组** 单独 SLA，不阻塞中等组生产上线。

---

## 2. 目标架构（唯一生产管线）

```text
                    solve_v4_production(group, tl, util_floor)
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         ▼                          ▼                          ▼
   classify + pure_cut         Weld-then-Cut               动态预算
   (0焊口快路)                 S1 互补连锁混拼              (WTC 上限；失败则剩余给 Lex/Ladder)
                               S2 经典 CSP
                               （库存感知，无题解写死）
         │                          │
         │                          ▼ 失败/util 不足
         │                   Recipe + LexMaster（refine）
         │                          │
         └──────────────► _lex_better ◄──────────────────────┘
                                    │
                         仍失败 → Fallback Ladder
```

> 详案见 [`v4-weld-then-cut-plan.md`](v4-weld-then-cut-plan.md)。Recipe+Lex 保留为 refine/兜底，不再作为紧库存题的唯一主路。

### 2.1 模块与文件规划（新建 + 复用）

| 模块 | 建议路径 | 复用来源 |
|------|----------|----------|
| **L0 Orchestrator** | `scripts/solve_v4_production.py` | 新建；`route_v4_arcflow` 只调此入口 |
| **L1 RecipeBuilder** | `scripts/recipe_builder.py` | 逻辑来自：`_standard_segment_lengths`、`_residual_seg_lengths`、语料导读「满装对」、整管长 |
| **L2 LexMaster** | `scripts/lex_master.py` | 抽取/包装 `_compress_types`、`_enum_pipe_patterns`、`_enum_cut_patterns`；整数 Phase 对齐 `_exp_colgen.solve_integer` |
| **Fallback** | `scripts/v4_fallback.py` | 调用 `_arcflow_v3`（restricted）、`_try_colgen_fallback`、`_ffd_weld_incumbent`、`_try_std_compress`、`solve_pure_cut` |
| **契约** | `backend/app/route_v4_arcflow.py` | 传 `util_floor`；`_reconcile_to_counts` 不变 |
| **baseline Tier** | `backend/app/solver.py` | **保留**为 `engine=baseline` 与 v4 失败最终兜底；不再单独演进 |

### 2.2 RecipeBuilder v1 规则（生产最小集）

**输入**：`MaterialGroup`, `util_floor`  
**输出**：`sigma_weld` / `sigma_cut`（默认 \|Σ\|≤48，**可调**；以「每管禁焊可达」优先于死守 48）, `seed_*`, `meta`

生成顺序（合并去重，超限按优先级截断）：

1. **T0 整管**：所有 `L ≤ max_stock` 的管长。  
2. **残料族**：复用 `_residual_seg_lengths`（**含 kerf**）。  
3. **组标准段**：import `_standard_segment_lengths`；每 stock 的 S/2、S/3；每管至多 1 个尾段。  
4. **满装对闭包**：主母料上 Top-K，`a+b+kerf ≤ S`，且 a/b 对至少一根管**拼层可达**（禁止笛卡尔全爆）。  
5. **禁焊可达剪枝（强制）**：对每管跑 `_enum_pipe_patterns`；不可达则邻域补残料一轮；仍不可达 → `hard_unreachable`，跳过 Lex 进 Fallback。  
6. **截断**：可达必需段不可丢；再按 T0→残料→标准段→满装对丢弃。

**不做 v1**：语料 catalog 自动写 Σ（放 v2）；全母料背包配方枚举。

### 2.3 LexMaster v1 词典序（与 SPEC 一致）

在 **Recipe 给的 Σ_weld / Σ_cut + seed 列** 上：

1. **可行性**：`used_len ≤ demand_length / util_floor`；段约束 `produced ≥ consumed`，导出后 **必须** `_reconcile_to_counts`  
2. **Pass A**：`min joints`（主）  
3. **Pass B**：在 **Pass A 实际段子集 σ_A** 上 `min weld_types`（默认；完整 Σ 仅可选加一轮）  
4. **Pass C**：固定 joints + weld_types，`min cut_types`  
5. **Pass D**：固定前三项，`min used_len`；比较含 `seg_types`

**列预算**：`col_cap`（如 20000）；DFS/枚举截断写入 `diagnosis` → Fallback，而非死循环。

### 2.4 Fallback Ladder（统一、有序；仅 L0 一层）

| 阶 | 触发 | 动作 | 交付条件 |
|----|------|------|----------|
| 3a | Lex 无解/超时/缺段 | **缺段回灌 → 再 Lex 一轮**；仍失败才 restricted arc-flow（Σ∪missing），短 tl | 更优则采纳 |
| 3b | 仍差且 demand≤800 | CG **仅在 Σ_recipe 上增列**，joints-first 整数 | 更优则采纳 |
| 3c | 仍无解或焊口/花样极差 | FFD-weld → **必须** `_try_std_compress`；**禁止**裸 `ffd_weld` 交付 | 必出可行解 |
| 3d | 有 pure_cut | `_lex_better(..., util_floor)`（含拼/切/段） | 0 焊口保底 |

**超长管**：classify 为 `all_stock_short` 时 **跳过「Pass1 失败即 None」**；直接允许 3c。  
**临时交付**：W1–W2 可用配置走 `rcg` 保业务不停工；Phase2 墙钟门禁通过后关闭（见实现计划 §7.1）。

---

## 3. 与现有调研的「收编」关系（不再分散投资）

| 原调研 | 终极方案中的位置 | 动作 |
|--------|------------------|------|
| 段长收敛 Tier T0–T3 | RecipeBuilder + LexMaster 列 cap | **迁入 v4**；baseline 仅 maintenance |
| 标准段「甲」 | RecipeBuilder §2.2 第 4 条 | **正式产品化** |
| V4 compress Pass3–6 | LexMaster 核心 | **提升为主路径** |
| arc-flow / cutcg | Fallback 3a | **降级** |
| _exp_colgen | Fallback 3b + 列扩展 | **限制在 Σ_recipe** |
| route3 / global / rcg | 不参与 v4 主链 | **保留可选 engine**，文档标注「对照/实验」 |
| GA / GPU / LNS | 不进入 v4 主链 | **归档**；超大组 future 可选 GA 搜 Σ |
| 语料高频段 | RecipeBuilder v2 | 上线 v1 后再做 |

---

## 4. 分阶段实施（唯一工期表）

### Phase 0 — 止血（1 周，可热修）

**目标**：不改变主架构，无解率下降、生产口径正确；完成 113146 Recipe 探针（禁焊可达 / \|Σ\|）。

- [ ] `route_v4_arcflow` 传 `util_floor`（无 legacy 用 `max(0.95, target_rate)`，配置 documented）  
- [ ] `solve_arcflow`：`internal_fallback` 开关；L0 上线后内部兜底关闭，只由 `v4_fallback` 负责  
- [ ] 禁止 verifier 通过前的裸 `ffd_weld` 对外（2201 类）  
- [ ] 子进程硬超时封装进现有脚本（语料回归 CI 用）  
- [ ] `probe_recipe_113146`：两题每管拼列≥1，\|Σ\| 落盘

**验收**：语料 20 例 **无解 = 0**；idx 2318 有解；探针 V0/V1 通过。

### Phase 1 — 生产编排壳（2 周）

**目标**：`solve_v4_production` 上线，内部仍可调旧组件。

- [ ] 新建 L0：预算切片、diagnosis 汇总、verifier 闸门  
- [ ] `engine=v4` 仅调 L0  
- [ ] baseline fallback 保留在 L0 最末（可选开关 `V4_ALLOW_BASELINE_FALLBACK=1`）

**验收**：picked20 真基准 **无回归**；API 返回含 `solver_path`。

### Phase 2 — RecipeBuilder + LexMaster v1（4–6 周，**核心**）

**目标**：默认路径走配方段 + 有限列 ILP。

- [ ] 实现 `recipe_builder.py`（§2.2）  
- [ ] 实现 `lex_master.py`（§2.3）  
- [ ] L0 默认：classify → Recipe → LexMaster → Ladder  
- [ ] Fallback 3a–3d 接入

**验收**：

- 语料固定 50 例：**不劣化 ≥ 75%**，无解 = 0  
- 焊口劣例 1297/1503/1107/1365：**至少 3/4 打平或优于**  
- picked20：L8/L12/L16 优势保持；L4 切法 +1 可接受

### Phase 3 — 规模档与 L20（4 周+，并行于 Phase 2 后期）

**目标**：不挂死、花样数量级下降。

- [ ] demand>800：跳过裸 CG；**管型簇聚合** 或 **分 sub-group Recipe**  
- [ ] L20 SLA：120s 内 util≥floor，拼法 <200（先数量级，再追 14）  
- [ ] 可选：`engine=global` 作 mega 组第二选择（文档化，非 v4 内置）

**验收**：L20 无 13min 挂死；拼法/切法较现 FFD **降 1–2 数量级**。

### Phase 4 — 生产化（2 周）

- [ ] CI：`scripts/_corpus_run.py` 固定 seed + 阈值门禁（无解=0，不劣化率≥X%）  
- [ ] 更新 `SPEC.md` §五/§六：**v4 production = Recipe+LexMaster**  
- [ ] `v4-acceptance-report.html` 生成器读 `solver_path`/diagnosis  
- [ ] 归档：`docs/research/*` 加索引表「已收编 / 对照 / 废弃」

---

## 5. 验收与数据体系（终极、不再改口）

### 5.1 三套数据集

| 集 | 用途 |
|----|------|
| **picked20 真基准** | legacy≠null 的 12 档，词典序对标 |
| **picked20 无基准** | legacy=null，仅 V4 快照回归 |
| **语料固定 50** | seed=0，中难+需焊+单 group，**生产门禁** |

### 5.2 指标输出（每次回归必报）

每档/每例：**焊口、拼法、切法、util、solver_path、elapsed、verdict**。

### 5.3 明确放弃的对标

- 不对 `legacy_claimed` 做「必须超越」  
- 不追 L9/L11 等已证伪基准的数字

---

## 6. 组织与决策（避免再次分裂）

1. **唯一 owner 路径**：`solve_v4_production`；PR 若只改 `_arcflow_v3` 默认行为而无 L0 调用，需说明理由。  
2. **新调研准入**：必须回答「如何进 RecipeBuilder 或 Fallback，否者只做 notes」。  
3. **引擎策略**：  
   - 产品默认：**v4 production**  
   - 对照：`baseline` / `route3` / `rcg` / `global`  
   - 前端可保留，文档标注「非默认生产」  
4. **时间盒**：Phase 2 未完成前，**不再开 L20 全枚举类原型**。

---

## 7. 风险与对策

| 风险 | 对策 |
|------|------|
| Recipe Σ 仍缺关键段 → 焊口劣 | 3a restricted arc-flow **只负责补段**，结果回灌 Σ 重跑 LexMaster（一轮） |
| col_cap 仍 skip | 分簇 LexMaster；先 min joints 再 compress，别一次枚举过大 |
| 与 baseline 双轨维护 | Phase 4 后 baseline Tier **冻结**；仅 bugfix |
| 性能 | Σ≤48、子进程超时、demand 分档 |

---

## 8. 终极方案一句话

**以 RecipeBuilder（需求对齐小 Σ）+ LexMaster（词典序有限列 ILP）为唯一默认生产路径；把 baseline 调研过的 Tier/标准段收编进 Recipe；把 arc-flow/CG/FFD 收成可观测的 Fallback 梯；用语料 50 例 + 真 legacy 12 档做 CI 门禁——在此之上 v4 才算生产就绪。**

---

## 9. 附录：Phase 0 立刻可执行的命令（给接手人）

```bash
cd 102
# 语料门禁（应先无解→0）
.venv\Scripts\python.exe scripts\_corpus_run.py --n 20 --tl 60 --proc_timeout 200 --seed 0

# picked20 全指标（必须 util_floor=legacy）
.venv\Scripts\python.exe scripts\_cmp_metrics.py 3,4,6,8,12,13,14,15,16,17,20 --tl 120

# 单例无解诊断
.venv\Scripts\python.exe scripts\_diag_nosol.py 2318 90
```

Phase 1 起：新增 `scripts/solve_v4_production.py` 并在 `route_v4_arcflow.solve_group` 唯一调用。
