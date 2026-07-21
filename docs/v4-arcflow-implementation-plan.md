# V4 (arc-flow) 完整实现方案

> **定位**：本文件是 V4 求解引擎的**实现规格**，明确到每个算法怎么做、每个阶段解什么、约束如何表达、失败如何归因、做到什么算合格。面向"零上下文接手者"（人或 AI agent）可据此独立实现与验收。
>
> **权威依据**：目标函数词典序与硬约束**以 `docs/SPEC.md` 为准**；候选生成规则、分阶段工程化、不可行诊断分类**参照 `docs/research/global-joint-model-spec-v1.md`**；与老软件对标口径参照 `docs/research/cutting-welding-best-practices.md`。三者冲突时以 SPEC + 本文件为准。
>
> **代码落点**：现状实现 `scripts/_arcflow_v3.py`（core）+ `backend/app/route_v4_arcflow.py`（适配器）。本方案在其上**增量演进**，不推倒重来。

---

## 0. 为什么要做这次演进（问题陈述）

现状 V4（`scripts/_arcflow_v3.py::solve_arcflow`）只实现了**两阶段词典序**：

- **Pass1**：`min 用料总长 usedlen`（`_arcflow_v3.py:556-573`）
- **Pass2**：固定 `usedlen ≤ U*`，`min 总焊口 joints`（`_arcflow_v3.py:575-583`）

即现状目标序为 **用料 → 焊口**，**到此为止**。它**没有**拼法种类 / 切法种类 / 段种类的压缩阶段。

用户实测结论（本项目对话）："用料/利用率已接近老软件，主要差在**切法/拼法花样太散 + 焊口偏多**"。根因正是：V4 在 Pass2 拿到"焊口最少"的一个解后就停手，解里可以充斥大量**只用一次的切法/拼法**——数学上等价、车间上灾难。老软件之所以被沿用，正是因为它**花样少、组织生产简单**。

因此本次演进的**唯一目标**：在保持用料/焊口不劣化的前提下，**把切法种类、拼法种类、段种类逐级压下来**，对齐 SPEC 第二节的完整词典序。

---

## 1. 目标函数（词典序，权威规格）

**以 `SPEC.md` 第二节为准**。逐级最小化/最大化，前者优先，仅前者相等时才比较后者：

| 序 | 指标 | 方向 | 语义 |
|----|------|------|------|
| 0 | **可行性 + 利用率硬底线** | 约束 | `util ≥ util_floor`（见 §1.1）。不达底线视为不可行，进入诊断，不进入花样压缩。 |
| 1 | **总焊口数 joints** | 最小 | Σ 每根成品管 (段数−1)。车间最看重。 |
| 2 | **拼法种类 weld_types** | 最小 | 不同"段有序序列"的种类数。 |
| 3 | **切法种类 cut_types** | 最小 | 不同"(定尺, 段多重集)"的种类数。 |
| 4 | **段种类 seg_types** | 最小 | 不同段长的种类数（段字母表大小）。 |
| 5 | **利用率 util** | 最大 | 需求总长 / 实际耗用母料总长。 |

> **注意与现状的差异**：现状把"用料"放在焊口**之前**（Pass1→Pass2）。SPEC 把**利用率放在最后**（第 5 档），只保留一个**硬底线**作可行性门槛（第 0 档）。本方案据此**重排**：用料不再是独立的第一优化目标，而是"底线约束 + 末档软目标"。这是与现状最本质的一处改动。

### 1.1 利用率硬底线 `util_floor`（用户已拍板）

```
util_floor = max(0.95, legacy_util - 1e-3)   # 有老软件结果时
util_floor = 0.95                            # 无老软件结果时
```

- `legacy_util` 来自样本 `MOMRESULTJSON.GeneralInfo.UtilRate`（见 `_v3_regress.py:_legacy_from_record`）。
- 若库存总长本身不足以达到 `util_floor`（`demand_length / stock_length < util_floor`），标记 `A_LENGTH_SHORTFALL`，不进入压缩（§5）。
- `util_floor` 只作**约束**，不作优化目标；达标后 util 只在第 5 档被动最大化。

### 1.2 词典序的工程实现方式：分阶段 lexicographic ILP

不使用单一加权目标（权重难标定、易被浮点淹没）。采用**逐阶段固定 + 收紧**：解完第 k 档后，把第 k 档的最优值作为**约束**冻结（可带极小 slack），再优化第 k+1 档。共 5 个 pass（见 §6）。这与 `global-spec-v1` §4 的 Phase A/B/C/D 工程化思路一致，也与现状 Pass1/Pass2 的写法（`freeTransform` + `addCons` + `setObjective`）同构，**改动面可控**。

