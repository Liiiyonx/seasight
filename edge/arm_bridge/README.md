# 机械臂桥接层（edge/arm_bridge）

> 用途：把「任意机械臂」和 SeaSight 平台解耦。机械臂 SDK 的差异全部隔离在
> `ArmDriver` 适配器里；平台侧 MQTT 契约、后端代码、派单状态机都不用改。
>
> 证据口径：本目录是 E1/E2 代码与自测设施。模拟驱动和内存 transport 只证明
> 协议闭环可复现，不构成物理拾取、现场验收或识别精度证据；感知精度仍为
> `not_evaluated`。

## 为什么是「任意机械臂无缝接入」

平台与机械臂之间只约定 MQTT 报文：

| 方向 | 主题 | 用途 |
| --- | --- | --- |
| 平台 → 桥 | `robot/{robot_id}/task` | 派单 dispatch |
| 平台 → 桥 | `robot/{robot_id}/cmd` | pause / resume / return_home / emergency_stop / ack |
| 桥 → 平台 | `robot/{robot_id}/cmd/ack` | 任务确认 |
| 桥 → 平台 | `robot/{robot_id}/task/progress` | collecting / done |
| 桥 → 平台 | `marine/{site_id}/{robot_id}/telemetry` | 电量、仓容、位置、任务号 |
| 桥 → 平台 | `marine/{site_id}/{robot_id}/status` | 上下线（LWT + retained） |

机械臂到货后只做一件事：在 `edge/arm_bridge/drivers.py` 里新增一个
`ArmDriver` 实现（HTTP、串口、CAN 都行），把 SDK 调用翻译成
`pick / pause / resume / return_home / emergency_stop`。桥接层和平台侧
一行都不用改。

## 文件

| 文件 | 职责 |
| --- | --- |
| `bridge.py` | MQTT 契约层：订阅、ACK、progress、telemetry、幂等、过期拒绝 |
| `drivers.py` | `ArmDriver` 协议 + 驱动注册表 + `SimulatedArmDriver` / `HttpArmDriver` / `HiwonderBusServoArmDriver` |
| `mqtt_transport.py` | paho-mqtt 真实传输，支持 LWT |
| `main.py` | 入口：`--dry-run` 自测或连接真实 broker |
| `config.yaml` | 设备、站点、MQTT、驱动配置 |
| `test_bridge.py` | 内存 transport 全链路测试 |
| `tools/teach_hiwonder_sequence.py` | 真机上多舵机示教录入，输出 `pick_sequence_file` |

## 无机械臂自测

不连 broker、不碰硬件，注入一条派单跑完整闭环：

```bash
.\.venv-analysis\Scripts\python.exe edge\arm_bridge\main.py --config edge\arm_bridge\config.yaml --dry-run
```

预期输出顺序：

```text
robot/RBT-ARM-01/cmd/ack        accepted=true, mode=navigating
robot/RBT-ARM-01/task/progress  collecting
marine/lianjiang/RBT-ARM-01/telemetry
robot/RBT-ARM-01/task/progress  done
marine/lianjiang/RBT-ARM-01/telemetry  idle
```

测试：

```bash
.\.venv-analysis\Scripts\python.exe -m pytest edge\arm_bridge -q
```

## 接真实 broker

```bash
.\.venv-analysis\Scripts\python.exe edge\arm_bridge\main.py --config edge\arm_bridge\config.yaml
```

配置文件里 `mqtt.username / mqtt.password` 用 `robot_device` 账号；真机联调前
必须替换占位密码。驱动先在 `config.yaml` 的 `driver.backend` 选
`simulated`，机械臂 SDK 就绪后改成 `http` 并填 `driver.http.endpoint`。

## 幻尔树莓派总线舵机机械臂（已适配）

实物是幻尔树莓派总线舵机机械臂时，`drivers.py` 已内置
`HiwonderBusServoArmDriver`（注册名 `hiwonder_bus_servo`），走
`ros_robot_controller_sdk.Board` 串口控制。到货后只做两件事：

1. 把商家资料里的 `ros_robot_controller_sdk.py` 放到树莓派上桥接层同目录，
   并安装 `pyserial`（驱动延迟导入 SDK，缺依赖时配置自检仍可跑，但物理动作
   会明确报 `DriverError`，不会假装成功）。
