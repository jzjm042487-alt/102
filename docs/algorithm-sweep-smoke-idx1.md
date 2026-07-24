# 算法冒烟扫盘（每引擎 1 例）

> 2026-07-22 · **未改算法**，仅计算记录。  
> 案例：`idx=1`（D1-入门，老软件 success）· `tl=45s` · 落盘 `data/legacy-cases/0001_*`

## 老软件基准（该例）

| 焊口 | 拼法 | 切法 | 利用率 |
|-----:|-----:|-----:|-------:|
| 0 | 0 | 1 | 0.9950 |

输入规模：管 26 根 / 1 型，原料 23 根 / 4 规格（见 `analysis.md`）。

## 各引擎结果

| 引擎 | 算出来？ | 校验 | 焊口 | 拼法 | 切法 | 利用率 | 耗时(s) | 备注 |
|------|:--------:|:----:|-----:|-----:|-----:|-------:|--------:|------|
| **baseline** | 是 | 通过 | 0 | 1 | 1 | 0.9950 | 0.27 | `OPTIMAL_LEXICOGRAPHIC` |
| **route3** | 是 | 通过 | 0 | 1 | 2 | 0.5129 | 0.35 | 可行但**用料几乎翻倍**，利用率偏低 |
| **rcg** | 是 | 通过 | 0 | 1 | 1 | 0.9950 | 0.75 | 与老解指标一致 |
| **global** | 是 | 通过 | 1 | 2 | 1 | 0.9950 | 13.5 | 利用率达标，**多 1 焊口** |
| **v4** | 是 | 通过 | 0 | 1 | 1 | 0.9950 | 0.25 | 生产包装 arc-flow，`ARCFLOW_TARGET_REACHED` |
| **arcflow_direct** | 是 | — | 0 | 0* | 1 | 0.9950 | 50.0 | 路径 `pure_cut`；*拼法计数与 API 口径不同 |

**结论（本例）**

- **6/6 均能产出数值解**；5 个 API 引擎均 **verifier 通过**。
- **与 legacy 四项最接近**：baseline、rcg、v4（焊口 0、util 0.995、切法 1）。
- **route3** 在本例明显**未压利用率**（0.51），需列入后续 D1 批量对比重点。
- **global** 慢一个数量级，且焊口 +1。
- **arcflow_direct** 走纯切仍要 ~50s（分类/网格开销），与 **v4 包装 0.25s** 差异来自是否走完整 `solve_arcflow` 预算。

## 复现命令

```powershell
cd 102
foreach ($e in @("baseline","route3","rcg","global","v4","arcflow_direct")) {
  .venv\Scripts\python.exe scripts\_algorithm_sweep_worker.py $e data\legacy-cases 1 45
}
```

资产清单：`docs/algorithm-asset-inventory.md`  
批量扫盘（可选）：`scripts/algorithm_sweep.py`
