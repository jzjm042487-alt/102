# 部署指南（Deployment Guide）

> 面向运维/实施：把蛇形管一维排料服务投入生产的完整步骤、配置口径、投产前 checklist 与回滚方案。
> 关联：`README.md`（功能与本地启动）、`docs/段长收敛方案-分级放松.md`（求解器策略）。

## 一、投产前 Checklist

### 必做（阻塞项）

- [ ] **求解时限对齐**：`NESTING_TIME_LIMIT_SECONDS` 默认 120s。真实紧供料组达到词典序最优可能需要 100s+，务必按现场可接受的响应时长设定；长任务改走异步 `/api/v1/jobs`。
- [ ] **BladeMargin（刀口余量）口径确认**：引擎默认 0 mm（对齐当前排料软件）。若现场实际有刀口余量，必须在每次请求的输入里显式传 `BladeMargin`，否则会算出"看似省料但切不出来"的方案。**需与客户/物管书面确认**。
- [ ] **真实批量压力测试**：用一批真实生产单（几十~上百条）跑 `scripts/benchmark_against_software.py`，确认解出率、无崩溃、无异常 422、耗时与内存可接受。
- [ ] **独立验证器通过**：任何返回给车间的方案必须 `verification.passed == true`；`verification_failed` 不得作为生产结果。

### 强烈建议

- [ ] **CORS 收窄**：`NESTING_CORS_ALLOW_ORIGINS` 默认 `*`，前后端分域部署时改为真实前端域名（逗号分隔）。
- [ ] **并发上限**：`NESTING_MAX_WORKERS` 按服务器核数设定（SCIP 单组吃满一核）；并发过高会导致排队超时。
- [ ] **异步优先**：`/api/v1/solve` 为同步阻塞接口，长任务会占用线程池；生产建议统一走 `/api/v1/jobs` 异步 + 轮询。
- [ ] **日志采集**：服务已输出请求耗时、`status`、按材质组的 `solve_status` 分布，接入日志系统便于运维排查。
- [ ] **数据目录持久化**：`NESTING_DATA_DIR`（异步任务 SQLite）挂持久卷并纳入备份（compose 已挂 `nesting-data` 卷）。

## 二、环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NESTING_TIME_LIMIT_SECONDS` | `120` | 每次求解默认时限（秒）；请求可用 `?time_limit_seconds=` 覆盖（1~3600）。 |
| `NESTING_MAX_WORKERS` | `2` | 异步任务线程池大小。SCIP 单组约占一核。 |
| `NESTING_CORS_ALLOW_ORIGINS` | `*` | 允许的跨域来源，逗号分隔；生产建议收窄为真实前端域名。 |
| `NESTING_DATA_DIR` | `<项目>/data` | 异步任务 SQLite 存储目录；容器内为 `/app/data`。 |

> `BladeMargin`、`Target_Util_Rate`、`Min_Welding_Length` 等**工艺参数不是环境变量**，随每次请求的输入 JSON 传入（可按 payload 全局或按材质组设置）。

## 三、部署步骤（Docker，推荐）

```bash
# 1. 构建并后台启动
docker compose up --build -d

# 2. 健康检查（solver 字段应为 SCIP，status=ok）
curl http://<host>:8000/api/v1/health

# 3. 冒烟：用示例输入跑一次同步求解
curl -X POST http://<host>:8000/api/v1/solve \
  -H "Content-Type: application/json" \
  --data-binary @examples-input.json
```

生产环境建议在前面挂 Nginx 反代 + HTTPS，仅暴露 `/api/` 与静态资源。

## 四、健康检查与就绪判断

- `GET /api/v1/health`：
  - `status=ok`、`solver=SCIP`：完全就绪。
  - `status=degraded`、`solver=deterministic-fallback`：**pyscipopt 未装/加载失败**，会退化为启发式，解质量下降。生产**不接受** degraded，需修复依赖后再上线。
- 容器 `HEALTHCHECK` 已内置（30s 间隔，探测 `/api/v1/health`）。

## 五、接口速览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/health` | 健康/求解器状态 |
| POST | `/api/v1/solve` | 同步求解（短任务）|
| POST | `/api/v1/jobs` | 提交异步任务（长任务，返回 202 + job_id）|
| GET | `/api/v1/jobs` | 近期任务列表 |
| GET | `/api/v1/jobs/{job_id}` | 查询任务结果 |

## 六、结果字段与生产判读

- `verification.passed`：**唯一放行门槛**，false 一律不作为生产结果。
- 每个材质组 `metrics.solve_status`：
  - `OPTIMAL_LEXICOGRAPHIC`：词典序最优（可证明）。
  - `FEASIBLE_TARGET_REACHED`：达到目标利用率，可用。
  - `*_INCOMPLETE_*`：预算内未完成全部词典序阶段，返回当前最优可行解。
  - `UNSOLVED`：超时/无解，附 `shortage_diagnosis`（缺料诊断/补料建议或 inconclusive 复核提示）。
- `metrics.candidate_tier`：命中的候选分级（`standard`/`standard_wide`/`full`），运维观测用。

## 七、回滚方案

1. **快速回滚镜像**：保留上一个可用镜像 tag，`docker compose down` 后将 compose 的 image/build 指回上一版本再 `up -d`。
2. **配置回滚**：仅改环境变量导致的问题（如时限过长），直接改 compose 的 `environment` 后 `docker compose up -d` 重建容器；数据卷不受影响。
3. **数据兼容**：异步任务 SQLite 仅存历史任务，回滚镜像不会破坏已完成任务记录；如需清空，删除 `nesting-data` 卷即可。
4. **降级排查**：若 health 变 degraded，先确认镜像内 `pyscipopt` 安装完整（`docker exec <c> python -c "import pyscipopt"`），修复后重建。

## 八、投产后监控建议

- 按日统计各 `solve_status` 占比与平均耗时（日志已含），关注 `UNSOLVED` 比例与超时组。
- 关注 `verification_failed` 出现（应为 0）；一旦非零，立即取样复现并暂停放行。
- 关注 degraded 健康态告警。
