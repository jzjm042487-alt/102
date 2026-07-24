# V4 生产终极方案 — 详细实现步骤（历史实施稿）

> **版本**：2026-07-23（评审修订 v2）  
> **状态更新（2026-07-24）**：本文的多路径编排、util floor、焊口优先和 `seg_types` 独立阶段均被现行 `docs/SPEC.md` 及项目 Skill `production-grade-nesting-solver` 覆盖。保留作历史实施证据，禁止作为新功能施工说明。  
> **外部对标**：工号 `113146` / 部件 `97D215MX`（对方 4–9 秒；我方现状 35s～挂死）  
> **修订来源**：2026-07-23 严格评审（禁焊区 / 段平衡 / Σ cap / 动态预算 / 兜底单点 / 临时交付）

---

## 0. 为什么必须按本方案做

| 事实 | 含义 |
|------|------|
| 调研已证：小段长字母表 + 覆盖 ILP → **秒级** | 秒解在机制上可行 |
| 113146 两材质对方 4s / 9s | 生产级墙钟标杆 |
| 我方 v4 arc-flow 同题挂死 | **主路径错误**，不是 SCIP 慢一点 |
| 多引擎并行试了七八天 | 资产够用；缺的是**唯一主路径落地** |
| 113146 有密集 `Unweldable_Area` | Recipe **必须**做禁焊可达剪枝，否则 Lex 秒级变秒崩 |

**一句话**：缺的是 **Recipe 小 Σ（事前，且对禁焊可达）**；有了它，LexMaster 有限列 ILP 才能秒级；arc-flow/CG/FFD 只做 Fallback。

---

## 0.1 开工前必做的 3 个验证（半天，阻塞 Phase2）

> **目的**：避免 W3 第一次跑 113146 才发现「Σ 合法但拼列为空」。

| # | 验证 | 做法 | 通过标准 |
|---|------|------|----------|
| V0 | **113146 \|Σ\| 实测** | `recipe_builder` 只实现 T0+残料+标准段，打印两题 `\|Σ\|`、截断前后 | 默认 cap 下仍能覆盖全部超长管的至少 1 条合法拼路径 |
| V1 | **禁焊下拼列非空** | 对每根管跑 `_enum_pipe_patterns(pipe, sigma, …)`，统计合法列数 | 每管 `len(pats)≥1`；若有 0 → 记录缺段 diagnosis，回灌残料/邻段后再测 |
| V2 | **段平衡约定试跑** | 用 Pass A 小模型 `produced ≥ consumed` 出解 → `_reconcile_to_counts` → verifier | verifier 通过；确认 surplus 由 reconcile 回收，**不**在模型里假用 `==` 却漏余料 |

三件在 Phase0 末 / Phase1 初完成；**未通过不得宣称 Phase2 机制成立**。

---

## 1. 目标架构（实现视角）

```text
engine=v4
    └─ backend/app/route_v4_arcflow.solve_group
           └─ scripts/solve_v4_production.solve(...)   ← L0 唯一编排
                  │
                  ├─ [临时] engine 对照 rcg（W1–W2 业务可交付，见 §7.1）
                  ├─ classify + solve_pure_cut          ← 快路（0焊口）
                  ├─ recipe_builder.build(...)          ← L1：Σ_weld + Σ_cut + seed
                  ├─ lex_master.solve(...)              ← L2：词典序有限列 ILP
                  ├─ v4_fallback.ladder(...)            ← 3a→3d（仅 L0 一层）
                  └─ assemble + verifier 闸门
```

### 1.1 新建 / 改造文件清单

