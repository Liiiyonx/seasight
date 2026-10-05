#!/usr/bin/env python3
"""验证 EMQX 的认证与 ACL 是否按预期工作（走 paho，不用手写报文）。

★ 为什么用 paho 而不是手写 CONNECT
--------------------------------------
第一版手写 MQTT CONNECT 报文，始终收不到 CONNACK，看起来像"认证失败"。
实际上认证一直是好的（backend 容器用 paho 连得好好的，CONNACK=0）。
错在我自报的报文有问题（MQTT 3.1.1 的 CONNECT flags / keepalive 编码）。
**结论：不要手写协议报文验证认证** —— 用真实客户端库，否则会把
"客户端实现有 bug"误判成"服务端配置有问题"，白查半天。

本脚本检查五项：
1. backend_service 用 .env 密码认证成功
2. robot_device 用 .env 密码认证成功
3. **旧占位密码 CHANGE_ME 被拒** —— 证明新密码真的生效了
4. robot_device 订阅**别人的**主题被拒（ACL 边界）
5. robot_device 订阅**自己的**主题被接受

★ 不打印任何明文密码。

用法::

    pip install paho-mqtt      # 首次
    python scripts/check_mqtt_auth.py
"""

import argparse
import io
import os
import re
import sys
import time

DEFAULT_DEVICE = "RBT-ARM-01"
DEFAULT_SITE = "lianjiang"


def read_env():
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env")
    if not os.path.isfile(path):
        print("!! 找不到 .env，先跑 python scripts/gen_local_secrets.py")
        sys.exit(2)
    env = {}
    for raw in io.open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _paho():
    try:
        import paho.mqtt.client as mqtt
        return mqtt
    except ImportError:
        print("!! 缺 paho-mqtt：pip install paho-mqtt")
        sys.exit(2)


def _new_client(mqtt, ws_path=None):
    """建 paho client，按需切到 WebSocket。

    ★ WebSocket 有四个坑（2026-10-05 实测，见 cloud-mqtt-wss-deploy.md）：
      1. 传输名是 "websockets"，不是 "ws"
      2. 传给 Client(transport=)，不是 connect()
      3. EMQX 5.8 要求 Sec-WebSocket-Protocol: mqtt，缺了 HTTP 400
      4. 树莓派（paho 1.6.1）需外部 websocket-client
    """
    if ws_path:
        try:
            client = mqtt.Client(transport="websockets")
        except TypeError:
            client = mqtt.Client()
        client.ws_set_options(
            path=ws_path,
            headers={"Sec-WebSocket-Protocol": "mqtt"},
        )
        return client
    try:
        return mqtt.Client()
    except TypeError:
        return mqtt.Client()


def try_connect(mqtt, host, port, user, pw, timeout=12, ws_path=None):
    """尝试连接，返回 CONNACK 返回码（None = 拿不到）。"""
    result = {}

    def on_connect(_c, _u, _f, rc):
        result["rc"] = rc

    client = _new_client(mqtt, ws_path)
    client.username_pw_set(user, pw)
    client.on_connect = on_connect
    try:
        client.connect(host, port, 20)
        client.loop_start()
        deadline = time.time() + timeout
        while time.time() < deadline and "rc" not in result:
            time.sleep(0.15)
        client.loop_stop()
        client.disconnect()
    except Exception:  # noqa: BLE001
        return None
    return result.get("rc")


