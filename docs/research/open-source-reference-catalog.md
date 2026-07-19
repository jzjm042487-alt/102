# 开源借鉴清单：蛇形管切焊排料

> 目标：列出可 clone、可精读、可映射到本项目模块的外部代码。  
> 本地克隆目录：`_refs/`（已加入 `.gitignore`，不入库）。

## 1. 借鉴原则

- **可借鉴算法骨架，不可照搬业务成品。**
- 没有任何开源仓库直接覆盖：蛇形管 + 禁焊区 + 最大焊口 + 切法/拼法联合少花样。
- 本项目必须自建业务层；外部代码只服务切割侧、列生成、弧流、Skiving（拼长）等通用能力。

## 2. 必 clone（高价值）

| 本地目录 | 仓库 | 借鉴层 | 对本项目的用途 |
| --- | --- | --- | --- |
| `_refs/vpsolver` | https://github.com/fdabrandao/vpsolver | 切割侧 arc-flow | 切法建模、图压缩、精确/强松弛下料 |
| `_refs/informs-csp-2023` | https://github.com/INFORMSJoC/2023.0399 | CSP + Skiving + B&C&P | 切割/拼长联合、列生成、整数启发式 |
| `_refs/columngenerationsolver` | https://github.com/fontanf/columngenerationsolver | 列生成框架 | 主问题/定价子问题组织方式 |
| `_refs/columngenerationsolverpy` | https://github.com/fontanf/columngenerationsolverpy | Python 列生成示例 | 快速理解 RMP + pricing 迭代 |

## 3. 建议精读（可不整仓 clone）

| 资源 | 用途 |
| --- | --- |
| https://github.com/Pyomo/pyomo/blob/main/examples/pyomo/columngeneration/cutting_stock.py | 经典列生成教学实现 |
| https://github.com/Gurobi/modeling-examples/tree/master/colgen-cutting_stock | 工业求解器视角的列生成 notebook |
| https://github.com/emadehsan/csp | OR-Tools 普通 1D CSP，仅作对照 |

## 4. 不建议当主参考

- `opcut` / `freecut`：偏 2D 板材排样，和蛇形管切焊业务差异大。
- 纯 GA/SA 下料 demo：与本项目选定主路线冲突。

## 5. 模块映射

| 本项目模块 | 优先参考 |
| --- | --- |
| 切法列 / 切割侧 pricing | VPSolver、columngenerationsolver |
| 拼法 / 段长拼长 | INFORMSJoC Skiving Stock 求解器 |
| 受限主问题 RMP | Pyomo / Gurobi / fontanf 示例 |
| 花样压缩（setup） | PMP/CSP-S 文献 + 分阶段 MILP（无直接成品） |
| 禁焊区 / 最大焊口 / must_use | **本项目自研** |
| verifier / MOM 输入 | **复用现有代码** |

## 6. Clone 命令（已执行）

```powershell
cd d:\codeing\07-share\0-plgl\102\_refs
git clone --depth 1 https://github.com/fdabrandao/vpsolver.git
git clone --depth 1 https://github.com/fontanf/columngenerationsolver.git
git clone --depth 1 https://github.com/fontanf/columngenerationsolverpy.git

# INFORMSJoC/2023.0399 全仓含大量实例，采用稀疏克隆仅取源码
mkdir informs-csp-2023
cd informs-csp-2023
git init
git remote add origin https://github.com/INFORMSJoC/2023.0399.git
git sparse-checkout set src README.md LICENSE CITATION.cff
git fetch --depth 1 origin HEAD
git checkout FETCH_HEAD
```

## 7. 使用约定

1. `_refs/` 只读参考，不修改上游代码后提交到本仓库。
2. 需要吸收的算法写成我们自己的模块，注明来源与许可证。
3. 发现可直接复用的函数级逻辑时，优先改写到 `backend/app/`，保持整数毫米与 verifier 契约。