| 动作 | 路径 | 职责 |
|------|------|------|
| **新建** | `scripts/recipe_builder.py` | Σ_weld / Σ_cut + seed 列 + 禁焊可达剪枝 |
| **新建** | `scripts/lex_master.py` | 有限列词典序 ILP（Pass A→D） |
| **新建** | `scripts/v4_fallback.py` | Fallback 梯 3a–3d（**唯一**兜底实现点） |
| **新建** | `scripts/solve_v4_production.py` | L0 编排 + **动态预算** + diagnosis |
| **新建** | `scripts/acceptance_113146.py` | 两道现场题墙钟/可行性门禁 |
| **新建** | `scripts/probe_recipe_113146.py` | V0/V1/V2 开工验证脚本 |
| **改造** | `backend/app/route_v4_arcflow.py` | 只调 L0；传 `util_floor` |
| **改造** | `scripts/_arcflow_v3.py` | Pass 失败 **return None 或返回未压缩解由 L0 处理**（见 §2.5）；支持 restricted Σ；禁止裸 ffd 对外交付 |
| **只读复用** | `_enum_pipe_patterns` / `_enum_cut_patterns` / `_compress_types` / `_residual_seg_lengths` / `_try_std_compress` / `_ffd_weld_incumbent` / `solve_pure_cut` | 从 `_arcflow_v3` import，不复制粘贴 |
| **只读复用** | `backend/app/solver.py` → `_standard_segment_lengths`、`_assemble_group_result` | Tier 思想进 Recipe；组装走现有契约 |
| **冻结** | `route3` / `rcg` / `global` / GPU/GA | 对照引擎；**不进** v4 主链 PR（临时交付除外） |

### 1.2 统一解字典（各层约定）

```python
{
  "joints": int,
  "weld_types": int,
  "cut_types": int,
  "seg_types": int,       # SPEC 第 4 档，必填
  "util": float,
  "used_len": int,
  "weld_patterns": dict,  # (pipe_index, seq) -> qty
  "cut_patterns": dict,   # (stock_len, sorted_segs) -> qty
  "solver": str,          # e.g. "lex_master" | "pure_cut" | "fallback_3c"
  "solver_path": list[str],
  "diagnosis": list[str],
  "elapsed_s": float,
}
```

词典序比较（与 SPEC 一致）：

```text
util < util_floor  → 直接丢弃（废解）
否则：joints → weld_types → cut_types → seg_types → used_len（util 末档被动）
```

实现函数：`_lex_better(a, b, util_floor)`（新建于 L0 或抽到 `scripts/v4_lex.py`）。  
**注意**：现有 `_better_of` 只比 joints/util，**不够**；新比较器必须含拼/切/段。

### 1.3 段平衡约定（强制写死，防 verifier 踩坑）

| 层 | 约束 | 说明 |
|----|------|------|
| LexMaster / compress ILP | `produced[ℓ] ≥ consumed[ℓ]`（每段 ℓ） | 允许切层略过产 |
| 导出后 | **必须**走 `_reconcile_to_counts` | surplus 段回收/标注，满足 verifier `SEGMENT_BALANCE` |
| 禁止 | 在模型里假写 `==` 却不做 surplus | 易导致不可行或假可行 |
| 可选 v1.1 | `produced == consumed + slack`，slack≥0 计入废料 | 与 ≥ + reconcile 等价，二选一，**全仓统一** |

**验收**：任意 Lex 解未经 reconcile **不得**进 API 响应。

---

## 2. Phase 0 — 止血（3–5 个工作日）

**目标**：不改世界观；无解率↓、口径正确、113146 至少有可对照快照 + V0/V1 探针。

### 步骤 P0-1：`util_floor` 进生产包装

**文件**：`backend/app/route_v4_arcflow.py`

1. `solve_group(group, time_limit, util_floor=None)` 增加参数。  
2. 若 `util_floor is None`：`util_floor = max(0.95, group.target_rate)`（`target_rate` 已是 0–1）。  
3. 调用改为 L0（Phase1 前可暂调 `solve_arcflow(..., util_floor=...)`）。  
4. 在 `solver.py` 的 `engine=="v4"` 分支：若调用方可传 floor 则传；否则用上式。

**验收**：`_cmp_metrics.py` 与 API 同口径；L4 类不再因 floor 过严误选焊接解。

### 步骤 P0-2：arc-flow 失败行为（与 L0 兜底**单点**约定）

**文件**：`scripts/_arcflow_v3.py` → `solve_arcflow`

**原则（评审修订）**：兜底只允许在**一处**实现，禁止内外双套。

