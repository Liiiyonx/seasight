# 机械臂智能拾取子系统（edge/grasp）

> 用途：把机械臂执行端从「厂商远程桌面观测窗」升级为「自带现场再训练能力的
> 自主作业端」—— 平台派单 → 输 IP 进作业台 → 现场再训练 → 全自动拾取。
>
> 证据口径：本目录是 E1/E2 代码与自测设施，**mock 模式只证明协议与闭环逻辑可离线
> 复现**，不构成物理拾取、现场验收或识别精度证据。留出集指标是训练集内部评估，
> 不等于现场精度；真机 + 受控实验仍只能标 E2/E3。

对应实施计划书：`项目文档/探海灵眸_机械臂智能拾取子系统_实施计划书.md`

## 三个组件与文件映射

| 计划书组件 | 实际落地 | 说明 |
| --- | --- | --- |
| `grasp_agent/` | `grasp_loop.py` + `vision_driver.py` | 六态闭环 + `ArmDriver` 适配（注册名 `oceanus_vision`） |
| `grasp_web/` | `web/server.py` + `web/static/index.html` | 树莓派上的训练作业台（FastAPI :8090） |
| `vision/` | `vision/*.py` | 数据集 / 训练 / 评估 / 原子部署 |
| （公共） | `arm_io.py`、`calibration.py`、`config.py` | ROS 接入层 / 9 点标定 / 配置加载 |

| 文件 | 职责 |
| --- | --- |
| `config.yaml` | 三组件共用配置（舵机 ID 映射、模型路径、MQTT、闭环参数） |
| `arm_io.py` | 相机订阅、IK 复用、舵机下发、**舵机回读**（厂商 `servo_states` 话题）、mock 合成画面 |
| `calibration.py` | 像素↔工作平面仿射标定（9 点采集 + 拟合 + 存盘） |
| `grasp_loop.py` | **全自动抓取闭环**：检测 → 锁定 → 视觉伺服 → 夹取 → 验证 → 放入旁边的料槽 |
| `vision_driver.py` | 把闭环包成 `ArmDriver`，接入 `edge/arm_bridge` |
| `web/server.py` | 作业台 HTTP/SSE 接口（采集/标注/训练/评估/部署/标定/作业） |
| `web/static/index.html` | 作业台单页界面 |
| `tools/gen_synthetic_dataset.py` | 合成数据集生成（帧 + 精确真值标注），无任何样本时快速跑通管线 |
| `tools/build_plastic_dataset.py` | 从公开基准构建**单类塑料袋**数据集（保持官方划分，非塑料类作背景负样本） |
| `tools/augment_color_negatives.py` | 注入高饱和彩色物体（不标注）作**干扰负样本**，让模型学会「彩色 ≠ 塑料袋」 |
| `tools/eval_robustness.py` | **抗干扰评测**：阈值扫描 + 误检归因 + 彩色干扰注入对比 |

## 抗干扰：不被其他物体或其他颜色干扰

这个诉求本质上是**精确率（Precision）**问题 —— mAP 高但精确率低，现场就是
机械臂去抓不该抓的东西。用 `tools/eval_robustness.py` 实测（公开基准 400 张测试图）：

| 观测项 | 结果 | 含义 |
| --- | --- | --- |
| 阈值扫描（部署尺寸 416） | conf 0.30 → P 0.521 / R 0.423；conf 0.60 → **P 0.654 / R 0.316**；conf 0.90 → P 0.933 但 **R 0.015** | **无法同时做到高精确率与可用召回**；「mAP50 0.42」这个数字严重高估了实际可用性 |
| 误检归因（313 个误检 @conf 0.35） | 落在其他物体上 **13.4%**（泡沫 23 / 其他 17 / 渔具 2）；落在纯背景上 **86.6%** | 误检主因是背景乱报，其次是其他物体 |
| **彩色干扰注入**（每图贴 4 个高饱和色块） | 检出 **695 → 1412（+103%）** | **模型会被彩色物体严重带偏** —— 这正是要解决的问题 |

### 对策实测（彩色负样本增强）

按上面的对策做了一版对照实验：**同配置（768/120 轮）只换数据集**，
唯一变量是「train/val 是否注入彩色干扰负样本」，因此可以干净归因。

