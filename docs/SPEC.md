# 蛇形管排料求解器 — 系统规格说明 (SPEC)

> **本文件的定位**：面向"零上下文接手者"（人或 AI agent）的**权威规格**。读完这一份，就能理解系统在解什么问题、目标函数与约束的精确定义、当前生效的架构、各源文件职责、以及"做到什么算合格"。
>
> **与 `research/GPU可插拔预解器设计方案.md` 的分工**：那份 1800 行文档是**设计决策档案**（记录"为什么这样做、试过哪些失败路径"），是过程日志；**本 SPEC 是"系统长什么样、怎么建"的规格**。若两者冲突，**以本 SPEC + 实际代码为准**。

---

## 一、问题定义

**一维带焊接的下料问题（1D Cutting Stock with Welding，两阶段）**：

给定一批**蛇形管需求**（每种管型有长度、需求根数），和一批**定尺原料（母料）**。需要：

1. **切割（Cut）**：把定尺母料锯成若干**段（segment）**。
2. **焊接（Weld / 拼接）**：把若干段焊接成一根成品管（长度 = 各段之和）。

一根成品管 = 一个**拼法（welding pattern）**，即一个段的有序序列（段之间为焊口）。
一根母料 = 一个**切法（cutting pattern）**，即从该定尺切出的段的多重集。

目标是在满足工艺约束下，用最少的母料满足全部管型需求，**同时让车间生产尽量简单**（见下节目标函数）。

---

## 二、目标函数（词典序，车间口径）— **最高优先级规格**

按以下优先级**逐级最小化 / 最大化**（前者优先，仅在前者相等时才比较后者）：

| 序 | 指标 | 方向 | 含义 |
|----|------|------|------|
| 1 | **总焊口数 (total joints)** | 最小 | Σ 每根成品管的(段数-1)。**车间最看重**——焊口越少工时越省，这是老软件被沿用的根本原因。 |
| 2 | **拼法种类数 (weld_types)** | 最小 | 不同"段序列"的种类数。种类越少，车间越好组织生产。 |
| 3 | **切法种类数 (cut_types)** | 最小 | 不同"母料切割方案"的种类数。 |
| 4 | **段种类数 (seg_types)** | 最小 | 不同段长的种类数（段字母表大小）。 |
| 5 | **利用率 (util)** | 最大 | 需求总长 / 实际耗用母料总长。 |

**关键决策**：利用率**放在最后**。宁可多用几根母料，也不打乱车间生产部署（多出的切法/拼法种类）。这条来自用户明确反馈：*"不能为了省几根材料打乱车间的生产部署"*。

**代码落点**：
- 词典序 → `scripts/_exp_ga.py::fitness_key`
- 内层 ILP 直接最小化总焊口数 → `_exp_ga.py::evaluate` 的 `joints_expr`

---

## 三、硬约束（必须满足，违反即不可行）

| 约束 | 字段来源 | 语义 |
|------|----------|------|
| **段长守恒** | — | 一根成品管的各段之和 = 该管型长度。 |
| **最大焊口数 `max_joints`** | `Max_Weldingjoint_Number`，被工艺规则收紧 | 单管焊口数 ≤ `max_joints`（即段数 ≤ `max_joints + 1`）。工艺规则：每 `Weld_Interval_mm`(默认3000) 最多 1 个焊口，且绝不超过 `Max_Joints_Cap`(默认13)。输入的占位大值(如2000)会被此规则钳制。 |
| **最小焊接间距 `min_weld_distance`** | `Min_Welding_Length`(默认500) | 相邻焊口间距 ≥ 该值（即内部段长 ≥ 该值）。 |
| **最小切割长度 `min_cut_length`** | `Min_Cut_Length`(默认0) | 每段长度 ≥ 该值。 |
| **禁焊区 `forbidden`** | `Unweldable_Area` | 焊口的绝对位置不得落在禁焊区间内。`weld_allowed(pos)` 判定。 |
| **母料库存** | `stock_demand` | 每种定尺用量 ≤ 库存数量。 |
| **必用料 `must_use_quantity`** | `must_use` / `Is_Must` | 标记为必用的母料必须被用满。 |
| **段长上界 `hi`** | 派生 | 单段长度 ≤ min(最长定尺, 最长管型)。段不可能比最长母料还长。 |

