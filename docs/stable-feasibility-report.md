# stable 可行性报告

本阶段只回答一件事：`engine="stable"` 能不能稳定给出**通过现有 verifier 的可行计划**。不宣称最优，不跟老软件比胜率，也不改默认引擎。

## 结论

在当前 `software_records.json` 上，正需求且老软件 `GeneralInfo.Result` 以 `success` 开头的记录是 **1591** 条（唯一 ID；计算状态 1/2/0 = 1508/82/1）。任务说明里的 1589 是同一筛选的早期计数，本快照多 2 条状态为 1 的成功记录，评测没有把它们删掉。

95% 门需要 **1512 / 1591**。按 `scripts/eval_stable_feasibility.py --time-limit 15` 全量重跑（4 进程，删掉旧 CSV 后重写），**1429 / 1591 = 89.82%** 给出可行计划且独立校验通过。还差 **83** 条，没有达到 95%。

| 结果 | 条数 |
| --- | ---: |
| 可行且校验通过 | 1429 |
| 策略用尽，未证明不可行（`UNPROVED_NO_SOLUTION`，问题状态 `UNSOLVED`） | 154 |
| 时限内没有解（`TIMEOUT_NO_SOLUTION`，问题状态 `TIMEOUT`） | 8 |

失败类里没有 `INFEASIBLE`，也没有任何 `OPTIMAL`。`INFEASIBLE` 只在准备后的数据上出现长度或最少段数缺口时使用（单测覆盖）。超时保持 `TIMEOUT`。1429 条可行行的「校验通过」都是「是」。

相对上一次 15 秒结果（1424 / 1591 = 89.50%）净增 **5** 条。逐案对照：7 条新解出，2 条上次可行、这次策略表走完仍无解。

新解出：

| 记录 | case_id | 上次 | 这次 | 耗时 |
| --- | --- | --- | --- | ---: |
| 1361 | `e3c7fee8-c0ac-4813-bae7-87f1ea3b8043` | UNSOLVED | adaptive-peel-long | 4.095 s |
| 1843 | `312a0381-85d4-4a5f-b7fb-196281e392bf` | UNSOLVED | cross-complement | 0.429 s |
| 2386 | `809bf44b-e2e2-4a7f-9c7f-14a4e7b19741` | UNSOLVED | cross-complement | 0.441 s |
| 2810 | `229ca50c-dc70-450e-ba8e-67a94a4a04ba` | UNSOLVED | pattern-scan | 2.392 s |
| 2812 | `e66fb8ce-a7ee-4b51-a834-a2405e9871e0` | UNSOLVED | pattern-scan | 2.354 s |
| 2854 | `f9e0be36-005c-4284-bc18-09da5a95a89f` | UNSOLVED | pattern-scan | 2.978 s |
| 2871 | `a2b9a1a2-4d36-4eab-84c4-ebd45d2361f7` | TIMEOUT（上次 114 s） | multi-stock-embed | 0.809 s |

2854 属于下面「管身份」那 14 条：遗留焊口行用的是 `figure_number`，求解器自己的计划仍用 `Parent_node|jlxh|cube_no`，这次校验通过。

上次可行、这次 15 秒内策略表走完仍无解（单独再跑 15 秒，结果相同）：

| 记录 | case_id | 上次 | 这次耗时 |
| --- | --- | --- | ---: |
| 2502 | `97517c72-a672-466e-a6f2-35bc48886a1a` | group-fallback，10.379 s | 12.490 s，`UNSOLVED` |
| 2830 | `eb562f68-8403-4819-87f7-5a4f98ad7611` | pattern-scan，2.754 s | 3.106 s，`UNSOLVED` |

这两条没有被记成不可行。新策略插在 `group-fallback` 前面，回退搜索分到的时间变短；`pattern-scan` 仍只扫自己的 2 秒切片。

15 秒是本次测量上限，不是引擎默认值。`solve_payload(..., engine="stable")` 在调用方不给时限时用 **3600 秒**。本次没有重跑 60 秒或 3600 秒。