| 指标 | v2（干净数据集） | **v4（彩色负样本增强）** |
| --- | ---: | ---: |
| **彩色注入后检出增幅** | **+103%** | **−2.4%** |
| 干净 test mAP50 @416 | 0.423 | 0.357 |
| P / R（F1 最优点） | 0.521 / 0.423 | 0.438 / 0.406 |
| 当前部署阈值下的 P / R | 0.654 / 0.316（@0.60） | 0.569 / 0.319（@0.50） |

**结论**：彩色干扰**从「检出翻倍」压到「基本无影响」** —— 核心诉求达成。
代价是整体精度下降（模型变保守）。这是一笔**有意的取舍**：
机械臂场景宁可漏抓，也不能被彩色物体带偏去抓错东西。
部署阈值因此由实测重新选定为 **0.50**（原 0.60 召回偏低）。

**仍未解决**：误检归因显示「落在其他物体上」占比没变（13.8%），
「落在纯背景上」仍是 86.2% —— 合成彩色负样本只解决**颜色**这一维度，
**其他物体**与**背景乱报**要靠现场真实样本（`eval_robustness.py` 可复测）。

> ⚠️ 真正解决现场抗干扰仍要靠现场样本：合成注入能降低对高饱和颜色的误敏，
> 但不能替代在真实水面/真实浮标上的采集与验证。

## 没有现场样本时，怎么先把模型做出来

### 路径 A（推荐）：公开基准 → 单类塑料袋

项目已有一套现成的公开数据集基础设施，不必临时上网找：

| 项 | 内容 |
| --- | --- |
| 数据集 | **SC-FloatWaste (FloatWaste)**，多区域河流漂浮垃圾检测基准 |
| 规模/许可 | 7,906 张标注图 / CC BY 4.0（可正式引用） |
| 来源 | HuggingFace `OliviaWuYJ/sc-floatwaste`；Zenodo DOI `10.5281/zenodo.21934286` |
| 落地情况 | 已按冻结配额下载 1,172 张（官方 train 600 / val 172 / test 400）+ 四契约类标注 |
| 溯源 | `ml/datasets/public_floatwaste/PROVENANCE.json` |
| 既有评测 | `docs/competitions/public-floatwaste-benchmark.md`（**感知精度对外口径的唯一事实源**） |

```bash
# 1) 构建单类塑料袋数据集（只保留 plastic，非塑料类作背景负样本；官方划分不变）
python edge/grasp/tools/build_plastic_dataset.py
#    → edge/grasp/data/public_plastic/{images,labels}/{train,val,test} + data.yaml

# 2) 训练（需要 torch + ultralytics；本机已有 Python 3.10 训练环境带 GPU）
yolo detect train model=yolo11s.pt \
  data=edge/grasp/data/public_plastic/data.yaml \
  epochs=120 imgsz=768 batch=12 device=0 workers=2 \
  patience=40 lr0=0.005 cos_lr=True

# 3) 在**独立测试集**上评测（官方 val 划分，未参与训练或调参）
yolo detect val model=runs/.../weights/best.pt data=<data.yaml> split=test imgsz=416

# 4) 导出 ONNX（尺寸必须与 vision.input_size 一致）→ 部署 → 校验
yolo export model=runs/.../weights/best.pt format=onnx imgsz=416 opset=12
python -c "...ModelStore.deploy(best.onnx, metrics=...)"
```

**实测结果（2026-10-15，RTX 5060 Laptop）**

| 版本 | 训练配置 | test mAP50 @768 | **test mAP50 @416（部署尺寸）** | P | R |
| --- | --- | ---: | ---: | ---: | ---: |
| v1 | 从 litter 权重续训，28 轮早停 | 0.426 (@640) | 0.382 | 0.489 | 0.461 |
| **v2** | `yolo11s.pt` 起训，768 / 120 轮（最优第 100 轮） | **0.494** | **0.423** | 0.512 | 0.498 |

测试集为 400 张 / 940 个真值框，**三区域汇总**（含两处完全跨域区域）。
项目历史同类评测（单类 `litter`，imgsz 1280、60 轮）为 0.5135 —— 分辨率与数据量是主要瓶颈，
详见 `public-floatwaste-benchmark.md` §4.2/§6。

> ⚠️ **口径边界（不得违反）**：以上都是**第三方公开数据集上的精度**，
> **不是连江现场精度，也不是本项目设备的实测精度**。对外只能写
> 「在公开漂浮垃圾基准上的精度」；禁止写成「现场验证」「真实海域验证」。
> 另外数据集是**河流**漂浮垃圾，现场是**海水养殖区**（含鲜艳彩色浮标），
> 存在域差异；现场精度必须在连江采集独立测试集后另行评测。

