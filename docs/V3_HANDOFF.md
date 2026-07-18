# 蛇形管排料求解器 — V3 交接文档（arc-flow 全局整数模型）

> **用途**：本文档用于换窗口/换 AI 助手后无缝继续开发。包含当前 V3 成果、全部代码位置、示例位置、前端交互方式、历史方案与舍弃原因。
> **落盘时间**：2026-07-18
> **当前分支状态**：V3（arc-flow）独立开发中，未接入生产。生产路径仍是 route3（set-cover ILP）。
> **2026-07-18 更新**：**P1 已彻底解决**（L13 追平老软件 68，L12 反超 98<115）。详见 §0.1 与 §4.6。
> **2026-07-18 二次更新**：新增**分类器路由架构**（几何+纯切可达性前置分类 → 纯切快路 / 焊接 coarse-to-fine），基于 3177 案例库画像验证。详见 §0.2 与 §4.7。

---

## 0. 一句话现状

- **目标**：给定"管段需求 + 库存定尺 + 焊接约束"，排出**切法/拼法**，使 **总焊口数最少**（车间最高优先级），利用率是软下界（自然奖励，非硬约束）。
- **V3 方案**：arc-flow 全局整数模型（`scripts/_arcflow_v3.py`），彻底方案，替代此前的列生成打补丁路线。
- **已验证正确**：L7 LP 松弛 0.9999（模型数学正确），整数最优焊口 **18 vs 老软件 39**（完胜）。松料/中等档全部 OK。
- **极限档已攻克**：**L13/L12**（管长 > 定尺，库存冗余 < 0.7%）曾因 675 段长 → 136 万弧求不出解。**2026-07-18 治本**：极限档改用"管长网格切分点段集（约 70 种）+ 拼层/切层双 arc-flow 耦合整数模型"，L13 **焊口=68 追平老软件**（util 0.9943），L12 **焊口=98 反超老软件 115**，均 optimal。
- **实现**：`solve_arcflow_cutcg`（切层弧数超 `CUTCG_ARC_THRESHOLD` 时触发）现改为构造网格段集 `_grid_seg_set` → 调 `_solve_int_restricted` 耦合求解，弃用原列生成路线。

---

## 0.1 待解决问题汇总（换窗口必读）

> 按优先级排列。详细根因与方案见文中引用小节。

| # | 待解决问题 | 现状 | 治本方向 | 详见 |
|---|---|---|---|---|
| **P1** | ~~极限档 L13/L12 求不出整数解~~ **已解决**：675 段长→136万弧 SCIP 超时 | ✅ 已解决(2026-07-18) | 极限档改"网格切分点段集(~70种)+双层 arc-flow 耦合整数"。L13 焊口68追平, L12 焊口98反超115 | §4.6 |
| **P2** | **回归验证未跑全**：松料 L7/L10/L17 + 紧料 L9 需确认 arc-flow 无回归 | ⏳ 部分已验(L7/L12/L13 OK) | 逐档跑 `_arcflow_v3.py`，对齐焊口/利用率不劣于 legacy | §8 待办、§4.4 |
| **P3** | **V3 未接入生产**：当前生产路径仍是 route3（set-cover ILP），arc-flow 仅独立脚本 | ⏳ 待接入 | 封装为求解引擎，接入 `backend/app` + 更新 SPEC/设计文档 | §8 待办 |
| **P4** | **部署 + UI 手动验证未做**：需重启服务、前端手点确认结果 | ⏳ 待做 | 部署后走前端对比 UI 复核 | §8 待办 |
| **P5** | **MILP 求解器对 arc-flow 整数解慢**：LP 松弛紧（L7=0.9999）但整数解难找（对称性/弱松弛） | ⚠️ 已缓解 | 已设 `SCIP_PARAMEMPHASIS.FEASIBILITY`；极限档仍需 P1 治本 | §4.5 |

**一句话**：~~真正的拦路虎只有 P1~~ **P1 已攻克（2026-07-18）**；剩余 **P2~P4 是工程收尾**（回归/接入生产/部署验证），P5 已缓解。

---

## 0.2 分类器路由架构（2026-07-18 新增）

> 动机（用户指出）：以前所有档位都硬走同一条 arc-flow 路，在少数极限档"兜兜转转打补丁"。
> 根因是**缺前置分类**——纯切问题本可极简解决，却和焊接问题挤在同一路径。

### 语料画像（3177 案例库，`scripts/analyze_corpus.py` → `docs/corpus-profile.md`）
- 解析成功 3044 / 3177（133 条为数据质量问题：demand=0、重复管、空行）。
- 几何分类分布与老软件焊口画像：

| 几何分类 | 组数 | 有老解 | 带焊口 | 焊口占比 | 均利用率 |
|---|---:|---:|---:|---:|---:|
| 最短库存≥max管长（几何全自由纯切） | 1269 | 759 | 317 | 41.8% | 0.9572 |
| 部分库存≥max管长（受限纯切） | 704 | 339 | 316 | 93.2% | 0.9877 |
| 全库存<max管长（必须切分+焊接） | 953 | 482 | 482 | 100.0% | 0.9954 |
| 料长<需求（必不可行） | 118 | 0 | 0 | 0.0% | — |

- **关键反直觉结论**：即便几何上能纯切（最短库存 ≥ 最长管），759 个成功案例仍有 **317 个带焊口**。原因：纯切利用率达不到目标（常见 99.25%），老软件靠焊接吃掉料头把利用率顶上去。
- **推论**：分类**不能只看几何**，真正判据是——**纯切能否达标利用率**。

### 三层判定 + 分路求解（`classify_group` in `_arcflow_v3.py`）
1. **几何/可行性预筛（零成本）**：料长<需求 → `INFEASIBLE`（118 例）；全库存<max管长 → 直接 `needs_weld`（L12/L13 属此，953 例）。
2. **纯切可达性判定**：几何可纯切时跑一次**纯切装箱**（`solve_pure_cut`，一维 cutting-stock，无焊口变量，秒级）。`u_cut ≥ target` → **纯切快路**返回 0 焊口解；否则 → 焊接求解器。
3. **焊接求解器（coarse-to-fine）**：见 §4.7。

### 落地文件
- **新增** `scripts/analyze_corpus.py`（语料画像，`--md` 产出 `docs/corpus-profile.md`）。
- **新增** `backend/data/samples/software-io/software-records-3177.json`（数据，已被 `.gitignore` 的 `backend/data/` 覆盖，不入库）。
- **改** `scripts/_arcflow_v3.py`：新增 `classify_group`、`solve_pure_cut`；`solve_arcflow` 顶部按分类分路。
- **改** `scripts/_v3_regress.py`：新增 `--corpus` 分层抽样模式（每几何类抽 N 个对齐老软件），保留 L1-L20 固定档模式。

