# 真实数据样本（学习 / 回归素材）

本目录收纳来自现场的真实数据，用于分析、调参与回归验证。**不是**代码运行的必需输入，可安全增删。

## 目录结构

```
data/samples/
├── software-io/       现有排料软件的真实请求与返回
└── manual-nesting/    人工排料的结果数据
```

### software-io/ — 现有软件真实输入/输出
- 现场真实调用当前排料软件时的**输入 payload** 与**返回结果**。
- 用途：作为求解器改造的对照基准（我们的引擎在同一输入上应做到"不更差、尽量更优"）。
- 命名：`req_<短id>_input.json`（请求）、`req_<短id>_output.json`（返回），成对时用同一 `<短id>` 关联。

### manual-nesting/ — 人工排料数据
- 老师傅/工艺人员手工排出的方案。
- 用途：提炼现场"习惯规则"（切法统一、短管集中、段长偏好等），作为软约束/后处理的经验来源。
- 命名：`manual_<工程或批次>_<短描述>.json`（或原始格式，附一行来源说明）。

## 已收录

| 文件 | 类型 | 说明 |
|---|---|---|
| `software-io/req_92de3aca_input.json` | 软件输入 | 首份真实请求（4 材质组：φ45×12 / φ45×12 / φ51×13 / φ51×9.5），诊断与 demo 的主用例 |
| `software-io/batch-DGMOM/software_records.json` | 软件输入+输出（批量） | MOM 导出 3177 条单组记录（1640 成功/228 失败/760 未完成），对标金矿。字段字典见 `docs/research/真实数据字典-DGMOM批次.md` |
| `manual-nesting/batch-DGMOM/manual_cutting_list.csv` | 人工切法料单 | 车间人工排的切法（定尺→段长组合），金标准 |
| `manual-nesting/batch-DGMOM/part_process_detail.csv` | 成品管工艺明细 | 弯头/坡口/焊接标准/工时等零件级属性 |

> DGMOM 批次的完整结构、三类分布与字段含义见 `docs/research/真实数据字典-DGMOM批次.md`。

## 注意
- 数据可能含现场标识（图号、工程号），仅内部研发使用。
- 已入库：`software-io/batch-DGMOM/software_records.json`（教授软件 3177 条输入 + 2417 条结果）、`req_92de3aca_input.json`。
- 同内容副本 `software-io/software-records-3177.json`、我们引擎的 `req_92de3aca_ours.json`、人工切法/工艺 CSV、运行时 sqlite/日志不入库。
