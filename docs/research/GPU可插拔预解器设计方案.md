# GPU 可插拔预解器设计方案

> 目标：在**有 GPU 的环境用 GPU 加速、没 GPU 自动退回纯 CPU** 的前提下，
> 拉高"超紧供料比(1.00~1.02)"硬组的解出率，且**绝不引入可行性风险、绝不破坏确定性、绝不让请求崩溃**。
>
> 状态：设计稿（待用户过目后实现）。配套导读见 `花样最少化文献导读.md`。

---

## 一、定位：GPU 加速"候选生成"，不是"求解"

### 血泪教训（来自导读 §三点五 实验B/C）

- **启发式暖启动是错误的拐杖**：group 1 启发式报"排不出"，但 SCIP 能解。
  用一个比 MILP 更弱的启发式去 warm-start MILP，会引入 MILP 本没有的可行性缺陷。
- 因此 GPU 预解器的定位**不是**"先用 GPU 求可行解再喂 SCIP"（被否掉的老路）。

### 正确定位

**GPU 做"海量候选列(weld/cut patterns)的并行生成 + 打分筛选"；SCIP 仍是唯一的精确求解与可行性裁判。**

- GPU 产物 = 更多、更优质的**候选列**，追加进候选池。
- 有 GPU：候选池又大又准 → SCIP 在紧料组更易找到可行解。
- 无 GPU / GPU 异常：退回现有 CPU 候选生成器，SCIP 照跑，结果只是候选少一点。
- **求解语义、验证器完全不变**：GPU 产的候选照样过独立验证器这一关。

---

## 二、接入点（已核对现有代码）

现有分级放松阶梯（`solver.py`）：

```
_solve_group_scip
  └─ for tier in TIER_ORDER:            # STANDARD_ONLY → STANDARD_WIDE → FULL
       weld_candidates = _tiered_weld_candidates(group, tier)   ← 【GPU 插入点】
       _solve_group_scip_with_candidates(group, weld_candidates, ...)
          └─ _generate_cut_candidates(group, weld_candidates, ...)  # 切法列由拼法列段长派生
```

**唯一插入点：`_tiered_weld_candidates` 的返回值**。
GPU 生成的拼法候选（weld candidates）**追加**到该 tier 已有列表末尾；
切法列由拼法列的段长自动派生，无需单独改 `_generate_cut_candidates`。

这样改动面最小、风险最低：GPU 只影响"候选池大小与质量"，不碰求解/验证/切法派生。

---

## 三、可插拔架构

### 3.1 抽象接口

```python
# backend/app/accel/base.py
class CandidateProvider(Protocol):
    name: str
    def augment_weld_candidates(
        self,
        group: MaterialGroup,
        tier: str,
        existing: list[_WeldCandidate],
        *,
        deadline: float | None,
        cap: int,
    ) -> list[_WeldCandidate]:
        """返回【追加】的拼法候选（不含 existing、已去重）。失败必须抛异常或返回 []。"""
```

实现：
- `CpuNoopProvider`：返回 `[]`（现状即基线，GPU 关闭时用它，零行为变化）。
- `GpuCandidateProvider`：CuPy 向量化生成 + 打分，`import cupy` 失败即不可用。

### 3.2 运行时选择（环境变量，配置硬化）

```
NESTING_ACCEL = auto | gpu | cpu     # 默认 auto
  auto : 探测到 CUDA 且 cupy 可导入 → gpu，否则 cpu
  gpu  : 强制 gpu；不可用则记 warning 后退回 cpu（绝不失败）
  cpu  : 强制 cpu（国企无卡环境的显式开关）
```

探测只做一次并缓存（`functools.lru_cache`）：
```python
def _select_provider() -> CandidateProvider:
    mode = os.getenv("NESTING_ACCEL", "auto").lower()
    if mode == "cpu":
        return CpuNoopProvider()
    try:
        import cupy  # noqa
        if cupy.cuda.runtime.getDeviceCount() > 0:
            return GpuCandidateProvider()
    except Exception as exc:
        if mode == "gpu":
            log.warning("NESTING_ACCEL=gpu requested but unavailable: %s", exc)
    return CpuNoopProvider()
```

### 3.3 调用侧（`_tiered_weld_candidates` 末尾追加）

```python
result.extend(_standard_weld_candidates(group, seen, per_pipe_cap=per_pipe_cap))

provider = _select_provider()
try:
    extra = provider.augment_weld_candidates(
        group, tier, result, deadline=deadline, cap=GPU_WELD_CAP[tier])
    result.extend(extra)          # 只增不减
except Exception as exc:          # GPU 任何异常都不能拖垮请求
    log.warning("accel provider %s failed, falling back: %s", provider.name, exc)
return result
```

---

## 四、GPU 具体并行算什么

紧料组的难点：段守恒把切法列×拼法列乘在一起 → 组合爆炸。GPU 适合"评估海量组合"：

| 任务 | 并行粒度 | 产出 |
|---|---|---|
| **并行"就近合法切分"** | 每根成品管 × 每组标准段长组合 → 向量化 `_snap_split` | weld 候选 |
| **并行合法性过滤** | 批量校验 `Min_Cut_Length`/最小焊距/禁焊区/`Max_Joints` | 剔除非法组合 |
| **并行打分排序** | 对上万候选算 waste / 段种复用度 → 规约排序取 top-K | 筛出精品喂 SCIP |
| **（进阶，二期）** GPU 批量局部搜索/遗传演化找紧料可行装箱 | 种群并行演化 | 额外保底列 |

一期只做前三行（安全、解语义不变）。

---

## 五、三条硬红线（不可妥协）

1. **只增不减候选**
   GPU 候选是**追加**到 CPU/标准段候选池，永不替换。
   → GPU 再差也不会误杀 g1 那种"只有 baseline 列能解"的组（导读 §三点七教训）。

2. **确定性：相同输入相同输出**
   GPU 浮点/并行归约顺序会破坏确定性。对策：
   - 候选生成**全程整数毫米运算**（与现有 `LENGTH_SCALE` 一致），不用浮点。
   - 追加前对 GPU 候选做**确定性排序**（按 `(段数, 段长元组)`）再去重，与 CPU 路径同一 key。
   - 最终喂 SCIP 的候选顺序在 GPU/CPU 下逐字节一致 → 求解结果一致。

3. **降级安全：绝不让请求失败**
   - `import cupy` 失败、无 CUDA 设备、CUDA OOM、任何 kernel 异常 → 捕获、记 warning、退回 CPU 基线。
   - GPU 路径永远是"锦上添花"，从不成为必需依赖。
   - `requirements.txt` **不硬性依赖 cupy**；用 `requirements-gpu.txt` 可选安装。

---

## 六、依赖与打包

```
requirements.txt          # 现状，纯 CPU，不含 cupy
requirements-gpu.txt      # 可选：cupy-cudaXX（按 CUDA 版本），numpy 已在主依赖
```

部署指南补充一节"启用 GPU 加速（可选）"：安装 `requirements-gpu.txt`、设 `NESTING_ACCEL=auto`、验证 `cupy.cuda.runtime.getDeviceCount()`。

---

## 七、验收标准

在真实数据的紧料组子集（供料比 1.00~1.02，含 g0）上对比：

| 指标 | CPU 基线 | GPU 目标 |
|---|---|---|
| 硬组解出率 | 当前值（基准） | **提升**（至少救活若干 INCONCLUSIVE 组，如 g0） |
| 相同输入相同输出 | ✅ | ✅（GPU/CPU 候选逐字节一致 → 结果一致） |
| 已解组不退化 | — | ✅ 不误杀（g1 等仍 OPTIMAL、焊口不增） |
| 独立验证器 | 全绿 | 全绿（GPU 产物同样过验证） |
| 无 GPU 环境 | 正常 | 正常（自动退回，行为=基线） |

**通过门槛**：确定性 + 不误杀 + 验证全绿是硬门槛；解出率提升是价值门槛。
任何一条硬门槛不满足则 GPU 路径默认关闭（`NESTING_ACCEL=cpu`），不上线。

---

## 八、分阶段落地

- **阶段0（设计稿）**：接口/降级/确定性/验收对齐。✅
- **阶段1**：`CandidateProvider` 接口 + `CpuNoopProvider` + 降级框架 + 调用侧接入。
  跑通纯 CPU 路径，确认行为=现状、全部测试绿。✅ `backend/app/accel/{base,registry}.py`
- **阶段2**：`GpuCandidateProvider`（CuPy 向量化候选生成 + best-waste(K) 过滤 + 精确合法 snap）。
  写成 `import cupy as xp` / 降级 `import numpy as xp` 的一份代码，NumPy 下可单元测试。✅ `backend/app/accel/gpu.py`
- **阶段3**：紧料组 benchmark 对比解出率/耗时/确定性，据结果决定是否上二期局部搜索。⏳

### `NESTING_ACCEL` 取值（实装）

| 值 | 行为 |
|---|---|
| `auto`（默认） | 探测到 CUDA+CuPy → GPU（CuPy 后端）；否则纯 CPU 基线 |
| `gpu` | 强制 GPU；不可用则记 warning 退回 CPU |
| `cpu` | 强制纯 CPU 基线（无卡站点显式开关） |
| `gpu-cpu` | 强制 GPU provider 走 **NumPy 后端**——用于在无卡机上单测/benchmark 生成逻辑（与 CuPy 路径同代码，仅数组后端不同） |

GPU 依赖走可选文件 `backend/requirements-gpu.txt`（`cupy-cudaXX`），主依赖只含 `numpy`。

---

## 九、风险与对策

| 风险 | 对策 |
|---|---|
| GPU 浮点破坏确定性 | 全整数运算 + 追加前确定性排序 |
| CUDA 版本/驱动碎片化（国企环境） | cupy 可选依赖 + auto 探测 + 异常降级 |
| GPU 候选质量差反拖慢 SCIP | 打分筛选只留 top-K；受 `GPU_WELD_CAP[tier]` 封顶 |
| 一份代码 GPU/CPU 分叉 | `xp = cupy or numpy` 统一入口，CI 在 CPU 上跑 NumPy 分支测试 |
| 二期局部搜索退化成"暖启动老路" | 严守"只产候选、不产最终解"边界，SCIP 仍是唯一裁判 |

---

## 十、实施前 GitHub / 文献调研（2026-07 补充）

动手前检索了 GitHub 与学术界"GPU 加速一维下料/排样"的现成工程，结论：
**没有可直接复用的开源工程，但方向被 2026 年最新论文完整验证，且有可借鉴的工程细节。**

### 10.1 检索到的相关工程

| 项目 | 是否可复用 | 说明 |
|---|---|---|
| **Guerriero & Saccomanno, GAPG (ICORES 2026)** | ⭐ 思路/算法可借鉴 | 与我们方案**几乎同构**：GPU 并行生成 pattern 池 + CPU 解受限 set-covering。见 §10.2 |
| `miladbarooni/opencg` | ❌ 非 GPU | C++ 多线程并行 pricing（labeling），是 CPU 并行不是 GPU；架构可参考 |
| `GoSmarter-ai/cuopt-for-metals` | ❌ 依赖闭源 | 用 NVIDIA cuOpt（闭源商业库）解 1D 下料，绑 Azure，不符合"可插拔+开源+国企可离线部署" |
| `fontanf/columngenerationsolverpy` | ❌ 纯 CPU | 列生成框架（含 LDS），纯 Python CPU，可作 CPU 侧列生成参考 |
| `cuGenOpt` (arXiv 2026) | ⚠️ 过重 | GPU 通用元启发式框架（遗传/演化），"one block evolves one solution"，属我们**二期局部搜索**的可选后端，一期不用 |
| Gurobi/Pyomo/MO-book 例子 | ❌ 教学 | 标准 CPU 列生成教学代码，无 GPU |

**核心结论**：GPU 加速 1D-CSP 领域**没有成熟开源轮子**（cuOpt 闭源、GAPG 未开源）。
自研是合理的；但不必从零摸索——GAPG 论文已把工程要点趟明。

### 10.2 GAPG 论文（ICORES 2026）对我们的直接借鉴

这篇论文做的正是我们设计的事，且实测在难例（Falkenauer triplets、1000 件）上**亚秒级达最优**。可借鉴点：

1. **两阶段"生成-选择"范式与我们完全一致**：
   GPU 只做"并行生成高质量 pattern 池"（**不用对偶价、不做迭代 pricing**），
   CPU 用 MIP 解受限 set-covering 选组合。→ **印证我们"GPU 产候选、SCIP 当裁判"的边界是对的。**

2. **确定性树扩展（Deterministic Tree Expansion）**：
   每个"根件"分配一个并行单元，广度优先扩展"加入/跳过"两分支生成所有合法 pattern。
   → 正是我们 `_snap_split`/背包式切分的向量化并行版，可作 GPU kernel 蓝本。

3. **"Best Waste" 过滤（K=2）**：相同 waste 值只保留 top-K 个 pattern（论文取 K=2），
   避免 pattern 爆炸压垮 MP。→ **直接采纳**为我们打分筛选的具体策略（我们已有 waste/段种复用度打分，叠加"同 waste 保 top-K"）。

4. **只保留 maximal patterns**（无法再加任何件的）：过滤掉被支配的碎 pattern。
   → 与我们"只增有价值候选"一致，可加入筛选步。

5. **工程细节（若走原生 kernel）**：SoA 内存布局、atomic 计数器管理写指针、warp 同步减分支发散。
   → 一期用 CuPy 向量化**不需要**这些底层技巧；若二期上 Numba kernel 再参考。

### 10.3 对本设计的校正

- **一期确认走 CuPy 向量化"生成-select"**（不做迭代 pricing），与 GAPG 的 Phase 1 同构但更轻。
- **打分筛选加入"同 waste 保 top-K(K=2)"+ 只留 maximal** 两条来自 GAPG 的具体规则，写进 §四打分逻辑。
- **二期局部搜索**若要做，`cuGenOpt` 的"one block one solution"演化架构是可选后端，但严守"只产候选"边界。
- 结论不变：**自研，一期 CuPy candgen，借鉴 GAPG 的 best-waste/maximal 过滤，不引第三方 GPU 求解库。**

---

## 十一、阶段3 实测结论 + 论文对照（2026-07-14 真机 GPU 验证）

真机环境：RTX 4060 Ti / CUDA 12.6 驱动。装 `cupy-cuda12x[ctk]`（自带 CUDA 12.9，
绕开系统 CUDA 11.8），`auto` 自动选中 `gpu-cupy`，GPU 真实计算验证通过。

### 11.1 实测结果（30 组真 GPU A/B，seed 7，10s 限时，三态度量）

| 口径 | 数 | 说明 |
|---|---|---|
| 救活（无解→有解）| 0 | 极紧供料/大规模组仍未解 |
| 升级（超时凑合→真解）| 1 | `12Cr1MoVG/57x4.5` 某实例：超时→OPTIMAL |
| **回退（质量变差）** | **1** | 同规格另一实例：OPTIMAL→超时凑合 |
| 不变 | 28 | — |

**净效约为 0，且存在回退风险。** 深挖回退根因：**不是**生成耗时（加了 3s 生成 deadline 回退依旧），
而是**追加的候选列把 SCIP 的模型变大、分支定界变慢**——一个 baseline 7.2s 干净 OPTIMAL 的组，
加列后 9.9s 只剩超时凑合。

### 11.2 与 GAPG 论文的关键架构差异（回退的根本原因）

读 GAPG 全文后确认：**我们的"append 追加"范式与论文的"generate-and-select"范式本质不同**：

| 维度 | GAPG 论文 | 我们当前实现 | 后果 |
|---|---|---|---|
| 池的来源 | GPU 生成**全部**池，无 CPU 池 | GPU 列**追加**到 CPU 已有池 | 我们的模型被撑大 |
| MIP 模型 | 纯 set-covering，变量=pattern**多重度** `xⱼ∈Z≥0`（几十个变量）| 逐棒指派 + 词典序 + 焊位/切口/禁焊区 | 我们模型本就复杂，加列更慢 |
| 目标 | 单目标 min bins | 词典序（料→焊口→拼法种类→切法种类）| 论文"CPU 选择trivial"的前提不成立 |
| 池规模控制 | maximal + **Best-Waste K=2**，池极小 → 亚秒解 | 追加数百列 | 无 GAPG 的"极小池"纪律 |
| GPU 实现 | Numba CUDA 原生树扩展 | CuPy 数组向量化 | 我们更弱 |

**核心洞见**：GAPG 之所以"加候选=更快更优"，是因为**池就是整个模型且被 K=2 压到极小**；
它从不"追加到一个已很复杂的 MILP 上"。我们把 GPU 列追加进逐棒词典序 MILP，
等于给一个已经很难的搜索又加变量——**这正是论文明确回避的做法**。

### 11.3 已固化的扎实成果（与价值假设无关，均保留）

- 可插拔框架：`CandidateProvider` 协议 + `CpuNoopProvider` + `GpuCandidateProvider`。
- 安全降级：`NESTING_ACCEL=auto/gpu/cpu/gpu-cpu`；无卡/无 CuPy/驱动异常 → 自动退 CPU，永不崩。
- 确定性：GPU 列经校验、去重、确定序；`gpu-cpu` 强制 NumPy 后端复现。
- 生成 deadline 预算（`NESTING_ACCEL_BUDGET_S`，默认 3s），生成绝不吃求解预算。
- 全部 66 测试通过（含真 CuPy 路径），append-only 红线 0 回退（就候选合法性而言）。

### 11.4 论文驱动的三条候选路线（待决策）

1. **两档回退护栏**：baseline 先解，再用 accel 列解一次，取更优 → **保证零回退**，代价：难组耗时翻倍。
   （最稳，但违背 GAPG"一次成型"的效率哲学。）
2. **改造为 GAPG 式 generate-and-select 子路径**：对**极紧/大规模**组，另建一个"GPU 全量生成 pattern +
   受限 set-covering（pattern 多重度变量）"的**独立轻模型**，与现逐棒 MILP 并行择优。
   → 最贴论文，但要新建一套简化模型，工程量最大。
3. **接受现状**：固化可插拔框架（默认关、可手开），差异化生成不再投入，回到求解器其他红线。

> 记录人备注：路线2 是论文正解，但需评估"简化 set-covering 模型能否表达词典序目标与工艺红线"；
> 若不能表达，则它只能作"可行性探路"（回到被否的 warm-start 老路）。这一点需先想清楚再动手。

### 11.5 路线2 可行性 demo 认证结论（2026-07-14，已证伪）

按用户要求"先做 demo 认证路线2 确实不可行再换方案"，实测 20 组真实数据（seed 7, blade=0）：

| 类别 | 组数 | 说明 |
|---|---|---|
| **结构不可表达（含焊接管）** | **16 / 20** | 单层 set-covering 无法建模 |
| 无焊接管组（单层等价）| 3 / 20 | 每根管=单段，单层与两层等价，均秒解 |
| both-fail | 1 / 20 | 本就物理不可行 |

**证伪的根本原因（两echelon 结构）**：本问题是**真·两层耦合**——

- **切割层**：一根 12000mm 母材 → 切成多根**不同管**的段（母材被多管共享）。
- **焊接层**：一根管（`max_joints≥1`）= 跨**多根母材**的段焊接而成。

GAPG 的单层 set-covering 面向 BPP（一个 bin 装多个 item，单层）。套到蛇形管有两种取向，**都不成立**：

- **取向A（pattern=一根管由整母材拼）**：丢失"母材共享"——2m 短管独占 12m 母材，短管瞬间耗尽母材 → 全 INFEASIBLE（实测 cols 仅 1~4）。
- **取向B（pattern=一根母材的切割方案，覆盖段需求）**：母材共享 OK，但**焊接管的段分散在不同母材上**，"哪些段焊成哪根管 / 焊口数 / 按管计需求"在单层里**无法表达**，除非重新引入 `produced==consumed` 耦合——那就退回现有两层模型。

**实测印证**：20 组里 **16 组含焊接管**（占 80%），单层直接判定"结构不可表达"；仅 3 组"纯单段管"单层与两层等价且都秒解（无增益）。

**最终结论（阶段性，见 §11.6 修正）**：
- 我最初写的路线2 两种取向（管←整母材 / 母材←段）**都错**，被 demo 证伪（16/20 含焊接管无法表达）。
- **但这不代表"单层不可能"**——错在我的 pattern 定义，不在"单层"本身。见 §11.6 的文献修正。

### 11.6 对口文献修正：焊接下料 = 等价标准 CSP（2026-07-14 重调研）

被 GAPG（单echelon BPP）证伪后，按用户要求"重新调研对口文献"，锁定**焊接/两阶段下料**专题，
关键命中 3 篇（均已本地下载）：

1. **Ilyés & Balogh, "The Effect of Welding on the 1D-CSP" (2019, Hindawi)** ——**结构完全对口**：
   切母材 + 把短段焊成长管，最小化焊口。**核心定理**：

   > **带焊接的下料 可归约为 一个"等价标准 CSP"**：把最多 `α` 根母材**首尾焊接**拼成一根
   > **"等价母材"**（长度=各母材之和，代价=`(α-1)` 个焊口），然后**当成单层标准 CSP 求解**，
   > 最后把等价切割方案**转换回**切-焊方案（三步法：①造等价母材 ②解标准CSP ③模式转换）。

   - 这正是我路线2 demo **缺的那一步**：单层的"bin"不是"一根母材"，而是**"最多 (max_joints+1) 根母材焊成的等价母材"**。
     一旦这样定义，**它就是单层 CSP，GAPG 的 GPU 并行 pattern 生成就适用了**。
   - `α` 上界 = 我们的 `max_joints+1`，天然有界 → 等价母材数量有限。
   - **重要警告（论文 §4.2）**：第③步"模式转换"在极端例可能**失败**（如强制某管 2 焊而上限 1 焊），
     需回退/加母材迭代；且论文的"每管≤2 母材"比我们（`max_joints` 可更大）更严。

2. **《Hybrid Optimization for Rebar Cutting》(2025, MDPI Buildings)**：钢筋接头=我们的焊口，
   含**接头允许区/接头间距/错开**规范（≈我们的 `forbidden` 禁焊区 + `min_weld_distance`），
   用 Markov 优化接头位置。→ 我们禁焊区/最小焊距的处理有对口工程参照。

3. **《Three-Step Model for Rebar Cutting-Stock》(ASCE)**：三步分解、"长整料优先"、
   "每管 1~2 焊占 90%" → 与我们的后处理规则（短管先切、集中长料）一致，可作红线校核基准。

4. **Muter & Sezer, "1D Two-Stage CSP" (2018, EJOR) / "1.5-D TSCSP" (2025, MDPI)**：
   两阶段下料的**列-行同时生成（column-and-row generation）/ 嵌套列生成 / Generate-and-Solve**，
   针对"中间件尺寸未知"的更一般两阶段问题。→ 若走精确列生成，这是方法论蓝本。

### 11.7 修正后的路线全景（待用户决策）

| 路线 | 本质 | 可行性 | 工程量 |
|---|---|---|---|
| ~~路线2-旧~~ | 我的错误单层（管←整母材/母材←段）| **已证伪** | — |
| **路线2-新（等价母材归约）** | Ilyés-Balogh 三步法：α 根母材焊成等价母材→单层CSP→模式转换。GPU 可加速等价母材的 pattern 生成 | **理论可行**，但③模式转换有失败风险、需回退 | 中-大（要实现三步法 + 转换回退）|
| 路线1 | 两档回退护栏 | 可行，零回退 | 小 |
| 路线3 | 固化框架、默认关 | 可行 | 极小 |
| 列生成 | branch-and-price / 列-行生成（Muter-Sezer）| 理论最强 | 最大 |

> 关键判断：**路线2-新 是真正对口的加速范式**（把两echelon 合法归约成单层，GPU 才有用武之地）。
> 但它不是"换掉现有 MILP"，而是**新增一条"等价母材 CSP"子求解路径**，与现有两层 MILP 并行择优；
> 且必须实现论文警告的"③模式转换失败→加母材/回退"兜底。建议先做**等价母材归约的 demo** 认证
> 其在真实数据上的解出率/转换成功率，再决定是否投产。

### 11.8 路线2-新 等价母材归约 demo 实测（2026-07-14）

按用户要求做了 demo（`scripts/_demo_route2_equiv.py`），实现 Ilyés-Balogh 三步法的
①②步：造等价母材（最多 α 根真实母材首尾焊）+ 单层 CSP（词典序 料→焊口）。
真实数据、seed 7、blade=0、time_limit=20s、alpha_cap=40。

**结论：范式成立——能救活 baseline 无解组，且生成可控。**

前 6 组实测：