| 阶段 | `solve_arcflow` 行为 | 谁负责 3c/3d |
|------|----------------------|--------------|
| **Phase0（L0 尚未上线）** | Pass1/2 失败 → 内部走 FFD→compress→pure_cut→`_better_of`；**禁止裸 ffd 交付** | arc-flow 内部 |
| **Phase1+（L0 已上线）** | Pass1/2 失败 → **return None**（或返回 `{"solver":"arcflow_failed", ...}` 标记）；**不再**内部跑完整 ladder | **仅** `v4_fallback.ladder` |

Phase0 合入时在 `solve_arcflow` 加开关：

```python
def solve_arcflow(..., internal_fallback: bool = True):
    ...
```

L0 调用时固定传 `internal_fallback=False`。

**验收**：

```bash
.venv\Scripts\python.exe scripts\_diag_nosol.py 2318 90
# Phase0: 必须有解
```

### 步骤 P0-3：硬超时回归脚本

**文件**：已有 `scripts/_corpus_run.py` / `_algorithm_sweep_worker.py`

1. CI/本地门禁统一：`subprocess` + `timeout`，禁止「tl=60 实际跑 600s」。  
2. 输出固定到 `data/` 下 jsonl（已 ignore）。

**验收**：语料 20 例（seed=0）无解率统计可复现。

### 步骤 P0-4：113146 基线快照（只记录、不改算法）

**新建**：`scripts/acceptance_113146.py`

- 输入：两道微信题 JSON（拷到 `data/acceptance/113146/`）。  
- 引擎：`baseline,rcg,v4`（各子进程硬超时 90s）。  
- 落盘：`data/acceptance/113146/baseline_snapshot.json`。  
- 写入 `target_wall_clock_s: {t91: 4, cr: 9}`。

### 步骤 P0-5：开工探针（新增）

**新建**：`scripts/probe_recipe_113146.py`

1. 实现最小 Recipe：T0 + `_residual_seg_lengths` + 标准段（S/2、S/3）。  
2. 打印两题：`|Σ|`、每管合法拼列数、触 DFS budget 的管。  
3. 若某管列数=0：输出「建议补段」候选（残料邻域 ±kerf 一轮）。

**验收**：两题每管至少 1 条合法拼路径；`|Σ|` 记入 `data/acceptance/113146/sigma_probe.json`（作为 cap 调参依据）。

### Phase 0 完成门禁

- [ ] idx 2318 有解  
- [ ] 语料 20 无解 = 0（或明确列出不可物理行）  
- [ ] `route_v4` 传 util_floor  
- [ ] 113146 快照已存  
- [ ] V0/V1 探针通过（P0-5）

---

## 3. Phase 1 — 编排壳 L0（5–8 个工作日）

**目标**：`engine=v4` 只进 L0；内部暂时仍可调旧 `solve_arcflow`，但带 diagnosis / **动态预算**。

### 步骤 P1-1：实现 `solve_v4_production.py`

```python
def solve(group, *, tl: float, util_floor: float | None = None, verbose: bool = False) -> dict | None:
    """
    动态预算（评审修订，取代固定 10/55/25/10）：
      - pure_cut: 上限 min(0.10*tl, 5s)，成功可直接返回
      - recipe+lex: 上限 min(0.70*tl, 12s) 出 Pass A；有解则剩余给 B/C
      - 若 lex 在上限内无可行解：剩余预算 **全部** 给 ladder
      - assemble/verify: 预留 1s 硬缓冲
    """
```

实现清单：

1. `util_floor = max(0.95, util_floor or group.target_rate)`。  
2. `t0 = monotonic()`；每阶段检查剩余预算。  
3. `classify_group`；若可 pure_cut：跑 `solve_pure_cut`，成功则 `solver_path=["pure_cut"]`。  
4. **Phase1 过渡**：主求解 `solve_arcflow(..., util_floor=..., internal_fallback=False)`，写入 `solver_path=["arcflow_legacy_main"]`。  
5. `None` 时走 `v4_fallback.ladder`（Phase1：仅 3c+3d）。  
6. 返回统一解字典；失败返回 `None`（route 可回退 baseline，或 L0 末可选 baseline）。

### 步骤 P1-2：改 `route_v4_arcflow.solve_group`

