# stable 可行性报告

本阶段只回答一件事：`engine="stable"` 能不能稳定给出**通过现有 verifier 的可行计划**。不宣称最优，不跟老软件比胜率，也不改默认引擎。

## 结论

在当前 `software_records.json` 上，正需求且老软件 `GeneralInfo.Result` 以 `success` 开头的记录是 **1591** 条（唯一 ID；计算状态 1/2/0 = 1508/82/1）。任务说明里的 1589 是同一筛选的早期计数，本快照多 2 条状态为 1 的成功记录，评测没有把它们删掉。

按 `scripts/eval_stable_feasibility.py --time-limit 15` 复跑（4 进程），**1424 / 1591 = 89.50%** 给出可行计划且独立校验通过。没有达到 95% 的硬指标。

| 结果 | 条数 |
| --- | ---: |
| 可行且校验通过 | 1424 |
| 策略用尽，未证明不可行（`UNPROVED_NO_SOLUTION`，问题状态 `UNSOLVED`） | 158 |
| 时限内没有解（`TIMEOUT_NO_SOLUTION`，问题状态 `TIMEOUT`） | 9 |

失败类里没有 `INFEASIBLE`，也没有任何 `OPTIMAL`。`INFEASIBLE` 只在准备后的数据上出现长度或最少段数缺口时使用（单测覆盖）。超时不会被改写成不可行。

15 秒是本次测量上限，不是引擎默认值。`solve_payload(..., engine="stable")` 在调用方不给时限时用 **3600 秒**。对上面 9 条超时又用 60 秒重跑了一次：2772、3015、3078 三条由受限主问题找到可行解并过校验，另外 6 条仍然超时。因此正式 3600 秒下限至少是 **1427 / 1591 = 89.69%**。其余 158 条在 15 秒内已经把策略表跑完，加时不会再多试一类构造，不能靠把时限改成 3600 秒补到 95%。

耗时（含校验）：中位数 0.09 秒，P90 10.1 秒。有个别调用超过 15 秒，来自回退分配或受限主问题在建模阶段没有立刻停下。可行解的利用率只作报告：均值 0.962，范围 0.019–1.000，不参与搜索。

## 引擎在做什么

`stable` 接在 `solve_payload` 上，默认引擎仍是 `baseline`。它按固定顺序试构造策略，谁先给出能通过校验的计划就停：

1. 整根、长短定尺剥离、等分拼法
2. `stock-tail`：每根管先占一根定尺，余料再去套剩余库存
3. `complement-mix`：把一根料上的零头拆成能和另一段在同一根定尺上严丝合缝的拼法，再按根数配比
4. 现有 `_solve_group_fallback`（到点停止，避免吃掉整个时限）
5. 模式扫描、自适应剥离
6. 件数不超过 80 时，把 baseline 受限主问题只当作可行性探针

业务参数只在这条路径上改，和 verifier 共用同一份准备后的输入：

- 没写 `BladeMargin` 时用 5 mm，写了就保留
- `Min_Welding_Length` 本阶段忽略（写成 0）
- 不套 `长度 // 3000` 焊口帽，保留输入的焊口上限
- 重复的 `Parent_node|jlxh|cube_no` 只留第一行需求（与老软件产量一致），需求 ≤ 0 的行丢掉
- 禁焊区终点写在起点前面时先交换，解析器和校验器看到的是同一组区间

结果里 `summary.optimality_claimed` 恒为 false，`lexicographic_optimal` 与 `core_lexicographic_optimal` 恒为 false。

## 必测样例

单测 `backend/tests/test_engine_stable.py` 已通过：

- `data/input_a598e024.json`
- `backend/tests/fixtures/production_phi51x13.json`（φ51×13）
- `software_records.json` 第一条（φ51×9.5）

三条都是 `FEASIBLE` 且 verifier 通过。同一文件还检查了：默认引擎在 `valid_case` 上仍是 `TARGET_REACHED` 且不带 `stable` 标记；总供料不够时是 `STOCK_GAP` / `INFEASIBLE`；超时状态不会被叫成不可行。

## 可行解从哪来

| 策略 | 条数 |
| --- | ---: |
| group-fallback | 522 |
| whole-first | 484 |
| complement-mix | 127 |
| stock-peel-long | 106 |
| stock-tail | 56 |
| pattern-scan | 42 |
| uniform-3 | 41 |
| uniform-6 | 19 |
| uniform-4 | 12 |
| stock-peel-short | 10 |
| adaptive-peel-long | 4 |
| uniform-2 | 1 |

没过的题大多是管种多、余量不到需求 2% 的交叉套料。构造策略能配平「一段定尺 + 一段零头」和少量互补焊口，配不平多种管同时拆成三四段、零头又要混排在同一根定尺上的题。这类失败记为未证明，不记为无解。

## 结果文件

- `docs/stable-feasibility-results.csv`：1591 行。列包括 case_id、是否可行、用料、焊口、拼法种类、切法种类、耗时、策略来源、失败类。利用率、校验通过、失败说明是附加列。
- 复现：`PYTHONPATH=backend python3 scripts/eval_stable_feasibility.py --time-limit 15 --workers 4`
- 脚本按 case_id 续跑。本次 CSV 是 15 秒全量结果；其中 8 条原先计划已经生成，但输入禁焊区端点写反导致校验报错，端点交换后重新校验为通过，这 8 行已按当前代码刷新。
