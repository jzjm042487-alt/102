# lexcg 列生成词典序评估报告

## 结论

`engine=lexcg` 已接入 `solve_payload`。词典序是用料总长、接头总数、有序拼法种类、切法种类。目标利用率只记录，不作为硬约束。本轮没有改求解算法，只把评测补到全量。

评测仍在进行。CSV 现有 lexcg 663 行、baseline 662 行，两边都到齐的案例 662 / 1589。下面的胜负只统计已经写进 CSV 的行，没有把未跑的案例算成失败。

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
| lexcg | 663 | 439 | 27.6% |
| baseline | 662 | 555 | 34.9% |

lexcg 已覆盖结果的来源：native 229，warm-start-assisted 210。其中四层都是 proved 的 0 条。

## 相对老软件的词典序

只统计解出且验证通过、并且老软件四项都能读到的行。`not_covered` 是还没有可比较方案的行（未解、超时、验证失败，或该引擎还没跑）。

| 引擎 | 胜 | 平 | 负 | 不可比 |
| --- | ---: | ---: | ---: | ---: |
| lexcg | 211 | 133 | 95 | 224 |
| baseline | 126 | 115 | 314 | 107 |

利用率（高者更好，只在已覆盖的行上比较）：

| 引擎 | 高于老软件 | 相同 | 低于老软件 |
| --- | ---: | ---: | ---: |
| lexcg | 62 | 288 | 89 |
| baseline | 45 | 350 | 160 |

## 相对 baseline 的词典序

两边都已有行的案例 662 条。两边都验证通过时：lexcg 胜 231，平 138，负 41。用料更短：lexcg 111，baseline 32，相同 267。利用率更高：lexcg 111，baseline 32，相同 267。

只有 lexcg 覆盖 28 条，只有 baseline 覆盖 145 条，两边都没覆盖 79 条。

## 失败与超时

类别含义：`timeout` 进程被杀掉；`error` 求解抛错或子进程没交回结果；`verifier_failed` 有方案但验证器拒绝；`infeasible` 求解器声明不可行；`unsolved_pricing_truncated` 定价预算用尽后仍无整数解；`unsolved` 无整数解且没有截断标志。

lexcg 类别计数：{'solved_verified': 439, 'unsolved_pricing_truncated': 134, 'unsolved': 88, 'infeasible': 2}

baseline 类别计数：{'solved_verified': 555, 'infeasible': 106, 'timeout': 1}

### lexcg