### 路径 B：合成数据（连公开数据都不便获取时的兜底）

```bash
python edge/grasp/tools/gen_synthetic_dataset.py --count 160 --tag synthetic_demo --build
```

> ⚠️ 合成数据训出的权重只在合成画面分布内有效，**只证明管线可用**，
> 不代表任何真实场景精度。仅用于离线调试 UI 与状态机。

## ROS 没起来时作业台的表现（现场排障）

现场最常见的故障是忘了 `source` 或厂商节点没启动。此时：

* **作业台照常启动**（不会整个崩掉），`/api/status` 的 `ros_error` 给出可执行的排障指引；
* 页顶显示红色「硬件未接入 + 原因」胶囊；
* 实时画面区显示 **NO CAMERA SIGNAL 占位帧**（而不是让 `<img>` 永远挂着、
  把整个页面卡在 loading）；
* 检测器仍然加载已部署模型 —— 所以「模型是否就绪」与「硬件是否就绪」是两件独立的事，
  可以分别排查。

## 为什么平台侧零改动

`edge/arm_bridge/bridge.py` 已把 `edge/` 加入 `sys.path`，因此只需在
`arm_bridge/config.yaml` 里把 `driver.backend` 改成 `oceanus_vision`，
`build_arm_driver()` 就会惰性加载 `grasp.vision_driver` 并注册进驱动表。
平台侧 MQTT 契约、派单状态机、后端代码**一行都不用改**。

## 三条硬约束（都来自厂商源码实测结论）

1. **不抢串口**。厂商 `hiwonder_servo_manager` 已持有舵机总线。我们只发
   `/servo_controllers/port_id_1/multi_id_pos_dur`（厂商 serialproxy 订阅它），
   舵机回读读厂商已发布的 `/servo_controllers/port_id_1/servo_states`
   （`ServoStateList`，含 `voltage` / `position` / `error`）—— 与厂商节点共存。
2. **不 import 厂商 `bus_servo_control`**。它在 import 时注册 SIGINT handler 并
   调用 `rospy.signal_shutdown()`，在 uvicorn 进程里会劫持优雅退出信号。
   这里自己发同一种消息，行为等价、无副作用。
3. **IK 复用厂商 `kinematics.ArmIK`**。逆运动学是纯计算、无副作用，复用它与厂商
   解算完全一致；不做第二套实现。

舵机 ID 映射（厂商源码口径）：`ID1` 夹爪（脉宽越大越开）／`ID2` 腕部旋转（500 中位）
／`ID3-6` 逆运动学四关节。`gripper_open / gripper_close` 等**需在真机核对**
（配置里已标注）。

## 「无预设动作」的落点

厂商 demo 是「预设初始位姿 + 录制关节序列回放」，每个脉宽都是录制值。
本子系统里每个动作都由感知输出经 IK 现算：

```text
YOLO 检出 → 像素↔世界坐标 → ArmIK.setPitchRanges() → 总线舵机下发
```

夹爪也是**自适应渐闭**：小步闭合 + 回读位置停滞判断，夹到即停，不靠固定脉宽。
放置动作同样不含任何录制序列：`coord` 模式由 IK 现算到料槽坐标，`pose` 模式
只回放**一个示教终点位**（见下节），不是「预设动作序列」。

## 与厂商源码对齐的口径（已逐条核对，改前先看这里）

真机动作全靠这几个常数，**它们不是猜的，来自 `机械臂源码/ArmPi FPV源码/`**：

| 口径 | 值 | 厂商依据 |
| --- | --- | --- |
| 夹爪方向 | **脉宽越大越闭** | `warehouse/in.py` 夹住 450 / 松开 75；`Sensor/AIColorsorting.py` 注释「爪子张开 = servo1-280、夹持器闭合 = servo1」 |
| 开合脉宽 | 开 200 / 闭 450 | 同上（厂商用过 75/450、220/500、150/450） |
| 抓取俯仰角 | **-175°**（竖直朝下） | `warehouse/in.py`、`object_sorting/sorting.py`：`grasp_pos.rotation.r = -175`，区间 [-180, 0] |
| 原位姿 `home` | (0, 0.17, 0.30) / -65° | `face_detect/scripts/face.py` initMove |
| 侧向工位几何 | x=±0.275、y=0、底层 z=0.02、俯仰 -130 | `warehouse/in.py` 的 `shelf_position` / `roll_dict`（R1/L1） |
| 舵机话题 | `multi_id_pos_dur` 下发 / `servo_states` 回读 | `hiwonder_servo_serialproxy.py`；`ServoState` 含 goal/position/error/voltage |
| 坐标原点 | 臂座安装面（「云台中心在地面的投影」） | `Ai_FPV/ArmIK/InverseKinematics.py` 注释 |

