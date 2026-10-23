# 边缘检测器（edge/detector）

不依赖训练数据的海漂垃圾识别。本目录下有**两条通道**：

| 通道 | 文件 | 默认 | 定位 |
| --- | --- | --- | --- |
| `cv` | `detector.py`（`CvDetector`） | **是** | OpenCV 传统视觉，零数据依赖，主告警链路 |
| `world` | `world_detector.py`（`WorldDetector`） | 否 | 开放词汇零样本（YOLO-World），演示与对照 |

两条通道的**输出契约完全一致**，所以下游（时序校验 → MQTT → 派单 → 大屏）一行都不用改；
但**置信度尺度不同**，时序门限必须各用各的 —— 细节见本文最后的"开放词汇通道"一节。

## 为什么不是 YOLO

原本的路线是 YOLO11s + ONNX，但它的前置依赖是**数据集**：
`ml/configs/seasight.yaml` 的 stats 至今是 0（一张图都没采）。
采集 → 标注 → 训练 → 导出 → 边缘部署，整条链 3~4 周且受天气与出海排期支配。

而这套 OpenCV 方案的**数据依赖是 0**：不需要一张标注图，写完就能在真实画面上跑。

| | OpenCV 方案 | YOLO 方案（自训练） | 开放词汇方案（零样本） |
| --- | --- | --- | --- |
| 数据依赖 | 0 张 | 2000 张全标框 | 0 张 |
| 工期 | 2~3 天 | 3~4 周 | 已有权重，接上即用 |
| 召回 | 中（60~75%） | 高（数据足够时 85%+） | 未标定，**不报精度** |
| 可解释性 | **强**（阈值当场可调） | 弱（黑盒） | 弱（黑盒） |
| 加新类别 | 改规则 | **重采重训** | **改一个词** |
| 硬件 | 纯 CPU | 需要 NPU/GPU | 最好有 GPU（本仓库默认 CPU） |
| 上限 | 低 | 高 | 中（受提示词与域差异支配） |

**误报怎么办**：交给下游时序校验器（`simulator/simulator.py` 的 `TemporalValidator`）。
架构本来就是按"检测器会误报"设计的 —— 设计文档 §2.3 记录原始检测 5772 条 → 确认事件 52 条。
本模块实测（合成海面 200 帧）：原始检测 599 → 确认事件 3，**抑制率 76%**。

## 管线

```
帧 → ROI 掩膜（排除天空/岸线/固定物）
   → 前景 = 背景减除(KNN) ∪ 白亮通道 ∪ 暗色通道
   → 形态学开/闭（吃噪点、填空洞）
   → 轮廓筛选（面积 / 凸包填充度 / 长宽比）
   → 特征（HSV 明度饱和度、圆度、填充度、长宽比）
   → 规则分类 foam / plastic / fishing_gear / other
   → 伪置信度（含反光惩罚）
   → TemporalValidator（复用，不改）
   → MQTT 上报（契约不变）
```

**为什么三路前景**：
- 背景减除抓"新出现的、在动的"；
- 白亮通道抓泡沫 —— 泡沫可能停着不动，几十帧后会被背景模型学成背景，从此永不报警；
- 暗色通道抓渔网绳索 —— KNN 会把"比背景暗"的东西标成**阴影(127)**，阈值化时被剔掉，
  实测暗色细长渔具的候选区因此整帧为空。

## 调参（现场答辩可以直接改给评委看）

所有参数在 `edge/config.yaml` 的 `detector` 段，改完重启生效。
`detector.py` 里的 `DEFAULT_CONFIG` 是兜底默认值，
**测试会逐项与 config.yaml 对账** —— 防止"改了配置却不生效"这类静默失效。

现场最常调的四个：

| 场景 | 参数 | 怎么调 |
| --- | --- | --- |
| 漏检多 | `background.dist2_threshold` | 调小（更敏感，噪点也更多） |
| 误报多 | `temporal.min_hits` | 调大（要更多帧连续命中才确认） |
| 正午反光误报 | `confidence.glare.penalty` | 调大 |
| 岸边建筑/天空误报 | `roi` | 填多边形顶点（相对坐标 0~1） |

## 已知边界（答辩时请主动说，不要藏）

