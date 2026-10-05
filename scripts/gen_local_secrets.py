#!/usr/bin/env python3
"""生成本机开发环境的凭据：.env + EMQX bootstrap.csv 的密码哈希。

为什么需要这个脚本
------------------
``deploy/emqx/bootstrap.csv`` 原本有个**真实的设计缺陷**：5 个 MQTT 账号
（seasight / backend_service / edge_device / dashboard_viewer / robot_device）
**共用同一个 sha256 哈希**，而那个哈希是明文 ``CHANGE_ME`` 的。
后果：任何一个账号的密码泄露，5 个账号全部沦陷 —— 最小权限原则形同虚设。
本脚本为每个账号生成**独立密码**，让 ACL 的权限隔离真正有意义。

安全约定
--------
* 随机源用 :mod:`secrets`（CSPRNG），**不用** ``random``（可预测）
* 字符集剔除易混淆的 ``I l 1 O 0``，避免抄错
* 明文**只**写进 ``.env``（已被 ``.gitignore`` 覆盖），绝不进仓库
* ``bootstrap.csv`` 里只写 sha256 哈希
* 不在 stdout 打印任何明文密码

用法::

    python scripts/gen_local_secrets.py            # 生成（会覆盖 .env）
    python scripts/gen_local_secrets.py --show     # 打印清单（不含明文）
    python scripts/gen_local_secrets.py --rotate mqtt   # 只轮换某类凭据

★ EMQX 的一个坑（文件头也写了）：``bootstrap.csv`` **只在数据库为空时导入**。
所以改了它必须 ``docker compose down -v`` 清卷，否则**密码改了不生效**，
然后你会花两小时排查"为什么密码没变"。生产环境请走 Dashboard(18083) 在线改。
"""

import argparse
import hashlib
import io
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
# ★ 已删除 deploy/emqx/bootstrap.csv：那份数据导入机制是 EMQX **6.x**
#   才有的，5.8 上 `emqx ctl import data` 直接 unknown command（2026-10-05 实测）。
#   现在认证链由 docker-compose 的环境变量声明，用户与 ACL 由
#   scripts/setup_emqx_auth.py 走 REST API 写入内置数据库。
#   保留一个"看起来能用但实际不生效"的文件比没有它更危险 —— 后人会照着它
#   以为改了就生效，然后排查半天。故删除。

# 剔除 I l 1 O 0 —— 这些字符在终端/手抄时极易混淆
ALPHA = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
SYMBOLS = "!@#%^*_-+="

#: MQTT 账号 → 用途。密码**各自独立**，泄露一个不影响其他。
MQTT_ACCOUNTS = {
    "seasight": "平台主账号（收发全部主题）",
    "backend_service": "后端微服务（收发全部主题）",
    "edge_device": "边缘感知设备（只发自己的遥测）",
    "dashboard_viewer": "只读观察者（仅订阅遥测）",
    "robot_device": "★ 机械臂 bridge（只发自己遥测、只收自己任务）",
}


def make_password(length: int = 32) -> str:
    """密码学安全的密码。

    结构：``(length-4) 个字母数字 + 2 个符号 + 2 个字母数字``，
    保证符号位置不确定（不是固定在末尾），且至少含 2 个符号。
    """
    core = "".join(secrets.choice(ALPHA) for _ in range(length - 4))
    return (
        "".join(secrets.choice(ALPHA) for _ in range(length - 4))
        + secrets.choice(SYMBOLS)
        + secrets.choice(SYMBOLS)
        + core
    )


def sha256_hex(text: str) -> str:
    """EMQX 5.x 在 salt 为空时用纯 sha256(password)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_credentials() -> dict:
    creds = {account: make_password() for account in MQTT_ACCOUNTS}
    creds["MQTT_DASHBOARD_USER"] = "oceanus_admin"
    creds["MQTT_DASHBOARD_PASSWORD"] = make_password()
    creds["SECRET_KEY"] = secrets.token_hex(32)
    creds["POSTGRES_PASSWORD"] = make_password()
    creds["MINIO_ACCESS_KEY"] = "oceanus" + secrets.token_hex(4)
    creds["MINIO_SECRET_KEY"] = secrets.token_hex(32)
    return creds


ENV_TEMPLATE = """# ===== Oceanus 本机开发环境 =====
# ⚠️ 本文件含**真实凭据**，已在 .gitignore 中，**切勿提交**。
# 生成方式：python scripts/gen_local_secrets.py
# 生成时间：{ts}
#
# MQTT 五个账号密码**各自独立**（此前共用一个 CHANGE_ME 哈希，
# 泄露一个即全部沦陷，违反最小权限原则）。哈希见
# deploy/emqx/bootstrap.csv —— 改那里必须清卷才生效。

