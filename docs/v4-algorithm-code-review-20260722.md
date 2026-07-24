# V4 Arc-Flow 算法实现代码审查报告（历史审查记录）

> **状态更新（2026-07-24）**：审查结论基于焊口优先和多引擎 fallback 架构。现仅作为缺陷证据；当前算法契约以 `docs/SPEC.md` 和项目 Skill `production-grade-nesting-solver` 为准。
>
> **角色**：高级算法工程师视角  
> **日期**：2026-07-22  
> **审查对象**：`scripts/_arcflow_v3.py`、`scripts/_exp_colgen.py`、`backend/app/route_v4_arcflow.py`、`backend/app/route_c_bp.py`  
> **对照规格**：`docs/SPEC.md`（词典序、硬约束）、`docs/v4-arcflow-implementation-plan.md`（S1–S6 计划）

---

## 0. 执行摘要（给接手 agent 的三句话）

1. **V4 不是空壳**：已实现「分类器 → 耦合 arc-flow（拼层+切层）→ 词典序多 pass → 产出段集上全列式种类压缩」的完整管线，并在部分 picked20 / 语料样本上明显优于老软件。  
2. **但工程上未完成**：列生成（CG）未接入主路径；生产 API 未传验收用 `util_floor`；`cutcg` 与「标准 arc-flow」两条路径行为不一致；大量案例在时限内 **Pass1(min 焊口) 无解即整题返回 None**，没有 FFD/纯切兜底。  
3. **真实语料 20 例抽样**：优于 8、焊口劣 4、拼法劣 1、**无解 7（35%）**——当前最大短板是 **「简单 2 管型仍无解」**，不是 L20 单点。

---

## 1. 系统里到底有几条求解路径（必须先看懂）

实际运行时存在 **多条并行演进、未完全统一的引擎**，文档与代码不一致：

| 路径 | 入口 | 状态 | 说明 |
|------|------|------|------|
| **GA + ILP** | `scripts/_exp_ga.py` | SPEC §六仍标为「当前主力」 | 词典序在 GA 适应度里；与 V4 是不同产品线 |
| **V4 Arc-Flow** | `route_v4_arcflow.solve_group` → `solve_arcflow` | 可选 `engine=v4` | 本审查重点 |
| **Route-C BP** | `route_c_bp.py` | 独立分支 | 列生成 + 后接 `_compress_types` |
| **CG 实验** | `_exp_colgen.solve_colgen_full` | **仅实验 + 窄条件兜底** | 见 §4.3 |

**结论**：用户口头说的「V4」在代码里 = `_arcflow_v3.solve_arcflow`；**不是**完整的「动态列生成重构版」；CG 只是 `ffd_weld` 失败后的补丁，且大规模档被闸门跳过。

---

## 2. 方法的具体实现（按数据流拆解）

### 2.1 输入与预处理

- **契约层**：`backend/app/domain.py` — 毫米整数、禁焊区、`max_joints` 工艺钳制等（与 SPEC §三一致）。  
- **等价管型合并**：`merge_equivalent_pipes`（`_exp_colgen.py`）— V4 与语料脚本都会调用，减少重复管型。

### 2.2 前置分类 `classify_group`（` _arcflow_v3.py:164`）

逻辑：

1. 料总长 < 需求总长 → `infeasible`  
2. **最长母料 < 最长管** → 直接 `needs_weld`，**且不跑** `solve_pure_cut`（`pure_result=None`）  
3. 否则跑 `solve_pure_cut`，若 `util >= util_floor` → `pure_cut` 快路（0 焊口）  
4. 否则 `needs_weld`，但保留 `pure_result` 作后续 `_better_of` 候选  

**算法含义**：用一次 cutting-stock（整管为段、无焊口）判断「纯切是否已满足利用率底线」。

**短板（与无解强相关）**：

- 分支 2「超长管」**永远不生成** `pure_fallback`；若后续焊接 MILP 失败，只能 `None`。  
- 语料 idx **2318** 即此类：`管长 14250 > 母料 12000`，老软件有解（60 焊口），V4 在 90s 内 Pass1 超时 → **整题 None**（见 §6.1）。

### 2.3 拼层：离散焊点 + 管身弧 `build_pipe_arcs`

