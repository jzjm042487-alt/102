# 真实数据字典与分析：DGMOM 批次（软件对接 + 人工排料）

> 来源：东方锅炉 MOM 系统导出，用户提供（2026-07）。
> 用途：对标学习——把「现有排料软件」「车间人工排料」两套真实结果作为金标准，评估并改进我们的引擎。
> 归档位置：
> - 软件对接输入/输出：`backend/data/samples/software-io/batch-DGMOM/software_records.json`
> - 人工排料切法料单：`backend/data/samples/manual-nesting/batch-DGMOM/manual_cutting_list.csv`
> - 成品管工艺明细：`backend/data/samples/manual-nesting/batch-DGMOM/part_process_detail.csv`

## 一、三份文件的角色

| 文件 | 原名 | 角色 | 规模 |
|---|---|---|---|
| `software_records.json` | DGMOMPTDGMOMGLLXWTFJB.json | 排料软件对接表：每条记录 = 一个材质规格组的输入+输出 | 3177 条记录，62MB |
| `manual_cutting_list.csv` | ...JJWLGLQFLDB.csv | 车间**人工排料的切法料单**（金标准之一） | 44 列，12MB |
| `part_process_detail.csv` | ...JJWLGLLDSJB.csv | 成品管零件工艺明细（弯头/坡口/焊接标准/工时） | 78 列，50MB |

## 二、软件记录 JSON：字段与三类分布

顶层 `{"RECORDS": [ {...32字段...}, ... ]}`，每条记录一个材质规格组。关键字段：

| 字段 | 含义 |
|---|---|
| `MOMPROBLEMJSON` | **排料输入**（单组，顶层直挂 `material`/`specifications`/`Pipe`/`Stock`/工艺参数），3177 条全非空 |
| `MOMRESULTJSON` | **排料输出**（含 `GeneralInfo` 汇总 + `Result` 拼法/切法/余料），2417 条非空 |
| `MOMCALCULATESTATUS` | 计算状态：**0=未计算/未完成，1=成功排出，2=无解/失败** |
| `MOMMATERIAL` / `MOMOUTSIDEDIAMETER` / `MOMWALLTHICKNESS` | 材质 / 外径 / 壁厚 |
| `MOMMATERIALRATE` | 目标利用率（如 0.9925） |
| `MOMPROBLEMNO` / `WORKSHOPID` / `MOMCALCULATETIME` | 问题号 / 车间 / 计算时间 |

**三类分布（状态 × 输出）**：

| 类别 | 判据 | 条数 | 含义 |
|---|---|---|---|
| 第一类·未排产/未完成 | status=0 且输出空 | 760 | 未解出或之前未解出的数据 |
| 中间态 | status=0 且输出非空 | 549 | 有输出但状态未置 1（待确认口径） |
| **第三类·成功排出** | status=1，输出非空 | **1640** | **实际排出结果——对标金矿** |
| 第二类·无解/失败 | status=2，输出非空 | 228 | 求解失败但带诊断输出 |

> 注：3177 条输入全部非空（此格式单组直挂，无 `input.data` 包裹，与早期 `req_92de3aca_input.json` 的多组包裹格式不同——解析时需分别适配）。

### 软件输入 `MOMPROBLEMJSON` 结构（单组）
`material, specifications, Target_Util_Rate, Pipe[], Stock[], Raw_material_price_m, welding_price_per, Adjacent_Distance, Corner_Distance, Adjacent_Corner_Distance, Min_Welding_Length, evaluate_strategy, id`
- `Pipe[]`：`figure_number, jlxh, cube_no, Com_draw_number, Parent_node, Scr_draw_number, pipe_length, pipe_demand, Max_Weldingjoint_Number, Unweldable_Area`
- `Stock[]`：`stock_length, stock_demand, must_use`

### 软件输出 `MOMRESULTJSON` 结构
- `GeneralInfo`：`UtilRate`(利用率), `WeldingJointQuantity`(焊口数), `TimeCost`, `PipeLength_Of_SingleMaterialSpecification`(需求长度), `StockLength_Of_SingleMaterialSpecification`(用料长度), `Result`("Success!")
- `Result.WeldingPattern.WeldingPipe[]`：**拼法** — `FigureNumber, Material, Length(成品管长), InPipeNumber, jlxh, Pattern[{Part:"500 1708 5997 1895", Number}]`
- `Result.CuttingPattern.CuttingPipe[]`：**切法** — `Length(定尺), Part("1895 10100"), TrimLoss(余料), Number(根数)`
- `Result.UnusedMaterials[]`：未用库存

**样例（φ51×9.5，id bbefadac…）**：软件排出 **利用率 99.526% / 焊口 31**；拼法 10100 = `500+1708+5997+1895`；切法反复复用 `2208 / 1708 / 5997 / 500` 等少数段长 → **现实印证「标准段长复用降花样」**。