★ **`plane_z`（水面高度）必须实测**：厂商在桌面抓取用 z=0.01~0.015，因为台面≈原点平面；
船上甲板高于水面，水面 z 是**负值**（甲板高出水面 8cm → -0.08）。这个值不准，
爪子要么压不到袋子、要么撞下去。作业台「5 · 9 点标定」里可直接填并保存。

## 放置点：袋子放进旁边的料槽

派单后的验收动作只有一句：**识别静态塑料袋 → 夹取 → 放进臂座旁边的料槽**。

抓取点每帧由视觉现算，但料槽是**固定工位**，所以放置点只需给一次。两种给法
（`coord` 优先，字段见 `config.yaml` 的 `agent.place`）：

| 给法 | 写入位置 | 适用 |
| --- | --- | --- |
| `coord: [x, y]` | `calibration.json` 的 `place`（作业台保存时） | 已知料槽在工作平面坐标系里的位置（默认 `[0.275, 0.0]` = 厂商侧向货架同口径；左舷填 `-0.275`） |
| `pose: {舵机ID: 脉宽}` | 同上 | **现场最省事**：作业台「手动点动」把臂摆到槽口上方 → 点「用当前姿态作放置点」 |

高度与姿态一律**绝对量**（`approach_z` 0.12 / `drop_z` 0.02 / `retreat_z` 0.20 / `pitch` -130），
不用「水面 + 偏移」——水面在甲板以下时那个算法会把臂送到槽口下面去。

* 示教值写在 `calibration.json`（与设备绑定，重标定后需重新示教），`config.yaml`
  里的值只作兜底；两处都没有时**闭环直接拒绝开工**（`/api/grasp/start` 返回 400，
  `GraspLoop.run()` 返回「未配置料槽放置点」）——夹起来没地方放比不抓更糟，
  绝不允许「夹住了就算成功」。
* 放置动作：抬到槽口上方 → 下探（`place.drop_z`，**绝对 z**，默认 0.02）→
  松爪 → 停顿 `settle_s` 等袋子落进槽 → 抬离 → 回待命位；全程用 `place.pitch`（-130°，
  厂商侧向工位姿态），不是待命位的 -65°。
* 结果分两个字段上报：`ok`（夹到了）与 `placed`（**真的放进槽了**）。`placed=False`
  时驱动把 `review_result` 置为 `recheck` 并记 warning —— 人手还得去把夹爪上那袋取下来。
* `tools/precheck.py` 把「放置点」列为**真机必需项**（与标定并列），mock 模式下
  用演示放置点（`source=demo`，真机上会被判为未示教）。


## 抗浮标三层防线

| 层 | 落点 |
| --- | --- |
| 训练层 | 浮标作 `buoy` **显式负类**参与训练（`vision/dataset.py` 采集时把浮标拍进画面） |
| 推理层 | 仅 `plastic_bag` 且过阈值才成候选（`vision/detector.py` `targets()`） |
| 决策层 | HSV 饱和度辅助校验 + 轨迹邻域锁定（`grasp_loop.py` `_candidates/_lock`） |

HSV 只作**辅证**：水面反光会破坏颜色稳定性，主判据始终是模型类别与置信度。

## 快速开始

### 真机一键自动抓取（识别 → 抓取 → 入槽，无需平台派单）

树莓派上「开机后机械臂自己捡」是**一条命令**：

```bash
bash edge/grasp/deploy/run_autopick.sh          # 起厂商 ROS 节点 + 等话题就绪 + 自检 + 常驻自捡
bash edge/grasp/deploy/run_autopick.sh --check  # 只做前置检查，不动机械臂（排障第一条）
sudo systemctl enable --now oceanus-arm-autopick  # 交给 systemd，开机自启
```

