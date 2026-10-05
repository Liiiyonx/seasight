"""开发机上的 bridge 闭环自测（不碰真实机械臂）。

★ 为什么需要这个
----------------
树莓派连不上本机 broker（AP 隔离），所以 bridge 的 MQTT 逻辑
**至今没在真实链路上跑过**。不能等网络通了才发现契约有 bug。

这里用 **simulated 驱动**（确定性、无硬件）在开发机上跑真实 MQTT 链路：
  真实 broker(本机 1883) → 真实 paho → 真实 ArmBridge →
  真实 driver registry → simulated 驱动（不驱动任何硬件）
这样能验证：订阅/发布主题、ACK 回执、progress 上报、遥测上报、
幂等去重、乱序处理 —— 全部走生产代码路径，只把最后一步换成模拟。

安全边界：**不连接树莓派、不驱动任何关节**。
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "edge"))

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", ".env")


def read_env():
    if not os.path.isfile(ENV_PATH):
        print("!! 找不到 .env")
        sys.exit(2)
    env = {}
    for raw in open(ENV_PATH, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    env = read_env()
    broker = "127.0.0.1"
    port = 1883

    # 用 robot_device 身份（与树莓派上 bridge 同账号同权限）
    user = env.get("ARM_MQTT_USERNAME", "robot_device")
    pw = env.get("ARM_MQTT_PASSWORD", "")
    if not pw:
        print("!! .env 里没有 ARM_MQTT_PASSWORD")
        return 2

    site = "lianjiang"
    # 用 SELFTEST 而不是 RBT-ARM-01：ACL 把 robot_device 绑死在真实设备号上
    # （最小权限，2026-10-05 实测用别的 id 会被静默拒绝，看着像 bridge 坏了）。
    # deploy/emqx/oceanus_acl.conf 里为 SELFTEST 单开了同名规则。
    dev = "SELFTEST"
    task_topic = "robot/%s/task" % dev
    cmd_topic = "robot/%s/cmd" % dev
    ack_topic = "robot/%s/cmd/ack" % dev
    progress_topic = "robot/%s/task/progress" % dev
    telemetry_topic = "marine/%s/%s/telemetry" % (site, dev)
    status_topic = "marine/%s/%s/status" % (site, dev)

    import paho.mqtt.client as mqtt

    received = {"ack": [], "progress": [], "telemetry": [], "status": []}

    # 平台侧观察者（用 backend_service 账号，它有全量订阅权限）
    watcher = mqtt.Client()
    watcher.username_pw_set(env.get("MQTT_USERNAME", "backend_service"),
                             env.get("MQTT_PASSWORD", ""))

    def on_message(_c, _u, m):
        try:
            payload = json.loads(m.payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"_raw": m.payload.decode("utf-8", "replace")[:120]}
        for key, prefix in (("ack", ack_topic), ("progress", progress_topic),
                            ("telemetry", telemetry_topic),
                            ("status", status_topic)):
            if m.topic == prefix:
                received[key].append(payload)

    watcher.on_message = on_message

    # ★ 订阅必须在 connect 之后。paho 允许先 subscribe()，但只有在
    #   连接已建立时才会真正发 SUBSCRIBE；连接前调用会被静默丢弃
    #   —— 症状是"脚本跑完一条消息都没收到"，看起来像 broker 坏了。
    #   第一版就是栽在这里（subscribe 写在 loop_start 之前）。
    _subscribed = {"done": False}

    def _on_watcher_connect(_c, _u, _f, rc):
        if rc != 0:
            print("  [!] watcher CONNACK=%s（订阅不会生效）" % rc)
            return
        for t in (ack_topic, progress_topic, telemetry_topic, status_topic):
            _c.subscribe(t, qos=1)
        _subscribed["done"] = True

    watcher.on_connect = _on_watcher_connect
    watcher.connect(broker, port, 20)
    watcher.loop_start()
    deadline = time.time() + 10
    while time.time() < deadline and not _subscribed["done"]:
        time.sleep(0.1)
    if not _subscribed["done"]:
        print("!! watcher 订阅未建立，终止（避免误判为 bridge 无响应）")
        return 5
    time.sleep(0.5)
    print("平台观察者已订阅 4 个主题（用 backend_service 账号）")

    # ---- 启动真实 ArmBridge（simulated 驱动）----
    from arm_bridge.bridge import ArmBridge
    from arm_bridge.drivers import build_arm_driver
    from arm_bridge.mqtt_transport import MqttTransport
    from device_sim.protocol import DeviceMode, Position
    import yaml

    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "edge", "arm_bridge", "config.yaml")
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    cfg.setdefault("driver", {})["backend"] = "simulated"
    driver = build_arm_driver(cfg, "simulated")
    print("驱动: %s（模拟，不接硬件）" % type(driver).__name__)

    transport = MqttTransport(
        host=broker, port=port, username=user, password=pw,
        client_id="robot-%s" % dev, keepalive=60, use_lwt=True,
        will_topic=status_topic,
    )
    # transport 是构造参数（不是 start 的参数）—— ArmBridge.__init__
    # 的第三个位置参数是 driver，transport 走关键字传入。
    #
    # ★★ 必须自己调 transport.connect() —— ArmBridge 既不在 __init__ 里连，
    #   也不在 start() 里连；生产入口 main.py:221 是显式调的。
    #   漏掉这一步的表现极具迷惑性：subscribe() 会「成功」（handler 注册了）、
    #   connected 一直是 False、**一条消息都收不到**，看起来像 broker 或 ACL
    #   有问题。排查时我因此误判了三次。
    transport.connect()
    deadline = time.time() + 10
    while time.time() < deadline and not transport.connected:
        time.sleep(0.1)
    if not transport.connected:
        print("!! transport 未连上 broker，终止（避免误判为 bridge 无响应）")
        return 5
    print("transport 已连接 broker（connected=True）")

    bridge = ArmBridge(device_id=dev, site_id=site, driver=driver,
                       transport=transport)
    bridge.start()
    time.sleep(1.2)
    print("bridge 已启动，device_id=%s site_id=%s" % (dev, site))
    print()

    # ---- 1. 发一个任务，看 ACK ----
    print("【1】平台派单 → 应收到 ACK")
    # ★ 报文必须是 DeviceCommand 格式，不是平台 UI 层的 task 格式。
    #   DeviceCommand.from_dict 要求 6 个字段全部存在：
    #     command_id / device_id / seq / issued_at / expires_at / action
    #   缺任何一个都会进 "command_invalid" 分支静默丢弃 —— bridge 不报错，
    #   只是什么都不发生。第一版我发的是 task_id+target 格式，零响应，
    #   误以为是 broker 或 ACL 的问题，实际是自己的报文不对。
    now = time.time()
    task = {"command_id": "SELFTEST-001", "device_id": dev, "seq": 1,
            "issued_at": now, "expires_at": now + 120, "action": "dispatch",
            # dispatch 动作要求 params 里同时有 task_id 与 target{lng,lat}，
            # 否则被 _reject(reason="missing_target") 静默拒绝。
            "params": {"task_id": "T-PICK-001",
                       "target": {"lng": 119.6540, "lat": 26.3870}}}
    watcher_publish = mqtt.Client()
    watcher_publish.username_pw_set(env.get("MQTT_USERNAME", "backend_service"),
                                    env.get("MQTT_PASSWORD", ""))
    watcher_publish.connect(broker, port, 20)
    watcher_publish.loop_start()
    watcher_publish.publish(task_topic, json.dumps(task), qos=1)
    print("    已发 command_id=SELFTEST-001 action=dispatch")
    time.sleep(3.0)

    # ---- 2. 发一个命令，看 ACK ----
    print()
    print("【2】平台发命令 → 应收到 ACK")
    cmd = {"command_id": "SELFTEST-CMD-001", "device_id": dev, "seq": 2,
           "issued_at": time.time(), "expires_at": time.time() + 60,
           "action": "return_home"}
    watcher_publish.publish(cmd_topic, json.dumps(cmd), qos=1)
    print("    已发 command_id=SELFTEST-CMD-001")
    time.sleep(2.5)

    # ---- 3. 上线状态 + 遥测 ----
    print()
    print("【3】bridge 主动上报上线状态与遥测")
    bridge.publish_online()
    time.sleep(0.8)
    # 遥测走私有方法 _publish_telemetry（关键字参数），它内部会调 driver.status()
    bridge._publish_telemetry(
        status=DeviceMode.IDLE, task_id=None,
        location=Position(119.6540, 26.3870), now=time.time())
    time.sleep(1.5)

    # ---- 4. 重复任务（幂等）----
    print()
    print("【4】重复投递同一 command_id（幂等去重检查）")
    n_before = len(received["progress"])
    watcher_publish.publish(task_topic, json.dumps(task), qos=1)
    time.sleep(2.5)
    n_after = len(received["progress"])
    print("    progress 条数 %d → %d %s"
          % (n_before, n_after,
             "（已去重，正确）" if n_after == n_before else "（又发了一条，检查幂等）"))

    # ---- 收尾 ----
    print()
    print("=" * 64)
    print("收到的消息统计")
    print("=" * 64)
    for key in ("ack", "progress", "telemetry", "status"):
        print("  %-10s %d 条" % (key, len(received[key])))

    for key in ("ack", "progress", "telemetry", "status"):
        if received[key]:
            print()
            print("--- %s 首条 ---" % key)
            print("  " + json.dumps(received[key][0], ensure_ascii=False)[:220])

    bridge.stop()
    watcher.loop_stop()
    watcher.disconnect()
    watcher_publish.loop_stop()
    watcher_publish.disconnect()

    print()
    ok = (len(received["ack"]) >= 2 and len(received["progress"]) >= 1
          and len(received["telemetry"]) >= 1)
    print("=" * 64)
    print("结论：%s" % ("✓ bridge 完整链路可用（订阅/ACK/progress/遥测）"
                       if ok else "★ 有缺失，见上面统计"))
    print("  （真实树莓派链路只差网络：AP 隔离 + STA 切换）")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