耗时（含校验）：中位数 0.086 秒，P90 10.087 秒，最大 27.844 秒。有个别调用超过 15 秒，来自回退分配或受限主问题在建模阶段没有立刻停下。可行解的利用率只作报告：均值 0.962，范围 0.019–1.000，不参与搜索。

## 哪些超时需要更长时间

8 条超时都超过了 15 秒墙钟（策略在时限边界上才停）。按上次引擎的 60 秒重跑来标注；那个 60 秒结果是旧策略表测的，这次代码没有重测。

| 记录 | case_id | 本次耗时 | 上次 60 秒 |
| --- | --- | ---: | --- |
| 2772 | `38f86cc8-968a-4e79-bed0-773b43e5b909` | 15.012 s | 受限主问题解出并过校验 |
| 3015 | `f5f9cb92-9c54-4919-b5c4-f5b700d6a390` | 15.417 s | 受限主问题解出并过校验 |
| 3078 | `a564944f-f2cd-44e9-bdd3-46ff4732ef87` | 15.181 s | 受限主问题解出并过校验 |
| 2972 | `dd83bc1b-8257-4b0e-b452-1ab8ab2c6699` | 27.844 s | 仍超时 |
| 2975 | `0b768805-2b75-49f5-8b0d-429d9f04d3ab` | 27.611 s | 仍超时 |
| 3043 | `b675be9e-58a5-4155-899a-baa6ba9d38e3` | 27.777 s | 仍超时 |
| 3072 | `07039e33-15d3-4476-af58-c4bb9d16ecec` | 27.599 s | 仍超时 |
| 3114 | `d5271777-5c00-4e60-ba2f-c65f2149e408` | 20.023 s | 仍超时 |

2772、3015、3078 是明确需要长于 15 秒的三条：上次在约 60 秒由受限主问题解出。若这三条在新代码上仍然如此，正式时限的下限是 **1432 / 1591 = 90.01%**。这是条件推断，不是这次测到的数。

2972、2975、3043、3072、3114 上次 60 秒仍超时，本次已经跑到 20–28 秒。2871 已离开超时列表。154 条 `UNSOLVED` 在 15 秒内把策略表跑完，加时不会再多试一类构造。

## 这次改了什么

`stable` 接在 `solve_payload` 上，默认引擎仍是 `baseline`。构造顺序是：整根、长短定尺剥离、等分、`stock-tail`、`complement-mix`，然后下面四条新策略，再是到点停止的 `_solve_group_fallback`、模式扫描、自适应剥离，件数不超过 80 时才把 baseline 受限主问题当作可行性探针。谁先给出能通过校验的计划就停。

业务参数只在这条路径上改，和 verifier 共用同一份准备后的输入：

- 没写 `BladeMargin` 时用 5 mm，写了就保留
- `Min_Welding_Length` 本阶段忽略（写成 0）
- 不套 `长度 // 3000` 焊口帽，保留输入的焊口上限
- 重复的 `Parent_node|jlxh|cube_no` 只留第一行需求，需求 ≤ 0 的行丢掉
- 禁焊区终点写在起点前面时先交换

结果里 `summary.optimality_claimed` 恒为 false，`lexicographic_optimal` 与 `core_lexicographic_optimal` 恒为 false。

### 必用料：默认关闭

遗留方案不执行「用了可用料就必须把必用料用完」。`NestParam.EnforceMustUse`（也认 `enforce_must_use`，以及根上的同名键）现在是开关：

- 调用方没写这个键：stable 准备阶段写成 `0`，verifier 的 `must_use_enforced` 返回 false，不报 `MUST_USE_STOCK_NOT_USED`
- 调用方写成 1 / true / 是：stable 和 verifier 都执行原来的组级优先规则（用了可用料，必用料就要用完；组里没有可用料时，只用一部分必用料仍然合法）
- `baseline` 的回退和 SCIP 默认参数仍是 `enforce_must_use=True`。只有 stable 传入 false

这个开关不改库存数量，指标仍按输入根数对账。

### 禁焊区：闭区间，含端点