def try_subscribe(mqtt, host, port, user, pw, topic, timeout=12,
                  ws_path=None):
    """连接后请求订阅，返回 ( granted_qos, error_str )。

    granted_qos == 0x80 (128) 表示被拒；EMQX 5.x 订阅被拒会断连，
    所以也把"连接被断"当作被拒处理。
    """
    state = {"suback": None, "denied": False}

    def on_connect(_c, _u, _f, rc):
        if rc == 0:
            _c.subscribe(topic, qos=0)

    def on_subscribe(_c, _u, _mid, granted, _props=None):
        state["suback"] = granted[0] if granted else None

    def on_disconnect(_c, _u, _rc, _props=None, _reason=None):
        if state["suback"] is None:
            state["denied"] = True

    client = _new_client(mqtt, ws_path)
    client.username_pw_set(user, pw)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    try:
        client.connect(host, port, 20)
        client.loop_start()
        deadline = time.time() + timeout
        while time.time() < deadline and state["suback"] is None \
                and not state["denied"]:
            time.sleep(0.15)
        client.loop_stop()
        client.disconnect()
    except Exception:  # noqa: BLE001
        state["denied"] = True
    if state["denied"] and state["suback"] is None:
        return None, True
    return state["suback"], False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST_LOCAL", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--transport", choices=["tcp", "ws"], default="tcp",
                    help="云端用 ws（经 nginx 443 → emqx:8083）")
    ap.add_argument("--ws-path", default="/mqtt",
                    help="WebSocket 路径，须与 nginx location 一致")
    args = ap.parse_args()

    mqtt = _paho()
    env = read_env()
    ws_path = args.ws_path if args.transport == "ws" else None
    if ws_path:
        print("传输: WebSocket（路径 %s）" % ws_path)
    svc = env.get("MQTT_USERNAME", "backend_service")
    svc_pw = env.get("MQTT_PASSWORD", "")
    arm = env.get("ARM_MQTT_USERNAME", "robot_device")
    arm_pw = env.get("ARM_MQTT_PASSWORD", "")

    if not svc_pw or not arm_pw:
        print("!! .env 里缺 MQTT_PASSWORD 或 ARM_MQTT_PASSWORD")
        return 2

    print("目标 broker: %s:%d" % (args.host, args.port))
    print("（不打印任何明文密码）\n")
    ok = True

    print("=== 1) %s 认证（应通过）===" % svc)
    rc = try_connect(mqtt, args.host, args.port, svc, svc_pw, ws_path=ws_path)
    good = (rc == 0)
    print("   CONNACK=%s  %s" % (rc, "✓ 接受" if good else "✗ 失败"))
    ok = ok and good

    print("\n=== 2) %s 认证（机械臂账号，应通过）===" % arm)
    rc = try_connect(mqtt, args.host, args.port, arm, arm_pw, ws_path=ws_path)
    good = (rc == 0)
    print("   CONNACK=%s  %s" % (rc, "✓ 接受" if good else "✗ 失败"))
    ok = ok and good

    print("\n=== 3) 旧占位密码 CHANGE_ME（应被拒）===")
    rc = try_connect(mqtt, args.host, args.port, arm, "CHANGE_ME",
                      ws_path=ws_path)
    rejected = (rc in (4, 5))
    print("   CONNACK=%s  %s" % (rc, "✓ 已失效" if rejected
                                else "★ 仍能连 —— 密码没换成功"))
    ok = ok and rejected

    print("\n=== 4) ACL 边界：%s 订阅**别的**设备主题（应被拒）===" % arm)
    qos, denied = try_subscribe(mqtt, args.host, args.port, arm, arm_pw,
                               "robot/OTHER-DEVICE/task", ws_path=ws_path)
    blocked = denied or qos == 128
    print("   granted_qos=%s  %s" % (qos,
          "✓ 被拒" if blocked else "★ 被接受 —— ACL 有洞"))
    ok = ok and blocked

    print("\n=== 5) ACL 正常：%s 订阅自己的 %s/task（应接受）==="
          % (arm, args.device))
    qos, denied = try_subscribe(mqtt, args.host, args.port, arm, arm_pw,
                               "robot/%s/task" % args.device, ws_path=ws_path)
    allowed = (qos == 0)
    print("   granted_qos=%s  %s" % (qos,
          "✓ 接受" if allowed else "✗ 被拒"))
    ok = ok and allowed

    print("\n" + "=" * 62)
    print("总体：%s" % ("✓ 认证与 ACL 全部符合预期"
                       if ok else "★ 有项不符合预期"))
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