**焊口位置合法性**（`_legal_pattern` + `_weld_windows` 共同保证）：焊口是沿管的**绝对位置**，取值范围 `[min_cut, L - min_cut]`，挖掉禁焊区，且相邻间距满足 `min_weld_distance`。
> ⚠️ **易错点**：焊口位置上界是 `L - min_cut`，**不能用 `hi`（最长定尺）截断**——长管(L>hi)靠后的焊口位置本就 > hi。`hi` 只约束**单段长度**，不约束**焊口绝对位置**。历史上这里的 bug 曾导致长管样本全部秒 FAIL（详见设计文档 §11.38 十）。

---

## 四、输入 / 输出契约

### 4.1 输入 schema（MOM 导出，`parse_problem` 归一化）

顶层信封支持多种：`{"input":{"data":[...]}}`、`{"OriginalProblem":{...}}`、`{"data":[...]}`、或单组 `{"Pipe":[...],"Stock":[...]}`。

**每个物料组（material group）**含：
- `material`, `specifications`：材质/规格（分组键）。
- `Pipe[]`：每根含 `pipe_length`(管长)、`pipe_demand`(需求根数)、`Max_Weldingjoint_Number`(最大焊口)、`Unweldable_Area`(禁焊区，支持 `[[0,100],...]` 或字符串 `"[0,100],[1153,1513]"` 等多种编码)、`figure_number`/`Parent_node`/`jlxh`/`cube_no`(管型身份，四元组唯一)。
- `Stock[]`：每种含 `stock_length`(定尺长)、`stock_demand`(库存数，特殊值 `"S"`→5)、`must_use`/`Is_Must`(是否必用)。
- 组级参数：`Target_Util_Rate`(目标利用率，默认99.25)、`Min_Welding_Length`、`Min_Cut_Length`、`Weld_Interval_mm`、`Max_Joints_Cap`、`BladeMargin`(锯缝，默认0)、`KerfMode`(`BETWEEN_PARTS`/`WITH_REMAINDER`)。

**长度归一化（关键不变式）**：所有长度在 `to_units` 里用 **Decimal 精确取整到整数毫米**（标量/禁焊区末端向上取整，禁焊区起点向下取整），全程整数毫米运算，绝不引入二进制浮点。归一化留审计轨迹(`InputNormalization`)。

### 4.2 数据模型（`backend/app/domain.py`，全部 frozen dataclass）

- `Interval(start, end)` — 禁焊区间，`.contains(pos)`。
- `PipeDemand` — 管型：`length, demand, max_joints, forbidden, pipe_id(=Parent_node|jlxh|cube_no)` 等；`.weld_allowed(pos)` 判焊口合法。
- `StockSupply(length, quantity, must_use_quantity)` — 定尺供应。
- `MaterialGroup` — 物料组：`pipes, stocks, min_weld_distance, min_cut_length, blade_margin, kerf_mode` 等；派生属性 `demand_length`(需求总长)、`stock_length`(库存总长)。
- `NestingProblem(task_id, groups, source)`。

### 4.3 输出

求解结果按段/切法/拼法给出方案，并伴随指标：`joints, weld_types, cut_types, seg_types, util, used_len`。所有长度输出为整数毫米（`from_units`）。

---

## 五、当前生效架构：GA 外层 + ILP 内层

**核心思想**：不逆向老软件的段长，而是**用遗传算法自动搜索"段长集合"**，内层 ILP 在给定段集下求最优切/拼方案。段长由全局优化涌现，不做单管算术派生。

```
┌─────────────────────────────────────────────────────────┐
│ GA 外层 (ga_run)                                          │
│  个体 = 每管型一个合法拆分(段元组)；段集 = 所有拆分的并    │
│  ┌───────────────────────────────────────────────────┐   │
│  │ 内层评估 evaluate(group, segs, ...)                │   │
│  │   1. enum_pipe_pats: 枚举每管型用 segs 的合法拼法   │   │
│  │   2. enum_cuts: 枚举母料切法 (来自 _exp_ksplit)     │   │
│  │   3. ILP(pyscipopt): min 总焊口数, s.t. 需求/库存/  │   │
│  │      段供需平衡/用料≤target*(1+slack)                │   │
│  │   → 返回 {joints, weld_types, cut_types,           │   │
│  │           seg_types, util}                          │   │
│  └───────────────────────────────────────────────────┘   │
│  适应度 = fitness_key(词典序: 焊口→拼→切→段→利用率)       │
│  选择(精英) → 交叉(段池复用促少段) → 变异(重切/复用)      │
│  自适应 slack: 整代不可行则放宽启动, 出解后收紧            │
│  提前停: 焊口数≤老软件 且 利用率不劣化                     │
└─────────────────────────────────────────────────────────┘
```

