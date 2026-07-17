# 蛇形管一维排料系统

面向锅炉过热器、再热器和省煤器车间的管段切割、拼接与余料优化系统。

## 当前生产基线

- 按 MOM 已处理的材质与规格建立独立原料池；
- `Pipe`按管圈下的管段处理，标识使用`Parent_node + jlxh + cube_no`；
- 自动生成有序管段拼法并避开禁焊区；
- 支持相邻焊口最小距离、有限不定尺库存和切缝；
- 所有生产长度和坐标严格使用整数毫米，小数输入按具体字段拒绝，不静默取整；
- 按利用率、焊口数、拼法种类、切法种类依次优化，并优先重复已启用的标准方案；
- 输出原材料切法、管段拼法、未用整料和新余料；
- 使用独立验证器复算所有长度、数量和工艺约束；
- 同步API与持久化异步任务接口均可使用。

求解采用双路径：优先运行 SCIP 联合 MILP，按“使用原料总长 → 焊口数 → 全局有序拼法数 → 切法数 → 方案复用 → 稳定决胜”逐层优化；候选受限主问题无法覆盖时，自动运行四种确定性排料策略并按相同生产优先级择优。拼法按同一材质规格池内的有序料段序列去重，切法按“原管长度 + 无序切段集合”去重。返回状态`TARGET_REACHED`表示达到输入目标并通过工艺校验；是否完成全部词典序最优证明以每个材质池的`optimization_phases_completed`和`lexicographic_optimal`为准。

## 已验证生产案例

整数毫米生产回归使用真实`Φ51×13`材质池的显式整数化副本，当前稳定结果：

| 指标 | 结果 |
|---|---:|
| 需求总长 | 450,060 mm |
| 使用原材料 | 452,250 mm |
| 综合利用率 | 99.5158% |
| 独立校验 | 0项问题 |

原 MOM 样例中的`pipe_length=17310.2`按新规则作为反例保留，接口会返回`422`并精确指出`input.data[0].Pipe[0].pipe_length`；不会把设计长度静默修改为整数。相同整数输入重复运行时，切法、拼法及`pattern_id`保持一致。

## 本地启动

```powershell
./scripts/setup.ps1
./scripts/run.ps1
```

打开 <http://127.0.0.1:8000>。

健康检查：`GET /api/v1/health`

同步求解：`POST /api/v1/solve`

异步任务：`POST /api/v1/jobs`，随后查询`GET /api/v1/jobs/{job_id}`。

仓库中的`examples-input.json`为整数毫米示例输入，`examples-result.json`为对应的已验证输出。

批处理：

```powershell
./.venv/Scripts/serpentine-nest.exe input.json result.json --time-limit 60
```

运行测试：

```powershell
$env:PYTHONPATH = "$PWD/backend"
./.venv/Scripts/python.exe -m pytest backend/tests
```

## Docker

```powershell
docker compose up --build -d
```

## 可选引擎：等价母材求解（route-2）

除主引擎（两层 MILP）外，提供一个**可选的等价母材 CSP 引擎**，用于补强两层 MILP 解不出的紧组。它默认**关闭**，通过环境变量开启：

```powershell
# 开启（大小写不敏感，取值 1/on/true/yes 之一均可）
$env:NESTING_ROUTE2 = "1"
```

- 开启后，每组在主引擎求解之后运行 route-2，**仅当按词典序（利用率 → 焊口数）严格更优时**才替换结果；引擎不可用或未解出时静默回退，**绝不使任何组变差**。
- 结果与主引擎同 schema，必须通过同一独立验证器。
- 已知边界：对处于可行域边缘（约 0.2% 余量）的超紧组仍可能 `INFEASIBLE`，此时由主引擎兜底。详见 `docs/research/GPU可插拔预解器设计方案.md` §11.12–§11.14。

## 生产边界

每个返回方案必须通过独立验证器；验证失败会被API标记为`verification_failed`，不能作为生产结果。接入车间前仍应完成历史订单批量基准验收，并锁定以下现场口径：

- 输入未提供`BladeMargin`时当前默认0 mm（对齐现场当前排料软件口径）；若现场实际有刀口余量，必须在输入中显式传入`BladeMargin`；
- `pipe_length`、`stock_length`、禁焊区坐标、切缝、最小焊口距离和余料阈值必须为整数毫米；
- 输入未提供可利用余料阈值时，所有正余量均保留并标注；
- 已确认的旧MOM异常`stock_demand="S"`兼容解释为5，同时写入结果警告；正式接口应改为数字；
- `TARGET_REACHED`是生产达标状态，`OPTIMAL`才表示求解器能够证明当前模型最优；
- 切缝损失、不可用废料、可回库余料和未使用整料分别输出，不能混入一个`TrimLoss`字段。
