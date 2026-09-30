"""探海灵眸 SeaSight —— WP-14 作业设备数字孪生与故障注入。

独立于感知事件模拟器（`edge/simulator/`）的机器人作业单元数字孪生：

- ``protocol``：冻结协议（命令 / ACK / 遥测 / 故障码）与平台侧 ACK 幂等。
- ``transport``：可注入传输抽象与内存 transport。
- ``device``：DeviceTwin 状态机（命令闭环、断网排队、重连补传、急停优先）。
- ``faults``：故障注入脚本、确定性场景与报告。

全部为 E1/E2：孪生与闭环在假时钟 + 内存 transport 下确定可复现，
不代表真实设备、真实边缘盒或现场验证。
"""

from .device import (
    DEVICE_SIM_SEQ_FILE_ENV,
    DeviceTwin,
    TwinConfig,
    load_seq_from_file,
    save_seq_atomic,
)
from .faults import DeviceScenario, FaultReport, run_device_scenario
from .protocol import (
    Ack,
    AckResult,
    AckTracker,
    COMMAND_ACTIONS,
    DeviceCommand,
    DeviceMode,
    FAULT_CODES,
    FAULT_PRIORITY,
    FakeClock,
    Position,
    TELEMETRY_FIELDS,
    Telemetry,
)
from .transport import MemoryTransport

__all__ = [
    "COMMAND_ACTIONS",
    "TELEMETRY_FIELDS",
    "FAULT_CODES",
    "FAULT_PRIORITY",
    "DeviceMode",
    "FakeClock",
    "Position",
    "DeviceCommand",
    "Ack",
    "Telemetry",
    "AckResult",
    "AckTracker",
    "MemoryTransport",
    "DeviceTwin",
    "TwinConfig",
    "DEVICE_SIM_SEQ_FILE_ENV",
    "load_seq_from_file",
    "save_seq_atomic",
    "DeviceScenario",
    "FaultReport",
    "run_device_scenario",
]