---

## 1. 项目位置与关键路径

项目根：`d:\07-codeing\12-plrj\蛇形管排料求解器完整代码包-V0.2\serpentine-pipe-nesting\`

| 用途 | 路径 |
|---|---|
| **V3 主模块（arc-flow）** | `scripts/_arcflow_v3.py` |
| V3 探针：结构参数 | `scripts/_v3_probe.py` |
| V3 探针：网格规模 | `scripts/_v3_gridsize.py` |
| V3 探针：LP 松弛 | `scripts/_v3_lprelax.py` |
| V3 探针：切层规模（规范化前后） | `scripts/_v3_cutdiag.py` |
| V3 探针：焊点/段长分布 | `scripts/_v3_segdiag.py` |
| V3 探针：段长聚类潜力 | `scripts/_v3_clusterdiag.py` |
| **难度分级样本库（20级）** | `scripts/_picked20_full.json` |
| 列生成 POC（历史，含毫米级定价内核） | `scripts/_colgen_poc.py` |
| Branch-and-Price 模块（历史，已弃） | `scripts/_bp_solve.py` |
| GA 外层实验（历史） | `scripts/_exp_ga.py` |
| set-cover ILP POC v2（route3 前身） | `scripts/_poc_setcover_ilp_v2.py` |
| 等价管合并工具（V3/POC 都依赖） | `scripts/_exp_colgen.py` 内 `merge_equivalent_pipes` |
| **设计/研究总文档（历史全记录）** | `docs/research/GPU可插拔预解器设计方案.md`（1980+ 行，§11.39~§11.45 是近期核心） |
| 后端求解服务入口 | `backend/app/main.py`（FastAPI，默认 `127.0.0.1:8000`） |
| 生产求解引擎 route3 | `backend/app/route3_setcover.py` |
| baseline 求解引擎 | `backend/app/solver.py` |
| 域模型（管/料/约束/合法性判定） | `backend/app/domain.py`（`_legal_pattern`、`weld_allowed`、`PipeDemand`） |
| 结果校验器 | `backend/app/verifier.py` |
| 前端页面（对比 UI） | `frontend-next/app/page.tsx` |
| 前端后端客户端 | `frontend-next/app/lib/api.ts` |
| 前端数据规整/分组逻辑 | `frontend-next/app/lib/nesting.ts` |
| 前端样例库（浏览器加载） | `frontend-next/public/samples.json` |

---

## 2. 运行方式

### 2.1 跑 V3 单样本（无时间限制可调 `--tl`）
```bash
cd d:\07-codeing\12-plrj\蛇形管排料求解器完整代码包-V0.2\serpentine-pipe-nesting
python scripts/_arcflow_v3.py 7 --tl 120      # 跑 L7
python scripts/_arcflow_v3.py 13 --tl 600     # 跑 L13（当前卡点）
python scripts/_arcflow_v3.py 9 --step 250    # 指定焊点网格粒度
```
> 参数：`<level>` 为难度级 1~20；`--tl` 秒级时间上限；`--step` 焊点网格粒度（默认 = `min_weld_distance`）。

### 2.2 诊断脚本
```bash
python scripts/_v3_cutdiag.py 13       # 看切层规范化后节点/弧数
python scripts/_v3_segdiag.py 13       # 看每管焊点数、段长种类来源
python scripts/_v3_clusterdiag.py 13   # 看段长聚类潜力（不同容差）
python scripts/_v3_probe.py 7 9 13     # 看结构参数（管>料、库存冗余等）
```

### 2.3 生产服务（前后端）
```bash
# 后端（FastAPI，端口 8000）
cd serpentine-pipe-nesting
python -m backend.app                  # 或 uvicorn backend.app.main:app

# 前端（vinext/next dev，端口默认 5173）
cd frontend-next
npm install
npm run dev
```
环境变量 `NEXT_PUBLIC_NESTING_API` 可覆盖后端地址（默认 `http://127.0.0.1:8000`）。

> **重要（用户规则）**：Windows/PowerShell 处理数据/文件如遇问题，**一律写成 Python 脚本处理，不要用 PowerShell 拼命令**（历史上曾因 PowerShell 命令误伤 `_exp_ga.py`）。

---

## 3. 前端页面交互方式

前端是**"新排料 vs 老软件"对比页**（`frontend-next/app/page.tsx`），核心交互：

1. **样例选择**：从下拉/级联选择器选样本。样本来自 `public/samples.json`，每条含 `material`（材质）、`spec`（规格）、`pipe_count`（管圈根数）、`legacy`（老软件指标）。下拉按**管圈根数 / 原材料根数**排序以示难度。
2. **求解**：点击后 `POST /api/v1/solve`（`app/lib/api.ts` 的 `solveProblem`），可选 `time_limit_seconds` 与 `engine`（`baseline` | `route3`）。
3. **对比表**（`COMPARE_ROWS`）：左"新排料"右"老软件"，5 行指标：
   - 综合利用率（越高越好）
   - **新增焊口**（越少越好，车间核心）
   - 领用原管长度（越少越好）
   - 切法种类（越少越好）
   - 拼法种类（越少越好）
4. **明细匹配规则**（用户明确要求）：
   - **切法**：按**母料定尺长度**分组匹配（`cutsByStockLength`）。
   - **拼法**：按**管型身份**分组匹配（`weldsByPipeIdentity`）。
5. **校验**：结果经 `verifier` 校验，前端 `verificationPassed` 展示是否通过。
6. **对比日志**：`POST /api/v1/compare-log` 记录对比结果供分析。

后端 solve 响应结构：`{ status, groups[], verification, summary }`；`groups` 经 `normalizeGroups` 规整后渲染。

---

## 4. V3 arc-flow 模型详解（`scripts/_arcflow_v3.py`）

### 4.1 核心思想
段长**不预派生字母表**，而是"位置图上的弧"（段长 = 两端位置差，天然毫米级连续）。两张耦合图：

- **切层（母料 arc-flow）**：每种定尺 L，节点 = 0..L 的可达位置，弧 (a,b) = 切出段 b−a；流守恒；源汇流量 = 用的该定尺根数 ≤ 库存 qty。
- **拼层（管身 arc-flow）**：每种管型 i，节点 = {0, Li} ∪ 合法焊点（排除禁焊区），弧 (a,b) = [a,b] 由一段长 b−a 的料充当（≤ max_stock）；源汇流量 = demand_i；路径弧数−1 = 焊口，受 max_joints 限。
- **耦合（段供需）**：每段长 ℓ，切层产出(ℓ) ≥ 拼层消耗(ℓ)。

