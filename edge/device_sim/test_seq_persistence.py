"""WP-14E 设备孪生遥测序号跨进程持久化测试。

验证（全部在内存 transport + 假时钟下，不依赖真实网络 / broker / 设备）：
- 默认**不写盘**：未配置路径（构造参数 / 环境变量）时行为与既有版本完全一致；
- 路径可配置：构造参数优先，环境变量 `DEVICE_SIM_SEQ_FILE` 兜底；
- 原子写盘：临时文件 + 替换，写盘后无 `.tmp` 残留、内容为合法 JSON；
- 跨进程单调：写盘 → 新进程重新加载 → 新号严格大于旧号，不回退到 0、
  不重复已用序号；
- 降级不崩溃：文件缺失（首次运行）正常从 0 开始；文件损坏以 warning
  从最后一个可解析值（或 0）继续；落盘失败仅内存推进，绝不抛异常。

★ 临时文件策略（沙箱绕过基线）：本沙箱对 pytest ``tmp_path`` / ``tempfile``
  的**目录迭代**报 ``PermissionError``（已知环境限制），因此本文件不依赖
  ``tmp_path`` 夹具，改用「系统临时目录下的直接文件路径」（``seq_file``
  夹具，创建/清理均为文件级操作，不迭代目录）；正常环境同样适用，
  总控复跑不受影响。测试只做文件级读写，不修改、不删除任何既有文件。

证据等级：E1/E2 —— 孪生与本地文件持久化可复现，不代表真实设备或现场验证。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
sys.path.insert(0, str(EDGE))

from device_sim.device import (  # noqa: E402
    DEVICE_SIM_SEQ_FILE_ENV,
    DeviceTwin,
    load_seq_from_file,
    save_seq_atomic,
)
from device_sim.protocol import FakeClock, SequentialIdFactory  # noqa: E402
from device_sim.transport import MemoryTransport  # noqa: E402

DEVICE = "RBT-SEQ-01"


@pytest.fixture
def seq_file() -> Path:
    """系统临时目录下的唯一文件路径（直接文件，无目录迭代）。

    绕过沙箱对 ``tmp_path`` 目录迭代的 PermissionError；文件级创建与
    清理在正常环境同样成立。返回的路径**尚不存在**（由测试决定是否创建）。
    """
    path = Path(tempfile.gettempdir()) / f"wp14e_seq_{os.getpid()}_{uuid.uuid4().hex[:10]}.json"
    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def make_twin(seq_store_path, *, clock_start: float = 0.0) -> DeviceTwin:
    """构造带持久化路径的孪生（订阅 + 初始遥测）。"""
    twin = DeviceTwin(
        DEVICE,
        MemoryTransport(),
        clock=FakeClock(clock_start),
        id_factory=SequentialIdFactory("ack"),
        seq_store_path=seq_store_path,
    )
    twin.start()
    return twin


# ----------------------------------------------------------------------
# 默认不写盘（保持既有行为）
# ----------------------------------------------------------------------
class TestDefaultNoDiskWrite:
    def test_without_path_no_file_created(self, seq_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """未配置路径（构造参数与环境变量都没有）→ 绝不落盘，行为与旧版一致。"""
        monkeypatch.delenv(DEVICE_SIM_SEQ_FILE_ENV, raising=False)
        twin = DeviceTwin(
            DEVICE,
            MemoryTransport(),
            clock=FakeClock(0.0),
            id_factory=SequentialIdFactory("ack"),
        )
        twin.start()
        assert twin.seq_store_path is None
        assert twin.seq == 1, "默认从 1 起计（首 tick）"
        twin.tick()
        twin.tick()
        assert twin.seq == 3
        assert not seq_file.exists(), "未配置路径时不得写盘"


# ----------------------------------------------------------------------
# 写盘 → 新进程重新加载 → 严格单调
# ----------------------------------------------------------------------
class TestCrossProcessMonotonic:
    def test_reload_continues_strictly_increasing(self, seq_file: Path) -> None:
        """写盘 → 新进程重新加载 → 新号 > 旧号；不回退、不重复已用序号。"""
        twin1 = make_twin(seq_file)
        twin1.tick()
        twin1.tick()
        twin1.tick()
        assert twin1.seq == 4            # start() 1 tick + 3 tick
        assert twin1.history["telemetry"][-1].seq == 4
        assert seq_file.exists()

        # 「进程重启」：全新 DeviceTwin 实例（新构造、重新加载文件）
        twin2 = make_twin(seq_file)          # start() 触发首 tick → 5
        assert twin2.seq == 5, "新进程必须从上次值续上并严格递增"
        assert twin2.history["telemetry"][0].seq == 5
        assert twin2.history["telemetry"][0].seq > twin1.seq
        # 已用过的 1..4 绝不重现
        assert all(t.seq > 4 for t in twin2.history["telemetry"])
        twin2.tick()
        assert twin2.seq == 6

    def test_multiple_restart_cycles_keep_increasing(self, seq_file: Path) -> None:
        """连续多次「重启」：每次新进程的首 tick 号都大于上一进程的最大号。"""
        prev_max = 0
        for i in range(3):
            twin = make_twin(seq_file)
            twin.tick()
            first_seq = twin.history["telemetry"][0].seq
            assert first_seq > prev_max, f"第 {i + 1} 次重启回退了序号"
            prev_max = first_seq
        assert prev_max >= 3

    def test_no_reuse_of_used_seq_after_reload(self, seq_file: Path) -> None:
        """重载后首个新序号必须 > 文件中的最大值，不得从 1 或旧值重来。"""
        twin1 = make_twin(seq_file)
        for _ in range(10):
            twin1.tick()
        persisted = json.loads(seq_file.read_text(encoding="utf-8"))["seq"]
        assert persisted == twin1.seq == 11

        # 「新进程」：先构造（加载文件）再启动，验证加载值与启动后自增
        twin2 = DeviceTwin(
            DEVICE,
            MemoryTransport(),
            clock=FakeClock(0.0),
            id_factory=SequentialIdFactory("ack"),
            seq_store_path=seq_file,
        )
        assert twin2.seq == 11, "构造时加载上次值，下一 tick 才自增"
        twin2.start()
        assert twin2.seq == 12
        assert twin2.history["telemetry"][0].seq == 12


# ----------------------------------------------------------------------
# 原子写盘
# ----------------------------------------------------------------------
class TestAtomicWrite:
    def test_no_tmp_leftover_and_valid_json(self, seq_file: Path) -> None:
        """原子写：落盘后无 `.tmp` 残留，内容为合法 JSON（{"seq": N}）。"""
        twin = make_twin(seq_file)
        for _ in range(5):
            twin.tick()
        tmp_path = Path(f"{seq_file}.tmp")
        assert not tmp_path.exists(), "原子写不得留下临时文件"
        data = json.loads(seq_file.read_text(encoding="utf-8"))
        assert data == {"seq": twin.seq}
        assert twin.seq == 6

    def test_save_seq_atomic_roundtrip(self, seq_file: Path) -> None:
        """底层辅助函数：save → load 一致，且重复 save 幂等覆盖。"""
        assert save_seq_atomic(seq_file, 7) is True
        assert load_seq_from_file(seq_file) == (7, "ok")
        assert save_seq_atomic(seq_file, 8) is True
        assert load_seq_from_file(seq_file) == (8, "ok")
        assert not Path(f"{seq_file}.tmp").exists()


# ----------------------------------------------------------------------
# 降级：缺失 / 损坏 / 写盘失败
# ----------------------------------------------------------------------
class TestDegradation:
    def test_missing_file_is_normal_first_run(self, seq_file: Path) -> None:
        """文件缺失 = 首次运行：从 0 开始，下一 tick 为 1，不抛异常。"""
        assert not seq_file.exists()
        assert load_seq_from_file(seq_file) == (0, "missing")
        twin = make_twin(seq_file)
        assert twin.seq == 1

    def test_corrupt_file_resumes_from_last_parseable(self, seq_file: Path) -> None:
        """损坏文件：warning 降级，从最后一个可解析值继续。"""
        seq_file.write_text("garbage {{{ seq: 42 }}} trailing", encoding="utf-8")
        twin = DeviceTwin(
            DEVICE,
            MemoryTransport(),
            clock=FakeClock(0.0),
            id_factory=SequentialIdFactory("ack"),
            seq_store_path=seq_file,
        )
        assert twin.seq == 42, "损坏时取最后一个可解析值"
        twin.start()
        assert twin.seq == 43

    def test_totally_corrupt_file_starts_from_zero(self, seq_file: Path) -> None:
        """完全不可解析：warning 降级从 0 开始，首 tick 为 1，不抛异常。"""
        seq_file.write_text("!!! not a number !!!", encoding="utf-8")
        twin = DeviceTwin(
            DEVICE,
            MemoryTransport(),
            clock=FakeClock(0.0),
            id_factory=SequentialIdFactory("ack"),
            seq_store_path=seq_file,
        )
        assert twin.seq == 0
        twin.start()
        assert twin.seq == 1
        assert twin.history["telemetry"][0].seq == 1

    def test_save_failure_degrades_in_memory_only(self, seq_file: Path) -> None:
        """落盘失败（父目录不存在）：仅内存推进，遥测闭环不中断、不抛异常。"""
        target = Path(f"{seq_file}_nonexistent_dir") / "seq.json"
        twin = make_twin(target)
        twin.tick()
        assert twin.seq == 2            # start() 1 tick + 1 tick，内存仍推进
        assert twin.history["telemetry"][-1].seq == 2
        assert not target.exists()


# ----------------------------------------------------------------------
# 路径配置：环境变量与构造参数优先级
# ----------------------------------------------------------------------
class TestPathConfiguration:
    def test_env_var_configures_path(self, seq_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 DEVICE_SIM_SEQ_FILE 可配置路径（不传构造参数）。"""
        monkeypatch.setenv(DEVICE_SIM_SEQ_FILE_ENV, str(seq_file))
        twin = DeviceTwin(
            DEVICE,
            MemoryTransport(),
            clock=FakeClock(0.0),
            id_factory=SequentialIdFactory("ack"),
        )
        twin.start()
        assert twin.seq_store_path == str(seq_file)
        assert seq_file.exists(), "环境变量配置的路径应生效并写盘"

    def test_constructor_path_takes_precedence_over_env(
        self, seq_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """构造参数优先于环境变量。"""
        ctor_target = Path(f"{seq_file}_ctor.json")
        monkeypatch.setenv(DEVICE_SIM_SEQ_FILE_ENV, str(seq_file))
        twin = make_twin(ctor_target)
        assert twin.seq_store_path == str(ctor_target)
        assert ctor_target.exists()
        assert not seq_file.exists(), "构造参数应覆盖环境变量"
        ctor_target.unlink(missing_ok=True)

    def test_load_seq_from_file_float_json_ok(self, seq_file: Path) -> None:
        """JSON 中 seq 为整数形态浮点（3.0）也按 3 解析（ok）。"""
        seq_file.write_text('{"seq": 3.0}', encoding="utf-8")
        assert load_seq_from_file(seq_file) == (3, "ok")
