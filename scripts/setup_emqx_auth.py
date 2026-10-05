#!/usr/bin/env python3
"""在 EMQX 5.8 上创建 MQTT 用户并写入主题 ACL（走 REST API）。

为什么需要这个脚本
------------------
项目原先用 ``deploy/emqx/bootstrap.csv`` 一次性导入用户与 ACL。实测证明
**该机制在 EMQX 5.8 上不存在**（它是 6.x 才有的数据导入功能）::

    $ docker compose exec emqx emqx ctl import data /opt/emqx/data/bootstrap.csv
    Error: unknown command      # 只列出 gateway-* 命令

症状：``allow_anonymous=false`` 但用户库为空 → **所有 MQTT 连接被拒**
（客户端发出 CONNECT 后被对端直接关闭，没有 CONNACK）。

认证链本身已由 ``docker-compose.yml`` 的 ``EMQX_AUTHENTICATION`` 环境变量
声明（``password_based`` + ``built_in_database``），本脚本只负责把**用户**
写进内置数据库。

★ 5.8 的 API 认证方式
---------------------
5.x 的 REST API **不接受 Dashboard 账号密码**，只认 API Key/Secret
（否则返回 ``BAD_API_KEY_OR_SECRET``）。API Key 通过登录接口换取：

1. ``POST /api/v5/login``  →  拿 ``token``
2. ``Authorization: Bearer <token>``  →  后续请求

用法::

    python scripts/setup_emqx_auth.py            # 建用户 + 写 ACL
    python scripts/setup_emqx_auth.py --check    # 只检查当前状态
    python scripts/setup_emqx_auth.py --reset    # 先删掉同名用户再建

★ 安全：明文密码只从 .env 读，**不打印**；本脚本可重复执行（幂等）。
"""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

DASHBOARD = "http://127.0.0.1:18083"
API = DASHBOARD + "/api/v5"

#: MQTT 账号 → 主题规则。**与 deploy/emqx/oceanus_acl.conf 保持一致** ——
#: 那份是文件兜底，这份是内置库（可经 Dashboard 动态改，不用重启）。
#: (action, permission, topic)
ACLS = {
    "backend_service": [
        ("allow", "subscribe", "marine/+/+/event"),
        ("allow", "subscribe", "marine/+/+/telemetry"),
        ("allow", "subscribe", "marine/+/+/status"),
        ("allow", "subscribe", "robot/+/cmd/ack"),
        ("allow", "subscribe", "robot/+/task/progress"),
        ("allow", "publish", "robot/+/task"),
        ("allow", "publish", "robot/+/cmd"),
    ],
    "seasight": [
        ("allow", "subscribe", "#"),
        ("allow", "publish", "#"),
    ],
    "robot_device": [
        # ★ 收紧到具体 device_id：文件 ACL 里的 + 通配符会允许访问
        #   其它设备的主题，这里用精确 id 补上这一层。
        ("allow", "subscribe", "robot/RBT-ARM-01/task"),
        ("allow", "subscribe", "robot/RBT-ARM-01/cmd"),
        ("allow", "publish", "robot/RBT-ARM-01/cmd/ack"),
        ("allow", "publish", "robot/RBT-ARM-01/task/progress"),
        ("allow", "publish", "marine/lianjiang/RBT-ARM-01/telemetry"),
        ("allow", "publish", "marine/lianjiang/RBT-ARM-01/status"),
    ],
    "edge_device": [
        ("allow", "publish", "marine/+/+/telemetry"),
        ("allow", "publish", "marine/+/+/status"),
        ("allow", "subscribe", "marine/+/+/cmd"),
    ],
    "dashboard_viewer": [
        ("allow", "subscribe", "marine/+/+/telemetry"),
        ("allow", "subscribe", "marine/+/+/status"),
        ("allow", "subscribe", "robot/+/task/progress"),
        ("allow", "subscribe", "robot/+/cmd/ack"),
    ],
}

#: .env 里哪个键提供该账号的密码
ENV_KEYS = {
    "backend_service": "MQTT_PASSWORD",
    "seasight": None,          # 与 backend_service 同密码（兼容旧配置）
    "robot_device": "ARM_MQTT_PASSWORD",
    "edge_device": None,
    "dashboard_viewer": None,
}


def read_env(env_file=None):
    """读环境变量文件。

    ★ 云端用 ``--env-file .env.production``：云端的凭据与本机不同
      （``.env.production`` 里是 MQTT_BACKEND_PASSWORD 等），
      用本机 .env 会导致建出来的用户密码与云端 EMQX 不一致。
    """
    if env_file:
        path = env_file
    else:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".env")
    if not os.path.isfile(path):
        print("!! 找不到 %s" % path)
        print("   本机：先跑 python scripts/gen_local_secrets.py")
        print("   云端：需先准备 .env.production（gitignore）")
        sys.exit(2)
    print("使用凭据文件: %s" % path)
    env = {}
    for raw in io.open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def api(method, path, body=None, token=None, timeout=15):
    url = API + path if path.startswith("/") else path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read().decode("utf-8", "replace")
        return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def login(env):
    """登录 Dashboard 换 API token。"""
    user = env.get("MQTT_DASHBOARD_USER", "")
    pw = env.get("MQTT_DASHBOARD_PASSWORD", "")
    if not user or not pw:
        print("!! .env 里没有 MQTT_DASHBOARD_USER / MQTT_DASHBOARD_PASSWORD")
        return None
    status, body = api("POST", "/login", {"username": user, "password": pw})
    if status == 200 and isinstance(body, dict):
        return body.get("token") or body.get("access_token")
    print("!! 登录失败 HTTP %s: %s" % (status, body))
    return None