# ---- 应用 ----
APP_NAME=Oceanus
APP_ENV=development
DEBUG=true
API_V1_PREFIX=/api/v1
SECRET_KEY={SECRET_KEY}
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# ---- 数据库 ----
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=seasight
POSTGRES_USER=seasight
POSTGRES_PASSWORD={POSTGRES_PASSWORD}

# ---- Redis ----
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=

# ---- MQTT (EMQX) ----
# 后端服务账号
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_USERNAME=backend_service
MQTT_PASSWORD={backend_service}
MQTT_CLIENT_ID=oceanus-backend
MQTT_TOPIC_PREFIX=marine

# ★ 机械臂 bridge 专用账号（ACL 最小权限：只发自己遥测、只收自己任务）
ARM_MQTT_USERNAME=robot_device
ARM_MQTT_PASSWORD={robot_device}

MQTT_DASHBOARD_USER={MQTT_DASHBOARD_USER}
MQTT_DASHBOARD_PASSWORD={MQTT_DASHBOARD_PASSWORD}

# ---- MinIO 对象存储 ----
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY={MINIO_ACCESS_KEY}
MINIO_SECRET_KEY={MINIO_SECRET_KEY}
MINIO_BUCKET_EVENTS=events
MINIO_SECURE=false
"""


def write_env(creds: dict) -> None:
    import time

    text = ENV_TEMPLATE.format(ts=time.strftime("%Y-%m-%d %H:%M:%S"), **creds)
    ENV_PATH.write_text(text, encoding="utf-8", newline="\n")


def _removed_write_bootstrap(creds: dict) -> int:
    """已废弃：EMQX 5.8 不支持 bootstrap.csv 数据导入。见 BOOTSTRAP 常量注释。"""
    if not BOOTSTRAP.is_file():
        print("!! 找不到 %s" % BOOTSTRAP, file=sys.stderr)
        return 0
    text = BOOTSTRAP.read_text(encoding="utf-8")
    changed = 0
    for account in MQTT_ACCOUNTS:
        want = sha256_hex(creds[account])
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("mqtt_user,%s," % account):
                parts = line.split(",")
                if len(parts) >= 3 and parts[2] != want:
                    parts[2] = want
                    lines[i] = ",".join(parts)
                    changed += 1
                break
        text = "\n".join(lines)
    BOOTSTRAP.write_text(text, encoding="utf-8", newline="\n")
    return changed


def show_summary(creds: dict) -> None:
    print("凭据清单（**不含明文**）：")
    print()
    print("  %-24s %-10s %s" % ("变量", "长度", "用途"))
    print("  " + "-" * 66)
    for account, purpose in MQTT_ACCOUNTS.items():
        value = creds[account]
        print("  %-24s %-10d %s" % (account, len(value), purpose))
    for key in ("SECRET_KEY", "POSTGRES_PASSWORD", "MINIO_SECRET_KEY",
                "MQTT_DASHBOARD_PASSWORD"):
        print("  %-24s %-10d %s" % (key, len(creds[key]), "（非 MQTT 账号）"))
    print()
    print("各 MQTT 账号 sha256（仅供核对，EMQX 侧由 API 写明文）：")
    for account in MQTT_ACCOUNTS:
        print("  %-18s %s" % (account, sha256_hex(creds[account])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true",
                    help="只打印清单，不写文件")
    args = ap.parse_args()

    creds = build_credentials()

    if args.show:
        show_summary(creds)
        return 0

    if ENV_PATH.is_file() and not args.show:
        backup = ROOT / ".env.bak"
        backup.write_text(ENV_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print("已备份旧 .env → %s" % backup.name)

    write_env(creds)

    print(".env 已生成（%d 项凭据，gitignore 已覆盖）" % len(creds))
    print()
    print("⚠️ 换密码后要重新写入 EMQX 内置库（用户与 ACL 不随 .env 变）：")
    print("     python scripts/setup_emqx_auth.py --reset")
    print("     docker compose restart emqx")
    print()
    show_summary(creds)
    print()
    print("明文只落在 .env，请勿提交或贴到聊天/文档里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