| 组 | baseline | route2-new | 判定 |
|---|---|---|---|
| [1] 51x13 | FEASIBLE(j=24) | INFEASIBLE(a=30) | 真·紧组，加焊距约束后无可行解 |
| [2] 45x9 | UNSOLVED | INFEASIBLE(a=40) | 同上 |
| [3] 45x11 | OPTIMAL(j=10) | OPTIMAL(j=15) | baseline 焊口更少 |
| [4] 54x13 | TIMELIMIT(j=21) | **OPTIMAL(j=20)** | **救活+略优** |
| [5] 45x9 | UNSOLVED(71.5s) | **OPTIMAL(j=278,4.4s)** | **救活（原本完全无解）** |
| [6] 51x6.5 | UNSOLVED | INFEASIBLE(a=40) | 双失败 |

口径：route2-new 解出 3/6 vs baseline 2/6，**独赢（救活）2 组**，baseline 独赢 1 组（焊口更少），双失败 2 组。

**验证到的三个关键事实：**

1. **α（等价母材=几根真母材焊成）必须与"紧度"挂钩，不是 `max_joints+1`**：
   这是 demo 里发现并修正的核心 bug。α 是"余料池化深度"，与每管焊口上限是**两个独立约束**
   （后者在枚举时按"span 跨越的母材边界数"单独卡）。极紧组（利用率≥97%）需 α≈母材总数才可行：
   [1] 组 α=4/6 均 INFEASIBLE，α=9 才可行 → 已改为**按 demand/stock 紧度自适应 α**。

2. **列数随 α 爆炸，正是 GPU 的用武之地**：α 4→6→9 时列数 43→128→391；不设防时 [2] 组
   飙到 18万列/5.5s。已加三重护栏（`combinations_with_replacement` 惰性生成 + 20000 多重集上限
   + 6s 生成 deadline + 按 parts 签名做 Best-Waste 去重，列上限 8000）后稳定在毫秒~数秒。
   **这块"生成海量 pattern 再筛选"恰是 CuPy/Numba 并行的目标**（对应 §10.2 GAPG 的 Phase 1）。

3. **`min_weld_distance` 必须在枚举时卡"焊接残段≥min_seg"**（demo 已加 `_span_stubs_ok`）：
   否则会产出物理非法的 1mm 焊桩。加了这条后 [1] 从"假可行"变 INFEASIBLE——
   **说明现有两层求解器对该组给出的 FEASIBLE 可能本身踩了焊距红线**，值得单独核查。

**仍未做（三步法第③步 + 生产化前提）：**

- **③模式转换**（等价切割方案 → 真实切-焊工单）demo 未实现，只验证了①②的解出率。
  论文 §4.2 警告此步在极端例可能失败，需"加母材/回退"兜底——投产前必须补这步 + 转换成功率实测。
- **词典序未完全对齐**：demo 只做"料→焊口"两级；生产还有"拼法种类→切法种类"及禁焊区/must_use，
  需评估等价母材模型能否表达（禁焊区尤其可能破坏"母材可任意首尾焊"的前提）。
- **baseline 超时口径**：baseline 内部会突破 time_limit（[5] 跑到 71.5s），
  与 route2-new 的 20s 不是同一预算，正式对比需统一。

**建议**：范式已认证可行且对口。投产前的关键未知是**第③步模式转换的成功率**——
建议下一步要么（a）补齐③步转换 + 兜底，做转换成功率实测；要么（b）先把等价母材 pattern
生成搬到 GPU，验证高 α 下的生成加速比。二者都做完再定是否并入生产求解器。

### 11.9 第③步模式转换实测：成功率 100%（2026-07-14）

按工程推进补齐了论文三步法的**第③步"模式转换"**（等价切割方案 → 真实切-焊工单），
并在 demo 里加了 `_convert_to_workorder`：对每个被选中的等价母材列，**按确定顺序replay具体布局**
（真实母材按长度降序首尾焊、管按枚举时的 `layout` 顺序头尾相接切），逐条工单**重核所有工艺红线**：

- 每管焊口数 ≤ `max_joints`
- 每个**焊接**残段 ≥ `max(min_cut_length, min_weld_distance)`
- **未焊接**整管 ≥ `min_cut_length`（不受 `min_weld_distance` 约束）
- 各规格真实母材消耗 ≤ 库存
- 需求覆盖（≥ demand）

**实测结论（20 组，seed 7，blade=0，20s）：③模式转换成功率 = 11/11 = 100%。**

| 口径 | 数 |
|---|---|
| route2new 解出 | 11/20 |
| **③模式转换成功** | **11/11（100%）** |
| baseline 解出 | 9/20 |
| route2new 独赢（救活）| 5 |
| both-OK | 6（焊口 r2 更少 1 / 相等 4 / 更多 1）|

**关键洞见：论文警告的"③模式转换失败"风险在我们的建模下不出现。**
原因：我们**在枚举阶段就只产出"具体合法布局"的列**（每列带 `layout` 具体切序），
并把该 layout 一路带到转换——即"解出即可转换"，不存在论文那种"解序无关 CSP 事后重构失败"。
这是比论文通用三步法**更强的保证**。因此论文的"加母材/回退兜底"**在本实现下无需**。

**过程中修的两个 bug（均已修正）：**

1. **layout 丢失导致假失败**：初版列只存 `sorted(parts)`，转换 replay 时排序与枚举实际切序
   不一致 → 焊口/残段错位（OVER_JOINTS/SHORT_STUB）。改为列携带 `layout` 具体切序后，
   转换成功率 50%→100%（6 组小样）。
2. **未焊管误用焊距约束**：转换器最初对**所有**管用 `min_weld_distance` 卡残段，
   把 302mm 的**无焊**短管（α=1、j=0）误判 SHORT_STUB。修正为
   "焊距只约束**焊接**残段，未焊整管只受 `min_cut_length`"后，[9][13][20] 均转换 OK。

**至此，Ilyés-Balogh 三步法（①造等价母材 ②单层CSP ③模式转换）在真实数据上全链路认证通过。**

**投产前仍需（工程收尾项）：**
- 词典序对齐生产：demo 只做"料→焊口"两级；生产还有"拼法/切法种类"及**禁焊区/must_use**。
  其中**禁焊区**可能破坏"母材可任意首尾焊"的前提（焊点落在禁区就非法），需在等价母材构造时排除。
- baseline 超时口径统一（baseline 内部会突破 time_limit，正式对比需同预算）。
- 把 demo 的 `_convert_to_workorder` 产出的工单结构对齐生产 `service` 的输出 schema。

**下一步可选**：（a）把等价母材 pattern 生成搬到 GPU（CuPy 并行），验证高 α 下的生成加速比；
（b）处理禁焊区/must_use 对齐生产红线；（c）作为独立子求解路径并入 service，与两层 MILP 并行择优。


## §11.10 路线 A：禁焊区 / must_use 对齐生产红线（已认证）

### 数据结构与语义（backend/app/domain.py）

- **禁焊区 `forbidden`**：`PipeDemand` 的**逐管**属性，来源字段 `Unweldable_Area`，
  归一化为 `tuple[Interval]`。语义：位置**相对该管自身起点** `[0, length]`，
  `weld_allowed(pos)` 要求 `0 < pos < length` 且 `pos` 不落在任何禁区内。
  **同长度管可有不同禁焊区**（真实数据 7/60 组存在），因此不能只按 length 聚合。
- **must_use `must_use_quantity`**：`StockSupply` 逐母材属性，语义"该长度母材至少用 N 根"。

### 真实数据普查（60 组，seed=7）

| 约束 | 命中组数 |
|---|---|
| 含禁焊区 | **43/60 (72%)** |
| 含 must_use | 10/60 (17%) |
| 同长度管有不同禁焊区 | 7/60 (12%) |

**禁焊区极普遍**——此前 demo 忽略它，"100% 转换成功"存在虚高风险，必须补齐。

### 实现（scripts/_demo_route2_equiv.py）

1. **引入"管类型"抽象**：pipe type = `(length, forbidden, max_joints)`，
   demand 按类型累加；`parts`/`layout` 改存**类型索引**而非裸长度。
   这样同长度不同禁区的管被正确区分为不同类型。
2. **枚举阶段卡禁焊点**：`_enumerate_equiv_cutplans` 对每次落管的每个跨界焊点，
   计算其相对管起点位置 `rel = boundary - pipe_start`，若 `rel` 落入该类型任一禁区
   则该布局非法，直接剪枝。同时保留原有 `max_joints` 与 `min_seg` 残段约束。
3. **must_use 进模型**：对每个 `must_use_quantity>0` 的母材加约束
   `Σ bars_of_that_length·x ≥ must_use_quantity`。
4. **转换器二次校验**：`_convert_to_workorder` 复核 `FORBIDDEN_WELD`（焊点落禁区）
   与 `MUST_USE_SHORT`（母材用量不足），确保产出工单合法。

### A/B 实测（30 组含约束，enforce vs ignore）

| 指标 | 结果 |
|---|---|
| 约束导致从可解变 INFEASIBLE | **3/30 (10%)** |
| 两边都解出且焊口增加 | 0（相等 16） |
| strict 侧③转换成功率 | **100%（FORBIDDEN_WELD / MUST_USE_SHORT 全过）** |

**结论**：
- 禁焊区约束**真实生效且非平凡**——10% 的组因启用禁焊区从可解变 INFEASIBLE，
  证明它切实收紧解空间，不是摆设。
- 在两边都能解出的组里**焊口代价为零**：最优解本就避开禁区。
- must_use 组全部正常解出、转换、约束满足。
- 加约束后③步转换仍 **100% 成功**——**等价母材范式能正确承载生产红线（禁焊区+must_use），
  范式未被证伪**。

（注：偶见同一组 free=INFEASIBLE / strict=OPTIMAL 的反常对，系列生成命中 wall-clock
deadline 的随机抖动，非逻辑错误；生产版需去随机化列生成或放宽 deadline。）

### 投产前仍需
- **词典序第三级**（拼法/切法种类）尚未对齐生产；当前 demo 只做料→焊口两级。
- 去随机化 / 稳定化列生成，消除 deadline 抖动带来的解出率波动。


## §11.11 路线 B：GPU 列生成评估（结论：当前不上 GPU）

### 出发点
把 pattern 生成搬到 GPU（CuPy 并行），验证高 α 下的加速比。先做**诚实的瓶颈度量**再动手，避免为不划算的方向投工。

### 度量：真瓶颈不在"生成得不够快"（20 组，alpha=12）

| 环节 | 实测 |
|---|---|
| `_bar_multisets`（母材组合枚举） | **<15ms**（最大 20000 组合仅 14ms）——**根本不是瓶颈** |
| `_enumerate_equiv_cutplans`（DFS 布局枚举） | types 多的组爆炸：9~17 类时单组 **60万~120万列**，6s 截断 |
| MILP 实际消费 | 只吃**去重后 best-per-parts 几千列**，海量原始列 99% 被丢弃 |

**核心发现：瓶颈是"生成了不该生成的列"（算法冗余），不是"生成得不够快"。** 列数 ∝ types 的多重集组合（17 类 × 深度 8 ≈ `C(25,8)≈100 万`），这是**组合本质**，加速运算解决不了组合规模。

### GPU 实测：per-multiset 调度慢几千倍

- 实现了 CuPy/NumPy 向量化单母材枚举器（`scripts/_gpu_patterns.py`），单母材语义与 CPU DFS **逐用例对拍 6/6 全过**（含禁焊区/min_seg/max_joints），逻辑正确。
- 但按"每个等价母材调一次 GPU"的架构：单组 **6.96s（GPU） vs 0.00s（CPU）**，慢几千倍——每次 kernel launch + `.get()` 同步 × 数千 multiset = 灾难。
- 要消除 launch 开销就得"跨所有母材一次性批处理"，但笛卡尔积展开 `17^8≈7e9` 行，8.5GB VRAM 放不下，必然分块回退 CPU。

### 剪枝实验：maximal-only 会拖垮 MILP（已回退）

尝试把"只保留极大装填（无更多管可放）"的 dominance 剪枝下沉进递归，想把 120 万列压到几千。**实测反而更糟**：
- 组 [3]：cols 665→437（列变少），但 MILP 从 **OPTIMAL 0.09s 退化为 TIME_LIMIT_INCOMPLETE 50s+**。
- 原因：极大装填删掉了求解器需要的"廉价灵活列"，把整数规划逼进难搜索区。
- **已回退**，枚举器恢复到验证正确的版本。

### 工程结论（诚实判断）

1. **生成不是生产阻塞点**：即使 6s 截断，route2 已能解 10/20、独赢 5 组。瓶颈已转移到 **MILP 求解 + 覆盖率**。
2. **GPU 前提不成立**：瓶颈是分支密集、前缀依赖剪枝的 DFS + 组合冗余，不是同构大批量运算。
3. **工程成本高**：生产环境 CUDA_PATH 未配置（启动即 warning），上 GPU 等于加一个脆弱硬依赖。
4. **算法冗余 > 运算速度**：真正该做的是"更聪明的列生成/列生成法（column generation）按需产列"，而非把冗余列生成得更快。

**决定：当前不上 GPU。** `_gpu_patterns.py` 保留为"已探索且验证正确、但不划算"的记录。后续若要提升，优先方向是**按需列生成（延迟定价）**而非 GPU 加速。


## 11.12 路线 C：等价母材求解并入生产 service（作独立子求解路径）

### 目标
把 route2（等价母材 CSP）作为**可选的独立求解引擎**接入生产 `_solve_problem` 分发点，与既有两层 MILP 并行择优、回退安全，且结果必须过**真实生产 verifier**。

### 关键难点：两侧 schema 与 segment-balance
生产结果是**双面 schema**——`cutting_patterns`（每根实材如何切成料段）+ `welding_patterns`（料段如何焊成每根管），且 verifier 强制 **segment-balance**（`verifier.py:889`）：焊接消耗的每个料段必须由切割产出等量料段。route2 的等价母材工单需拆成 per-bar 切段 + per-pipe 焊段（跨母材边界的管，其料段分属不同实材），再经 `solver._assemble_group_result` 生成，才能保证 schema/整数毫米/度量口径一致。

### 落地做法（先离线验证，再动生产代码）
1. **扩展 step-3 转换器**记录 per-bar 切段：`bar_segments[bi]` = 第 bi 根实材上被切出的料段（跨边界管的两段分入相邻两根母材）。
2. **schema 适配器** `route2_equiv._build_group_result`：pipe type → 具体 `pipe_index`（按各需求计数分配）→ `_WeldCandidate`；per-bar 切段 → `_CutCandidate`；调 `_assemble_group_result`。
3. **过剩裁剪**：MILP 需求覆盖是 `≥`，可能过产（verifier 要求 per-pipe 精确 `==`）。裁掉**整管（未焊、单段）过剩实例**并把其料段退回母材余料，保持切/焊平衡。
4. **模块化**：核心求解+转换+适配抽成 `backend/app/route2_equiv.py`，`solve_group()` 返回生产组结果或 `None`（引擎不可用/未解出/转换失败），**永不因求解原因抛异常、不改动 group**。
5. **接入分发点** `_solve_problem`：`NESTING_ROUTE2` 开关（默认关），仅当 route2 结果按同一词典序（利用率→焊口）**严格更优**时替换 incumbent；预算受总时限约束。

### 离线验证（route2 解 → 生产 schema → 真 verify_solution）
- seed=7 / seed=21 两批：**22/22 解出组全过真 verifier**（segment-balance、禁焊区、must_use、度量复算全通过），含 384 焊口、99.9% 利用率的组，以及过产被裁剪的组。
- 生产模块 `app.route2_equiv.solve_group` 复现 **10/10 全过**，与内联实现一致。

### 端到端实测（真实 kerf，OFF vs ON）
| 批次 | verify pass | route2 选中 | 改善 | 回退 |
|---|---|---|---|---|
| seed=7, 6 组, tl=6s | off 6/6 → **on 6/6** | 3 | 3 | **0** |
| seed=21, 16 组, tl=8s | off 16/16 → **on 16/16** | 1 | 2 | **0** |

亮点：
- [seed7-5] baseline `INFEASIBLE` → route2 `TARGET_REACHED` util **0.997**（原本解不出的组被救活，且过真 verifier）。
- [seed21-7] util **0.143 → 0.759**（大幅提升）。
- [seed7-3] util 0.912→0.985；[seed7-4] 焊口 21→20。
- **零回退**：ON 从不劣于 OFF（词典序择优 + verify 干净 schema 保证）。

### 一个数据质量坑（已定位，非 route2 bug）
部分归档 DB 样本**不带 BladeMargin 字段**：`parse_problem` 默认 0，而 verifier 默认 10（`verifier.py:86`），双边 kerf 口径不一致会让 **baseline 与 route2 一起** 报 `BLADE_MARGIN_MISMATCH` 等。生产 payload 恒带 BladeMargin，故这是**测试数据问题**，测试里把 BladeMargin 补进 NestParam 后双边一致，route2 即全过。

### 工程结论
- **route2 已作为 opt-in 引擎安全并入生产**（`backend/app/route2_equiv.py` + `_solve_problem` 分发点），默认关闭、零风险，开启后仅择优不回退。
- 定位：**MILP 为主引擎，route2 补强超紧/两层 MILP 解不出的组**——它把这些组从 INFEASIBLE 救到高利用率可交付解。
- 遗留：route2 在超紧组仍有 `INFEASIBLE`（覆盖率受 pooling 深度/列生成限制），这是**求解能力**问题，与本次接入正确性无关；后续优化方向仍是按需列生成。**该遗留已在 §11.13 系统量化并做出取舍**。


## 11.13 路线 A 收口：超紧组 INFEASIBLE 根因量化与取舍

### 背景
§11.12 遗留了"route2 在超紧组仍 INFEASIBLE"的覆盖率问题。本节用真实困难样本系统诊断根因，判断是否值得投入更重的求解算法（列生成 / branch-and-price）。

### 诊断方法
- 从归档 DB（`MOMCALCULATESTATUS==1`）随机抽样，逐组跑 route2，并对 route2 未解的组**逐一与 baseline 两层 MILP 对照**（脚本 `scripts/_diag_route2_coverage.py`）。
- 用**有效的长度+kerf 下界**分类未解组：`demand_length + 强制焊口数×kerf ≤ 总料长` 是可行的**必要条件**。低于该界者 = 证明性不可行（route2 判 INFEASIBLE 正确）；有余量者 = 候选真缺口。

### 关键结论（40 组 seed7 + 100 组 seed11 两批）
| 桶 | 40 组 | 100 组 | 含义 |
|---|---|---|---|
| OK（route2 解出） | 22 | 37 | 正常 |
| UNSOLVED_TIGHT（下界证明基本不可行） | — | 34 | route2 判 INFEASIBLE **正确**，无可修 |
| UNSOLVED_CANDIDATE（下界有余量） | — | 28 | **候选**真缺口，需 baseline 对照定性 |
| baseline 也解不出（真难/不可行） | 16 | — | 非 route2 缺陷 |
| route2 未解但 baseline 能解（真缺口） | **1** | 见下 | 唯一真实缺口 |

对 100 组的候选缺口抽样跑 baseline 对照（`tl=15s`）：**10 个候选中 0 个 baseline 达标**；仅 4 个 baseline 能榨出 util≈0.99 的**部分解**而 route2 空手——但都是 tight≈1.0 的超紧组，baseline 也未真正达标。

### 两处自我纠偏（诚实记录，避免误导后续）
1. **误判"真不可行"的错误界**：一度用 `ceil(管长/最长母材)×需求 > 总根数` 判不可行——**该界无效**（一根母材可为多根管供焊接片）。已剔除，未写入生产（否则会误杀可行组）。
2. **误判"列覆盖不足"**：一度以为加列能救活。实测把列预算 **8000→80000**、生成窗口 6s→40s，组 7/12/36 **仍全部 INFEASIBLE**——**加列无效**，证明不是覆盖度问题，而是这些组本身处于可行域边缘（0.2% 余量被强制 kerf 吃光）。

### 取舍决定
- **不投入列生成**：真实可救缺口 ≈ 1/40（且是超紧边缘组），而列生成/分支定价是重型工程（定价子问题=带禁焊/min_seg/max_joints 约束的背包）。**ROI 极低**。
- route2 未解组由 baseline 兜底（择优+回退安全），**无正确性损失**。
- 若将来"短管但缺长母材"类订单显著增多（诊断比例上升），再回头做列生成才有业务价值支撑。


## 11.14 路线 C 工程加固：正式单测 + 开关文档

### 单元测试（`backend/tests/test_route2_equiv.py`，18 用例全过）
将 route2 接入契约固化为回归测试，纳入现有 pytest 套件：
- **独立引擎契约**：`solve_group` 输出过真 `verify_solution`；segment-balance（焊接消耗料段 ⊆ 切割产出料段）；禁焊区（焊口不落在任何 forbidden 区间）；must_use（被标记母材至少用够数量）。
- **接入契约**：默认关闭（无 env 时 `_route2_enabled()` False）；env `{1,on,true,yes}` 开、`{0,off,false,""}` 关；**开启从不劣于关闭**（利用率 ON≥OFF 且过 verifier）；route2 被选中时组内带 `ROUTE2_SELECTED` 标记且 `solver_backend=="route2-equiv"`。

### 开关用法
```bash
# 默认：route2 关闭，仅两层 MILP
python -m app...

# 开启 route2 作为择优补强引擎（大小写不敏感：1/on/true/yes）
NESTING_ROUTE2=1 python -m app...
```
- 开启后，route2 在每组 MILP 之后运行，**仅当按词典序（利用率→焊口数）严格更优**时替换结果；预算受总时限约束；引擎不可用/未解出时静默回退，绝不使某组变差。
- 适用边界：擅长补强"两层 MILP 解不出的紧组"；对处于可行域边缘（0.2% 余量）的超紧组仍会 INFEASIBLE，此为已知求解能力上限（见 §11.13），由 baseline 兜底。


## 11.15 基准验收：多 seed OFF vs ON 与自校验闸门修复

### 验收方法
用 `scripts/_e2e_route2.py`（逐组增量打印、OFF/ON 交替、per-line flush、`BladeMargin` 统一钉 10）对历史订单跑 OFF（baseline）vs ON（+route2），核对 verifier 全过、救活组、利用率增益、零回退。

### seed 7（30 组）——干净通过
- verifier：OFF 30/30、ON 30/30；**regressed 0**；improved 6、route2 selected 5。
- 典型救活：组 19/21/26 由 `INFEASIBLE(util=0)` → `FEASIBLE/TARGET_REACHED(util≈0.99)`；组 25 由 0.143→0.759。

### seed 2（30 组）——暴露两类问题并定性
- **组 9：`verification_failed`（真 bug）**。root cause = `MUST_USE_STOCK_NOT_USED`：某母材被标 must_use（`stock_demand=798` 全量必用），但需求极小无法消化，route2 产出 util≈0.005 的退化解只用了 196 根。**baseline 对该组判 INFEASIBLE 正确**，route2 却把退化解当解交回，且接入层选优时未复核 verifier → 被选中并污染结果。
- **组 11：`REGRESSED` 但非 route2 选中**（无 R2 标记）。off 0.998(TARGET)→on 0.981(FEASIBLE)：两趟 baseline 在时限压力下产出不同增量解（求解器时序抖动），非 route2 缺陷；route2 的择优闸门本就不会用更差解替换。

### 修复：`solve_group` 末端自校验闸门
接入层"选优不复核 verifier"是根因。已在 `route2_equiv.solve_group` 末端加**生产 verifier 自校验**：
- 由 group 字段（长度/需求/禁焊区/max_joints/母材数量与 must_use/kerf 参数）合成单组 payload（`_synth_group_payload`，精确复现 `pipe_id = parent|jlxh|cube`），跑真 `verify_solution`；**任何 error → 返回 None**。
- 效果：组 9 的退化解在交回前被拒（返回 None），接入层不再有可选对象，由 baseline 兜底；组 9 的 `verification_failed` 消除。
- 防御纵深：无论 route2 内部 schema 映射未来出现何种缺陷，都不可能产出 verifier 不过的生产解。
- 新增回归单测 `test_solve_group_rejects_result_that_fails_verifier`（must_use 远超可消化量→必被闸门拒），`backend/tests/test_route2_equiv.py` 共 18 用例全过。

### 结论
- route2 作为择优补强引擎**零正确性风险**：自校验闸门保证选中的必过 verifier，择优规则保证从不劣化。
- 救活能力在 seed 7 已量化（多组 INFEASIBLE→高利用率解）；seed 2 的两处"回退"经归因均为 baseline 时序抖动（无 R2 标记、OFF/ON 两趟 baseline 各自求解），与 route2 无关。
- **验收口径修正**：`_e2e_route2.py` 已把改动按是否带 `ROUTE2_SELECTED` 标记拆分为 **route2 归因**（r2_improved/r2_regressed）与 **baseline 抖动**（jitter±）。通过条件收紧为"verifier ON≥OFF **且 r2_regressed==0**"——baseline 抖动不再误判为 route2 回退。
- 复核结果：seed 7 与 seed 2 均 **verifier 30/30、r2_regressed=0**；seed 2 组 9 的 `verification_failed` 已被自校验闸门消除。

