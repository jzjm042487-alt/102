# 全局切焊联合排料引擎 — 模型规格（V1）

> 状态：待确认后进入实现  
> 依据：`docs/research/cutting-welding-best-practices.md`  
> 目标：支撑可直接编码的 MVP，而不是再次打补丁

## 1. 目标与边界

### 1.1 本规格解决什么

为蛇形管“切割 + 焊接”建立统一全局模型，输出可生产方案，并满足：

1. 全排完（每根管型需求被满足）
2. 利用率 ≥ 95%（业务硬底线；物理不可行除外）
3. 遵守禁焊区、最大焊口、最小焊距、最小段长、库存、must_use
4. 在底线之上压缩切法种类、拼法种类、总焊口数、段长种类
5. 所有输出必须通过独立 `verify_solution`

### 1.2 本规格不解决什么（V1 不做）

- 不重写前端 UI
- 不做完整 branch-and-price 最优证明
- 不删除 `baseline` / `route3`（保留对照）
- 不针对单条失败样本加特例规则

### 1.3 MVP 交付物

| 交付物 | 路径建议 |
| --- | --- |
| 求解引擎 | `backend/app/solver_global.py` |
| 候选生成 | `backend/app/global_candidates.py` |
| API 接入 | `engine=global` |
| 回归脚本 | `scripts/audit_global.py` |
| 单测 | `backend/tests/test_solver_global_*.py` |

## 2. 输入输出契约

### 2.1 输入

复用现有 `parse_problem` / `MaterialGroup`：

- 按材质规格分组独立求解
- 全程整数毫米
- 硬约束字段：`max_joints`、`forbidden`、`min_weld_distance`、`min_cut_length`、`blade_margin`、`kerf_mode`、`stocks.quantity`、`must_use_quantity`

### 2.2 输出

必须能被现有 `verify_solution` 验收，至少包含：

- 每个物料组的切法列表（母料长度 + 段多重集 + 使用次数）
- 每个物料组的拼法列表（管型 + 有序段序列 + 使用次数）
- 指标：`used_len`、`util`、`joints`、`cut_types`、`weld_types`、`seg_types`
- 状态：`FEASIBLE` / `TARGET_REACHED` / `INFEASIBLE` / `TIMELIMIT_*`

## 3. 数学模型

### 3.1 集合

- `I`：管型集合
- `W_i`：管型 `i` 的合法拼法候选集合
- `P`：切法候选集合
- `S`：段长字母表（由拼法候选导出）
- `L`：母料定尺长度集合

### 3.2 决策变量

- `u[i,w] ∈ Z≥0`：管型 `i` 使用拼法 `w` 的根数
- `x[p] ∈ Z≥0`：切法 `p` 使用的母料根数
- （阶段 B/C 才启用）
  - `y[p] ∈ {0,1}`：切法 `p` 是否启用
  - `z[t] ∈ {0,1}`：拼法类型 `t` 是否启用

### 3.3 硬约束

1. **需求覆盖**  
   `Σ_w u[i,w] = demand_i`  ∀i

2. **库存上限**  
   `Σ_{p: stock(p)=L} x[p] ≤ quantity[L]`  ∀L

3. **段长平衡**  
   `Σ_p prod[p,s] · x[p] ≥ Σ_{i,w} cons[i,w,s] · u[i,w]`  ∀s ∈ S

4. **候选合法性前置**  
   进入模型的每个拼法必须已通过：
   - 段长之和 = 管长
   - 焊口数 ≤ max_joints
   - 焊口位置不在禁焊区
   - 相邻焊口间距 ≥ min_weld_distance
   - 每段 ≥ min_cut_length
   - 每段 ≤ max_stock_length

5. **切法物理预算**  
   `sum(parts) + kerf_loss(parts) ≤ stock_length`

6. **利用率底线（可行方案）**  
   `demand_length / used_length ≥ 0.95`  
   其中 `used_length = Σ_p stock(p) · x[p]`  
   若库存总长本身不足以达到 95%，标记为物理/库存不可行，不进入花样压缩。

7. **must_use**  
   V1：按材质规格组“必用料优先占用”表达为  
   `Σ_{p: stock(p)=L*} x[p] ≥ must_use_quantity[L*]`（在库存允许且需求足够时）  
   若需求不足，只使用满足需求所需的必用料，不强行用超。

## 4. 分阶段目标（词典序工程化）

### Phase A — 可行性 + 用料

目标：最小化 `used_length`  
约束：第 3.3 节硬约束 + util ≥ 95%

输出：可行方案 `A*`，记录 `used_len_A`、`cut_types_A`、`weld_types_A`、`joints_A`

### Phase B — 切法压缩

约束：`used_length ≤ used_len_A * (1 + slack)`  
默认 `slack = 0.002`（0.2%）  
目标：最小化 `Σ y[p]`（切法种类）

### Phase C — 拼法 / 焊口压缩

约束：保持 Phase B 的 `used_length` 上限与 `cut_types ≤ cut_types_B`  
目标：最小化 `α · weld_types + joints`  
默认 `α` 取足够大，使少一种拼法优先于减少少量焊口；V1 取 `α = max(1, total_demand)`。

### Phase D — 段长字母表压缩（可选）

仅当 A/B/C 已达标时尝试；V1 可后置。

### 验收对标（有 legacy 时）

