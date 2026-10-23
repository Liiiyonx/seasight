"""派单 → 视觉闭环 → 放入料槽：**桥接层契约 + 真驱动** 的整体自测（全 mock）。

与 `test_grasp_place.py` 的分工：

* 那份锁**闭环内部**的动作序列（坐标、示教位姿、拒绝开工）；
* 这份锁**平台那侧看到的东西**：MQTT 派单进来 → ack → progress
  collecting/done → telemetry 里带真实回读的 servos。

用的是真正的 `OceanusVisionArmDriver`（不是 SimulatedArmDriver），所以它同时
证明了 README 里那句「平台侧零改动、换驱动不动契约」不是口头承诺：
同一个 `ArmBridge` + 同一套报文，底下换成视觉闭环驱动照样跑通。

⚠️ 证据口径：mock 合成画面 + 内存 transport，只证明**协议与闭环逻辑**可复现，
不构成物理拾取或现场验收证据。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
GRASP = HERE.parent
EDGE = GRASP.parent
for _p in (str(EDGE), str(GRASP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from arm_bridge.bridge import ArmBridge                                  # noqa: E402
from device_sim.protocol import FakeClock, SequentialIdFactory           # noqa: E402
from device_sim.transport import MemoryTransport                         # noqa: E402
from grasp.vision_driver import OceanusVisionArmDriver                   # noqa: E402

DEVICE = "RBT-ARM-01"
SITE = "lianjiang"

#: 测试用配置：把闭环周期压到毫秒级，跑一次派单只要几秒
CFG: dict[str, Any] = {
    "site_id": SITE,
    "device_id": DEVICE,
    "web": {"mock": True},
    "arm": {"gripper_open": 500, "gripper_close": 200},
    "vision": {"input_size": 640, "conf_threshold": 0.5, "target_class": "plastic_bag"},
    "agent": {
        "frame_period_ms": 1,
        "max_retry": 1,
        "lock_min_hits": 1,
        "work_radius": 0.25,
        # 料槽放置点：与 config.yaml 同口径（现场由作业台示教覆盖）
        "place": {"coord": [0.16, 0.02], "settle_s": 0.0},
    },
}


def dispatch_payload(task_id: str = "tsk_arm_0001") -> dict[str, Any]:
    return {
        "command_id": f"cmd_{task_id}",
        "device_id": DEVICE,
        "seq": 1,
        "action": "dispatch",
        "issued_at": 0.0,
        "expires_at": 30.0,
        "params": {
            "task_id": task_id,
            "target": {"lng": 119.6531, "lat": 26.3867},
            "priority": 1,
        },
    }


def test_dispatch_drives_vision_pick_and_reports_placed(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(CFG, allow_unicode=True), encoding="utf-8")

    driver = OceanusVisionArmDriver(
        config_path=str(cfg_path),
        mock=True,
        # 指到一个不存在的标定文件：闭环因此装载演示标定（含演示放置点），
        # 不会因为开发机上恰好有 calibration.json 而变得不确定
        calibration_path=str(tmp_path / "no_calibration.json"),
    )
    transport = MemoryTransport()
    bridge = ArmBridge(DEVICE, SITE, driver, transport,
                       clock=FakeClock(0.0), id_factory=SequentialIdFactory("ack"))
    bridge.start()

    transport.deliver(bridge.task_topic, dispatch_payload(), qos=1)

    # ---- 平台侧看到的：ACK → collecting → done ----
    acks = [p for t, p, _ in transport.published if t == bridge.ack_topic]
    assert len(acks) == 1 and acks[0]["accepted"] is True
    progress = [p for t, p, _ in transport.published if t == bridge.progress_topic]
    assert [p["status"] for p in progress] == ["collecting", "done"]
    assert progress[-1]["collected_weight"] == 0.05
    # 默认 review_result=recheck：没人工核对照片前不写 confirmed（口径纪律）
    assert progress[-1]["review_result"] == "recheck"

    # ---- 执行侧真的走完了「识别 → 夹取 → 放入料槽」 ----
    outcome = driver.last_outcome or {}
    assert outcome.get("ok") is True, outcome
    assert outcome.get("placed") is True, f"没放进料槽：{outcome}"
    assert outcome.get("target_gone") and outcome.get("grip_stalled")

    # ---- 放置点已就绪（否则闭环根本不开工） ----
    assert driver.info()["place"]["ready"] is True

    # ---- telemetry 带逐舵机回读（"机械臂真的动了"的硬证据字段） ----
    telemetry = [p for t, p, _ in transport.published if t == bridge.telemetry_topic]
    assert telemetry, "没有任何 telemetry 报文"
    assert telemetry[-1]["task_id"] == "tsk_arm_0001"
    assert telemetry[-1]["status"] == "idle"          # 一单做完即回 idle
    assert telemetry[-1]["servos"], "telemetry 里没有舵机回读"


def test_dispatch_is_refused_when_no_place_point(tmp_path: Path) -> None:
    """没有放置点：驱动明确抛错 → 桥接层报 pick_failed，绝不假装完成。"""
    cfg = dict(CFG)
    cfg["agent"] = {**CFG["agent"], "place": {}}      # 既无 coord 也无 pose
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    driver = OceanusVisionArmDriver(
        config_path=str(cfg_path),
        mock=True,
        calibration_path=str(tmp_path / "no_calibration.json"),
    )
    # 演示标定会自带演示放置点 —— 这里显式清掉，模拟"真机上还没示教"
    driver.cal.place = None
    driver.loop.cal.place = None

    transport = MemoryTransport()
    bridge = ArmBridge(DEVICE, SITE, driver, transport,
                       clock=FakeClock(0.0), id_factory=SequentialIdFactory("ack"))
    bridge.start()
    transport.deliver(bridge.task_topic, dispatch_payload("tsk_no_place"), qos=1)

    progress = [p for t, p, _ in transport.published if t == bridge.progress_topic]
    assert progress, "至少要有 collecting 报文"
    assert progress[-1]["status"] == "done"
    assert progress[-1]["review_result"] == "recheck"
    assert progress[-1]["collected_weight"] == 0.0
    # 桥接层把它记成 pick_failed（而不是一次"完成"），平台侧据此派人工复核
    assert bridge.counter["pick_failed"] == 1
    failed = [e for e in bridge.events if e["kind"] == "pick_failed"]
    assert failed and "料槽放置点" in failed[0]["detail"]["error"]