def list_users(token):
    """列出内置库里的 MQTT 用户。

    ★ 5.x 返回的是**分页信封** ``{"data": [...], "meta": {...}}``，
      不是裸数组（第一版按数组解析，导致"无法列出用户"的误判）。
      复数路径 ``/authentications/...`` 在 5.x 是 404。
    """
    status, body = api("GET", "/authentication/password_based:built_in_database/users",
                       token=token)
    if status != 200 or not isinstance(body, dict):
        return None
    return [u.get("user_id") for u in body.get("data", [])]


def create_user(token, user_id, password):
    return api("POST",
               "/authentication/password_based:built_in_database/users",
               {"user_id": user_id, "password": password,
                "is_superuser": False}, token)


def set_acls(token, user_id, rules):
    """写该用户的主题 ACL。

    5.x 的规则端点是 ``PUT /authorization/sources/built_in_database/rules``，
    body 里带 ``username`` 字段 —— **不带 username 的 per-user 路径
    （``.../users/{uid}``）在 5.8 是 404**（实测）。
    规则里 ``permission`` 只接受 ``subscribe`` 或 ``publish``；
    要"两者都允许"就写两条（5.x 不支持 ``all``）。
    """
    payload = []
    for action, permission, topic in rules:
        payload.append({"action": action, "permission": permission,
                        "topic": topic})
    return api("PUT", "/authorization/sources/built_in_database/rules",
               {"username": user_id, "rules": payload}, token)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只列出现有用户")
    ap.add_argument("--reset", action="store_true", help="先删同名用户再建")
    ap.add_argument("--env-file",
                    help="凭据文件（云端传 .env.production）")
    ap.add_argument("--dashboard-url",
                    help="EMQX Dashboard 地址（云端传 https://域名:18083 或"
                         " http://127.0.0.1:18083）")
    args = ap.parse_args()

    if args.dashboard_url:
        global DASHBOARD
        DASHBOARD = args.dashboard_url.rstrip("/")
    env = read_env(args.env_file)
    token = login(env)
    if not token:
        return 2
    print("✓ Dashboard 登录成功")

    existing = list_users(token)
    if existing is None:
        print("✗ 无法列出用户（认证链是否配好？）")
        print("  检查: docker compose exec emqx emqx ctl conf show authentication")
        return 1
    print("当前用户: %s" % (existing or "(空)"))

    if args.check:
        return 0

    # ★ 两套环境的变量名不同，**必须都试**（2026-10-05 修正）：
    #   本机 .env            : MQTT_PASSWORD / ARM_MQTT_PASSWORD
    #   云端 .env.production : MQTT_BACKEND_PASSWORD / MQTT_ROBOT_PASSWORD
    # 只找 ARM_MQTT_PASSWORD 会导致云端建出**密码错误的用户**，
    # 症状是设备连上后认证失败，而日志里看不出是变量名错配。
    def pick(*names):
        for n in names:
            v = env.get(n)
            if v:
                return v
        return ""

    svc_pw = pick("MQTT_PASSWORD", "MQTT_BACKEND_PASSWORD")
    arm_pw = pick("ARM_MQTT_PASSWORD", "MQTT_ROBOT_PASSWORD")
    passwords = {
        "backend_service": svc_pw,
        "seasight": svc_pw,
        "robot_device": arm_pw,
        # edge_device / dashboard_viewer 当前开发环境未使用，
        # 用后端密码占位（ACL 仍按最小权限生效）
        "edge_device": svc_pw,
        "dashboard_viewer": svc_pw,
    }

    ok = True
    print()
    print("--- 创建用户 ---")
    for user in ACLS:
        pw = passwords.get(user) or ""
        if len(pw) < 12:
            print("  %-18s 跳过（.env 里没有足够长的密码）" % user)
            ok = False
            continue
        if user in (existing or []):
            if not args.reset:
                print("  %-18s 已存在（--reset 可重建）" % user)
                continue
            st, _ = api("DELETE",
                        "/authentication/password_based:built_in_database/users/"
                        + user, token=token)
            print("  %-18s 已删除（HTTP %s）" % (user, st))
        st, body = create_user(token, user, pw)
        good = (st in (200, 201))
        print("  %-18s %s" % (user, "✓ HTTP %s" % st if good
                                else "✗ HTTP %s %s" % (st, body)))
        ok = ok and good

    print()
    print("--- 写主题 ACL ---")
    for user, rules in ACLS.items():
        st, body = set_acls(token, user, rules)
        good = (st in (200, 204))
        print("  %-18s %d 条规则 %s" % (
            user, len(rules),
            "✓" if good else "✗ HTTP %s %s" % (st, body)))
        ok = ok and good

    print()
    print("=" * 60)
    print("完成" if ok else "有项失败，见上")
    print("下一步：python scripts/check_mqtt_auth.py  验证认证与 ACL")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
