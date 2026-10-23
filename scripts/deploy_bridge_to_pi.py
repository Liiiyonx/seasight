"""把 bridge 部署到树莓派并用 ROS 后端做只读验证。

★ 安全边界（重要）：
  本脚本默认**只跑 status()** —— 读 /joint_states 拿关节角，不下发任何轨迹。
  驱动真机需要显式加 --allow-motion，且那一步也应由人在现场盯着做。
  首次部署就自动驱动机械臂是不负责任的。

前置（实测确认，2026-10-05）：
  · ROS 包齐全：rospy / sensor_msgs / control_msgs / trajectory_msgs
  · /joint_states 正在以 20Hz 发布
  · armpi_fpv_kinematics 已封装几何法逆运动学
  · 缺 paho-mqtt（脚本会 pip 装）

用法：
    python scripts/deploy_bridge_to_pi.py --host 192.168.149.1 --user ubuntu
    python scripts/deploy_bridge_to_pi.py ... --backend ros_arm_control
    python scripts/deploy_bridge_to_pi.py ... --allow-motion   # 才会下发轨迹
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys

BRIDGE_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'edge'
)

# 需要上树的最小文件集（bridge 只依赖这些；device_sim 在 edge/ 下）
INCLUDE_FILES = [
    'arm_bridge/__init__.py',
    'arm_bridge/bridge.py',
    'arm_bridge/config.yaml',
    'arm_bridge/drivers.py',
    'arm_bridge/mqtt_transport.py',
    'arm_bridge/ros_driver.py',
    'arm_bridge/tools/__init__.py',
    'arm_bridge/tools/teach_hiwonder_sequence.py',
    'device_sim/__init__.py',
    'device_sim/protocol.py',
]

STATUS_PROBE = r'''import json
import os
import sys

sys.path.insert(0, "/home/ubuntu/seasight_bridge")
sys.path.insert(0, "/home/ubuntu/seasight_bridge/arm_bridge")

os.environ.setdefault("ROS_MASTER_URI", "http://localhost:11311")
os.environ.setdefault("ROS_HOSTNAME", "localhost")

out = {"ok": False}
try:
    import rospy  # noqa: F401
    import yaml

    from arm_bridge.drivers import build_arm_driver

    cfg_path = "/home/ubuntu/seasight_bridge/arm_bridge/config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    dcfg = cfg.get("driver", {})
    backend = sys.argv[1] if len(sys.argv) > 1 else dcfg.get("backend", "simulated")
    params = dcfg.get(backend) or {}
    if not isinstance(params, dict):
        params = {}

    drv = build_arm_driver(dcfg, backend)
    st = drv.status()
    out["ok"] = True
    out["backend"] = backend
    out["driver_type"] = type(drv).__name__
    out["mode"] = st.mode
    out["battery"] = st.battery
    out["servos"] = st.servos
    out["bins"] = st.bins
except Exception as exc:
    import traceback
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
    out["trace"] = traceback.format_exc()[-500:]

print("__JSON__" + json.dumps(out, ensure_ascii=False))
'''


def main() -> int:
    ap = argparse.ArgumentParser(description='部署 bridge 到树莓派并只读验证')
    ap.add_argument('--host', default='192.168.149.1')
    ap.add_argument('--user', default='ubuntu')
    ap.add_argument('--password', default=None, help='留空读 PI_PASSWORD')
    ap.add_argument('--backend', default='ros_arm_control',
                    help='ros_arm_control | simulated | http | hiwonder_bus_servo')
    ap.add_argument('--remote-dir', default='/home/ubuntu/seasight_bridge')
    ap.add_argument('--allow-motion', action='store_true',
                    help='★ 会真实驱动机械臂。默认不设，只做只读验证。')
    args = ap.parse_args()

    password = args.password or os.environ.get('PI_PASSWORD', '')
    if not password:
        print('未提供密码：设环境变量 PI_PASSWORD', file=sys.stderr)
        return 2
    if args.allow_motion:
        print('★ --allow-motion 已开启：接下来的动作会真实驱动机械臂，'
              '请确保现场有人盯着。', file=sys.stderr)

    try:
        import paramiko
    except ImportError:
        print('需要 paramiko：pip install paramiko cryptography', file=sys.stderr)
        return 2

    missing = [f for f in INCLUDE_FILES
               if not os.path.isfile(os.path.join(BRIDGE_SRC, f))]
    if missing:
        print('本地缺少文件：%s' % missing, file=sys.stderr)
        return 2

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=args.user, password=password,
                    timeout=25, banner_timeout=25, auth_timeout=25)
    except Exception as exc:  # noqa: BLE001
        print('连接失败 %s@%s：%s' % (args.user, args.host, exc), file=sys.stderr)
        print('排查：① 电脑是否连上树莓派 AP 热点 ② 树莓派是否还开着', file=sys.stderr)
        return 1

    def run(cmd: str, timeout: int = 40) -> str:
        _, so, se = cli.exec_command(cmd, timeout=timeout)
        so.channel.settimeout(timeout)
        try:
            out = so.read().decode('utf-8', 'replace')
        except Exception as exc:  # noqa: BLE001
            return '(read timeout: %s)' % type(exc).__name__
        err = se.read().decode('utf-8', 'replace')
        return (out or '') + (('\n[stderr] ' + err) if err.strip() else '')

    try:
        # ── 1. 上传 ──
        print('1) 上传 bridge（%d 个文件）→ %s' % (len(INCLUDE_FILES), args.remote_dir))
        # 先递归建目录（首次部署时目标目录不存在，sftp.mkdir 不建父目录）
        print('   ' + run('mkdir -p %s' % args.remote_dir).strip())
        sftp = cli.open_sftp()
        for rel in INCLUDE_FILES:
            local = os.path.join(BRIDGE_SRC, rel.replace('/', os.sep))
            remote = posixpath.join(args.remote_dir, rel)
            try:
                sftp.mkdir(posixpath.dirname(remote))
            except IOError:
                pass  # 已存在
            sftp.put(local, remote)
            print('   %s' % rel)
        sftp.close()

        # ── 2. 依赖 ──
        print('\n2) 检查/安装依赖…')
        need = run(
            "python3 -c 'import paho.mqtt.client' 2>/dev/null "
            "&& echo HAVE_PAHO || echo NEED_PAHO"
        )
        if 'NEED_PAHO' in need:
            print('   安装 paho-mqtt…')
            print('   ' + run(
                'python3 -m pip install --user --quiet paho-mqtt 2>&1 | tail -3',
                timeout=180).strip())
        else:
            print('   paho-mqtt 已有')

        # ── 3. 配置 backend ──
        print('\n3) 设置 driver.backend = %s' % args.backend)
        cfg_remote = posixpath.join(args.remote_dir, 'arm_bridge', 'config.yaml')
        print('   ' + run(
            "python3 - <<'PYEOF'\n"
            "import io, re\n"
            "p = '%s'\n"
            "s = io.open(p, encoding='utf-8').read()\n"
            "s = re.sub(r'(\\n\\s*)backend: \"[^\"]*\"', r'\\1backend: \"%s\"', s, count=1)\n"
            "io.open(p, 'w', encoding='utf-8').write(s)\n"
            "print('backend =', re.search(r'backend: \"([^\"]*)\"', s).group(1))\n"
            "PYEOF" % (cfg_remote, args.backend)).strip())

        # ── 4. 只读验证 ──
        print('\n4) 只读验证 status()（★ 不下发任何轨迹）')
        sftp = cli.open_sftp()
        probe = posixpath.join(args.remote_dir, '_status_probe.py')
        with sftp.open(probe, 'w') as f:
            f.write(STATUS_PROBE)
        sftp.close()
        blob = run(
            'source /opt/ros/melodic/setup.bash 2>/dev/null; '
            'cd %s && timeout 30 python3 _status_probe.py %s 2>&1 | tail -5'
            % (args.remote_dir, args.backend), timeout=60)
        print('   ' + blob.strip()[-500:])

        if '__JSON__' in blob:
            payload = json.loads(blob.split('__JSON__', 1)[1].split('\n')[0])
            print('\n── 结果 ──')
            if payload.get('ok'):
                print('   ✓ status() 成功（驱动类型：%s）'
                      % payload.get('driver_type'))
                print('     mode=%s battery=%s bins=%s'
                      % (payload.get('mode'), payload.get('battery'),
                         payload.get('bins')))
                servos = payload.get('servos') or {}
                if servos:
                    print('     关节遥测（%d 个）：' % len(servos))
                    for sid, d in sorted(servos.items(), key=lambda kv: int(kv[0])):
                        print('       #%-3s %-8s %8s rad = %8s deg'
                              % (sid, d.get('joint', '?'), d.get('position'),
                                 d.get('position_deg')))
                else:
                    print('     ⚠ servos 为空 —— 没读到 /joint_states')
                    print('       检查：ROS master 是否可达、节点是否在发话题')
            else:
                print('   ✗ 失败：%s' % payload.get('error'))
                if payload.get('trace'):
                    print(payload['trace'])
        else:
            print('   （未拿到结构化结果）')

        if not args.allow_motion:
            print('\n（未加 --allow-motion，故**没有**下发任何运动指令。'
                  '首次真机动作请人在现场盯着做。）')
    finally:
        cli.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