---

## 2. 硬约束（必须满足，违反即不可行）

**以 `SPEC.md` 第三节 + `verifier.py` 为准**（verifier 是独立复算，是最终裁判）。V4 生成的每个候选与每个解都必须满足：

| 约束 | 语义 | 表达位置 |
|------|------|----------|
| 段长守恒 | 一根成品管各段之和 = 管型长度 | 候选生成 `_legal_pattern`；模型内 arc-flow 路径守恒 |
| 最大焊口数 `max_joints` | 单管段数 ≤ `max_joints + 1`；工艺规则：每 `Weld_Interval_mm`(默认3000) 最多 1 焊口，且 ≤ `Max_Joints_Cap`(默认13) | 候选生成时钳制 |
| 最小焊接间距 `min_weld_distance` | 相邻焊口间距 ≥ `Min_Welding_Length`(默认500)，即**内部段**长 ≥ 该值 | `build_pipe_arcs` 弧合法性 |
| 最小切割长度 `min_cut_length` | 每段 ≥ `Min_Cut_Length`（拼法 ≥2 段时对每段生效，整管豁免） | 段字母表下界 + verifier `MIN_CUT_LENGTH_VIOLATION` |
| 禁焊区 `forbidden` | 焊口绝对位置不落入 `Unweldable_Area` 区间 | `weld_allowed(pos)` |
| 母料库存 | 每种定尺用量 ≤ `stock_demand` | 模型 `used_vars[L] ≤ qty[L]` |
| 必用料 `must_use` | 必用料优先用满（组级优先级，非逐长度下界，见 verifier §843-866） | 模型软/硬约束（§6.6） |
| 段长上界 `hi` | 单段 ≤ min(最长定尺, 最长管型) | 段字母表上界 |

> **易错点（来自 SPEC §九-3 / §11.38 十）**：焊口**绝对位置**上界是 `L - min_cut`，**不能用 hi 截断**——长管(L>hi)靠后的焊口位置本就 > hi；`hi` 只约束**单段长度**。历史上此 bug 导致长管样本全部秒 FAIL。V4 演进不得破坏此不变式。

> **整数毫米不变式**：全程整数毫米，Decimal 精确取整，绝不引入二进制浮点长度（SPEC §九-1）。

---

## 3. 现状架构与本方案的关系

```
route_v4_arcflow.py (适配器)
  └─ solve_arcflow(group, tl, step)   ← 本方案主要改动点
       ├─ classify_group()            分类路由（保留）
       ├─ 可行性 precheck             保留 + 补 util_floor 判定
       ├─ build_pipe_arcs()           拼层弧（保留）
       ├─ 段字母表 (_grid_seg_set)    保留 + 覆盖锚点（§4）
       ├─ 切层弧 + 耦合约束           保留
       ├─ Pass1 min usedlen           → 改为 util_floor 约束（§6.1）
       ├─ Pass2 min joints            保留为第 1 档（§6.2）
       ├─ 【新增】Pass3 min weld_types（§6.3）
       ├─ 【新增】Pass4 min cut_types（§6.4）
       ├─ 【新增】Pass5 min seg_types / max util（§6.5）
       └─ _extract() → 产出 schema    保留
```

**保留（不动）**：arc-flow 建模、分类路由、coarse-to-fine（`step` 网格）、`_find_legal_order`/`node_budget` 防爆炸、`_extract`、verifier、API/Job 外壳。

**改造**：Pass1 语义（用料→底线约束）、Pass2 后接新增 Pass3/4/5、引入 setup-cost 变量与热启动。

**明确不做**（避免走回头路，SPEC §六-已废弃）：不引入列生成/pricing（V3_HANDOFF 已记录放弃）、不做网格枚举段长回退、不做单管 DP。种类压缩靠**setup-cost 二元变量 + 分阶段 ILP + local branching 启发式**，全部在 SCIP 内表达。

---

---

## 4. 候选生成（段字母表 + 弧构造 + 覆盖锚点）

V4 用 **arc-flow**，不显式枚举"切法/拼法列"，而是枚举**段字母表**并构造弧图，切法/拼法作为弧图上的**流路径**隐式表达。这是 V4 与 baseline（显式列枚举）的本质区别，也是它能扛大样本的原因。

### 4.1 段字母表 `alphabet`（算法：网格 + 覆盖锚点）

**目标**：给出一个**尽量小但覆盖充分**的候选段长集合 `S`。段种类越少（第 4 档目标越好），但太少会不可行。

现状 `_grid_seg_set`（`_arcflow_v3.py:596-618`）算法：

