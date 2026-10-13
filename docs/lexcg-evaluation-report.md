# lexcg 列生成词典序评估报告

## 结论

`engine=lexcg` 已接入 `solve_payload`。词典序是用料总长、接头总数、有序拼法种类、切法种类。目标利用率只记录，不作为硬约束。本轮没有改求解算法，只把评测补到全量。

评测仍在进行。CSV 现有 lexcg 84 行、baseline 84 行，两边都到齐的案例 84 / 1589。下面的胜负只统计已经写进 CSV 的行，没有把未跑的案例算成失败。

默认引擎仍是 `baseline`。是否改默认要等全量两边都跑完再判断；未完成时不切换。

## 1508 与 1589

文件 `backend/data/samples/software-io/batch-DGMOM/software_records.json` 共 3177 条，ID 互不重复。

| 口径 | 条数 |
| --- | ---: |
| `legacy_result_status == success`（Result 以 success 开头、利用率 > 0、有切割行） | 1591 |
| 其中清洗后管件需求为 0 | 2 |
| 需求大于 0，且 `parse_problem` 成功 | 1589 |
| 上述 1589 里 `MOMCALCULATESTATUS == 1` | 1506 |
| 全部 success 里 `MOMCALCULATESTATUS == 1`（含 2 条零需求） | 1508 |
| success 且需求大于 0、但计算状态不是 1 | 83（状态 2 有 82 条，状态 0 有 1 条） |

1589 与 1508 不是包含关系。1508 是「老软件结果成功且计算状态为 1」，里面有 2 条零需求；1589 是「结果成功且需求大于 0」，里面有 83 条计算状态不是 1。两边相减的 81 来自这 83 条减去那 2 条。仓库里较早的导出摘要记的成功数是 1590，比当前文件少 1，也不是 1508。本次按要求评测全部 1589 条正需求成功案例，不按计算状态删行。

`docs/legacy-corpus-export-summary.md` 的成功数 1590 来自另一份导出路径上的同结构文件，状态分类也把无法解析的结果记成 `no_parse`，而当前文件里对应的是 `empty` 759 条加结果 JSON 损坏 549 条，再加 `failed:Failed!` 278 条。

## 评测设置

- 引擎：`lexcg` 与 `baseline`，各一条，求解参数 `time_limit_seconds=60`。
- 并行：4 个进程，每个案例单独进程。进程在时限后再过 30 秒仍未退出则杀掉，记 `TIMEOUT`。壁钟超过 60 秒但进程自己返回的，保留该结果，并在 `elapsed_seconds` 里写真实耗时。
- 词典序比较键：（用料，接头，拼法种类，切法种类），小者胜。利用率单独比较，不进入这个键。拼法把 0 接头整管算进去；老软件列是按这个口径重算的，不是旧提取器丢掉整管之后的数字。
- 覆盖：有整数方案且独立验证器通过。验证失败、未解、超时、异常都不算覆盖。
- 77/80 的先前 8 秒前缀已被这次 60 秒结果替换；旧数字还在 git 历史里。
- 缺省锯缝是 5 mm。问题里没写 `BladeMargin` 时，两边都按 5 mm，和老软件导出不一定同一口径，所以相对老软件的用料差混有锯缝差。lexcg 与 baseline 用同一次解析。

## 覆盖

| 引擎 | 已有行 | 解出且验证通过 | 覆盖率（相对 1589） |
| --- | ---: | ---: | ---: |
| lexcg | 84 | 65 | 4.1% |
| baseline | 84 | 74 | 4.7% |

lexcg 已覆盖结果的来源：native 37，warm-start-assisted 28。其中四层都是 proved 的 0 条。

## 相对老软件的词典序

只统计解出且验证通过、并且老软件四项都能读到的行。`not_covered` 是还没有可比较方案的行（未解、超时、验证失败，或该引擎还没跑）。

| 引擎 | 胜 | 平 | 负 | 不可比 |
| --- | ---: | ---: | ---: | ---: |
| lexcg | 34 | 8 | 23 | 19 |
| baseline | 16 | 7 | 51 | 10 |

利用率（高者更好，只在已覆盖的行上比较）：

| 引擎 | 高于老软件 | 相同 | 低于老软件 |
| --- | ---: | ---: | ---: |
| lexcg | 18 | 28 | 19 |
| baseline | 9 | 21 | 44 |

## 相对 baseline 的词典序

两边都已有行的案例 84 条。两边都验证通过时：lexcg 胜 47，平 16，负 1。用料更短：lexcg 33，baseline 1，相同 30。利用率更高：lexcg 33，baseline 1，相同 30。

只有 lexcg 覆盖 1 条，只有 baseline 覆盖 10 条，两边都没覆盖 9 条。

## 失败与超时

类别含义：`timeout` 进程被杀掉；`error` 求解抛错或子进程没交回结果；`verifier_failed` 有方案但验证器拒绝；`infeasible` 求解器声明不可行；`unsolved_pricing_truncated` 定价预算用尽后仍无整数解；`unsolved` 无整数解且没有截断标志。

lexcg 类别计数：{'solved_verified': 65, 'unsolved_pricing_truncated': 17, 'unsolved': 2}

baseline 类别计数：{'solved_verified': 74, 'infeasible': 10}

### lexcg

- `unsolved`：2 条。51abd2ad-4478-4b56-968f-45664e5f1705, be8163b2-5866-416a-b5db-1ea9c9544c18
- `unsolved_pricing_truncated`：17 条。ebf13527-e604-4554-a673-6d5507742f32, 6a4fecf7-d070-4629-8bcc-a0ffa5c3b091, e7838732-3b59-4716-9ca3-3a7a952f6e1c, 95947fc1-483a-434d-95f2-a656981d8fd0, d63121ee-fe51-4908-8aae-c41d363667bd, 074b9061-230a-4888-b404-32aa8af28fe5, 7defcea0-5095-45cf-9b6f-0e033b027791, eabf2b06-3921-43bb-995b-2f7d99b00760, 9cfd3c61-da57-4e78-9d39-ecba829379f7, 6ab108c0-2dd1-4401-8824-d5c576af6d15, 48afa095-f8ec-42d1-901e-a42c9fccc443, 17e6b0d4-c343-4c70-aef3-7bb2bf23cd77, 42b9c5fc-c84a-4468-a15b-d6d8e01bbd7c, a63b97c6-f6d5-4587-b200-f84ed7f0f1de, 581e3490-7831-45c6-9518-006a56871141, 7faf8533-d544-4ef8-9e72-a302e90e0a73, 7d09b887-c05c-4956-a06c-6faae3e7ff62

### baseline

- `infeasible`：10 条。6a4fecf7-d070-4629-8bcc-a0ffa5c3b091, ba90d3b8-ddea-4135-a31c-9b93abf9e455, e7838732-3b59-4716-9ca3-3a7a952f6e1c, d63121ee-fe51-4908-8aae-c41d363667bd, 51abd2ad-4478-4b56-968f-45664e5f1705, 7defcea0-5095-45cf-9b6f-0e033b027791, eabf2b06-3921-43bb-995b-2f7d99b00760, 6ab108c0-2dd1-4401-8824-d5c576af6d15, 17e6b0d4-c343-4c70-aef3-7bb2bf23cd77, 581e3490-7831-45c6-9518-006a56871141

明细：`docs/lexcg-corpus-results.csv`。