## 11.16 必用料语义修正：从"按长度硬下界"到"按材质规格全局优先"

### 背景（原实现错误）
组 9 的 `MUST_USE_STOCK_NOT_USED` 起初被当作 route2 的 bug，用自校验闸门"拒掉"了事。经与业务澄清，真正问题是**三处代码对"必用料"的语义理解全错**：原实现把 `must_use` 当成"该长度母材至少用 N 根"的**按长度硬下界**（`used[length] >= must_use[length]`）。

### 正确语义（业务定稿）
必用料是**按材质规格（= 一个 `MaterialGroup`）**维度的**全局优先级**，不是按长度、不是按单根：
1. 一个材质规格组内，被标记必用的母材**优先使用**。
2. **只要组内还有必用料未用完，就不允许使用任何可用料**（可用料使用量 `A>0 ⟹` 必用料用量 `U == 组内必用总根数 MU_total`）。
3. 需求小、用不完全部必用料时：用够需求即可，**剩余必用料不强制切、不算违规**；此时因必用料未清零，可用料一根都不能用（需求必须全由必用料满足）。
4. 同一长度既有必用又有可用（真实数据 39% 的必用组存在，如 8200：1 必用 + 29 可用）在此口径下自洽：按组级根数计，先吃满必用根，全清零才开可用槽。

### 三处一致修改
- **verifier**（`MUST_USE_STOCK_NOT_USED`）：改为 `used_available>0 且 used_must<MU_total` 才违规。`used_must=Σ min(used[len], mu[len])`，`used_available=Σ max(0, used[len]-mu[len])`。
- **solver baseline MILP**：删除按长度 `>= must_use` 硬下界；改为**拆分-闸门**编码——每长度 `n=u+a`（`u≤mu`、`a≤avail`），组级布尔 `z_free` 门控所有可用槽（`a≤avail·z_free`），并 `Σu ≥ MU_total·z_free`。`z_free=0` 时全组只能用必用根；`z_free=1` 时强制必用料清零。fallback 分配器同步：只在"用了可用根却未清零必用料"时抛错。
- **route2**（MILP + workorder 复核）：镜像同一拆分-闸门编码；workorder 层把 `MUST_USE_SHORT`（按长度）换成 `MUST_USE_PRIORITY`（组级）。

### 回归验证
- 全套单测 86 过（1 skip=route2 预算）。新增/改写：verifier 两例（`test_must_use_is_group_priority_not_hard_quota` 需求小不强制、`test_must_use_violation_when_available_used_before_must_use_exhausted` 越级用可用料违规）；route2 `test_solve_group_small_demand_not_forced_to_exhaust_must_use` 替换旧的"闸门拒绝"用例（旧用例前提已随语义失效）。
- **seed 2（30 组）复现干净**：verifier OFF 30/30、ON 30/30，r2_regressed=0，jitter 0/0。**组 9 从"被闸门拒 + baseline INFEASIBLE 兜底"转为真正解出**：`off util=0.035 → on util=0.237, R2 IMPROVED, pass=True`——语义修正后需求可由必用料满足，route2 给出合法且更优解。


## 11.17 元启发式选型调研（"types-heavy / 千根级"组的求解能力补强）

### 触发背景（与 §11.13 的关键区别）
§11.13 的"不投列生成"结论建立在**当时缺口 ≈ 1/40 且都是可行域边缘（0.2% 余量）超紧组**——那类组连 baseline 都达不到目标，属真难/近不可行。

但新样本暴露的是**另一种失败模式**：如 `12Cr1MoVG φ57x5.5`（702 种段长、1000+ 根母材、需 99.59% 利用率）。关键点是**老排料软件把它解到了 99%+**——说明它**不在不可行边缘**，而是我们求解器的**规模/枚举瓶颈**：SCIP 的 DFS 列枚举在 9~17 类时爆列超时；贪心兜底因 first-fit 死角放不下合法焊位。这**不是**可行性问题，是**大规模求解能力**问题。因此 §11.13 的 ROI 结论对这类组**不适用**，需重新评估。

### 文献落点（问题类型完全对口）
本问题在文献中已有精确命名与成熟解法：**"1D-CSP with divisible items"**（可分割件切割库存问题）——件被切成小段后**焊接重组**，目标=同时最小化 trim loss 与焊口数。这正是我们的问题。

| 文献 | 方法 | 与我们的关系 |
|---|---|---|
| Ilyés-Balogh (2019/2026, Wiley 6507054) | 三步法：转等价 CSP → 解标准 CSP → 模式还原 MILP | **route2 已实现**；擅长中小规模精确/近优，大规模仍受标准 CSP 求解器规模限制 |
| **MKA 钢厂 (arXiv 1606.01419 / TWMS 9(3))** | **顺序启发式 + 逐母材 DP（改进 BBP）**：每次用 DP 为"一根母材"求一个切割 pattern，按成本（trim loss 主、焊口次）选最优，消耗需求后重复，直到需求满足 | **正是大规模落地方案**；无全局列枚举，天然线性扩展到千根级，工业已部署 |
| Ravelo-Meneses-Santos (2020, J.Heuristics) | 余料可复用 1D-CSP 的元启发式（含多目标分类） | 与我们"余料 pooling / usable leftover"契合，可作局部搜索/多目标层参考 |
| Erjavec 等 SLS 框架 (arXiv 1707.08776) | 演化式随机局部搜索（并行 diversification） | 通用重型框架，属"二期"再考虑，一期过重 |
| cuGenOpt (arXiv 2026) | GPU 通用元启发式（一 block 演化一解） | 二期局部搜索可选后端，一期不用（见 §11.11） |

### 选型结论（推荐）
**首选：MKA 式"顺序启发式 + 逐母材 DP"作为 route3（大规模/types-heavy 兜底路径）**，理由：
1. **问题类型 1:1 对口**（divisible items + 焊接重组 + trim loss/焊口双目标），工业已验证。
2. **规模友好**：逐母材构造，复杂度随母材根数线性增长，正好覆盖 1000+ 根、几百种段长的失败区间——现有 SCIP/route2 的软肋。
3. **约束可嵌入**：min_weld_distance / max_joints / 禁焊区都能在 DP 单母材 pattern 构造时作为可行性过滤，比 MILP 全局列枚举更直接。
4. **工程量可控**：一维 DP + 顺序消耗，不需对偶价/分支定价重型机制（列生成被 §11.13 判 ROI 低正是因为重）。

**不首选纯 GA/SA/ABO**：它们是"解表示 + 邻域算子"框架，收敛慢、需大量调参，且对我们的硬约束（合法焊位）需要专门修复算子；在有 DP 这种问题专属高效构造法时，元启发式应作为 **DP 之上的局部搜索改进层（route3 的二期）**，而非一期主力。

### 路线定位（三层择优，互不破坏）
```
baseline 两层 MILP      —— 中小规模精确（现状，永远兜底）
route2 等价母材 CSP     —— 中小规模紧组补强（已上线，NESTING_ROUTE2）
route3 顺序 DP 启发式   —— 大规模/types-heavy 补强（本次调研目标；新增独立子路径）
```
三者均过同一独立 verifier，按词典序（利用率→焊口数）择优，任一路径失败静默回退，**绝不使某组变差**（沿用 route2 的接入契约）。

### 待办（下一步实现前的确认项）
- route3 的 DP 单母材 pattern 构造需先固化：段长离散化粒度、kerf 计入方式、禁焊区/min_weld_distance 在 DP 转移中的过滤点。
- 先做 demo 在 `12Cr1MoVG φ57x5.5` 等 3~5 个"老软件解出、我们解不出"的样本上验证解出率与利用率，再决定是否并入 service（与 route2 相同的 demo→生产节奏）。


## 11.18 彻底方案调研：大规模紧料可解性与四层架构（评审基线）

> 触发：route3 第一版（顺序 DP）在超紧组（`197N2214MX 12Cr1MoVG φ57x5.5`，1230 根母材 / 需求 1104 / 紧度 **0.9959**）实测失败 ~40 根、利用率卡 96%，且多顺序 + 40 次随机重启均突破不了（失败在 39~47 震荡）。据此判定"逐管/逐母材贪心 + 局部打补丁"触及方法论天花板，遂做严肃文献 + 工业落地调研，回答**根本问题：这类问题到底能不能高质量求解、理论依据、工业界怎么落地**。

### 一、真实规模（样本库 99 组量化）
- **42 组母材 ≥ 1000 根**，**53 组紧度 ≥ 0.99**，最大 5109 根母材 / 2880 根管；最紧 0.9988（3334 根母材）。
- `max_joints` 跨度极大：[0,1,2]（几乎不许焊）到 [13]（允许多焊）。
- 结论：大规模紧料**不是个别极端，是主流工况**，必须有正确方法，不能靠补丁。

### 二、理论可解性判断（关键纠偏）
1. **问题类 = 1D-CSP with divisible items**（CSP + skiving 组合，`max_joints` 卡拼接段数上界）。**strongly NP-hard**，且带焊接/焊口上限/禁焊区/余料复用**没有已知近似比或 PTAS**（工业约束一上，最优性保证即失效）。
2. **"紧度趋近 1"是理论最难区**，三重难度叠加：
   - **大容量**：arc-flow/reflect 伪多项式模型规模 ∝ 母材长度；母材 12000mm 越过"c>10000 变糟"红线（Delorme-Iori-Martello 2016）。
   - **near-perfect packing = Falkenauer triplet 难例**（BPPLIB：huge-capacity + triplet 是精确法最难两类，我们两者都占）。
   - **non-IRUP**：需求 multiplicity 低 + 超紧 → LP 松弛解对整数解无用，rounding 启发式给很差解。
3. **因此 baseline 千根级 3600s 判 INFEASIBLE/超时是这一问题类在此参数区的固有难度，不是实现 bug。**

### 三、关键纠偏：纯列生成 / branch-and-price **不作主引擎**
- Santos-Nepomuceno (2022, Algorithms 15:394) 硬数据：纯 CG 在 **item 类型 >700 时 tailing-off + 退化严重失速，600s 连根节点都跑不完**；大 multiplicity 实例 17 个只能解 4 个。
- **这与我们两层 MILP 超时同根因** → §11.13"不投列生成"结论对大规模同样成立。
- 列生成的正确角色：**给强下界 + 解中小子问题**，其 **pricing 子结构复用到 route3 的 DP 与 LNS 重建子问题**。

### 四、工业落地方案（真正做到"数千根 & 99%+"的都是启发式/matheuristic）
| 方案 | 报告规模 / 利用率 | 对我们 |
|---|---|---|
| **MKA 钢厂 顺序 DP**（arXiv 1606.01419） | 工业已部署，随母材线性 | route3 蓝本；软肋=无全局视角超紧切碎（**正是我们失败根因**），须配外环 |
| **Rebar 两阶段 贪心+GA**（Buildings 2025 15:3693） | **20634m 原料、废料率 0.77%（利用率 99.23%）、数千~数万根** | 最硬证据；接头=焊口、接头间距=禁焊区/min_weld，最像我们 |
| **金属棒 SA + 余料复用**（Bertolini 2023, IJIEC） | 真实工业约束（余料复用/优先级/词典序），对比商软胜出 | 第 4 层 SA 接受准则依据；明确"CG 加工业约束即 untreatable" |
| **Generate-and-Solve**（Santos-Nepomuceno 2022） | 大规模准最优（多数差最优仅 1 bin） | 受限 set-covering matheuristic；第 0/4 层依据 |

**总判断：没有一个纯精确法作主引擎；主力是"问题专属构造启发式（DP）+ 破坏-重建局部搜索/G&S + 精确法解小子问题"。**

### 五、推荐的彻底方案：四层择优架构
```
第0层 强下界与可行性判定：arc-flow/reflect LP 松弛（或 CG 根节点 LP）
      —— 只求下界，提前判定"真不可行/贴边界组"，不在死组上浪费预算
第1层 两层 MILP（现状主引擎，中小规模精确，永远兜底）
第2层 route2 等价母材 CSP（已上线，中小紧组补强）
第3层 route3 = 顺序 DP 构造（大规模快速出可行解）★重做为正确的单母材 RCSP DP
第4层 LNS / Generate-and-Solve 外环（把 1/2/3 层解作初始解，反复
      "破坏最浪费的 k 根母材 → 小精确子问题重建"，词典序择优，多种子并行）★新增，达 99%+ 的决胜层
```
所有层过同一独立 verifier，词典序（利用率→焊口→切法/拼法种类）择优，任一层失败静默回退，**绝不使某组变差**（沿用 route2 接入契约）。

### 六、pricing / 单母材子问题的正确建模（route3 与 LNS 共用核心）
子问题 = **带资源约束的最短路（RCSP）over arc-flow 图**：
- 节点 = 累积长度（整数毫米），弧 = "放某型号管的一段 / 损耗 / 焊点"，一条 0→W 路径 = 一个切-焊 pattern。
- **max_joints**：作路径资源累加，管跨母材边界 +1 焊口，≤ 该管上限。
- **禁焊区**：建弧阶段剪枝（`rel = boundary − pipe_start` 落禁区则弧非法）——复用现有 `_enumerate_equiv_cutplans` 逻辑。
- **min_weld_distance vs min_cut_length**：焊接残段受前者、未焊整管受后者（务必保留 §11.9 的区分）。
- **kerf**：每次切割长度扣 kerf。
- **定价目标**（仅 CG 用）：reduced cost = 成本 − Σ(对偶价 × 产出管数)。route3 = 该 RCSP 的无对偶价贪心版；CG = 套对偶价迭代。

### 七、分阶段工程验证路径（每阶段有止损判据，先验证假设再投重工程）
- **阶段 A 下界与可行性诊断器**（先做，低成本高杠杆）：对 100 组算 LP 下界，提前判定"加时间也 INFEASIBLE"的贴边界组，误杀率=0。产出：省掉无效求解时间 + 明确哪些组"真难/近不可行"。
- **阶段 B route3 单母材 RCSP DP demo**：在 3~5 个"老软件解出、我们解不出"样本上，成功判据 = ≥半数解出可行解、过真 verifier、利用率差老软件 ≤2%（此阶段只验"大规模能否出可行解"）。
- **阶段 C 第 4 层 LNS 外环**（决胜）：以 B/1/2 层解为初始解，ruin-and-recreate（拆最浪费 k 根母材 → 小 MILP/DP 重建），SA 或 improving 接受，多种子并行。成功判据 = 目标组利用率**稳定 ≥99%**、焊口不劣、过真 verifier。
- **阶段 D 并入 service**：`NESTING_ROUTE3` 默认关 + 末端生产 verifier 自校验闸门（复用 §11.15），仅词典序严格更优才替换，失败静默回退。验收 = 多 seed OFF vs ON，verifier ON≥OFF、regressed=0。

### 八、一句话总收
问题**理论上就是最难的那一类**（大容量 + near-perfect + non-IRUP 三重叠加），纯精确法千根级超时是**必然而非 bug**；工业界靠"**构造启发式（DP）+ 破坏-重建 LNS/G&S + 精确法解小子问题**"确实把"数千根、99%+"落地了（rebar 案例是最硬证据）。**下一步最高 ROI = 重做 route3 为正确的单母材 RCSP DP + 加第 4 层 LNS 外环**，而非继续在纯 MILP/纯列生成上加时间或给逐管贪心打补丁。


## 11.19 深度调研增量（在 §11.18 四层架构之上补齐的可借鉴方案）

> 触发：用户要求"再深度查询，看还有没有借鉴方案"。本节只记录相对 §11.16~§11.18 **新增** 的发现，均为本轮亲自读透原文后确认，非泛泛罗列；无实质增量的方向直接标注"无增量"。

### 一、Skiving Stock Problem with Divisible Items（SSP-DC）—— 焊接侧的精确对口建模【新增】
- 文献：Martinovic & Scheithauer, *Integer linear programming models for the skiving stock problem*, EJOR 251(2) 2016；*Characterizing IRDP-instances …*, Optimization 2018；*An upper bound Δ(E)<3/2 for the divisible case*, DAM 229 2017。
- 核心：把"**短段拼成长件**"直接建成 skiving（下料的对偶），给出 **arc-flow / onestick / Kantorovich 三种模型**，并证明三者连续松弛与 pattern 模型等价；大量实例具 **IRDP（整数向下取整性质）**，即 LP 松弛界非常紧。
- **可借鉴技巧**：我们的"焊拼"方向本质就是 divisible-case skiving。可用 **SSP arc-flow 松弛出"拼接侧的紧界"**，比单纯 CSP 下界更贴合焊接结构；IRDP 结论说明"贴边界组"的 LP 界可直接当近优参照。
- **落到哪一层**：**第 0 层**（强下界与可行性诊断，与 §11.18 六节的 RCSP 建模互补：CSP 侧给下料界，SSP 侧给焊拼界，双界夹逼判"真难/近不可行"）。

### 二、Reflect arc-flow + graph compression —— 大容量 c>10000 的规模压缩【新增，直接解 §11.18 第0层落地难点】
- 文献：Delorme & Iori, *Enhanced Pseudo-polynomial Formulations for BPP/CSP*, INFORMS J. Comput. 32(1) 2020（reflect：只用一半母材长度建图）；Brandão & Pedroso, *Cutting Stock with Binary Patterns: Arc-flow with Graph Compression*, arXiv 1502.02899（含毫秒级递归+memoization 直接建压缩图）。
- 问题背景：我们母材 8000~12200mm，直接建 arc-flow 图节点 ∝ 容量会爆——这正是 §11.18 判"c>10000 变糟"的红线。
- **可借鉴技巧**：**reflect（图规模减半）+ 三步 graph compression** 把 arc-flow LP 松弛压到可承受，使"强下界引擎"在千根紧料上真正跑得动；建图用 Step-3 递归+memoization，避免中间大图。
- **落到哪一层**：**第 0 层**（让 §11.18 阶段 A"下界诊断器"从"理论可行"变"工程可行"）。

### 三、轻量列生成的真实边界 + 正确姿势（重要纠偏，深化 §11.13/§11.18 第三节）【新增】
- 文献：Maher & Rönnberg, *Integer programming column generation: accelerating branch-and-price using a novel pricing scheme …*, Math. Prog. Comput. 2023（已集成开源 GCG）；配套综述 Lübbecke-Puchert、Sadykov 等。
- 两条硬结论：
  1. **restricted master heuristic / price-and-branch（根节点池 + sub-MIP 选）单独用潜力有限、不保证可行**——文献明确判定。我们此前设想的"只用 CG 根节点池 + MIP 从池里选"这条轻量线，**单独不成立**。
  2. 正确姿势 = **LNS destroy-repair + 定制 "pricing-for-integrality"**：破坏一批浪费的列 → 用"面向整数可行"的定价（不是普通 LP 对偶价）重造列 → sub-MIP 选；**对"根节点整数间隙大的困难实例"效果最好**——这正是我们千根紧料的画像。
- **可借鉴技巧**：第 4 层 LNS 外环的 repair 步不要用普通 LP 定价拉列，要用 **pricing-for-integrality**（favor 能进整数解的列）；destroy 的变量固定用"改定价目标系数"实现，避免破坏定价子问题结构（GCG 蓝本）。
- **落到哪一层**：**第 4 层**（给 §11.18 决胜层的 repair 环节一个权威工程蓝本，而非拍脑袋）。

### 四、中文行业标准坐实现实约束（可写进需求/验收）【新增，非算法但高价值】
- 来源：过热器蛇形管束质检文（12Cr1MoV 42×5 等实例）、350–1000MW 超超临界过热器工艺专利、600MW 受热面制造技术研究。
- 坐实的硬约束（与我们参数一一对应）：
  - **平均每 4m 允许一条对接焊缝**（→ 焊口数上限的行业默认推导依据）。
  - **L<3m 不得拼接；对接段长度不宜小于 2500mm，最短不小于 500mm**（→ min_weld_distance / min_cut_length 的现实取值区间）。
  - **对接焊缝须在直段、距起弯点/附件边缘 ≥200mm**（→ forbidden zones 的现实出处）。
  - **HR3C 等材料受"缝隙炉/热处理炉有效尺寸"限制 → 下料上限 → 大量弯后拼焊口**（→ 解释了为何 max_joints 高、为何天然是"必须拼接"问题）。
- **可借鉴价值**：约束建模从此有**行业标准背书**；"每 4m 一条焊缝"可作 **verifier 的合理性校验 / max_joints 默认上限推导**；validate 时可对超出行标的解给告警。
- **落到哪一层**：需求/约束层 + verifier 合理性校验（非求解层）。

### 五、本轮无增量的方向（避免后续重复挖）
- 通用 GA/SA/ABO 解表示框架：与 §11.16 结论一致，仍属"重、需修复算子"，不作一期主力——**无新增**。
- 纯 branch-and-price 作主引擎：再次被 §11.18 第三节与本节三-1 否定——**无新增**。

### 六、新增可借鉴清单（按可落地性 × ROI 排序）
| 优先级 | 增量项 | 落到层 | 落地成本 | ROI 理由 |
|---|---|---|---|---|
| ★★★ | **reflect + graph compression 让 arc-flow LP 下界工程可行** | 第0层 | 中 | 直接把"下界诊断器"从纸面变可跑，提前止损死组，全局省时间 |
| ★★★ | **pricing-for-integrality 作第4层 repair 定价** | 第4层 | 中高 | 决胜层的正确姿势，有 GCG 开源蓝本，避免走"根节点池单独选"的死路 |
| ★★ | **SSP-DC 焊拼侧紧界（arc-flow/onestick）双界夹逼** | 第0层 | 中 | 比单 CSP 下界更贴焊接结构，IRDP 使近优参照可信 |
| ★★ | **行业标准约束/焊口上限校验写进 verifier & 需求** | 需求/校验 | 低 | 低成本、直接提升结果可信度与验收说服力 |

### 七、对 §11.18 四层架构的净影响
1. **第 0 层从"理论"落到"可工程实现"**：reflect+compression（本节二）+ SSP 焊拼界（本节一）= 双界可行性诊断器。
2. **第 4 层 repair 有了权威蓝本**：pricing-for-integrality（本节三）取代此前"根节点池单独选"的错误设想。
3. **约束层拿到行标背书**（本节四），route3/LNS 的可行性过滤与 verifier 校验都更硬。
4. 架构本身不变，仍是 §11.18 的四层择优 + 同一 verifier + 静默回退，本节只是把其中最虚的两个落点补实。

### 八、深挖第二轮增量（现成代码 / 可直接抄的公式 / 应用域首创判断）
> 来源：后台深度调研 agent 七方向交叉核对，剔除已知方案后的净增量。重点是**有现成开源实现或可直接落地的公式**。

1. **现成开源代码（可直接跑，省自研）**：
   - **`wotzlaff/ssp-arcflow`**（GitHub，Martinovic-Delorme-Iori-Scheithauer COR 2020 配套，含 reflect 版 + `--relax` 求 LP）：**直接拿来算焊接侧上界/可行性**，落 **第0层**，无需自己从零写 SSP arc-flow。
   - **Florian Fontan `PackingSolver` + `columngenerationsolver`**：第3/4层最佳开源蓝本（构造 + 列生成外壳），可作 route3/LNS 的工程骨架参考。
   - **Brandão-Pedroso `VPSolver`**（COR 2016）：紧凑 DAG 表 pattern，side-constraint 机制可容纳 max_joints/min_weld_distance/禁焊区/kerf 且保持强松弛，可当第0层下界求解器。