1. `from solve_v4_production import solve as solve_v4_production`。  
2. `res = solve_v4_production(group, tl=time_limit, util_floor=...)`。  
3. 现有 `_reconcile_to_counts` + `_assemble_group_result` **不变**。  
4. 把 `res["solver_path"]` / `diagnosis` 写入 result 的 `warnings` 或 metrics 扩展字段。

### 步骤 P1-3：最小 Fallback（唯一兜底点）

**文件**：`scripts/v4_fallback.py`

```python
def ladder(group, *, util_floor, budget_s, incumbent=None, recipe=None) -> dict | None:
    # Phase1: 仅 3c + 3d
    # Phase2: 补 3a + 3b（见 §4.4）
```

### 步骤 P1-4：临时业务交付（新增，不进主链永久化）

在 `solver.py` / 配置层增加 **可选** 开关（默认关）：

```text
V4_TEMP_ENGINE=rcg   # W1–W2：113146 类现场题可先用 rcg 出合法解（~30–40s）
```

- 文档标明：**临时**，Phase2 G1/G2 达标后删除或改为仅对照。  
- 目的：业务不等到 W4 才有解。

### Phase 1 完成门禁

- [ ] `engine=v4` 代码路径 **唯一** 经 L0（grep 确认无第二处直接 `solve_arcflow`）  
- [ ] API 结果含 `solver_path`  
- [ ] picked20 真基准 12 档：**无回归**  
- [ ] L0 调用 `internal_fallback=False`；ladder 为唯一兜底  
- [ ] 临时 rcg 开关文档化（可选启用）

---

## 4. Phase 2 — RecipeBuilder + LexMaster（核心，15–25 个工作日）

**目标**：默认路径改为 Recipe → LexMaster；arc-flow 退出主路径。  
**墙钟验收样例**：113146 两材质（目标 ≤15s 出合法解；冲刺 ≤对方×3）。

### 4.1 RecipeBuilder — 逐步实现

**文件**：`scripts/recipe_builder.py`

```python
@dataclass
class Recipe:
    sigma_weld: list[int]            # 拼层可达段（禁焊剪枝后）
    sigma_cut: list[int]             # 切层段（可 ⊇ sigma_weld）
    sigma: list[int]                 # 兼容别名 = sorted(set(sigma_weld)|set(sigma_cut))
    seed_weld: list[tuple]
    seed_cut: list[tuple]
    meta: dict                       # |Σ|、截断、每管可达列数、cap 使用情况

def build(group, *, util_floor: float, sigma_cap: int | None = None) -> Recipe:
    ...
```

**`sigma_cap` 默认策略（评审修订）**：

```text
默认 = 48
若 P0-5 探针显示 113146 需要更大：取 max(48, ceil(1.2 * probe_|Σ|))，写入常量 SIGMA_CAP_DEFAULT
cap 是可调参数，不是教条；CI 以「每管可达」优先于「|Σ|≤48」
```

#### 步骤 P2-R1：T0 整管长

1. 对每根 `pipe.length`，若 `≤ max_stock` → 加入候选 Σ。  
2. 单测：纯可切组 Σ 至少包含全部管长。

#### 步骤 P2-R2：残料族

1. 复用 `_residual_seg_lengths(pipes, stock_qty, max_stock, min_cut, kerf)`。  
2. 合并进候选，去重，过滤 `≤0` 与 `> max_stock`。  
3. **kerf 感知**：母料余料项必须与 `_residual_seg_lengths` 一致（含 `-kerf`），禁止手写 `S-k*Lp` 漏 kerf。

#### 步骤 P2-R3：组标准段（收编 baseline Tier）

1. 从 `solver._standard_segment_lengths(group)` 取候选，**优先 import**，避免分叉。  
2. 额外强制：每 stock 的 `S/2`、`S/3`（kerf 感知与 baseline 一致）。  
3. 每管至多 1 个尾段 `L % S_std`（`S_std` 取最常用标准段）。

#### 步骤 P2-R4：满装对闭包（受控 + 可达）

1. 取 demand 加权 Top-K 管长（K≤8）。  
2. 对主母料 S（库存根数最多的长度），找 `(a,b)` 满足：  
   - `a + b + kerf ≤ S`（**不是** `≈`；余量可进废料/短段）  
   - `a,b` 已在候选或为残料诱导  