```
min_seg = max(min_cut, 1)
S = {}
for each pipe p (length L):
    if min_seg <= L <= max_stock:  S.add(L)          # 整管段（纯切/短管，0 焊口）
    a = step
    while a < L:
        if min_seg <= a       <= max_stock: S.add(a)   # 切分点左段
        if min_seg <= L - a   <= max_stock: S.add(L-a)  # 切分点右段
        a += step
return sorted(S)
```

**保留此算法**，并**新增两条规则**（对齐 `global-spec-v1` §5.2 覆盖锚点）：

1. **stock-aligned 锚点**：对每个定尺 `Lstock` 与每根管 `L`，加入 `Lstock`（整根定尺当一段）与 `L − Lstock`（互补段），保证"单焊口对齐定尺"的高密度切法可达。这直接帮助**减少焊口**（一刀切到定尺长）。
2. **覆盖锚点**：字母表中每个被任何拼法弧引用的段长，必须**至少有一条**切层产出弧能生产它（否则耦合约束 §6 令模型不可行）。生成后做一次 self-check，缺失的段长补进对应定尺的切层弧。

> **`step`（粗到精网格步长）**：coarse-to-fine 的核心旋钮。现状档位 `[69,35,23,15, 全量]`（L13 实测）。**保留 coarse-to-fine**：先用粗档（大 step，段少）快速拿可行解与 incumbent，再逐步细化。§6/§8 的热启动依赖它。

> **禁止**：按某个 `sample_id` 手工塞段长（`global-spec-v1` §5.2 明令）。所有段长必须由上述统一规则涌现。

### 4.2 拼层弧 `build_pipe_arcs`（拼法 = 管身图上的路径）

每管型建一张**管身有向图**：节点 = 管上位置（0, 焊口位置候选, …, L），弧 `(a, b, seg)` 表示"从位置 a 焊接一段长 `seg=b−a` 到位置 b"。一条 0→L 的路径 = 一个**拼法**。弧合法性内嵌硬约束：

- 内部段（两焊口之间）长度 ≥ `min_weld_distance`；
- 焊口绝对位置 ∈ `[min_cut, L−min_cut]` 且不落禁焊区（`weld_allowed`）；
- 路径弧数 ≤ `max_joints + 1`（段数上限）。

**保留现状实现**（`_arcflow_v3.py:408-419` 收集 `seg_lengths`；流守恒见 :452-469）。

### 4.3 切层弧（切法 = 母料图上的路径，Valério de Carvalho 规范化）

每种定尺 `L` 建一张 arc-flow 图：节点 = 母料上已消耗物理长度（含内部切缝 kerf），弧 = 放一段 + 废料弧到 L。一条 0→L 路径 = 一个**切法**。

**关键：规范化去对称**（`_arcflow_v3.py:471-521`）——段按**非增序**放置（`maxseg[a]` = 到达 a 时下一段允许的最大长度），任意多重集只保留唯一规范路径，弧数从组合级降到近线性。**必须保留**，否则大样本弧爆炸。

**规模阀门**：`_cut_arc_count > CUTCG_ARC_THRESHOLD(200000)` 时切到 `solve_arcflow_cutcg`（切层列生成变体，实为网格段集 + 限定段整数求解，见 §6.7），**保留**。

### 4.4 候选生成的单测（必须）

- **合法性**：每条拼层弧过 `_legal_pattern`；每条切层路径 `sum(parts)+kerf ≤ L`。
- **覆盖锚点**：字母表每个段长至少一条切层产出弧。
- **禁样本特例**：字母表生成只依赖 `(pipes, stocks, params, step)`，不读 `sample_id`。

---

## 5. 不可行诊断分类（失败必须归因）

**以 `global-spec-v1` §6 为准**。求解失败必须归到下列之一，**禁止"再加一条规则"式打补丁**：

| 代码 | 判定条件 |
|------|----------|
| `A_LENGTH_SHORTFALL` | `demand_length / stock_length < util_floor`（库存总长不够） |
| `A_PROCESS_INFEASIBLE` | 某管型无任何合法拼法（`build_pipe_arcs` 出空图） |
| `A_STOCK_STRUCTURE` | 总长够但定尺结构撑不住 max_joints 等（拼层可行、耦合后切层不可行） |
| `C_CANDIDATE_INSUFFICIENT` | 段字母表/弧池不足致模型不可行（放宽 step 或补锚点后可解则归此类） |
| `C_INTEGER_FAIL` | LP 松弛可行但整数失败 |
| `C_TIMEOUT` | 时限内无可行解 |
| `E_VERIFICATION_FAILED` | 求解器输出未过 `verifier.verify_solution` |