- **强反光、雨天、夜间显著退化** → 产品约束写成"作业时段：白天平潮"，而不是假装全天候。
- **`plastic` 准确率明显低于 `foam`** → 塑料颜色杂、形状不定，单帧可分性本来就低；
  建议产品线先聚焦 `foam`（EPS 泡沫浮球碎片），这也是连江养殖区最突出、治理优先级最高的类别。
- **定位是设备点位而非目标真实经纬度** → 摄像头固定安装时需预标定朝向与视场角，
  否则事件坐标就是摄像头自己的坐标。

## 测试

```bash
cd edge/detector && python -m pytest test_detector.py test_world_detector.py -v
```

**75 项**（cv 16 + world 59），全部用合成帧与注入式假 runner，
**不需要摄像头、不需要视频、不需要数据集、不需要 torch**。

★★ 新增测试文件后请同时改 `Makefile` 的 `test-edge` 目标：
那一行写的是 **pytest 的文件名参数**，不是目录。
漏加的话 `make test-edge` 照样"全绿"—— 只是新测试根本没跑。

其中数项是契约与隔离守卫：

- `CLASS_NAMES` 必须与 `backend/app/services/ai/server.py`、`ml/configs/seasight.yaml`
  逐项一致（第四处定义漂移会直接失败）；
- **open-vocab 标签必须穷举映射回四类**（`label_map.py`），
  并用 AST 解析 `scripts/detect_marine_demo.py` 的 `DEFAULT_CLASSES`，
  上游加了提示词而映射表没跟上会直接失败；
- **cv 通道实现必须与开放词汇通道无关**（`detector.py` / `__init__.py` 里不得出现
  world 相关符号）——因为 `edge/` 被只读挂载进线上容器，后端有 3 处
  `from edge.detector.detector import CvDetector`，多一个 import 就可能把平台拖挂。

## 开放词汇通道（backend: world）

**它是什么**：YOLO-World 零样本检测。现场说一个新词（"塑料瓶""渔网""泡沫浮球"），
系统立刻能检出这一类，**不需要采集、标注、重训** —— 这是它相对 `cv` 通道唯一的硬性优势。

**它不是什么**：不是"更准的 OpenCV"，也**不承诺精度**。

### ★ 门限口径：绝不要套用 0.45

`temporal.min_confidence = 0.45` 是给 `cv` 通道的**伪置信度**（范围 0.30~0.95）标定的。
开放词汇模型的输出分数低一个量级 —— 本仓库历史实测：

| 来源 | 最高置信度 |
| --- | --- |
| 5 张演示原图（`artifacts/marine-detections-yoloworld.json`，imgsz=1600） | **0.413**，且**没有一条 ≥0.45** |
| 四份 prompt 标定记录（`artifacts/vision-preview/`） | **0.184**（foam cup） |

若共用 0.45，本通道的检测会在时序环节被**全部丢掉**，而且不报任何错。
所以本通道有自己的 `conf` 与 `temporal_min_confidence`（默认 **0.02**，
在 `edge/config.yaml` 的 `detector.world` 段），由
`edge/main.py:_effective_min_confidence` 按通道取值。

### ★ 默认 0.02 是怎么来的（不是拍的，也不是"标定"出来的）

先说清性质：**独立测试集真值标注为 0，没有任何数据能标定这个门限。**
所以 0.02 不是"精度最优值"，而是为满足一个功能性要求选定的**演示工作点**：

> 要求：默认门限下，`CLASS_NAMES` 的**四个类别都必须可报**。
> 否则"现场加一个词就能检出这一类"的演示会毫无反应，
> 而现象只是"加词没效果"，根本看不出是门限造成的。

依据是 `scripts/compare_vision_backends.py` 产出的
`artifacts/metrics/vision_backends_latest.json` 里 `threshold_probe` 段
（5 张 4K 真实照片，门限压到 0.001 后的实测逐类最高分）：

| 契约类别 | 检出条数 | 最高分 | 在 0.10 下可报？ | 在 0.02 下可报？ |
| --- | --- | --- | --- | --- |
| `other` | 171 | 0.5824 | ✅ | ✅ |
| `fishing_gear` | 26 | 0.0514 | ❌ | ✅ |
| `plastic` | 11 | 0.0355 | ❌ | ✅ |
| `foam` | 9 | **0.0260** | ❌ | ✅ |