- 焊点集合 `_weld_points`：在 `min_wd` 网格上 snap，并受母料长度集合诱导（库存边界）。  
- 每条弧 `(a,b,seg)` 表示管上从位置 `a` 到 `b` 的一段，段长 `seg`。  
- **段字母表 Σ 的来源**：所有管型弧上的 `seg` 并集 — **不是**老软件式的「需求对齐配方枚举」，而是 **固定网格 + 焊点可达段**。

**根本限制**：若最优解需要「非网格上的关键段长」（例如精确残料 11335），除非该点落在 `_weld_points` / residual grid 上，否则 **主模型根本表达不出** 该解。

### 2.4 切层：Valério de Carvalho 规范化 arc-flow

- 对每种母料长度 `L`，在段集 `seg_sorted` 上建 **非增序** 整数流，变量为段弧 + 废料弧 + `used` 根数。  
- 与拼层 **耦合**：标准路径用 `==` 段平衡（`solve_arcflow` 612–626）；`cutcg` 子问题用 `>=`（1649–1662），依赖后续 `_maybe_compress` 用 `==` 全列式修正 — **若压缩跳过则平衡口径与 verifier 可能不一致**。

### 2.5 主路径 `solve_arcflow`（切层弧数 ≤ `CUTCG_ARC_THRESHOLD`）

**词典序实现（与 SPEC 对齐部分）**：

| 阶段 | 代码位置 | 实际优化目标 | 与 SPEC 关系 |
|------|----------|--------------|--------------|
| Pass1 | ~641–678 | `min joints`，约束 `usedlen <= demand/util_floor` | 焊口第一 ✓ |
| 探路 | floor 不可达时 | `min usedlen` → 再 `min joints`（含 FFD used_len 上界） | 底线可行性 ✓ |
| Pass2 | ~689–705 | 固定 `joints <= J*`，`min usedlen` | 末档利用率 tiebreak ✓ |
| Pass3–6 | `_compress_types` | `min 拼法 → 切法 → 段种类`，再 `min 焊口` | 对应 SPEC 2–4 档 ✓ |

**未在主线 MILP 内做的**：Pass2 之后 **没有** 在原始 arc-flow 流变量上直接加 `zW/zC`（种类在 **后处理压缩** 里做）。计划书 S2/S3 的「路径变量 + setup」只在 `_compress_types` 的全列式里出现。

**Pass1 失败行为（严重缺陷）**：

```text
if m.getNSols() == 0:
    return _better_of(None, pure_fallback, group, verbose)
```

- 若 `pure_fallback is None`（超长管分类）且 SCIP `timelimit` → **直接 None**。  
- **不会**像 `solve_arcflow_cutcg` 末尾那样调用 `_ffd_weld_incumbent`。  
- 这解释了语料中多例「2 管型、老软件有解、V4 无解/超时」。

### 2.6 极限档 `solve_arcflow_cutcg`（切层弧爆炸）

触发：`n_cut_arcs > CUTCG_ARC_THRESHOLD`（约 20 万弧量级）。

**Coarse-to-fine**：

- 多档 `seg_grid`：step 网格 ∩ full_segs，可选 **residual_grid**（门控：`len(pipes)<=3` 且存在 `pipe.length > max_stock`）。  
- 每档 `_solve_int_restricted`：`min joints`，`usedlen <= stock_length`（**不用** `util_floor` 作 cap，注释写明避免 L9 类 infeasible）。  
- 时间：`PROBE_CAP=10s` / `IMPROVE_CAP=30s` — **总时限 tl 再大也会被档内封顶吃掉**（L11 600s 仍 147s 结束的原因之一）。  
- 全档失败 → `_ffd_weld_incumbent` → 可选 `_try_std_compress`（标准段 + `_compress_types`）。  
- 有解 → `_maybe_compress`（段集来自 best 的 cut/weld patterns）。

**FFD 兜底特征**：几何构造，util 高，**拼法/切法种类≈需求根数级**（语料 idx **2201**：拼法 35、切法 36 vs 老 5/8，词典序判「优于」仅因焊口 35<36）。

### 2.7 种类压缩 `_compress_types`（核心「压花样」实现）

**算法**：在 **有限段集** `seg_set` 上：

1. `_enum_pipe_patterns`：DFS 枚举合法拼法路径（budget 5万）  
2. `_enum_cut_patterns`：枚举母料切法列（budget 20万，可选 `max_parts`）  
3. MILP：`xp` 拼法列 + `yc` 切法列 + `zW/zC/zS` 二元 setup  
4. 多 pass 冻结：Pass3 拼法 → Pass4 切法 → Pass5 段 → Pass6 焊口  