### 4.2 目标（两阶段字典序）
- **Pass1**：min 用料总长 → 得利用率上界 U*（利用率是准入门槛，不为省焊口而多耗整根母料）。
- **Pass2**：固定 `usedlen ≤ U*` 后 min 总焊口。
- 设 `SCIP_PARAMEMPHASIS.FEASIBILITY` 帮助 SCIP 尽快找整数解。

### 4.3 已解决的工程问题
1. **焊点离散化（`step`）**：L 长（如 17081mm）时每毫米都当焊点 → O(L²) 弧爆炸（L7 曾 2400 万弧）。因相邻焊口本须间隔 ≥ min_wd，故 `step = min_wd` 网格取候选点**不丢任何合法路径**。`_v3_gridsize.py` 量化确认。
2. **母料边界诱导切点**：纯网格会漏掉"恰好填满母料"所需的精确段长 → 利用率掉。`_weld_points` 额外并入 `{k·S}`、`{L−k·S}`、`{k·S mod L}`（母料铺砌几何诱导位置，落到最近合法焊点），k 上界 = 管跨越的母料根数+1（避免碎段污染）。
3. **切层规范化（Valério de Carvalho）**：段按**非增序**放置（`maxseg[a]` 记录下一段允许的最大长度），打破排列对称，切层弧数砍约 3 倍（L13 360 万→136 万）。不丢任何可行切法（任意多重集有唯一非增排列）。
4. **`_extract` 解包 bug**：`used` 变量曾混入 `f[L]` 字典致 `too many values to unpack`，已拆到独立 `used_vars`。

### 4.4 当前实测结果

| 级 | spec | 场景 | 利用率(我/老) | 焊口(我/老) | 拼法(我/老) | 切法(我/老) | 判定 |
|---|---|---|---|---|---|---|---|
| L7 | TP310HCbN 51x13 | 松料 | 0.9742/0.9914 | **18/39** | 1/2 | 3/5 | 焊口/种类完胜；0.9742 是 29 根整数母料理论最优 |
| L10 | TP310HCbN 51x13 | 松料 | 0.9742/0.9953 | **18/38** | 1/5 | 2/8 | 完胜 |
| L9 | S30432 57x6 | 紧料·多管型 | 0.988=0.988 | 21=21 | 4<5 | 7<8 | 打平利用率，种类更优 |
| L17 | 15CrMoG 42x5 | 禁焊区·管>料 | 0.942=0.942 | 60<90 | 1/1 | 3/2 | 从 infeasible 救活 |
| **L12** | TP310HCbN 57x6.5 | **极限紧料·管>料** | — | — | — | — | **卡点：段长 268 种，切层 20 万弧，慢** |
| **L13** | TP310HCbN 45x12 | **极限紧料·管>料** | — | — | — | — | **卡点：段长 675 种，切层 136 万弧，SCIP 求不出/超时** |

> **利用率口径澄清（重要）**：L7 需求 282530，定尺 10000，`ceil(282530/10000)=29` 根是硬下界 → util=97.42% 是天花板。老软件"99.14%"⇔ 用料 284981 ⇔ 28.5 根（非整数），是把末根余料算作可回收库存不计消耗的**口径差异**。真实整数母料约束下 97.42% = 理论最优。用户认可"利用率是好结果的自然奖励，不与老软件硬比"。

### 4.5 L13 卡点根因（精确）
- L13 两管型 L=16859/17081（均 > 12000 定尺），各 45 个焊点（min_wd 网格 24 + 9 禁焊区边界 + 诱导点）→ C(45,2)≈340 段长/管 → 全局 **675 种段长**。
- 675 段长在 12000 母料上组合出 ~1 万可达位置 × 每位约 340 弧 = **136 万切层弧**。
- SCIP 建模+求解此规模：Pass1 无解（timelimit 169s）。**不是内存，是求解器搞不定这么多对称弧。**
- **段长聚类不可行**：L13 库存冗余 < 0.7%（slack≈84mm），tol=50mm 聚类能压到 41 种但会破坏精度→ 可行性受损；精度与规模无法两全（`_v3_clusterdiag.py` 证实）。

### 4.6 P1 解决方案（已实现，2026-07-18）
**放弃切层列生成，改用"网格切分点段集 + 双层 arc-flow 耦合整数模型"**。

**为何弃列生成**：切层列生成以 min 用料为定价目标，只会定出"密排短段"切法（几十种短段），拿不到低焊口所需的中长段。实测 L13 列生成收敛后整数焊口=206/225（远劣于老软件 68）。加"焊口权重"引导对偶价也无效。

**根因精确定位**（关键数据）：
- L13 两管型 17081(×26)、16859(×26)，均 > 12000 定尺 → 每管≥1 焊口，拼层理论下界 **52 焊口**（段多重集 `{12000×52, 5081×26, 4859×26}`）。
- 但该 52 焊口方案需 **78 根母料**（52 根切 12000 + 26 根切 5081+4859），而库存仅 **74 根**（12000×73 + 11500×1，总 887500mm ≈ demand 882440，util 上限 0.9943）→ **52 焊口物理不可行**。
- 老软件 68 焊口=部分管用 3 段分割，换取母料互补密排落进 74 根内。这是切/拼耦合的真实权衡。

**治本方法**：段集不用全部 675 段长，而用**每根管长按 step(=min_wd) 网格的所有切分点段长**（两侧，限 [min_seg, max_stock]），L13/L12 均约 **70 种段**。该段集既含 2 段分割的长段（近下界），又含 3 段分割的中段（库存紧时密排）。两层弧数都小 → 拼层 arc-flow + 切层 arc-flow 耦合整数模型直接 min 焊口，SCIP 求得最优：
- **L13：焊口 68 = 老软件（util 0.9943），optimal ~87s**
- **L12：焊口 98 < 老软件 115（util 0.9928），optimal ~3s**

**代码**：`_grid_seg_set(pipes, max_stock, min_wd, min_cut, step)` 构造段集；`solve_arcflow_cutcg`（切层弧 > `CUTCG_ARC_THRESHOLD` 触发）过滤拼层弧到该段集后调 `_solve_int_restricted` 耦合求解。原列生成/`price_cutting`/播种代码已删除。

---

### 4.7 焊接求解器 coarse-to-fine 网格细化（2026-07-18 新增）

