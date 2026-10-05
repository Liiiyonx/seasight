"""厂商示教格式与遥测的回归测试。

覆盖两个真实缺陷（都在 2026-10-05 精读厂商资料后才发现）：

1. ``_load_sequence_file`` 原本只接受 ``{"duration","positions"}`` 格式，
   而厂商示教程序 ``案例5 示教记录实现/bus_servo_record.py`` 写出的是
   **扁平的位置列表** ``[1000, 940]`` —— 直接喂进去会解析失败。
2. ``status()`` 调``Board.get_battery()`` 只调一次，而该方法是非阻塞的
   （队列空即返回 None），所以 battery 永远是构造时的初值 —— 一个
   "声明了却没实现"的静默缺陷。

这些用假Board 验证，不需要真机。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arm_bridge.drivers import (  # noqa: E402
    DeviceMode,
    HiwonderBusServoArmDriver,
    Position,
)


class FakeBoard:
    """模拟厂商 SDK 的 Board，只实现我们用到的部分。"""

    def __init__(self, *, battery_vin: int | None = 7400,
                 servo_values: dict[int, dict[str, int]] | None = None,
                 battery_empty_first: int = 0) -> None:
        self._battery_vin = battery_vin
        self._servo_values = servo_values or {}
        # 前 N 次 get_battery 返回 None（模拟异步队列尚未收到回包）
        self._battery_empty_first = battery_empty_first
        self._battery_calls = 0
        self.reception = False
        self.stopped: list[int] = []
        self.torque: list[tuple[int, bool]] = []
        self.moves: list[tuple[float, list]] = []

    # --- 我们驱动用到的 ---
    def enable_reception(self, on: bool = True) -> None:
        self.reception = on

    def get_battery(self):
        self._battery_calls += 1
        if self._battery_calls <= self._battery_empty_first:
            return None
        return self._battery_vin

    def bus_servo_read_vin(self, sid):
        return [self._servo_values.get(sid, {}).get("vin", 11000)]

    def bus_servo_read_temp(self, sid):
        return [self._servo_values.get(sid, {}).get("temp", 42)]

    def bus_servo_read_position(self, sid):
        return [self._servo_values.get(sid, {}).get("position", 512)]

    def bus_servo_enable_torque(self, sid, on):
        self.torque.append((sid, on))

    def bus_servo_set_position(self, duration, positions):
        self.moves.append((duration, positions))

    def bus_servo_stop(self, sids):
        self.stopped.extend(sids)


def _driver(board: FakeBoard, **kw) -> HiwonderBusServoArmDriver:
    """构造一个已注入 FakeBoard 的驱动，绕过真实串口。"""
    d = HiwonderBusServoArmDriver(
        pick_positions=[[1, 500]],
        **kw,
    )
    d._board = board
    return d


# ---------- ① 厂商扁平格式的序列文件 ----------


def test_vendor_flat_position_file_is_accepted(tmp_path: Path) -> None:
    """厂商示教程序写出的 [1000, 940] 必须能被直接回放。"""
    f = tmp_path / "servo_positions.json"
    f.write_text(json.dumps([1000, 940]), encoding="utf-8")

    seq = HiwonderBusServoArmDriver._load_sequence_file(str(f))
    assert len(seq) == 2, seq
    assert seq[0]["positions"] == [[1, 1000]]
    assert seq[1]["positions"] == [[1, 940]]


def test_vendor_flat_file_with_steps_wrapper(tmp_path: Path) -> None:
    """{'steps': [1000, 940]} 这种包装也要能吃。"""
    f = tmp_path / "wrapped.json"
    f.write_text(json.dumps({"steps": [1000, 940]}), encoding="utf-8")
    seq = HiwonderBusServoArmDriver._load_sequence_file(str(f))
    assert len(seq) == 2


def test_our_own_format_still_works(tmp_path: Path) -> None:
    """我们自己的 step 格式不能被这次改动破坏。"""
    f = tmp_path / "ours.json"
    f.write_text(
        json.dumps([{"duration": 1.5, "positions": [[1, 500], [2, 480]]}]),
        encoding="utf-8",
    )
    seq = HiwonderBusServoArmDriver._load_sequence_file(str(f))
    assert seq[0]["duration"] == 1.5
    assert seq[0]["positions"] == [[1, 500], [2, 480]]


# ---------- ② 电池遥测：非阻塞队列需要重试 ----------


def test_battery_read_retries_until_queue_fills() -> None:
    """get_battery 前几次返回 None（队列空），重试后应拿到真实电压。"""
    board = FakeBoard(battery_vin=7400, battery_empty_first=3)
    d = _driver(board, battery=100)

    st = d.status()
    # 7400mV 落在 (6000, 8400] 区间 → 中段电量；关键不是 100（初值）
    assert st.battery != 100, "battery 应来自真实电压而非构造初值"
    assert 0 <= st.battery <= 100
    assert board._battery_calls >= 4, "应至少重试到队列有数据"


def test_battery_handles_permanent_empty_queue() -> None:
    """队列始终空时不能抛异常，battery 退回初值。"""
    board = FakeBoard(battery_vin=None, battery_empty_first=999)
    d = _driver(board, battery=88)
    st = d.status()
    assert st.battery == 88


# ---------- ③ 舵机遥测（证据链）----------


def test_status_reports_per_servo_telemetry() -> None:
    """电压/温度/位置要能读出来 —— 这是"真机真的动了"的硬证据。"""
    board = FakeBoard(
        battery_vin=7400,
        servo_values={1: {"vin": 11500, "temp": 38, "position": 620}},
    )
    d = _driver(board, servo_ids=[1, 2])
    st = d.status()

    assert "1" in st.servos
    assert st.servos["1"]["position"] == 620
    assert st.servos["1"]["temp"] == 38
    assert st.servos["1"]["vin"] == 11500
    # 舵机 2 没给数据 → 用了 SDK 默认值，仍应有条目
    assert "2" in st.servos


def test_status_without_board_is_safe() -> None:
    """从未连过真机时 status 不能抛异常。"""
    d = HiwonderBusServoArmDriver(pick_positions=[[1, 500]])
    d._board = None
    st = d.status()
    assert st.battery == d.battery
    assert st.servos == {}


def test_telemetry_failure_does_not_break_status() -> None:
    """某个舵机读失败时，其余仍要能返回（总线回读是异步的）。"""

    class FlakyBoard(FakeBoard):
        def bus_servo_read_temp(self, sid):
            if sid == 1:
                raise RuntimeError("timeout")
            return [45]

    board = FlakyBoard(battery_vin=7400)
    d = _driver(board, servo_ids=[1, 2])
    st = d.status()
    assert "temp" not in st.servos.get("1", {})
    assert st.servos.get("2", {}).get("temp") == 45


# ---------- ④ 驱动契约 ----------


def test_driver_still_satisfies_protocol() -> None:
    """新增字段不能破坏 ArmDriver 协议的实现完整性。"""
    d = HiwonderBusServoArmDriver(pick_positions=[[1, 500]])
    for name in ("pick", "pause", "resume", "return_home", "emergency_stop", "status"):
        assert callable(getattr(d, name)), name


def test_emergency_stop_then_status_reports_mode() -> None:
    """急停后 status 要能看到状态变化。"""
    board = FakeBoard()
    d = _driver(board)
    d.emergency_stop()
    assert d.status().mode == DeviceMode.E_STOP


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