3. **可达门槛（新增）**：`a` 或 `b` 至少能作为某管型的合法分割段（对该管 `weld_allowed` 在对应位置成立，或经 `_enum_pipe_patterns` 冒烟存在含 a/b 的路径）。  
4. 每母料最多加 12 对；**禁止**笛卡尔全爆。

#### 步骤 P2-R5：禁焊可达剪枝（**新增，关键**）

对候选 Σ：

1. 令 `sigma_cut = 候选全集`（切层可保留「只用于填母料、不进拼路径」的短段）。  
2. 对每管 `pipe`：在候选上跑 `_enum_pipe_patterns`（budget 见 P2-L1）。  
3. 若某管 `pats==[]`：  
   - `diagnosis` 记录 `pipe_unreachable`；  
   - 对 `(Lp, S)` 补一轮邻域残料：`Lp - k*S ± {0, kerf}` 与 `S - k*Lp - kerf` 中未收录者；  
   - 再测一次；仍不可达 → 标记 `meta["hard_unreachable"]`，交 L0 直接进 Fallback（不要空跑 Lex）。  
4. `sigma_weld` = 在**至少一根管**的合法拼路径中出现过的段长 ∪ 全部 T0 整管长。  
5. 输出前再按优先级截断到 `sigma_cap`（先保可达所需段，再丢满装对/标准段）。

#### 步骤 P2-R6：截断策略

优先级从高到低：

1. 使每管可达所必需的段（R5 锁定，**不可丢**）  
2. T0 整管  
3. 残料  
4. 标准段  
5. 满装对  

超 cap：只丢 4→5；若仍超 → 提高 cap 并写入 `meta["sigma_cap_raised"]=...`。

#### 步骤 P2-R7：单元测试

**新建**：`scripts/tests/test_recipe_builder.py`（或 `backend/tests/`）

- 113146 两题：每管可达；`|sigma_weld|` 有界；含残料（`all_stock_short`）。  
- 随机 D1：Σ 非空。  
- 满装对：构造无 kerf 的假对，断言不被收录。

### 4.2 LexMaster — 逐步实现

**文件**：`scripts/lex_master.py`

```python
def solve(group, recipe: Recipe, *, util_floor: float, tl: float,
          col_cap: int = 20000) -> dict | None:
    ...
```

#### 步骤 P2-L1：列枚举（有预算 + 不完整标记）

1. `seg_set = set(recipe.sigma_weld)`（拼）；切层用 `recipe.sigma_cut`。  
2. 对每管：`_enum_pipe_patterns(pipe, seg_set, ..., budget=PIPE_PAT_BUDGET)`。  
3. 若 `cnt` 触 budget：  
   - `diagnosis.append(f"pipe_{i}_enum_truncated")`；  
   - **仍返回已枚举列**；  
   - 若该管列为空 → 整题 return None，由 L0 进 Fallback（可对截断管单独 3a 补段）。  
4. 对每 stock：`_enum_cut_patterns(L, sorted(sigma_cut), kerf, budget=...)`。  
5. 合并 `recipe.seed_*`。  
6. `total_cols > col_cap` → 停止扩列，`diagnosis` 记录，L0 决定 Fallback（**禁止死循环重枚举**）。

#### 步骤 P2-L2：可行性 Pass（util floor）

建 SCIP 模型（参考 `_compress_types` / `_exp_colgen.solve_integer`）：

- 决策：拼列用量、切列用量。  
- 约束：管需求覆盖、**段 `produced ≥ consumed`**、库存根数、`used_len ≤ demand_length / util_floor`。  
- 无可行 → return None。

#### 步骤 P2-L3：词典序 Pass A–D

| Pass | 目标 | 固定 |
|------|------|------|
| A | min joints | util floor |
| B | min weld_types | joints ≤ A\* |
| C | min cut_types | joints, weld_types |
| D | min used_len | 前三项；记录 seg_types |

**B/C 段集来源（评审修订，写死）**：

| 模式 | 段集 | 何时用 |
|------|------|--------|
| **默认 v1** | **Pass A 解实际用到的段子集** `σ_A` | 列数可控；可能次优种类 |
| **可选** | 完整 `recipe.sigma_weld` | 仅当 `\|σ_A\|` 过小且 diagnosis 提示种类可压时，**加一轮** |