`foam` 是最紧的一类，门限必须低于 0.026 才能让它可报 → 取 0.02。

**⚠️ 曾经的真实缺陷**：默认值原为 0.10，在该门限下上表后三类的最高分全部低于门限，
即**这个通道永远只会输出 `other`**。`test_world_detector.py` 里
`test_default_conf_keeps_every_contract_class_reportable` 现在守住这条，
默认值一旦被调回 0.026 以上就会失败并要求重新取证。

现场换门限：`--world-conf 0.05`（或用 `edge/config.yaml`），即时生效。
**换之前先看 `threshold_probe`** —— 它给出分数分档与逐类最高分，
能直接回答"这次改动会让哪一类消失"。

### ★ 合成序列为什么不能用来考察本通道

`--sequence` 那一组里本通道**原始输出就是 0**（不是被门限丢的：
诊断显示压到 0.001 重跑同样 `raw=0`、`dropped_*` 全为 0）。

原因不是分辨率 —— 把合成帧放大 3 倍（1920×1080）结果一样是 0。
合成帧是程序化绘制的**纯色圆盘**（单色背景 + 3 个色块圆），
缺少开放词汇零样本模型赖以判别的外观纹理，属于其输入分布之外。

**所以这 0 条不得读作"开放词汇通道无效"**，它只说明
**这批合成真值不适合考察该通道**。本通道的实际表现请看静态照片组
（那里的 4K 真实照片有真实纹理）。对照脚本会自动把这条写进 `caveats`。

### 用法

```bash
# 演示：合成海面 + 现场加两个词，把检测框画出来
python edge/main.py --source synthetic --backend world --show \
    --world-prompt "foam buoy" --world-prompt "plastic bag"
```

### 依赖是可选的，缺了会明确失败

`torch` / `ultralytics` / `transformers` 与 CLIP 文本编码器权重
（`~/.cache/clip-vit-base-patch32/pytorch_model.bin`）**不在边缘测试环境里**。
缺失时：

- `WorldDetector.available == False`，`last_error` 给出可执行提示（含镜像下载示例）；
- `edge/main.py` 在启动时**直接退出并打印原因**（退出码 3）。

★ 这一条是刻意设计的：本通道不可用与"画面里真的没有垃圾"在数字上一模一样
（都是零检测），如果静默继续，现场会把"没跑起来"讲成"没有污染"。

### ★★ 权重路径：`weights` 是相对路径，按**仓库根**解析，不按 cwd

这是实测踩到、且**会把现场演示直接卡死**的一个坑：

`edge/config.yaml` 里写的是裸文件名 `yolov8s-worldv2.pt`，
而 ultralytics **按当前工作目录**找权重。于是 —

| 启动位置 | 结果 |
| --- | --- |
| 仓库根 | 找到本地文件，正常启动 |
| `edge/`（`edge/main.py` 的常规用法） | **找不到 → 静默转向联网下载** |

联网那条路在本机上的实测表现：重试 3 次，每次都卡在
`curl: (56) schannel: server closed abruptly`，**启动挂住 2 分钟以上，
且不抛出任何异常** —— 现象只是"程序没反应"，
完全看不出它是在下载权重。

**已做的处理**（`world_detector.py`）：

1. `resolve_weights_path()` 把相对路径一律按**仓库根**解析，与 cwd 无关；
2. 新增配置项 `detector.world.allow_weights_download`，**默认 `false`** ——
   本地找不到权重就**显式报错**（报错里给出解析后的绝对路径），
   绝不让 ultralytics 静默下载。现场网络不可预期，"卡住"比"失败"糟得多。
3. 可用性探测（`_probe_environment`）也一并检查权重文件，
   使失败在**启动阶段**就报出来，而不是等到 `load()`。
4. 回归测试 `test_world_detector.py::TestWeightsPathResolution`（6 项）
   把"解析结果不随 cwd 变化""默认禁止下载"钉死。

首次部署确需下载时：把 `allow_weights_download` 改成 `true`，
或按解析出的绝对路径手工放好权重。

