# 算法冒烟扫盘 #2（每引擎 1 例）

> 2026-07-22 · 案例 **`idx=3`**（D1-入门）· `tl=45s`

## 案例背景

- **老软件**：`failed:Failed!`，**无有效对标**（仅看各引擎能否算、算得是否一致）。
- **输入**（见落盘 `analysis.md`）：3 种管长 / 26 根量级、需焊几何（`some_stock_fit`），D1 档中「稍复杂」的一例。

## 各引擎结果

| 引擎 | 算出来？ | 校验 | 焊口 | 拼法 | 切法 | 利用率 | 耗时(s) |
|------|:--------:|:----:|-----:|-----:|-----:|-------:|--------:|
| **baseline** | 是 | 通过 | 10 | 3 | 3 | 0.9741 | 29.8 |
| **route3** | 是 | 通过 | 10 | 3 | 3 | 0.9741 | 26.5 |
| **rcg** | 是 | 通过 | **2** | 3 | 3 | 0.9741 | 0.35 |
| **global** | 是 | 通过 | **2** | 3 | 3 | 0.9741 | 0.52 |
| **v4** | 是 | 通过 | 10 | 3 | 3 | 0.9741 | 85.2 |
| **arcflow_direct** | 是 | — | **2** | 1 | 2 | 0.9741 | 42.5 (`compress`) |

## 结论（本例）

- **6/6 均有解**；API 五引擎 **校验均通过**。
- **util / 拼法 / 切法** 在 API 路径上高度一致（0.9741，3 拼 3 切）；**焊口分两组**：
  - **10 焊口**：baseline、route3、**v4**（走同一类 lex 焊接路径）
  - **2 焊口**：rcg、global、**arcflow_direct**（列生成 / compress 更省焊口）
- **v4 最慢（~85s）**，本例落在与 baseline 相同的重路径，未体现 idx=1 上的快路径。
- 老软件算失败 → 本例适合作为 **「无 legacy、比引擎互相对齐」** 的冒烟样例。

## 与 #1（idx=1）对照

| | idx=1 | idx=3 |
|---|--------|--------|
| 老软件 | success，可对标的 | failed，无可对标 |
| 典型耗时 | 亚秒级（除 arcflow_direct/global） | 数十秒级（baseline/v4/arcflow_direct） |
| 引擎分歧 | route3 util 崩 | **焊口 10 vs 2** 分歧 |

## 复现

```powershell
cd 102
foreach ($e in @("baseline","route3","rcg","global","v4","arcflow_direct")) {
  .venv\Scripts\python.exe scripts\_algorithm_sweep_worker.py $e data\legacy-cases 3 45
}
```

上一例：`docs/algorithm-sweep-smoke-idx1.md`