**关键防爆炸机制**（必须保留，否则大样本卡死）：
- `_find_legal_order`：给定段多重集，**回溯搜一个合法排列**（带 `budget` 节点上限），替代 `itertools.permutations` 的 k! 爆炸。
- `enum_pipe_pats` 的 `node_budget`(默认200000)：DFS 硬上限。
- `seed_splits`：把个体自带的拆分作为**必含种子**，保证即使 DFS 预算截断也不漏可行拼法（避免误判不可行）。

---

## 六、源文件职责表

### 生效路径（当前系统）
| 文件 | 职责 |
|------|------|
| `backend/app/domain.py` | 输入解析、长度归一化、数据模型。**契约层，最先读**。 |
| `backend/app/solver.py` | `_legal_pattern`(焊口合法性判定) 等求解基础。 |
| `scripts/_exp_ga.py` | **当前主力求解器**：GA外层+ILP内层，焊口优先目标。 |
| `scripts/_exp_colgen.py` | `merge_equivalent_pipes`(等价管型合并)、`legacy_alpha_and_metrics`(读老软件方案指标)。 |
| `scripts/_exp_ksplit.py` | `enum_cuts`(母料切法枚举)。 |
| `scripts/_batch_worker.py` | 批量评估 worker：单样本跑 GA、算老软件指标、判定 WIN/TIE/LOSE。 |
| `scripts/_run_hard.py` | 无时限跑指定难样本。 |
| `backend/app/verifier.py` | 方案合法性校验。 |

### 已废弃 / 实验路径（勿当作生效逻辑）
- **网格枚举段长** — 已被数据推翻（设计文档 §11.38 八）。
- **route3 sequential DP / 单管 DP** — 局部最优，放弃（§11.36）。
- **纯精确 MILP / 列生成直接求解** — 不收敛（§11.36）。
- 以上仅作历史参考，**不要基于它们扩展**。

---

## 七、验收标准 — **"不比老软件劣化"即合格**

用户明确口径：*"验收标准只要不比老软件劣化就可以验收"*。逐指标按第二节词典序比较：

1. **总焊口数** ≤ 老软件 —— **首要且必须**（这是老软件被沿用的原因）。
2. 拼法种类 ≤ 老软件。
3. 切法种类 ≤ 老软件。
4. 段种类 ≤ 老软件。
5. 利用率 ≥ 老软件 - 1e-3（不明显劣化即可）。

**判定实现**：`scripts/_batch_worker.py` 的 verdict 逻辑（WIN/TIE/LOSE_JOINTS/LOSE_UTIL/MIXED），焊口数优先，利用率作下限。

> **当前已知差距**：小样本冒烟测试中 GA 初代 `joints=208` 而老软件 `joints=78`，需 GA 多代进化压降。**压总焊口数是当前主攻方向**。

---

## 八、如何重建/验证（给接手者）

```bash
# 环境：Python + pyscipopt(SCIP)
cd serpentine-pipe-nesting

# 单样本跑 GA（<id前缀> 取样本 id 前缀）
python scripts/_exp_ga.py <samples.json> <id前缀> --pop 30 --gen 25 --tl 20 --seed 0

# 无时限跑难样本
python scripts/_run_hard.py

# 单元测试
python -m pytest backend/tests/
```

**新 agent 的推荐阅读顺序**：本 SPEC → `domain.py`(契约) → `_exp_ga.py`(求解器主体) → 需要历史"为什么"时再查 `research/GPU可插拔预解器设计方案.md`。

---

## 九、不变式清单（改代码时勿破坏）

1. 全程**整数毫米**，绝不引入二进制浮点长度。
2. 目标函数词典序**焊口数第一、利用率最后**。
3. 段长 ≤ min(最长定尺, 最长管型)；焊口绝对位置上界是 `L - min_cut`（**不用 hi 截断**）。
4. `enum_pipe_pats` 必须带 `node_budget` + `seed_splits`，否则大样本卡死。
5. 禁焊区、min_weld_distance、min_cut、max_joints 全部是硬约束。
6. 文件读写一律用 **Python / 编辑器工具**，**不用 PowerShell 的 `-replace`/`Set-Content` 改代码**（曾导致 UTF-8 源文件损坏）。