`Interval.contains` 仍是 `start ≤ 位置 ≤ end`。端点上的焊口不合法。新策略只接受 `_acceptable` 放过的拼法，焊口往合法侧取（端点外侧），不把遗留方案里落在端点上的焊口抄进来。单测 `test_multi_stock_embed_rejects_closed_zone_endpoints` 覆盖了这件事。

### 四条构造策略，以及比最长优先更宽的装箱

诊断见 `docs/stable-unsolved-diagnosis.md`。四条策略都有大约 1 秒的本地时限；本地超时只在全局时限已到时才向上抛。它们使用 `_Pool.pack_flexible`：在原有 `pack_counter` 失败之后，再试四种「先定种子、再把一根定尺填满才开下一根」的顺序。默认装箱路径不走这四种顺序，避免拖慢 `complement-mix` 和 `pattern-scan`。

`_patterns_for` 在按余料排序之后，保留每一条已经含有定尺长度的拼法，其余仍最多 18 条。补齐生成仍停在 36 条。`pattern-scan` 另有 2 秒切片。

| 策略 | 在做什么 | 本次成为首个成功策略的条数 |
| --- | --- | ---: |
| `cross-complement` | 两段互补：一段是定尺或整管，另一段是 `定尺 − 刀缝 − 那段`。先装全需求，再只拆仍停在整管上的管子 | 22 |
| `shared-tail` | 三段：两根管共用一条跨管尾料，头段再拆成合法焊口 | 2 |
| `multi-stock-embed` | 一根管嵌入 1–3 段定尺，零头可以在前、中、后；只取最长的 8 种定尺 | 1 |
| `short-stock-split` | 只有一种管时，把长段再拆开去填短定尺 | 0 |

22 条 `cross-complement` 里，18 条上次由 `group-fallback` 解出，2 条上次由 `pattern-scan` 解出，2 条（1843、2386）是新解。2 条 `shared-tail`（2826、2828）上次都是 `group-fallback`。唯一的 `multi-stock-embed` 是 2871。`short-stock-split` 没有成为任何一条的首个成功策略。诊断里的四类结构因此只被这张表盖住一小部分；不能把「策略写了」说成「那一类已经解出」。

## 可行解从哪来

| 策略 | 条数 |
| --- | ---: |
| group-fallback | 501 |
| whole-first | 484 |
| complement-mix | 128 |
| stock-peel-long | 106 |
| stock-tail | 56 |
| pattern-scan | 43 |
| uniform-3 | 41 |
| cross-complement | 22 |
| uniform-6 | 17 |
| uniform-4 | 12 |
| stock-peel-short | 10 |
| adaptive-peel-long | 5 |
| shared-tail | 2 |
| uniform-2 | 1 |
| multi-stock-embed | 1 |

`group-fallback` 从 522 降到 501，是因为新策略排在它前面，先解出了其中一部分。

## 管身份：`figure_number` 和 `Parent_node`

这次不改身份逻辑。下面是数据，供决定用哪一个字段当管号。

统计对象是求解器能看见的 `Pipe` 列表（扁平载荷，或只有 `OriginalProblem` 时用里面的 `Pipe`），并且 `pipe_demand > 0`。全库 3177 条记录都至少有一行正需求，合计 **29378** 行。另有 418 行需求 ≤ 0，没有计入。`figure_number == Parent_node` 的有 28815 行；两者不同的有 **563** 行，分布在 **92** 条记录里。

全库字符串（同一字符串在多条记录里合并）：

| 字段 | 不同取值 | 对应另一字段恰好 1 个 | 对应 2 个 | 对应更多 |
| --- | ---: | ---: | ---: | --- |
| `figure_number` | 1550 | 1525 | 24 | 1 个图号对应 24 个父节点：`197N2214MX` |
| `Parent_node` | 1537 | 1477 | 60 | 没有父节点对应 3 个及以上图号 |