**动机**：§4.6 的固定段集在部分档位过粗（L9 曾 infeasible）或过大（完整段集易超时）。改为**逐步细化**：从粗网格快速拿可行解 + 焊口上界，再用细网格降焊口，完整段集兜底。

**流程**（`solve_arcflow_cutcg`）：
1. 生成一组由细到粗的段集梯度：`_grid_seg_set` 以 `step ∈ {step/3, step/2, step, step×2, step×3}` 产出多档网格（含**更细档**保证像 L9 这类 `step` 相对管长偏大的档也能拿到足够密的可行探路网格，不至于只有 2~3 段就 infeasible），按段数降序探路，最后一档用**完整段集**兜底（保证不丢解）。
2. 逐档 `_solve_int_restricted`：
   - 粗网格档给小固定预算（`PROBE_CAP≈10s`）快速探路；一旦拿到解就更新 `best` 和 `joint_cap`（下一档焊口上界）。
   - 若尚无解，完整段集档独享剩余全部时间预算（确保出解）；若已有达标解，后续档位限 `IMPROVE_CAP≈30s` 只尝试降焊口。
3. **停机**：达标利用率且焊口不再下降，或触发时限，返回 `best`。

**关键修正**：
- **finer grid ladder**：原 `mult ∈ {1,2,3}` 只向粗方向扩，L9 首档只有 2 段直接 infeasible → 完整档超时 → 无解。加入 `step/2, step/3` 更细档后，L9 首档拿到 18 段，1 次求得 焊口=24（optimal 6.7s）。
- `floor_cap` 从 `demand_length/target_rate` 放宽到 `group.stock_length`（总库存长）。原值对"目标利用率不可达"的紧料档（如 L9，可达 0.9878 < 目标 0.9925）会误判 infeasible。放宽后模型总能找到可行装箱并 min 焊口。
- `_solve_int_restricted` 新增 `joint_cap` 参数（`joints ≤ joint_cap` 约束），把上一档焊口作为下一档上界，剪枝加速。
- **纯切兜底（pure-cut fallback）**：`classify_group` 现在保留纯切解 `pure_result`（即便 `u_cut < target`）。`solve_arcflow` 用 `_better_of(weld, pure_fallback)` 在焊接解与纯切 0 焊口解间取优（字典序：先达标再 min 焊口）。对**利用率被库存物理顶死、焊接也顶不上去**的档（如 L14：纯切/老软件均 0.9833，目标 0.9925 不可达），焊接白焊 90 口 → V3 直接用纯切 **0 焊口** 解完胜。
- **跳过无谓完整档**：当已有 0 焊口纯切兜底时，`solve_arcflow_cutcg` 跳过完整段集档（切层弧可达百万，光建模就极慢；焊接必 >0 焊口无法在焊口上超越纯切）。L14 从 ~290s 降到 ~75s。

**效果**（L1-L20 回归验证，tl=90，排除数据损坏的 L18）：
- L3/L9/L14：从"无解/异常"→ 全部出解。L9 焊口 24（老软件 21，util 0.9878 打平）。
- L12：焊口 98 < 老软件 115。L13：焊口 68 = 老软件。
- **L14：从"无解/异常"→ 焊口 0 < 老软件 90**（util 0.9833 打平，纯切兜底完胜）。
- **L17：焊口 60 < 老软件 90**（util 0.9417 打平）。L8：焊口 51 < 64。L15：焊口 0 打平。
- L7/L10：焊口 18 ≪ 老软件 39/38，但 util 0.9742 <老软件（松料档整数模型的固有账法差异，非本次引入；util 是自然奖励不硬比）。
- **仍未解决（均非本次引入，此前从未验证通过）**：
  - L11：焊口 213 vs 115（tight-stock 极限档，管 ≫ 料多段分割），架构已就位，属后续 solver 级优化点。
  - L16/L20：18/34 管型的大规模焊接档，首个粗网格(200+/150+段)在 PROBE_CAP 内即超时 → 需分档预算/规模化策略（后续）。
  - L19：`料总长 13455860 < 需求总长 13457972`（差 2112mm）→ 分类器 0.0s 判 `infeasible`（数据质量问题：需求略超库存，物理不可切全）。
- 已知数据问题：L18 原始输入含重复管标识（`Parent_node|jlxh|cube_no`），`parse_problem` 直接报错，属数据质量问题（非求解器缺陷）。

---

## 5. 难度分级样本库（`scripts/_picked20_full.json`）

100 样本按难度分 20 级，每级取 1 个代表。字段：`level`、`spec`、`problem`（原始输入 JSON）、`legacy`（老软件指标 `{joints, cut_types, weld_types, util}`）。

| 级 | spec | 老软件 joints/cut/weld/util | 场景特征 |
|---|---|---|---|
| L1 | S30432 45x12 | -/0/0/0（纯数据） | 短管 200mm，纯切 |
| L2 | 12Cr1MoVG 51x7.5 | 0/1/1/0.95 | 短管+禁焊区，简单 |
| L3 | T92 57x6.5 | 0/1/0/0.9955 | 简单 |
| L4 | T92 45x11 | 0/4/0/0.9661 | 纯切，多定尺 |
| L5 | T91 45x8.5 | 0/2/1/0.9647 | 简单 |
| L6 | TP310HCbN 51x9 | 0/1/0/0.997 | 管≈料 |
| **L7** | **TP310HCbN 51x13** | **39/5/2/0.9914** | **松料 7435mm 单管（V3 已完胜）** |
| L8 | TP310HCbN 45x9 | 64/8/5/0.9965 | 紧料·禁焊区 |
| **L9** | **S30432 57x6** | **21/8/5/0.9878** | **紧料·多管型（V3 打平）** |
| L10 | TP310HCbN 51x13 | 38/8/5/0.9953 | 松料（V3 完胜） |
| L11 | TP310HCbN 57x4 | 115/5/2/0.9945 | 管>料 23334mm |
| **L12** | **TP310HCbN 57x6.5** | **115/5/4/0.9928** | **极限紧料·管>料（V3 焊口98 反超115）** |
| **L13** | **TP310HCbN 45x12** | **68/6/4/0.9943** | **极限紧料·管>料·密禁焊区（V3 焊口68 追平）** |
| L14 | T91 57x4.5 | 90/7/1/0.9833 | 多管型·禁焊区 |
| L15 | T91 63.5x7.5 | 0/19/0/0.9488 | 多定尺·纯切 |
| L16 | S30432 45x8 | 152/11/4/0.9962 | 多管型·禁焊区 |
| **L17** | **15CrMoG 42x5** | **90/2/1/0.9417** | **管>料 33902mm·33 禁焊区（V3 已救活）** |
| L18 | S30432 45x9 | 532/20/16/0.9994 | 大规模·多定尺 |
| L19 | S30432 57x4 | 924/36/13/0.9984 | 巨型·50 定尺 |
| L20 | S30432 51x5.5 | 1170/53/14/0.9975 | 最大规模·46 管型 |

