# 老软件指纹规则理解（全档，先读懂再改引擎）

> 数据：`legacy-fingerprints-all`（1586 success）+ 教师列 motif 挖掘。
> 本文件只描述「老软件解里反复出现什么」，**不写实现补丁**。

## 0. 一句话

老软件解看起来像：**先定每根成品管怎么拼（焊列），再定母料怎么切出这些段（切列），全局选少数切法并大量复用**。难度升高时，焊段变碎、Σ 变大、0 焊口消失——不是换了一套路由，而是同一套「拼+切」解变得更复杂。

## 1. 分档：在解什么题

| 档位 | 题数 | 0焊口题 | 含义 |
|------|-----:|--------:|------|
| D1-入门 | 347 | 291 (84%) | 以纯切为主 |
| D2-常规 | 581 | 156 (27%) | 焊/切混合 |
| D3-进阶 | 325 | 17 (5%) | 几乎必焊 |
| D4-困难 | 213 | 4 (2%) | 几乎必焊 |
| D5-极难 | 120 | 0 (0%) | 几乎必焊 |

### 几何约束（硬规则）

- `all_stock_short`（成品长于所有母料）：**100% 有焊接**，没有 0 焊口解。
- `all_stock_fit`：可以 0 焊，也可以焊（全档约 58% 为 0 焊；D1 更高）。
- `some_stock_fit`：多数仍焊。

## 2. 焊接规则（从教师焊列统计）

### 2.1 长管（成品 > S_max）

锚点统计（按焊件件数加权）：`{'long_max_not_stock': 31117, 'long_max_is_smax': 50591, 'long_max_other_stock': 65304}`

- **绝大多数**长管焊法的最长段 = `S_max`（整根最长母料当锚段）。
- 少数用其它母料长度当最长段；极少数最长段不是任何库存规格。
- 典型形状：`(R, S_max)` 两段；或把 `R` 再拆成 2～3 段 → 3/4 段焊。

### 2.2 短管（成品 ≤ S_max）

- **1 段**（整管当一段，等于纯切用料）在各档都很多。
- 需要吃料头/拼利用率时出现 **2 段**：常见 motif 为
  - `2seg_stock_gap`：用 `母料 - kerf - 管长` 一类缺口当一段；
  - `2seg_pack_L_leftover` / `2seg_kkerf_L`：把管长在母料上整段码放后的余料当一段；
  - `2seg_no_full_stock`：两段都不是整根母料规格。
- 更碎的短管 3 段相对少，但 D3+ 会增多。

### 2.3 每管型用几种拼法？

各档「同一管键下不同拼法条数」均值：

- D1-入门: 1.07
- D2-常规: 1.27
- D3-进阶: 1.18
- D4-困难: 1.08
- D5-极难: 1.08

多数情况接近 **每管型一种主拼法**；D2 多拼法比例更高（与早期 fingerprint 的 multi_pattern 一致）。

### 2.4 分档焊 motif 榜（件数加权 Top）

#### D1-入门

- `pipe_short`: 51874
- `1seg`: 50827
- `2seg_no_full_stock`: 791
- `3seg_no_stock_anchor`: 256
- `short_multi_split`: 256
- `2seg_kkerf_L`: 195
- `2seg_pack_L_leftover`: 163
- `2seg_stock_gap`: 112

#### D2-常规

- `pipe_short`: 140336
- `1seg`: 126120
- `2seg_no_full_stock`: 11915
- `pipe_long`: 4988
- `2seg_kkerf_L`: 3966
- `2seg_pack_L_leftover`: 3629
- `short_multi_split`: 3155
- `2seg_stock_gap`: 3154
- `3seg_no_stock_anchor`: 3034
- `2seg_stock_anchor`: 2865
- `2seg_R_plus_S`: 2865
- `3seg_stock_anchor`: 1086

#### D3-进阶

- `pipe_short`: 102244
- `1seg`: 69986
- `2seg_no_full_stock`: 30803
- `pipe_long`: 19012
- `2seg_stock_anchor`: 9051
- `2seg_R_plus_S`: 9051
- `2seg_kkerf_L`: 7596
- `2seg_pack_L_leftover`: 6818
- `2seg_stock_gap`: 5804
- `3seg_stock_anchor`: 5749
- `short_multi_split`: 3414
- `3seg_no_stock_anchor`: 3311

#### D4-困难

- `pipe_short`: 99430
- `1seg`: 60937
- `2seg_no_full_stock`: 57627
- `pipe_long`: 46920
- `3seg_stock_anchor`: 13092
- `2seg_stock_anchor`: 8302
- `2seg_R_plus_S`: 8302
- `2seg_kkerf_L`: 5403
- `2seg_pack_L_leftover`: 4898
- `2seg_stock_gap`: 4452
- `short_multi_split`: 3818
- `3seg_no_stock_anchor`: 3570

#### D5-极难

- `pipe_long`: 76092
- `pipe_short`: 52948
- `2seg_no_full_stock`: 51334
- `3seg_stock_anchor`: 30634
- `1seg`: 28874
- `4seg_stock_anchor`: 6797
- `2seg_stock_anchor`: 5382
- `2seg_R_plus_S`: 5382
- `4seg_R_triple`: 4359
- `short_multi_split`: 4001
- `2seg_stock_gap`: 3525
- `2seg_pack_L_leftover`: 3525

