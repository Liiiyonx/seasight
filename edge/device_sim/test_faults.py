"""WP-14 故障注入测试 —— 确定性场景与报告。

验证：
- 固定随机种子 + 相同场景 → 逐字节相同的 JSON 报告（确定性）；
- 噪声开启时不同种子产生不同噪声哈希、同种子可复现；
- 注入故障在遥测中被观测、清除后进入恢复计数，
  ``device_fault_recovery_rate`` 语义正确（无故障时为 None）；
- ACK 幂等统计（重复 / 乱序 / 超时）在场景层正确。

全部在内存 transport + 假时钟 + 确定性 ID 下运行，不依赖公网或真实设备。
证据等级：E1/E2。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
sys.path.insert(0, str(EDGE))

from device_sim.device import TwinConfig  # noqa: E402
from device_sim.faults import DeviceScenario, run_device_scenario  # noqa: E402

DEVICE = "RBT-001"
TARGET = {"lng": 119.66, "lat": 26.39}


def build_scenario() -> DeviceScenario:
    sc = DeviceScenario("base")
    sc.cmd(1.0, "dispatch", task_id="tsk_001", target=TARGET)
    sc.cmd(3.0, "pause")
    sc.cmd(4.0, "resume")
    sc.inject(5.0, "gps_lost")
    sc.recover(6.0, "gps_lost")
    sc.cmd(7.0, "return_home")
    return sc


# ----------------------------------------------------------------------
# 确定性
# ----------------------------------------------------------------------
def test_scenario_deterministic_same_seed() -> None:
    scenario = build_scenario()
    r1 = run_device_scenario(DEVICE, scenario, seed=42, end_at=8.0)
    r2 = run_device_scenario(DEVICE, scenario, seed=42, end_at=8.0)
    assert r1 == r2
    assert r1.to_json() == r2.to_json()


def test_noise_seed_changes_hash_and_same_seed_reproduces() -> None:
    scenario = build_scenario()
    r1 = run_device_scenario(DEVICE, scenario, seed=1, noise=True, end_at=8.0)
    r2 = run_device_scenario(DEVICE, scenario, seed=2, noise=True, end_at=8.0)
    r3 = run_device_scenario(DEVICE, scenario, seed=1, noise=True, end_at=8.0)
    assert r1.data["noise"] is True
    assert r1.data["noise_hash"] is not None
    assert r1.data["noise_hash"] != r2.data["noise_hash"]  # 种子影响输出
    assert r1.data["noise_hash"] == r3.data["noise_hash"]  # 同种子可复现


def test_report_json_serializable() -> None:
    report = run_device_scenario(DEVICE, build_scenario(), seed=7, end_at=8.0)
    parsed = json.loads(report.to_json())
    assert parsed["device_id"] == DEVICE
    assert parsed["seed"] == 7
    assert parsed["scenario_id"] == "base"


# ----------------------------------------------------------------------
# 故障注入 / 观测 / 恢复
# ----------------------------------------------------------------------
def test_fault_inject_recover_cycle_in_report() -> None:
    scenario = DeviceScenario("cycle")
    scenario.cmd(1.0, "dispatch", task_id="t1", target=TARGET)
    scenario.inject(2.0, "gps_lost")
    scenario.recover(4.0, "gps_lost")
    report = run_device_scenario(DEVICE, scenario, end_at=5.0)
    data = report.data
    faults = data["faults_injected"]
    assert len(faults) == 1
    assert faults[0]["fault"] == "gps_lost"
    assert faults[0]["recovered"] is True
    assert data["faults_recovered"] == 1
    assert data["device_fault_recovery_rate"] == 1.0
    assert data["fault_detected_telemetry"] >= 1
    assert data["fault_observations"]["gps_lost"]["samples"] >= 1


def test_fault_injected_but_not_recovered_rate() -> None:
    scenario = DeviceScenario("no_recover")
    scenario.inject(1.0, "gps_lost")
    report = run_device_scenario(DEVICE, scenario, end_at=2.0)
    data = report.data
    assert data["faults_injected"][0]["recovered"] is False
    assert data["faults_recovered"] == 0
    assert data["device_fault_recovery_rate"] == 0.0


def test_no_faults_recovery_rate_is_none() -> None:
    scenario = DeviceScenario("clean")
    scenario.cmd(1.0, "pause")
    report = run_device_scenario(DEVICE, scenario, end_at=2.0)
    assert report.recovery_rate is None
    assert report.data["faults_injected"] == []
    assert report.data["ack_outcomes"]["new"] == 1


def test_emergency_stop_fault_recovered_by_command() -> None:
    scenario = DeviceScenario("estop_fault")
    scenario.inject(1.0, "emergency_stop")
    scenario.cmd(2.0, "resume")  # 命令驱动恢复
    report = run_device_scenario(DEVICE, scenario, end_at=3.0)
    data = report.data
    assert data["faults_injected"][0]["fault"] == "emergency_stop"
    assert data["faults_injected"][0]["recovered"] is True
    assert data["device_fault_recovery_rate"] == 1.0
    # 恢复后遥测故障码回到 none
    assert data["fault_observations"]["none"]["samples"] >= 1


def test_communication_lost_buffers_and_flushes() -> None:
    scenario = DeviceScenario("net")
    scenario.inject(1.0, "communication_lost")
    scenario.cmd(2.0, "pause")  # 离线期间下发 → 设备收不到（平台侧无 ACK）
    scenario.recover(3.0, "communication_lost")
    report = run_device_scenario(DEVICE, scenario, end_at=4.0)
    data = report.data
    assert data["telemetry_buffered"] >= 2
    assert data["telemetry_flushed"] >= 2
    assert data["faults_injected"][0]["fault"] == "communication_lost"
    assert data["faults_injected"][0]["recovered"] is True


def test_battery_critical_forced_fault_in_report() -> None:
    scenario = DeviceScenario("batt")
    scenario.inject(1.0, "battery_critical")
    scenario.recover(3.0, "battery_critical")
    report = run_device_scenario(DEVICE, scenario, end_at=4.0)
    data = report.data
    obs = data["fault_observations"]["battery_critical"]
    assert obs["samples"] >= 1
    assert data["device_fault_recovery_rate"] == 1.0


# ----------------------------------------------------------------------
# ACK 幂等统计（场景层）
# ----------------------------------------------------------------------
def test_ack_outcomes_duplicate_fault() -> None:
    scenario = DeviceScenario("dup_ack")
    scenario.inject(1.0, "duplicate_ack")
    scenario.cmd(2.0, "pause")
    report = run_device_scenario(DEVICE, scenario, end_at=3.0)
    assert report.ack_outcomes["new"] == 1
    assert report.ack_outcomes["duplicate"] == 1
    assert report.data["acks_published"] == 2


def test_ack_outcomes_out_of_order_fault() -> None:
    scenario = DeviceScenario("ooo_ack")
    scenario.inject(1.0, "out_of_order_ack")
    scenario.cmd(2.0, "pause")
    scenario.cmd(2.0, "pause")
    report = run_device_scenario(DEVICE, scenario, end_at=3.0)
    assert report.ack_outcomes["new"] == 1
    assert report.ack_outcomes["out_of_order"] == 1


def test_ack_outcomes_timeout_late() -> None:
    scenario = DeviceScenario("late_ack")
    scenario.cmd(1.0, "pause", ttl=2.0)  # 有效期至 t=3
    report = run_device_scenario(
        DEVICE,
        scenario,
        end_at=5.0,
        config=TwinConfig(command_processing_delay=3.0),  # t=4 才处理 → 已过期
    )
    assert report.ack_outcomes["late"] == 1
    assert report.data["commands_rejected"] == 1


def test_ack_timeout_fault_no_ack_in_report() -> None:
    scenario = DeviceScenario("no_ack")
    scenario.inject(1.0, "ack_timeout")
    scenario.cmd(2.0, "pause")
    report = run_device_scenario(DEVICE, scenario, end_at=3.0)
    assert report.data["acks_published"] == 0
    assert report.data["acks_suppressed"] == 1
    assert sum(report.ack_outcomes.values()) == 0  # 平台侧一条回执都没收到


# ----------------------------------------------------------------------
# 命令闭环（场景层）
# ----------------------------------------------------------------------
def test_emergency_stop_priority_and_recovery_in_report() -> None:
    scenario = DeviceScenario("estop")
    scenario.cmd(1.0, "dispatch", task_id="t1", target=TARGET)
    scenario.cmd(2.0, "pause")
    scenario.cmd(2.0, "resume")
    scenario.cmd(2.0, "emergency_stop")
    scenario.cmd(3.0, "resume")  # 急停已恢复后再 resume → noop
    report = run_device_scenario(DEVICE, scenario, end_at=4.0)
    data = report.data
    assert data["commands_issued"] == 5
    assert data["commands_processed"] == 5
    # 急停最先执行：pause 被急停拦截（rejected），resume 作为恢复命令放行。
    # 回执按处理顺序发布（急停 seq4 先于 seq2/seq3），平台据此识别出乱序 ACK。
    assert data["commands_rejected"] == 1
    assert data["ack_outcomes"]["new"] == 3
    assert data["ack_outcomes"]["out_of_order"] == 2


def test_expired_command_scenario_rejected() -> None:
    scenario = DeviceScenario("expired")
    scenario.cmd(1.0, "pause", ttl=0.5)  # 有效期至 t=1.5
    report = run_device_scenario(
        DEVICE, scenario, end_at=3.0, config=TwinConfig(command_processing_delay=2.0)
    )
    data = report.data
    assert data["commands_rejected"] == 1
    assert data["ack_outcomes"]["late"] == 1
