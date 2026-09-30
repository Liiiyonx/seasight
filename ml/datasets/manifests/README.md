# 数据集清单机制（ml/datasets/manifests）

> 探海灵眸 SeaSight — WP-06 感知评测的数据集隔离机制。
> 上位口径：`项目文档/探海灵眸_项目计划书.md` §6.2「数据分集不得随机泄漏」。

## 1. 为什么需要清单

`ml/datasets/` 下不允许直接堆图片。每一批数据必须有一份**清单（manifest）**，
写明它属于哪个分集、什么采集条件、什么证据等级、如何与其它分集隔离。
评测脚本（`ml/scripts/evaluate_opencv.py`）**只认清单，不扫目录**，
这样"独立测试集被训练脚本偷偷读走"这类泄漏在机制上就不存在。

## 2. 四类分集（冻结，不可改名）

| 清单类型 | manifest_type | 用途 | 隔离方式 | 谁可以用 |
| --- | --- | --- | --- | --- |
| 训练集 | `train` | 模型拟合 | 与测试/盲测物理分离，可按点位、日期逻辑切分 | 训练脚本 |
| 验证集 | `val` | 调参与阈值选择 | 与测试/盲测物理分离 | 调参脚本 |
| 独立测试集 | `test` | 只做最终评测 | 按日期+点位隔离，**评审前不得触碰** | 仅评测脚本 |
| 现场盲测集 | `blind` | 交付答辩前封存、现场演示 | 物理封存（只读介质/只读目录），答辩时解锁 | 仅现场演示 |

**冻结口径：**
- 独立测试集和现场盲测集**不得参与任何训练、调参、阈值选择**，否则评测数字作废。
- 现场盲测集交付答辩前封存：从 `ready` 改 `sealed`，任何写入都被视为违规。
- 泄漏判定规则：任何图片/点位/日期同时出现在 `train|val` 与 `test|blind` → 视为泄漏，
  评测报告必须标记 `isolation_violation: true`（由评测脚本在加载时自动检查清单间重叠）。

## 3. 物理 / 逻辑隔离

`isolation.method` 二选一，必须写清怎么隔离：

- `physical`：不同分集放在**不同目录树**（或不同只读介质），路径上互不可达。
- `logical`：同一物理目录内，用清单条目显式圈定归属；**同一文件不得出现在两份清单**。

推荐组合：
- train / val：`logical`（同一采集批次按时间窗切分），写清切分规则（如"按天：1-20 天训练、21-25 天验证"）。
- test：`physical`（独立目录 + 独立采集时间窗/点位），绝不同源同批。
- blind：`physical` + 封存（`status: sealed`，只读）。

## 4. 证据等级（冻结，与计划书 §1 一致）

每个清单必须带 `data_type` 与 `evidence_level`：

| data_type | 允许的 evidence_level | 说明 |
| --- | --- | --- |
| `planned`（未采集） | `E0` | 只有规划，没有数据 |
| `synthetic`（合成帧） | `E1` / `E2` | **最高 E2，禁止写成 E3/E4** |
| `quasi_real`（准真实：水槽/码头受控环境） | `E2` | 受控实验 |
| `real`（真实设备/真实海域/真实用户） | `E3` | 真实场景；E4 需合同/订单/验收，本清单机制不自动授予 |

评测脚本会对清单的 `evidence_level` 做上限校验：超出允许范围直接报错，不产出指标。

## 5. 当前状态

`ml/datasets/` 只有 `.gitkeep`，**没有任何图片**。
因此 `example_*.yaml` 全部是 `status: planned`、`sample_count: 0`、`evidence_level: E0` 的示例模板：
它们演示结构，不冒充已采集数据。评测脚本对 `planned` 或空数据输出 `not_evaluated`，
**绝不生成虚假精度**。

## 6. 文件约定

- `template_manifest.yaml`：唯一模板（schema），新增清单必须逐字段对齐。
- `example_train.yaml` / `example_val.yaml` / `example_test_independent.yaml` / `example_blind_live.yaml`：
  四类分集的示例，演示隔离写法。
- 真实清单文件建议命名：`<类型>_<采集批次>.yaml`（如 `test_20261001_lianjiang.yaml`），
  清单名即证据，不许用 `data_final_v2.yaml` 这类无法追溯的名字。
