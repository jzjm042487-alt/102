# 老软件语料单案例落盘格式（测试契约）

> 由 `scripts/export_legacy_cases.py` 从 MOM 导出 JSON 生成。  
> 落盘根目录默认：`data/legacy-cases/`（已在 `.gitignore`，本地全量生成）。

## 目的

后续 V4 / 回归脚本**不再直接读 80MB+ 原始导出**，而是：

1. 按 `idx` 或 `index.json` 定位案例；
2. 读 `input.json` 作为求解输入；
3. 读 `output.json` + `metrics.json` 作为老软件对标输出与七项指标。

## 目录布局

```text
data/legacy-cases/
├── index.json                 # 全量索引（metrics 摘要）
├── 0000_37428552/             # {idx:04d}_{id前缀}
│   ├── input.json             # 清洗后 MOMPROBLEMJSON
│   ├── output.json            # MOMRESULTJSON（slim 或完整）
│   ├── metrics.json           # meta + 七项指标 + 错误信息
│   └── analysis.md            # 中文逐案分析
└── ...
```

## 七项关键指标（`metrics.metrics`）

| 字段 | 含义 | 来源 |
|------|------|------|
| `pipe_types` | 管子类型数 | 解析后 `MaterialGroup.pipes`（合并等价管后） |
| `pipe_pieces` | 管子根数（需求总量） | Σ `pipe.demand` |
| `stock_types` | 原材料规格种类数 | 不同 `stock_length` 种数 |
| `stock_pieces` | 原材料根数（库存总量） | Σ `stock.quantity` |
| `welding_joints` | 焊口数 | `GeneralInfo.WeldingJointQuantity` |
| `weld_pattern_types` | 拼法数 | `WeldingPattern` 中 `Part` 元组去重（≥2 段） |
| `cut_pattern_types` | 切法数 | `(母料长, sorted(Part))` 去重 |

附加常用字段：

- `utilization_rate` — `GeneralInfo.UtilRate`
- `legacy_status` — `success` / `no_result` / `failed:...` 等
- `difficulty` — 易 / 中 / 难（与 `_corpus_scan` 一致，**粗分**）
- `geom_class` — 首组几何分类（`analyze_corpus.geom_class`）

**第二步难度分层**（正式门禁用）：每案 `difficulty.json` + 根目录 `difficulty_pools.json`，见 [`legacy-corpus-difficulty-taxonomy.md`](legacy-corpus-difficulty-taxonomy.md)。

## 输入清洗规则

与 `benchmark_against_software._clean_payload` 一致：

- 去掉 `pipe_demand == 0` 的行；
- 相同 `(Parent_node|figure, jlxh, cube_no)` 合并需求。

解析走 `backend.app.domain.parse_problem`；多组问题时 `aggregate` 为各组相加。

## 输出解析规则

- **焊口数**：验收对标以 **GeneralInfo** 为准（与历史脚本一致）。
- **拼法 / 切法**：从 `Result.WeldingPattern` / `CuttingPattern` **重算**（与 `_corpus_worker.legacy_metrics` 一致）。
- `analysis.md` 中若 GeneralInfo 焊口与 Pattern 重算不一致会标注说明。

## 命令

```bash
cd 102
# 全量 3177 条（约数分钟，体积数 GB 若 --full-output）
.venv\Scripts\python.exe scripts\export_legacy_cases.py

# 抽样
.venv\Scripts\python.exe scripts\export_legacy_cases.py --from 0 --to 99

# 仅老软件成功且输入可解析（约 1580 条，适合 CI 子集）
.venv\Scripts\python.exe scripts\export_legacy_cases.py --only-solved

# 指定语料路径
.venv\Scripts\python.exe scripts\export_legacy_cases.py --source "C:\path\DGMOMPTDGMOMGLLXWTFJB.json"
```

## 测试代码加载

```python
from pathlib import Path
from scripts.legacy_corpus_lib import load_legacy_case, analyze_record

case = load_legacy_case(Path("data/legacy-cases"), idx=2318)
problem_payload = case["problem"]   # 直接 POST / solve
legacy = case["legacy_result"]
floor = case["metrics"]["metrics"]["utilization_rate"]
```

或不经落盘、直接分析语料：

```python
from pathlib import Path
from scripts.legacy_corpus_lib import load_corpus_records, analyze_record

rec = load_corpus_records()[2318]
doc = analyze_record(rec, 2318)
assert doc["metrics"]["pipe_types"] is not None
```

## 与 picked20 / 语料脚本的关系

- `scripts/_corpus_worker.py` 应逐步改为 `load_legacy_case` 或 `legacy_corpus_lib.extract_*`。
- `util_floor` 回归取 `metrics.utilization_rate`（或 `output.json` → GeneralInfo）。
- `docs/legacy-corpus-export-summary.md` 为每次全量导出后的统计摘要（可提交 Git，案例目录不提交）。