**保护阀**：`total_pats + total_cuts > col_cap`（默认 20000）→ **整段压缩跳过**，保留原解。

**短板**：

- 压缩只在 **Pass2 实际产出段集** 或 cutcg best 的段集上运行；段集稍大即跳过 → **L16 切法 18 vs 老 11**、语料 **拼法劣** 仍常见。  
- `_enum_pipe_patterns` 在「产出段集内无合法拼法」时 **整段返回 None**（848–850），不会降级。  
- Pass6 失败时有 fallback 到 Pass5 可行解（1005–1007），逻辑复杂，调试成本高。

### 2.8 列生成 `_exp_colgen.py`（实验管线，未生产化）

**已实现**：

- RMP LP（连续）+ `price_weld`（DP/RCSP 风格）+ `price_cut`（背包）  
- `candidate_lengths` + `_residual_split_lengths`（超长管残料段）  
- `solve_integer`：Phase0 min 用料 → Phase1 min 焊口（+ 拼法种类惩罚）→ `consolidate_types` 贪心删列  
- `solve_colgen_full`：FFD 种子 → CG 循环 → 整数解  

**接入点**：`_try_colgen_fallback` **仅当** `solve_arcflow_cutcg` 最终结果 `solver == "ffd_weld"` 时调用。

**硬闸门**：

```python
if total_demand > 800:
    return None  # 跳过 CG，避免 L20 级 RMP 挂死
```

**已知问题**：

- `from scripts._exp_colgen import ...` 在部分 cwd 下可能 `ImportError`（应用 `from _exp_colgen` 更稳）。  
- CG 整数 Phase 仍可能在 **错误段池 Σ** 上证明「170 焊口最优」（L11），追不上老软件结构。  
- **未**对标准 `solve_arcflow` 主路径（非 ffd_weld）调用 CG。

### 2.9 生产适配 `route_v4_arcflow.py`

```python
res = solve_arcflow(group, tl=time_limit, verbose=False)
```

**缺失**：

- **未传 `util_floor`** → 退回 `group.target_rate`（常 0.9925），比验收脚本用的 `legacy_util` **严得多** → 易 infeasible / 误选焊接解。  
- 无解返回 `None` → `solver.py` 回退 baseline，**前端可能看不到 V4 诊断码**。

### 2.10 Route-C BP 中的压缩

- 列生成求可行解后调用 `_compress_types`，`seg_set > 12` 直接 skip。  
- 与 arc-flow 主路径共享同一压缩器，**优点**：种类压减逻辑一致；**缺点**：BP 池子碎列多，压缩常 skip。

---

## 3. 与《实现计划 S1–S6》的对照：完成了什么、没完成什么

| 阶段 | 计划内容 | 代码现状 | 完成度 |
|------|----------|----------|--------|
| **S1** | Pass1 改为 util floor；焊口优先 | `solve_arcflow` Pass1/2 + `_util_floor` | **主路径已完成**；生产 API 未传 floor |
| **S2** | 主线 MILP 内 min 拼法种类 | 仅在 `_compress_types` Pass3 | **部分**：后处理式，非计划中的主线 xp/zW |
| **S3** | min 切法种类 | `_compress_types` Pass4 | **部分** |
| **S4** | min 段种类 + util | Pass5 + Pass2 usedlen | **部分** |
| **S5** | 热启动 + local branching | cutcg warm start、compress Pass6；**锚点段集已回退** | **约 40%** |
| **S6** | 结构化诊断 | `_diagnose` 已挂到 result | **基本完成** |
| **列生成重构** | 动态拼法列，逼近老软件 | 实验脚本 + 窄兜底 | **未生产完成** |
| **L20 配方 CG** | 管型聚合配方 ILP | `_l20_recipe.py` 等已删/放弃 | **未做** |
| **全量回归无退化** | 接入后 20 样本 | picked20 部分优；语料 35% 无解 | **未达标** |

**文档漂移**：`SPEC.md` §六仍写 GA 为生效路径；V4 在 `solver.py` 为 `engine=v4` 可选 — **接手者必须以代码分支为准，不能只看 SPEC 第六章**。

---

## 4. 当前短板（按算法根因分类）

### 4.1 可行性 / 无解（P0）

