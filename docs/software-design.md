# 探海灵眸 SeaSight — 软件系统详细技术方案

> 本文是软件部分的完整技术方案，面向技术评审与团队内部对齐。
>
> 篇幅较长，可按需跳读：**第 1 节**讲清系统边界与判断依据；**第 2–5 节**是五个子系统的设计；**第 6 节**是数据模型；**第 7 节**是关键技术难题与解法；**第 8–11 节**是工程保障、演进路线与工作量拆分。

---

## 目录

1. [系统定位与设计前提](#1-系统定位与设计前提)
2. [边缘感知软件](#2-边缘感知软件)
3. [AI 推理服务](#3-ai-推理服务)
4. [数据平台（后端）](#4-数据平台后端)
5. [前端可视化](#5-前端可视化)
6. [数据模型](#6-数据模型)
7. [关键技术难题与解法](#7-关键技术难题与解法)
8. [工程化与可靠性](#8-工程化与可靠性)
9. [部署与运维](#9-部署与运维)
10. [演进路线](#10-演进路线)
11. [工作量拆分](#11-工作量拆分)

---

## 1. 系统定位与设计前提

### 1.1 三个前提

本方案的每一个技术选择，都建立在这三个前提上：

| 前提 | 内容 | 推导出的设计 |
| --- | --- | --- |
| **P1** | 面向**区域治理**，不是单台设备 | 感知在岸、决策在云、执行在水；核心表是事件表与工单表 |
| **P2** | 现场是**弱网 + 无人值守** | MQTT + 本地队列 + 断网补传 + 遗嘱消息 |
| **P3** | 三组并行开发，**工期 3 周** | 接口先冻结；任何单一依赖缺失不阻断启动；不引 Kafka 等重组件 |

### 1.2 系统边界

**做什么**：

- 从摄像头视频流中识别 4 类海漂垃圾
- 把识别结果变成可追溯的事件与工单
- 自动/人工派单给水面机器人
- 沉淀治理数据，输出量化报表

**不做什么**（明确排除，避免方案膨胀）：

- ❌ 不存原始视频（只存垃圾证据帧，降低存储与合规风险）
- ❌ 不做机器人自主导航算法（由硬件组负责，平台只下发目标点）
- ❌ 不做垃圾成分化验与后端处置（岸基精分是线下流程）
- ❌ 不做多租户 SaaS（单县部署，operator 按乡镇分权即够）

### 1.3 五个子系统全景

```
┌─────────────────────────────────────────────────────────────────────┐
│  ① 边缘感知软件  edge/                                               │
│     拉流 → 抽帧 → 推理 → ★时序校验 → 节流 → MQTT 上报                │
│     关键：把误报压下来，而不是把检出率刷上去                          │
└────────────────────────────┬────────────────────────────────────────┘
                             │ MQTT QoS1（事件）
┌────────────────────────────▼────────────────────────────────────────┐
│  ④ 数据平台  backend/ + frontend/                                    │
│     事件服务 → Redis Stream → 派单引擎 → 工单状态机 → 大屏/报表       │
│     关键：平台是中枢不是显示屏                                        │
└────────────────────────────┬────────────────────────────────────────┘
                             │ MQTT QoS1（派单）/ 遥测回传
┌────────────────────────────▼────────────────────────────────────────┐
│  ② 机载软件  edge/robot/                                             │
│     接单 → ACK → 导航 → 打捞（三仓粗分）→ 回传 → 复核                 │
│     关键：应用层 ACK，而不是靠 MQTT QoS                          │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  ③ 模型训练管线  ml/                                                 │
│     数据集管理 → 两阶段训练 → 验证 → ONNX 导出 → 边缘部署            │
│     关键：flipud=0（禁上下翻转）+ 负样本抑制误检                      │
└─────────────────────────────────────────────────────────────────────┘
                             │ best.onnx
┌────────────────────────────▼────────────────────────────────────────┐
│  ⑤ AI 推理服务  backend/app/services/ai/                             │
│     HTTP 包装模型，支持热加载；模型缺失时降级 stub 模式                │
│     关键：独立进程，算法组可单独发版                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 边缘感知软件

### 2.1 流水线

```
RTSP 流（1080p@25fps）
   │
   ├─► ① 抽帧       每 3 帧取 1 帧 → ~8fps
   ├─► ② 预处理     letterbox 到 640×640（等比缩放 + 114 灰填充），归一化
   ├─► ③ 推理       YOLO11s，ONNX Runtime / TensorRT
   ├─► ④ 置信度过滤  conf < 0.45 丢弃
   ├─► ⑤ ★时序校验   目标级跟踪 + 三重门限关联
   ├─► ⑥ 上报节流    同目标 90 秒冷却
   └─► ⑦ 组包上报   MQTT QoS1，带 seq
```

> ② 处的 letterbox 细节与还原公式见 §3.3 —— 它是「框画偏了」这类
> 问题的头号嫌疑，且不在 stub 路径上，改预处理后务必单独验证。

### 2.2 为什么抽帧到 8fps

垃圾漂浮速度约 0.1~0.5 m/s。以 0.5 m/s、摄像头覆盖 100m 视野、1080p 宽度 1920px 估算：

- 每帧垃圾移动约 `0.5 / (100/1920) = 9.6 px`
- 8fps 下，相邻帧位移 9.6px，远小于垃圾自身尺寸（通常 40~80px）
- **IoU 保持在 0.7 以上，跟踪完全稳定**

25fps 相对 8fps，只是把算力花在几乎不动的目标上。降到 8fps 后算力节省 68%，跟踪质量无损失。

### 2.3 ★ 时序校验：本项目最核心的算法设计

#### 问题

海面有两类高频干扰：

| 干扰 | 视觉特征 | 与 EPS 泡沫的相似度 |
| --- | --- | --- |
| **浪花泡沫** | 白色、团状、有纹理、**随机位置出现又消失** | 极高，单帧上几乎无法区分 |
| **阳光反光** | 高亮斑点、**随波浪位置变化** | 高 |

不做过滤时实测：**每分钟上百条误报**。平台会被淹没，操作员会直接关掉告警——这是所有同类项目最常见的死法。

#### 判据

**浪花是瞬态的，垃圾是持续的。**

滑动窗口内同一目标连续 `min_hits` 帧命中 → 升级为确认事件。

#### 一个必须避开的实现陷阱

**第一版实现按"像素格网"统计命中**：把画面切成 64×64 的格子，统计每个格子在窗口内的命中次数。

结果：**确认事件 0 条**。

原因：目标检测框**逐帧抖动**——同一个垃圾，第 1 帧框在 `[412,288,468,331]`，第 3 帧可能在 `[408,291,465,336]`。格网边界恰好穿过目标时，两帧会落进**不同格子**。15 帧窗口里，同一个目标可能散落在 4~5 个格子里，每个格子都凑不满 3 次命中。

> **这是一个极易踩且不易发现的坑**：代码不会报错，日志显示"抑制率 100%"，看起来"效果很好"，实际上系统完全失效。

#### 正确实现：目标级跟踪

把**目标**（而不是像素格子）作为跟踪单元：

```python
class _Track:
    """被跟踪的目标。"""
    track_id: int
    cls: str              # 类别
    bbox: list[int]       # 最近一帧的框
    hits: int             # 累计命中
    misses: int           # 累计丢失
    confirmed: bool       # 是否已升级为确认事件
```

关联新检测框的三重门限：

| 条件 | 阈值 | 作用 |
| --- | --- | --- |
| **类别相同** | — | 防止泡沫与渔网互相"续命" |
| **中心位移** | < 60 px | 目标不会瞬移 |
| **IoU** | > 0.25 | 必须有实际重叠 |

```python
def _match(new_det, track):
    if new_det.cls != track.cls:
        return False
    if _center_distance(new_det.bbox, track.bbox) > match_distance:
        return False
    if _iou(new_det.bbox, track.bbox) < min_iou:
        return False
    return True
```

匹配成功 → `hits += 1`, `misses = 0`
无匹配 → `misses += 1`
`misses > max_misses` → 淘汰该 track

#### 为什么必须加 IoU 门限

只靠中心距离时，随机噪声会"碰巧"靠近跟踪目标：40 帧随机噪声里，实测误确认 3 次；300 条检测变成 55 个事件。

加上 `min_iou=0.25` 并把 `match_distance` 从 120 收到 60 后，测试全部通过：**300 条原始检测 → 1 个确认事件**。

#### 参数取值与依据

下表是**唯一真源**：`TemporalValidator.__init__` 的默认值与
`edge/simulator/config.yaml` 的 `temporal` 段必须与它逐项一致，
由 `test_defaults_match_design_doc` 与 `test_config_yaml_declares_all_thresholds`
两处断言守住（改一处忘改另一处会直接测试失败）。

| 参数 | 值 | 依据 |
| --- | --- | --- |
| `window_frames` | 15 | 8fps 下约 1.9 秒，覆盖一个完整浪周期 |
| `min_hits` | 3 | 浪花在同一位置连续 3 帧（0.375 秒）出现的概率极低 |
| `grid_size` | 64 | 上报冷却的空间聚合格网边长（px）；只用于节流，不用于跟踪 |
| `min_confidence` | 0.45 | 低于此值的框噪声大，不进入跟踪 |
| `match_distance` | 60 | 中心位移上限（px），1080p 下约为画面宽度的 3% |
| `min_iou` | 0.25 | 允许框抖动，但拒绝无重叠的"碰巧靠近" |
| `max_misses` | 5 | 丢失 5 帧（0.6 秒）后淘汰，容忍短暂遮挡 |
| `decimation` | 4 | 每 4 帧推一次模型 —— 真实边缘盒算力所限，且天然压噪声 |

> **参数漂移是怎么发生的、又是怎么被抓住的。**
>
> `max_misses` 一度出现三方不一致：本文档写 **5**、代码默认值写 **6**、
> 单元测试里也硬写 **6**。功能上没有任何报错 —— 因为测试只是**复述**
> 了那个数字，而不是去**核对**它，所以永远"通过"。
>
> 修法不是把数字改成一致就完事，而是让不一致变可检出：
> 测试改为从 `inspect.signature` 读取实现默认值，再与本文档参数表
> 正则比对。于是"文档与代码各说各话"从一类隐形缺陷变成一条会失败的断言。
>
> 同一轮还发现 `match_distance` 与 `min_iou` **在实例化校验器时被漏传**，
> 只能靠类默认值兜着 —— 意味着在 `config.yaml` 里改了不生效且不报错。
> 现已补齐注入链，并加断言确保配置键齐全。

#### 实测效果

模拟器 15 秒运行结果（7 台设备、840 帧）：

```
原始检测     5772 条
确认事件     52 个
抑制率       42.5%
```

三个场景交叉验证，抑制率稳定收敛：

| 场景 | 原始检测 | 确认事件 | 抑制率 |
| --- | --- | --- | --- |
| demo | 5772 | 52 | 42.5% |
| stress | 23734 | 183 | 41.9% |
| regression | 7150 | 47 | 42.8% |

**抽帧系数与抑制率的定量关系**（单元测试固定场景，60 帧、每帧 4 个瞬态噪声）：

| `decimation` | 喂入检测 | 被吸收 | 被抑制 | 抑制率 |
| --- | --- | --- | --- | --- |
| 1（不抽帧） | 300 | 59 | 241 | 80.3% |
| 2 | 300 | 29 | 271 | 90.3% |
| 4（默认，≈8fps） | 300 | 14 | 286 | 95.3% |

这张表把"抽帧天然压噪声"从论断变成了数字：抽帧系数每翻倍，
同类噪声被吸收的机会就减半。**注意它比上面三个场景的抑制率高一倍**——
因为这里刻意把噪声密度拉满（每帧 4 个纯随机检测），而真实模拟器里
噪声只按 50% 概率出现 1~3 个。两个数字都对，只是场景不同，
**引用时务必带上场景，否则很容易被问"到底是多少"**。

> **抑制率的统计口径踩过两次坑，都值得记下来。**
>
> 第一次：把"目标淘汰数"和"帧级 candidates - confirmed"混合累加，分母却是检测数 —— 单位不一致，算出 **140.9%** 的抑制率。修正为三个同单位计数 `fed` / `absorbed` / `suppressed`，恒满足 `fed = absorbed + suppressed`，抑制率必然落在 0~100%。现在由单元测试 `test_suppression_ratio_in_range` 守住这条不变量。
>
> 第二次（更隐蔽）：抑制率修正后只有 20%，看着偏低的根源是 `--interval` 被当成"事件间隔"填了 6 秒，导致抽帧率只有 1.4fps。语义拆开成"抽帧间隔"（默认 0.125s ≈ 8fps）与"上报冷却"（`--event-interval`）后，再叠加抽帧推理对噪声的压制，才收敛到 42% 这个真实水平。

### 2.4 上报节流

即使通过时序校验，同一片持续存在的垃圾也不该每帧都报。所以加两层节流：

| 层 | 参数 | 作用 |
| --- | --- | --- |
| 目标级冷却 | `cooldown_seconds: 90` | 同一目标 90 秒内只报一次 |
| 全局最小间隔 | `min_interval_seconds: 2` | 任意两次上报至少间隔 2 秒 |

### 2.5 幂等基础：seq 序号

平台用 `(device_id, seq)` 唯一约束做幂等，所以边缘端必须：

1. **单调递增**：每次上报前 `seq += 1`
2. **持久化**：每次自增后同步写本地文件

```json
{ "CAM-MABI-01": { "seq": 4821 } }
```

**为什么必须持久化**：边缘盒断电重启后若 seq 归零，重启后第一条事件 seq=1，而平台里 `(device, 1)` 已存在 → 平台判为重复 → **后续所有事件都被静默吞掉，且没有任何报错**。这是演示现场最容易翻车的点。

### 2.6 断网补传

```python
class OutboxBuffer:
    """本地待发队列。断网时积压，恢复后按原 seq 顺序补传。"""
    max_size: int = 1000      # 满了丢最旧（新的更重要）
    flush_batch: int = 20     # 每批补传条数
    flush_interval: float = 1.0
```

**必须按原 seq 顺序补发**。乱序补发虽然不会重复入库（唯一约束仍生效），但平台看到的事件时间顺序错乱，热力图与趋势图会出现"倒流"。

### 2.7 遗嘱消息（LWT）

连接时注册遗嘱，异常断开时 broker 代发，平台**立即**知道设备离线，不用等心跳超时。

```python
will_set(
    topic=f"marine/{site}/{dev}/status",
    payload=json.dumps({"device_id": dev, "online": False}),
    qos=1,
    retain=True,
)
```

详见 `mqtt-topics.md` 第六节。

---

## 3. AI 推理服务

### 3.1 为什么独立成进程

| 方案 | 问题 |
| --- | --- |
| 模型逻辑写在平台里 | 算法组换权重 → 要重启平台；平台组调接口 → 需要环境里有 onnxruntime 与 GPU 驱动 |
| **独立 HTTP 服务** | ✅ 算法组可热加载模型（`POST /infer/reload`），不影响平台；平台组只需一个 URL |

三个接口：

| 接口 | 说明 |
| --- | --- |
| `GET /infer/health` | 返回后端类型、模型版本、类别列表 |
| `POST /infer/detect` | 上传图片，返回 `detections` + `aggregate` |
| `POST /infer/reload` | 热加载，不重启 |

### 3.2 stub 模式（重要的工程妥协）

模型文件不存在时，服务**不崩溃**，而是进入 stub 模式返回确定性模拟结果：

```python
def _stub_infer(self, image_bytes: bytes) -> list[dict]:
    """用字节哈希生成确定性结果——同一张图结果一致，便于写测试断言。"""
    digest = int(hashlib.md5(image_bytes).hexdigest()[:8], 16)
    count = digest % 4 + 1
    cls = CLASS_NAMES[digest % len(CLASS_NAMES)]
    ...
```

**为什么这很重要**：数据集采集与标注需要时间（W1–W2），但平台组从 W1 就要开始联调。stub 模式让平台组在**没有任何模型**的情况下跑通全链路——这是三组并行开发能成立的关键。

### 3.3 预处理：必须用 letterbox，不能直接 resize

推理前把任意分辨率的图缩到 640×640。**必须等比缩放 + 灰边填充**：

```python
scale = min(640 / w, 640 / h)          # 取小者，保证长边塞得进
new_w, new_h = round(w * scale), round(h * scale)
canvas = Image.new("RGB", (640, 640), (114, 114, 114))   # ★ 114 灰
canvas.paste(resized, (pad_x, pad_y))
```

两个容易踩的点：

| 点 | 为什么 |
| --- | --- |
| **等比而非拉伸** | 1080p 是 16:9、640×640 是 1:1。直接拉伸让宽高比失真，模型训练时见的是 letterbox 后的图，推理不照做会让框的位置与形状系统性偏移，**越靠边缘偏得越多** |
| **填充色取 114 灰** | Ultralytics 训练端的默认填充色就是 114。换成纯黑会让模型在图像边界看到训练时从未见过的分布，边缘目标置信度莫名偏低 —— 这类问题极难归因 |

还原时必须先减 padding 再除 scale，顺序反了或漏掉 padding，
所有框会朝左上角整体平移：

```
x_原图 = (x_640 - pad_x) / scale
y_原图 = (y_640 - pad_y) / scale
```

> **为什么不直接 resize 完事**：项目早期确实是直接 resize，也没报错 ——
> 因为那时走的是 stub 模式，压根没跑真实推理。等真模型挂上去，
> 框会整体偏移，而"偏移多少"取决于原图宽高比，看起来像是模型不准，
> 实际是预处理错了。这类缺陷的修复成本远高于预防成本。

### 3.4 后处理：必须同时支持两种导出格式

| 导出配置 | 输出形状 | 每行含义 |
| --- | --- | --- |
| `nms=false`（**本项目默认**） | `(1, 4+nc, anchors)` | `cx, cy, w, h` + nc 个类别分数 |
| `nms=true`（EfficientNMS 烘进图） | `(1, N, 6)` | `x1, y1, x2, y2, score, class_id` |

判别方式：**末维为 6 且第二维不是 4+nc 时，按已烘 NMS 解析**。

```python
out = np.asarray(outputs[0])
if out.ndim == 3:
    out = out[0]                      # 去 batch 维
if out.ndim == 2 and out.shape[-1] == 6:
    return self._parse_nms_baked(out, ...)   # [x1,y1,x2,y2,score,cls]
# 否则按原始格式：(4+nc, anchors) → 转置 → cx/cy/w/h 换算
```

> **为什么两种都要支持**：默认选 `nms=false` 是为了部署后能调 IoU 阈值
> （不同海域垃圾密度差异大，能调阈值是实际需要，见下方说明）。
> 但队友若为了省一次 NMS 改成 `nms=true`，服务也必须能接住 —— 否则
> 一换模型就当场 500。
>
> **这个缺陷曾经真实存在**：早期 `_postprocess` 只认第一种格式，
> 遇到已烘 NMS 的输出会拿 6 个元素当 `4+nc` 拆，类别分数取空后
> `np.argmax` 抛 `ValueError`。因为一直没有真实模型（走 stub 模式），
> 缺陷潜伏了很久 —— 直到补了 `TestPostprocessNmsBakedFormat` 一组测试
> 用构造张量才把它钉出来。
>
> **教训**：stub 模式让三组能并行开发，代价是**真实推理路径不被覆盖**。
> 所以这条路径必须靠「构造符合真实形状的张量」来测，
> 不能指望端到端测试 —— 端到端跑的是 stub。

### 3.5 输出框必须裁剪到图像范围内

letterbox 还原后，边缘目标的框可能落到负数或超出原图尺寸
（模型对 padding 区域的预测）。而契约（`mqtt-topics.md`）要求
`bbox` 是「设备原始分辨率下的像素坐标，原点左上」，前端按
`bbox / 分辨率` 换算百分比定位 —— 越界值会让框跑到容器外面。

所以还原后统一 `clip` 到 `[0, w] × [0, h]`，并丢弃裁剪后
面积不足 1 像素的退化框（那不是有效目标）。

### 3.6 后端优先级

```
TensorrtExecutionProvider  →  Jetson / 有 NVIDIA GPU 的边缘盒
CUDAExecutionProvider      →  有 NVIDIA GPU 的服务器
CPUExecutionProvider       →  x86 兜底
```

RK3588 走 RKNN，需用 `airockchip/ultralytics_yolov8` 分支导出（见 `deployment.md` 4.2）。

---

## 4. 数据平台（后端）

### 4.1 分层

```
app/
├── api/v1/          接口层 —— 只做参数校验与编排
├── core/            配置、依赖注入、异常、安全
├── db/              会话与基类
├── models/          SQLAlchemy ORM（六张核心表）
├── schemas/         Pydantic 出入参（API 契约）
├── repositories/    数据访问（含 PostGIS 空间查询）
├── services/        业务逻辑（事件服务、派单引擎、消费者）
├── mqtt/            MQTT 客户端与消息处理器
├── ws/              WebSocket 连接管理
└── main.py          应用入口与生命周期
```

**各层的硬规则**：

| 层 | 允许 | 禁止 |
| --- | --- | --- |
| `api/v1/` | 参数校验、编排、schema 转换 | 写 SQL、写业务分支 |
| `services/` | 业务逻辑、状态机 | 直接写 SQL |
| `repositories/` | 查询构造、空间查询 | 业务判断 |
| `models/` | 表结构、常量类 | 任何逻辑 |
| `mqtt/` | 报文解析、转发到 service | 持有请求级 session |

**为什么这么严**：派单逻辑有三个调用入口——MQTT 上行、HTTP 备用通道、定时补派。逻辑一旦写进接口层，必然出现三份有细微差异的实现。

### 4.2 事件流处理

```
MQTT 上行
   │
   ▼
handle_event()                    ── mqtt/handlers.py
   │  ① Pydantic 校验（字段缺失直接丢弃 + 记日志）
   │  ② EventService.ingest()
   │        ├─ 幂等判重 (device_id, seq)
   │        ├─ 校验设备已注册
   │        ├─ 落库 t_event (status=new)
   │        └─ enqueue_dispatch=True 时才投 Redis Stream
   │           （当前两个入口都同步派单，传 False）
   │  ③ WebSocket 广播 new_event（大屏标红）
   │  ④ 高优先级类别 → _try_dispatch()
   ▼
同步派单路径：_try_dispatch → DispatchEngine.dispatch_for_event()
异步/显式入队路径：Redis Stream: stream:events → dispatch_consumer()
   │
DispatchEngine.dispatch_for_event()
```

### 4.3 ★ 派单引擎：五步筛选

```
① 防重复派单   该 event 是否已有在途任务？（含部分唯一索引兜底）
       │ 无
② 防抖合并     200m 内、10 分钟窗口内是否有活跃任务？
       │           有 → 合并（机器人不必反复跑同一片水域）
       │ 无
③ 可用性过滤   在线 + 电量 ≥30% + 三仓总占用 <80%
       │
④ KNN 就近     ST_DWithin 粗筛（走 GiST）+ <-> 算子排序（走 GiST）
       │
⑤ 类别加权     泡沫类任务顺路优先（减少空驶）
       │
⑥ 创建任务     状态直接 assigned，记录 assigned_at
       │
⑦ MQTT 下发    robot/{robot_id}/task
       │
⑧ 等 ACK       15 秒超时 → 状态回退 pending，换下一台
```

#### 第 ② 步为什么是必需的

没有防抖时：同一片水域 3 分钟内报 10 条事件 → 派 10 趟 → 机器人来回跑同一段航路。这是演示时最容易被看出来的"蠢"，也是运营方会立刻质疑的点。

实现：`TaskRepository.find_mergeable_task()` 用 `ST_DWithin` 找 200m 内、时间窗内的活跃任务，找到就把新事件并入。

#### 第 ④ 步的 SQL

```sql
SELECT r.*, ST_Distance(r.location::geography, e.location::geography) AS dist_m
FROM t_device r
WHERE r.device_type = 'robot'
  AND r.status = 'online'
  AND ST_DWithin(r.location::geography, e.location::geography, 3000)  -- 米制粗筛
ORDER BY r.location <-> e.location                                     -- KNN 算子
LIMIT 5;
```

**为什么必须这么写**：

| 写法 | 索引使用 | 复杂度 |
| --- | --- | --- |
| `ORDER BY ST_Distance(...)` | ❌ 不走索引 | 全表扫描 + 每行算距离 |
| `ST_DWithin(...) + <->` | ✅ 走 GiST | 索引范围扫描 |

`speed` 上：100 台机器人规模下，前者约几十毫秒，后者亚毫秒。规模再大差距会放大。

### 4.4 ★ 任务状态机

```
pending ──► assigned ──► navigating ──► collecting ──► done
   │            │              │              │
   │            └──► pending   │              │
   │           （ACK 超时回退）  │              │
   └────────────┴──────────────┴──────────────┴──► cancelled
```

```python
class TaskStatus:
    TRANSITIONS = {
        PENDING:    {ASSIGNED, CANCELLED},
        ASSIGNED:   {NAVIGATING, PENDING, CANCELLED},   # 回退：ACK 超时重派
        NAVIGATING: {COLLECTING, CANCELLED},
        COLLECTING: {DONE, CANCELLED},
        DONE:       set(),                              # 终态
        CANCELLED:  set(),                              # 终态
    }
```

**唯一合法入口**是 `DispatchEngine.transition()`：

```python
async def transition(self, task, target_status, **kwargs):
    if not TaskStatus.can_transition(task.status, target_status):
        raise InvalidStateTransitionError(task.status, target_status)
    ...
    # 时间戳链
    if target_status == ASSIGNED and task.assigned_at is None:
        task.assigned_at = now
    elif target_status == NAVIGATING:
        task.ack_at = task.ack_at or now
    elif target_status == COLLECTING:
        task.started_at = task.started_at or now
    elif target_status == DONE:
        task.finished_at = now
        # 同步事件状态
        event.status = EventStatus.RESOLVED
```

**★ 禁止在别处直接改 `task.status`。**

**为什么要状态机**：工单流转是最容易出脏数据的地方。没有状态机时，"已完成"的任务可能被误改回"作业中"，`finished_at` 被反复覆盖，报表统计全乱。状态机把非法流转挡在写入之前，报错清晰（`code=4002`，附当前态与目标态）。

### 4.5 幂等设计

**QoS1 是"至少一次"，重复投递是正常行为。**

```python
# 应用层预查（快速返回，避免触发约束异常）
if await self.events.exists_by_device_seq(payload.device_id, payload.seq):
    return EventIngestResult(duplicate=True, ...)

# 数据库层兜底（并发下预查会漏）
CONSTRAINT uq_event_device_seq UNIQUE (device_id, seq)
```

还有一层防重复派单——**部分唯一索引**：

```sql
CREATE UNIQUE INDEX uq_task_active_event
    ON t_task (event_id)
    WHERE status NOT IN ('done', 'cancelled') AND event_id IS NOT NULL;
```

它保证「同一事件的未完成任务最多一条」，是并发场景下的最后一道防线。

### 4.6 WebSocket 实时推送

```python
class ConnectionManager:
    async def broadcast(self, message_type: str, data: dict):
        # push_new_event / push_task_update / push_robot_status
```

单进程内存广播。多实例部署时需换成 Redis Pub/Sub，替换 `broadcast` 实现即可。

**兜底策略**：WebSocket 只推增量，丢了不会自愈。所以前端保留 30 秒全量轮询对账——**WS 负责快，轮询负责对**。

### 4.7 统一响应与错误码

```json
{ "code": 0, "message": "ok", "data": {}, "trace_id": "a3f2c9d1e8b74f02" }
```

| 段 | 范围 | 含义 |
| --- | --- | --- |
| 1xxx | 1000–1005 | 通用 |
| 2xxx | 2001–2003 | 设备 |
| 3xxx | 3001–3002 | 事件 |
| 4xxx | 4001–4004 | 任务 |
| 5xxx | 5001–5002 | AI |

**HTTP 状态码统一 200**，业务错误看 `code`。理由：前端只需处理一个维度；只有系统级错误（422 参数校验、500 未捕获异常）才用非 200。

### 4.8 定时任务

| 任务 | 间隔 | 作用 |
| --- | --- | --- |
| `dispatch_consumer` | 常驻 | 消费 Redis Stream 执行派单 |
| `pending_dispatcher` | 30 秒 | 补派积压事件（机器人离线期间产生的） |
| 日报表聚合 | 每日凌晨 | 写 `t_report_daily` 宽表（备赛期可手工触发） |

---

## 5. 前端可视化

### 5.1 技术栈选择

| 需求 | 选型 | 理由 |
| --- | --- | --- |
| 框架 | Vue 3 + Vite | 组合式 API，政务项目生态成熟，上手快 |
| 状态 | Pinia | 官方推荐，TypeScript 友好 |
| 图表 | ECharts | 地图、热力图、折线一站式 |
| 视频 | **mpegts.js** | HTTP-FLV 低延迟；**flv.js 已停维护** |
| 地图 | 腾讯地图 GL JS | 国内合规持牌；政务项目常用 |
| 请求 | axios | 拦截器统一拆响应信封 |

### 5.2 页面结构

| 路由 | 页面 | 核心内容 |
| --- | --- | --- |
| `/` | 监测大屏 | 指标卡 + 视频宫格 + 地图 + 实时告警流 + 趋势/类别/机器人状态 |
| `/events` | 事件明细 | 筛选栏 + 明细表 + 分页 + 事件↔工单关联 |
| `/tasks` | 工单看板 | 五列看板 + 状态统计条 + 详情抽屉（生命周期时间戳链） |
| `/devices` | 设备管理 | 设备清单 + 地图 + 详情（三仓电量 / 内嵌视频） |
| `/reports` | 治理报表 | 汇总指标 + 乡镇对比 + 类别分布 + 量化明细 |

### 5.3 视频播放

```js
mpegts.createPlayer({
  type: 'flv',
  isLive: true,
  url: flvUrl,
}, {
  enableWorker: true,
  liveBufferLatencyChasing: true,   // 追帧，防止延迟累积
  liveBufferLatencyMaxLatency: 3.0,
  liveBufferLatencyMinRemain: 0.5,
})
```

> **为什么不用 flv.js**：该项目已停止维护。在长时间直播流上会出现延迟持续累积——跑几小时后延迟到几十秒，且无修复。mpegts.js 是活跃维护的继任实现。

**检测框叠加**：按 `bbox` 与设备分辨率（1920×1080）归一为百分比定位，所以 bbox 必须基于设备原始分辨率。

### 5.4 地图合规

**必须用国内持牌地图服务商**：腾讯 / 高德 / 百度 / 天地图。

腾讯地图走 `_TMapSecurityConfig` 代理模式：

```js
window._TMapSecurityConfig = {
  // 授权通过后端代理完成
}
// ★ SDK URL 不带 key
script.src = 'https://map.qq.com/api/gljs?v=1.exp&libraries=service'
// ★ 不传 mapStyleId
```

**安全检查**：源码中搜索不到任何地图密钥（`*_key`、`appkey`、`ak=` 等模式）。这是交付前的必检项。

**加载失败兜底**：地图 SDK 加载失败时降级为列表视图 + 内联 SVG 图标，不白屏。

### 5.5 实时数据

```js
// 收敛在 Pinia store 里
const { connected } = useRealtime({
  onMessage: (msg) => {
    if (msg.type === 'new_event') store.pushEvent(msg.data)
    if (msg.type === 'task_update') store.pushTask(msg.data)
    if (msg.type === 'robot_status') store.patchRobot(msg.data)
  }
})
```

重连策略：指数退避（1s → 30s 封顶），25 秒心跳，`onUnmounted` 自动关闭。

**`pushEvent` 里做了两件事**：
1. 去重（同 `event_id` 不重复插入）
2. 2 秒后取消高亮（避免长时间占用动画资源）

### 5.6 必须注意的工程细节

| 细节 | 后果 |
| --- | --- |
| `chart.setOption(option, true)` 第二参数 | 不加 → 切换筛选时旧数据残留 |
| `onUnmounted` 里 `chart.dispose()` | 不加 → 切换路由几次内存泄漏 |
| `mpegts.destroy()` | 不加 → 视频元素累积，页面越来越卡 |
| resize 防抖（150ms） | 不加 → 拖窗口时 ECharts 卡死 |
| WebSocket 在 `onUnmounted` 关闭 | 不加 → 旧组件回调被调用，报错刷屏 |

---

## 6. 数据模型

### 6.1 六张核心表

```
t_device ──┐
           │ device_id
           ▼
      t_event ★ ──── event_id ────► t_task ★
                                     │
                                     ├──► t_track（按日分区）
                                     └──► t_report_daily（预聚合宽表）

t_user（独立）
```

### 6.2 表设计要点

#### `t_event` — 事件表（★核心表）

```sql
event_id        VARCHAR(64) UNIQUE     -- 业务编号
device_id       VARCHAR(64)
event_time      TIMESTAMPTZ            -- 事件时间（设备时间）
location        geometry(Point, 4326)  -- 空间索引
main_class      waste_class_enum
det_count       SMALLINT
max_confidence  NUMERIC(5,4)
evidence_url    TEXT                   -- MinIO 地址
model_version   VARCHAR(32)            -- 可追溯到具体模型
seq             BIGINT                 -- 幂等序号
status          event_status_enum

CONSTRAINT uq_event_device_seq UNIQUE (device_id, seq)   -- ★幂等保证
CREATE INDEX ... USING GIST (location)                   -- ★热力图
CREATE INDEX ... (event_time DESC, main_class)           -- ★复合索引
```

**`model_version` 字段的价值**：它让"某段时间误报率突然升高"能被反查——是新模型上线导致的吗？没有这个字段，模型迭代就是盲改。

#### `t_task` — 工单表（★核心表）

**时间戳链**是设计亮点：

```sql
created_at      -- 创建
assigned_at     -- 派单
ack_at          -- 机器人确认
started_at      -- 开始作业
finished_at     -- 作业完成
```

五个时间戳串起来，就能算出：**派单延迟**（assigned-created）、**响应延迟**（ack-assigned）、**航行耗时**（started-ack）、**作业耗时**（finished-started）。

这些指标是运营优化的依据——如果航行耗时远大于作业耗时，说明机器人布置位置不合理。

`review_result`（`confirmed` / `not_found` / `recheck`）让系统能反算**误报率**。

#### `t_track` — 轨迹表

按日 RANGE 分区。理由：机器人每 5 秒回传一个点，3 台机器人一年约 1900 万点，不分区会让索引膨胀、查询变慢。

```sql
) PARTITION BY RANGE (recorded_at);
-- 建表时自动创建 当日-1 ~ 当日+7 共 9 个分区
```

#### `t_report_daily` — 预聚合宽表

```sql
UNIQUE (stat_date, township, main_class)
```

报表页只读这张表 → 事件量到百万级仍毫秒响应。**用存储换查询速度**——宽表的行数是 `天数 × 乡镇数 × 类别数`，一年也就几万行。

#### 枚举类型

```sql
waste_class_enum:    foam / plastic / fishing_gear / other
event_status_enum:   new / dispatched / resolved / ignored
task_status_enum:    pending / assigned / navigating / collecting / done / cancelled
review_result_enum:  confirmed / not_found / recheck / pending
user_role_enum:      admin / operator / approver / viewer
```

> **类别顺序不可改**。`foam/plastic/fishing_gear/other` 的顺序即模型输出的类别索引，改了要重训模型。所以 `WasteClass` 常量类与 `seasight.yaml` 的 `names` 映射必须严格一致。

### 6.3 数据保留策略

| 表 | 保留 | 处理 |
| --- | --- | --- |
| `t_event` | 永久 | 年约 29 万条，可忽略 |
| `t_task` | 永久 | 治理凭证，不可删 |
| `t_track` | 90 天 | 超期降采样（1 点/分钟）后归档 |
| 证据帧 | 180 天 | MinIO 生命周期规则自动过期 |
| `t_report_daily` | 永久 | 体积极小 |

### 6.4 ER 关系

```
       ┌──────────────┐
       │  t_device    │
       │  device_id PK│
       └──────┬───────┘
              │ 1:N (device_id)
              ▼
       ┌──────────────┐              ┌──────────────┐
       │  t_event  ★  │  event_id    │   t_task  ★   │
       │  event_id PK │◄────────────►│  task_id PK   │
       │  UNIQUE(dev, │  1:N (可空)  │  robot_id FK  │
       │        seq)  │              │  status (FSM) │
       └──────────────┘              └──────┬───────┘
                                            │ 1:N
                                            ▼
                                     ┌──────────────┐
                                     │  t_track     │
                                     │  (按日分区)   │
                                     └──────────────┘

       ┌──────────────┐
       │  t_report_   │  ← 由定时任务从 t_event + t_task 聚合生成
       │   daily      │     报表页只读此表
       └──────────────┘
```

---

## 7. 关键技术难题与解法

### 7.1 难题一：海面误报压制

| 项 | 内容 |
| --- | --- |
| **表现** | 浪花与阳光反光在单帧上与 EPS 泡沫极相似，不做过滤时每分钟上百条误报 |
| **错误解法** | 提高置信度阈值（会同时压掉真实小目标）；按像素格网统计（检测框抖动导致永远凑不满命中，实测 0 确认事件） |
| **正确解法** | **目标级跟踪 + 三重门限关联**（类别 + 中心位移 + IoU），miss 衰减淘汰 |
| **实测** | 300 条检测 → 1 个确认事件（单元测试）；模拟器 5772 条检测 → 52 个确认事件，抑制率 42.5% |

### 7.2 难题二：QoS1 重复投递导致重复派单

| 项 | 内容 |
| --- | --- |
| **表现** | MQTT QoS1 是"至少一次"，重传会让同一事件入库多次、派多趟单 |
| **解法** | 三层防护：① `(device_id, seq)` 唯一约束；② 应用层预查快速返回；③ `t_task` 上的部分唯一索引防同事件重复派单 |
| **配套** | 边缘端 seq 必须单调递增 + **持久化**（重启后续上，否则归零会被判重吞掉） |

### 7.3 难题三：视频转发挤占推理算力

| 项 | 内容 |
| --- | --- |
| **表现** | 若让边缘盒转流给前端，一路 4Mbps 转发持续占用 CPU，推理帧率掉下去 |
| **解法** | **视频流旁路**：go2rtc 直接拉 RTSP 转发给前端；边缘盒独立拉一路做推理。两条链路互不干扰 |
| **前提** | 摄像头需支持多路并发拉流（主流型号支持 4~8 路） |

### 7.4 难题四：空间查询性能

| 项 | 内容 |
| --- | --- |
| **表现** | `ORDER BY ST_Distance(...)` 不走索引，全表扫描 + 每行算距离 |
| **解法** | `ST_DWithin(geography, geography, 米)` 粗筛（走 GiST）+ `<->` KNN 算子排序（走 GiST） |
| **热力图特有坑** | `ST_SnapToGrid` 必须先投影到 3857（米制）；返回**密度**而非总数，否则大网格天然更热 |

### 7.5 难题五：任务状态脏数据

| 项 | 内容 |
| --- | --- |
| **表现** | 直接 `UPDATE status` 会让"已完成"被改回"作业中"，时间戳被覆盖，报表统计全乱 |
| **解法** | `TaskStatus.TRANSITIONS` 显式迁移表 + `DispatchEngine.transition()` 唯一入口 + 非法跳转抛错（`code=4002`） |

### 7.6 难题六：ACK 丢失导致工单永久卡死

| 项 | 内容 |
| --- | --- |
| **表现** | QoS1 只保证"送达 broker"，不保证"设备收到并处理"。机器人离线时派单石沉大海，工单卡在 `assigned` 永不推进 |
| **解法** | 应用层 ACK（`robot/{id}/cmd/ack`）+ 15 秒超时回退 `pending` 换车重派 |

### 7.7 难题七：模型训练集小且场景特殊

| 项 | 内容 |
| --- | --- |
| **表现** | 备赛期数据集仅数百张，通用增强策略会引入错误特征 |
| **解法** | ① 用 **YOLO11s**（非 n，n 在海上小目标召回不足）；② **两阶段训练**（先 freeze=10 训 head，再全量微调）；③ 加 200~500 张**纯背景负样本**；④ **强制 `flipud=0`**——水面倒影会让模型学到"垃圾长在天空上"；⑤ `mixup=0`——垃圾与海面混合产生不真实纹理；⑥ `close_mosaic=15` 让模型最后适应真实分布 |

### 7.8 难题八：浏览器播放长时直播流延迟累积

| 项 | 内容 |
| --- | --- |
| **表现** | flv.js 已停维护，长时间播放延迟累积到几十秒 |
| **解法** | 改用 **mpegts.js** + `isLive: true` + `liveBufferLatencyChasing: true` 自动追帧 |

---

## 8. 工程化与可靠性

### 8.1 降级设计（关键理念：**任何单一依赖缺失都不阻断启动**）

| 缺失组件 | 降级行为 | 服务能否启动 |
| --- | --- | --- |
| Redis | 队列失效，`pending_dispatcher` 定时扫表补派 | ✅ |
| MQTT Broker | 设备通道不可用，可用 HTTP `/events` 备用通道 | ✅ |
| MinIO | 证据帧不上传 | ✅ |
| 模型文件 | AI 服务进 stub 模式，返回确定性模拟结果 | ✅ |
| passlib/bcrypt | 登录降级为演示账号比对 | ✅ |

**为什么这很重要**：演示现场一个组件挂掉就全盘皆输，是最不可接受的风险。宁可功能降级，也要保证"能演示"。

### 8.2 容错矩阵

| 失效点 | 兜底机制 |
| --- | --- |
| MQTT 断连 | 边缘端本地队列（1000 条），恢复后按 seq 顺序补传 |
| 边缘盒崩溃 | seq 持久化到本地文件，重启续上 |
| MQTT 重复投递 | 唯一约束 + 预查 + 部分唯一索引 |
| Redis 不可用 | 事件仍入库；定时补派 |
| 机器人 ACK 超时 | 15 秒回退 pending 重派 |
| 无可用机器人 | 事件保持 new，机器人上线后补派 |
| 消费处理异常 | 不 ACK（留 pending list）或进死信 stream |
| 数据库断连 | `pool_pre_ping` + `pool_recycle=3600` |
| 前端丢消息 | 30 秒全量轮询对账 |
| 地图 SDK 失败 | 内联 SVG 兜底，不白屏 |

### 8.3 可观测性

| 手段 | 位置 |
| --- | --- |
| 结构化日志（loguru） | 全部服务；关键路径带模块前缀 `[派单]` `[状态机]` `[事件]` |
| trace_id | 每请求注入，响应头返回 `X-Trace-Id` |
| 健康检查 | `GET /health`（含 WS 连接数）、`GET /infer/health` |
| 死信队列 | `stream:dead_letter`，存处理失败的消息 |
| 模拟器统计 | 运行结束打印确认数/抑制率/重复数 |

### 8.4 安全

| 项 | 措施 |
| --- | --- |
| 传输 | 生产全站 HTTPS/WSS；MQTT over TLS 8883 |
| 密码 | bcrypt cost=12，不存明文 |
| 令牌 | HMAC-SHA256 签名载荷，含 `exp` |
| 权限 | admin / operator（乡镇辖区）/ approver（仅审批）/ viewer（只读） |
| 设备隔离 | EMQX ACL 按 clientid 前缀授权 |
| 前端隔离 | **前端绝不直连 MQTT**，只连后端 WebSocket |
| 敏感配置 | 全走环境变量，`.env` 在 `.gitignore` 排除 |
| 数据合规 | 边缘侧即脱敏，不存原始视频，只存垃圾证据帧 |

### 8.5 测试

| 层 | 位置 | 覆盖 |
| --- | --- | --- |
| 单元 | `edge/simulator/test_temporal.py` | 时序校验 7 项（最有价值的测试组） |
| 单元 | `backend/tests/` | 状态机、NMS、schema 校验、**MQTT 路由与状态映射（76 项）**、**WS 推送负载契约（18 项）** |
| 静态 | `scripts/check_contract_drift.py` | 文档声明集合 ↔ 代码实现集合穷举比对 |
| 自证 | `scripts/selftest_contract_drift.py` | 注入 12 种历史缺陷，验证上述**脚本型与测试型**守卫真的会红 |
| 冒烟 | `scripts/smoke_test.py` | 端到端七步（含幂等验证）—— **只走 HTTP，不含 MQTT 与 WS 推送** |
| 人工 | 浏览器 | 大屏、地图、视频、看板 |

---

## 9. 部署与运维

详见 `deployment.md`。要点：

| 项 | 方案 |
| --- | --- |
| 一键起全套 | `docker compose up -d`（postgres / redis / emqx / minio / go2rtc / backend / ai-service / frontend） |
| 无 Docker | 裸机模式，托管 venv + 手动起 PostgreSQL/Redis |
| 前端托管 | Nginx + SPA 路由回退 + WebSocket 升级头 + `proxy_read_timeout 3600s` |
| 边缘盒 | systemd 服务，`Restart=always` |
| 数据库运维 | 分区滚动创建（或 pg_partman）、每日 pg_dump、索引使用率巡检 |
| 上线检查 | 15 项 checklist（密钥、HTTPS、备份演练、地图合规等） |

---

## 10. 演进路线

### 10.1 一期（备赛期，当前）

- 单机部署，三台机器人，六个摄像头点位
- 规则派单（距离 + 电量 + 仓容 + 类别加权）
- 固定网格热力图
- 人工复核

### 10.2 二期（赛后 3~6 个月）

| 方向 | 内容 |
| --- | --- |
| 派单智能化 | 引入潮汐/风向数据，预测垃圾漂移趋势，**提前布置**而非事后追打 |
| 热力图升级 | 网格按数据密度自适应（高密度区域细分网格） |
| 多机器人协同 | 多台机器人分区并行 + 动态任务再分配 |
| 误报自学习 | 用 `review_result=not_found` 的样本自动回灌训练集，形成闭环迭代 |
| 移动端 | 乡镇操作员小程序（接单、拍照上报、进度查看） |

### 10.3 三期（1~2 年）

| 方向 | 内容 |
| --- | --- |
| 溯源分析 | 结合洋流模型与事件时空分布，反推垃圾来源区域 |
| 多模态 | 水面 + 空中（无人机）+ 卫星遥感数据融合 |
| 跨区域协同 | 多县市数据互通，共享垃圾漂移模型 |
| 硬件升级 | 机器人自主靠岸、自动充电、自动倾卸，实现零人工干预 |

### 10.4 技术债与已知限制

诚实记录当前方案的不足：

| 限制 | 影响 | 缓解 |
| --- | --- | --- |
| WebSocket 单进程内存广播 | 无法水平扩展 | 多实例时换 Redis Pub/Sub（接口已预留） |
| 派单未考虑实时海况 | 大风浪天派单可能失败 | 二期引入气象数据 |
| 数据集规模小 | 模型泛化能力有限 | 持续采集 + 负样本强化 + 两阶段训练 |
| 未做视频流录制 | 无法事后回看完整过程 | 有需求时加 go2rtc 录制开关 |
| 日报表聚合依赖定时任务 | 任务失败会导致报表滞后 | 备赛期提供手工触发接口 |
| 令牌未做刷新机制 | 24 小时后需重新登录 | 备赛期可接受；生产接入正式 SSO |

---

## 11. 工作量拆分

### 11.1 平台组

| 模块 | 文件 | 状态 |
| --- | --- | --- |
| 工程骨架 | `backend/app/{main.py,core/,db/}` | ✅ |
| ORM 模型 | `models/{device,event,task,misc}.py` | ✅ |
| Pydantic 契约 | `schemas/__init__.py` | ✅ |
| 数据访问层 | `repositories/__init__.py` | ✅ |
| 事件服务 | `services/event.py` | ✅ |
| 派单引擎 | `services/dispatch.py` | ✅ |
| 后台消费者 | `services/consumer.py` | ✅ |
| MQTT 层 | `mqtt/{client,handlers,topics}.py` | ✅ |
| WebSocket | `ws/manager.py` | ✅ |
| HTTP 接口 | `api/v1/`（8 个路由模块，22 个端点） | ✅ |
| 数据库脚本 | `db/init/{01_schema,02_seed}.sql` | ✅ |
| 数据库迁移 | `backend/{alembic.ini,alembic/}`（双轨制，见 ADR-007/008） | ✅ |
| 容器编排 | `docker-compose.yml`、`Dockerfile*` | ✅ |
| 部署配置 | `deploy/{emqx,go2rtc,nginx}/` | ✅ |
| 前端全部 | `frontend/src/`（20 个文件） | ✅ |
| 文档 | `docs/` 七份 + `README.md` | ✅ |
| 端到端冒烟 | `scripts/smoke_test.py` | ✅ |
| 单元/契约测试 | `backend/tests/`（217 项，含 `test_mqtt_layer.py` 76 项、`test_ws_contract.py` 18 项）、`edge/simulator/test_temporal.py`（11 项） | ✅ |
| 接口一致性校验 | `scripts/check_api_contract.py` | ✅ |
| 仓库卫生检查 | `scripts/check_gitignore.py` | ✅ |
| 契约漂移检查 | `scripts/check_contract_drift.py`（MQTT 主题树、状态映射、类别枚举，见 ADR-014） | ✅ |
| 检查脚本自证 | `scripts/selftest_contract_drift.py`（注入 12 种历史缺陷，覆盖脚本型与测试型守卫） | ✅ |
| WS 推送负载契约 | `ws/manager.py`（`WS_MESSAGE_CONTRACT`）+ `tests/test_ws_contract.py`（AST 穷举扫描，见 ADR-016） | ✅ |
| 数据集体检 | `ml/scripts/check_dataset.py`（支持 `--write-stats` 回填统计） | ✅ |
| 推理后处理双格式解析 | `services/ai/server.py`（`_postprocess` + `_letterbox`，见 §3.3–3.5） | ✅ |
| 参数真源守卫 | `test_temporal.py`（代码默认值 ↔ 设计文档参数表 ↔ config.yaml） | ✅ |

**验证层次**（详见 `README.md` 第四节）：

| 命令 | 层 | 依赖基础设施 |
| --- | --- | --- |
| `make test-edge` | 算法逻辑 + 参数真源 | 否 |
| `make test` | 仓库全量：后端契约 + MQTT 层 + WS 负载 + 上报响应 + 派单引擎 + 静态守卫 + edge/ml | 否 |
| `make check` | 静态检查（接口一致性 + 仓库卫生 + 契约漂移） | 否 |
| `make check-contract-selftest` | 守卫自证（注入 12 种 MQTT/WS 缺陷验证） | 否 |
| `make check-events-selftest` | 守卫自证（注入 4 种上报契约缺陷验证） | 否 |
| `make check-dispatch-selftest` | 守卫自证（注入 5 种派单引擎缺陷验证） | 否 |
| `make check-finalize-selftest` | 守卫自证（注入 7 种派单收尾缺陷验证） | 否 |
| `make check-pel-selftest` | 守卫自证（注入 4 种 PEL 回收缺陷验证） | 否 |
| `make check-data` | 训练数据完整性 | 否 |
| `make smoke` | 端到端链路（**只打 HTTP，不经过 MQTT 与 WS 推送**） | **是** |

> ⚠️ **覆盖盲区（一）真实推理路径**：`make smoke` 与 `make test` 走的都是
> `stub` 推理，真实 ONNX 路径（letterbox、两种输出格式、越界框裁剪）
> **只有靠构造张量的单元测试覆盖**。改动 `services/ai/server.py` 后
> 务必跑 `pytest tests/test_ai_postprocess.py`，端到端测试抓不到那里。
>
> ⚠️ **覆盖盲区（二）MQTT 协议层**：`make smoke` 只打 HTTP 接口，
> **完全不经过 MQTT**。因此 MQTT 路由与状态映射必须靠
> `tests/test_mqtt_layer.py` 覆盖 —— 该层缺陷不抛异常、不打日志，
> 端到端测试也看不见（HTTP 路径是好的）。改动
> `mqtt/{client,handlers,topics}.py` 后务必跑
> `pytest tests/test_mqtt_layer.py` 与 `make check-contract`。
>
> ⚠️ **覆盖盲区（三）WS 推送负载**：`make smoke` 也不走 WebSocket。
> 推送 data 是手工拼的 dict，`api/v1/tasks.py` 推完整 `TaskOut`，
> 而 `handlers.py` 推手工裁剪的子集 —— 形态不同，subset 漏字段时
> 前端静默拿到 `undefined`。改动任何 `ws_manager.push_*` 调用点后
> 务必跑 `pytest tests/test_ws_contract.py`（AST 穷举扫描）。
>
> ⚠️ **覆盖盲区（四）上报响应契约**：`make smoke` 打的是 HTTP `/events`，
> 但它只断言「收到 200」，**不断言响应体里 `task_created`/`task_id` 是否说了真话**。
> 该接口曾用「查全库最新一条任务」冒充「本次派单结果」，
> 于是无可用机器人时也报 `task_created=true`，且 `task_id` 指向别人的任务。
> 改动 `api/v1/events.py` 的 `ingest_event` 或 `_try_dispatch` 返回值后
> 务必跑 `pytest tests/test_events_ingest_contract.py`。
>
> ⚠️ **覆盖盲区（五）派单引擎的「优化类」逻辑**：`make smoke` 只验证
> 「事件能不能派出去」，**不验证派得好不好**。而派单引擎的第五步
> 类别匹配加权曾整段失效——函数收了 `task` 形参却从不使用，
> 加权退化成只看事件类别的常量，文档承诺的「同类别顺路复用」
> 从未生效。**系统照样跑、日志照样打 INFO，只是"没那么聪明"**。
> 改动 `services/dispatch.py` 后务必跑
> `pytest tests/test_dispatch_engine.py`。
>
> ⚠️ **覆盖盲区（六）守卫自身**：`check_contract_drift.py` 依赖文档格式
> （表格结构、反引号、箭头符号）；`test_ws_contract.py`、
> `test_events_ingest_contract.py`、`test_static_guards.py`
> 依赖 AST 扫描能找到目标。任一失效都会导致**零问题通过**。
> 怀疑守卫本身有问题时先跑 `make check-contract-selftest`、
> `make check-events-selftest` 与 `make check-dispatch-selftest`。
>
> ⚠️ **覆盖盲区（七）「实现了但没接线」**：函数存在于源码里、grep 得到、
> 单看实现也正确，但**没有任何调用点**。这类缺陷连"扫一遍调用点"的工具
> 都抓不到——必须显式断言「该方法在定义处之外至少有一个调用点」。
> 本工程实测抓到 `handle_ack_timeout`（文档「五步筛选」的第五步）零调用，
> 于是机器人掉线后任务永久停在 `assigned`，且因为**没有代码在跑**，
> 连一条日志都不会有。对应守卫
> `tests/test_dispatch_finalize.py::TestAckTimeoutIsWired`。
>
> ⚠️ **覆盖盲区（八）Redis PEL 看不见**：`xreadgroup(">")` 永远读不到
> PEL 条目，而处理失败的消息又刻意不 ACK —— 两者叠加会让消息
> **永久滞留且 PEL 无限增长**（不受 `maxlen` 裁剪影响）。
> 功能上被定时补派从数据库兜住，所以外表完全正常，
> 只能靠 `XPENDING` 才看得见。必须「不 ACK 就有人认领」
> （`xautoclaim`，见 ADR-020），守卫在 `tests/test_consumer_pel.py`。

### 11.2 算法组

| 模块 | 文件 | 状态 |
| --- | --- | --- |
| 数据集配置 | `ml/configs/seasight.yaml` | ✅ |
| 超参配置 | `ml/configs/train_args.yaml` | ✅ |
| 两阶段训练脚本 | `ml/scripts/train_two_stage.py` | ✅ |
| ONNX 导出脚本 | `ml/scripts/export_onnx.py` | ✅ |
| 边缘模拟器 | `edge/simulator/`（配置 + 模拟器 + 测试） | ✅ |
| 时序校验单元测试 | `edge/simulator/test_temporal.py`（11 项，含参数真源守卫） | ✅ |
| **真实数据集采集标注** | `ml/datasets/`（目录骨架 + 放置说明已就位） | ⏳ 待做 |
| **baseline 训练** | `ml/scripts/train_two_stage.py`（脚本已就绪，待数据） | ⏳ 待做 |
| **真实边缘推理程序** | `edge/main.py` | ⏳ 待做 |

### 11.3 硬件组

| 模块 | 状态 |
| --- | --- |
| 船体结构设计 | ⏳ |
| 三仓分拣机构 | ⏳ |
| 电气与控制方案 | ⏳ |
| 机器人侧 MQTT 接入 | ⏳（报文契约已冻结在 `mqtt-topics.md`） |

### 11.4 关键路径

```
数据集采集标注（算法组，W1-W2）──┐
                                  ├──► baseline 训练（W2-W3）──► ONNX 导出 ──► 边缘部署
边缘盒硬件到位（硬件组，W2）─────┘

接口冻结（三组，W1）──► 平台事件流打通（W2）──► 派单闭环（W3）──► 联调录屏（W4）
```

**最长关键路径是"数据集采集标注"**。它不受代码进度影响，且不可压缩——所以 W1 就要启动，不能等平台做完。

---

## 附：技术选型总表

| 层 | 选型 | 备选 | 选择理由 |
| --- | --- | --- | --- |
| 后端框架 | FastAPI | Django / Flask | 异步原生、自带 OpenAPI、与算法同语言 |
| 主数据库 | PostgreSQL 14 + PostGIS | MySQL + 自算距离 | 空间索引与距离算子是一等公民 |
| 缓存/队列 | Redis 7 (Streams) | Kafka / RabbitMQ | 消费者组 + ACK + pending list；零新增组件 |
| 消息总线 | EMQX 5 | Mosquitto | 规则引擎、Dashboard、ACL 完善 |
| 对象存储 | MinIO | 本地磁盘 / 阿里云 OSS | S3 兼容，迁云成本近零 |
| 流媒体 | go2rtc | ZLMediaKit / MediaMTX | 单文件零依赖，上线 5 分钟 |
| 视频播放 | mpegts.js | flv.js | flv.js 已停维护，长时播放延迟累积 |
| 前端框架 | Vue 3 + Vite | React | 国内政务项目生态成熟，上手快 |
| 图表 | ECharts | Chart.js / D3 | 地图 + 热力图 + 折线一站式 |
| 地图 | 腾讯地图 GL JS | 高德 / 天地图 | 国内合规持牌；`_TMapSecurityConfig` 代理模式可隐藏密钥 |
| 检测模型 | YOLO11s | YOLOv8n / RT-DETR | 小数据集上 s 比 n 召回更好；边缘端 ONNX 生态成熟 |
| 推理运行时 | ONNX Runtime / TensorRT | RKNN / OpenVINO | 跨平台，x86 与 ARM 都能落 |
| 容器 | Docker Compose | K8s | 单机规模，K8s 是过度设计 |

---

相关文档：`architecture.md`（架构与数据流）· `api.md`（接口契约）· `mqtt-topics.md`（MQTT 主题树）· `deployment.md`（部署）· `development.md`（协作规范）