**诊断实现**：`solve_arcflow` 在每个 pass 无解时（现状 `_arcflow_v3.py:565-568, 586-589`）返回结构化 `{status, code, detail}`，而非静默 `return None` / 静默 fallback。适配器 `route_v4_arcflow.py` 把诊断透传给 API，前端可显示（解决"卡 500 秒不知发生了什么"的老问题）。

> **兜底解**：`pure_fallback`（纯切 0 焊口解）在焊接无解/更差时保留为兜底（`_arcflow_v3.py:406, _better_of`）。这是**兜底**，不是**fallback 到 baseline**——种类压缩失败绝不静默退回 baseline，而是返回"当前最好的已验证解 + 诊断"。

---

---

## 6. 分阶段词典序求解（5 个 Pass 的精确算法）

沿用现状的 lexicographic-ILP 写法：解完一档 → `freeTransform()` → 把该档最优值 `addCons` 冻结 → `setObjective` 下一档 → `optimize()`。一次建模，5 次求解，模型骨架不变，只换目标与追加约束。

**核心建模难点（必须先讲清）**：arc-flow 的流变量是**聚合的**——`g[i][(a,b)]` 是"有多少根 i 管走这条弧"，`f[L][(a,b)]` 是"有多少根 L 定尺放这段"。它们**不区分具体是哪一根管/母料的哪一种整体方案**。而"切法种类 / 拼法种类"是**整根方案（path）级别**的概念，聚合流天然拿不到。因此 §7 需要引入**path 层变量**来表达种类，是本方案最实质的新增建模。§6 先给出各 pass 的目标与约束，§7 展开 path 变量。

### 6.1 Pass 0/1 — 可行性 + 利用率底线（改造现状 Pass1）

**现状**：`min usedlen`（把用料当第一目标）。**改为**：

```
# 约束（硬底线，取代"min usedlen 当第一目标"）
add:  usedlen <= demand_length / util_floor + eps      # util >= util_floor
# 先求一个可行解确认底线可达；目标此档可置常数 0（纯可行性）或仍 min usedlen 拿紧 incumbent
setObjective(0)  或  setObjective(usedlen, "minimize")   # 见下注
optimize()
if NSols == 0:  diagnose → A_* / C_TIMEOUT ; return 诊断
U_feas = getObjVal (若 min usedlen) 或 当前解 usedlen
```

> **注**：保留 `min usedlen` 作为本档目标是**允许的且推荐的**——它不违反词典序（util 在第 5 档最大化，与"用料尽量少"同向），且能借用现状"极紧 LP 松弛 + FEASIBILITY emphasis"（`_arcflow_v3.py:558-561`）快速拿到好 incumbent 供后续 pass 热启动。区别仅在于：**用料不再冻结为等式**，后续 pass 允许在 `usedlen ≤ demand_length/util_floor` 的**底线松弛**内变动，以换取更少种类。这正是 SPEC"宁可多用几根料也不打乱车间"的落地。

### 6.2 Pass 1 — min 总焊口（保留现状 Pass2）

```
freeTransform(); setParam time
# 不再冻结 usedlen 为 U*，而是保留底线约束 usedlen <= demand_length/util_floor
joints = Σ_i ( Σ_{arc∈i} g[i][arc] - demand_i )       # 现状 :579-582
setObjective(joints, "minimize"); optimize()
J* = getObjVal
```

冻结：`add joints <= J*`（进入下一档）。

### 6.3 Pass 2 — min 拼法种类 weld_types（新增）

冻结 `joints <= J*`，最小化**不同拼法路径的种类数**。种类的表达见 §7.1（path 层 `zW[pattern]` 二元变量 + setup 约束）。

```
freeTransform()
add: joints <= J*                    # 焊口不劣化
setObjective( Σ_pattern zW[pattern], "minimize" ); optimize()
WT* = getObjVal
```

### 6.4 Pass 3 — min 切法种类 cut_types（新增）

冻结 `weld_types <= WT*`，最小化不同切法种类。表达见 §7.2（`zC[cutpattern]`）。

```
freeTransform()
add: joints <= J* ;  Σ zW[pattern] <= WT*
setObjective( Σ_cut zC[cut], "minimize" ); optimize()
CT* = getObjVal
```

### 6.5 Pass 4 — min 段种类 seg_types / max util（新增，末档）

冻结前四档，最后压段字母表实际使用的种类数，并在其内最大化利用率（等价 min usedlen）：

```
freeTransform()
add: joints<=J* ; ΣzW<=WT* ; ΣzC<=CT*
# 段种类: ys[seg] 二元, ys[seg]=1 iff 该段被任何切/拼弧使用
setObjective( BIG * Σ_seg ys[seg] + usedlen , "minimize" )
optimize()
```

