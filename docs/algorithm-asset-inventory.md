# 算法资产盘点（代码现状）

> 盘点日期：2026-07-22 · **本表描述「有什么」**；跑分见 `scripts/algorithm_sweep.py` 与 `docs/algorithm-sweep-report.md`。

## 1. 生产 API 引擎（`solve_and_verify` → `solver.solve_payload`）

| engine | 模块 | 角色 | 失败行为 |
|--------|------|------|----------|
| **baseline** | `backend/app/solver.py` | SCIP 列生成 + Tier 标准段 + fallback | 主路径默认 |
| **v4** | `backend/app/route_v4_arcflow.py` → `scripts/_arcflow_v3.solve_arcflow` | Arc-flow + 分类 + compress | 返回 None → **回退 baseline** |
| **route3** | `backend/app/route3_setcover.py` | 全局集合覆盖 ILP | None → baseline |
| **rcg** | `backend/app/route_b_rcg.py` | 列生成（Route B） | None → baseline |
| **global** | `backend/app/solver_global.py` | 切焊联合全局模型 | None → baseline |

依赖：**PySCIPOpt**（`has_scip`）；无 SCIP 时 baseline 走 `_solve_group_fallback`。

## 2. 可选叠加（非独立 engine 参数）

| 资产 | 模块 | 启用方式 |
|------|------|----------|
| **route2 等价库存 CSP** | `backend/app/route2_equiv.py` | 环境变量 `NESTING_ROUTE2=on`，在 baseline 路径上**择优替换** |
| **GA / GPU 实验** | `scripts/_gpu_patterns.py` 等 | 未接入 `engine=`，研究脚本 |

## 3. 脚本侧核心（不经过 `engine=`，研发/语料常用）

| 资产 | 入口 | 说明 |
|------|------|------|
| **Arc-flow V3 直连** | `scripts/_arcflow_v3.solve_arcflow` | 语料对比 `_corpus_worker` 使用；比 `engine=v4` 多 verbose/参数面 |
| **CutCG 子路径** | `solve_arcflow_cutcg` | arc-flow 内部分支 |
| **列生成实验** | `scripts/_exp_colgen.py` | FFD/CG 窄兜底，`solve_colgen_full` |
| **纯切快路** | `solve_pure_cut`（`_arcflow_v3`） | 0 焊口分类 |

## 4. 本次扫盘默认跑的引擎

与 `benchmark_against_software.py` 对齐，并增加 **arcflow_direct**（记录研发路径与 `v4` 生产包装差异）：

1. `baseline`
2. `route3`
3. `rcg`
4. `global`
5. `v4`
6. `arcflow_direct`（`solve_arcflow`，带 `util_floor`）

不含：`route2`（需环境变量，单独扫盘可加 `--route2`）。

## 5. 输出指标（扫盘记录）

| 字段 | 含义 |
|------|------|
| `solve_status` | 组级求解状态 |
| `verified` | 生产校验器是否通过 |
| `welding_joints` / `weld_pattern_types` / `cut_pattern_types` | 四项对标 |
| `utilization_rate` | 利用率 |
| `elapsed_s` | 墙钟 |
| `error` | 异常摘要 |

对照老软件：`metrics.json` 中 legacy 七项（有则填 `legacy_*`）。

## 6. 相关文档

- V4 / 路线：`docs/V3_HANDOFF.md`、`docs/v4-production-ultimate-plan.md`
- Global：`docs/research/global-joint-model-spec-v1.md`
- 语料难度：`docs/legacy-corpus-difficulty-taxonomy.md`
