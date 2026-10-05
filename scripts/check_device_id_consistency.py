#!/usr/bin/env python3
"""device_id 跨文件一致性守护 —— 防止「平台派的单 bridge 收不到」。

★ 为什么这个守护是必需的（2026-10-05 实际踩过）
--------------------------------------------------
平台按 ``robot/{robot_id}/task`` 派单，bridge 订阅
``robot/{device_id}/task``。**协议完全一致，唯一差别是 ID 值。**

我第一版把 bridge 配成 ``RBT-ARM-01``，而平台的机器人来自
``backend/db/init/02_seed.sql`` 的 t_device 表，只有
``RBT-001 / RBT-002 / RBT-003``。结果是：

  - bridge 订阅 ``robot/RBT-ARM-01/task``  → broker **接受订阅，不报错**
  - 平台发布 ``robot/RBT-001/task``        → broker **接受发布，不报错**
  - 两者**永不相交**，bridge 一条消息都收不到

**最难查的一点：任何地方都不报错。** 连接正常、订阅正常、发布正常、
ACL 正常（两边各自都在自己的白名单里），只是永远对不上。
没有这个守护测试，这类错配会一路活到路演现场。

三处必须一致
------------
1. ``edge/arm_bridge/config.yaml`` 的 ``device_id``
2. ``deploy/emqx/oceanus_acl.conf`` 里 ``robot_device`` 的主题规则
3. ``backend/db/init/02_seed.sql`` 里 t_device 的 device_id

用法::

    python scripts/check_device_id_consistency.py

退出码 0 = 一致；1 = 不一致（会打印具体差在哪）。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BRIDGE_CONFIG = ROOT / "edge" / "arm_bridge" / "config.yaml"
ACL_CONF = ROOT / "deploy" / "emqx" / "oceanus_acl.conf"
SEED_SQL = ROOT / "backend" / "db" / "init" / "02_seed.sql"


def read_bridge_device_id():
    """从 config.yaml 取 device_id（只认行首的键，避免命中注释）。"""
    if not BRIDGE_CONFIG.is_file():
        return None
    in_mqtt = False
    for raw in BRIDGE_CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("mqtt:"):
            in_mqtt = True
            continue
        # 顶层键（无缩进）
        if not line.startswith((" ", "\t")):
            in_mqtt = False
            if line.startswith("device_id:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def read_seed_robot_ids():
    """从种子 SQL 里取 t_device 的 robot 类 device_id。"""
    if not SEED_SQL.is_file():
        return []
    text = SEED_SQL.read_text(encoding="utf-8")
    ids = set()
    for m in re.finditer(r"\(\s*'(RBT-\d+)'\s*,\s*'robot'", text):
        ids.add(m.group(1))
    return sorted(ids)


def read_acl_robot_ids():
    """从 ACL 里取 robot_device 被允许的设备号（``robot/<id>/...``）。

    ★ 只看**实际规则行**（以 ``{`` 开头、不是 ``%%`` 注释），
      且该行的 username 必须是 robot_device —— 别的账号的规则
      （backend_service 的 ``#``、edge_device 的 ``+``）不算。
    """
    if not ACL_CONF.is_file():
        return set()
    ids = set()
    for raw in ACL_CONF.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("{") or "robot_device" not in line:
            continue
        for m in re.finditer(r'"robot/([^/"]+)/', line):
            ids.add(m.group(1))
    return ids


def main() -> int:
    print("=" * 66)
    print("device_id 跨文件一致性检查")
    print("=" * 66)

    bridge_id = read_bridge_device_id()
    seed_ids = read_seed_robot_ids()
    acl_ids = read_acl_robot_ids()

    print("  bridge config.yaml device_id : %s" % (bridge_id or "(取不到)"))
    print("  种子数据里的 robot device_id : %s" % (", ".join(seed_ids) or "(无)"))
    print("  ACL 里允许的设备号           : %s" % (", ".join(sorted(acl_ids)) or "(无)"))

    problems = []

    if not bridge_id:
        problems.append("config.yaml 里取不到 device_id")
    else:
        if seed_ids and bridge_id not in seed_ids:
            problems.append(
                "★ bridge 的 device_id=%s 不在平台种子数据 %s 里 —— "
                "平台永远不会派单给它" % (bridge_id, ", ".join(seed_ids)))
        # ACL 里可能含 SELFTEST（自测用），排除后再比
        real_acl = {i for i in acl_ids if i != "SELFTEST"}
        if real_acl and bridge_id not in real_acl:
            problems.append(
                "★ ACL 允许的设备号 %s 不含 bridge 的 %s —— "
                "bridge 会被 ACL 拒收自己的任务"
                % (", ".join(sorted(real_acl)), bridge_id))

    print()
    if problems:
        print("✗ 发现 %d 处不一致：" % len(problems))
        for p in problems:
            print("   " + p)
        print()
        print("修法：三者必须取同一个值。权威来源是")
        print("  backend/db/init/02_seed.sql 的 t_device 表 ——")
        print("那是平台实际派单时用的 ID。")
        print("若要为机械臂单独建档，先在 02_seed.sql 加一行 t_device，")
        print("再同步 edge/arm_bridge/config.yaml 与 deploy/emqx/oceanus_acl.conf。")
        return 1

    print("✓ 三处一致：平台会派单给 bridge，ACL 也放行它")
    if "SELFTEST" in acl_ids:
        print("  （ACL 里另有 SELFTEST，那是 selftest_bridge_loop.py 专用，正常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