v1 实现路径：

1. Pass A 出可行解。  
2. 从 A 的 `cut_patterns`/`weld_patterns` 提取 `σ_A`。  
3. 在 `σ_A` 上跑 `_compress_types` 风格 Pass B/C（已验证可工作）。  
4. 列数 ≤ 5000 可用 zW/zC big-M；更大则只做启发式压缩 + 局部 ILP。

**禁止**：在超大完整 Σ 上同时挂全部种类 big-M（调研已证慢）。

#### 步骤 P2-L4：解导出

从 SCIP 解填 `weld_patterns` / `cut_patterns` / `seg_types`；`solver="lex_master"`。  
**必须**经 reconcile 后再比词典序。

#### 步骤 P2-L5：LexMaster 单测（新增）

**新建**：`scripts/tests/test_lex_master.py`

- D1 `idx=1`（或 corpus 等价）：Pass A 得 joints=0（若物理允许）。  
- 小手工例：2 管 1 母料，断言 util≥floor 且 verifier 过。  
- 禁焊不可达注入：Recipe 标记 hard_unreachable 时 Lex 不被调用（由 L0 测）。

### 4.3 L0 切换主路径

**文件**：`scripts/solve_v4_production.py`

```text
pure_cut?
  else:
    recipe = build(group)
    if recipe.meta.get("hard_unreachable"):
        sol = ladder(..., budget=剩余, recipe=recipe)
    else:
        sol = lex_master.solve(..., tl=lex_budget)
        if sol is None or (incumbent 更差且仍有预算):
            sol = ladder(..., budget=剩余全部, incumbent=sol, recipe=recipe)
        # 可选仅一轮：3a 缺段回灌 → 扩 Σ → 再 lex 一次
```

### 4.4 Fallback 补全

| 阶 | 实现要点 |
|----|----------|
| **3a** | **不是**模糊「扩一圈网格」。定义为：从 Lex/Recipe `diagnosis` 提取 `missing_segs` / `pipe_unreachable` 建议段 → 并入 Σ → **再跑一轮 LexMaster**（tl≤0.15×总预算）。仅当 Lex 连续失败才允许 restricted arc-flow（段集=`sigma ∪ missing`，tl≤0.10×总预算） |
| **3b** | `_try_colgen_fallback` 仅允许 pricing 在 Σ 内；`demand>800` **跳过** |
| **3c** | FFD → `_try_std_compress` 强制 |
| **3d** | `solve_pure_cut` + `_lex_better` |

### Phase 2 完成门禁（必须全过）

| # | 门禁 | 标准 |
|---|------|------|
| G1 | **113146 T91** | 合法解 + verifier 过 + 墙钟 **≤15s**（冲刺 ≤8s） |
| G2 | **113146 12Cr1MoVG** | 同上 **≤15s**（冲刺 ≤12s） |
| G3 | 语料固定 50 | 无解=0；相对 legacy success 不劣化 ≥75% |
| G4 | picked20 真基准 | 无回归；L8/L12/L16 优势保持 |
| G5 | 主路径占比 | 50 例中 `solver_path` 含 `lex_master` ≥60% |
| G6 | 禁焊题 | 113146 探针每管可达；API 解无 `FORBIDDEN_WELD` |

```bash
.venv\Scripts\python.exe scripts\probe_recipe_113146.py
.venv\Scripts\python.exe scripts\acceptance_113146.py --tl 15 --engines v4
.venv\Scripts\python.exe scripts\_corpus_run.py --n 50 --tl 60 --proc_timeout 120 --seed 0
.venv\Scripts\python.exe scripts\_cmp_metrics.py 3,4,6,8,12,13,14,15,16,17,20 --tl 120
```

---

## 5. Phase 3 — 规模档 / L20（10–15 个工作日，可与 P2 后期并行）

### 步骤 P3-1：demand 分档路由（仍在 L0 内）

| demand 根数 | 策略 |
|-------------|------|
| ≤150 | 完整 Recipe+Lex |
| 151–800 | Σ cap 降为 32（**可达段例外不砍**）；col_cap 降；优先 joints |
| >800 | **禁止** 3b CG；管长聚类成 ≤12 簇再 Recipe；或切 sub-group |

