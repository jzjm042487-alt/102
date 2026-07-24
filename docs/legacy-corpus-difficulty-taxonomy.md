# 语料难度分类体系（第二步）

> 面向 **可计算案例**（输入可解析、库存总长 ≥ 需求总长），用于回归抽样、时限 SLA、门禁分层。  
> 实现：`scripts/legacy_difficulty.py` · 批量标注：`scripts/classify_legacy_difficulty.py`

## 1. 「可计算」定义

同时满足：

| 条件 | 说明 |
|------|------|
| 输入可解析 | `parse_problem(clean_mom_problem(...))` 成功 |
| 需求有效 | 管子根数 &gt; 0 |
| 物理长度可行 | 首组 `geom_class ≠ infeasible_len`（库存总长 ≥ 需求总长） |

不满足者标记为 **`不可计算`**，并记录 `block_reason`（如 `stock_shorter_than_demand`）。

当前全库（3177 条）约 **3010** 条可计算；其中约 **1580+** 条同时有老软件 `success` 输出（可观测输出段长离散度）。

## 2. 五档标签

| 档位 | 含义 | 典型用途 |
|------|------|----------|
| **D1-入门** | 规模小、管长集中、几何友好 | 单测、冒烟 |
| **D2-常规** | 中等根数、离散适中 | 日常 CI |
| **D3-进阶** | 根数或离散度有一项偏高 | 发布前回归 |
| **D4-困难** | 大规模或多管长/多段长 | 性能与 Fallback 压测 |
| **D5-极难** | 超大组 + 高离散 + 强几何约束 | 专项 SLA（如 L20 类） |

## 3. 评分模型（composite 0–20 → 五档）

五个维度，各 **0–4 分**，相加得 `composite_score`：

### 3.1 输入管子根数 `pipe_pieces`

按语料分位数分桶：

| 分数 | 根数上限 |
|------|----------|
| 0 | ≤ 30 |
| 1 | ≤ 80 |
| 2 | ≤ 180 |
| 3 | ≤ 500 |
| 4 | &gt; 500 |

### 3.2 原材料根数 `stock_pieces`

| 分数 | 库存根数上限 |
|------|--------------|
| 0 | ≤ 20 |
| 1 | ≤ 77 |
| 2 | ≤ 200 |
| 3 | ≤ 800 |
| 4 | &gt; 800 |

### 3.3 输入管长离散程度

在合并后的 **管长×根数** 直方图上计算：

- `unique_lengths` — 不同成品管长度种数  
- `cv` — 按根数加权的变异系数  
- `entropy_norm` — 长度分布归一化熵（0=单一长度，1=均匀分散）

由 `(unique_lengths, cv, entropy_norm)` 映射到 0–4（管型种类多、CV 大、熵高 → 分高）。

### 3.4 输出段长离散程度（「产出材料段」）

老软件 **Success** 时：扫描 `WeldingPattern` + `CuttingPattern` 中所有 `Part` 拆出的 **整数段长**，按 `Number` 加权，同样算 `unique_segments / cv / entropy_norm` → 0–4 分。

**无老解时**：不造假输出，改用 **推断分** `inferred_no_legacy`：

- `all_stock_fit` → 偏低（多为少焊/整管）  
- `some_stock_fit` → 中等偏高  
- `all_stock_short` → 高（必切必焊，段种类通常多）

有老解的案例在 `difficulty.json` 里 `output_segment_dispersion.source = legacy_patterns`。

### 3.5 算法负载（工程理解）

综合几何与建模复杂度，0–4 分：

- `some_stock_fit` +1，`all_stock_short` +2  
- 管型 ≥ 12 → +1  
- 原料规格 ≥ 4 → +1  
- 多 material group → +1  

反映：弧流网格、列生成、焊口 Pass 的预期压力（与纯规模正交）。

### 3.6 合成与分档

```
composite = s_pipe + s_stock + s_in + s_out + s_algo   # 0..20

 0–4  → D1-入门
 5–8  → D2-常规
 9–12 → D3-进阶
13–16 → D4-困难
17–20 → D5-极难
```

`reasons[]` 列出最主要 1–3 条中文原因（便于 `analysis.md` 人工抽查）。

## 4. 落盘字段

每个案例目录新增：

- **`difficulty.json`** — 完整轴分数、离散度统计、档位、原因  

根目录新增/更新：

- **`difficulty_pools.json`** — 各档 `idx` 列表 + 全部可计算 `idx`  
- **`index.json`** — 每条增加 `difficulty: { tier, tier_id, composite_score, computable }`  

汇总文档（可提交 Git）：

- **`docs/legacy-corpus-difficulty-summary.md`** — 各档数量与样例 idx  

## 5. 命令

```bash
cd 102
.venv\Scripts\python.exe scripts\classify_legacy_difficulty.py
```

重新导出语料后需 **先 export 再 classify**。

## 6. 测试用法

```python
import json
from pathlib import Path

pools = json.loads(Path("data/legacy-cases/difficulty_pools.json").read_text(encoding="utf-8"))
d3 = pools["pools"]["D3-进阶"]  # 该档全部 idx
```

或读单案：

```python
d = json.loads(Path("data/legacy-cases/0123_xxxx/difficulty.json").read_text(encoding="utf-8"))
assert d["tier"].startswith("D")
```

## 7. 与旧「易/中/难」的关系

`metrics.json` 里仍保留 `_corpus_scan` 的粗分 `difficulty`（仅管型数+根数）。  
**本体系为第二步正式分层**，后续门禁、抽样请以 `difficulty.json` / `difficulty_pools.json` 为准。