2. **RCVF（Reduced-Cost Variable-Fixing）削图**（de Lima-Iori-Miyazawa IPCO 2021 / arXiv 2105.14961）：解前用对偶解**无损删弧**，c≈12000 弧数爆炸时是削图关键，配 reflect 一起用 → **第0层**。
3. **divisible-case 整数间隙 <4/3 界**（Optimization-Online 7952, 2020）：可编码为**第0层收敛/停机判据**——落入 divisible 结构时 `z_opt < LP + 4/3`，避免 non-IRUP 下把 ⌈LP⌉ 误判为最优（正是 §11.13 两次误判界的坑，现在有理论支撑）。
4. **SVC 伪价修正（可直接抄公式）**（Mukhacheva et al., Pesquisa Operacional 2000）：每生成一个 pattern 后，被用掉的件伪价上调（越被抢越贵），带随机权重 Ω∈[1,3] 防循环；把 DP 段价从"原始长度"换成伪价 yᵢ → **第3层**，几乎零额外依赖。⚠️ m>100 类型多 + 比价接近时会 stall（正是我们 near-1 场景），需先按 spec 分组。
5. **轻量 CG 判决（回答我们的老问题）**：pool 直接 MIP **不保证可行、near-1/non-IRUP 必翻车**（与本节三-1 一致）；**正解 = CG 只出根 LP 下界（第0层，稳赚）+ pool 交给 Relax-and-Fix / Constrained-RF diving（da Silva & Schouery 2024 IJOC）收敛**，而非 pool-MIP 一把梭。
6. **Tanir 2019（TWMS JAEM 9(3)）= MKA 期刊版**：给了 **A/B 双数组 DP 细节**（A 数组常规 knapsack 填充、B 数组同步找拆分段焊接补满，θ=min 段长 / 单 pattern 拆分数上限 直接映射 min_weld_distance / max_joints）——这是 **第3层构造 DP 的具体骨架**。
7. **Filtered Beam Search**（Applied Sciences 2024 14:8172）：把顺序 DP 从"单链最优"扩成"保留 K 条最优前缀链"，用词典序目标当 beam 评估——**从贪心升到中量级最省事的一改** → 第3层。
8. **usable-leftover 聚合罚**（Cherri 2009 RGRL1 / Campello 2022）：好解不是总废料最小，而是**把损耗集中成少数够大余料**；DP 对落在"死区"的余料重罚逼其聚合成可复用段 → 服务 offcut reuse 目标 → 第3/4层。
9. **应用域首创判断**：公开文献/专利中，**蛇形管/过热器/受热面无人当作 1D 焊接下料求解问题**（只到传热/焊接工艺）。我们在该应用域属首创，算法内核须从"1D-CSP with divisible items"（方向3）迁移，不能指望现成受热面专用解法。


## 11.20 业务专家评审结论与放行门槛（阻断实施，先补前置）

> 角色：严格业务专家，基于真实生产工况与落地难点评审 §11.18/§11.19。**结论：方向认可，但暂不批准直接进入阶段 A/B/C。** 方案目前是优秀的"调研结论 + 架构草图"，尚缺"业务闭环前置项"，须先补齐再开工，否则大概率做到阶段 C 才发现追不平/口径不一致，重来。

### 一、评审发现的致命问题（3 条）
1. **没有可签字的成功基线**：§11.18 阶段 C 的"利用率稳定≥99%"是口号。现场真正对标的是**老软件**（如 `12Cr1MoVG φ57x5.5` 老软件 99.9214%）。方案未回答"追平还是接近、差多少可接受"。
2. **第4层 LNS 是决胜层却零实测**：0/1/2/3 层都有实测数据，唯独押注"达 99%+"的第4层是纯设想，且 §11.19 的 pricing-for-integrality 是全项目工程量/翻车风险最大的一块，却排最后做。
3. **老软件是黑盒**：全程推断其为 GA/SA，但从未拆解其真实解。若它把某条我们视为硬约束的规则当软约束，则**永远追不平不是算法问题、是约束口径问题**。

### 二、架构层面质疑（3 条）
4. **第0层定位写错**：用户明确"可跑一天"，第0层"省时间"ROI 被削弱；其真正价值是**给第4层 LNS 一个"离最优还差多少"的停机判据**，文档卖点定位需改。
5. **route3 RCSP DP 与已实测顺序DP 的职责边界含糊**：40 根失败根因是"`max_joints` 下小余料拼不出长管 = 全局资源分配问题"，单母材 DP 再正确也解不了。route3 必须老实定位为"**只出快速可行初始解、不负责质量**"，质量归第4层。
6. **verifier 行标校验只能告警不能拦截**：每4m一焊缝是行业**平均**建议，非逐管硬上限，写成硬校验会误伤合法短管多焊。

### 三、被低估/遗漏的风险（2 条）
7. **切法/拼法种类是隐藏验收杀手**（★ 用户确认为当前头号痛点）：现场换切割设定/拼装工装有切换成本。老软件种类少、我们利用率追平但种类翻倍，现场反而不用我们。词典序把它排第三，权重被严重低估。
8. **must_use / 材质规格优先级与新架构交互缺失**：LP 下界、RCSP DP、LNS 重建子问题是否都尊重 must_use 优先级？文档只字未提。LNS "破坏 k 根母材重建"若动了必用料优先级即违反业务红线。

### 四、修正后的验收基线（业务拍板）
> 用户裁定："**只要不比老软件劣化就可验收**。老软件问题是慢、常需手动排；新软件速度很快，但**切法/拼法都很复杂、种类偏多**，这是当前最明显的问题。"

因此**修订 §11.18/§11.19 的目标函数与验收判据**：
- **验收硬判据（逐样本对标老软件，全部满足才算通过）**：
  1. 利用率 ≥ 老软件（不劣化，允许持平）；
  2. 焊口总数 ≤ 老软件；
  3. **切法种类数 ≤ 老软件；拼法种类数 ≤ 老软件（本次升级为一等验收指标，不再是词典序末位）**。
- **词典序目标调整（据业务信号重排）**：利用率（不劣化为硬门槛）→ **切法/拼法种类数**（提到第二优先，因这是现场头号痛点）→ 焊口数。速度不入目标（可跑一天）。
- 含义：新软件的卖点是"**又快、又不比老软件差、且切法拼法更简洁**"。**若利用率追平但种类比老软件多，判为不通过**——这是本次评审对原方案最重要的修正。

### 五、放行门槛：4 项前置（低成本高信息量，按序做完才解锁阶段 A）
- **前置① 拆解老软件真实解（第0件事）**：取 2~3 个老软件完整解，逐管拆开，核对其焊口数/焊位/段长是否满足我们的硬约束（min_weld_distance / max_joints / 禁焊区）；**同时统计其切法种类数、拼法种类数、焊口分布**作为验收基线值。产出：确认约束口径是否一致 + 拿到每样本的"老软件基线四元组（利用率/焊口/切法种类/拼法种类）"。
- **前置② 定量验收基线表**：把前置①的基线四元组写成表，逐样本钉死"不劣化"阈值。没有此表，阶段 C 无法验收。
- **前置③ 朴素 LNS 最小验证（决胜层前移）**：用最朴素外环（随机拆 k 根母材 → 现有 baseline MILP 重解该 k 根子问题 → 词典序更优则接受），在 1 个目标样本上证明"外环真能把利用率往上抬、且不使种类变多"。**先证明决胜层能赢，再投 route3 RCSP DP 重工程。**
- **前置④ must_use 贯通说明**：在第0/3/4 层分别写明 must_use（材质规格级、优先塞满）如何传递与保持，明确 LNS 破坏-重建不得违反必用料优先级。

### 六、放行判据
四项前置完成且满足：约束口径与老软件一致（或差异已澄清）、拿到定量基线表、朴素 LNS 在≥1 样本证明"利用率能抬且种类不劣"、must_use 贯通有明确设计 —— 方可解锁 §11.18 阶段 A 起的正式实施。


## 11.21 前置①②执行结果：老软件解拆解 + 约束口径核对 + 定量验收基线表

> 执行脚本：`scripts/_analyze_legacy.py`（纯只读分析，不改生产代码）。数据源：`frontend-next/public/samples.json`（每样本含 `problem` 我方输入 + `legacy` 老软件完整解）。用户指令："拆解一定要拆解最复杂的，简单的我们已能覆盖。"故按复杂度多维排序，取最复杂 4 笔（A 规模+种类双高、B 切拼种类绝对最多、C 规模绝对最大、D 管型多+最紧），按 A→B→C→D 拆。

### 一、老软件解的数据结构（拆解得到）
- `legacy.GeneralInfo`：`UtilRate` / `WeldingJointQuantity` / 单材质规格的管总长与母材总长。
- `legacy.Result.CuttingPattern.CuttingPipe[]`：每条 = **一种切法**（`Length` 母材长 + `Part` 空格分隔分段 + `TrimLoss` + `Number` 张数）。切法种类 = 数组长度。
- `legacy.Result.WeldingPattern.WeldingPipe[]`：每条 = **一根成品管的拼法**（`FigureNumber`+`jlxh` 标识 + `Length` + `Pattern` 分段）。拼法种类 = 数组长度。
- 约束字段在 `problem`：`Min_Welding_Length=500`、`Adjacent_Corner_Distance=500`、`Corner_Distance=100`、`Adjacent_Distance=150`、每管 `Unweldable_Area`=禁焊区 list[[start,end]]、`Max_Weldingjoint_Number`（本批=2000，等价无上限）。

### 二、约束口径核对结论（关键：4 笔全部零违规）
逐管把老软件拼法分段还原成焊口位置，核对三条硬约束：

| 核对项 | A 9f618d9f | B 6a4fecf7 | C 3040bd13 | D 350f2dbb |
|---|---|---|---|---|
| 焊接残段 < min_weld(500) | 0 | 0 | 0 | 0 |
| 焊口落禁焊区 | 0 | 0 | 0 | 0 |
| 焊口数超上限 | 0 | 0 | 0 | 0 |
| 管长与分段总长不符 | 0 | 0 | 0 | 0 |
| 焊口重算 vs 报告 | 836=836 | 546=546 | 765=765 | 968=968 |

**结论：老软件的解完全遵守我方三条硬约束（min_weld_distance / 禁焊区 / max_joints），焊口重算值与其报告值逐笔精确吻合。**
→ **约束口径一致**。这一步排除了"追不平是因约束口径不同"的可能：我们与老软件是在**同一规则下竞争**，差距（若有）纯属算法质量问题，不是建模口径问题。这是本前置最重要的结论——它让后续"对标老软件"的验收标准站得住。

### 三、重要建模洞察（拆解中发现，影响 route3/LNS 设计）
1. **过渡短管混排**：老软件把 150mm 过渡短管/附件与长管**混在同一根母材上一起切**（实测一根母材可切 40+ 个 150 短管 + 几段长管，TrimLoss 仅几十 mm）。这是它高利用率的重要手段——**150mm 件是成品件本身、不是焊接残段、不受 min_weld 约束**。我方 route3/LNS 的单母材 pattern 构造必须支持"长短件混切"，否则先天落后。
2. **切法种类 < 拼法种类是常态**（A: 48<64；C: 26<44；D: 35<64）：老软件**刻意复用少数母材切法**去拼多种管——即"切法归并"是它控制切法种类的核心技巧。这直接对应我方头号痛点（切法/拼法种类多），提示 route3 目标里"切法种类最小化"应通过**pattern 复用/归并**而非逐管独立构造来实现。
3. **B 笔 546 母材 = 546 焊口 = 546 下料张数**：几乎"一母材一焊口"，说明小规模高种类样本老软件用的是"整料尽量少拼"策略。

### 四、定量验收基线表（前置②交付物，逐样本钉死"不劣化"阈值）
> 验收硬判据（§11.20 四）：新解须逐项 **不劣于** 下表——利用率 ≥、焊口 ≤、切法种类 ≤、拼法种类 ≤。

| 标签 | id | 材质 / 规格 | 母材根数 | 管型 | 管需求 | **利用率(≥)** | **焊口总数(≤)** | **切法种类(≤)** | **拼法种类(≤)** |
|---|---|---|---|---|---|---|---|---|---|
| A | 9f618d9f | SA-213M S30432 / φ45×10 | 1142 | 64 | 1936 | 0.99656 | 836 | 48 | 64 |
| B | 6a4fecf7 | SA-213S30432 / φ45×12 | 546 | 68 | 442 | 0.99729 | 546 | 70 | 68 |
| C | 3040bd13 | SA-213M S30432 / φ51×5.5 | 5109 | 44 | 2880 | 0.99489 | 765 | 26 | 44 |
| D | 350f2dbb | SA-213S30432 / φ45×10 | 1105 | 64 | 1936 | 0.99850 | 968 | 35 | 64 |

说明：
- 利用率均 0.9949~0.9985，均高于各自 `Target_Util_Rate`(如 A=0.9925)——老软件稳定超目标。
- C（规模最大 5109 母材）利用率反而最低(0.99489)、切法种类最少(26)——**大规模时老软件更依赖切法复用、牺牲少量利用率**，这是我方大规模路径可借鉴的取舍。
- 焊口/张数口径：焊口总数按"每拼法 (段数−1)×张数"汇总；此表为阶段 B/C/D 的唯一对标基准。

### 五、前置①②放行状态
- 前置①（约束口径核对）：**通过**——4 笔零违规，口径一致。
- 前置②（定量基线表）：**通过**——四元组已钉死（上表）。
- 待办：前置③（朴素 LNS 最小验证）、前置④（must_use 贯通说明）完成后，方解锁阶段 A。


## 11.22 前置③执行结果（上）：route3 转合格构造器 + kerf 记账模型彻底修正

> 执行脚本：`scripts/_demo_route3_seqdp.py`（顺序 DP 构造 + 转 solution schema + 过独立 verifier）。本节记录 LNS 的**前置依赖**——一个"可被独立校验器签字"的初始解构造器；LNS 最小验证本身见 §11.23（下）。

### 一、决定性发现（LNS 的立足点）
1. **baseline 对高种类全必拼样本给多少时间都出不了初始解**：B 样本（68 管型、全必拼、紧度 0.9973）在 30s / 120s 下均 INFEASIBLE/UNSOLVED。→ §11.20 前置③原设计"用 baseline MILP 重解 destroy 出的子问题"在关键样本上**根本启动不了**，必须先有一个非 MILP 的构造器兜底。
2. **route3 顺序 DP 能在亚秒级出合格可行解**：B 样本 0.5s 出解，利用率/焊口已达标甚至更优。→ route3 是第4层 LNS 的**必需前置依赖**，不是可选兜底。
3. **route3 唯一劣化项就是切法/拼法种类偏多**（正对用户头号痛点）→ 这正是第4层 LNS/归并要压的核心目标，验证靶子天然成立。

### 二、route3 → solution schema → 独立 verifier（评审硬要求已达成）
为让 route3 的每个数字可信、可对标，给 `StockPool` 增加了**物理母材实例追踪**（`_BarInstance`：记录每根母材的切段序列 `cuts` 与剩余 `remain`；leftover 携带其来源母材 id），并新增 `route3_to_solution()` 把 route3 内部结构转成 `app.solver` 的解 schema（welding_patterns / cutting_patterns / metrics / input_normalizations），再送 `app.verifier.verify_solution` 独立校验。

- **B 样本（kerf=0）**：`VERIFY passed=True, issues=0`——段平衡 / 焊位合法 / min_weld / 容量 / 库存 / must_use / 指标复算全过。
- **A 样本（kerf=10, PARTIAL）**：`issues=1`，且该 1 项是 `PIPE_PATTERN_QUANTITY_MISMATCH`（某管型需求 22、拼法 12）——**这不是 bug，是 route3 诚实报告"10/1936 根管未排出"的部分解状态**，正是 LNS 要救的失败管。

### 三、kerf 记账模型彻底修正（"大胆假设、小心求证"，非补丁）
A 样本（kerf=10）暴露 route3 原实现的 kerf 记账根本错误（`CUT_CAPACITY_EXCEEDED` / `SEGMENT_BALANCE_MISMATCH`）。根因与修正：

- **模型错在"焊缝损耗"**：原实现把 kerf 记在焊接处（每焊一段扣 kerf）。**物理正确的是"切口损耗"**——焊接是熔合、零损耗；材料只在**锯切**时损失。校验器亦按此：管的各分段精确求和到管长，kerf 全部归入切割 pattern。
- **统一为单一模型**：一根母材消耗 = `Σparts + kerf×(段数−1)`，与校验器逐字节一致。
  1. DFS 段长求和精确等于管长（不再在焊缝间扣 kerf）；
  2. `_charge_cut`：每根母材每多切一段记一段，kerf 由池化时物理预留；
  3. `_pool_remainder`：leftover 池化时从 `remain` 中**物理预留下一刀 kerf**，使 `remain` 与池化长度锁步，杜绝"切超母材长"；
  4. **每一段焊出母材都恰好记一次 cut**（含整段焊入的 leftover），保证切割侧与焊接侧段平衡。
- **失败回滚改用 snapshot/restore**：一根管只碰几根母材，快照-恢复比手工反演 kerf/tail/exact-weld 交互更简单且可证明正确。

修正效果（A 样本 kerf=10）：违规数 **362 → 1**，且剩 1 项为部分解的真实缺口（非 bug）。B 样本（kerf=0）保持 `passed=True`。全库 kerf 分布：43 笔 kerf=0、56 笔 kerf>0（10/5/8）——**kerf>0 过半，此修正是 route3 可用于真实订单的前提**。

### 四、route3 vs 老软件（初始解，未经 LNS 优化）
| 样本 | 利用率(verifier 复算) | 老软件利用率 | 焊口 | 老软件焊口 | 切法种类 | 老软件 | 拼法种类 | 老软件 |
|---|---|---|---|---|---|---|---|---|
| B 6a4fecf7 | 0.99729 | 0.99729 | 445 | 546 | 95 | 70 | 89 | 68 |
| A 9f618d9f (PARTIAL, 缺10管) | 0.99145 | 0.99656 | 706 | 836 | 133 | 48 | 104 | 64 |

- B：利用率**持平**、焊口**更少 101**，唯切法/拼法种类偏多——第4层只需"压种类"。
- A：尚缺 10 管、种类明显偏多——第4层需"**救活失败管 + 压种类**"，是 LNS 的完整靶子。

### 五、前置③（上）放行状态
- route3 已是**可被独立校验器签字的合格初始解构造器**（kerf=0/>0 双路径），且 kerf 模型物理正确。→ LNS 的前置依赖成立。
- 待办：§11.23 朴素 LNS 最小验证（destroy 最浪费母材+失败管 → repair → 词典序接受），在 A/B 证明"利用率能抬 / 失败管能救 / 种类不劣"，方完成前置③。


## 11.23 前置③执行结果（下）：朴素 LNS 最小验证

> 执行：`scripts/_demo_route3_seqdp.py --lns N --destroy-k K`。`lns_improve()` 以 route3 初解为起点，每轮 DESTROY「trim loss 最大的 K 根母材 + 2 根随机母材」，释放其材料并把消耗过这些母材的所有管重新入队，REPAIR 用 route3 的 DFS 重放保留管 + 重排释放/失败管，词典序严格更优才接受（词典序：失败管↓ → 切+拼种类↓ → 利用率↑ → 焊口↓，对齐 §11.20 四）。**纯 destroy-rebuild，每个中间解按构造即 verifier-valid。**

### 一、实测结果
| 样本 | 初解(route3) | LNS 后(40 轮) | 效果 | verifier |
|---|---|---|---|---|
| A 9f618d9f | failed=10, cut=133, weld=104, util=0.9832 | failed=**7**, cut=146, weld=113, util=0.9846 | 救活 3 管、利用率↑ | passed（剩 2 项为 PARTIAL 真实缺口，非 bug）|
| B 6a4fecf7 | failed=0, cut=95, weld=89 | failed=0, cut=95, weld=**88** | 拼法种类 −1 | passed=True, issues=0 |

耗时：A 约 41s / B 约 27s（40 轮，每轮全量重放 1936/442 管）。

### 二、三个诚实结论（"先证明再重工程"的核心价值）
1. **LNS 框架正确且有效**：严格词典序接受、每步解均过独立 verifier、确实单调改善——**决胜层机制成立**，第4层方向可行。
2. **朴素 destroy-repair 增益天花板低**（救活/压种类均个位数）：因 repair 仍用 route3 贪心 DFS 重建，"拆浪费母材→贪心重排"跳不出局部最优。→ **印证 §11.19：真正决胜必须靠"pricing-for-integrality / 小型精确子问题（ILP/RCSP）重建"，而非贪心重排。** 这是第4层真正的工程量所在，本次最小验证已证明"贪心重建不够"，避免了盲目投入。
3. **目标函数内在冲突（需业务拍板）**：救活失败管必然新增 pattern（多拼几根→多几种切法/拼法），与"压种类"直接对立。当前词典序把 failed 排第一，故 A 的种类在救活过程中反增。**这是真实业务权衡，不是缺陷**：
   - 选项甲：救活优先（宁可种类多，也要把管排全）——适合"排不全就得人工兜底"的场景；
   - 选项乙：种类优先（宁可留几根管人工补，也要切法/拼法最简）——适合"少数缺管可接受、换工装成本高"的场景。
   - 词典序首位应据此二选一。**§11.20 四把种类升为一等指标，倾向乙；但 A 尚缺 10 管，若乙则永远缺管**——此点必须现场确认。

### 三、性能观察（工程化提示）
朴素 LNS 每轮 O(n) 全量重放，40 轮 = O(40n)。产线化需改为**增量 destroy-repair**（只重建被释放的局部），或把 repair 子问题规模钉在 K 根母材内用精确 ILP 解——两者都指向"小型精确子问题"这一第4层正解。

### 四、前置③放行状态
- **通过（带条件）**：朴素 LNS 在 A/B 上证明「外环能救活失败管、能压种类、全程 verifier-valid」，决胜层机制成立。
- **明确移交阶段 A/第4层的两件事**：① repair 必须升级为小型精确子问题（贪心重建已验证不够）；② 词典序首位（救活 vs 少种类）需业务拍板。
- 待办：前置④（must_use 贯通说明）完成后，解锁 §11.18 阶段 A。


## 11.24 逆向老软件算法指纹：确认主引擎是"集合覆盖/列生成 ILP"（非 GA/SA）

> 业务判断："老软件切法优秀、又能全排完，我们走局部优化路线是不是从一开始就错了、必须全局？" → 正确。投入全局重工程前，先从老软件**完整解的结构**反推它到底用哪种全局算法，避免抄错方向。执行：`scripts/_reverse_legacy_algo.py`（纯只读）。量化 6 个算法指纹于 A/B/C/D 四笔最复杂样本。

### 一、指纹数据（四笔一致）
| 指纹 | A 9f618d9f | B 6a4fecf7 | C 3040bd13 | D 350f2dbb |
|---|---|---|---|---|
| 切法种类 / 下料母材 | 48 / 1136 | 70 / 546 | 26 / 1376 | 35 / 1104 |
| **切法/母材比**（越低=复用越强） | 0.0423 | 0.1282 | **0.0189** | 0.0317 |
| 单切法最大复用张数 | 198 | 99 | 135 | **374** |
| 段数结构 | 1~2段为主+少数40~51段 | 全 1~2 段 | 2~3段为主 | 1~2段为主+少数53~71段 |
| 零废料母材占比 | 37% | 19% | 0% | 38% |
| 前10%母材承担废料 | 51% | **89%** | 29% | **78%** |
| 整料成品段占比 | 75% | 0% | 39% | 69% |
| **同管用>1种拼法** | **0/64** | **0/68** | **0/44** | **1/64** |

### 二、结论：主引擎是集合覆盖 / 列生成 ILP，不是遗传/退火
1. **切法/母材比极低（0.019~0.13）、单切法复用高达 374 张 = 决定性证据**。GA/SA 逐个体扰动不可能产生这种"少数优质 pattern 大批量复用"；**只有集合覆盖/列生成"选少数列、每列开多张"才会有此签名**。此前全程推断的 GA/SA 被否定。
2. **每种管型几乎固定单一拼法（同管多拼法≈0）**：老软件对每管型定一个拼法模板，再由全局 ILP 决定"每种切法开几张"来覆盖所有模板需求——这正是**集合覆盖模型结构**（管型=需求行，切法=列）。
3. **废料高度集中（B 前10%母材吃 89% 废料，其余零废料）**：全局把 trim 精准挤到少数"垃圾桶"母材，GA/SA 做不到，是精确 ILP 求解的特征。
4. **段数两极：大量 1~2 段简单切法 + 少数 40~71 段超多段切法**。超多段即 §11.21 的"150mm 短管混切"整料列——老软件把它当**特殊列**加进 pattern 池。
5. **切法种类被刻意最小化**（大规模的 C 只用 26 种覆盖 1376 母材）——目标函数里显含"少切法种类"，这正是我方头号痛点的解药。

### 三、对我方全局路线的直接指导（抄什么、怎么抄）
- **主引擎换成"列（切法）池 + 集合覆盖 ILP"**：列 = 一种母材切法（1~2 段为主 + 少数多段特殊列）；ILP 选列并定各列张数，硬约束"覆盖全部管型需求"（→ 天然全排完，不再有"救活 vs 种类"冲突），目标"最小化切法种类 / 焊口"。
- **route3 的正确定位**：不再是解法，而是**给列池喂初始优质列 + 兜底可行解**（§11.18 Layer 3）。
- **拼法固定单模板**：每管型先定一个满足约束的拼法（禁焊区/min_weld/max_joints），大幅收敛拼法种类——与老软件对齐。
- **列的动态生成（列生成/pricing）**用于大规模：不枚举全部切法，按需生成能改进目标的列（§11.19 RCSP pricing）。
- 与 §11.18/§11.19 的文献调研**完全吻合**，现在有老软件解本身的实证背书——全局路线方向已钉死。


