# D5 专线算法设计：B 打样 → C 沉淀

**日期:** 2026-07-24  
**状态:** 历史 D5 实验设计；不得作为生产求解器规范。  
**成功标准（已废止）:** 原设计以焊口 / 拼法 / 切法 / 利用率对比，并允许人审混合结果。当前权威验收以 `docs/SPEC.md` 和项目 Skill 为准。  
**非目标:** 不以「有解率」为第一 KPI；不在 V4 上打补丁冲 D5；不把僵硬字典序自动判死一切「单维劣、整体可能更好」的案

---

## 1. 背景与约束

- D1/D2：当前 V4 有可行解，**可接受，本专线不改动** `solve_v4_production` 主路径。
- D5：V4 + fingerprint 兜底在极难档几乎无效（fp 命中 0）；route3 能开一部分案，但拼切型易爆炸，不符合对标标准。
- **时限:** 不要求秒级。单案可以秒级，也可以数小时；预算由探针参数控制。失败形态允许「超时未优」，不允许「烂解充数」。

## 2. 两阶段路线（已确认）

| 阶段 | 名称 | 做什么 | 验收 |
|------|------|--------|------|
| **B** | Motif 列工厂 + SPEC setcover | 用全档/D5 指纹招式生成焊列/切列，SCIP 按字典序优化 | D5 success 相对 legacy **非劣率**为主指标 |
| **C** | 强底层沉淀 | 在 B 验证「目标+招式空间」正确后，把同一目标做成更干净的全局/B&P（减少对手工 motif 表的依赖） | 不弱于 B 的非劣率；motif 表可降级为热启动 |

原则：**先赢验收，再换底层。** 禁止反过来「先堆底层、验收用有解凑」。

## 3. 阶段 B 设计

### 3.1 边界

- **新入口:** `scripts/solve_d5_motif.py`（或等价模块）暴露 `solve_d5(group, *, tl, util_floor=None, legacy_metrics=None) -> dict | None`
- **调用方:** 仅 D5 探针 / 可选 `engine=d5`；**默认生产 V4 不自动跳 D5**
- **禁止:** 题号特判；为刷有解而放宽 SPEC 门控

### 3.2 列工厂（焊）

按 `docs/legacy-fingerprint-rules-understood.md` 的 D5 侧重：

1. 长管：最长段优先等于 **任意库存规格**（含非 S_max），再补余段 → `2/3/4seg_stock_anchor`
2. 长管：必要时 `4seg_R_triple` 类多段（受工艺 `max_joints` / 几何合法约束）
3. 短管：`1seg` 为主；需要时 `2seg_stock_gap` / `2seg_pack_L_leftover` / `2seg_kkerf_L`
4. 每管型优先少拼法（接近「一型一主拼」），多拼法只在覆盖/util 需要时加

### 3.3 列工厂（切）

1. 少切型、高复用；`k-copies`；近满切 / kerf-fill
2. 切字母表尽量来自当前焊列用到的段长（本地 alphabet），避免无关段膨胀类型数

### 3.4 求解与目标

> **历史目标（已废止）**：以下 D5 实验曾使用焊口优先。当前统一目标以 `docs/SPEC.md` 和项目 Skill `production-grade-nesting-solver` 为准：`weld_types → joints → cut_types → util`，且每阶段均须锁定前序最优值。

SCIP（或现有 setcover 骨架）分阶段或字典序（历史实现）：

1. `joints` 最小  
2. `weld_types` 最小  
3. `cut_types` 最小  
4. `util` 最大（或 used_len 最小）

输出字段对齐 V4 解：`joints / weld_types / cut_types / seg_types / util / used_len / solver_path`

### 3.5 四维比较 + 人审模型（替代「硬丢弃一切非字典序优」）

求解器内部仍按 **焊口 → 拼法 → 切法 → util** 优化（与老软件痛点一致）。

探针侧对每个有解案输出四维对照，并打 **自动标签**（供筛选，不代替人审）：

| 标签 | 含义 |
|------|------|
| `lex_non_worse` | 字典序非劣（焊口≤且…且 util≥legacy-1e-3） |
| `mixed` | 有维更好、有维更差（例如焊口少但拼法多）— **必须人审** |
| `lex_worse` | 字典序全面不优于（首个严格更差维更差，且无补偿维更好） |
| `unsolved` | 无解 |

**人审确认后的结论**（写入 `human_verdict`）才计入「整体非劣/整体更好」统计。  
第一轮不做加权黑盒分数；用你在 HTML 上的确认样本，再沉淀评分模型（避免先验拍权重）。

