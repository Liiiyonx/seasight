"""把厂商 SDK 部署到树莓派并验证真实舵机通信。

背景：树莓派上没装 `ros_robot_controller_sdk`，导致
`HiwonderBusServoArmDriver` 与 `tools/teach_hiwonder_sequence.py` 都用不了。
但**资料包里现成就有** —— `6.总线舵机二次开发教程-树莓派版本/程序文件/案例*/`
每个案例目录各附一份。复制过去即可，无需联网。

本脚本做三件事（★ 第3 步是只读的，**不驱动舵机**）：
  1. 复制 SDK 到树莓派 ~/ 目录
  2. 验证可导入 + 能打开串口
  3. 只读回读舵机 ID / 位置 / 电压 / 温度 —— 证明"舵机真的接着"

第 3 步刻意不发任何运动指令。首次通电的机械臂不应该被自动驱动。

用法：
    python scripts/deploy_arm_sdk.py --host 192.168.149.1 --user ubuntu
    python scripts/deploy_arm_sdk.py --host ... --sdk-path<资料包里的 sdk 路径>

密码走环境变量 PI_PASSWORD，不进命令行历史。
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_SDK = (
    r'<USER_HOME>\Desktop\树莓派总线舵机机械臂和麦轮底盘相关资料'
    r'\总线舵机机械臂相关资料\6.总线舵机二次开发教程-树莓派版本\程序文件'
    r'\案例3 控制总线舵机转动\ros_robot_controller_sdk.py'
)

# 双通道只读探测：两条控制通道都试一遍。
# ★ 为什么必须都试：资料里厂商 GUI 用 /dev/ttyS0@115200，而厂商示例与
#   我们的驱动用 /dev/ttyAMA0@1000000。若只试一条就下结论，
#   分不清是「通道选错」还是「舵机根本没回应」—— 而这两者的排查方向
#   完全不同（前者改 config 即可，后者要查硬件）。
#   2026-10-05 实测：两条都能开串口，但都无回包 → 指向硬件侧。
CHANNELS = [
    ("/dev/ttyAMA0", 1000000, "厂商示例/我们的驱动"),
    ("/dev/ttyS0", 115200, "厂商 GUI"),
]

# 只读回读脚本：只调用 read_* 方法，绝不调用 set_*/stop。
#
# ★ 每个阶段都flush 打印，并用 alarm 给回读加硬超时 ——
#   厂商 SDK 的 bus_servo_read_id() 内部是阻塞等待：串口能开但舵机
#   没回包时它会一直等下去。没有超时的话，脚本会挂在那里看不出原因。
#   实测（2026-10-05）：Board() 能构造、enable_reception 成功，
#   但 read_id 拿不到回包 —— 说明串口通了、舵机没回应。
READONLY_PROBE = r'''
import json
import signal
import time

import ros_robot_controller_sdk as rrc

CHANNELS = [
    ("/dev/ttyAMA0", 1000000, "vendor-examples/our-driver"),
    ("/dev/ttyS0", 115200, "vendor-gui"),
]


def _timeout(signum, frame):
    raise TimeoutError("read timeout: 舵机无回包")


results = []
for dev, baud, tag in CHANNELS:
    entry = {
        "device": dev, "baud": baud, "tag": tag,
        "open": False, "reply": False, "ids": [], "servos": {},
        "stage": None, "error": None,
    }
    # 每个通道给 8 秒硬超时：厂商 SDK 的 read_id 内部是阻塞等待，
    # 串口能开但舵机不回包时会一直等下去，没有超时看不出原因。
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(8)
    try:
        entry["stage"] = "open"
        board = rrc.Board(device=dev, baudrate=baud, timeout=2.0)
        entry["open"] = True
        board.enable_reception()
        time.sleep(0.3)
        entry["stage"] = "read_id"
        ids = board.bus_servo_read_id(255)
        entry["reply"] = True
        entry["ids"] = list(ids) if ids else []
        signal.alarm(0)
        for sid in (entry["ids"] or [1])[:6]:
            item = {}
            for key, fn in (("vin", board.bus_servo_read_vin),
                            ("temp", board.bus_servo_read_temp),
                            ("position", board.bus_servo_read_position)):
                try:
                    v = fn(sid)
                    if isinstance(v, (list, tuple)):
                        v = v[0] if v else None
                    if isinstance(v, (int, float)):
                        item[key] = v
                except Exception:
                    pass
            if item:
                entry["servos"][str(sid)] = item
    except Exception as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        entry["stage"] = entry["stage"] or "open"
    finally:
        signal.alarm(0)
    results.append(entry)

print("__JSON__" + json.dumps({"channels": results}, ensure_ascii=False))
'''


def main() -> int:
    ap = argparse.ArgumentParser(description='部署厂商 SDK 并只读验证舵机通信')
    ap.add_argument('--host', default='192.168.149.1')
    ap.add_argument('--user', default='ubuntu')
    ap.add_argument('--password', default=None, help='留空读环境变量 PI_PASSWORD')
    ap.add_argument('--sdk-path', default=DEFAULT_SDK)
    ap.add_argument('--dest', default='ros_robot_controller_sdk.py')
    ap.add_argument('--timeout', type=int, default=40)
    args = ap.parse_args()

    password = args.password or os.environ.get('PI_PASSWORD', '')
    if not password:
        print('未提供密码：设环境变量 PI_PASSWORD 或加 --password', file=sys.stderr)
        return 2

    if not os.path.isfile(args.sdk_path):
        print('找不到 SDK 文件：%s' % args.sdk_path, file=sys.stderr)
        print('用 --sdk-path 指定资料包里的ros_robot_controller_sdk.py', file=sys.stderr)
        return 2

    try:
        import paramiko
    except ImportError:
        print('需要 paramiko：pip install paramiko cryptography', file=sys.stderr)
        return 2

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=args.user, password=password,
                    timeout=args.timeout, banner_timeout=args.timeout,
                    auth_timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print('连接失败 %s@%s：%s' % (args.user, args.host, exc), file=sys.stderr)
        print('排查：① 树莓派是否上电 ② 电脑是否连上它的 AP 热点', file=sys.stderr)
        return 1

    def run(cmd: str, timeout: int | None = None) -> tuple[str, str]:
        _, so, se = cli.exec_command(cmd, timeout=timeout or args.timeout)
        so.channel.settimeout(timeout or args.timeout)
        try:
            return so.read().decode('utf-8', 'replace'), se.read().decode('utf-8', 'replace')
        except Exception as exc:  # noqa: BLE001
            return '(read timeout: %s)' % type(exc).__name__, ''

    try:
        # ── 1. 复制 SDK ──
        print('1) 上传 SDK（%d 字节）…' % os.path.getsize(args.sdk_path))
        sftp = cli.open_sftp()
        remote = '/home/%s/%s' % (args.user, args.dest)
        sftp.put(args.sdk_path, remote)
        sftp.close()
        out, _ = run('ls -l %s' % remote)
        print('   已放置：%s' % out.strip())

        # ── 2. 验证可导入 + 串口可开 ──
        print('\n2) 验证导入与串口…')
        out, err = run(
            'cd ~ && python3 -c "'
            'import ros_robot_controller_sdk as r; '
            'b = r.Board(); '
            'print(\'SDK_OK board=\', type(b).__name__)" 2>&1 | tail -5',
            timeout=args.timeout,
        )
        print('   ' + (out or err).strip())
        if 'SDK_OK' not in (out or ''):
            print('   ★ SDK 未能正常打开串口。常见原因：'
                  '串口被厂商 GUI 占用，或应该用 /dev/ttyS0 @115200')

        # ── 3. 只读回读（不驱动） ──
        print('\n3) 只读回读舵机状态（★ 不发任何运动指令）…')
        # ★ 探针必须放在~ （SDK 所在目录），否则 sys.path[0] 是脚本自己的
        #   目录，找不到 ros_robot_controller_sdk。同时显式设 PYTHONPATH 兜底。
        sftp = cli.open_sftp()
        probe_remote = '/home/%s/_probe_readonly.py' % args.user
        with sftp.open(probe_remote, 'w') as f:
            f.write(READONLY_PROBE)
        sftp.close()
        out, err = run(
            'cd /home/%s && PYTHONPATH=/home/%s python3 %s 2>&1 | tail -6'
            % (args.user, args.user, probe_remote),
            timeout=args.timeout)
        blob = (out or '') + (err or '')
        print('   ' + blob.strip()[-600:])

        if '__JSON__' in blob:
            import json
            payload = json.loads(blob.split('__JSON__', 1)[1].split('\n')[0])
            channels = payload.get('channels') or []
            print('\n── 结论 ──')

            for ch in channels:
                mark = '✓' if ch.get('reply') else ('·' if ch.get('open') else '✗')
                print('   %s %-16s @%-8d %s' % (
                    mark, ch.get('device'), ch.get('baud'), ch.get('tag')))
                print('       串口: %s   回包: %s' % (
                    '开' if ch.get('open') else '打不开',
                    '有' if ch.get('reply') else '无'))
                if ch.get('error'):
                    print('       错误: %s' % ch['error'])
                for sid, d in (ch.get('servos') or {}).items():
                    print('       #%s  %s' % (
                        sid,
                        '  '.join('%s=%s' % (k, v) for k, v in d.items())))

            ok = [c for c in channels if c.get('reply')]
            opened = [c for c in channels if c.get('open')]

            print()
            if ok:
                ch = ok[0]
                print('   ✓ 舵机通信正常（走 %s @%d）'
                      % (ch['device'], ch['baud']))
                if ch.get('ids'):
                    print('     舵机 ID：%s' % ch['ids'])
                print('\n   把该通道写进配置：')
                print('     # edge/arm_bridge/config.yaml')
                print('     driver:')
                print('       backend: "hiwonder_bus_servo"')
                print('       hiwonder_bus_servo:')
                print('         serial_port: "%s"' % ch['device'])
                print('         baudrate: %d' % ch['baud'])
                print('\n   下一步：录示教序列（3–5 个姿态即可）')
                print('     python3 ~/teach_hiwonder_sequence.py \\')
                print('       --device %s --servo-ids 1 2 3 4 5 6 \\'
                      % ch['device'])
                print('       --output pick_sequence.json')
            elif opened:
                print('   ✗ 串口能打开，但舵机无回包 —— 两条通道都试过了，')
                print('     所以**不是通道选错**，问题在硬件侧：')
                print('     · 机械臂是否通电（扩展板 LED 亮/每 2 秒闪）')
                print('     · 厂商扩展板是否上电 —— 舵机供电由扩展板提供，'
                      '不由树莓派供电')
                print('     · 总线舵机是否插在扩展板的总线口上')
                print('     · 舵机 ID 是否为默认 0（可用广播 255 查，见案例2）')
                print('     · 74HC126 三态缓冲器是否插好（半双工方向切换靠它）')
                print()
                print('   验证手段：接上电后用厂商 GUI（ArmPi_PC_Software）'
                      '读一次舵机 —— GUI 能读到就说明硬件没问题，')
                print('   剩下的是我们与它的串口/波特率需要对齐。')
            else:
                print('   ✗ 串口打不开。检查：')
                print('     · config.yaml 的 serial_port 是否与实际一致')
                print('     · 串口是否被厂商 GUI 占用（先关掉 ArmPi_PC_Software）')
                print('     · 板载 UART 是否被蓝牙占用'
                      '（/boot/firmware/config.txt）')
        else:
            print('   （未拿到结构化结果，见上方原始输出）')
    finally:
        cli.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