`BIG` 取 `> max_possible_usedlen`，使"少一种段"严格优先于"少用料"，同时 usedlen 作末档 tiebreak（第 5 档 max util）。

### 6.6 必用料约束（§2 的 must_use）

必用料是**组级优先级**（verifier §843-866）：不得在必用料未用满时动用可用料。V4 表达：

```
# 每种必用定尺 Lm: used_vars[Lm] >= must_use_qty[Lm]   （需求足够时的硬下界）
# 需求不足时放宽为"用满或用尽需求"，用指示变量避免过约束
```

在所有 pass 中恒定生效（不随档变化）。

### 6.7 大样本路径（coarse-to-fine + 切层列生成变体）

`_cut_arc_count > 200000` 时走 `solve_arcflow_cutcg`（`_arcflow_v3.py:433`）：coarse-to-fine 逐档细化段集，每档限定段整数求解，取首个达标档的解。**新增 pass 在此路径同样适用**——在选定的达标档段集上追加 Pass2/3/4（种类压缩）。粗档段少、path 变量少，种类压缩反而更快，与 coarse-to-fine 天然契合。

---

## 7. 种类压缩的建模（本方案的技术核心）

聚合流拿不到"种类"。引入 **path 层二元变量**桥接。为控规模，只对**被 LP 松弛/上一档解实际用到的路径**建 path 变量（懒生成），而非枚举全部路径。

### 7.1 拼法种类 `weld_types`

**path 提取**：对每管型 i，在弧图上枚举"被当前流用到的 0→L 路径"（用 `_find_legal_order` 同款回溯，带 `node_budget`）。设候选拼法路径集合 `P_i`，每条 `p` 对应一个段序列。

引入变量：
- `xp[i,p] ≥ 0` 整数：用路径 p 生产 i 管的根数（path 层流分解）。
- `zW[τ] ∈ {0,1}`：拼法**种类** τ（去管型身份的段序列，与 verifier `welding_types` 口径一致——见 verifier :497,653）是否被启用。

约束：
```
Σ_{p∈P_i} xp[i,p] = demand_i                          # path 分解满足需求
Σ_{p: uses arc (a,b)} xp[i,p] = g[i][(a,b)]            # path 层与聚合流一致（弧守恒桥接）
xp[i,p] <= demand_i * zW[type(p)]                      # 启用 setup：用了就点亮种类
```
目标（Pass2）：`min Σ_τ zW[τ]`。

> **规模控制**：`P_i` 不全枚举。做法：Pass1 拿到 min-joints 解后，只对该解中出现的路径 + 少量"合并候选"（把两条相近路径归并到同一种类的替代路径）建 `xp`。若 Pass2 因候选不足无法进一步压，允许一轮**路径补充**（在弧图上找能替换现有路径、使种类更少的等价路径），gap-driven，不写样本补丁。

### 7.2 切法种类 `cut_types`

完全对称：
- `yc[L,q] ≥ 0` 整数：定尺 L 用切法路径 q 的根数。
- `zC[σ] ∈ {0,1}`：切法**种类** σ = `(L, 段多重集)`（与 verifier `cutting_types` 口径一致，verifier :668,823 用 `(stock_length, sorted(parts))`）。

约束：
```
Σ_q yc[L,q] = used_vars[L]
Σ_{q: uses arc} yc[L,q] = f[L][(a,b)]
yc[L,q] <= qty[L] * zC[type(q)]
```
目标（Pass3）：`min Σ_σ zC[σ]`。

### 7.3 段种类 `seg_types`

聚合层即可表达，无需 path 变量：
```
ys[seg] ∈ {0,1}
Σ_{arc: fseg==seg} f[L][arc]  <=  BIGSEG * ys[seg]     # 该段被切层生产则点亮
```
目标（Pass4）：`min Σ ys[seg]`（+ usedlen tiebreak）。

### 7.4 用启发式加速种类压缩（工程实践）

纯 setup-cost ILP 在大样本可能慢。叠加以下**任一/组合**（在 SCIP 内或外层）：

1. **Local branching**：以上一档解为中心，限制"改变的 path 数 ≤ k"，滚动扩大 k。适合"从可行解出发少量重排以并种类"。
2. **Relax-and-fix**：先固定大部分 `zW/zC`，只放开少数最可能合并的种类求解，迭代。
3. **RINS/贪心并类后修复**：贪心地把"只用 1~2 次的稀有种类"重指派到已启用种类的等价路径，再让 ILP 修复可行性。