生产接入前：仅自动采纳 `lex_non_worse`；`mixed` 是否放行由后续模型/规则表决定。

### 3.6 时间预算

- 探针默认可为大预算（例如 600s–数小时，参数化 `--tl`）
- 分阶段：先可行+焊口，再锁焊口压拼/切型，再压 util
- 超时：返回当前 best（含可能 `mixed`）；无任何可行解 → 无解

## 4. 阶段 C（后置，本阶段只定接口）

- 同一 `solve_d5` 外观；内部可替换为 global / B&P
- B 的列工厂降级为：初始列 / 热启动 / 可行解种子
- 触发条件：阶段 B 在 D5 上非劣率达到约定门槛后，再开 C 实施计划

## 5. 测试与验收

### 5.1 范围

- **只测 D5** legacy-success（约 120 案），脚本独立：`scripts/probe_d5_motif.py`
- **不**为本专线重跑 D1/D2（除非回归抽检）

### 5.2 主指标（按优先级）

1. **`human_non_worse_rate`**（人审确认整体非劣/更好）— 最终 KPI  
2. **`lex_non_worse_rate`** — 自动字典序非劣（中间指标）  
3. `mixed` 数量与人审通过率 — 用于沉淀模型  
4. 无解数 — **次要**；无解↓ 但人审非劣不↑ = **不合格**

### 5.3 对照

- 基线：`v4_all_tiers_probe` 中 D5（not_worse_rate ≈ 2.5%，无解 76）
- 阶段 B 合格线（建议）：人审或字典序非劣率先到 ≥20%，再冲 ≥50%（以人审为准）

### 5.4 产物（必须含 HTML 详审包）

根目录：`data/acceptance/d5_motif_probe/`

| 文件 | 内容 |
|------|------|
| `index.html` | 总表：idx、四维 ours/legacy/Δ、自动标签、跳转到个案 |
| `cases/<idx>.html` | **单案详审页**（见下） |
| `cases/<idx>.json` | 机器可读全文（ours + legacy 指标与模式） |
| `summary_brief.json` | 汇总计数 |

**单案 HTML 必须能让人核对拼法/切法，至少包含：**

1. 题头：idx、耗时、solver_path、自动标签  
2. 四维对照表：焊口 / 拼法种类 / 切法种类 / 利用率（ours | legacy | Δ | 优/平/劣）  
3. **拼法明细表**：每种焊模式的段长序列、用量、涉及管型/长度、焊口贡献  
4. **切法明细表**：每种切模式的母料长、段序列、根数、余料/近满标记  
5. 若有 legacy 侧模式（语料里能拿到则展示）：并排或分节对照，便于看「差在哪」  
6. 页内备注区占位（可选：`human_verdict` 下拉建议值：整体更好 / 打平 / 整体更差 / 待定）— 第一轮可为静态，你线下记录也行

过滤：总表可按 `lex_non_worse` / `mixed` / `lex_worse` / `unsolved` 筛选，优先审 `mixed`。

## 6. 明确不做

- 不用 route3「有解优先」挂 V4 尾部充当 D5 方案  
- 不为单题 idx 写死模板  
- 不修改 D1/D2 生产路径以服务 D5  
- 不把「INFEASIBLE 变 FEASIBLE 但全面劣于」算里程碑

## 7. 文件规划（实施时）

| 路径 | 职责 |
|------|------|
| `scripts/solve_d5_motif.py` | D5 求解入口 |
| `scripts/d5_column_factory.py`（可选拆分） | 焊/切列生成 |
| `scripts/probe_d5_motif.py` | D5 复测 + 写 JSON |
| `scripts/probe_d5_motif.py`（报告段） | **复用** `scripts/_gen_report.py` 的 `HTML_TMPL` / `_write_report`；字段对齐 cuts/welds SVG |
| `docs/superpowers/plans/2026-07-24-d5-motif-setcover.md` | 实施计划（确认本规格后写） |

---

## 8. 待确认项

- [x] 认可「B→C」与对标老软件（非有解优先）  
- [x] 四维比较 + **人审确认 mixed**；结果必须 **HTML 详审**（拼法/切法明细）  
- [x] HTML **复用** `_gen_report.py`（已确认）  
- [ ] 阶段 B 合格线数字（建议 ≥20% 人审非劣）是否锁定  
- [x] 进入实施（核心算法已开工）  

