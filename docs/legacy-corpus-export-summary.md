# 老软件语料落盘汇总

> 由 `scripts/export_legacy_cases.py` 生成，勿手改。

- 语料路径: `C:\Users\15709\Downloads\DGMOMPTDGMOMGLLXWTFJB.json`
- 落盘目录: `D:\codeing\07-share\0-plgl\102\data\legacy-cases`
- 记录范围: 0..3176
- 案例数: **3177**
- 耗时: 21.3s

## 老软件结果状态

| 状态 | 数量 |
|------|-----:|
| `success` | 1590 |
| `no_parse` | 1309 |
| `failed:Failed!` | 278 |

## 难度分布（可解析输入）

| 难度 | 数量 |
|------|-----:|
| 难 | 1654 |
| 中 | 1333 |
| 易 | 154 |
| None | 36 |

## 几何分类（首组）

| 分类 | 数量 |
|------|-----:|
| `all_stock_fit` | 1290 |
| `all_stock_short` | 986 |
| `some_stock_fit` | 734 |
| `infeasible_len` | 131 |
| `None` | 36 |

## 成功案例规模（摘要）

- 成功且输入可解析: **1590**
- 焊口: min=0, max=32753, median=57
- 利用率: min=0.0188, max=1.4972

## 单案例目录结构

见 `docs/legacy-corpus-case-format.md`。

## 测试加载示例

```python
from pathlib import Path
from scripts.legacy_corpus_lib import load_legacy_case
case = load_legacy_case(Path('data/legacy-cases'), idx=0)
problem = case['problem']
legacy = case['legacy_result']
```