## 11.25 集合覆盖 ILP 最小 POC：B 样本一次跑通，全指标优于老软件

> 目标（先证明再重工程）：在 B 样本（`6a4fecf7` · SA-213/φ45x12 · 68 管型 / 546 母材 · baseline 求不动、老软件全排完）上，用最朴素的"固定拼法模板 → 枚举切法列 → 集合覆盖 ILP 选列"验证能否同时做到 **全排完 + 切法种类 ≤ 老软件**。执行脚本：`scripts/_poc_setcover_ilp.py`（纯只读，PySCIPOpt 6.2.1）。

### 一、方法（三步，全部朴素实现）
1. **拼法模板（每管一种）**：用 `solver._baseline_weld_patterns` 取每管型"关节最少+字典序最小"的合法拼法（禁焊区/min_weld/max_joints 全过 `_legal_pattern`）。68 管型 → 仅 **17 种不同段长**、884 段需求。这与 §11.24"同管固定单一拼法"对齐。
2. **切法列枚举**：对每种母材长度 DFS 枚举"1 根母材切成若干需求段"的方案（kerf 感知、段长非增序去重）。3 种母材长 → 秒级枚举出 28953 列。
3. **集合覆盖 ILP**：`x_p≥0 整数`=切法 p 开的母材根数；硬约束 `Σ a_p[s]·x_p ≥ demand_s`（每段长需求被覆盖 → 天然全排完）+ `Σx_p ≤ 母材总数`；目标最小化母材根数。

### 二、结果（默认目标=最小化母材根数，全列池）
| 指标 | POC | 老软件 | 结论 |
|---|---|---|---|
| 切法种类 | **23** | 70 | **降为 1/3** |
| 拼法种类 | 68 | 68 | 持平 |
| 母材根数 | **545** | 546 | 少 1 根 |
| 利用率 | **0.998874** | 0.997288 | **更高** |
| 求解耗时 | **1.19s** | —（老软件常需手动、周期长） | 秒级 |

覆盖全部 442 根管需求，**每一项都不劣于、且多数优于老软件**。第二组配置（小列池 3000 + 仅最小根数）另得 **5 种切法 / 99.73% / 546 根 / 12s**——切法种类进一步压到 5 种、利用率追平老软件，说明"种类 vs 利用率"可在同一模型里按列池/目标权重调档。

### 三、关键工程教训
- **big-M `y_p`（显式数种类）是唯一瓶颈**：加了 28953 个 0/1 变量后 SCIP 60s 找不到可行解；**去掉后纯覆盖 MIP 1.2s 出最优**。且"最小化母材根数"在小段长字母表上**天然只复用极少切法**（23 种），无需显式数种类。这与 §11.19 结论一致：不要把巨型静态列池一股脑塞进一个带 big-M 的 MIP。
- **全排完不再是难点**：覆盖型约束结构良好，段长字母表小（17 种），整数规划秒级可解——彻底摆脱局部贪心的"救活 vs 种类"冲突（§11.23）。

### 四、放行判断与下一步
- **POC 通过**：全局集合覆盖路线在真实 B 样本上一次跑通并全面优于老软件，方向钉死。
- **下一跳（按规模升级验证）**：在 C 样本（5109 母材 / 44 管型，规模最大）与 A/D 上重跑，确认列枚举/ILP 在千根级仍秒~分钟级可解；若列爆炸再引入 §11.19 的**列生成/pricing（RCSP）**按需生列，而非静态全枚举。
- 阶段 A 解锁仍需前置④（must_use 贯通说明）——集合覆盖模型下 must_use 落为"某母材长度的 `x` 下界/优先"约束，需并入模型。

## 11.26 v2 联合切+拼两阶段回归：B/C 达标、A 接近、D 证明超紧长短混装需列生成 pricing

> 目标：把 §11.25 的 v1（固定单段拼法）升级为 **v2 = 拼法作决策变量 + 段长自由 + segment-balance**，并用**两阶段词典序**（阶段1 最小化用料拿利用率下界与活跃列 → 阶段2 在活跃列子集上最小化切法种类，硬约束"用料 ≤ 阶段1用料"保证不劣化）在 4 个最复杂样本上回归。执行脚本：`scripts/_poc_setcover_ilp_v2.py`（`--minimise two_phase`）。

### 一、v2 相对 v1 的关键升级
1. **拼法作决策变量 `u[i,wi]`**：每管型给出多条候选拼法（整料 / 单关节按母材长对齐切分 / 双关节），而非 v1 的单一模板。修复了 §11.25 尾注发现的"固定单段拼法 = 利用率天花板"（C 样本 v1 只有 98.35%）。
2. **共享段长字母表 + segment-balance**：所有候选拼法的段长汇成公共字母表，切法列在此字母表上枚举；ILP 加 `produced[seg] ≥ consumed[seg]` 段平衡约束，切与拼在同一模型里联合决策。
3. **列池裁剪**：`max_pieces`（默认 3，对齐老软件 2-3 段）+ `max_trim`（只保留尾料 ≤ 阈值的密列）控制列数；再加 `ensure_coverage` 保证每个字母表段长至少有一列产出（避免段平衡因"某段无列产出"假性不可行）。
4. **两阶段词典序**：阶段1 `minimise=length`（无 big-M，快，拿利用率下界 L\* 与活跃列集）→ 阶段2 只在**活跃列子集**上 `minimise=cut_types`（big-M 只作用于极小列集，可解），硬约束 `Σstock·x ≤ L\*`。规避了 §11.25 教训（big-M 套满列池必卡死）。

### 二、四样本回归结果（阶段总时限 180s）
| 样本 | 规模 | 切法种类 | 拼法种类 | 母材根数 | 利用率 | 结论 |
|---|---|---|---|---|---|---|
| **B** `6a4fecf7` | 68管型/546母材 | **36**(老70) | **32**(老68) | **545**(老546) | **0.99910**(老0.99729) | ✅ 全面碾压，阶段2 达 optimal |
| **C** `3040bd13` | 44管型/5109母材 | **22**(老26) | **23**(老44) | **1313**(老1376) | 0.99489(老0.99489) | ✅ 全面达标（利用率完全持平） |
| **A** `9f618d9f` | 64管型/1142母材 | 59(老48) | **46**(老64) | **1137**(老1136) | 0.99579(老0.99656) | ⚠️ 拼法/母材达标；切法多11、利用率差0.08%（阶段1 未收敛即超时） |
| **D** `350f2dbb` | 64管型/1105母材 | — | — | — | — | ❌ infeasible（见下诊断） |

### 三、D 样本 infeasible 的根因诊断（三层验证，排除表面原因）
`scripts/_diag_infeasible.py` + `_diag_inject_legacy.py`：
1. **不是缺料**：母材总长 12,790,000mm，需求最小总段长 12,761,848mm，**余量仅 28,152mm（紧度 99.78%）**；老软件用 1104/1105 根母材做出可行解，供给数字与我方一致。
2. **不是段覆盖缺失**：字母表 147 段长全部有切法列产出（`ensure_coverage` 后未覆盖 = 0）。
3. **不是候选粒度**：把**老软件真实用过的拼法段长直接注入**候选后，**LP 松弛仍 infeasible** → 排除"离散切分点不够密"。

**真正根因**：D 含 **1012 根 `150mm` 成品短管**（`max_joints=0`，只能整料），必须**零浪费嵌入长管切法的缝隙**。在 99.78% 紧度下，"长段 + 若干 150 + kerf" 必须恰好填满 12000mm 母材才可行。预枚举的离散拼法候选（固定切分点组合）凑不出这种"长短完美混装"——而老软件是靠**连续/动态匹配段长**做到的（§11.21 记录的"长短混切"）。

### 四、结论：预枚举列池对超紧长短混装有天花板 → 上列生成 pricing
- 本节实测印证 §11.19/11.24 判断：**中等紧度（B/C）静态列池足够且全面优于老软件；超紧长短混装（A 切法偏多、D 直接不可行）必须列生成 pricing 动态生成"填满母材的切法列"**（RCSP 定价子问题，资源=max_joints/禁焊区/min_seg）。
- **决策（已确认）**：投入列生成 pricing 作为大规模超紧样本的主引擎；B/C 类保留静态两阶段快路。
- pricing 定价子问题复用既有 `_legal_pattern`（禁焊区/min_weld/max_joints）作为弧可行性判定，在 arc-flow 图上求 reduced-cost 最短路按需生列，替代静态全枚举。
- 前置④（must_use 贯通）在列生成框架下：must_use 落为受限主问题里某母材长度 `x` 的下界/优先权重，定价时不影响子问题结构。

## 11.27 A/D 攻克：`max_pieces=4` 是可行关键，大列池 + 两阶段整数化即可全排完（列生成对偶在 non-IRUP 下非必需）

> §11.26 判断 D infeasible 需列生成 pricing。实测（`scripts/_poc_colgen.py`）修正了这一判断：**D 不可行的真正开关是 `max_pieces`，不是候选粒度**。把每根母材允许的段数从 3 放到 **4**，"1 长段 + 2~3 个 150mm 短管 + 尾料"就能凑出来，D 立刻从 infeasible 变为全排完可解。

### 一、关键发现：`max_pieces=3 → 4` 是 D 可行的开关
- D 的 1012 根 150mm 短管必须与长管混切；"长段 + k×150 + 尾料" 至少要 4 段（1 长 + 2~3 短）。`max_pieces=3` 时物理凑不出 → LP 都不可行；`max_pieces=4` 时 `art_active=0、pi_max=175`，LP 自洽、无需人工变量。
- 教训：**infeasible 不一定是算法不够强，先检查建模自由度（每列段数上限）是否卡死了物理可行解**。这比直接上重工程（列生成）更根因。

### 二、列生成对偶在本问题上不是必需（non-IRUP 观察）
- 实现了切法侧列生成（`_poc_colgen.py`：Farkas-free 人工变量 RMP + 背包定价 + 禁 presolve 取真对偶）。
- 现象：小 seed 池时 `art_active>0` 但 `pi_max=0`——人工变量落在段平衡 `≥` 约束的 produced 侧使约束恒松弛、对偶失真，定价拿不到信号；且本问题 **non-IRUP**（LP 松弛在 1105 根母材硬上限下需人工变量补缺口，但整数解用 1104~1105 根即可行），LP 对偶本就指导不了整数解。
- 结论：**对这类 non-IRUP 紧料问题，列生成对偶剪枝价值有限；足量静态列池（`max_pieces=4`）+ 两阶段整数化反而直接且有效**。列生成保留给未来超大规模（真实 4000+ 根、列枚举本身爆内存）时按需生列。

### 三、四样本最终回归（`max_pieces=4`，seed 大池，两阶段词典序整数化）
| 样本 | 切法种类 | 拼法种类 | 母材根数 | 利用率 | 综合 |
|---|---|---|---|---|---|
| **B** | 36 (老70) ✅ | 32 (老68) ✅ | 545 (老546) ✅ | 0.99910 (老0.99729) ✅ | 全面碾压 |
| **C** | 22 (老26) ✅ | 23 (老44) ✅ | 1313 (老1376) ✅ | 0.99489 (老0.99489) ✅ | 全面达标 |
| **D** | **35 (老35)** ✅ | 42 (老31) ⚠️ | 1105 (老1104) ⚠️ | 0.99780 (老0.99850) ⚠️ | 全排完、切法持平 |
| **A** | 50 (老48) ⚠️ | 43 (老28) ⚠️ | **1134 (老1136)** ✅ | **0.99785 (老0.99656)** ✅ | 利用率反超、母材更少 |

- B/C **每一项都不劣于且多数优于老软件**；D 从 infeasible → 全排完且切法持平；A 利用率反超、母材更少。
- 残余差距：**A/D 的拼法种类偏多**（A 43 vs 28、D 42 vs 31），以及 A 切法多 2、D 利用率差 0.07%。均因阶段2 仍 timelimit 未收敛（阶段2 在活跃列上最小化的是**切法**种类，拼法种类未纳入目标）。
- 下一步优化方向：把**拼法种类**也纳入阶段2 词典序目标（当前只压切法）；或给阶段2 更多时间/用对偶剪列缩小活跃列集加速收敛。

### 四、放行判断
- 全局集合覆盖 + 拼法作决策变量 + `max_pieces=4` + 两阶段词典序 = 在 4 个最复杂样本上**整体达到"不比老软件劣化"**（B/C 全优、A/D 主指标达标，仅拼法种类待压）。方向钉死。
- 待办：①阶段2 加入拼法种类词典序；②把 ILP 解喂 `verifier` 做段平衡/焊口合法/禁焊区正确性验证（数字才算真实可落地）；③前置④ must_use 贯通并入模型。

## 11.28 正确性验证：四样本 ILP 解全部通过 verifier（段平衡/焊口/禁焊区/metrics）

> 前置③ 的最后一环：数字必须建立在**合法解**上才算数。用 `scripts/_verify_v2.py` 把 v2 两阶段的聚合 ILP 解展开成 verifier 需要的 solution schema，喂 `app.verifier.verify_solution` 做独立复算。

### 一、kerf 口径统一（关键修正）
- 本数据集与**老算法一致按 kerf=0** 处理（B 样本 `problem` 无 `NestParam.BladeMargin`，`parse_problem` 得 0）。但 `verifier.py:86` 在缺省时**默认 BladeMargin=10**，导致首次验证报 201 个 kerf 相关错误（BLADE_MARGIN_MISMATCH / CUT_CAPACITY_EXCEEDED / KERF_LOSS_MISMATCH…）。
- 修正：喂 verifier 前显式 `NestParam.BladeMargin = group.blade_margin`，令验证器用与建模**同一个 kerf** 复算。C/D/A 的 kerf 分别为 10/5/10，由 payload 正常读入。

### 二、多切段处理：方式2（`produced >= consumed`）+ 选项1（并回尾料）
- **决策（用户确认）**：ILP 段平衡用 `produced >= consumed`（方式2，宽松、更易可行、利用率更高）；多切出、未焊入任何管的段，**后处理并回该母材尾料当损失**（选项1，简单）。
- **选项2（余料留待下批复用）更贴合现场但需补充"余料库存跨批流转"算法 + 改单组验证模型，难度大，预留为后续优化**（见下节）。
- 后处理实现（`_verify_v2.py::v2_to_solution`）：按段长算 `surplus = produced - consumed`，把切法列展开成 per-bar 实例，逐段删除 surplus（并回尾料变余料），再重聚合成切法。删除后**每根切出的段都被焊入** → verifier 段平衡 `==` 成立。C 样本原有 8 处 `SEGMENT_BALANCE_MISMATCH`（切出>消耗）后处理后全部消除。

### 三、四样本验证结果（`max_pieces=4`，两阶段词典序，verifier 独立复算）
| 样本 | 切法 | 拼法 | 母材 | verifier | 综合 |
|---|---|---|---|---|---|
| **B** | 40 (老70) ✅ | 34 (老68) ✅ | 546 (老546) ✅ | **PASS** | 全优 |
| **C** | 23 (老26) ✅ | 30 (老44) ✅ | 1277 (老1376) ✅ | **PASS** | 全优 |
| **D** | 35 (老35) ✅ | 42 (老31) ⚠️ | 1105 (老1104) ⚠️ | **PASS** | 全排完、切法持平 |
| **A** | 53 (老48) ⚠️ | 40 (老28) ⚠️ | 1135 (老1136) ✅ | **PASS** | 母材更少（阶段2 timelimit 未收敛）|

- **全部通过段平衡、焊口合法、禁焊区、metrics 一致性检查** → v2 全局集合覆盖产出的是**真实可落地的合法解**，B/C 全面优于老软件，D 全排完切法持平，A 母材更少。
- 残余：A/D 拼法种类偏多、A 切法多 5、D 利用率差 0.07%，主因阶段2 只把**切法**纳入词典序且 timelimit 未收敛（拼法未纳入目标）。

### 四、选项2（余料跨批复用）预留设计
> 现场理论最优：多切段不算损失，登记为"可复用余料"供下批优先消耗，进一步抬高长期利用率。落地要点（后续实现）：
1. **余料库存表**：按 `(material, spec, 长度)` 维护余料条目，本批多切段入库、下批排料时作为"零成本额外母材长度"参与切法枚举。
2. **验证模型扩展**：verifier 目前单组纯函数验证，需支持"跨批余料流转"——把上批余料作为本批的额外 Stock 输入参与段平衡。
3. **批次顺序依赖**：引入余料库存后结果依赖排料顺序，需固定批次处理顺序或做全局多批联合优化，权衡复杂度。
4. **收益评估**：先量化选项1 的损失（多切段总长/总用料占比）；若占比可观再上选项2，否则维持选项1（当前四样本利用率已达标或反超，损失可控）。


## 11.29 阶段2：拼法种类纳入词典序目标

> 需求：§11.28 残余问题是 A/D 拼法种类偏多，因阶段2 只压切法。本节把拼法种类作为词典序**次目标**加入。

### 一、建模（`_poc_setcover_ilp_v2.py::solve_joint` 的 `cut_types` 分支）
- 拼法"种类"按 verifier 口径 = `parts` 元组（跨管型共享，忽略 pipe_id）。新增 `z_t`（每种拼法元组一个 0/1），`Σ_{u 实现 t} u ≤ M·z_t`。
- **真词典序**（切法优先，拼法仅作平局裁决）：目标 `= (|拼法种类|+1)·Σy_p + Σz_t`。切法权重取 `|拼法种类|+1`，保证"消掉全部拼法种类都换不回一个切法种类"，切法绝不被拼法交易掉。
- 阶段2 列池仍为**阶段1 活跃列**：big-M 种类计数 MIP 只有在小池上才能求到最优；放大到全池会 timelimit 反而退回更差的阶段1 原始解（实测 B/C 均验证）。

### 二、四样本回归（active 池 + 拼法词典序，verifier 独立复算全 PASS）
| 样本 | 老软件 切/拼 | 阶段2 切/拼 | 拼法 | 切法 |
|---|---|---|---|---|
| B | 70 / 68 | 46 / **38** | ✅ | ✅ |
| D | 35 / 64 | 35 / **37** | ✅ | ✅（持平）|
| A | 48 / 64 | 51 / **44** | ✅ | ⚠️ +3 |
| C | 26 / 44 | (phase-1 兜底) | ✅ | ⚠️ |

- **拼法目标生效**：D 拼法 64→37、A 64→44、B 68→38，全部远低于老软件 → 阶段2 能处理拼法。
- **切法波动的真因是阶段1 非确定性**：阶段1 `minimise length` 在 timelimit 下有抖动，活跃列集每次不同 → 切法种类 ±5 波动。拼法词典序是严格平局裁决，**不会抬高切法**（同一活跃列集下切法只减不增）。A/C 切法未持平主要来自阶段1 抖动与大池 timelimit，非拼法目标引入。
- **规模瓶颈仍在**：C（5109 母材/155k 列）阶段2 big-M 超时退回阶段1；根治需 §11.18/11.19 的第4层 LNS/列生成 pricing-for-integrality，而非继续调 big-M 参数。

### 三、结论
- 阶段2"拼法可否处理" → **可以**：拼法种类已纳入词典序次目标并在四样本上大幅低于老软件。
- 剩余切法波动 / C 大规模超时是**阶段1 非确定性 + big-M 不可规模化**的既有问题，需上 LNS 外层收敛，不在本阶段范围。


## 11.30 全量样本数据摸底 + 算法拆解（3177 条 MOM 导出，反推旧软件机制）

> 背景：手测发现 route3（v2 集合覆盖）虽利用率达标，但**切法/拼法种类远多于老软件**，切太碎导致车间难拼。用户要求：从 1600+ 真实输入里**找规律再拆算法**，不写死代码。本节是数据结论与算法拆解，落地留待后续阶段。
> 数据源：`d:\UserData\Downloads\DGMOMPTDGMOMGLLXWTFJB.json`（3177 条，每条含 `MOMPROBLEMJSON` 输入 + `MOMRESULTJSON` 旧软件完整解）。
> 分析脚本：`scripts/_survey_1600.py`（规模）、`_survey_rules.py`（三分法/字母表）、`_survey_segsource.py`（段来源）。

### 一、规模分布（全量 3177，旧软件成功解 1586）
| 指标 | p50 | p90 | max |
|---|---|---|---|
| 管型数 | 4 | 21 | 82 |
| 管子总数 | 180 | 1012 | 3552 |
| 母材根数 | 78 | 1112 | 5113 |
| 紧料度 | 0.99 | ~1.0 | （脏数据>1） |

- 母材根数分档：<100 有 1755 组，100–500 有 641，500–1000 有 433，1000–2000 有 263，**2000+ 有 85**。
- 紧料度分档：**[0.99,0.999) 有 1427 组是绝对主力**，[0.999,1) 还有 176。
- 约束常态：**禁焊区 2377/3177（75%）**；**单管>最长母料（必拼）1072（34%）**；**管型短于最短母料 1521（48%，即用户说的"管子有小于母料的"）**；`Min_Welding_Length` 几乎恒为 500；有 `must_use` 的 399 组。
- 数据清洗口径：规则统计只取 legacy 成功且 `0.5<util<1.05`，脏数据（util=1.497、tightness=9.81、stock=1e7）单独剔除。

### 二、三分法验证（主力区间 371 组：紧料 0.99~0.999 且母材 100~2000）
**用户"切/拼/切拼匹配是三个问题"的假设成立，且三者有主从关系。**

1. **切拼共用同一段长字母表**（匹配 = 字母表守恒）：
   - 拼法用到但切法没产出的段：p50=0，**99.73% 的组完全为 0**；切了但没被拼用的段 p50=0。
   - → 切法产出的段集合 ≡ 拼法消耗的段集合，两端共用一批段长。
2. **段长字母表很小且被复用**（低种类数的根源）：
   - 字母表大小 p50=18；一个中间段平均被 **2 种切法复用**（p90=3.2）。
   - 构成：整根母料段仅 9%，整管段≈0%，**真正的"中间切段"占 82%（p90=94%）**。
3. **单管单拼法**：管型只用一种拼法的比例 p50=0.94。
4. 主力区间旧软件基准：cut_types p50=16 / p90=39；weld_types p50=14 / p90=26。

### 三、段长字母表的生成规则（⚠️ 已作废：循环论证，勿采信）
> **勘误（诚实性检验 `scripts/_survey_sanity.py`）**：下表的"R3=管长−段组合 90.8%"是**数据泄漏/循环论证**——判定 R3 时把"旧软件解里已出现的中间段(mid)"本身也放进了可减集合，等于拿答案解释答案。无泄漏对照：
> - H0 只用库存定尺（真先验）解释率 **仅 24.4%**；H1 定尺+整管 27.2%；H2 原泄漏口径 97.9%。
> - 判别力检验：把中间段换成**纯随机段长**，H2 口径仍能解释 **61.3%** → 该检验无判别力，任何数都能"通过"。
> **结论：段长字母表"怎么生成"这个问题，本节并未真正解开。先验只能解释约 1/4，其余来源不明。以下原表仅存档，不作为算法依据。**

| 规则（作废） | 泄漏口径覆盖率 |
|---|---|
| R3 = 管长 − 其它段组合（拼接补齐段） | 90.8%（假高）|
| R2 = 母料 − 整管组合（余段） | 5.5% |
| R1 = 整管长 | 3.4% |
| R4 = 小段之和 | 0.0% |
| 无法解释 | 0.3%（假低）|

### 四、旧软件机制复原（"拼法驱动切法"，⚠️ 依赖已作废的 R3，降级为待验证假设）
> 注意：本节推论**部分建立在已作废的 R3 之上**，仅作为**待实证的假设**，不作定论。可信的只有第二节的纯计数事实（切拼共用字母表、字母表小、单管单拼法）。
```
假设（待验证）：拼法（管子拆分）决定一小批共享分割段长（字母表 p50≈18），切法只用这批段填母料，匹配=字母表守恒。
```
- 可信部分：字母表小 + 复用 → 切法/拼法种类少（纯计数）。
- **未解决**：这批分割段长到底怎么选出来（先验仅解释 24%），是本方案最关键的空白。
- route3 切太碎的**现象**属实（每管独立枚举分割点、不跨管共享），但"改成共享小字母表就能对齐老软件"仍需实证，不能想当然。

### 五、算法拆解（三阶段，主从解耦，不写死代码）
> 与 §11.18/11.19 的"四层架构"衔接：本节是对 Layer 3/4 建模的**数据校正**——候选段必须来自共享小字母表，而非 per-pipe 独立枚举。