| 现象 | 根因 | 代码锚点 |
|------|------|----------|
| 超长管档 Pass1 超时 → None | `all_stock_short` 无 `pure_fallback`；主线无 FFD 兜底 | `classify_group:188-191`, `solve_arcflow:679-683` |
| 语料 7/20 无解 | 同上 + 时限内 MIP 难求整数解 | `_corpus_run_out.json` |
| idx 2318 复现 | 1 管型×60，14250>12000，504 拼弧，Pass1 `timelimit` 90s | `_diag_nosol.py` 实测 |
| 生产更严 | `route_v4` 无 `util_floor`，target 0.9925 | `route_v4_arcflow.py:126` |

### 4.2 焊口劣于老软件（P1）

- 段集表达不足：网格焊点未覆盖老软件用的 **精确残料段/配方段**。  
- `cutcg` 单档时间过短，joint_cap 收紧后 warm start 失败则档内无解。  
- 压缩 Pass6 仅在种类 pass 之后补焊口，**不能挽回** Pass1 就选错的段结构。

### 4.3 拼法/切法散乱（P2）

- FFD 兜底：`weld_types ≈ 管数`，`cut_types` 极大（2201、L20）。  
- `_compress_types` 因 `col_cap` 或 `seg_set>12`（BP）跳过。  
- 词典序判「优于」可仅因焊口少 1，**掩盖花样灾难**（验收脚本需同时看拼/切）。

### 4.4 性能 / 时限语义（P1）

- `solve_arcflow(tl=120)`：**多 pass 共用同一 `limits/time`**，且 cutcg **PROBE/IMPROVE 封顶** — 外层加大 tl 无效。  
- CG：`demand>800` 跳过；否则 RMP 重建代价大。  
- 子进程验收必须用 **硬 kill**（`_corpus_run.py`），否则单档可跑 200s+。

### 4.5 验收与指标口径（P1）

- 老软件拼/切种类需从 `Result.WeldingPattern/CuttingPattern` **重算**（`_corpus_worker.legacy_metrics`）。  
- 必须与 `util_floor=legacy_util` 一致，否则 L4 类误判（已踩坑）。

---

## 5. 未完成程序部分（明确清单）

1. **列生成生产接入**  
   - 非仅 `ffd_weld` 触发；需与 arc-flow 共享词典序整数恢复；大规模档需 **聚合配方 / 分治**，不能裸 `solve_colgen_full`。

2. **主线失败兜底链统一**  
   - `solve_arcflow` 在 Pass1 失败时应与 cutcg 对齐：`FFD → CG(可选) → pure(若有) → _better_of`，而不是 `None`。

3. **`classify_group` 超长管分支**  
   - 应保留「焊接无解时的几何可行解」至少 FFD；可考虑「纯切不可行但已知必须焊」仍缓存 `solve_pure_cut` 的松弛信息（若可）。

4. **生产 `util_floor` 策略**  
   - API 层：无 legacy 时用 `max(0.95, target_rate)` 还是固定 0.95 需产品定；**不能**静默用 target 当 floor。

5. **cutcg 与 util_floor 统一**  
   - cutcg 用 `stock_length` 作 used 上界，与主线 `demand/util_floor` 不一致，导致「同题两路径不同可行域」。

6. **L20 / 大规模**  
   - 闸门后仅 FFD；`_try_std_compress` 对混合母料常失败；配方级枚举已证明组合爆炸。

7. **伪基准后续**  
   - `legacy=null` 的 8 档需 V4 官方基准（用户 todo b3），否则回归无对照。

8. **SPEC 与代码同步**  
   - 更新 §五/§六：V4 arc-flow 已是并行引擎；GA 是否仍默认需确认 `solver.py` 默认 engine。

---

## 6. 典型失败案例（便于下一位 agent 复现）

### 6.1 无解：语料 idx 2318

```text
管型: (14250, demand=60), max_joints=4
母料: (12000, qty=102)
老软件: joints=60, util≈0.95, weld_types=2, cut_types=3
分类: needs_weld / all_stock_short, pure_result=None
拼层: 504 弧, 137 段种类
Pass1(min焊口): timelimit 90s → 无解 → RESULT None
```

**修复方向（建议）**：Pass1 失败 → `_ffd_weld_incumbent`；加长时限或降模（单管型可专用 2 段枚举）；传对 util_floor。

### 6.2 花样爆炸：语料 idx 2201

```text
V4: joints=35, weld_types=35, cut_types=36, solver=ffd_weld
老: joints=36, weld_types=5, cut_types=8
词典序: 「优于」（仅焊口）
```