开发机一侧的打包与部署也固化成了脚本（含 38MB 模型，默认只验证不动臂）：

```powershell
python scripts\deploy_autopick_to_pi.py --host 192.168.149.1 --dry-run   # 先看要拷什么
python scripts\deploy_autopick_to_pi.py --host 192.168.149.1             # 部署 + 只读验证
python scripts\deploy_autopick_to_pi.py --host 192.168.149.1 --apply     # 确认后才真开跑
```

> ★ 为什么需要 `run_autopick.sh` 这一层：`supervisor` 本身**不启动厂商 ROS 节点**，
> 以前 `ExecStart` 直接跑 supervisor，板子重启后没人起那三组 launch，
> 于是拿不到 ROS 链路、按设计 exit 2 —— 表现为「服务 failed、机械臂一动不动」。
> 详见 **[deploy/README.md](deploy/README.md)**（含退出码语义、首次跑通清单、排障对照表）。

### 开发机（无 ROS，合成画面跑通 UI）

```bash
python -m pip install fastapi "uvicorn[standard]" paho-mqtt opencv-python numpy PyYAML
python edge/grasp/web/server.py --mock          # 脚本方式启动（自行处理 sys.path）
# 浏览器打开 http://127.0.0.1:8090
```

mock 模式下：画面是合成水面（低饱和塑料袋 + 高饱和浮标），检测用 HSV/轮廓合成，
舵机为模拟下发，标定自动装载演示映射 —— 因此**整条六态闭环可以离线跑完**，
用于验证 UI、状态机与抗干扰逻辑。

### 树莓派真机

```bash
# 1) 依赖
pip3 install -r edge/grasp/requirements.txt      # opencv 可用 apt 的 python3-opencv
# 2) 拷到树莓派（保持目录名 grasp，确保 edge/ 与 armpi_fpv 工作空间都在）
source ~/armpi_fpv/devel/setup.bash
# 3) 起作业台
python3 /path/to/edge/grasp/web/server.py --config /path/to/edge/grasp/config.yaml
```

页面流程：采集（把浮标一起拍进去）→ 标注（袋 = `plastic_bag`，浮标 = `buoy`）
→ 训练（或先跑免训练校准档）→ 评估通过后部署 → 9 点标定 → **放置点示教**
（手动点动到槽口上方 → 「用当前姿态作放置点」）→ 开始作业。

### 接入平台派单

```yaml
# edge/arm_bridge/config.yaml
driver:
  backend: "oceanus_vision"
  oceanus_vision:
    config_path: null       # 默认读 edge/grasp/config.yaml
    review_result: "recheck"
```

平台上把 `driver.backend` 显示同步即可（`ArmSimConsole.vue` 的 `DRIVERS` 已加
`oceanus_vision`）。派单后桥接层照旧发 `ack / progress / telemetry`，其中
telemetry 的 `servos` 字段来自**真实舵机回读** —— 这是「机械臂真的动了」的硬证据。

## 无硬件自测

```bash
# 桥接层与驱动注册不受影响
python -m pytest edge/arm_bridge -q

# 抓取闭环 + 放置入槽 + 作业台接口（全 mock，无需 ROS/相机/硬件）
python -m pytest edge/grasp -q

# 视觉驱动可从注册表构造（不发硬件调用）
python edge/arm_bridge/main.py --config edge/arm_bridge/config.yaml \
  --check-driver --driver oceanus_vision

# 协议闭环（仍用 simulated 驱动，不触发真机）
python edge/arm_bridge/main.py --config edge/arm_bridge/config.yaml --dry-run

# 启动自检（真机：配置/模型/标定/放置点/相机；--mock 在笔记本上空跑）
python edge/grasp/tools/precheck.py
```

`edge/grasp/tests/` 里那份「派单 → 拾取 → 入槽」的端到端测试用的是**真驱动**
（`OceanusVisionArmDriver`）+ 内存 transport，所以它同时证明了「换驱动不动平台契约」
不是口头承诺。

## 口径红线

- mock / 合成画面 / 留出集指标都不代表现场验收，真机受控实验仍只能标 **E2/E3**。
- 不把「链路跑通」写成「识别准了」；感知精度以留出集 mAP 表述，并注明非现场精度。
- 未完成标定、未部署模型、未示教放置点时，`pick()` 明确失败上报，**绝不假装成功**。
- `placed=True/False` 必须分开说：夹住了 ≠ 放进槽了。