## 3. 切割规则（从教师切列统计）

共同特征：

1. **少类型、高复用**：档位越高，切/棒比越低（少数切法开很多张）。
2. **k 拷贝**很常见：同一段规格在一根母料上切多件（`cut_kcopies_k*`）。
3. **近满切**常见：`cut_nearfill_le50` / `cut_kerf_fill_*`（留刀缝或很小余料）。
4. **混合多件**在难档增多：`cut_mixed_parts*`、`cut_many_parts_ge5`。
5. D1 大量 `cut_1seg` / 近满或整棒——就是纯切。

### 分档切 motif 榜

#### D1-入门

- `cut_partial`: 1993
- `cut_1seg`: 1678
- `cut_many_parts_ge5`: 1481
- `cut_nearfill_le50`: 618
- `cut_mixed_parts2`: 468
- `cut_kcopies_k39`: 396
- `cut_kerf_fill_5`: 322
- `cut_kcopies_k2`: 215
- `cut_kcopies_k58`: 178
- `cut_mixed_parts3`: 141
- `cut_kerf_fill_10`: 132
- `cut_kcopies_k4`: 105

#### D2-常规

- `cut_nearfill_le50`: 15813
- `cut_mixed_parts2`: 10447
- `cut_kerf_fill_5`: 8435
- `cut_partial`: 6493
- `cut_1seg`: 5700
- `cut_many_parts_ge5`: 5295
- `cut_full_bar`: 4849
- `cut_mixed_parts3`: 4128
- `cut_kerf_fill_10`: 3409
- `cut_kerf_fill_20`: 1699
- `cut_mixed_parts4`: 1366
- `cut_kerf_fill_15`: 783

#### D3-进阶

- `cut_nearfill_le50`: 38712
- `cut_mixed_parts2`: 30707
- `cut_1seg`: 23324
- `cut_full_bar`: 22342
- `cut_kerf_fill_10`: 17064
- `cut_kerf_fill_5`: 14658
- `cut_partial`: 10971
- `cut_many_parts_ge5`: 10577
- `cut_mixed_parts3`: 4336
- `cut_mixed_parts6`: 3677
- `cut_mixed_parts5`: 2508
- `cut_mixed_parts4`: 2499

#### D4-困难

- `cut_nearfill_le50`: 63035
- `cut_mixed_parts2`: 57486
- `cut_1seg`: 42416
- `cut_full_bar`: 38339
- `cut_kerf_fill_5`: 29625
- `cut_kerf_fill_10`: 26278
- `cut_partial`: 15083
- `cut_mixed_parts3`: 9646
- `cut_many_parts_ge5`: 7140
- `cut_mixed_parts4`: 2760
- `cut_mixed_parts6`: 2457
- `cut_kerf_fill_20`: 2191

#### D5-极难

- `cut_1seg`: 97038
- `cut_full_bar`: 85122
- `cut_mixed_parts2`: 66692
- `cut_nearfill_le50`: 66131
- `cut_kerf_fill_5`: 39987
- `cut_kerf_fill_10`: 18823
- `cut_partial`: 12153
- `cut_mixed_parts3`: 5259
- `cut_many_parts_ge5`: 4074
- `cut_kerf_fill_20`: 2845
- `cut_mixed_parts4`: 1506
- `cut_mixed_parts5`: 1254

## 4. 拼起来：老软件在遵循的「工作流」（推断）

```
对每类成品管:
  if 管长 <= 某根母料:
      优先 1 段（整管）
      若要抬利用率 / 吃余料 → 用 gap / pack leftover / k·kerf 拆成 2 段
  else:  # 长于所有母料
      必焊；优先 (余段 R, 最长母料 S_max)
      若 R 仍难切满 → 按母料 leftover / k·kerf / 半料 再拆 R

全局:
  枚举/生成能生产这些段的切法列
  用集合覆盖式优化：选很少的切法类型，决定各开几张
  目标侧表现为：焊口优先，其次利用率（语料观感）
```

没有证据表明存在「D1 走纯切引擎、D5 走另一引擎」的显式路由；
更像是 **同一套拼+切列 + 全局优化**，随实例变难自动用上更碎的列。

## 5. 对我们造引擎的含义（只理解，不实施）

| 场景 | 老软件在做什么 | 我们若只学 D2 失败簇会怎样 |
|------|----------------|---------------------------|
| D1 纯切 | Σ≈1，切法很少，几乎不焊 | 硬造焊缝/扩 Σ → 污染、util 差、甚至无解 |
| D2 混合 | 1 段 + (R,S) + leftover | 大致对口，但仍要干净 Σ |
| D4/D5 | 大 Σ、多段焊、多混合切 | 规则不够会缺列；乱扩又会超时 |

**理解结论**：规则要按「几何 + 是否需要焊」分层描述，而不是一套焊缝启发式打天下。

## 6. 机器可读明细

- `data/acceptance/legacy-fingerprints-all/rule_mine_by_tier.json`