不作为求解器内部硬目标，作为外部回归判定：

- util ≥ max(0.95, legacy_util - 1e-3) 或至少 ≥ 0.95 且切法明显更少
- joints / cut_types / weld_types 尽量不劣于 legacy

## 5. 候选生成规则（禁止样本特例）

### 5.1 拼法候选 `build_weld_pool(group)`

对每个管型生成有界候选，统一规则：

1. 若 `pipe.length ≤ max_stock`：加入整管拼法 `(L,)`
2. 单焊口：优先 stock-aligned 位置  
   `k ∈ {stock_len, L-stock_len}` 及合法窗内均匀采样，上限 `WELD_SPLIT_CAP=12`
3. 双焊口：仅当 `max_joints ≥ 2`，由单焊口优质位置组合，上限 `WELD_TWO_CAP=20`
4. 三焊口及以上：V1 仅在 `max_joints` 允许且前两层候选无法使单段 ≤ max_stock 时启用，上限 `WELD_K_CAP=30`
5. 全部候选必须过 `_legal_pattern`

禁止：按某个 sample_id 手工塞拼法。

### 5.2 切法候选 `build_cut_pool(group, alphabet)`

1. 仅使用拼法池导出的段长字母表
2. 优先高密度列：`remnant ≤ MAX_TRIM`（默认 50mm，可配置）
3. 每根母料段数上限 `MAX_PIECES=6`
4. 每定尺列数上限 `CUT_PER_STOCK_CAP=400`
5. 总列数上限 `CUT_TOTAL_CAP=6000`
6. **覆盖锚点**：字母表中每个段长至少有一个可生产列（必要时放宽 trim）

禁止：按失败样本临时加神秘段长。

### 5.3 列补充（V1.1，MVP 后）

当 Phase A 不可行且诊断为 `CANDIDATE_POOL_INSUFFICIENT`：

- 根据未覆盖段长 / 对偶价格生成新列
- 统一 gap-driven，不手写样本补丁

V1 MVP 可先做静态池 + 覆盖锚点；列生成接口预留。

## 6. 不可行诊断分类

求解失败必须归因到以下之一：

| 代码 | 含义 |
| --- | --- |
| `A_LENGTH_SHORTFALL` | 库存总长 < 需求总长 |
| `A_PROCESS_INFEASIBLE` | 某管型无任何合法拼法 |
| `A_STOCK_STRUCTURE` | 总长够，但定尺结构无法支撑 max_joints 等 |
| `C_CANDIDATE_INSUFFICIENT` | 候选池不足导致模型不可行 |
| `C_INTEGER_FAIL` | LP/松弛可行但整数失败 |
| `C_TIMEOUT` | 时限内无可行解 |
| `E_VERIFICATION_FAILED` | 求解器输出未过 verifier |

禁止把上述失败一律当成“再加一条规则”。

## 7. 求解流程

```text
parse_problem
  → for each MaterialGroup:
      precheck
      weld_pool = build_weld_pool
      alphabet = segments(weld_pool)
      cut_pool = build_cut_pool(alphabet)
      A = solve_phase_A
      if infeasible → diagnose → return
      B = solve_phase_B(A)
      C = solve_phase_C(B)
      pack production schema
  → verify_solution
```

时间预算建议：

- **时限是输入条件**，由 API / CLI / 调用方传入 `time_limit_seconds`，求解器不得写死总时限。
- 组内阶段分配：Phase A 50%，B 30%，C 20%（按传入时限切分）。

## 8. 与现有代码关系

### 复用

- `backend/app/domain.py`
- `backend/app/verifier.py`
- `backend/app/solver.py` 中的 `_legal_pattern` 等合法性工具
- API / Job 外壳

### 新建

- `solver_global.py`：主流程与分阶段 ILP
- `global_candidates.py`：拼法/切法池
- `scripts/audit_global.py`：样本回归

### 降级为对照

- `baseline`、`route3`、GA 实验脚本：不删，不扩展为主线

### 外部参考（`_refs/`）

- `vpsolver`：切割侧弧流思想
- `informs-csp-2023`：CSP + Skiving + 列生成/整数启发式
- `columngenerationsolver(py)`：RMP + pricing 组织

## 9. 验收标准（MVP）

1. 小型构造样例：全排完、util ≥ 95%、verifier 通过
2. 至少 10 条真实样本：
   - verifier 通过率 100%（对声明可行的解）
   - 可行样本 util ≥ 95%
   - 不得出现“解出但校验失败”
3. 与 legacy 对比报告输出到 `data/audit/`
4. API 支持 `engine=global`，默认仍可为 baseline（开关接入，不强制切换生产默认）

## 10. 实现顺序

1. 候选生成 + 单测（合法性、覆盖锚点）
2. Phase A ILP + 输出 schema + verifier
3. Phase B/C
4. `engine=global` API
5. `audit_global` 回归
6. （后续）列生成补充

## 11. 待你确认的关键默认值

若无异议，实现将采用：

- util 底线：`0.95`
- Phase B slack：`0.002`
- `WELD_SPLIT_CAP=12`，`CUT_TOTAL_CAP=6000`，`MAX_TRIM=50`
- **时限：输入条件**（API `time_limit_seconds` / CLI `--time-limit`，不在引擎内写死总预算）
- 第一版引擎名：`global`
