"""MQTT 主题树设计规范。

主题结构（3~4 层，静态结构，变量放 payload）：

    marine/{site_id}/{device_id}/event        垃圾检测事件   QoS1
    marine/{site_id}/{device_id}/telemetry    心跳/电量/仓容 QoS0
    marine/{site_id}/{device_id}/status       online/offline retained QoS1
    marine/{site_id}/{device_id}/cmd          下发指令(云→边) QoS1
    marine/{site_id}/{device_id}/cmd/ack      指令回执        QoS1
    robot/{robot_id}/task                     派单下发        QoS1
    robot/{robot_id}/task/progress            作业回传        QoS1
    robot/{robot_id}/cmd/ack                  任务确认        QoS1

规范要点：
1. 低层用 + 通配订阅（marine/+/+/event），禁止 # 大范围订阅
2. 禁止前导斜杠 / 开头
3. 全小写
4. device_id 作 ACL 隔离维度（EMQX 按 clientid 前缀授权）

QoS 选择：
- 遥测（telemetry）：QoS0 —— 高频、丢一两帧无所谓
- 事件与指令：QoS1 —— 必须确保送达，但要幂等去重
- 不用 QoS2 —— 四次握手开销大，备赛不值得

LWT（遗嘱消息）用于离线检测：
    连接时注册 will_topic = marine/{site}/{dev}/status
    will_payload = {"online": false, "ts": ...}
    retain = true, qos = 1
    连上后主动发一条 retained {"online": true}

retain 只用于状态类主题，绝不用于高频事件流
（会让 broker 膨胀，且新订阅者会收到过期告警）。
"""

from __future__ import annotations


class Topics:
    """主题构造函数（保证命名一致性，避免各处手写字符串）。"""

    PREFIX = "marine"

    # ---------- 上行：设备 → 平台 ----------
    @staticmethod
    def event(site_id: str, device_id: str) -> str:
        """垃圾检测事件上报。"""
        return f"marine/{site_id}/{device_id}/event"

    @staticmethod
    def telemetry(site_id: str, device_id: str) -> str:
        """设备遥测（心跳、电量、仓容）。"""
        return f"marine/{site_id}/{device_id}/telemetry"

    @staticmethod
    def status(site_id: str, device_id: str) -> str:
        """设备在线状态（retained）。"""
        return f"marine/{site_id}/{device_id}/status"

    # ---------- 下行：平台 → 设备 ----------
    @staticmethod
    def cmd(site_id: str, device_id: str) -> str:
        """通用指令下发。"""
        return f"marine/{site_id}/{device_id}/cmd"

    @staticmethod
    def cmd_ack(site_id: str, device_id: str) -> str:
        """指令回执。"""
        return f"marine/{site_id}/{device_id}/cmd/ack"

    # ---------- 机器人专用 ----------
    @staticmethod
    def robot_task(robot_id: str) -> str:
        """派单下发。"""
        return f"robot/{robot_id}/task"

    @staticmethod
    def robot_progress(robot_id: str) -> str:
        """作业数据回传。"""
        return f"robot/{robot_id}/task/progress"

    @staticmethod
    def robot_ack(robot_id: str) -> str:
        """任务确认。"""
        return f"robot/{robot_id}/cmd/ack"

    # ---------- 订阅通配（后端使用） ----------
    EVENT_WILDCARD = "marine/+/+/event"
    TELEMETRY_WILDCARD = "marine/+/+/telemetry"
    STATUS_WILDCARD = "marine/+/+/status"
    ROBOT_PROGRESS_WILDCARD = "robot/+/task/progress"
    ROBOT_ACK_WILDCARD = "robot/+/cmd/ack"


# 主题 → 处理器的映射建议（供 handlers 层使用）
SUBSCRIBE_PLAN: dict[str, str] = {
    Topics.EVENT_WILDCARD: "handle_event",
    Topics.TELEMETRY_WILDCARD: "handle_telemetry",
    Topics.STATUS_WILDCARD: "handle_device_status",
    Topics.ROBOT_PROGRESS_WILDCARD: "handle_robot_progress",
    Topics.ROBOT_ACK_WILDCARD: "handle_robot_ack",
}