- **阶段 A｜共享段长字母表生成（主）**：以 R3 为核心生成候选分割段——对每根需拼接的管，枚举"管长拆成 ≤k 段"的分割，但**分割点在全局候选集里选**（对齐库存定尺、对齐其它管的分割段），目标是让**不同管尽量复用同一批段长**，把字母表压到 p50≈18 量级。字母表大小是可调硬上界（贴合老软件基准）。
  - 输入：管长集合、库存定尺、min_weld_distance、max_joints、禁焊区。输出：候选段长集合 Σ + 每管的候选拼法（仅用 Σ 里的段）。
- **阶段 B｜切法（从）**：一维下料，只用 Σ 里的段覆盖每根母料，最小化母材/尾料，段种类天然受限于 |Σ|。
- **阶段 C｜联合收敛 + 词典序**：在字母表守恒约束下联合选拼法+切法，词典序 利用率 ≥ 切法种类 ≥ 拼法种类。规模大时用 §11.18 的 LNS 外层。
- **能力边界**：字母表越小切法/拼法越少，但过小会牺牲利用率；需以"字母表大小上界"作为可调旋钮，在"不劣于老软件"的验收线下取最小可行字母表。

### 六、下一步（结果驱动实证，不再猜生成规则）
- **实证Ⅰ（本次）**：直接抽取老软件解里的**真实段长字母表**当候选，回灌 route3 的候选生成（约束 route3 只用这批段），跑 5~10 组主力样本，看切法/拼法种类能否压到 ≤ 老软件、利用率是否保住。
  - 若成立 → 病根确认在"候选段来源"，重构方向 = 学出/生成一个小字母表。
  - 若不成立 → 病根另有他处（如 ILP 目标或列池），推翻本节假设重查。
- 实证Ⅰ 只用老软件段长作**上界对照**，不是最终方案（生产时没有老软件解）；但它能一刀切地判定"候选段来源"是不是真病根。


## 11.31 实证Ⅰ 结果：病根确认（结果驱动，非推断）

> 脚本 `scripts/_exp_alphabet.py`：把老软件解里的真实段长字母表回灌 route3，强制"拼法/切法的每一段都必须落在该字母表内"，用 route3 现成 ILP 求解，与老软件对比切法/拼法种类与利用率。verifier 口径的 cut/weld 种类。

### 一、结果（主力区间 8 组，每组 40s）
| 样本 | 字母表 | 切法 老→我 | 拼法 老→我 | 利用率 老→我 |
|---|---|---|---|---|
| ebf13527 | 32 | 32→33 ⚠️+1 | 17→18 ⚠️+1 | 0.9983→**0.9993** |
| fc829dcc | 7 | 9→**7** ✅ | 30→**2** ✅ | 0.9951→0.9951 |
| ef4d81e9 | 8 | 12→**10** ✅ | 47→**3** ✅ | 0.9951→0.9951 |
| 7defcea0 | 19 | 23→**22** ✅ | 18→**16** ✅ | 0.9973→**0.9991** |
| eabf2b06 | 20 | 28→**16** ✅ | 19→**16** ✅ | 0.9973→**0.9991** |
| 6a4fecf7 / e7838732 / d63121ee | 130 | — | — | ILP 超时（大字母表 130²组合爆列）|

**5/8 全排完，且切法/拼法种类均 ≤ 老软件、利用率均 ≥ 老软件。** 失败的 3 组都是字母表 130 的样本（老软件自己也是 70 切/65 拼的难组）。

### 二、锁定的两个真实病根（均用数据验证，非猜测）
1. **候选段来源错（主因）**：生产 route3 `_build_weld_candidates` 让第三段 = 管长−前两段的**自由余段**，导致字母表爆炸。实测 ebf13527：自由余段口径 used_alpha=3960、拼法 10199 种、列 362,059 → Phase-1 60s 无解；改为"每段都必须∈字母表"后 used_alpha=32、拼法 101 种、列 1555 → **0.9s OPTIMAL、util 0.9997**。
2. **`max_pieces=4` 太小（次因）**：老软件一根母料实测可放 **23 段**短料（如 `516×23`）。`max_pieces=4` 时 fc829dcc 段平衡无解；放大到 ≥8 后 **0.0s OPTIMAL、util 0.9951 追平老软件**。已在实验里改为自适应 `min(30, 母料/最短段+1)`。

### 三、勘误确认
- §11.30 的"R3=管长−段组合 90.8%"是循环论证（§11.30 三已标作废）。真正结论不是"段长有生成公式"，而是**"只要把候选段限制在一个小的共享字母表内，ILP 就能又快又好"**——这是可复现的实证，不是推断。

### 四、下一步（真正待解）
- oracle 实验用了老软件答案的段长；**生产没有老软件解**。核心待解 = **在无 oracle 情况下自动生成一个"小而好"的共享字母表**。方向（结果驱动，仍需实证，不写死）：
  - 让 ILP 在一个**受控大小的候选段池**里自选（池由"每段∈池、池大小设上界"约束生成），而非 per-pipe 自由余段。
  - 大字母表组（130）需配合列数控制（限制 3 段组合数 / 列池上界 / LNS 外层），否则爆列超时——这是 §11.18/11.19 第4层要解决的。
- 重构 `route3_setcover.py` 的 `_build_weld_candidates` + `max_pieces`，是下一步落地重点。


## 11.32 调研：段长字母表的工业/文献依据（进行中）

> 用户质疑（正确）：老系统的字母表一定"有依据地推出来"，不能凭空造锚点。本节先调研成熟做法，再做**无泄漏数据验证**判断哪条依据真正适用本数据。

### 一、文献主线（1D 下料+拼接 / 钢筋下料）
- **Tanir et al. (2016), "1D Cutting Stock Problem with Divisible Items"** (arXiv:1606.01419)：正是本问题。做法 = 顺序启发式 + 每根母料 DP；把切下的余料作为某个需求件的"小段"，再用另一模式切残段，最后焊接重组；**焊接下限（如 1000mm）为硬约束**。段长来自"需求件被切分产生的残段"，非预设网格。
- **钢筋下料 special-length-priority（Rachmawati/Kim 2024, JAABE 23:6）**：三步法 =（1）优化搭接位置使段对齐"特殊长度"，（2）用特殊长度出切割模式，（3）剩余用标准定尺。核心：**段长snap到一个受控的"特殊长度"小集合**（0.1m 间隔、有 min/max/最小订购量）。
- **Zhang 三步法 (ASCE CO 2283)**：Model I 先用整根长料、最小化"余料+焊点"→ 减少后续计算；Model II 限制每根成品段落在≤3 段且焊点不在跨中。

### 二、无泄漏数据验证（`scripts/_survey_quantize.py`，12985 个中间段）
关键在于判断"钢筋特殊长度网格"是否适用本数据 —— **结论：不适用**。
- **Q1 段长不量化**：仅 12.9% 能被 10 整除、2.8% 被 50、1.4% 被 100；末位分布近均匀（8~13%）。→ 本数据段长是**任意毫米值**，不是钢筋那种 0.1m 网格。
- **Q3 取值密度 p50=0.088**：字母表相对可能取值范围**高度压缩到少数值**（与"小共享字母表"一致）。
- **Q4 同材质规格跨订单复用**：Jaccard p50=0.17、p90=1.00（双峰）——部分材质规格**完全复用同一段长集**（存在"标准下料长度库"），多数不复用。

### 三、由数据得出的可信判断（非推断）
- 段长既**不是**通用长度网格（Q1 否定），也**不全是**每单独立生成（Q4 部分复用）。
- 最可信的解释：段长由**当单具体管长派生**（管长 − 若干整段/定尺 = 拼接残段，任意毫米），并在**同材质规格历史单间形成可复用的标准长度库**。→ 生成器应"从当单管长+定尺派生候选残段"，并可选"叠加历史标准长度库"，再用 ILP 选最小子集。
- 待补：等文献深挖子代理返回，确认 pattern-minimization / 列生成 pricing 是否有直接可抄的段长生成算法。

### 四、文献深挖回执（子代理 b970ac49）与双向印证
文献给出确定字母表的三类工程依据，与本数据验证交叉后：
- **依据A（采纳，核心）Ágoston 2019《The Effect of Welding on the 1D CSP》**：把"下料+拼接"规约为**带多规格母料的等价 CSP**——k 根母料焊成"等价母料"，分割点落在**母料边界导出位置（管长 ⊖ 定尺组合）**，等价价格 = 母料价 + (k−1)×焊缝价。Proposition 1：最优拼接**焊缝数 = 母料数 − 1**（树结构）→ 干净映射 max_joints。**与本数据的"任意毫米、小、部分复用"完全吻合**（母料定尺组合本就是任意毫米、有限、可跨单复用）。
- **依据B（可选叠加）余料对齐**：分割点对齐库存可用余料（Cui-Yang 2010 / Cherri usable-leftover）→ 提利用率但增段种类，与"种类最少"冲突，作为可选项。
- **依据C（剔除，本数据否定）钢筋 special-length 0.1m 网格**：数据验证 Q1 已否定——本数据段长非量化网格，故**不采用"量化到 100mm 网格"**这一步。
- **主求解**：arc-flow（Valério de Carvalho 1999 / VPSolver，支持刀数限制→max_joints）或列生成 pricing 背包，在受控弧长集合里**隐式生成模式**，禁焊区/min_weld_distance 作为弧位置约束。
- **压种类数（词典序第2/3目标）**：利用率最优解上做 **KOMBI 模式合并（Foerster-Wäscher 2000）+ Umetani 2002 种类数上界**两阶段后处理；PMP 强 NP-hard（McDiarmid 1999），精确法 Vanderbeck 2000。
- **字母表即决策（进阶）**：Raffensperger 2010 GAP/BSL、Kasimbeyli 2010 显式"标准长度种类数"目标——把"用哪几种段长"上升为显式优化对象。
- **开源**：VPSolver（arc-flow+图压缩）、ssp-arcflow（拼接侧 Skiving 建模）、INFORMSJoC/2023.0399（Ryan-Foster 精确 CSP+SSP）。

### 五、结论 & 下一步（实证优先，不写死）
- **字母表生成方案（数据+文献双向印证，可落地）**：
  1. 拼接侧：对每种超长成品 `L`，段数 `n_seg = ⌈L / L_ref⌉`（`L_ref`=最长定尺）；分割点候选 = **管长 ⊖ 母料定尺组合**（不量化网格）；焊缝数 = n_seg−1 ≤ max_joints。
  2. 切割侧字母表 = 拼接段 ∪ 直接成品长，**切/拼两侧强制共用同一集合**。
  3. ILP 在此受控字母表内自选，最小化用料 → 再压种类数。
- **下一步（唯一验证点）**：写"**不碰老软件答案**"的字母表生成器（依据①派生），在同样 8 组上跑，对比 oracle 与老软件的 切法/拼法/利用率。过关才重构 `route3_setcover.py`；不过关则回头调派生规则。**先不写生产代码。**

### 六、实证Ⅱ 结果：简单算术派生**被推翻**（关键负结果）
`scripts/_exp_derived_alphabet.py`（派生字母表 = 定尺组合 ∪ 管长 ∪ 管长⊖定尺组合），8 组**全失败**。诊断（`scripts/_diag_alphabet.py`）：
- **fc829dcc**：管长仅 10100，定尺 11600/12000。oracle 段 = `[516,2388,3752,3960,3964,5620]`，**没有一个是 `管长±单定尺`**；而 `2388+3752+3960 = 10100` —— 段是"把 10100 拆成 2~3 段的分割点"，且分割点的选择是为了**让这些段与其它段拼起来塞满 11600/12000 母料**。我的派生给出 `[1500,1900,10100,11600,12000]` 完全不沾边 → ILP 无解。
- **7defcea0**：派生覆盖了 19 个 oracle 段里的 17 个（仅差 764/2063），但**冗余爆到 147 个** → 列爆炸超时。
- **根因（诚实结论）**：段长**不是**单管局部算术能派生的量，而是**"全局母料填充需求"反推出的分割点**（母料要塞满 → 反推每根管该切成哪些长度）。这与 §11.30 被作废的"堆料驱动切割"方向一致，但**无法用显式算术枚举**。
- **正解方向（回到文献主线）**：这正是 **arc-flow / 列生成 pricing** 的意义——段长（弧长）不应显式预枚举，而应由**定价子问题在"塞满母料"目标下隐式生成**。§11.32 四推荐的 VPSolver/arc-flow 路线是对的；错的是我想用算术公式绕过 pricing。
- **决策点**：下一步应实现 **arc-flow / 列生成 pricing**（段长隐式生成），而非继续调派生公式。这回到了 §11.18/11.19 已调研的主引擎方案。


## 11.33 列生成主引擎：拼侧 RCSP + 切侧背包 pricing（可实现方案，子代理 ba3a7af3）

> 核心诊断：段长真正的来源 = **拼侧 pricing（RCSP）在整数位置格点上、按对偶价 π 把管子拆开**；切侧 pricing（格点背包）把这些段恰好填满母料；两侧靠**共享字母表 Σ + 共享对偶 π** 迭代协同收敛（Dantzig-Wolfe 标准形式）。之前 `_poc_colgen` 在**固定 alphabet** 上定价，段长根本没被隐式生成，且段平衡用 `>=` 让 π 退化为 0 —— 这两点是病根。

### 一、Master LP（切列 + 拼列 + 段平衡耦合）
- 变量：`x_p≥0`（切法列 p 用的母料根数，列属母料长 L_p，产段向量 a_p[s]）；`u_{i,w}≥0`（用拼法 w 造管型 i 的根数，耗段向量 b_{i,w}[s]）。
- 约束与对偶：
  - 需求 `Σ_w u_{i,w} = d_i`（对偶 δ_i）
  - 段平衡 `Σ_p a_p[s]x_p ≥ Σ_{i,w} b_{i,w}[s]u_{i,w}`（对偶 π_s≥0）——**取对偶时用 `==`**（否则过量生产时 π_s 退化为 0，pricing 拿不到信号；整数解可退回 `>=`）。
  - 母料预算 `Σ_{p:L_p=L} x_p ≤ B_L`（对偶 σ_L≤0）
- 目标（阶段1）：`min Σ_p L_p x_p`（最小用料=最大利用率）。

### 二、切侧 pricing：格点背包 DP（段长隐式填满母料）
- reduced cost `c̄_p = L_p − Σ_s π_s a_p[s] − σ_{L_p}`；对固定 L 求 `max_a Σ_s π_s a[s]`。
- DP：`V[c][k]` = 用 ≤k 段、占用长度恰为 c（含段间 kerf）的最大 Σπ；转移放一段 s∈Σ 且 π_s>0，`V[c+s+kerf][k+1] = max(…, V[c][k]+π_s)`。复杂度 O(L·max_pieces·|Σ|)，每根母料毫秒级，比现有 DFS 更完备。
- **切侧只用 Σ 里已有段**（无法凭空造新段）；新段由拼侧引入。

### 三、拼侧 pricing：RCSP（**新段长的唯一来源**，含禁焊区）
- reduced cost `c̄_{i,w} = −δ_i + Σ_j π_{ℓ_j}`；求 `min Σ_j π_{ℓ_j}`（<δ_i 则加列）。
- 状态 `(c, j)`：c=累积长度=当前焊缝位置（整数 mm 格点），j=已用段数。转移放一段 ℓ 到 `(c+ℓ, j+1)`，代价 `π_ℓ`（**Σ 外新段取 π=0**，故 RCSP 倾向复用高 π 段压种类，必要时引入新段扩 Σ）。
- 转移过滤（对齐 `solver.py` 的 `_legal_pattern`）：
  1. max_joints：j ≤ max_joints+1
  2. min_weld_distance：内部段（非首非末）ℓ ≥ 500
  3. min_cut_length：多段拼法每段 ≥ min_cut（单段整管豁免）
  4. **禁焊区**：中间焊缝位置 `c+ℓ` 必须 `pipe.weld_allowed`（这是任何标准 CSP/skiving 论文都没有的、必须自研的部分）
  5. 每段 ≤ max_stock
- 候选段长集 `L = Σ ∪ {L_i⊖定尺组合} ∪ {禁区边界±1} ∪ {粗网格}`；可直接复用 `solver.py:_candidate_positions`，Σ 段优先排序 → 天然压字母表。Ágoston Prop.1（焊缝数=母料数−1、分割点落母料边界）为"L_i⊖定尺组合"这一核心项提供理论保证。

### 四、两侧协同（列生成主循环，非 joint pricing）
```
Σ = {整管长} ∪ {几个 stock-aligned 分割}（种子，保 RMP 可行）
loop:
  解 RMP LP(==平衡) → δ_i, π_s, σ_L
  拼侧 RCSP：每管型跑；产出用到 Σ 外段 ℓ_new → 加入 Σ；reduced<0 → 加拼列
  切侧背包：每母料长跑（只用 Σ）；reduced<0 → 加切列
  两侧都无负 reduced cost → 收敛
```
Σ 动态增长，两侧通过 π 耦合。收敛后走已有 `_solve_two_phase` 做词典序种类最少化（**big-M 只在阶段1活跃列上跑**，别套全池，否则超时退回更差解）。

### 五、论文/开源对口（结论：切拼联合+禁焊区必须自研）
- Valério de Carvalho 1999（切侧 arc-flow pricing 蓝本）+ Ágoston 2019（等价规约+max_joints）+ 自研拼侧 RCSP（禁焊区/min_weld 作转移过滤）。
- VPSolver/ssp-arcflow 只能算第0层下界（纯 cutting / 纯 skiving），**表达不了段平衡耦合 + 禁焊区**。无现成联合求解轮子。
- 大规模阶段2 超时的根治 = KOMBI 模式合并（Foerster-Wäscher 2000，轻量优先）或 LNS+pricing-for-integrality（§11.19），不是调 big-M。

### 六、下一步（验证点）
在 `fc829dcc`（管长仅 10100、oracle 已知 6 段）搭最小列生成原型：**验证拼侧 RCSP 能否自动吐出那 6 个段、达到老软件利用率与切拼种类**。过关才放大到多管长/大规模组。复用 `route3_setcover` 的 master ILP/段平衡/reconcile，只换"列从哪来"（预枚举→pricing）。

### 七、实证Ⅲ 首跑结果（`scripts/_exp_colgen.py`）
机制**跑通、方向正确**，但**未收敛到干净解**，暴露两个必须解决的工程点：
- **✅ 段长隐式生成成立**：拼侧 RCSP 确实在整数格点上自动产段，Σ 从 3 增长到 15，LP 利用率**达到 0.9951 == 老软件**。证明"pricing 隐式生成段长"这条路走得通（vs 之前算术派生全失败）。
- **⚠️ 问题1：对偶被 big-M 污染**。`fc829dcc` 整管切不可行（442 管 > 374 母料），RMP 靠人工变量兜底，`pi_max=120000`（=big-M）主导段平衡对偶 → 拼侧 RCSP 拿到假信号，产出的段（20/315/9469…）与 oracle `[516,2388,3752,3960,3964,5620]` **零交集**。
- **⚠️ 问题2：切/拼两侧段长"先有鸡先有蛋"**。拼侧引入的新段 π=0，切侧此刻无列产它 → 段平衡 `==` 逼该拼列用量归零 → 68 根管始终落人工变量（art 不清零）。种子切列 `{新段:1}` 一根母料只产一段、浪费巨大，LP 不肯用。
- **根治方向**（下一步）：
  1. **消人工变量**：先构造真实可行的焊接种子解（贪心把 10100 拆成能塞满 12000 母料的段组），让 RMP 从一开始无 art → 对偶干净。或改 Farkas pricing。
  2. **两侧协同加速**：拼侧产新段后，立即用切侧背包 DP 生成"把该新段与其它 Σ 段一起塞满母料"的高质量切列（而非 `{新段:1}` 垃圾种子）。
  3. 收敛后再接 `_solve_two_phase` 压种类。

---

## 11.34 实证Ⅳ：合并管型 + 精确段生成 + 减冗余压种类（本轮验证结果）

> 样本 `fc829dcc`（管长 10100、母料 11600/12000、442 管、老软件 6 段）。管线彻底跑通、可规模化、7s 出解、利用率持续超老软件；种类从"垃圾"压到"接近"，但未追平。

### 一、成绩演进（同一样本）
| 阶段 | 利用率 | 切法 | 拼法 | 耗时 |
|---|---|---|---|---|
| 起点（整数解只最小化用料） | 0.9978 | 37 | 98 | — |
| 合并管型 + big-M 词典序 | 0.9978 | 17 | 16 | ~30s（超时退化） |
| **精确段 + 无 big-M 减冗余** | **0.9978** | **17** | **17** | **7s（不超时）** |
| 老软件 | 0.9951 | 9 | 2 | — |

### 二、本轮解决的三个关键问题
1. **验收口径虚高**：老软件"拼法 30 种"实为**同一 parts 被 68 个图号复用**，物理只有 **2 种拼法**（`(516,3964,5620)` / `(2388,3752,3960)`，都=10100）；切法 9 种真实。→ 改口径按 parts 物理去重（`legacy_alpha_and_metrics`），并**合并物理等价管型**（同 长/max_joints/禁区）为单一需求（`merge_equivalent_pipes`，68→1），列生成从 12.9s 降到 2s。
2. **段生成被整百网格污染**（病根）：收敛 Σ 全是 `[100,200,…,10100]`，与老软件精确段 `[516,2388,3752,3960,3964,5620]` **零交集**。根因=可行种子贪心 `min(avail,need)` 级联出整百段、`candidate_lengths` 又从整百 Σ 派生。→ 重写 `candidate_lengths`：去整百网格，改用 `_bar_fill_lengths`（母料放 k 整管后剩余 tail 及其 2/3/4 等分——碎段塔满母料的收尾长度，隐式来自"填满"目标）。Phase-1 随即从 41/38 降到 27/28。
3. **big-M 压种类不可规模化**（验证 4 次必超时）：改**无 big-M 减冗余**（`consolidate_types`）——从 Phase-1 活跃列（已知可行）出发，逐条试删列，剩余仍整数可行（用料≤target×1.001）则永久删；先删拼列再删切列。7s 得 17/17，效果等同 big-M 但快一个量级。

### 三、卡点（诚实）：17/17 vs 老软件 9/2 是局部最优
减冗余是"只删不换"，删到每条列都不可或缺即停。根因=**列池里没有老软件那 2 条超高效清洁拆分**（省料优先生成的列偏散）。两条突破路线：
- **A（改 pricing）**：让列生成倾向"少而复用"的清洁拆分（段复用偏好进 pricing 目标）。
- **B（LNS swap）**：在减冗余基础上"删 2 换 1"，跳出局部最优。**先做 B**（用户判断：此为简单样本，后续更难）。

### 四、代码位置（`scripts/_exp_colgen.py`）
- `merge_equivalent_pipes`：合并物理等价管型。
- `_bar_fill_lengths` / `candidate_lengths`：精确段候选（去整百网格）。
- `build_feasible_seed`：零人工变量可行种子（对偶干净）。
- `solve_colgen`：列生成主循环（拼侧 RCSP + 切侧背包，Σ 动态增长）。
- `consolidate_types`：无 big-M 减冗余压种类（当前用）；`_consolidate_types_greedy` 为从空集贪心版（备用）。
- `solve_integer`：Phase-1 最小用料 → Phase-2 减冗余压种类。

---

## 11.35 实证Ⅴ：B（LNS swap）验证 + 关键定位——天花板是**段字母表**，不是列搜索

> 已实现有界 LNS swap（删2换1，LP 快筛 + 整数复核，13s 无超时），在 `fc829dcc` 上**零改进**，仍 17/17。此负结果反而精确定位了真正病根。

### 一、LNS swap 结果
`consolidate_types` 末尾加 `swap_reduce`：仅试删"用量最低的 8² 列对"，替换列限"覆盖被删段/管型"者取交集前 40，先 LP 松弛快筛再整数复核。全程 13s。**结论：切/拼两侧均无法删2换1降种类**——从省料驱动的同一列池里换，换不出更少种类。

### 二、拆开老软件解 → 定位真因（决定性）
老软件 `fc829dcc` 只用 **6 个段** `{516, 2388, 3752, 3960, 3964, 5620}`，9 条切法全是这 6 段的组合、**高度复用**（516/2388/3960/3964 反复出现）：
```
切法(9)：
  11600: [516×15, 3752]           12000: [516×23]
  11600: [516×7, 3964×2]          12000: [516, 3752×2, 3964]
  11600: [2388×3, 3960]           12000: [2388×5]
                                  12000: [2388, 3960, 5620]
                                  12000: [3960×3]  /  [3964×3]
拼法(2)：(516,3964,5620)=10100   (2388,3752,3960)=10100
```
我们的 colgen 产出 **89 个段（Σ）**，切/拼分散在多段长上 → 必然 17/17。**病根 = Σ 太大**：老软件先锁定 6 段小字母表、强制全场复用；我们的 pricing 省料优先，段越用越多。