2. 用商家「案例5 示教记录」把拾取动作录成舵机位置序列，填入
   `config.yaml` 的 `driver.vendors.hiwonder_bus_servo.pick_sequence`，再把
   `driver.backend` 改成 `hiwonder_bus_servo`。也可以直接用本仓库的录序列
   工具，把每一步存成 JSON，驱动启动时从文件读取：

   ```bash
   python edge\arm_bridge\tools\teach_hiwonder_sequence.py \
     --servo-ids 1 2 3 4 5 6 --duration 1.0 --output pick_sequence.json
   ```

   （树莓派上按环境用 `python3` 即可，参数不变。）

   然后把 `pick_sequence` 内联合掉，改成
   `pick_sequence_file: "pick_sequence.json"`。两种方式二选一，配置里
   同时出现会直接报错，避免旧序列被悄悄覆盖。

配置示例（`pick_sequence` 的位置是**示教舵机位置 0..1000**，不是经纬度；
平台派单里的 GPS `target` 只作为任务上下文，驱动不把它当关节角算）：

```yaml
driver:
  backend: "hiwonder_bus_servo"
  vendors:
    hiwonder_bus_servo:
      kind: "hiwonder_bus_servo"
      serial_port: "/dev/ttyAMA0"
      baudrate: 1000000
      timeout: 5.0
      servo_ids: [1, 2, 3, 4]
      home_positions: [[1, 500], [2, 500]]
      pick_sequence:
        - duration: 1.0
          positions: [[1, 650], [2, 500]]
        - duration: 1.0
          positions: [[1, 300], [2, 600]]
      collected_weight: 0.0
      review_result: "recheck"
      home:
        lng: 119.6540
        lat: 26.3870
```

`pick()` 的执行顺序固定为：使能舵机扭矩 → 按 `pick_sequence` 逐段下发 →
`bus_servo_stop` 停住 → 回平台 `done`。`pause / return_home / emergency_stop`
同样只调桥接层命令，不新增平台契约。麦轮底盘的总线电机接口
（`set_motor_speed / set_motor_duty`）已在资料中确认，桥接层暂时预留底盘
语义，接入底盘时新增一个 `DriveDriver` 即可，不动本驱动。

`pick_sequence_file` 支持 `tools/teach_hiwonder_sequence.py` 生成的 JSON，
文件结构为 `{format, version, servo_ids, default_duration, steps}`，驱动只
取 `steps`（`[{duration, positions}, ...]`）。文件路径相对于桥接进程的
工作目录。

`review_result` 默认 `recheck`。只有人工核对拾取前后照片后，才把
`confirmed` 写进配置；不要把「机械臂动了一下」当成平台审核结论。

## 新增一台机械臂的步骤

1. 在 `drivers.py` 实现 `ArmDriver`，例如
   `class DobotDriver(HttpArmDriver)`、`class MySerialArmDriver(ArmDriver)`，
   或复用已内置的 `HiwonderBusServoArmDriver`。
2. 注册驱动名：
   `register_arm_driver("dobot", DobotDriver)`（幻尔驱动已注册为
   `hiwonder_bus_servo`）。
3. 在 `config.yaml` 的 `driver` 段把 `backend` 改成新驱动名，并新增
   `driver.<name>` 配置块（或放在 `driver.vendors.<name>` 里指定 `kind`）。
4. 先验证驱动能从注册表 + 配置构造（不发网络/硬件调用）：

   ```bash
   .\.venv-analysis\Scripts\python.exe edge\arm_bridge\main.py \
     --config edge\arm_bridge\config.yaml --check-driver --driver hiwonder_bus_servo
   ```

5. 跑 `--dry-run` 做无机械臂协议自测（固定使用 simulated 驱动，不触发真机），
   再接真实 broker 跑平台「派单 → ACK → collecting → done」联调。

`main.py` 不再需要为新品牌改分支；`build_arm_driver()` 从注册表和配置段
构造驱动。`--driver <name>` 可临时覆盖配置文件里的 `backend`。

## 口径红线

- 真机械臂 + 仿真海面 / 受控实验只能标 **E2**，不得写成 E3/E4。
- 感知精度保持 `not_evaluated`，不把「链路跑通」写成「识别准了」。
- 本目录所有模拟、内存 transport 和自测输出都不代表现场验收。