**修复方向**：FFD 后强制 `_try_std_compress` 或拒绝把 ffd 当最终解除非拼法种类 ≤ 阈值。

### 6.3 压缩跳过：L16

```text
V4: joints=38, weld_types=1, cut_types=18, solver=compress
老: joints=152, cut_types=11
```

焊口大优，但切法仍多 — **段集/列枚举预算**问题，不是焊口问题。

---

## 7. 建议的逐步修复顺序（与代码挂钩）

### Step 1 — 统一失败兜底（改 `_arcflow_v3.solve_arcflow`）

- Pass1/Pass2 无解时：尝试 `ffd_weld` → `_better_of(ffd, pure_fallback)`。  
- **验收**：语料 idx 2318/2345 至少产出可行解，焊口有限。  
- **单测**：`all_stock_short` + 短 tl 不再 None。

### Step 2 — 生产 `util_floor`

- `route_v4_arcflow.solve_group`：若调用方无 legacy，用 `max(0.95, group.target_rate)` 并文档化；若有 benchmark 对照，传 legacy util。  
- **验收**：L4 稳定 pure_cut 0 焊口。

### Step 3 — 语料「无解 7 例」逐个 verbose

- 脚本：`_diag_nosol.py <idx>`  
- 分类：超时 vs infeasible vs compress 失败  
- **目标**：无解率 <10%（20 例样本）。

### Step 4 — 焊口劣 4 例（1297/1365/1503/1107）

- 对比老软件 `WeldingPattern` 段长集合 vs V4 `seg_set`  
- 缺段则扩 `_weld_points` / residual grid 门控（勿全局放开拖慢 L16）。

### Step 5 — 拼法/切法（2612/2201/L20）

- 提高 `col_cap` 或 **分段压缩**（先压焊口锚定段集再枚举）  
- L20 单独子问题：**配方级**（非全枚举），或接受 FFD+强压缩时限。

### Step 6 — CG 生产化（可选，在 Step 1–5 后）

- 修正 import；demand 分档；joints-first 整数；与 arc-flow 结果 `_better_of` 合并。

---

## 8. 审查结论（高级算法工程师判定）

| 维度 | 判定 |
|------|------|
| **理论方向** | 耦合 arc-flow + 后验种类压缩 + 残料段增强 **正确**，与老软件「少焊口、少花样」一致 |
| **实现完整度** | **约 55–65%**：主路径可跑、压缩器有效，但兜底不统一、CG 未闭环、大规模未解 |
| **SPEC 符合度** | 词典序在 **主路径+压缩** 基本符合；**种类优化偏后处理** 与最初 S2–S4「主线内 setup」有差距 |
| **可验收性** | picked20 真基准 **部分达标**；真实语料 **不达标（35% 无解）** |
| **最大技术债** | ① 主线无解即 None ② 生产无 util_floor ③ cutcg/主线/cg 三路径可行域不一致 ④ 压缩 col_cap 导致花样债 |

**总评**：当前版本是 **可继续投入的「算法原型进工程」阶段**，不是可宣称「全面超越老软件」的终态。下一位 agent 应 **先修无解链与 API floor**，再攻焊口与花样，**不要先跑 L20 全量配方 CG**。

---

## 9. 关键文件索引

| 文件 | 职责 |
|------|------|
| `scripts/_arcflow_v3.py` | V4 核心：分类、arc-flow、cutcg、compress、FFD、CG 钩子 |
| `scripts/_exp_colgen.py` | 列生成实验 + `solve_colgen_full` |
| `backend/app/route_v4_arcflow.py` | 生产适配（缺 util_floor） |
| `backend/app/route_c_bp.py` | BP + compress |
| `scripts/_corpus_run.py` | 语料批量对比（子进程超时） |
| `scripts/_diag_nosol.py` | 单例无解诊断 |
| `scripts/_cmp_metrics_out.json` | picked20 全指标快照 |
| `scripts/_corpus_run_out.json` | 语料 20 例快照 |
| `docs/v4-current-handoff-20260722.md` | 上一轮交接（偏结果） |
| `docs/v4-arcflow-implementation-plan.md` | S1–S6 计划与附录实测 |

---

## 10. 附录：语料 20 例完整快照

见 `scripts/_corpus_run_out.json`。汇总：`优于 8 | 焊口劣 4 | 拼法劣 1 | 无解 7`。

无解 idx：`2149, 1613, 2318, 1467, 1239, 2345, 2450`（均为 2 管型为主，需 Step 1 兜底）。