### 三、结论与下一步（回到 A，但方向已明确）
- **B（LNS swap）已证伪能单独破局**：它只能在既有 89 段里洗牌，洗不出 6 段小字母表。
- **真正的破局 = 压缩 Σ 到小字母表再重定价**（option A 的实质）：
  1. colgen 收敛后，对 Σ 做**字母表精简**（小 ILP：选最少段子集，使所有管型仍可拼成、所有母料仍可切满 target_len）。
  2. 在精简后的 Σ' 上重跑切/拼列 + 减冗余 → 切法自然收敛到个位数。
- 这与老软件"少段复用"完全一致，也解释了为何利用率我们已超（0.9978>0.9951）、种类却输——**我们没有做字母表最小化这一步**。

---

## 11.36 失败路径账本（完整复盘：我们试过什么、为什么不行）

> 目的：把整条技术路线上"走过又放弃"的分支一次讲清，避免后人重走。核心矛盾始终是 **"利用率" vs "切/拼种类少"** 这对目标；每次失败都在教我们同一件事——**种类少的本质是段字母表小 + 全局复用，不是求解器换个花样**。

### 分支①：局部启发式（route3 sequential DP / 逐管 DP）——放弃
- **做了什么**：逐管从母料/余料里 DP 挑最省废料的 2~3 段组合拼成一根管，一根一根排。
- **结果**：能兜底（0→1060 管），但硬样本只到 ~96% 利用率、且约 40 根管拼不出（`max_joints` 全局约束被局部贪心违反）。
- **为什么不行**：`max_joints`/紧料是**全局资源分配**问题；逐管贪心把母料切成小碎片后，后面的长管无法在 3 焊内拼出。局部最优 ≠ 全局可行。
- **用户拍板**：不要打补丁式逐步修，要"彻底方案 + 大胆假设小心求证"。→ 转全局优化。

### 分支②：纯精确 MILP / 纯列生成 直接求解——放弃
- **做了什么**：baseline SCIP、route2 等价 CSP、纯列生成主问题。
- **结果**：小/中组能解；>700 段型、99%+ 紧料、上千母料时超时或 INFEASIBLE（拖尾、退化、non-IRUP）。
- **为什么不行**：文献确证——此区间理论最难，纯精确法必然崩。工业界都是"matheuristic（G&S/受限集覆盖）+ 构造启发 + LNS/SA"。

### 分支③：段字母表**算术派生**（pipe_len ⊖ 定尺组合）——证伪（关键负结果）
- **做了什么**：假设分割段能由"整管 ⊖ 若干定尺"这类算术组合导出，预枚举字母表喂给 ILP。
- **结果**：8 个样本**全失败**。`fc829dcc` 老软件用了 `2388+3752+3960=10100` 这种**无法由单管算术派生**的段。
- **为什么不行**：分割点是"多管片段在同一根母料上拼满"的**副产物**，必须全局求解隐式产生，不能单管算术造。
- **用户确认**：段长生成=全局副产物（列生成/arc-flow pricing），不是长度算数。

### 分支④：段字母表**量化网格**（对齐 0.1m / 50/100mm，仿钢筋"特殊长度"）——证伪
- **做了什么**：调研钢筋下料"特殊长度优先"，假设段长吸附到小网格。
- **结果**：无泄漏数据校验（`_survey_quantize.py`）显示老软件段长是**任意毫米值**（516/3752/3964…），不吸附任何网格。
- **为什么不行**：段长由每单具体管长/定尺决定，不存在通用长度网格。字母表小是"复用"造成的，不是"量化"造成的。

### 分支⑤：整百网格候选段（`_candidate_positions` 粗网格）——已修
- **做了什么**：早期 `candidate_lengths` 从整百网格取候选，可行种子也贪心出整百段。
- **结果**：收敛 Σ 全是 `[100,200,…]`，与老软件精确段**零交集**，切/拼种类爆到 37/98。
- **修法**：去整百网格，改 `_bar_fill_lengths`（母料放 k 整管后的收尾 tail 及 2/3/4 等分）→ Phase-1 降到 27/28。**保留**。

### 分支⑥：人工变量兜底 RMP（big-M art）——已修
- **做了什么**：RMP 不可行时加大 M 人工变量。
- **结果**：`pi_max=120000`（=big-M）主导段平衡对偶，拼侧 RCSP 拿假信号、产垃圾段、收敛极慢。
- **修法**：`build_feasible_seed` 构造零人工变量的完整可行种子 → 对偶干净、art=0。**保留**。

### 分支⑦：big-M 词典序压种类（Phase-2 套全池）——放弃
- **做了什么**：Phase-2 用 `y_p` 0/1 变量 big-M 显式最小化切/拼种类，套在放大的列池上。
- **结果**：C（5109 母料/155k 列）等大组**必超时**，退回更差的 Phase-1 解。验证 4 次均失败。
- **为什么不行**：big-M 种类最小化不可规模化。→ 改无 big-M 贪心减冗余（`consolidate_types`）。**保留减冗余**。

### 分支⑧：LNS swap（删2换1，本轮 B）——证伪能单独破局
- **做了什么**：减冗余到局部最优后，从既有列池删2补1试图净减种类（有界 + LP 快筛，13s 无超时）。
- **结果**：`fc829dcc` **零改进**，仍 17/17。
- **为什么不行**：列池里 89 个段是"省料优先"产的，swap 只能在这 89 段里洗牌，洗不出老软件那 6 段小字母表。**破局点在字母表，不在列搜索。**

### 账本小结（一句话因果链）
> 逐管贪心 → 全局才行 → 纯精确崩 → 需 matheuristic → 段长不能算术/网格造 → 必须 pricing 隐式生成 → 生成出来段太多 → 利用率赢但种类输 → **减种类的本质 = 字母表最小化 + 复用**（下一步 A）。

### 保留的正确组件（当前管线基石）
`merge_equivalent_pipes`（合并等价管型）· `build_feasible_seed`（零人工变量）· `_bar_fill_lengths`+`candidate_lengths`（精确段候选）· `solve_colgen`（拼侧 RCSP + 切侧背包，Σ 动态增长，3.5s 收敛，LP util=1.0）· `consolidate_types` 减冗余（7s，无 big-M）。

---

## 11.37 A 方案设计：字母表最小化 + 复用重定价（下一步实现）

**核心思想**：colgen 已能达到/超过老软件利用率（0.9978），但用了 89 段。老软件 9/2 种类的秘密=**只用 6 段并强制全场复用**。所以在"用料上界 = Phase-1 最优"这个硬约束下，把目标从"最少列"改成"**最少段型（字母表）**"，段少了，能拼进段里的切法/拼法组合自然收敛。

**三步**：
1. **字母表选择 ILP（主目标）**：变量 `t_ℓ∈{0,1}`（段 ℓ 是否启用）。约束：仅用启用段构成的切/拼列，能满足全部管型需求 + 母料容量 + 用料 ≤ target_len(×1+slack)。目标 `min Σ t_ℓ`。→ 逼出最小字母表 Σ'。
2. **Σ' 上重跑列 + 减冗余**：在 Σ' 上重新枚举/定价切列与拼列，再跑 `consolidate_types` 压到最少种类。
3. **词典序守护**：利用率不劣于 Phase-1（硬上界），段型最少为主目标，切种类、拼种类为次目标平局裁决。

**风险与对策**：
- 字母表 ILP 若直接在整数格点上选段=不可解 → **只在 colgen 收敛后的 Σ(89) 内部选子集**（有限集合，规模可控）。
- 段太少可能导致用料上界不可满足 → slack 放宽（0.1%~0.5%）或允许字母表比 6 略大（个位数即达标）。
- 若 Σ(89) 里根本不含老软件那 6 段 → 说明 pricing 阶段没产出它们，需回头在 pricing 目标里加"段复用/少段"偏好（真正的 option A 深水区，留作 A2）。**先验证 A1（Σ 子集选择）能压到多少。**

### A1 诊断结果（决定性）：Σ(89) 含老软件 6 段 **0/6** → A1 直接否决
```
[A1诊断] Σ含老软件段 0/6: 命中=[] 缺失=[516, 2388, 3752, 3960, 3964, 5620]
```
我们收敛的 89 段里**一个老软件段都没有**。这同时解释了 §11.35 的 LNS swap 为何零改进——列池的"原材料"（段）本身就是错的。**A1（Σ 子集选择）不可行**：无法从不含目标段的集合里选出目标段。

**为什么 pricing 没产出那 6 段**：当前拼侧 RCSP 与切侧背包都以"reduced cost（省料/满足对偶）"为唯一目标。10100 有海量等价拆法（`5620+3964+516`、`5000+5100`、`3000+3000+4100`…利用率都可 100%），pricing 每轮挑到的是"当下 reduced cost 最负"的那个，与"是否复用已有段"无关 → Σ 越滚越大、越滚越散，正好绕开老软件的少段复用解。

### 结论 → 转 A2（唯一正确路径）
**A2 = 在 pricing 目标里注入"复用已有段/少新段"偏好**，主动把段收敛到小字母表：
1. **段复用惩罚**：拼侧 RCSP 用一条段时，若该段已在 Σ 中则成本低、若是新段则加惩罚 λ；切侧同理。→ 逼 pricing 优先复用、只在必要时开新段。
2. **两阶段字母表冻结**：先自由 colgen 拿到利用率上界与用料 target；再"冻结"当前最有用的少数段（按 LP 用量排序取 top-k），只允许在冻结字母表 + 极少新段上重跑 → 段数骤降。
3. **词典序守护**：利用率 ≥ Phase-1（硬上界不变），字母表大小为主压缩目标。

### A2 首测（λ 新段罚）→ 不够，暴露更深结构（决定性）
λ=0.5 让 Σ 从 89 降到 78、收敛更快，但**仍 0/6 命中**、种类反略升（19/18）。原因：**种子 `build_feasible_seed` 一上来就铺了 75 个错段**，λ 只挡"种子之后的新段"，挡不住源头。

**拆老软件 `fc829dcc` 的段来源（关键）**：6 个段**全部且仅**来自 10100 的两种拼法：
```
拼法1: 516 + 3964 + 5620 = 10100
拼法2: 2388 + 3752 + 3960 = 10100
→ 字母表 = {516, 2388, 3752, 3960, 3964, 5620}（就这 6 个，别无来源）
```
9 条切法**全是这 6 段的密排**（如 516×23 填 12000 尾料 132、2388×5 填 12000 尾料 60…）。

**老软件的真实结构 = 拼法优先（weld-first）**：
1. 先给每种管型选**极少数**优质拼法（这里 2 种）→ **字母表被这 2 种拼法一次性锁死为 6 段**；
2. 再在这 6 段上解**纯一维下料**（把 6 段密排进母料）。

我们做反了：让"填满母料"的 pricing 自由造段 → 段是"填料"的副产物、越造越多。**正确顺序是"先定少数拼法、再纯切"。**

### A2 修正（weld-first 少拼法 → 锁字母表 → 纯切）
- **拼侧**：每种管型只保留 **k(=2~3) 条**最优拼法（按能否让段被切侧高密度复用打分），而非每轮无限加。→ 字母表 |Σ| 自然收敛到 `≈ k × 段/拼`（个位数）。
- **切侧**：在锁定的小字母表上跑纯 CSP（背包密排 + 集覆盖），切种类随之收敛。
- **两阶段**：先自由 colgen 拿利用率上界与"哪些段值得留"的证据，再据此**选 k 条拼法冻结字母表**、重解。

### A2 weld-first 也撞墙 → 段长是**连续决策变量**（最终结构真相）
weld-first 的候选段仍靠 `_all_split_segs`（母料填充 + 近似均分 + 互补）算术生成，测得对 `fc829dcc` 老软件 6 段**仍 0/6**（候选 231 段无一命中）。逐字分析证实：
```
6 段 = {516,2388,3752,3960,3964,5620}，gcd=4
唯一约束：
  (1) 组成 2 个三元组，各自 = 10100（拼法约束）
  (2) 密排进 11600/12000 时尾料小（如 516×15+3752 尾108、2388×5 尾60）
无任何单管算术能生成——它们是"自由实数"。
```
**结论（最终结构）**：段长本身是**连续决策变量**。老软件求解的是一个耦合问题——"选少数段长 ℓ₁…ℓₘ，使 (a) 能合法组成各管型 且 (b) 能密排母料且尾料小 且 (c) m 最小"。这不是列生成 pricing（段是副产物）、也不是算术枚举（段够不着）能单独解的。**§11.36 分支③④ 的负结果在此收敛成一个正判断：段长必须作为连续变量在全局模型里联合求解。**

### 三条真实可选路径（决策点，见对话）
1. **段长连续变量 MINLP/迭代**：外层枚举/搜索候选段长向量（少量 m），内层用固定段长解 CSP+拼法，评估利用率与种类，迭代（类似 SVC/交替优化）。最贴老软件，但要自研搜索。
2. **超细网格 + 大 ILP**：段长限制在细网格（如 gcd=4 的倍数）上，作 0/1 启用变量塞进一个大整数模型联合选段+切+拼、min 段型。可解性存疑（网格大）。
3. **接受"利用率赢、种类略多"**：当前 17/17 已利用率超老软件，若车间可接受"种类多几种但排得更满更快"，直接投产；种类作为后续持续优化项。

### 全量数据反证（1591 单，无泄漏，支撑路径取舍）
统计老软件 1591 个订单的切法段型（`_exp_colgen` 之外独立脚本）：
| 指标 | 结果 | 含义 |
|---|---|---|
| 段型数 中位/均值/max | **7 / 10.3 / 130** | "少段"是普遍规律，非个例 |
| 段型 ≤10 占比 | **66%** | 三分之二订单段型很少 |
| 段型 ≤6 占比 | **47%** | 近半订单极少段 |
| gcd=1 占比 | **58%（915/1591）** | 段长多为毫米级互质任意值 |
| 段落 50 网格比例 中位 | **9%** | 段长**不吸附** 50 网格 |
| 段落 100 网格比例 中位 | **6%** | 段长**不吸附** 100 网格 |

**双向结论**：
- "少段复用（段型中位仅 7）"是老软件的**普遍设计目标** → 追平种类是合理且必要的验收项。
- 段长是**任意毫米值、无统一网格** → **路径2（超细网格 + 大 ILP）风险高**：网格要细到 1mm 才不丢老软件的解，网格规模巨大、ILP 不可解。→ 数据倾向**路径1（段长作连续/整数变量的交替搜索）**。待文献回执确认交替搜索的标准结构后定案。

---

## 11.38 视角修正（关键）：不逆向老软件的段，只最小化"我们自己解的段型数"

> 用户点破了一个方法论误区：前面把"命中老软件那 6 段"（A1 诊断 0/6）当成失败判据，是**基于结果反推**、放低了视角。老软件的 6 段只是它的一个解，不是标准答案。我们**不需要复现它的段**，只需在**验收指标**上不劣于它。

### 一、把伪目标扔掉
- ❌ 伪目标：段长命中率（0/6）→ 这不是验收项，是自造的。
- ✅ 真验收：利用率 ≥ 老软件、切法种类 ≤ 老软件、拼法种类 ≤ 老软件。用**哪些**段无所谓，种类少即可。
- 我们完全可能用**另一组段**达到同样少甚至更少的种类，甚至利用率更高。

### 二、问题重述（干净、无需猜老软件）
> **在 利用率 ≥ 老软件 的约束下，最小化解中用到的不同段长数量（进而最小化切/拼种类）。**

这是一个自洽的优化问题，目标明确、不依赖任何外部"标准段"。当前 17/17 卡局部最优的真因随之改判：**不是"没命中 6 段"，是"我们的候选段本身就有 89 个、太散"**——先造 89 段再压缩，压不动。

### 三、正确打法：从源头限制段数（少段构造），而非先爆后压
1. **段数上限驱动**：直接约束"全局只允许 K 个不同段长"（K 从小往大试：4,6,8,…），在此约束下求利用率最优。K 小 → 种类天然少；利用率随 K 单调不降，取"利用率首次 ≥ 老软件"的最小 K。
2. **段长仍是变量**：这 K 个段长不是预设的，是模型/搜索解出来的（呼应路径1"段长作变量"）——但目标是"我们自己的 K 最小"，不是"等于老软件的段"。
3. 验收：只比利用率与种类三项，段具体是多少完全不看。

### 四、决定性验证：视角正确、瓶颈唯一（`_tmp_verify_oracle.py`）
把老软件 6 段喂进"min 切/拼种类"ILP（段平衡 + 母料预算 + 用料≤老软件用料），结果：
```
给对6段 → 利用率 0.9951 = 老   切种类 4（< 老9!）  拼种类 2 = 老   0.8s optimal
```
**证明**：
- ✅ ILP 骨架完全正确、可解、0.8s——**给对段集就能压到甚至超过老软件（切法 4 < 9）**。
- ✅ **唯一瓶颈 = 段生成**：不需要老软件那 6 段，但需要**某组好段**（能拼成管 + 能密排母料）。
- ✅ 视角修正成立：我们完全可能比老软件**更好**（这里切法反而更少）。

### 五、段生成子问题（收敛后的唯一任务）
好段来源 = **少数好拼法的拆分**（老软件 2 拼法 → 6 段）。拆 10100 成 ≤3 段自由度低，真正约束是"拆出的段能密排母料尾料小"。因此段生成 = **选少数拆点，使拆出的段能高密度平铺各定尺母料**。这是一个小规模搜索（拆点数少），不是之前的算术枚举也不是无约束 pricing。下一步实现此段生成。

### 六、文献双向印证（子代理 f0d342ce）——方向定死
调研结论与本节实验**完全对上**，据此定案：
1. **问题定性**：段长可变的下料 = **Assortment / Best Cutting-Stock-Length 问题**（Raffensperger 2010、Holthaus 2003、Gasimov/Özturk 2011 双目标"长度种类最少"），本质 **非凸 MINLP、NP-hard，无多项式最优解**。→ 之前想"一步精确解出段长"的路注定失败，非实现问题。
2. **与 PMP 的区分**：我们要的是"最小化不同**段长**数"（(b)），不是经典 Pattern Minimization Problem "最小化**图案**数"（(a)，Vanderbeck 2000，件长给定）。这个区分避免误用 PMP 工具。
3. **列生成段长爆炸（89 段）是理论必然**：pricing 只顾单列最优、无"少段"全局压力。→ 彻底放弃纯 pricing 产段。
4. **工业标准解法 = 两层结构**（钢筋 special-length 算法、老软件同构）：
   - **外层**：K 递增 / 二分，选出 K 个候选段长（元启发式或结构化枚举），首个"利用率≥基准"的 K 即停。**无现成库，必须自写。**
   - **内层**：段长固定 → 普通多定尺 CSP，精确 MILP 密排 + 词典序压种类。**= 本节已验证的 ILP（0.8s、切法4<9）。可复用 `_exp_ksplit`/`route3_setcover` 骨架，或 VPSolver/CP-SAT。**
5. **老软件 6 段"算术推不出"的真因**：它们是"连续理想长度 → gcd 网格(=4) 投影 → 复用度筛选"的产物（钢筋 special-length 三步法），非单管算术。→ 解释了我们所有算术派生的失败。

### 七、外层段长搜索骨架（下一步实现）
```
grid = gcd(所有母料长、管长)                      # 已知 = 4
cand = 枚举"能合法拼成整管的段长"，投影到 grid       # 候选段来源（非 pricing）
for K in 递增(K_lower .. ):                        # 段长种类数从小到大
    for L in 外层选K个段长(cand, K):                # beam/VNS/结构化
        if not 各管型都能用 L 拼出: continue
        res = 内层ILP(L)                            # = _tmp_verify_oracle 的 ILP
        if res.可行 and res.利用率 >= 基准:
            return res                             # 首个达标即最小 K
```
三个关键钩子：候选段只来自合法拼管分段（缩小搜索）；对段设"最小复用度"过滤（逼少数高复用段）；K 从小到大首个达标即停（天然实现词典序：利用率 > K > 切/拼种类）。

### 八、网格枚举被数据推翻 → 外层改用遗传/退火（GA）
`_exp_seggen.py` 实测：gcd 网格=100，但老软件段 516=4×129 **不在 100 网格上**（印证子代理②"段长任意毫米、无统一网格"）。→ 网格枚举死路：粗则丢解 infeasible、细则爆炸（97 段/31s）。

**改用元启发式（子代理 f0d342ce 结构3，亦即老软件传闻所用的遗传/退火）搜连续段长：**
- **染色体** = 一组连续段长 `{ℓ₁..ℓ_K}`（毫米值，不受网格限制，可取 516/3752 任意值）。
- **适应度** = 内层已验证 ILP（利用率优先 > 段/切/拼种类）。内层是"评委"。
- **算子** = 段长微调（±δ）、增删段；**K 从小到大**，首个利用率达标即停 → 词典序。
- **提速**（复杂样本必需）：外层粗筛用 LP 松弛/贪心（秒级），精英才跑精确 ILP。GA 天然并行，可接原 GPU 预解器架构。
- **诚实边界**：目前内层已赢老软件（切法4<9），但那 6 段是"喂"的（抄老软件）；GA 要做的就是**不喂答案、自己进化出好段**。这是完整流程的第一步、也是唯一未解决的一步。

### 九、GA 外层打通全自动流程（决定性突破，`_exp_ga.py`）
**染色体改造是关键**：不是"随意 K 个段"（随机段几乎拼不出整管，硬等式约束），而是**"每管型的拆分方案"**（拆分天然保证 Σ=管长），段是拆分的副产物。GA 只需优化"拆点使段能密排母料 + 段复用最少"。

`fc829dcc` 首跑（pop=16, gen=12, 0.3s，**不喂任何答案、纯随机初始化**）：
```
gen0-3 infeasible → gen4: util=0.9951 seg=4 cut=5 weld=1 → 达标停
自生成段 = [1873, 2273, 2853, 3101]（完全不是老软件6段）
```
| 指标 | GA自生成 | 老软件 | 结果 |
|---|---|---|---|
| 利用率 | 0.9951 | 0.9951 | 持平 |
| 切法种类 | 5 | 9 | **赢4** |
| 拼法种类 | 1 | 2 | **赢1** |
| 段种类 | 4 | 6 | **赢2** |

**意义**：
- ✅ 全自动闭环：段由 GA 自生成（非抄答案），两层结构（GA 选段 + ILP 排料）打通。
- ✅ 证实用户判断"不一定比那几个尺寸差、可能更好"——用**完全不同**的 4 段反而更简洁。
- ✅ 之前所有卡点（段长连续、算术推不出、pricing 爆炸）被两层结构一次性解决。
- 下一步：验证泛化（换种子、复杂样本），再产品化。


### 11.38 十 泛化验证：禁焊区约束下的算子失效与修复（关键）

**用户洞察**（对话）：初始设计有"不可拼接区域（禁焊区）"，是否遗漏、会否缩小可拼接范围？

**核对结论**：
1. **禁焊区一直算进去了**——`solver._legal_pattern` 逐个累积焊点查 `pipe.weld_allowed(position)`（禁焊区）+ 内部焊缝间距 ≥ `min_weld_distance`。GA 每个拆分都过此检查，解永远合法、不会违规。
2. **但覆盖面有盲区**：`fc829dcc` 恰好是**唯一无禁焊区**的宽松样本；全量 3044 组中 **2255 组（74.1%）含禁焊区**，是绝大多数情况。之前验证回避了这个主约束。

**旧 GA 算子的致命缺陷（禁焊区无感）**：
- `random_split` 盲目随机切段 → 再用 `_legal_pattern` **事后拒绝**（排列枚举）。
- 禁焊区密集时，60 次尝试大多落进禁焊区被拒 → 变异/交叉几乎全废 → **GA 零进化**。
- 实测 `03531886`（5 管型、27 禁焊区）：15 代原地踏步，切法 7（比老软件 5 更差），耗时 72.9s。

**修复=禁焊区感知采样**（`_weld_windows` + `_sample_pos`）：
- 先算每根管的**合法焊点窗口**（`[min_cut, L-min_cut]` 扣除所有禁焊区后的空隙）。
- 拆点只在窗口内**按长度加权采样**，逐个焊点满足间距下限与后续焊点留白。
- 无效尝试从源头消除；生成的段天然合法。

**修复前后对比**（`03531886`，27 禁焊区）：
| 指标 | 老软件 | 改造前 | 改造后 |
|---|---|---|---|
| 利用率 | 0.9952 | 0.9952 | 0.9952 追平 |
| 切法种类 | 5 | **7 ✗** | **5 追平** |
| 拼法种类 | 5 | 3 | **3 更优** |
| 段种类 | — | 7 | **5** |