### 步骤 P3-2：L20 SLA

1. 不裸枚举千列 FFD 交付。  
2. 120s 内：util≥floor，拼法 <200（数量级）。  
3. 禁止 13min 挂死（硬超时杀进程 + 返回 3c 解）。

### Phase 3 门禁

- [ ] L20 硬超时内必返回  
- [ ] 拼/切种类较改造前 FFD 降 1 数量级以上  

---

## 6. Phase 4 — 生产化固化（5–8 个工作日）

### 步骤 P4-1：CI 门禁脚本

`scripts/ci_v4_gate.py`：

1. 跑 `probe_recipe_113146`（快失败）。  
2. 跑 acceptance_113146（≤15s）。  
3. 跑语料 50 seed=0。  
4. 失败则非 0 退出。

### 步骤 P4-2：文档对齐

1. `SPEC.md` §五/§六：默认路径 = Recipe+LexMaster；段平衡约定引用本文 §1.3。  
2. `docs/research/` 加索引：已收编 / 对照 / 废弃。  
3. 更新 `algorithm-asset-inventory.md`：v4 = L0 生产路径；rcg 临时交付标注过期。

### 步骤 P4-3：代码归属（不阻塞功能，但列入 DoD 跟踪）

将 `recipe_builder` / `lex_master` / `v4_fallback` / `solve_v4_production` 迁入：

```text
backend/app/engine_v4/
```

`scripts/` 仅保留 CLI/验收入口。避免生产 API 长期 `sys.path` 依赖 scripts。

### 步骤 P4-4：冻结

- baseline Tier：**仅 bugfix**。  
- 新调研 PR 模板必填：「如何进入 Recipe 或 Fallback？」  
- 删除或关闭 `V4_TEMP_ENGINE`。

---

## 7. 日计划建议（压缩版）

### 7.1 双轨时间线（评审修订）

| 轨道 | 时间 | 交付 |
|------|------|------|
| **业务临时** | W1 内 | 113146 可用 `rcg`（或 baseline）出合法解；墙钟不追对方，但**不停工** |
| **算法达标** | W4–W6 | v4 LexMaster ≤15s；关闭临时开关 |

### 7.2 单人主攻周表

| 周 | 交付 |
|----|------|
| **W1** | Phase0 全部（含 P0-5 探针）+ P1 编排壳 + 临时 rcg 开关 |
| **W2** | Recipe R1–R6（含禁焊剪枝）+ 单测；L0 接 Recipe |
| **W3** | LexMaster L1–L3（Pass A）+ L5 单测；113146 第一次 ≤60s |
| **W4** | Pass B/C 用 σ_A；Fallback 3a（缺段回灌）；113146 ≤15s |
| **W5** | 语料 50 + picked20 回归；修劣化 |
| **W6** | Phase3 分档 + CI + SPEC + engine_v4 迁移启动；宣布生产就绪 |

（两人并行：A=Recipe/Lex，B=Fallback/验收/CI → 可缩到 ~4 周。）

---

## 8. 明确不做（防再次分裂）

1. 不再新开 GA/GPU/LNS 主路径实验。  
2. 不把 route3/rcg/global 设为**永久**默认（临时开关除外，Phase2 后关闭）。  
3. 不在 Phase2 完成前做 L20 全配方枚举原型。  
4. 不追 `legacy_claimed` 伪基准数字。  
5. 不为「再快 10%」回头改 arc-flow 默认网格当主菜。  
6. **不在** `solve_arcflow` 与 `v4_fallback` **同时**维护两套完整兜底逻辑。

---

## 9. 风险登记（实现时对照）