- `infeasible`：2 条。c5b6ed42-7753-4a58-8df7-feca17e960ce, dfbdd35f-dabb-44e6-bcf5-7c1c97081ebc
- `unsolved`：88 条。51abd2ad-4478-4b56-968f-45664e5f1705, be8163b2-5866-416a-b5db-1ea9c9544c18, 263a87ba-9579-4cab-b054-ce44ad682ead, 6de75e7d-8d91-461e-bed7-460e65910c1f, acfa103c-d1e5-445e-bfa3-b1275e10c36f, bc44ade5-fedd-4614-8200-801a4527e16b, 97daace3-94d2-4167-bbd4-d752d4e18a30, da759244-2729-41be-9361-ae5fe9e9729f, 94d6e633-99e5-42fb-a6eb-aa4be749259d, c17c8302-07c3-43df-89ee-5cd4e5d8a8c0, f354a356-3c22-4a6e-b527-3095a79f44d9, 952d6591-7503-41e5-a9ff-093a680897e2, 6f62fd56-d7e0-4e3d-9538-5e7e8ccf072b, 7b89ccf5-5861-4819-ba8a-e42a5f1aa23e, a6120171-8598-412e-bda1-e5efb2c00082, 47c1a028-8b2e-47af-8162-9af4a529a1e5, 869a1e81-ac29-43ba-aaff-67d7048eeac3, d98bd5fc-00d3-403a-a9f5-670daaf2ffee, 1e9b3aee-02d1-4fdc-9ff4-a320e8574920, 78f407a0-04d2-4626-93a5-82baf94b2b55, e4103719-df04-4f15-b86b-f1920e2b32d4, 3d485cfb-3c54-43a3-9d22-b9d6ea6e737d, cc356b83-dce9-4501-b404-4e8a9e4452ee, ba8b38bd-c795-42d5-806d-ccf5cd7ed5b0, 50a47a35-9cc8-4b15-b578-1f0500a8a048, 7a62e7a8-f459-44ac-acfe-56f341507ea5, 755a3887-3580-40fc-a67f-792924f647d6, 097afd3c-4274-4c32-adae-04eae5976e80, 39e65882-b6be-4883-81ea-6bd6b0db2d40, 8fd0d6cc-6fbb-4543-a6fa-1086b73fef94, deb854dd-cb18-4fa4-b610-be01ff9fa1e6, 72976af3-6f54-434f-975b-cd4223de0590, ebb28df9-d846-4c03-839a-150d4ca67fc8, c6934300-5cbc-4dd6-869a-49880c07f277, e1b0c46a-1a02-4109-9e60-c479848c01ac, d51e1a45-ed7b-4522-a3ca-7f478979586f, 160654cf-b850-466a-bc8e-b02380767cb6, 8cf10c5d-1d62-458a-b280-654107922bc3, 1c82a3bc-de82-487e-9bfc-45993221e714, 205d5f19-bc98-4339-9ab9-c00e3e2416df, 75a67df8-139e-4991-a74d-9641d9003f06, 31f1e411-a59a-4cd6-a8dc-42705e1cbeec, 8b0c0acf-01bf-4b79-8d98-17e7c4f5719a, 1a9d9dc1-5adb-4208-adf9-ff26072fd5ec, b8588a1e-a869-4718-a6f8-5841816e1689, dde9fe06-cedf-4890-b832-32a018a90acb, e417e1fe-8582-4648-b376-e3192d2b52c7, 94021069-d415-4988-9c30-b2450f7c16a7, f1e6da21-d229-413b-b1d1-9e9989a42156, d588b2ab-d7f9-48b8-a102-f06b5dfc3234, 9e6c7ca8-6a4a-496f-83c2-a65f7014aa4f, 61611fe8-3fe0-40a2-9ab9-8c5be03b96c3, 5b86d8f5-9e6a-49b9-b919-8b731b2c6961, ba6e2bd0-2d2d-43ba-bda9-7c20f590bb97, 94098172-8252-4faf-a6cb-cc7f5fef009e, b7c8a23c-8467-4aa7-805a-f6e53bfe4c13, 35297231-1a0f-4ce2-b266-85f3f80a85f6, a6c6746d-e3bd-4f35-a02d-a4c127ae9e29, 1fa0bcea-1913-4776-b4d1-7f2fff876eba, e4c0169d-411e-4eaf-9081-a0aa6e145a2b, 4ea3d2b5-eccd-4319-89a2-0174ab939426, cbf2d87e-f272-4695-b7bd-82d27a9beaab, 4369fc29-3c8e-42f3-93b6-258c11aa6fd7, 4a3d366e-0794-4cf9-9c55-7dbd6b524670, 68e07edb-7811-4171-9516-b4e40e54b399, abf187fe-e594-4aaf-a5bf-7f4c0e0fd79b, 9b5eadce-d694-4c09-854e-571b11edff6d, 126197a2-77ef-437e-b56f-d5de3676d7e8, 828b299c-7bde-4cd3-bf2c-88b27747946d, 922a1409-0758-44df-93ec-476156b104fd, e958c425-5281-4ba5-8977-472d0a58e0ae, 6e7b4354-4d09-4c45-bf45-56a634022157, e26b8b33-926f-4b51-bada-dbd9ab5f242d, 4d940c2b-b3c7-45af-9981-424971a0b283, b2b55e5d-2661-4616-abca-ee01aec4d1a0, c65c7b86-69ed-4cb6-ab9e-c89516800c9b, 47890f85-0ac0-498b-9481-b5438248b54e, f22292c8-49ee-4891-ac20-f9242df0f2e0, cb67d9c0-cad1-41bd-87cb-f01d424d58e6, 7e7df5aa-9748-481d-9b09-22aa90b641fe … 另有 8 条，见 CSV
- `unsolved_pricing_truncated`：134 条。ebf13527-e604-4554-a673-6d5507742f32, 6a4fecf7-d070-4629-8bcc-a0ffa5c3b091, e7838732-3b59-4716-9ca3-3a7a952f6e1c, 95947fc1-483a-434d-95f2-a656981d8fd0, d63121ee-fe51-4908-8aae-c41d363667bd, 074b9061-230a-4888-b404-32aa8af28fe5, 7defcea0-5095-45cf-9b6f-0e033b027791, eabf2b06-3921-43bb-995b-2f7d99b00760, 9cfd3c61-da57-4e78-9d39-ecba829379f7, 6ab108c0-2dd1-4401-8824-d5c576af6d15, 48afa095-f8ec-42d1-901e-a42c9fccc443, 17e6b0d4-c343-4c70-aef3-7bb2bf23cd77, 42b9c5fc-c84a-4468-a15b-d6d8e01bbd7c, a63b97c6-f6d5-4587-b200-f84ed7f0f1de, 581e3490-7831-45c6-9518-006a56871141, 7faf8533-d544-4ef8-9e72-a302e90e0a73, 7d09b887-c05c-4956-a06c-6faae3e7ff62, 98c0115f-6f7a-4143-9f44-8bc9228f0e82, d597de88-d4d0-4392-b554-67738d4eb1bc, 0dc611a3-2eb9-4d96-a0a6-76092eb49523, 08dd8732-04ae-45bb-9cf2-cdd17d2214ac, 54703a83-ad1a-4fd2-9913-1af418868c5b, 4249740b-7fb5-404b-ab71-8e5978570275, 4ac70031-cdff-412c-9726-ba4da13c8902, 4154e28b-1255-477b-9701-ef52f2cbedac, cb10365e-74ed-4731-a056-fc9fd52937a7, db1ce8b4-d5bb-44a9-bce4-9087325fe5f5, 774cf25e-879c-4877-976b-e69ed008c685, e5063b1b-cb2a-4b7a-a591-6194012cf7fe, d0892049-ef5a-40a4-a444-b61865c85fb2, 3727b57b-e90f-4f08-b911-85c378136ab5, 7a799950-3bf3-49d0-8599-287765a772f9, 624588c7-1bbe-463c-a372-dd7144c3f0e0, 56971121-11b1-4cb5-ace9-f5132f1ef221, d8e445b0-7861-4f6d-b9ce-7fa8b831311b, 4cb40602-8853-49f6-9946-01210297c3d4, 94ed57b8-4e08-481e-8e71-bfb5b5e88fa0, 2247d310-de4c-4215-b2c7-d8da1a81e3e9, 1dd45bf5-ba4e-42aa-b3a7-3a0d9a22fb7d, 34f2fb93-faaf-4ea9-bdf5-e794f96d3052, d0297505-5d8f-4599-8cfd-b5d0f61fd5e2, 1dc9e62c-4250-4d1c-85d8-b62f69808fdd, 64431766-d89a-4ae9-888d-79b6ea2580ca, de554e10-3cd9-4b06-ba1b-f266e0d92179, f9e71d7d-7cb1-47a6-9ed3-f9efa6a04e6a, ea2b1e51-1968-4bbf-8c09-f379773653dc, 8e15bab4-60ed-4ff2-83ab-87f2341b2318, c0066e59-1818-4539-9a71-030034db45ee, 24b5c94d-7fbd-4394-b08c-0cc7f91cb77c, 738fedc9-ee74-4f5d-b3ab-a17c3c3c4a88, 7e1c7873-83d2-439c-8ffb-51075eb13fbe, 880302f0-e830-47e2-b916-f7ac2825cdee, defa2e0e-6eff-4225-9f87-5f91592c66b7, 23182fce-e468-43a4-8baa-db9de9372f33, 69cab007-74af-4492-af3f-c03324ca9169, 1733e973-aba2-4c5e-86c3-cb4525625ed3, f730b0fe-ec65-4558-8dfa-1f4a44fa9fe8, 7d508b81-92d8-4c84-acd6-ac2ef02b1541, bf7ff43d-46de-4464-a2b3-88404774e746, e7c6991b-7035-46d5-bba7-ff3be9a18e21, 3457a7df-75eb-4c67-952e-558256ac9a0e, 19760649-9fc5-4128-b959-3fe05ef9c9e5, 4589332f-644b-442d-8fdd-cedd2e60b635, b9d74073-0118-42c7-8e1d-12de044b73f3, a0fa9c8a-0ccd-47d0-b934-7e35c80ba115, c1a1d6f9-1cf9-427d-9914-b4a11921f800, 18dee6a8-002e-4d8a-b039-f7cfa9ee6674, 9981261f-272d-4408-b348-12cb97a34200, 3c8db714-f3cb-469c-8bec-10f4cf5ea81a, e2c24b7b-6034-47ba-949b-484ccba5979c, 293ccf0a-ce58-4967-9914-d2933006bc6e, b3f249f1-d464-402c-80c4-c14108399f7d, 9545a75c-92fa-49fc-8e63-f527a5fd9410, 0504fd86-695b-40b4-b18b-9c04b1c568ce, 530b54c5-6e73-44ae-8dbe-69de5da2cc2f, afa0df9d-afab-4ced-a887-a86d4cb7fad1, e3d076a8-b1d9-49cc-973a-d7a27426cff3, 58229902-8025-487d-a0a7-b1e11b01928e, 8364fad5-715e-4492-9fe8-d633ce837634, df3b504e-352b-4755-ad1e-062a9f64d581 … 另有 54 条，见 CSV