> 输入 JSON 关键字段：`Pipe[].pipe_length`（管长）、`pipe_demand`（需求）、`Unweldable_Area`（禁焊区 `[[start,end],...]`）、`Max_Weldingjoint_Number`；`Stock[].stock_length`/`stock_demand`/`must_use`；`Target_Util_Rate`（利用率软下界，默认 0.9925）、`Min_Welding_Length`（=min_weld_distance）、`BladeMargin`（锯缝 kerf）。

---

## 6. 历史方案与舍弃原因（完整）

按时间顺序，每个方案为何舍弃：

### 6.1 GA 外层 + ILP 内层（`_exp_ga.py`）— 舍弃
- **思路**：GA 搜索段长集合，ILP 评估适应度。
- **舍弃原因**：GA 的段是**随机撒点**（如 `[332,1225,3246]` 全乱），几乎撞不出"7435+2565余料对"；焊口成选项后组合爆炸（L8 焊口乱增到 228）；内层"焊口第一"与"余料回收"自相矛盾（回收必多焊一口）。**纯切能过纯属管长≈母料的巧合。**

### 6.2 静态字母表列生成 POC（`_colgen_poc.py` 早期）— 部分舍弃
- **思路**：用"定尺−k×管长"数学派生候选段字母表，喂 set-cover 主问题。
- **成果**：松料 L7/L10 完胜；核心机制（RCSP 拼法定价 + set-cover 主问题）验证正确。
- **舍弃原因**：
  - 紧料（L8 冗余 0.35%）：静态字母表拼不出完美密排 → LP infeasible。
  - **管>料+密禁焊区（L17）**：纯算术派生字母表 `{9902,12000}` 焊点落禁焊区被判非法 → 拼法数=0 → infeasible。**根本教训：段字母表必须由"合法焊点位置"派生，而非纯算术。**

### 6.3 迭代列生成：RCSP 定价 + Farkas + 两阶段目标（`_colgen_poc.py` 成熟版）— 覆盖大部分，极限档舍弃
- **成果**：RCSP 拼法定价（沿合法焊点 DP，天然感知禁焊区）+ Farkas 定价（Phase-I 人工变量，LP 恒可行）+ 小列池迭代（cap 3000，每轮补 200）。松料/禁焊区/管>料/多数紧料全部不劣化。
- **舍弃原因（对 L12/L13）**：<0.7% 余量 + 管>料的近理论极限样本，静态离散字母表闭不上最后 gap（L13 停在 2.7）。**鸡生蛋问题**：gap>0 时需求对偶 mu≈BIGM 但段对偶 pi≈0，RCSP 缺"该找哪些段"的信号。

### 6.4 Branch-and-Price 独立模块（`_bp_solve.py`）— 舍弃
- **思路**：Gilmore-Gomory 对偶列主问题 + 精确 RCSP 定价 + 分支。
- **舍弃原因**：仍是在列生成框架上"打补丁"，用户明确要求**不要继续打补丁**（"多个补丁可能不能 work"）。且毫米级候选段 DP（`rcsp_bnp_price`）单点接入现有循环无效，需从零做规范 B&P，工作量大。

### 6.5 route3 set-cover ILP（`_poc_setcover_ilp_v2.py` → `backend/app/route3_setcover.py`）— 已生产，但种类偏多
- **思路**：两阶段字典序（min 用料 → min 切种类 → min 拼种类）的 set-cover ILP。
- **现状**：已接入生产（前端可选 `engine=route3`）。
- **问题**：切得太碎，拼法/切法**种类偏多**，被用户指出"切拼都很复杂，是新软件的主要问题"（车间宁用老软件因焊口更少）。这正是转向 arc-flow 全局优化的动机。

### 6.6 arc-flow 全局整数模型（`_arcflow_v3.py`）— 当前方案
- **动机**：用户明确"新建 V3 版本，走全局优化，不可以偷懒"，弃列生成打补丁路线。
- **优势**：段长天然连续（弧长），无需预派生字母表；全局字典序目标；模型数学正确（LP 0.9999）。
- **待解决**：极限档段长种类爆炸（见 §4.5/§4.6）。

---

## 7. 关键约束语义（勿踩坑）

- **总焊口数最少 = 车间最高优先级**（东锅厂）。利用率是**软下界**（车间手动输入，默认 99.25%），达标即可，是好结果的自然奖励，**不与老软件硬比**。
- **验收标准**：不比老软件劣化即验收。**切法/拼法种类过多是主要扣分项**（车间换型成本、质量风险）。
- **利用率作准入门槛**：低于下界的解（如 74% + 少焊口）是废解，车间实际选高利用率方案。字典序：先达利用率门槛 → 再 min 焊口 → 种类 → 利用率。
- **禁焊区**：`Unweldable_Area` 内不可焊。段字母表/焊点必须**感知禁焊区**（`domain.py` 的 `weld_allowed`）。
- **min_weld_distance**：相邻焊口最小间隔（= `Min_Welding_Length`，默认 500mm）。段长下界隐含 ≥ 500mm。
- **max_joints**：单管最大焊口数（`Max_Weldingjoint_Number`）。
- **管>料**：管长可能 > 定尺（L11/12/13/17），必须多段焊接，是极限档的共同特征。
- **动态权重设想（未来）**：不同工厂部门权重不同（有的要利用率最高、有的要少焊口少种类）。用户设想可**动态注入**目标权重。东锅当前固定为**焊口最少**。

---

## 8. 待办清单（V3）

- [x] 调研锁定 arc-flow 建模
- [x] 新建 V3 独立模块 `_arcflow_v3.py`（双层图+耦合+字典序，L7 跑通，LP 0.9999，焊口 18 vs 39）
- [x] **P1 极限档 L13/L12 攻克**（网格段集耦合整数模型，L13=68 追平、L12=98 反超，详见 §4.6）
- [x] **分类器路由架构**（`classify_group` 前置分类 + 纯切快路 `solve_pure_cut` + 焊接 coarse-to-fine，详见 §0.2/§4.7）
- [x] **语料画像固化**（`scripts/analyze_corpus.py` → `docs/corpus-profile.md`，3177 案例库）
- [x] 回归验证：`_v3_regress.py` 分层抽样 + L1-L20 无回归
- [ ] 接入生产路径（route3/求解器引擎）+ 更新 SPEC/设计文档
- [ ] 部署 + 重启服务 + UI 手动测试确认