### 对照工具

```bash
python scripts/compare_vision_backends.py --sequence --frames 60
```

输出 `artifacts/metrics/vision_backends_latest.json`。**这是定性对照，不含精度指标** ——
独立测试集为空（清单 `status=planned`），precision/recall 的分母为零，
定量评测走 `ml/scripts/evaluate_opencv.py`（带清单门禁）。
对照含两组：静态照片组（注意 cv 通道不适用于单张照片，脚本会把原因算出来写进说明）
与合成序列组（两条通道同帧对照，含程序化真值的命中计数，口径为 E1/E2 合成数据）。

### 前端对照数据导出

```bash
python scripts/export_channel_compare.py
```

输出 `frontend/public/demo/vision-channel-compare.json`，供前端
**「双通道对照」页（`/vision-compare`）**使用。它回答的问题是
"**两条通道各自在什么条件下有效**"，不是"谁更准"，所以刻意带了**两个 regime**：

| regime | 传统视觉 (cv) | 开放词汇 (world) |
| --- | --- | --- |
| 单张 4K 照片 | **0 条**（适用范围之外：需要连续帧建背景模型） | 正常检出（来自已验证样例数据） |
| 连续帧（合成序列） | 正常检出（172 条 / 93.3% 合成命中） | **0 条**（合成帧缺外观纹理，不在其输入分布内） |

只摆一列会把"不适用"读成"更差"—— 两个 regime 都摆出来，才能把
"两条通道回答不同问题"讲完整。页面上在两列下方都会把这句话写明。

（历史注：`/analyze` 的手动上传分析页已并入「智能助手」对话页，`/analyze`
现在 redirect 到 `/assistant`；本对照页是独立路由，不吃这个 redirect。）

## 评测方法（WP-06 感知评测）

**能力口径（冻结）**：本检测器只能称为 **OpenCV 传统视觉**，不是 YOLO、
不是视觉大模型、不是已训练模型。评测只回答"这套 OpenCV 规则在给定数据上表现如何"。

**怎么评测**（从仓库根）：

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe ml\scripts\evaluate_opencv.py --config ml\configs\seasight.yaml --output artifacts\metrics\opencv_latest.json
```

评测脚本**只认数据集清单**（`ml/datasets/manifests/`，训练/验证/独立测试/现场盲测
四类分集），不扫目录，防止测试集泄漏。输出 JSON 含：命令、日期、代码版本
（无 .git 时用固定串+文件指纹）、样本量、分母、按 seasight.yaml 类别的
precision/recall/F1、confounder 分层（光照/背景/目标尺寸等），每项带 `evidence_level`。

**数据为空 → `not_evaluated`**：`ml/datasets/` 目前一张图都没有，
评测输出 `not_evaluated` 并给出原因，**绝不生成虚假精度**（退出码 0）。

**76% 抑制率的口径边界**：README 开头那句"抑制率 76%"只针对**时序链路**
（200 帧 / 599 原始检测 / 3 确认事件），公式是 `(fed - absorbed) / fed`，
分母是"喂进时序校验器的检测数"，不是帧数、更不是确认事件数。
它是 `edge/simulator/simulator.py` 的 `TemporalValidator` 统计，**不代表检测器精度**，
禁止对外写成"识别精度 76%"。

**指标口径守卫**：`edge/detector/test_metrics.py` 锁定上述口径
（抑制率恒在 0~1、fed = absorbed + suppressed、单帧噪声只能进 suppressed；
precision/recall 分母为零 → `not_evaluated`，数值为 None 而非 0）。

## 换回 YOLO 时要动什么

只换 `CvDetector.detect()` —— 它的输出契约
（`{class, confidence, bbox:[x1,y1,x2,y2]}`）与 AI 推理服务完全一致，
下游（时序校验 → MQTT → 派单 → 大屏）一行都不用改。

**这一条已经被验证过了**：`WorldDetector` 是第二个实现同一契约的通道，
接线方式是 `edge/config.yaml` 的 `detector.backend: cv|world`
（默认 `cv`，现网行为不变）。换通道时唯一需要额外处理的是**置信度标定**
（见上面「门限口径」一节）—— 契约相同不代表分数同尺度。