这些是**加速手段**，不改变词典序与最优性判定（时限内取最好已验证解）。**优先实现 setup-cost ILP + local branching**，其余按需。

---

---

## 8. 热启动（贯穿各 Pass，工程实践）

分阶段 ILP 的每一档都应把上一档的可行解作为 **warm start** 喂给 SCIP，避免每档从零求解。

- **档间热启动**：Pass k 结束拿到解向量 → 构造 Pass k+1 的初始可行解（上一档解天然满足 k+1 的追加约束，因为 k+1 只是多了 setup 目标）→ `m.createSol()` + `m.setSolVal()` + `m.trySol()`。
- **coarse→fine 热启动**：粗档段集解 → 映射到细档段集（粗档段是细档段的子集）作为初始解，加速细档。现状 coarse-to-fine 已逐档跑，只需把上档解显式喂入下档。
- **pure_fallback 作 incumbent**：纯切 0 焊口解始终可作一个合法 incumbent 传入（焊口目标下它劣，但保证 Pass 有解、有界）。
- **legacy 解作 warm start（可选，仅热启动，不作目标）**：有老软件方案时，可将其切/拼方案翻译成 path 变量初值喂入 Pass2/3——**仅用于加速收敛**，绝不写入目标或作为答案（对齐用户"指标自洽、不依赖 legacy"的要求）。

> SCIP warm start API：`Model.createPartialSol()/setSolVal()/trySol()`。整数流变量与 path 变量都要设初值，且需内部一致（`xp` 聚合回 `g`）。

---

## 9. 验收与测试

### 9.1 验收口径（以 SPEC §七为准，逐指标词典序对比老软件）

"**不比老软件劣化即合格**"：

1. 总焊口数 ≤ 老软件 —— 首要且必须。
2. 拼法种类 ≤ 老软件。
3. 切法种类 ≤ 老软件。
4. 段种类 ≤ 老软件。
5. 利用率 ≥ 老软件 − 1e-3。

> 老软件指标**仅作外部回归判定**，不作求解器内部目标（对齐 `global-spec-v1` §4 验收对标、用户"自洽最优"要求）。

### 9.2 必过的独立校验

每个输出解**必须过 `backend/app/verifier.py::verify_solution`**（`passed==True`，零 error）。verifier 是独立复算，不信求解器自报指标。任何"解出但校验失败"记 `E_VERIFICATION_FAILED`，视为不合格。

### 9.3 测试层次

1. **单测**：候选生成合法性/覆盖锚点（§4.4）；每个 pass 的约束冻结正确性（下一档不劣化上一档）。
2. **单例自查**（用户手动验收前的自检）：用 `_arcflow_v3.py <level> --tl <T>` 打印 `joints/weld_types/cut_types/seg_types/util` 五元组，逐档对比老软件与"演进前基线"（附录 A）。
3. **回归**：`_v3_regress.py`（固定档 L1–L20 与分层抽样），确认无焊口/利用率回归。
4. **用户手动验证**：在用户指定单例上，由用户人工确认切/拼方案的"花样收敛"达标，作为最终验收（用户明确要求）。

### 9.4 CLI

```bash
cd 102
# 单例（level 取 _picked20_full.json 的 level）
python scripts/_arcflow_v3.py 7  --tl 40
python scripts/_arcflow_v3.py 13 --tl 120
# 回归
python scripts/_v3_regress.py --tl 120
```

---

## 10. 工程实践清单（Definition of Done）

- [ ] 词典序严格按 SPEC §二（焊口→拼→切→段→util），util 仅第 0 档底线 + 第 5 档软目标。
- [ ] 每档用 `freeTransform + addCons(冻结) + setObjective` 串接，档间不劣化有单测覆盖。
- [ ] path 层变量（`xp/zW`, `yc/zC`）与聚合流（`g/f`）一致性约束正确，懒生成控规模。
- [ ] 全程整数毫米，无二进制浮点长度（SPEC §九-1）。
- [ ] 焊口绝对位置上界 `L−min_cut`，不用 hi 截断（SPEC §九-3）。
- [ ] `enum_pipe_pats`/path 枚举带 `node_budget` + 种子，大样本不卡死（SPEC §九-4）。
- [ ] 失败归因到 §5 七类之一，透传诊断，**不静默 fallback 到 baseline**。
- [ ] 每个输出解过 verifier（零 error）。
- [ ] 候选生成不读 `sample_id`，无样本特例（§4.4）。
- [ ] 时限是输入参数，不写死；档间按 A50%/B30%/C20% 或均分传入时限（`global-spec-v1` §7）。
- [ ] 改代码用编辑器工具，不用 PowerShell `-replace`/`Set-Content`（SPEC §九-6）。
- [ ] 每完成一个子任务运行 `_v3_regress.py` 确认无回归。