## 三、人工排料切法料单 CSV（`manual_cutting_list.csv`）

每行 = 一种切法（一种定尺切成一组段长）。关键列：

| 列 | 含义 |
|---|---|
| `MOMCUTTINGNO` | 切法编号 |
| `MOMMATERIAL` / `MOMSPECIFICATIONS` | 材质 / 规格 |
| `MOMMATERIALLENGTH` / `MOMMATERIALQTY` | 定尺长度 / 用几根 |
| `MOMLENGTH1..6` + `MOMQTY1..6` | 该定尺切成的段长(最多6种)及各自段数 |
| `MOMHEADCOUNT` | 段数/头数 |
| `MOMTOTALLENGTH` | 切出总长 |
| `MOMVERIFYRESULT` | 人工校验结论（"正确"） |
| `MOMMANHOURS` / `MOMTUBECUTTINGMANHOURS` | 工时 |

例：定尺 8200 切成 `2415×1 + 2215×1 + 1015×1`（余 140）。

## 四、成品管工艺明细 CSV（`part_process_detail.csv`）

成品管零件级几何/工艺属性，非排料直接输入，但含约束线索：
`MOMBENDNUMBER`(弯头数), `MOMBENDRADIUS`(弯曲半径), `MOMCUTTINGLENGTH`/`MOMCUTTINGQTY`(下料长/数量), `MOMPASSBALLDIAMETER`(通球径), `MOMLEFTWELD`/`MOMRIGHTWELD`(两端是否焊), `MOMWELDINGSTANDARD`(焊接标准), 及大量工时列。

## 五、后续学习/对标计划（未开始）

1. **建解析器**：把 `software_records.json` 拆成「输入」「软件输出指标+拼法+切法」三张规整表；把两个 CSV 归并到规格/图号维度。
2. **对标口径统一**：软件 `GeneralInfo` ↔ 我们 metrics（利用率、焊口、切法/拼法种类、余料）。
3. **抽取样本**：从 1640 条 status=1 成功记录里，按规模/难度分层抽样若干组，用我们引擎重排，逐组对比（用料、焊口、花样种类），量化差距。
4. **学习标准段长**：统计软件/人工在各规格下**高频复用的段长**，验证并标定我们「分级放松」方案里的标准段集与阈值（见 `docs/段长收敛方案-分级放松.md`）。
5. **回补真实工艺参数**：从数据反推 `Max_Weldingjoint_Number`、最小焊距、禁焊区等的真实分布，替换占位默认值。

## 六、对标进展与解析加固（2026-07）

### 6.1 已修复的真实数据解析边界（`domain.py`）

对标 status=1 记录时，发现真实导出数据的 `Unweldable_Area`（禁焊区）字段有多种脏格式，此前解析器只支持规范的 pair 列表，遇到其余格式直接抛 `ValueError` 使整组崩溃。已加固：

| 格式 | 出现次数 | 样例 | 处理 |
|---|---|---|---|
| list of pairs | 20534 | `[["0","150"],...]` | 直接使用（原支持） |
| empty/missing | 7857 | `""` / `[]` | 视为无禁焊区 |
| **纯字符串** | 785 | `"[0.0, 100.0],[1396.7, 1877.34]"` | 正则解析回 pair 列表 |
| **单元素字符串列表** | 574 | `["[0.0, 100.0],[178.0, 708.62]"]` | 取首元素按字符串解析 |
| **端点逆序** | 偶发 | `end < start` | 自动交换并告警，不再崩溃 |
| **零宽区间** | 偶发 | `start == end` | 跳过 |

新增 `_normalize_unweldable_area` / `_parse_area_string` 两个辅助函数；逆序/零宽在 `_parse_pipe` 内容忍并写入 `pipe_warnings`。全套 50 项测试通过，无回归。

### 6.2 对标脚本能力（`scripts/benchmark_against_software.py`）

- `--status` 支持逗号多选（如 `1,0`）；`--sample N --seed` 随机抽样；`--offset/--limit` 分片；`--quiet` 大批量静默；`--out` 落 CSV。
- 汇总输出：求解状态分布、解出率、独立校验通过率、利用率差(我们-软件)的均值/中位/最差/最好、焊口差同上。

### 6.3 首轮抽样画像（status=1，30 条，12s 时限）

- 解出率约 53%；UNSOLVED 约 43%（多为供料比≈1.005 的极紧组、数百根的超大组）。
- 解出组中利用率**中位数与软件基本持平**，但**存在个别组显著落后**（最差 -0.49）：典型是"该焊接跨定尺套裁却整根塞管"的组（如 S30432 φ45×9，我们 85.96% vs 软件 98.54%）。
- **结论**：差距集中在两类——(1) 极紧/超大组求解不出；(2) 低利用率组缺"标准段跨定尺共享+主动焊接"能力。两者都指向 `docs/段长收敛方案-分级放松.md` 的实现。