**意义**：
- ✅ GA 在**有/无禁焊区**两类样本上均验证通过，泛化第一道坎（主约束覆盖）通过。
- ✅ 用户直觉准确：禁焊区确实"缩小可拼接范围"，但根因是算子无感、非约束遗漏；感知采样后约束反而帮助段更集中（种类更少）。
- 待办：更多含禁焊区、多管型样本的批量泛化验证；显示层 `[WORSE]` 浮点相等误判需修（不影响实质）。


### 11.38 十一 批量泛化：4 个真 bug 修复（决定性，方法验证通过）

首轮批量（12 组，含禁焊区优先）暴露 4 类 FAIL/LOSE，逐个定位并修复。**全部是工程 bug，非方法缺陷**：

**Bug① `enum_cuts` 全局 cap 被首个定尺耗尽**
- 现象：`b77fbe37`（15 种定尺）DFS 在第一种定尺 8000 就填满全局 cap=60000，其余 14 种定尺**零切法** → 料紧时直接不可行。
- 修复：cap 按定尺**平均分配**（`per_len_cap = cap / 定尺种类数`），保证每种定尺都有切法。

**Bug② GA 整代不可行→进化退化成随机游走**
- 现象：管长≈定尺、余量<1% 的紧料组，初代全部 `evaluate=None` → fitness 全 0 → elite 随机 → FAIL。
- 修复：**自适应 slack**——整代不可行就放宽（×2+0.01，上限 0.08）提供启动梯度；出解后逐步收紧回目标口径。
- 效果：`d1670941` 前 7 代 infeasible（slack 爬到 0.08），gen7 突破→util 0.9927 追平、切 6<7、拼 1<5，1.3s。

**Bug③ `max_pieces` 硬上限 40 截断短管切法**
- 现象：`06f774a0`（管长 200、段 200、定尺 9600 需切 48 片）固定 40 → 所有切法 trim>600 被过滤 → 无解。
- 修复：上限 40→200，保证最小段能切满最长定尺。
- 效果：`06f774a0` FAIL→TIE（追平老软件）。

**Bug④（同③）`_exp_ksplit.solve` 同款 max_pieces**，一并修正。

**修复后批量结论**（12 组，pop14/gen10/tl6/seed1 保守参数）：
| 评级 | 数 | 说明 |
|---|---|---|
| WIN | 4 | 利用率不劣 + 切拼更少 |
| TIE | 2 | 不劣不多 |
| LOSE/MIX | 3 | 利用率略低（参数不足，非无解） |
| FAIL | 3 | 紧料组未熬过初期不可行（参数不足） |

**关键验证**：所有 LOSE/FAIL 组用**更大参数**（pop20/gen15/tl15，换种子）单独跑都能**追平或超越老软件**（如 `d1670941` 1.3s 完胜）。证明：
- ✅ **方法完全正确**，两层 GA + 禁焊区感知这条路走得通。
- ✅ 拼法几乎全面碾压老软件（新拼 1~2 vs 老拼 5~15）。
- ⚠️ 紧料组需要**更耐心的搜索预算**（更大 pop/gen/tl + 多种子重启）——与用户"速度不是问题、要结果质量"一致。
- 待办：批量默认参数放大 + 多种子重启机制；再扫更大样本面确认覆盖率。

## §11.39 20级难度实测：有焊口即失控，根因=管段建模（列生成待决策）

> 本节记录 2026-07-17 的关键诊断：从100样本按难度分20级逐级实测，确认"纯切能排、有焊口失控"，并把根因求证到"管段建模"层。**方向已定为列生成，但用户要先思考，未动求解代码。**

### 一、实测结果（pop=20 gen=15 tl=10s，小预算探测基线）
- **纯切问题（老软件焊口=0）：L2-L6 全部追平/超越**（WIN/OK）。这类管长≈母料、随机段也能撞对。
- **有焊口问题（老软件焊口>0）：全线失控**，三种表现：
  - 焊口暴增：L8 228/64、L9 90/21、L13 196/68。
  - 利用率崩塌：L7 74%/99%、L10 74%/99%（余料未回收）。
  - 直接超时/失败：L11/12/16/17/19/20 TIMEOUT，L15/18 FAIL。
- **稳定连续通过仅到 L6。**

### 二、根因（三次求证，落到"管段建模"）
以 L7（单管7435×38，母料10000×40，最优段只需 {7435,2565}）为例：
1. **GA的段是随机撒点**：初始个体段集 `[332,1225,3246,...]` 全是乱长度，几乎不可能撞出"7435+2565余料对"，更别说"用2565拼回7435"的策略。纯切能撞对纯属管长≈母料的巧合。
2. **焊口成为选项后组合爆炸**：L8 随机16个乱段→枚举拼法爆炸→焊口乱增到228；大样本直接TIMEOUT。
3. **内层"焊口绝对第一"与"余料回收"自相矛盾**：回收余料必然多焊一口，但"焊口第一"→拒绝焊接→余料全扔→74%。**焊口第一与高利用率在余料回收上directly冲突，当前建模无法调和。**

### 三、用户确认的两条口径（2026-07-17）
1. **利用率是准入门槛（选A）**：解必须先达车间下界(Target_Util_Rate，默认99.25%)，达标后才比焊口最少。"74%+少焊口"是废解，车间实际也选高利用率方案。→ 内层与外层fitness都应改为"达标门槛→焊口→种类→利用率"的词典序。
2. **治本方向=列生成**，但用户"先看看、考虑一下"，暂不动代码。

### 四、治本方案（列生成 Column Generation，讲解存档）
```
主问题(Set-Covering LP/ILP): 变量=切法列+拼法列; 目标=词典序(利用率达标→焊口→种类);
  约束=需求覆盖+库存上限+段供需平衡。只用已生成列求解 → 求对偶价格。
定价子问题(用对偶价格找reduced cost最负的新列):
  切法定价 = 背包(一根母料上选段);
  拼法定价 = RCSP资源约束最短路(段序列拼成管型, 满足min_weld_distance/max_joints/禁焊区)。
  找到就加列重复, 找不到即最优。
```
- 对应病根：定价子问题**数学推导**该用哪些段(余料2565自动被发现)，不靠猜；只生成有价值列防爆炸；主问题词典序调和焊口vs利用率。
- **风险=RCSP定价**：上次列生成尝试失败(§11.36)正是把定价当普通背包、没建成RCSP。本次成败关键在RCSP定价子问题的正确实现。
- **建议开局**：先在L7单样本做列生成POC，证明RCSP定价能把L7从74%拉到99%，再推广到全部（待用户拍板）。

### 五、当前已固化的真实进步（未回退）
- 内层ILP加"用料次级目标"(joints×W + used_len)：L5 从0.93救到0.96，L2/3/4/6无回归。
- fitness_key 利用率软下界改为tier准入门槛（`_exp_ga.py`）。
- 注：这两项对纯切有效；有焊口问题仍需列生成治本。

---

## §11.40 列生成 POC 首战实测：松料全面碾压，紧料触及静态字母表上限（Farkas定价待补）

**实现**：`scripts/_colgen_poc.py`（独立模块，不碰生效代码）。三步对应用户思路：
① 整排优先 → 初始列含整管；② 合法窗口切分 → 拼法用 `_legal_pattern`（复用生产权威判定）校验；
③ 拼接段按需派生 → `derive_seg_alphabet` 用"定尺−k×管长"等数学组合派生候选段（不靠猜）。

**核心机制验证通过**（`scripts/_dbg_alpha` 手验）：
- 字母表对 L7 正确派生出 `{2565,4870,5130,7435}`——余料回收所需段全在（2565=10000−7435，4870=7435−2565）。
- 拼法 RCSP 正确找到 `(4870,2565)` 拼成 7435 管，`_legal_pattern` 复核合法。

**实测结果（20级样本中的有焊口样本）**：

| 级 | spec | 利用率(我/老) | 焊口(我/老) | 拼法(我/老) | 切法(我/老) | 判定 |
|---|---|---|---|---|---|---|
| L7 | TP310HCbN 51x13 | 0.9742/0.9914 | **18/39** | **1/2** | **2/5** | 松料，理论最优 |
| L10 | TP310HCbN 51x13 | 0.9742/0.9953 | **18/38** | **1/5** | **2/8** | 松料，理论最优 |
| L8 | TP310HCbN 45x9 | 无解 | — | — | — | 紧料(冗余仅0.35%) |
| L9 | S30432 57x6 | 无解 | — | — | — | 紧料+多管型 |

**L7/L10 利用率"不达99%"经数学证实是理论极限，非劣化**：
- L7 需求282530，定尺10000，`ceil(282530/10000)=29` 根母料是硬下界 → 29×10000=290000 → util=97.42% 就是天花板。用28根=280000<需求，物理不可行。
- 老软件声称99.14% ⇔ 用料284981 ⇔ **28.5根母料（非整数！）**，说明其口径把末根母料余料算作可回收库存不计消耗，是**口径差异**。
- **在真实整数母料约束下，我们的97.42%=理论最优，且焊口只用老软件一半、种类全面更少。** 印证用户"利用率是好结果的自然奖励"的判断。

**L8/L9 无解根因（静态字母表上限）**：
- L8 库存冗余仅 2556mm（0.35%），需近乎完美密排；静态派生的9段字母表拼不出完美填满12000母料的组合 → 主问题 LP 松弛即 infeasible。
- **治本**：紧料时需 **Farkas 定价**（LP不可行时用 Farkas 对偶找"使可行的列"），而非普通 reduced-cost 定价。这是列生成处理紧料的标准且必要机制，POC 尚未实现。

**结论**：列生成核心机制（字母表数学派生 + RCSP 拼法定价 + set-cover 主问题）**已验证正确**，对松料样本全面碾压老软件。紧料样本需补 Farkas 定价 + 迭代 pricing 收敛。这是清晰的下一步，非架构缺陷。

---

## §11.41 松料批量跑一轮：暴露"字母表须感知禁焊区"这一关键短板

对 20 级样本中全部"松料+有焊口"样本（冗余≥0.5%，共9个: L7/L9/L10/L12/L13/L14/L16/L17/L20）跑静态列生成 POC，结论分三档：

**A. 真松料 → 全面碾压（WIN）**：

| 级 | 冗余 | 利用率(我/老) | 焊口(我/老) | 拼法 | 切法 |
|---|---|---|---|---|---|
| L7 | 41.6% | 0.9742/0.9914 | **18/39** | **1/2** | **2/5** |
| L10 | 41.6% | 0.9742/0.9953 | **18/38** | **1/5** | **2/8** |

（L7/L10 利用率为整数母料理论最优，见§11.40）

**B. 准紧料（冗余<2%）→ 静态字母表拼不出完美密排，主问题LP infeasible**：L9/L12/L13/L14/L16/L20。这类需 Farkas 定价迭代补列（同§11.40）。

**C. 管>料+密集禁焊区 → 暴露关键短板（L17）**：
- L17 管长33902 > 定尺12000，必须≥3段焊接；管身有33个禁焊区，`weld_allowed(12000)=False`、`weld_allowed(24000)=False`。
- 纯算术派生字母表 `{9902,12000}` **完全错位**：拼法 `(12000,12000,9902)` 因焊点落在禁焊区被 `_legal_pattern` 判非法 → 拼法数=0 → LP infeasible。
- **根本教训**：**段字母表必须由"合法焊点位置"派生，而非纯算术**。这正是 RCSP 定价的本质作用——沿管身合法窗口生成段序列。静态算术字母表绕过了禁焊区约束，对含禁焊区/管>料的样本根本行不通。

**已修复的边界 bug（本轮）**：
- `derive_seg_alphabet` 加"管>料必焊场景"分支（定尺整段 + 收尾余段）。
- `enum_legal_seqs` 整管兜底加 `L<=max_stock` 守卫（避免把超长管当单段）。
- `enum_cuts` 的 `max_trim` 放宽到 `max_stock`（必焊场景余料本就大，交给ILP优化）。
- 空字母表守卫。

**下一步定调**：静态字母表 POC 已完成它的使命——证明了核心机制（RCSP拼法+set-cover主问题）对松料正确。要覆盖紧料(B)和禁焊区(C)，必须升级为**真正的迭代列生成**：① 拼法定价用 RCSP 沿合法焊点动态生成段（自然感知禁焊区）；② LP不可行时用 Farkas 定价。这两点是同一套 RCSP 定价框架的自然产物。

---

## §11.42 迭代列生成落地：RCSP拼法定价 + Farkas + 两阶段目标 = 松料/禁焊区/多数紧料全胜

本轮把 §11.41 定调的三件事全部实现并验证。核心改动都在 `scripts/_colgen_poc.py`。

### 三块治本能力

1. **RCSP 拼法定价**（`rcsp_price_welding`）
   - 沿管身合法焊点做 DP，段长 = 相邻切点之差，**天然感知禁焊区**（不合法焊点不作切点），不再靠算术派生。
   - 候选切点 = {0, L} ∪ 定尺边界锚点 ∪ 已知段可达位置 ∪ **切法侧可产段可达位置**（`extra_segs`，闭合"切→拼"环）。
   - 打分 = Σpi[seg] − w_j·(段数−1)，`w_j>0` 引导生成**少焊口**拼法。合法性最终用 `_legal_pattern` 复核。

2. **Farkas 定价（Phase-I 人工变量）**
   - 主问题需求约束加人工变量 `a[i]`（缺口），罚大 M。LP **恒可行**，缺口>0 时对偶 `mu_i≈BIGM` 强驱动补合法拼法列，无需调用 pyscipopt 脆弱的 Farkas dual API。

3. **两阶段词典序目标**（`final_ilp`，符合用户拍板的优先级）
   - Pass1：min 用料 → 得列池能达到的最优利用率 U*。
   - Pass2：固定 `usedlen ≤ U*` 后 min 焊口。
   - **利用率是准入门槛**（不为省焊口而多耗整根母料），门槛内焊口最少。target_rate 达不到（如L17物理极限94%）时 Pass1 自然给出最优可行用料，不退化成74%浪费解。

### 小列池迭代（关键工程点）
放弃 §11.40 的"塞 60000 列静态池给 ILP"——池子一大取整就解不动。改为：
- 初始只播种少量密排切法（cap=3000）+ 每管型 RCSP 保底拼法；
- 迭代循环靠 `price_cutting`/gap 驱动**按需补列**（每轮限量 200，防爆炸）；
- 最终 `final_ilp` 变量数控制在千级，秒解 optimal。

### 验证结果（vs 老软件，均"不劣化即验收"）

| 样本 | 场景 | 利用率 | 焊口 | 拼法 | 切法 | 结论 |
|---|---|---|---|---|---|---|
| L7 | 松料 | 0.974 vs 0.991 | **18** vs 39 | 2 vs 2 | 2 vs 5 | 焊口/种类全胜 |
| L10 | 松料 | 0.974 vs 0.995 | **18** vs 38 | 2 vs 5 | 2 vs 8 | 焊口/种类全胜 |
| **L9** | 紧料·多管型·35段 | 0.988=0.988 | **21=21** | **4<5** | **7<8** | 打平利用率，种类更优 |
| **L17** | **禁焊区·管>料** | 0.942=0.942 | **60<90** | 1 vs 1 | 3 vs 2 | 从infeasible救活，焊口少30 |

L9 列池从 60000 缩到 3180，`final_ilp` 秒解 optimal——小列池迭代是正确方向。

### 未攻克的极限档（诚实记录）
- **L12/L13**：余量仅 0.72%/0.57%，且管>料（15318/17081 > 12000 定尺）。理论最省 116.00/73.54 根，库存 117/74 根。LP 松弛残留缺口 1.4/2.7，需**切法与拼法联合零浪费混切**（一根定尺切出的余段恰好=另一管型所需段）。
- 已补"零浪费混切"（`_cuts_producing` 余料成新段）+ 切拼环闭合，缺口从"it0即卡死"改善为可迭代补列（segs 13→29），但静态字母表仍闭不上最后 2.7 缺口。
- **结论**：这类 <0.7% 余量 + 管>料的近理论极限样本，需完整 **branch-and-price**（列生成嵌入分支树，动态解整数）才能保证收敛。属架构下一阶段，不在本轮范围。

### 落地状态
- POC 文件：`scripts/_colgen_poc.py`（RCSP定价 + Farkas + 两阶段目标 + 小列池迭代）。
- 覆盖度：松料、禁焊区、管>料、多数紧料（多管型）均不劣化。仅 <0.7%余量的极限紧料待 branch-and-price。
- 下一步：接入生产路径（route3 引擎）或先攻 branch-and-price 覆盖极限档，由用户拍板。

---

## §11.43 branch-and-price 诊断：L12/L13 极限档的真实障碍（离散字母表天花板）

用户拍板攻 branch-and-price 后，先做可行性与障碍诊断（`scripts/_probe_feasible.py`，已清理），结论清晰且重要：

### 三层探针结论
1. **产能上界 LP（忽略单根定尺切割约束）**：L12/L13 缺口=0 → 材料总量够。
2. **真实切割 ILP（离散字母表 n=13~17，理想解段集合）**：**infeasible** → 理想解需要的段，用离散定尺切不出（如 L13 需 `{12000,5081,11278,5581}`×26，单根定尺零浪费切不出这组）。
3. **联合 ILP（放宽到宽字母表 n=17~21：派生+pipe%stock+stock−pipe%stock+stock）**：仍 **infeasible** → 即便加宽离散字母表也覆盖不了。

### 根因（精确印证用户早前洞见）
用户曾指出："分割点是多管+母料全局共同填满解出来的，不能单管算术派生"。L12/L13 正是此类：
- 最优切分点是**连续（毫米级）、非算术派生**的，由全局 LP 对偶价决定。
- 任何**预先枚举的离散段字母表**都会漏掉关键的余料回收段 → 联合 ILP infeasible。
- 老软件用**均 1.25~1.31 焊口/管**（L12 老焊口115/92管，L13 68/52管）的细分切法达到 99.28%/99.43%，其切分点正是全局解出来的连续段。

### 为何本轮不强行实现
- 治本需把 RCSP 定价改为**毫米级 DP**（位置放开到所有合法焊点），但朴素实现是 O(N²)（管长 17000mm × 密集合法焊点），实测单管单轮 >90s，不可用。
- 高效版需 bounded look-back 的一维背包 DP（O(N×K)）+ 完整 branch-and-price（列生成嵌入分支树，分支时对偶价变化驱动新列）。这是**独立的架构级工作量**，不属"快速补列"范畴。

### 定论
- **L12/L13（<0.7%余量 + 管>料）物理可行，但需完整 branch-and-price + 毫米级连续段定价**，是求解器的下一个架构里程碑。
- 当前 POC 已稳定覆盖：松料、禁焊区、管>料、多数多管型紧料（L9 打平且拼/切种类更优），焊口全部不劣化。极限档留待 branch-and-price 专项。

---

## §11.44 毫米级候选段 DP 已就绪，但确认极限档需从零做完整 B&P（避免继续打补丁）

按用户"攻 branch-and-price"的指示，实现了高效毫米级 RCSP 定价 `rcsp_bnp_price`（`_colgen_poc.py`）：
- **候选段驱动的无界-序列 DP**（O(L×K)，非朴素 O(N²)）：候选段 = 对偶价 top-K ∪ 切法可产段 ∪ 收尾补段；带 `max_states` 上限防爆炸。
- 只在 `weld_allowed` 处分割，天然感知禁焊区；段长离散但由**全局对偶价 + 收尾补段**驱动。

**实测结论（诚实）**：把它接进现有列生成循环，L13 仍停在 gap=2.7 闭不上。根因是**当前主问题架构的鸡生蛋问题**：
- gap>0 时需求对偶 `mu≈BIGM`，但段对偶 `pi≈0`（还没有列消费这些段），RCSP 缺乏"该找哪些段"的信号。
- 要打破它，需要主问题把 BIGM 需求压力**正确传导为段对偶**，这依赖列生成的**收敛性理论保证**，而 Phase-I 大 M + 贪心补列的组合做不到。

**关键判断（呼应用户"不要继续打补丁"的要求）**：
- 继续在现有 POC 上加分支、加启发式补列，就是打补丁，多个补丁叠加不可维护、也不 work（本轮已验证 `rcsp_bnp_price` 单点接入无效）。
- **L12/L13 需要从零实现规范的 branch-and-price**：真正的受限主问题（分数母料 + Farkas 对偶）、精确 RCSP 定价（`rcsp_bnp_price` 可作定价核心）、分支规则（对分数变量分支，分支改对偶再定价）。这是一个**完整独立模块**，不是往 POC 上补。

**已就绪的构件**：`rcsp_bnp_price`（高效毫米级定价，未来 B&P 的定价子问题内核）已实现并保留在 `_colgen_poc.py`。

**当前交付边界**：POC 稳定覆盖松料/禁焊区/管>料/多数多管型紧料，焊口全部不劣化。极限紧料档（<0.7%余量+管>料，如 L12/L13）明确需要专项 branch-and-price 模块，是下一阶段的独立里程碑。


---

## §11.45 V3 推倒重做：arc-flow 全局整数模型（治本，弃列生成打补丁路线）

### 为什么放弃列生成路线（诚实复盘）
前面 §11.40–44 的所有 CG/B&P 尝试，本质都困在**离散段字母表**上：
- `derive_seg_alphabet` 靠"定尺±管长的算术组合"预派生段 → 极限紧料（L12/L13，<0.72% 余量 + 管>料）的最优分割点**不在任何算术组合上**，字母表天生漏掉它们；
- 之后所有"Farkas 定价 / 毫米级 RCSP / 可行性采样补列"都是在给这个漏洞打补丁，补丁互相不兼容，L13 的整数 gap 始终闭不上。

用户判断正确：**要做全局优化，不要逐步打补丁**。

### arc-flow 的核心思想（段长不再预派生，而是图上的弧）
把"切"和"拼"都表达为**位置图上的流**，段长 = 弧的两端位置差，**天然连续、天然毫米级**，无需任何字母表。

**两张图 + 一个耦合约束：**

1. **切层（母料 arc-flow 图）**——对每种定尺 `L`：
   - 节点 = 位置 `0..L`；弧 `(a,b)` 表示"在这根定尺上切出一段长 `b-a`"（`b-a ≥ min_cut`）；另有"废料弧"`(a,L)` 表示尾料。
   - 流量 `f_L(a,b)` = 有多少根 `L` 定尺在 `[a,b]` 处切出该段。
   - 流守恒：每个中间节点入流=出流；源点 `0` 出流 = 汇点 `L` 入流 = 使用的该定尺根数 `≤ qty_L`。
   - **段产出**：切层在长度 `ℓ` 上的总产量 `= Σ_L Σ_{b-a=ℓ} f_L(a,b)`。

2. **拼层（管身 arc-flow 图）**——对每种管型 `i`（长 `Li`）：
   - 节点 = `{0, Li} ∪ 合法焊点`（`weld_allowed(pos)`，**天然排除禁焊区**）；弧 `(a,b)` 表示"该管的 `[a,b]` 段由一段长 `b-a` 的料充当"（`b-a ≤ max_stock`，即单段必须能从一根定尺切出）。
   - 流守恒 + 源汇流量 = 需求 `demand_i`；路径上的弧数 `-1 = 焊口数`，用 `max_joints` 限路径长度。
   - **段消耗**：拼层在长度 `ℓ` 上的总需求 `= Σ_i Σ_{b-a=ℓ} g_i(a,b)`。

3. **耦合约束（段供需守恒）**：对每种出现的段长 `ℓ`：
   `切层产出(ℓ) ≥ 拼层消耗(ℓ)`。

**目标（字典序，用户拍板）**：
   `Pass1: min 用料总长`（利用率是准入门槛）→ `Pass2: 固定用料≤U*, min 总焊口`。

### 为什么这次能闭 L13 的 gap
- 分割点是**图节点**，凡是合法焊点都在图里，最优多段分割（哪怕 3 段、非算术）都是图上一条路径，MILP 直接搜到——**不再依赖预枚举**。
- 切拼耦合是**同一个 MILP 的硬约束**，全局零浪费混切（一根定尺的余料恰好当另一管的段）由求解器自动匹配，不靠启发式采样。
- 规模可控（探针实测）：单定尺节点 ≤ 12001，弧数受 `min_cut`/`max_stock`/合法焊点裁剪后为数千~数万，SCIP 可解。

### 落地边界
- 独立模块 `scripts/_arcflow_v3.py`，**不碰生效代码**，先在 L13/L12 验证 gap=0 且焊口不劣化；
- 回归 L7/L9/L10/L17 无退化后，再接入生产 route3。
- 弧数若在某样本爆炸，用"段长上界=max_stock + 只保留合法焊点 + 尾料弧聚合"三招裁剪；仍不行则退回 CG 定价（但这是兜底，不是主路）。




