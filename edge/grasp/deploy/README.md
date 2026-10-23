# 一键自动抓取 · 部署与操作手册（edge/grasp/deploy）

> 目标：**开机后机械臂自己识别、自己抓、自己放进料槽**，不需要平台派单、不需要点按钮。
>
> 证据口径：本手册里的 `--mock` 验证只证明**协议与闭环逻辑可离线复现**，
> 不构成物理拾取或现场验收。真机 + 受控实验只能标 **E2**。

---

## 一、为什么需要这一层（先看它到底补了什么）

仓库里原本已经有全部业务代码：六态抓取闭环（`grasp_loop.py`）、
ROS 接入层（`arm_io.py`）、视觉驱动（`vision_driver.py`）、常驻监督器
（`supervisor.py`）。systemd 单元 `oceanus-arm-autopick.service` 也早就存在。

**但那条链路在真机上是走不通的**，原因是 `ExecStart` 直接跑：

```
python3 -m grasp.supervisor
```

而 `supervisor` **只做业务**：它假设厂商的相机与舵机控制器节点已经在跑。
板子一重启，没有任何人去起那三组 launch，于是 `ArmIo` 拿不到 ROS 链路，
`precheck` 判 `✗ 相机/ROS`，监督器按设计**干净退出**（exit 2）。

现场看到的现象极具误导性：

* `systemctl status` 显示服务 `failed`（不是 crashed，是「按前置条件拒绝开工」）；
* journal 里只有一句「ROS 链路不可用」；
* 机械臂一动不动，但代码、模型、配置看起来都没问题。

`run_autopick.sh` 就是把「开机 → 机械臂开始自己捡」之间的空档填上：

| 步骤 | 原来 | 现在 |
| --- | --- | --- |
| source ROS + 厂商工作空间 | 手工 | 脚本内 |
| 起依赖 / 相机 / 舵机控制器三组 launch | 手工（且极易误起 `start_functions.launch`） | 脚本内，**显式不含** `start_functions` |
| 确认相机真的在出帧、舵机真的有回读 | 手工 `rostopic` | 脚本内，超时报错并指出看哪个日志 |
| 只读自检（模型/标定/放置点/相机） | 手工跑 precheck | 脚本内，不过就拒绝开工 |
| 起 supervisor 常驻自捡 | 手工 | 脚本内 |
| 停止时回收厂商节点 | 没人做 → 下次启动串口被占 | 脚本内按 launch 名精确回收 |

---

## 二、两条命令（这就是全部）

### 开发机（Windows）

```powershell
# 0) 部署包自检：必备文件 / 可执行位 / LF 行尾 / 端到端一轮（不连板）
python scripts\verify_autopick_package.py

# 1) 先干跑看它打算拷什么、执行什么（不连板、不改动）
python scripts\deploy_autopick_to_pi.py --host 192.168.149.1 --dry-run

# 2) 真部署（默认只验证，不启动任何会动机械臂的服务）
python scripts\deploy_autopick_to_pi.py --host 192.168.149.1

# 3) 确认验证通过、工作区无遮挡、有人在场，才让它开始自己捡
python scripts\deploy_autopick_to_pi.py --host 192.168.149.1 --apply
```

第 2 步会自动做：打包（含 38MB 模型）→ scp → 板上解包 → 装依赖 →
装 systemd 单元 → 跑**离线闭环验证**（`--mock --once`，合成画面 + 模拟舵机）
→ 跑**真机只读自检**（`--check`，不动机械臂）。

### 树莓派（板上）

```bash
cd /home/ubuntu/oceanus

# 只做前置检查，什么都不启动（现场排障第一条命令）
bash edge/grasp/deploy/run_autopick.sh --check

# 前台跑一次，所有输出都在眼前（首次真机动作建议这样跑）
bash edge/grasp/deploy/run_autopick.sh

# 交给 systemd 常驻 + 开机自启
sudo systemctl enable --now oceanus-arm-autopick
journalctl -u oceanus-arm-autopick -f
```

---

## 三、启动脚本的三个开关（现场最常用的三个）

| 命令 | 作用 | 什么时候用 |
| --- | --- | --- |
| `--check` | 只跑前置自检，不启动任何东西 | 排障第一条；改完配置/标定后复验 |
| `--mock --once` | 合成画面 + 模拟舵机跑一轮闭环，**不碰 ROS** | 判断「是代码问题还是硬件问题」 |
| `--skip-bringup` | 不起厂商节点，只要 supervisor | 厂商节点已经手工起好了 |