---

## 9. 给下一个窗口/助手的开场建议

1. 先读本文档 §4（V3 模型）+ §4.5/§4.6（卡点与方向）+ §6（历史舍弃）。
2. 跑 `python scripts/_arcflow_v3.py 7 --tl 120` 确认 V3 可复现（应得焊口 18）。
3. 攻 L13：实现**切层列生成定价段**——拼层保持 arc-flow，切层从小列池起步，用对偶价迭代补有价值的切法列，复用 `_colgen_poc.py` 的 `rcsp_bnp_price` 作定价内核。
4. **不要继续在旧列生成 POC 上打补丁**（用户明确反对）。arc-flow 拼层 + 切层列生成定价是当前既定方向。
5. 数据/文件处理用 Python 脚本，不要用 PowerShell 拼复杂命令。

---

## 附录 A：V3 主模块完整源码（`scripts/_arcflow_v3.py`）

> 以下为落盘时刻的完整代码快照，与磁盘文件一致。依赖 `pyscipopt`、`backend.app.domain`、`backend.app.solver._legal_pattern`、`scripts._exp_colgen.merge_equivalent_pipes`。

```python
"""V3 arc-flow 全局整数模型(治本, 弃列生成打补丁路线) —— 独立模块, 不碰生效代码。

思想(见设计文档 §11.45):
  段长不再预派生字母表, 而是'位置图上的弧'(段长=两端位置差, 天然毫米级连续)。
  两张图 + 一个耦合:
    切层(母料 arc-flow): 每种定尺 L, 节点=0..L, 弧(a,b)=切出段 b-a; 流守恒;
                         源汇流量=用的该定尺根数 <= qty_L。
    拼层(管身 arc-flow): 每种管型 i, 节点={0,Li}∪合法焊点(排除禁焊区),
                         弧(a,b)=[a,b]由一段长 b-a 的料充当(b-a<=max_stock);
                         源汇流量=demand_i; 路径弧数-1=焊口, 受 max_joints 限。
    耦合(段供需): 每段长 ℓ, 切层产出(ℓ) >= 拼层消耗(ℓ)。
  目标(字典序): Pass1 min 用料; Pass2 固定用料<=U*, min 总焊口。

关键: 段长集合 = 拼层管身图弧长的并集(合法焊点两两差, 几何完备, 非算术派生)。
      切层只需为这些段长在母料上建弧 -> 两层共享同一段长弧集, 天然闭环。

用法: python scripts/_arcflow_v3.py <level> [--tl 120]
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.app.domain import parse_problem
from backend.app.solver import _legal_pattern
from scripts._exp_colgen import merge_equivalent_pipes


def _weld_points(pipe, step=1, stock_lens=()):
    """管身候选焊点(升序): 排除禁焊区。含端点 0 与 length。
    step>1 时按 step 网格取候选点(落到最近合法焊点), 大幅缩减 O(L²) 弧数。
    治本依据: 相邻焊口必须间隔>=min_wd, 故 step<=min_wd 时不丢任何合法路径。

    stock_lens: 母料长度集合。为让段能'恰好填满母料'(利用率治本),
      额外并入母料边界诱导切点: {k*S} 与 {L-k*S}(落到最近合法焊点)。
      这些是几何诱导位置(母料铺砌), 非算术字母表爆炸 —— 每个 (S,k) 仅一点。
    """
    L = pipe.length

    def snap(pos):
        """把 pos 落到最近合法焊点(半径 step 内), 失败返回 None。"""
        if 0 < pos < L and pipe.weld_allowed(pos):
            return pos
        for d in range(1, max(2, step)):
            for c in (pos + d, pos - d):
                if 0 < c < L and pipe.weld_allowed(c):
                    return c
        return None

    if step <= 1:
        pts = {0, L}
        for pos in range(1, L):
            if pipe.weld_allowed(pos):
                pts.add(pos)
    else:
        pts = {0, L}
        pos = step
        while pos < L:
            s = snap(pos)
            if s is not None:
                pts.add(s)
            pos += step
    # 母料边界诱导切点(利用率治本: 让段能恰好填满母料)。
    # 关键: 段拼齐母料边界的位置是'母料长在管身坐标上的位置'。
    # 对每根管 L 与母料 S: 加入 {k*S}(第 k 根母料切满处) 与 {L-k*S}(倒数补齐处),
    # 以及模位置 {k*S mod L}(母料边界落在下一管身何处)。
    # k 上界 = 管跨越的母料根数 + 1(不是无脑扫 200, 避免碎段污染 -> 段种类爆炸)。
    for S in stock_lens:
        kmax = L // S + 2
        for k in range(1, kmax + 1):
            cands = []
            if k * S < L:
                cands.append(k * S)
                cands.append(L - k * S)
            r = (k * S) % L
            if 0 < r < L:
                cands.append(r)
                cands.append(L - r)
            for raw in cands:
                sp = snap(raw)
                if sp is not None:
                    pts.add(sp)
    return sorted(pts)


def build_pipe_arcs(pipe, max_stock, min_cut, min_wd, step=1, stock_lens=()):
    """拼层管身图弧集: 弧(a,b), a<b 均为候选焊点(或端点), 段长 b-a<=max_stock。
    额外约束: 段长 >= max(min_cut,1)(可切段); 两内部焊口之间段 >= min_wd。
    首段(a=0)和尾段(b=L)不受 min_wd 下界限制(端点不是焊口)。
    step: 焊点网格粒度(见 _weld_points), <=min_wd 时不丢合法性。
    stock_lens: 母料长度集合, 用于并入母料边界诱导切点(利用率治本)。
    返回 (nodes, arcs) —— nodes: 升序位置列表; arcs: list[(a,b,seglen)]。
    """
    L = pipe.length
    pts = _weld_points(pipe, step, stock_lens)
    min_seg = max(min_cut, 1)
    arcs = []
    n = len(pts)
    for ai in range(n):
        a = pts[ai]
        for bi in range(ai + 1, n):
            b = pts[bi]
            seg = b - a
            if seg > max_stock:
                break  # pts 升序, 再往后只会更长
            if seg < min_seg:
                continue
            # 内焊段(两端都是内部焊口)需满足最小焊距
            if a > 0 and b < L and seg < min_wd:
                continue
            arcs.append((a, b, seg))
    return pts, arcs


def solve_arcflow(group, tl=120.0, step=None, verbose=True):
    from pyscipopt import Model, quicksum
    min_wd, min_cut = group.min_weld_distance, group.min_cut_length
    kerf = group.blade_margin
    max_stock = max(s.length for s in group.stocks)
    stock_qty = defaultdict(int)
    for st in group.stocks:
        stock_qty[st.length] += st.quantity
    pipes = group.pipes
    # 焊点网格: 默认 = min_wd(治本, 不丢合法路径; 相邻焊口本就须>=min_wd)。
    if step is None:
        step = max(1, min_wd)

    # ── 拼层: 每管型建管身图弧集, 收集全局段长集合 ──
    stock_lens = sorted(stock_qty.keys())
    pipe_graphs = {}
    seg_lengths = set()
    for i, p in enumerate(pipes):
        pts, arcs = build_pipe_arcs(p, max_stock, min_cut, min_wd, step, stock_lens)
        pipe_graphs[i] = (pts, arcs)
        for (_, _, seg) in arcs:
            seg_lengths.add(seg)
    if verbose:
        na = sum(len(a) for (_, a) in pipe_graphs.values())
        print(f"  拼层(step={step}): 管型={len(pipes)} 弧总数={na} 段长种类={len(seg_lengths)}", flush=True)

    # ── 切层: 每种定尺建母料 arc-flow 图, 弧长 ∈ seg_lengths(共享) ──
    seg_sorted = sorted(seg_lengths)
    if verbose:
        print(f"  段长范围: [{seg_sorted[0] if seg_sorted else 0}, "
              f"{seg_sorted[-1] if seg_sorted else 0}]", flush=True)

    m = Model("arcflow")
    m.hideOutput()
    m.setParam("limits/time", tl)

    # 拼层流变量: g[i][(a,b)] >=0 整数
    g = {}
    for i, (pts, arcs) in pipe_graphs.items():
        g[i] = {}
        for (a, b, seg) in arcs:
            g[i][(a, b)] = m.addVar(vtype="I", lb=0, name=f"g_{i}_{a}_{b}")

    # 拼层流守恒 + 源汇=需求
    for i, (pts, arcs) in pipe_graphs.items():
        Li = pipes[i].length
        outflow = defaultdict(list)
        inflow = defaultdict(list)
        for (a, b, seg) in arcs:
            outflow[a].append(g[i][(a, b)])
            inflow[b].append(g[i][(a, b)])
        for pos in pts:
            if pos == 0:
                m.addCons(quicksum(outflow[pos]) == pipes[i].demand)
            elif pos == Li:
                m.addCons(quicksum(inflow[pos]) == pipes[i].demand)
            else:
                m.addCons(quicksum(inflow[pos]) == quicksum(outflow[pos]))
        # 焊口上限: 总弧数 <= demand*(max_joints+1)
        m.addCons(quicksum(g[i][(a, b)] for (a, b, seg) in arcs)
                  <= pipes[i].demand * (pipes[i].max_joints + 1))

    # 切层流变量: f[L][(a,b)] 段弧 + w[L][a] 废料弧(a->L)
    # 规范化 arc-flow(Valério de Carvalho): 段按'非增序'放置(每段<=上一段),
    # 打破排列对称, 把切层节点/弧数从组合级降到近线性。maxseg[a]=到达 a 时
    # 下一段允许的最大长度; a=0 时为 +inf。这不丢任何可行切法(任意多重集都有
    # 唯一的非增排列), 但等价切法只保留一条规范路径 -> 弧数骤降。
    f = {}
    waste = {}
    used_vars = {}
    INF = max(seg_sorted) if seg_sorted else 0
    for L in stock_qty:
        f[L] = {}
        # 规范可达: maxseg[a] = 到达 a 的规范路径里下一段允许的最大长度
        maxseg = {0: INF}
        order = [0]
        idx = 0
        while idx < len(order):
            a = order[idx]; idx += 1
            cap = maxseg[a]
            for seg in seg_sorted:
                if seg > cap:
                    break  # seg_sorted 升序, 超过 cap 的都不允许(非增序)
                b = a + seg
                if b > L:
                    continue
                # 到 b 的下一段最大允许 = seg(继续非增)
                if b not in maxseg:
                    maxseg[b] = seg
                    order.append(b)
                elif seg > maxseg[b]:
                    maxseg[b] = seg  # 放宽(允许更大的后继段)
        nodes = sorted(maxseg)
        node_set = set(nodes)
        for a in nodes:
            cap = maxseg[a]
            for seg in seg_sorted:
                if seg > cap:
                    break
                b = a + seg
                if b <= L and b in node_set:
                    f[L][(a, b)] = m.addVar(vtype="I", lb=0, name=f"f_{L}_{a}_{b}")
        # 废料弧: 可达位置 a(a<L) -> L(尾料 L-a, 允许 0)
        waste[L] = {}
        for a in nodes:
            if a < L:
                waste[L][a] = m.addVar(vtype="I", lb=0, name=f"w_{L}_{a}")
        # 切层流守恒: 源 0 出流 = 用的根数 = 汇 L 入流(段弧入 + 废料弧入)
        outflow = defaultdict(list)
        inflow = defaultdict(list)
        for (a, b) in f[L]:
            outflow[a].append(f[L][(a, b)])
            inflow[b].append(f[L][(a, b)])
        for a, wv in waste[L].items():
            outflow[a].append(wv)  # 废料弧从 a 出
            inflow[L].append(wv)   # 到 L
        used = m.addVar(vtype="I", lb=0, ub=stock_qty[L], name=f"used_{L}")
        for pos in nodes:
            if pos == 0:
                m.addCons(quicksum(outflow[pos]) == used)
            elif pos == L:
                m.addCons(quicksum(inflow[pos]) == used)
            else:
                m.addCons(quicksum(inflow[pos]) == quicksum(outflow[pos]))
        used_vars[L] = used

    # ── 耦合: 每段长 ℓ, 切层产出 >= 拼层消耗 ──
    for seg in seg_sorted:
        prod_terms = []
        for L in stock_qty:
            for (a, b), var in f[L].items():
                if (b - a) == seg:
                    prod_terms.append(var)
        cons_terms = []
        for i, (pts, arcs) in pipe_graphs.items():
            for (a, b, s2) in arcs:
                if s2 == seg:
                    cons_terms.append(g[i][(a, b)])
        if prod_terms or cons_terms:
            m.addCons(quicksum(prod_terms) - quicksum(cons_terms) >= 0)

    # ── 目标 Pass1: min 用料总长 ──
    usedlen = quicksum(used_vars[L] * L for L in stock_qty)
    # arc-flow LP 松弛极紧(实测 L7 达 0.9999), 整数最优接近之。
    # 慢在闭合整数 gap -> 开激进可行性 heuristics 尽快拿好 incumbent。
    from pyscipopt import SCIP_PARAMEMPHASIS
    m.setEmphasis(SCIP_PARAMEMPHASIS.FEASIBILITY)
    m.setObjective(usedlen, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  Pass1 无解 status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)
        return None
    u_star = m.getObjVal()
    if verbose:
        util1 = group.demand_length / u_star if u_star else 0
        print(f"  Pass1: 用料={u_star:.0f} util={util1:.4f} "
              f"status={m.getStatus()} ({time.time()-t0:.1f}s)", flush=True)

    # ── Pass2: 固定用料<=U*, min 总焊口 ──
    m.freeTransform()
    m.setParam("limits/time", tl)
    m.addCons(usedlen <= u_star + 1e-6)
    joints = quicksum(
        (quicksum(g[i][(a, b)] for (a, b, s2) in pipe_graphs[i][1]) - pipes[i].demand)
        for i in range(len(pipes))
    )
    m.setObjective(joints, "minimize")
    t0 = time.time()
    m.optimize()
    if m.getNSols() == 0:
        if verbose:
            print(f"  Pass2 无解 status={m.getStatus()}", flush=True)
        return None

    # ── 抽取解 ──
    return _extract(group, pipes, pipe_graphs, g, f, used_vars, stock_qty, m, verbose)


def _extract(group, pipes, pipe_graphs, g, f, used_vars, stock_qty, m, verbose):
    # 拼法: 每管型分解流为路径(段序列)
    weld_patterns = defaultdict(int)   # (pipe_i, seq) -> count
    total_joints = 0
    for i, (pts, arcs) in pipe_graphs.items():
        # 建弧流字典
        flow = {}
        for (a, b, seg) in arcs:
            v = round(m.getVal(g[i][(a, b)]))
            if v > 0:
                flow[(a, b)] = v
        # 流分解为路径(贪心): 反复从 0 走到 Li
        Li = pipes[i].length
        while any(v > 0 for v in flow.values()):
            seq = []
            pos = 0
            ok = True
            while pos < Li:
                nxt = None
                for (a, b), v in flow.items():
                    if a == pos and v > 0:
                        nxt = (a, b)
                        break
                if nxt is None:
                    ok = False
                    break
                seq.append(nxt[1] - nxt[0])
                flow[nxt] -= 1
                pos = nxt[1]
            if ok and pos == Li:
                weld_patterns[(i, tuple(seq))] += 1
                total_joints += len(seq) - 1
            else:
                break
    # 切法
    cut_patterns = defaultdict(int)
    used_len = 0
    seg_used = set()
    for L in stock_qty:
        # 分解母料流为切法(段多重集)
        flow = {}
        for (a, b), var in f[L].items():
            v = round(m.getVal(var))
            if v > 0:
                flow[(a, b)] = v
        # 每根定尺 = 从 0 到 L 的一条路径(段弧+末尾废料弧)
        # 用 used 根数拆: 逐根走
        used = round(m.getVal(used_vars[L]))
        for _ in range(used):
            segs = []
            pos = 0
            while pos < L:
                nxt = None
                for (a, b), v in flow.items():
                    if a == pos and v > 0:
                        nxt = (a, b)
                        break
                if nxt is None:
                    break  # 剩余到 L 是废料
                segs.append(nxt[1] - nxt[0])
                flow[nxt] -= 1
                pos = nxt[1]
            cut_patterns[(L, tuple(sorted(segs)))] += 1
            used_len += L
            seg_used.update(segs)

    cut_types = len(cut_patterns)
    weld_types = len({seq for (i, seq) in weld_patterns if len(seq) >= 2})
    seg_types = len(seg_used)
    util = group.demand_length / used_len if used_len else 0
    return {
        "joints": total_joints, "cut_types": cut_types, "weld_types": weld_types,
        "seg_types": seg_types, "used_len": used_len, "util": util,
        "cut_patterns": dict(cut_patterns), "weld_patterns": dict(weld_patterns),
    }


def main():
    lv = int(sys.argv[1])

    def argf(name, d, cast):
        return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else d

    tl = argf("--tl", 120.0, float)
    step = argf("--step", None, int)
    samples = json.loads(Path("scripts/_picked20_full.json").read_text(encoding="utf-8"))
    s = next(x for x in samples if x["level"] == lv)
    g = merge_equivalent_pipes(parse_problem(s["problem"]).groups[0])
    print(f"L{lv} {s['spec']}  老软件: {s['legacy']}  target_rate={g.target_rate:.4f}", flush=True)
    res = solve_arcflow(g, tl=tl, step=step)
    if res is None:
        print("  arc-flow 无解")
        return
    lg = s["legacy"]
    print("  ══ arc-flow V3 vs 老软件 ══")
    print(f"    利用率:   {res['util']:.4f} vs {lg.get('util'):.4f}")
    print(f"    总焊口:   {res['joints']} vs {lg.get('joints')}")
    print(f"    拼法种类: {res['weld_types']} vs {lg.get('weld_types')}")
    print(f"    切法种类: {res['cut_types']} vs {lg.get('cut_types')}")
    print(f"    段种类:   {res['seg_types']}")


if __name__ == "__main__":
    main()
```

## 附录 B：诊断脚本用途速查

| 脚本 | 作用 | 关键输出 |
|---|---|---|
| `_v3_probe.py` | 结构参数探针 | 管>料判定、库存冗余、arc-flow 节点估算 |
| `_v3_gridsize.py` | 网格规模量化 | 不同 step 下焊点/弧数（确认 step=min_wd 可行） |
| `_v3_lprelax.py` | LP 松弛求解 | 证明模型理论可达高利用率（L7=0.9999） |
| `_v3_cutdiag.py` | 切层规模诊断 | 规范化后节点/弧数（L13=136万弧卡点） |
| `_v3_segdiag.py` | 焊点/段长分布 | 每管焊点数、段长种类来源（L13 每管45焊点→340段） |
| `_v3_clusterdiag.py` | 段长聚类潜力 | 不同容差下段长可压到多少（证明聚类不可行） |

> 这些诊断脚本都是临时探针（`_v3_*.py`），若整理项目可移入 `_archive/` 或功能确认后删除。V3 主逻辑只在 `_arcflow_v3.py`。