### 实施顺序（子任务拆分，便于分次交付）

1. **S1 目标重排**：Pass1 用料→底线约束；保留 Pass2 焊口。跑 L1–L20 确认 util 达底线、焊口不升。（最小改动，先落地）
2. **S2 拼法种类 Pass2**：加 `xp/zW` + 一致性约束 + `min ΣzW`。单例验证 weld_types 下降。
3. **S3 切法种类 Pass3**：加 `yc/zC` + `min ΣzC`。
4. **S4 段种类 Pass4 + util 末档**：加 `ys` + `BIG·Σys + usedlen`。
5. **S5 热启动 + local branching**：档间 warm start，种类压缩提速。
6. **S6 诊断透传**：§5 七类结构化诊断，适配器/前端展示。

每步独立可验、独立回归，任一步出问题不影响已落地的前序收益。

---

## 附录 A：演进前基线（现状 V4，本机实测）

> 环境：Python 3.11 + pyscipopt 6.2.1（SCIP）。样本 `scripts/_picked20_full.json`。数值为现状 `solve_arcflow`（用料→焊口两阶段）输出，作为演进对照基线。

| level | 规格 | 时限 | util (现状 / 老软件) | 焊口 (现状 / 老) | 拼法 (现状 / 老) | 切法 (现状 / 老) | 段种类 | 现状不足 |
|-------|------|------|------|------|------|------|------|------|
| L7 | SA-213TP310HCbN/51x13 | 40s | **0.9742** / 0.9914 | 18 / 39 | 2 / 2 | 3 / 5 | 5 | util **未达底线**（现状"用料优先"也没顶到 target 0.9925）→ 印证 §6.1 把 util 改为底线约束的必要性 |
| L13 | SA-213TP310HCbN/45x12 | (档1) | 0.9943 / 0.9943 | 68 / 68 | — / 4 | — / 6 | — | 走 coarse-to-fine+切层列生成；全段档超时未收敛，取档1达标解；焊口/util 已追平老软件，但**拼法/切法种类未压**（现状无 Pass3/4）→ 印证 §7 种类压缩为主攻方向 |

**结论**：
- 焊口现状已能打平或优于老软件（L7 18<39，L13 68=68）——**Pass2 有效**。
- **切法/拼法种类现状不压**（无 Pass3/4），正是"花样太散"的根因。
- **util 现状不稳**（L7 未达底线）——把 util 从"第一目标"改为"底线约束 + 末档软目标"后，可释放搜索空间给种类压缩，同时用底线守住利用率。

> 附录 B（演进后对比）：见下。

## 附录 B：演进后对比（S1–S4 已实现，本机实测）

> 环境同附录 A。数值为改造后 `solve_arcflow`（词典序：util 底线 → min 焊口 → min 拼法种类 → min 切法种类 → min 段种类）输出。自检 = `scripts/_check_v3.py`（镜像 verifier 核心不变式：段供需**精确**平衡、需求满足、焊口计数、用料）。

### B.1 主路径（切层弧 ≤ 20 万，走 `solve_arcflow` 五档词典序）

| level | util (演进后 / 老) | 焊口 (后 / 老) | 拼法 (后 / 老 / 演进前) | 切法 (后 / 老 / 演进前) | 段种类 (后 / 演进前) | 自检 |
|-------|------|------|------|------|------|------|
| L7 | 0.9742 / 0.9914 | 18 / 39 | **1** / 2 / 2 | **3** / 5 / 3 | **3** / 5 | 通过 |
| L10 | 0.9742 / 0.9953 | 18 / 38 | **1** / 5 | **3** / 8 | 3 | 通过 |

### B.2 极限档（切层弧爆炸，走 `solve_arcflow_cutcg` + `_maybe_compress` 后处理压缩）

| level | util (后 / 老) | 焊口 (后 / 老) | 拼法 (后 / 老 / 演进前) | 切法 (后 / 老 / 演进前) | 段种类 (后 / 演进前) | 自检 |
|-------|------|------|------|------|------|------|
| L8 | 0.9965 / 0.9965 | 51 / 64 | **1** / 5 / 6 | **5** / 8 / 6 | **3** / 10 | 通过（演进前段平衡**破坏**，已修复） |
| L12 | 0.9928 / 0.9928 | 98 / 115 | **3** / 4 / 8 | **7** / 5 / 11 | **6** / 15 | 通过（演进前段平衡**破坏**，已修复） |
| L11 | 0.9980 / 0.9945 | 213 / 115 | 10 / 2 | 12 / 5 | 19 | 通过（段集大+切法列 2.1 万，压缩**自动跳过**；焊口本身未解好，属 S5） |