| 风险 | 信号 | 动作 |
|------|------|------|
| Σ 缺段 / 禁焊不可达 | 113146 焊口爆炸或 Lex None | R5 邻域回灌 → 3a 缺段回灌 **仅一轮** |
| 列枚举爆 / DFS 截断 | col 触 cap / `enum_truncated` | 降非锁定 Σ / 分簇 / 进 3c |
| Lex 慢 | >15s 无可行 | 动态预算：先 A 交付，B/C 异步或下一请求（v1.1） |
| 与 verifier 不一致 | verification_failed / SEGMENT_BALANCE | 统一 reconcile；检查 §1.3 |
| 双轨飘移 | recipe 与 `_standard_segment_lengths` 不一致 | **只保留一处实现** |
| 内外双兜底 | 同一失败路径跑两次 FFD | 检查 `internal_fallback=False` |
| Σ cap 不够 | probe 显示 >48 且砍后不可达 | **提高 cap**，不要死守 48 |
| 临时 rcg 忘记关 | 生产长期非 v4 | P4-4 门禁强制关 |

---

## 10. 验收样例资产位置

| 资产 | 路径 |
|------|------|
| 现场题（用户提供） | `问题__20260722T100005.json`（12Cr1MoVG）、`...T100015.json`（T91） |
| 建议入库 | `data/acceptance/113146/cr.json`、`t91.json` |
| 探针输出 | `data/acceptance/113146/sigma_probe.json` |
| 对方标杆 | T91 **~4s** util≈99.31%；Cr **~9s** util≈99.10% |
| 语料落盘 | `data/legacy-cases/` + `difficulty_pools.json` |
| 战略摘要 | `docs/v4-production-ultimate-plan.md` |

---

## 11. 完成定义（Definition of Done）

同时满足才可宣称「V4 生产就绪 / 可对标教授侧墙钟量级」：

1. `engine=v4` → **仅** `solve_v4_production`。  
2. 默认路径日志显示 **`lex_master`**（Fallback 为辅）。  
3. **113146 两题 ≤15s**、verifier 通过、util≥0.95、无禁焊违规。  
4. 语料 50：无解=0，不劣化≥75%。  
5. picked20 真基准无回归。  
6. SPEC 与 CI 已更新；段平衡约定写入 SPEC。  
7. `V4_TEMP_ENGINE` 已关闭；兜底单点（仅 L0 ladder）。  
8. （跟踪项）`engine_v4` 包迁移至少有计划或已完成。

---

## 12. 立刻可执行的第一步（今天）

```text
1. 建目录 data/acceptance/113146/ ，拷入两道 JSON
2. 完成 P0-1 util_floor + P0-2（internal_fallback 开关）
3. 新建 scripts/solve_v4_production.py 空壳：先转调 solve_arcflow(internal_fallback=False)，失败走最小 ladder
4. 新建 scripts/recipe_builder.py：T0+残料+标准段
5. 新建 scripts/probe_recipe_113146.py：打印 |Σ| + 每管合法拼列数（过 V0/V1）
```

若探针显示：`|Σ|` 几十级 **且** 每管拼列≥1，则 Phase2 机制验证过半；再上 LexMaster Pass A，冲击秒～十五秒。  
若某管列=0：**先修 Recipe 禁焊可达，再写 Lex**——否则后续全是空转。

---

## 13. 修订对照表（v1 → v2）

| 评审项 | v1 问题 | v2 处理 |
|--------|---------|---------|
| 禁焊区 | Recipe 只按长度生成 | §4.1 R5 + `sigma_weld`/`sigma_cut` |
| 满装对 | `≈S`、无可达检查 | `a+b+kerf≤S` + 可达门槛 |
| 段平衡 | 写 ≥ 未接 reconcile | §1.3 强制约定 |
| Σ≤48 | 教条 | 可调；探针定 cap；可达段不砍 |
| B/C 段集 | 未定义 | 默认 σ_A，可选完整 Σ 一轮 |
| 预算切片 | 固定 55/25 | 动态预算 §P1-1 |
| 3a 定义 | 「扩一圈」模糊 | 缺段回灌再 Lex，末选 restricted arc-flow |
| 双兜底 | P0 与 L0 叠床架屋 | `internal_fallback` 单点 §P0-2 |
| 业务等待 | W4 才有解 | §7.1 临时 rcg |
| DFS 爆炸 | 触 budget 未定义 | P2-L1 截断标记 + Fallback |
| 词典序 | 缺 seg_types | §1.2 五档 |
| Lex 单测 | 缺失 | P2-L5 |
| 包结构 | scripts 长期进生产 | P4-3 迁移跟踪 |
