# lexcg 列生成词典序评估报告

## 结论

`engine=lexcg` 已接入 `solve_payload`。词典序锁定为：用料总长、接头总数、有序拼法种类、切法种类。目标利用率只作标志，不作为硬约束。定价和切割搜索都有时间与节点预算；预算用尽时记为 incumbent 或 unproved，不报 `INFEASIBLE`，也不报 `OPTIMAL_LEXICOGRAPHIC`。

默认引擎仍是 `baseline`。本报告只跑完语料成功集的前 80 条（时限 8 秒），不是规格里的全量 60 秒评测。在这 80 条里，两边都给出并通过独立验证的 60 条中，lexcg 的词典序向量更好的有 43 条，baseline 更好的有 2 条，相同的有 15 条。另有 10 条只有 baseline 在预算内求出可行解。覆盖面还没有超过 baseline，因此没有把默认引擎换成 lexcg。

## 业务规则（按本次任务，不按旧 SPEC 的目标顺序）

1. 最小化总用料长度（料长 × 根数）。
2. 锁定第 1 层，最小化接头总数。
3. 锁定前两层，最小化拼法种类。拼法键是有序段长序列；同一序列在不同管子上算一种，反序算另一种。整管（0 个接头）计入拼法。
4. 锁定前三层，最小化切法种类。切法键是料长加段长多重集。

没有加权求和。`Min_Welding_Length` 不参与接头上限，但列必须满足验证器的相邻焊缝间距，否则方案会被拒绝。`Min_Cut_Length` 在两段及以上时仍然生效。锯缝默认 5 mm，模式 `BETWEEN_PARTS`（n 段收 n−1 刀，不计入零件长度）。求解器和验证器缺省值一致。输入里的 `Max_Weldingjoint_Number=2000` 表示不限制接头，不再用 `length // Weld_Interval_mm` 或 `Max_Joints_Cap` 去夹这个业务上限。baseline 的候选生成仍使用「每 3000 mm 一段、最多 13 个接头」的搜索预算，只为保住原回归用例的列池，不写回管子的接头上限。

必用料与验证器一致：一旦使用了非必用料，必用料必须用完；只用必用料且没有用完是允许的。段长产销在输出上取等式，避免 `SEGMENT_BALANCE_MISMATCH`。暖启动只来自 `_solve_group_fallback`，列标记为 `warm-start`；结果来源区分 `native` 与 `warm-start-assisted`。lexcg 失败时不会改标成 baseline。

利用率比较已改为分数对分数（`achieved + 1e-12 >= target_rate`）。原先 `achieved * 100` 会把大约 1% 以上的方案都当成已经达标。

## 验收用例

三例都通过修改后的独立验证器。

| 用例 | 用料 | 接头 | 拼法 | 切法 | 来源 | 说明 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `data/input_a598e024.json` | 170136 | 0 | 2 | 2 | native | 整管下料 |
| `production_phi51x13` | 452250 | 39 | 2 | 5 | native | 未注入回退段长；接头 ≤ 44，拼法 ≤ 6 |
| `req_92de3aca` 组下标 3，φ51×9.5 | 264550 | 20 | 2 | 4 | warm-start-assisted | 验证通过 |

φ51×13 的四层都是 incumbent，不是 proved。该管 `max_joints=2000`，拼法族不是穷举，不能标 `OPTIMAL_LEXICOGRAPHIC`。关掉暖启动后同一用料、接头 39、拼法 2、切法 5，来源 native。φ51×9.5 关掉暖启动后同样验证通过，来源 native，接头数高于 20。

另有一个管长 200、接头上限 0、两根 200 mm 料的小例，四层均为 proved，状态 `OPTIMAL_LEXICOGRAPHIC`。节点预算被截断且没有整数解时，状态是 `UNSOLVED`，`infeasibility_proved` 为假，不报不可行。切割背包在料长 100000、段长 `{1,2,3,5,7,11}`、节点预算 5000 时于 2 秒内返回，`truncated=true`，节点数贴着预算，只重建一个方案。

`backend/tests/test_lexcg.py`、`test_solver.py`、`test_verifier.py` 已通过。整数 φ51 回归仍要求默认引擎用料 452250，该断言仍然成立。

## 语料：跑完了什么

语料文件 `backend/data/samples/software-io/batch-DGMOM/software_records.json`。`legacy_result_status == success` 的记录是 1591 条，其中管件需求大于 0 的是 1589 条。任务说明里的 1508 与这次按该状态函数计出的条数不一致，本报告以 1589 为准，没有为了凑 1508 去删记录。

全量按每条 60 秒、两个引擎估算是数万秒，本次没有跑。实际完成的是成功集按文件顺序的前 80 条，每条 `lexcg` 与 `baseline` 各一次，求解时限 8 秒。进程若超过时限 25 秒会被杀掉并记为 `TIMEOUT`。两段壁钟时间大约是 112 秒和 115 秒。明细在 `docs/lexcg-corpus-results.csv`（160 行，80 个案例 × 2 个引擎）。

这 80 条的材料以 SA-213TP310HCbN、SA-213S30432、SA-213T92 为主，管件需求中位数 186。其中 77 条问题没有写 `BladeMargin` / `NestParam.BladeMargin`，求解与验证都按新的默认锯缝 5 mm。老软件导出通常不按这个缺省锯缝计料，所以下面「相对老软件」的用料好坏混有锯缝口径差。lexcg 与 baseline 之间的对比使用同一次解析，口径相同。拼法统计把 0 接头的整管算进去；旧提取器会丢掉这些序列，CSV 里的老软件拼法是按本任务口径重算的。

### 相对老软件（词典序：用料、接头、拼法、切法）

| 引擎 | 更好 | 更差 | 相同 | 未解 / 超时 |
| --- | ---: | ---: | ---: | ---: |
| lexcg | 29 | 25 | 7 | 19 未解 |
| baseline | 12 | 51 | 7 | 9 未解，1 超时 |

lexcg 已解出的 61 条全部通过独立验证。没有把超时或截断记成不可行。

### 相对 baseline（同一 8 秒名义预算）

两边都验证通过的有 60 条：lexcg 词典序更好 43，baseline 更好 2，完全相同 15。用料更短的是 lexcg 29 条、baseline 2 条、相同 29 条。接头更少的是 lexcg 31 条、baseline 14 条、相同 15 条。

只有一边解出：仅 lexcg 1 条，仅 baseline 10 条，两边都没有 9 条。baseline 有 14 条壁钟超过 8.5 秒（分级松弛没有在时限处干净停下），其中 1 条被进程杀掉，记为 `TIMEOUT`。lexcg 有 4 条略超过 8.5 秒。

lexcg 结果来源：native 54 条，warm-start-assisted 26 条。四层证明状态里，没有一条四层都是 proved。46 条四层都是 incumbent，19 条四层都是 unproved（没有整数解），其余停在某一层 incumbent、后面 unproved。

## 默认引擎

不切换。原因有三条：评测只覆盖 1589 条里的前 80 条；这 80 条里 baseline 仍多解出 10 条；大订单的拼法族在预算内不能穷举，不能把 incumbent 当成已证明的词典序最优。现有不指定 `engine` 的回归仍走 baseline。

## 搜索预算

焊缝定价在几何族上按对偶重打分，不是对每一毫米做资源约束最短路。切割背包每次只重建一个方案，相同段长只保留一个最满列，而不是每个合法副本一列。节点或时间用尽就停，并在指标里标 `pricing_truncated`。未证明的层状态是 `incumbent` 或 `unproved`。