其他参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--ros-setup` | `~/armpi_fpv/devel/setup.bash` | 厂商工作空间路径（编译产物），路径不同必须显式给 |
| `--log-dir` | `/tmp/oceanus-autopick` | 三组 launch 的日志落点；systemd 下用 `/var/log/oceanus-autopick` |
| `--topic-timeout` | `45` | 等相机/舵机话题就绪的秒数 |
| `--keep-bringup` | 关 | 退出时**保留**厂商节点（调试用） |
| `--config` | `edge/grasp/config.yaml` | 配置文件 |

---

## 四、退出码（systemd 靠它决定要不要重启）

| 码 | 含义 | systemd 行为 |
| --- | --- | --- |
| `0` | 收到停止信号后正常收尾 | 不重启 |
| `2` | 前置不满足（缺 ROS / 标定 / 模型 / 放置点） | **不重启**（`RestartPreventExitStatus`）——要人去修 |
| `3` | 连续抓取失败超限 | **不重启** ——要人去看夹爪/标定/相机 |
| `4` | 厂商节点超时未就绪（相机不出图 / 舵机控制器没起来） | **不重启** ——要人去看 `/var/log/oceanus-autopick/*.log` |

这样设计的原因：这四种都是「要人去修」的状态。让 systemd 无脑重启只会把
journal 刷爆，把真正的第一现场（第一份日志）冲掉。

---

## 五、真机首次跑通清单

按顺序做完，`--check` 必须六项全 ✓（`训练依赖` 非必需）。

### 1. 厂商 ROS 环境（板子上）

```bash
# 确认工作空间编译过
ls ~/armpi_fpv/devel/setup.bash        # 不存在就先 catkin_make

# 确认串口没被别人占着（半双工总线必须独占）
ls -l /dev/ttyS0
sudo usermod -aG dialout $USER         # 加完要重新登录才生效；或临时 sudo chmod 666 /dev/ttyS0
```

> ⚠️ **绝对不要启动 `start_functions.launch`**。它会把 object_sorting /
> object_tracking / warehouse 等 `required=true` 节点全拉起来，直接抢走相机
> 与控制权。这是「识别有框但机械臂不动」的头号死因。`run_autopick.sh` 只起
> 依赖 / 相机 / 舵机控制器三组。

### 2. 模型（最容易漏的一项）

`models/active/best.onnx` 在 `.gitignore` 里，**不会随 git 上板**。
部署脚本默认会带过去（38MB）。如果确实漏了：

```powershell
# 只补模型，不碰代码与 systemd
python scripts\deploy_autopick_to_pi.py --host 192.168.149.1 --only-model
```

或在作业台「4 · 部署与回滚」里部署一次。

### 3. 9 点标定（真机必需）

`calibration.json` 与设备绑定，**没有它闭环拒绝开工**。

1. 开发机或板上起作业台：`python3 -m grasp.web.server` → `http://<板子IP>:8090`
2. 「5 · 9 点标定」：把可检测目标放到 9 个已知网格点，采集 → 保存
3. RMSE 应为**非 0** 的毫米量级（几 mm 正常）。
   ★ RMSE 恰为 0 是**演示标定**的指纹，真机上不可能，脚本会把这种文件判为未标定。
4. **平面高度 `plane_z` 必填**：原点在臂座安装面。船上甲板高于水面，
   所以水面 z 是**负值**（甲板高出水面 8cm → `-0.08`）。这个值错了，
   爪子要么压不到袋子、要么撞下去。

### 4. 放置点示教（真机必需，与标定并列）

最后一步是「放进旁边的料槽」，没有放置点闭环**直接报错拒绝开工** ——
夹起来没地方放比不抓更糟，绝不允许「夹住了就算成功」。

作业台「手动点动」把臂摆到槽口上方 → 点「用当前姿态作放置点」。
或在 `config.yaml` 的 `agent.place.coord` 填工作平面坐标（默认 `[0.275, 0.0]`
是右舷料槽，左舷填 `-0.275`）。

### 5. 复验

```bash
bash edge/grasp/deploy/run_autopick.sh --check
```

---

## 六、排障对照表

| 现象 | 大概率原因 | 怎么做 |
| --- | --- | --- |
| 服务 failed，日志只有「ROS 链路不可用」 | 这是**旧单元**的行为；用本手册的 `run_autopick.sh` | 确认 `ExecStart` 指向 `run_autopick.sh`，重跑 `install.sh` |
| 退出码 4，日志里没有 `camera.log` | 日志目录不可写 | `sudo mkdir -p /var/log/oceanus-autopick && sudo chown $USER /var/log/oceanus-autopick` |
| 相机话题在列表里但没有帧 | 摄像头没插好 / 被别的进程占 | `tail -50 /var/log/oceanus-autopick/camera.log`；确认没有第二个进程开 `/dev/video0` |
| 舵机控制器起不来 | `/dev/ttyS0` 被上次的节点占着 | `pgrep -af roslaunch` 看有没有残留；正常退出会回收，异常 kill 需手工清 |
| `bash: run_autopick.sh: /bin/bash^M: bad interpreter` | 脚本被 checkout 成 CRLF | `.gitattributes` 已声明 `*.sh text eol=lf`；板上应急：`sed -i 's/\r$//' edge/grasp/deploy/run_autopick.sh` |
| 退出码 2 且 `✗ 模型` | 模型没上板 | 见上面「2. 模型」 |
| 退出码 2 且 `✗ 标定` / `疑似演示标定` | 没跑真机 9 点标定，或拷了 mock 的标定文件 | 重跑 9 点标定 |
| 退出码 2 且 `未设置料槽放置点` | 没示教放置点 | 见上面「4. 放置点示教」 |
| 退出码 2 且 `✗ 相机/ROS` | 没 source 厂商工作空间 / `--ros-setup` 路径不对 | `bash ... --check --ros-setup /实际/路径/devel/setup.bash` |
| 退出码 3，连抓失败 | 夹爪闭合方向不对 / 标定失效 / 舵机没使能 | 先 `--mock --once` 排除代码问题，再核 `gripper_open/close` 与标定 RMSE |
| 机械臂动但抓不到 | `plane_z` 不对（最常见）/ `grasp_pitch` 不是 -175 | 实测水面高度；抓取俯仰角用 -175（-65 是待命位角度） |

三组 launch 的日志是**第一现场**，排障永远先看它：

```bash
tail -n 50 /var/log/oceanus-autopick/{dependence,camera,servo}.log
```

---

## 七、与平台派单模式的关系（两者互斥）

| 模式 | 入口 | 语义 |
| --- | --- | --- |
| **常驻自捡**（本手册） | `run_autopick.sh` / `oceanus-arm-autopick` | 不等派单，开机后自己反复找目标抓 |
| 平台派单 | `arm_bridge` / `oceanus-arm-bridge` | 平台派一次单抓一个 |

两者都会调用同一个 `GraspLoop` 驱动**同一台机械臂**，同时跑会互相抢舵机总线。
`install.sh` 会自动 disable 另一个，但手工启服务时请自己注意。

要平台派单：

```bash
sudo systemctl disable --now oceanus-arm-autopick
sudo systemctl enable  --now oceanus-arm-bridge
```

---

## 八、无硬件自测（改完代码先跑这些）

```bash
# 板级桥接 + 抓取闭环 + 作业台接口（全 mock，不需要 ROS / 相机 / 硬件）
python -m pytest edge/grasp edge/arm_bridge -q

# 启动脚本本身：合成画面跑一轮（不碰 ROS）
bash edge/grasp/deploy/run_autopick.sh --mock --once

# 部署包完整性：必备文件 / 可执行位 / LF 行尾 / CRLF 自愈 / 端到端一轮
# （开发机上模拟板上流程：真的解包 + 真的跑一轮闭环）
python scripts/verify_autopick_package.py
```

---

## 九、口径红线

* `--mock` 通过 ≠ 真机能抓。合成画面只证明**状态机与协议闭环可复现**。
* `ok`（夹爪回读停滞 = 夹到东西）与 `placed`（真的放进料槽）**必须分开报**。
  桥接层驱动里 `placed=False` 会把 `review_result` 置为 `recheck`。
* `review_result` 默认 `recheck`：只有人工核对拾取前后照片后才改成 `confirmed`。
  不要把「机械臂动了一下」当成审核结论。
* 感知精度以留出集 mAP 表述，并注明**非现场精度**。
* 真机 + 受控实验只能标 **E2**，不得写成 E3/E4。