在单条记录内部，按字符串计数（还没拼 `jlxh|cube_no`）：图号对应的父节点个数是 {1: 27327, 2: 48, 24: 2}；父节点对应的图号个数是 {1: 27285, 2: 93}。92 条「有差异」的记录里，56 条在记录内部是字符串一一对应，12 条是一个图号对多个父节点，22 条是一个父节点对多个图号，2 条两种都有。

带 `jlxh|cube_no` 之后，重复三元组出现在同样的 16 条记录里（59、60、1282、1284、1286、1287、1299、1300、1917、1918、1920、1921、2117、2118、2119、2120）。这 16 条上，按父节点三元组合并的行，和按图号三元组合并的行，分组完全相同。全库 3177 条都是这样：两种三元组的重复分组没有差别。诊断里的 14 条上，两种三元组都唯一。

遗留焊口输出的 `WeldingPipe` 没有 `Parent_node`。1591 条带焊口结果的成功记录里，1587 条的键是 `FigureNumber + jlxh + InPipeNumber + Length + Material + Pattern`，另外 4 条连 `jlxh` 都没有。`InPipeNumber` 对应输入的 `cube_no`。拿 `FigureNumber|jlxh|InPipeNumber` 去对输入：

| 对上的方式 | 成功记录数 |
| --- | ---: |
| 图号三元组与父节点三元组同时对上（两者字符串相同） | 1541 |
| 只对上图号三元组 | 38 |
| 同一条记录里两种都有 | 6 |
| 两套三元组都对不上 | 5 |
| 同时对上，另有对不上的行 | 1 |

那 38 条里，每一行正需求的图号都和父节点不同，每一行焊口都对上图号三元组，对上父节点三元组的是 0。诊断里校验报 `UNKNOWN_PIPE` 的 14 条是这 38 条的子集，当时落在未解集合里。14 条全部是：管子根数 = 差异根数 = 焊口行数，两种三元组都唯一，焊口行没有 `Parent_node`。

| 记录 | case_id | 管子 / 焊口 | 对上图号 | 对上父节点 |
| --- | --- | ---: | ---: | ---: |
| 774 | `78f407a0-04d2-4626-93a5-82baf94b2b55` | 16 | 16 | 0 |
| 776 | `e4103719-df04-4f15-b86b-f1920e2b32d4` | 16 | 16 | 0 |
| 941 | `e86661a8-cc1e-413c-b1ee-fb81f322e2d7` | 6 | 6 | 0 |
| 1139 | `e12efc0a-3539-44d3-b61a-5b8578d8d03f` | 6 | 6 | 0 |
| 1601 | `e2c42a33-c71a-4a9e-afd7-1593169ad5d2` | 12 | 12 | 0 |
| 1602 | `82cfc78e-97c8-4eba-b16d-2fc2d7d9218c` | 6 | 6 | 0 |
| 1603 | `6d12a26e-3d7d-4add-99c0-5492ec421ea4` | 12 | 12 | 0 |
| 1604 | `42eea3ac-2207-4436-93b3-9f2b25660f34` | 6 | 6 | 0 |
| 2821 | `900f3f5d-e6f8-4dd8-9b3f-7874d8ed4ebe` | 12 | 12 | 0 |
| 2849 | `61c9c0fe-7727-4ab1-a40f-9ae31382bb09` | 12 | 12 | 0 |
| 2851 | `ae640dc4-3149-4dbc-be04-b87290c398e0` | 12 | 12 | 0 |
| 2852 | `6b5b278b-934a-43c6-957a-fc5a2a587f6e` | 6 | 6 | 0 |
| 2854 | `f9e0be36-005c-4284-bc18-09da5a95a89f` | 12 | 12 | 0 |
| 2980 | `f7c18ba2-348a-4d0b-aeb4-a140b55c65a0` | 18 | 18 | 0 |

例子（记录 774，输入三行与遗留焊口三行）：

