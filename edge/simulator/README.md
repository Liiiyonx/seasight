# 探海灵眸 SeaSight — 边缘感知模拟器

无真实摄像头时，用它驱动整套演示链路。模拟的是**边缘盒**的行为，
而不是简单的「随机发消息」——关键在于复刻真实边缘盒里几件容易做错的事。

## 它模拟了什么

| 能力 | 为什么重要 |
| --- | --- |
| **时序校验** | 浪花是瞬态的，垃圾是持续的。目标级跟踪连续命中 3 次才升级为确认事件，这是把误报压下来的第一道闸 |
| **抽帧推理** | 真实边缘盒不是逐帧跑模型，而是每 N 帧推一次（`decimation`，默认 4）。抽帧天然压噪声：只在单帧闪现的浪花被抽中的概率仅 1/N，凑不满 `min_hits` |
| **seq 单调递增** | 平台用 `(device_id, seq)` 唯一约束做幂等；模拟器按设备维护计数器，崩溃重启后从上次值续上 |
| **断网补传** | 网络断开时事件进本地队列，恢复后按原 seq 顺序补发——真实渔港 4G 信号就是这样，不模拟这条链路，平台的判重逻辑就永远测不到 |
| **QoS1 重传** | 可选的 `--dup-rate` 参数故意重复投递，验证平台判重是否真的生效 |
| **LWT 遗嘱** | 连接时注册遗嘱消息，异常断开时平台能立刻感知设备离线 |

## ⚠️ 两个容易搞混的参数

**`--interval` 是「抽帧间隔」，不是「事件上报间隔」。**
真实边缘盒把摄像头流抽帧到 ~8fps（每 3 帧取 1 帧），所以默认 0.125s/帧。
时序校验的窗口按帧计数（`window_frames=15`），15 帧 ≈ 1.9 秒。
如果把这里当成"事件间隔"填成 6 秒，15 帧要跑 90 秒窗口才填满，
你会看到「跑了半天一个事件都没有」——**系统没坏，是参数语义搞反了**。

**想控制演示节奏，用 `--event-interval`。**
它覆盖的是「同一格网的上报冷却」（配置里默认 90s）。
demo 演示时设 3，几秒就能看到连续告警；不设则用配置值。

```bash
# 演示节奏：8fps 抽帧 + 3 秒上报冷却
python simulator.py --dry-run --duration 15 --scenario demo --event-interval 3
```

## 用法

```bash
# 依赖
pip install pyyaml paho-mqtt

# 演示模式（预置的连江沿海点位，带时序校验，循环跑）
python simulator.py --scenario demo --loop

# 演示模式 + 快节奏（3 秒冷却，适合当场演示）
python simulator.py --scenario demo --event-interval 3

# 压力模式（高频产生事件，压测派单引擎）
python simulator.py --scenario stress

# 回归模式（可复现的随机种子，用于冒烟测试比对结果）
python simulator.py --scenario regression --seed 42 --duration 60

# 模拟网络抖动（每 8 个事件断一次网，队列积压后补传）
python simulator.py --scenario demo --net-drop-every 8

# 故意重复投递 30% 的消息，验证平台幂等
python simulator.py --scenario demo --dup-rate 0.3

# 只看报文不长连 Broker
python simulator.py --dry-run --duration 10
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--scenario` | `demo` | `demo` / `stress` / `regression` |
| `--host` / `--port` | `localhost` / `1883` | MQTT Broker |
| `--username` / `--password` | 空 | Broker 认证（EMQX 默认开启） |
| `--interval` | `0.125` | **抽帧间隔**秒数（模拟摄像头帧率），0.125 ≈ 8fps |
| `--event-interval` | 无 | 覆盖上报冷却秒数，控制演示节奏；不设则用配置里的 90s |
| `--loop` | 关 | 循环跑不退出 |
| `--duration` | `0` | 运行时长秒数，0=不限 |
| `--seed` | 无 | 随机种子，固定后结果可复现 |
| `--devices` | 全部 | 只模拟指定设备，逗号分隔 |
| `--dup-rate` | `0` | 重复投递比例 0~1 |
| `--net-drop-every` | `0` | 每 N 个事件模拟一次断网 |
| `--dry-run` | 关 | 只打印不进 Broker（看报文长什么样） |

## 输出里的统计口径

跑完会打印一段统计，重点是**误报抑制率**：

```
  原始帧数         : 840（7 台设备逐帧判定）
  原始检测数       : 5772（含瞬态噪声）
  时序校验确认事件 : 52
  被挡下的误报     : 2451
  误报抑制率       : 42.5%
```

抑制率 = **从未被稳定跟踪的检测数 / 喂进校验器的检测总数**，
三个计数（`fed` / `absorbed` / `suppressed`）同单位，所以恒在 0~100%，
且满足 `fed = absorbed + suppressed`。

实测三个场景都收敛在 42~43%：demo 42.5%、stress 41.9%、regression 42.8%。
这个数字不是"越高越好"——它反映的是模拟器里噪声检测的占比，
真实渔港的数据需要拿实测视频重新标定。

> 早期版本这里曾算出 140.9% 的抑制率。根因是**单位混用**：
> 分子用的是「淘汰的 track 数」，分母用的是「检测数」，
> 而一个 track 可能只对应 1 个检测也可能对应几十个。
> 现已统一为检测数口径，并有单元测试 `test_suppression_ratio_in_range` 守住。


## 报文契约

事件上报到 `marine/{site_id}/{device_id}/event`，QoS1：

```json
{
  "event_id": "evt_CAM-MABI-01_000123",
  "device_id": "CAM-MABI-01",
  "device_type": "shore_camera",
  "timestamp": "2026-09-18T01:23:45+08:00",
  "location": {"lng": 119.6531, "lat": 26.3867},
  "detections": [
    {"class": "foam", "confidence": 0.91, "bbox": [412, 288, 468, 331]}
  ],
  "aggregate": {"main_class": "foam", "count": 3, "max_confidence": 0.91},
  "evidence_url": null,
  "model_version": "det_v0.1.0",
  "seq": 123
}
```

完整主题树见 `../../docs/mqtt-topics.md`。