**结论（S1–S4）**：
- **拼法种类全面达到或优于老软件**：L7 1<2、L8 1<5、L10 1<5、L12 3<4。
- **切法种类显著收敛**：L7 3<5、L8 5<8、L10 3<8；L12 7 略高于老软件 5（可继续调）。
- **段种类大幅下降**：L8 10→3、L12 15→6。
- **修复正确性 bug**：cutcg 路径原 `>=` 耦合导致段"过产"（verifier `SEGMENT_BALANCE_MISMATCH`），压缩模型改 `==` 精确平衡后 L8/L12 自检通过。
- **焊口不劣化**：全部档焊口 ≤ 老软件或持平（L11 除外——其焊口层本身未解好，是 S5 目标）。
- **util 守住底线**：底线不可达档（L7/L10 受库存物理顶死）退化为"探最高可达利用率再 min 焊口"，不产生低利用率废解。

**遗留（转入 S5/S6）**：
- L11/L13 类极限档焊口质量差（213 vs 115）：需更好的段集/热启动/local branching 改善焊口层求解，压缩才有意义。
- 段集 >16 或枚举列 >2 万时压缩自动跳过（保留原解）：需 S5 的启发式列筛选来扩大压缩覆盖面。


## 附录 C：S5（热启动）+ S6（诊断透传）实现与实测

> 环境同附录 A/B。

### S6 诊断透传（已落地）

替换"静默 fallback"为结构化诊断，附在结果 `diagnosis` 字段并在 CLI/回归打印：

- `INFEASIBLE`：分类器判定无解（料总长 < 需求，或几何不可行），打印判据。
- `NO_SOLUTION`：焊接与纯切均无可行解。
- `UTIL_BELOW_FLOOR`：利用率 < 硬底线 `max(95%, legacy-1e-3)`（库存物理受限）。
- `FELL_BACK_TO_PURECUT`：焊接未给出不劣于纯切的解，回退纯切兜底（0 焊口）。
- `JOINTS_ABOVE_LB`：焊口高于理论下界 `Σ(⌈L/max_stock⌉-1)·demand` 超 15%，提示"段集不足/段集受库存约束未达最优分段"。

诊断只暴露成色、不改变解的选取，调用方（前端/报告/回归）据此显式呈现差距，避免"看到一个数字就当最优"。

### S5 热启动（已落地）

`_solve_int_restricted` 新增 `warm=` 入参：在 cutcg 的**完整兜底档**（段集是所有档的超集），把当前 `best` 的拼法（`weld_patterns`）映射为拼层弧流，用 `createPartialSol`+`setSolVal` 注入，切层由 SCIP `completesol`（`maxunknownrate=1.0`）补全；映射失败（弧不在当前图）则安全退化为冷启动。热启动时把 `joint_cap` 放宽到 `best` 焊口本身（否则 warm 解违反上界被判不可行）。

**L11 实测**：热启动成功注入（"注入拼层弧流 25 条，上一档 213 焊口"），但完整档 31s 仍未在预算内改进到 <213。

**关于 L11 的根因结论（重要）**：经定量分析，L11 只有 1 种管型 L=23335、demand=90，理论最小焊口下界 = 90（每管 2 段 1 焊口，23335≤2×12000）。库存 174×12000+11500+4800=2,104,300，需求总长 2,100,150 —— **废料预算仅 4,150mm**，几乎顶满。要达到接近下界的少焊口，需大量"11335+12000"这类互补大段，但 12000 母料切 11335 会浪费 665/根，90 根即 6 万 mm 浪费，远超 4,150 预算 —— 故**低焊口在本例物理上不可行**，老软件的 115 已是很强的解。我们的 213 是各**可解档段集**内的最优（48 段档 3.2s optimal 213），完整 388 段档含更优所需段但 MILP 30s 内解不动。

**S5 尝试与取舍**：曾试"最小焊口锚点段集"（为少段拼管构造互补大段并前插探路档），实测反而使 L11 从 213 退化到 228（稀疏档给出的次优 incumbent 过早收紧 joint_cap），且拉长 cutcg 硬档运行时——**已回退删除**。保留热启动（正确、安全、对更易档有益，对 L11 这类硬档无害但也无力回天）。

**判定**：L11/L13 的焊口差距本质是"MILP 在完整段集上的求解强度/时限"问题，非建模缺陷。进一步改善需要（超出本轮范围）：更强的 primal 启发式（RINS/local branching 迭代）、更长时限、或列生成+分支定价。