| 来源 | figure / FigureNumber | Parent_node | jlxh | cube / InPipeNumber | 长度 |
| --- | --- | --- | --- | --- | ---: |
| 输入 | `2213-000041` | `283M2213-2-4` | 43 | 1 | 22718.10 |
| 输入 | `2213-000036` | `283M2213-1-5` | 13 | 1 | 19306.30 |
| 输入 | `2213-000039` | `283M2213-2-2` | 39 | 1 | 19714.01 |
| 焊口 | `2213-000033` | （行上没有该字段） | 7 | 1 | 20282 |
| 焊口 | `2213-000034` | （行上没有该字段） | 9 | 1 | 20253 |
| 焊口 | `2213-000035` | （行上没有该字段） | 11 | 1 | 23287 |

焊口行的 `FigureNumber|jlxh|InPipeNumber` 等于输入的 `figure_number|jlxh|cube_no`。把它当成 `Parent_node|jlxh|cube_no` 就对不上，verifier 报 `UNKNOWN_PIPE`。

只用其中一个字符串、不带 `jlxh|cube_no`，会把不同管子并成一根：

- 一个图号、两个父节点。记录 175，`6bcc57d4-75ab-430a-a42a-a36ad53dd0fd`：图号 `196N211-1-10` 同时是父节点 `196N211-1-9`（jlxh 30，cube 1，长 2534，需求 38）和父节点 `196N211-1-21`（jlxh 107，cube 1，长 2534，需求 38）。只留图号会把需求收成 76。
- 一个父节点、两个图号。记录 365，`e01897df-dd18-46ef-819c-d2253768a1e1`：父节点 `196N211-1-10` 下面是图号 `196N211-1-9`（jlxh 30，长 2510.79，需求 38）和图号 `196N211-1-21`（jlxh 107，长 2510.79，需求 38）。只留父节点会把需求收成 76。
- 图号和父节点不同，但记录内部仍是一对一。记录 7，`62f3d903-1551-4754-a7fe-675c4ac6bb78`：两行都是图号 `192N2421-1-1`、父节点 `192N2421-1-0`、长 1606，靠 jlxh/cube 分开（4/2 需求 182，5/3 需求 4）。只留图号或只留父节点都会把这两根并掉。三元组能分开。

**建议（尚未改代码）：** 求解器的需求身份继续用 `Parent_node|jlxh|cube_no`。读遗留焊口时用 `figure_number|jlxh|cube_no` 去对，因为输出列就是 `FigureNumber` 和 `InPipeNumber`。以后若要自动对上，可以先试父节点三元组，焊口行没有 `Parent_node` 时再退到图号三元组。不要把父节点换成图号当唯一身份：两个字段在 563 行上不是同一个值；丢掉 `jlxh|cube_no` 之后，175、365、7 都会并错管子。在本库上，两种三元组的重复分组相同，所以「只把三元组里的字符串从父节点换成图号」不会改变这 16 条的去重结果，但会改掉求解器写出的 `pipe_id`，已经按父节点对过的计划会对不上。

## 必测样例

`PYTHONPATH=backend python3 -m pytest -q backend/tests/test_engine_stable.py backend/tests/test_verifier.py backend/tests/test_solver.py`：63 项通过。

其中 stable 单测仍覆盖：

- `data/input_a598e024.json`
- `backend/tests/fixtures/production_phi51x13.json`（φ51×13）
- `software_records.json` 第一条（φ51×9.5）

三条都是 `FEASIBLE` 且 verifier 通过。同一文件还检查了：默认引擎在 `valid_case` 上仍是 `TARGET_REACHED` 且不带 `stable` 标记；总供料不够时是 `STOCK_GAP` / `INFEASIBLE`；超时状态不会被叫成不可行；必用料缺省关闭、显式打开时生效；定尺对齐的两段拼法不会被 18 条上限丢掉；闭区间禁焊区的端点不会成为焊口。

## 结果文件

- `docs/stable-feasibility-results.csv`：1591 行。列包括 case_id、是否可行、用料、焊口、拼法种类、切法种类、耗时、策略来源、失败类。利用率、校验通过、失败说明、状态是附加列。
- 复现：`PYTHONPATH=backend python3 scripts/eval_stable_feasibility.py --time-limit 15 --workers 4`
- 脚本按 case_id 续跑。这次 CSV 是删掉旧文件之后的 15 秒全量结果。