### baseline

- `infeasible`：106 条。6a4fecf7-d070-4629-8bcc-a0ffa5c3b091, ba90d3b8-ddea-4135-a31c-9b93abf9e455, e7838732-3b59-4716-9ca3-3a7a952f6e1c, d63121ee-fe51-4908-8aae-c41d363667bd, 51abd2ad-4478-4b56-968f-45664e5f1705, 7defcea0-5095-45cf-9b6f-0e033b027791, eabf2b06-3921-43bb-995b-2f7d99b00760, 6ab108c0-2dd1-4401-8824-d5c576af6d15, 17e6b0d4-c343-4c70-aef3-7bb2bf23cd77, 581e3490-7831-45c6-9518-006a56871141, 98c0115f-6f7a-4143-9f44-8bc9228f0e82, 08dd8732-04ae-45bb-9cf2-cdd17d2214ac, 3fd04e25-7258-4210-8f5a-38856e153b5c, cb10365e-74ed-4731-a056-fc9fd52937a7, db1ce8b4-d5bb-44a9-bce4-9087325fe5f5, 4154e28b-1255-477b-9701-ef52f2cbedac, 7a799950-3bf3-49d0-8599-287765a772f9, 56971121-11b1-4cb5-ace9-f5132f1ef221, d8e445b0-7861-4f6d-b9ce-7fa8b831311b, 4cb40602-8853-49f6-9946-01210297c3d4, 94ed57b8-4e08-481e-8e71-bfb5b5e88fa0, 2247d310-de4c-4215-b2c7-d8da1a81e3e9, 1dd45bf5-ba4e-42aa-b3a7-3a0d9a22fb7d, 34f2fb93-faaf-4ea9-bdf5-e794f96d3052, d0297505-5d8f-4599-8cfd-b5d0f61fd5e2, 449ccb08-89ef-45dd-b2c4-6e8dc9a3ee4c, 6f62fd56-d7e0-4e3d-9538-5e7e8ccf072b, 64431766-d89a-4ae9-888d-79b6ea2580ca, 6f95f342-e931-4a71-bd7a-0fd7b05f0162, 738fedc9-ee74-4f5d-b3ab-a17c3c3c4a88, b47e1ce4-40b1-4b86-86e8-8ebe38d13ef3, dc03a52f-768b-4a9f-8160-45d02a865168, 23182fce-e468-43a4-8baa-db9de9372f33, 50a47a35-9cc8-4b15-b578-1f0500a8a048, 0f2dd18c-0cf7-47f7-a327-c5a6073f2fbb, 880302f0-e830-47e2-b916-f7ac2825cdee, b1dead24-55ff-4a99-bb52-65666faa22fa, f307af52-c24e-44f2-bb2b-880b1dcdb294, 69cab007-74af-4492-af3f-c03324ca9169, 8a59abc0-b4d6-4654-aa39-a532ff98754f, 097afd3c-4274-4c32-adae-04eae5976e80, f730b0fe-ec65-4558-8dfa-1f4a44fa9fe8, 7d508b81-92d8-4c84-acd6-ac2ef02b1541, 2ed4fcab-baa6-4577-ae63-50aceaf1340a, ccce0cce-a781-4b03-9db3-99fc8e07d044, e7c6991b-7035-46d5-bba7-ff3be9a18e21, ebb28df9-d846-4c03-839a-150d4ca67fc8, 3457a7df-75eb-4c67-952e-558256ac9a0e, d51e1a45-ed7b-4522-a3ca-7f478979586f, a0fa9c8a-0ccd-47d0-b934-7e35c80ba115, 1932bb99-1a6a-4fde-af51-ef17af143049, 8b5c0a8e-839b-489e-90f3-310b4d76aeb8, 18dee6a8-002e-4d8a-b039-f7cfa9ee6674, 9981261f-272d-4408-b348-12cb97a34200, 3c8db714-f3cb-469c-8bec-10f4cf5ea81a, b3beb977-486e-4824-80c9-c4a54320fa49, e2c24b7b-6034-47ba-949b-484ccba5979c, b3f249f1-d464-402c-80c4-c14108399f7d, 0504fd86-695b-40b4-b18b-9c04b1c568ce, 8364fad5-715e-4492-9fe8-d633ce837634, 9e6c7ca8-6a4a-496f-83c2-a65f7014aa4f, df3b504e-352b-4755-ad1e-062a9f64d581, 0b8a9537-f645-481d-aa45-3d4313fc7554, bbbf10d2-81c4-4d87-9fb3-f03630e8e3ba, 1c9092c0-7924-45f3-a3a7-375924cfd812, 9e6ff415-ea05-4587-b182-239ccd62cc59, cf748ff8-f99b-4683-89a1-9f369b701367, 10bdb83b-c9f2-4654-a295-dd2d1ce75711, c27636e6-ac9a-47f2-8cda-73d8ee9a1dee, 2c27dc1d-2850-4166-b2e1-cd6e24efb766, d9b24e25-a0fe-4288-9e67-e4350c15303b, e06b0d10-e4e1-4071-908e-4c0fe0b99421, b2d9d059-6902-4ec7-a60d-5c18df9f460e, 2c806dce-b8b2-4e18-824c-49e83325caa0, a026ee0e-dc28-4586-83e4-e812f049b038, 2e6d05e7-8eb9-49bd-a127-44324099c02a, 1c46186f-83e7-4615-9d83-98528ab0fba3, 7d21022a-8a0c-41b1-8370-76672cacf539, a6670c9e-dae4-4370-90b9-589e6e33ed53, cbe86faf-5b08-4461-86d8-a3a83379caa6 … 另有 26 条，见 CSV
- `timeout`：1 条。c0066e59-1818-4539-9a71-030034db45ee

明细：`docs/lexcg-corpus-results.csv`。
