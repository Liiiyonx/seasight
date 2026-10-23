#!/usr/bin/env bash
# ============================================================
# Oceanus 机械臂 · 一键端到端自动抓取入口（在树莓派上运行）
# ============================================================
# 解决什么问题
# ------------
# 原来的 `oceanus-arm-autopick.service` 直接 `python3 -m grasp.supervisor`，
# 但 supervisor **不含 ROS 接入的启动**：它假设厂商的相机 / 舵机控制器节点
# 已经在跑。板子重启后没人去起那三组 launch，于是 supervisor 按设计检查到
# `ros_error` 就干净退出（exit 2）—— 现象是「服务是 running 过、但机械臂
# 一动不动」，日志里只有一句「ROS 链路不可用」。
#
# 本脚本把「开机后到机械臂开始自己捡」之间的全部步骤串成一条命令：
#
#   0) 环境自检（ROS / 厂商工作空间 / config / 串口权限）
#   1) 启动厂商三组 bringup（依赖 / 相机 / 舵机控制器）
#      ★ 绝不启动 start_functions.launch —— 它会把 object_sorting /
#        object_tracking / warehouse 等 required=true 节点全拉起来，
#        直接抢走相机与控制权（"有框但机械臂不动"的头号死因）。
#   2) 等真实话题就绪（/usb_cam/image_raw 有帧、servo_states 有读数）
#   3) 跑只读自检 precheck.py（模型 / 标定 / 放置点 / 相机四项）
#   4) 起 supervisor 常驻自捡
#
# 三条硬约束（承自 arm_io.py / board_agent.py，都由厂商源码实测得出）
# ----------------------------------------------------------------
#   1. 不抢串口：只有 hiwonder_servo_manager 持有总线，本脚本只 source + 起节点。
#   2. 不抢摄像头：只让 usb_cam 打开 /dev/video*。
#   3. 不与 arm_bridge 同跑：本脚本 = 常驻自捡（无平台派单）。
#      要平台派单请改用 arm_bridge（见 deploy/README）。
#
# 用法
# ----
#   # 真机（systemd 用的就是这一条）
#   bash deploy/run_autopick.sh
#
#   # 只做前置检查，不启动任何东西（现场排障先跑这个）
#   bash deploy/run_autopick.sh --check
#
#   # 不碰 ROS：合成画面 + 模拟舵机，验证脚本与闭环本身（开发机/板子均可）
#   bash deploy/run_autopick.sh --mock --once
#
#   # 厂商节点已经在跑（手工起过），只要 supervisor
#   bash deploy/run_autopick.sh --skip-bringup
#
# 退出码（与 supervisor 对齐，systemd 的 RestartPreventExitStatus 依赖它）
#   0  正常收尾（收到停止信号）
#   2  前置不满足（缺 ROS / 缺标定 / 缺模型 / 缺放置点）—— 需要人去修，别自动重启
#   3  连续抓取失败超限 —— 也需要人看，别自动重启
#   4  厂商节点没能在超时内就绪（相机不出图 / 舵机控制器没起来）
set -uo pipefail

# ---------------------------------------------------------------- 参数
MOCK=0
ONCE=0
CHECK_ONLY=0
SKIP_BRINGUP=0
KEEP_BRINGUP=0
TOPIC_TIMEOUT=45
ROS_SETUP="${OCEANUS_ROS_SETUP:-$HOME/armpi_fpv/devel/setup.bash}"
ROS_DISTRO_SETUP="/opt/ros/melodic/setup.bash"
CONFIG=""
LOG_DIR="${OCEANUS_LOG_DIR:-/tmp/oceanus-autopick}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mock)          MOCK=1; shift ;;
    --once)          ONCE=1; shift ;;
    --check)         CHECK_ONLY=1; shift ;;
    --skip-bringup)  SKIP_BRINGUP=1; shift ;;
    --keep-bringup)  KEEP_BRINGUP=1; shift ;;
    --topic-timeout) TOPIC_TIMEOUT="${2:-45}"; shift 2 ;;
    --ros-setup)     ROS_SETUP="${2:-}"; shift 2 ;;
    --config)        CONFIG="${2:-}"; shift 2 ;;
    --log-dir)       LOG_DIR="${2:-}"; shift 2 ;;
    -h|--help)       sed -n '2,58p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # …/edge/grasp/deploy
GRASP="$(cd "$HERE/.." && pwd)"                          # …/edge/grasp
EDGE="$(cd "$GRASP/.." && pwd)"                          # …/edge
cd "$EDGE" || exit 2

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log()  { echo "[$(ts)] $*"; }
warn() { echo "[$(ts)] ⚠ $*" >&2; }
die()  { echo "[$(ts)] ✗ $*" >&2; exit "${2:-2}"; }

mkdir -p "$LOG_DIR" 2>/dev/null || true
# 解释器：板子上就是 python3；留环境变量是为了在开发机上用 mock 模式验证本脚本
PY="${OCEANUS_PY:-python3}"
# 强制 UTF-8 标准输出：自检表格用的是 ✓/✗ 与中文，在 GBK 控制台（Windows）
# 会直接 UnicodeEncodeError 把自检打断 —— 那会把「检查没通过」误报成「脚本崩了」。
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export PYTHONUNBUFFERED=1
[[ -n "$CONFIG" ]] || CONFIG="$GRASP/config.yaml"

CFG_ARGS=()
[[ -n "$CONFIG" ]] && CFG_ARGS=(--config "$CONFIG")

# ---------------------------------------------------------------- 0) 环境自检
log "Oceanus 一键自动抓取 —— 环境自检"

if [[ "$MOCK" -eq 0 ]]; then
  [[ -f "$ROS_DISTRO_SETUP" ]] || die "找不到 $ROS_DISTRO_SETUP —— 板子上没装 ROS Melodic？"
  if [[ ! -f "$ROS_SETUP" ]]; then
    die "找不到厂商工作空间 $ROS_SETUP
   需要先编译：cd ~/armpi_fpv && catkin_make
   路径不同就显式指定：--ros-setup /path/to/devel/setup.bash"
  fi
  # shellcheck disable=SC1090
  source "$ROS_DISTRO_SETUP"
  # shellcheck disable=SC1090
  source "$ROS_SETUP"
  # ★ non-interactive ssh（`ssh board 'bash run_autopick.sh --check'`）不会读
  #   ~/.bashrc，ROS_MASTER_URI / ROS_IP 这类只在交互 shell 里设的变量会缺，
  #   于是 rospy 可能连不上 master —— 现象是「手工跑好的、ssh 跑就 ✗ ROS」。
  #   所以支持用环境变量补：OCEANUS_ROS_MASTER_URI / OCEANUS_ROS_IP /
  #   OCEANUS_ROS_HOSTNAME（systemd 单元里也注入了这几项）。
  [[ -n "${OCEANUS_ROS_MASTER_URI:-}" ]] && export ROS_MASTER_URI="$OCEANUS_ROS_MASTER_URI"
  [[ -n "${OCEANUS_ROS_IP:-}" ]]         && export ROS_IP="$OCEANUS_ROS_IP"
  [[ -n "${OCEANUS_ROS_HOSTNAME:-}" ]]   && export ROS_HOSTNAME="$OCEANUS_ROS_HOSTNAME"
  command -v rostopic >/dev/null 2>&1 || die "source 之后仍然没有 rostopic —— ROS 环境没起来"
  log "ROS 已 source：master=${ROS_MASTER_URI:-<未设置>} host=${ROS_HOSTNAME:-${ROS_IP:-<未设置>}}（主机 $(hostname)）"
  # master 连不上时早说，别等到起 supervisor 才失败
  if ! timeout 10 rostopic list >/dev/null 2>&1; then
    warn "rostopic list 失败 —— 连不上 ROS master（${ROS_MASTER_URI:-<未设置>}）"
    warn "  若 master 就在本机：先起 roscore。若在别的机器：核对 ROS_MASTER_URI / ROS_IP 与网络。"
    warn "  ssh 非交互调用时可以这样补：OCEANUS_ROS_MASTER_URI=http://<host>:11311 bash run_autopick.sh ..."
    warn "  下一步会自动重试；仍失败则以退出码 4 结束。"
  fi
else
  log "mock 模式：不 source ROS、不接硬件（合成画面 + 模拟舵机）"
fi

[[ -f "$CONFIG" ]] || die "配置不存在：$CONFIG"

# 串口权限：舵机控制器要能打开 /dev/ttyS0
if [[ "$MOCK" -eq 0 ]]; then
  if [[ -e /dev/ttyS0 ]]; then
    if [[ ! -r /dev/ttyS0 || ! -w /dev/ttyS0 ]]; then
      warn "/dev/ttyS0 不可读写（当前用户 $(id -un)）—— 舵机控制器可能起不来"
      warn "修：sudo usermod -aG dialout $(id -un) 后重新登录；或 sudo chmod 666 /dev/ttyS0"
    fi
  else
    warn "/dev/ttyS0 不存在 —— 确认板子型号与舵机总线设备名"
  fi
fi

# ---------------------------------------------------------------- 1) 厂商 bringup
BRINGUP_STARTED=0

# 收尾时按 **launch 文件名** 精确回收本脚本起的 roslaunch。
#
# 为什么不用「杀掉自己的进程组」：setsid 会给每次 roslaunch 各建一个进程组，
# 三个 launch 分属三组，记一个 pgid 只能收掉其中一个，剩下两个会继续占着
# 串口与摄像头 —— 下次启动就撞「/dev/ttyS0 被占」。所以按名字回收。
# 为什么不用 pkill -f roslaunch：那会连别人手工起的 ROS 节点一起杀。
# 这里只匹配本子系统的三个 launch 文件，范围明确。
LAUNCH_FILES=(
  armpi_fpv_bringup/start_dependence.launch
  armpi_fpv_bringup/start_camera.launch
  hiwonder_servo_controllers/controller_manager.launch
)

stop_bringup() {
  [[ "$BRINGUP_STARTED" -eq 1 ]] || return 0
  if [[ "$KEEP_BRINGUP" -eq 1 ]]; then
    warn "--keep-bringup：保留厂商节点不回收"
    return 0
  fi
  log "收尾：回收厂商 bringup 节点"
  local f pids
  for f in "${LAUNCH_FILES[@]}"; do
    pids="$(pgrep -f -- "$f" 2>/dev/null || true)"
    [[ -n "$pids" ]] || continue
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
  done
  # 给 roslaunch 时间让它自己把子节点（含持有串口的 servo_manager）收干净
  local i
  for i in $(seq 1 20); do
    local alive=""
    for f in "${LAUNCH_FILES[@]}"; do
      alive="$alive$(pgrep -f -- "$f" 2>/dev/null || true)"
    done
    [[ -z "$alive" ]] && { log "  厂商节点已全部退出"; return 0; }
    sleep 0.25
  done
  warn "厂商节点 5s 内没退干净，发 SIGKILL"
  for f in "${LAUNCH_FILES[@]}"; do
    pids="$(pgrep -f -- "$f" 2>/dev/null || true)"
    # shellcheck disable=SC2086
    [[ -n "$pids" ]] && kill -KILL $pids 2>/dev/null || true
  done
}

# 整脚本退出（含 systemd stop / Ctrl-C）时一定要把 ROS 节点带走，
# 否则下次启动会撞上「串口被上一次的节点占着」。
# ★ 只在 EXIT 上挂一次：真按了 Ctrl-C，脚本会在 supervisor 收尾后正常走到 exit，
#   不在这里抢着退，免得厂商节点还没停干净就返回。
cleanup() {
  local rc=$?
  stop_bringup
  exit "$rc"
}
trap cleanup EXIT

start_bringup() {
  local launch_file="$1" log_name="$2"
  # setsid：让 roslaunch 脱离本脚本的会话，本脚本被 kill 时它不会跟着一起被信号带走，
  # 从而始终由 stop_bringup 统一回收（有始有终，不会留下孤儿占着串口）。
  setsid roslaunch "$launch_file" >"$LOG_DIR/$log_name.log" 2>&1 &
  echo $!
}

wait_topic() {
  # wait_topic <话题> <说明> [--frames]
  local topic="$1" label="$2" need_frames="${3:-}"
  local deadline=$(( SECONDS + TOPIC_TIMEOUT ))
  while (( SECONDS < deadline )); do
    if rostopic list 2>/dev/null | grep -qx -- "$topic"; then
      if [[ "$need_frames" == "--frames" ]]; then
        # 话题在列表里 ≠ 真在出帧（发布者起来了但设备没打开是常见静默失败）
        if timeout 3 rostopic echo -n 1 "$topic" >/dev/null 2>&1; then
          log "  ✓ $label（$topic 已有数据）"
          return 0
        fi
      else
        log "  ✓ $label（$topic 已在列表）"
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

if [[ "$SKIP_BRINGUP" -eq 0 && "$MOCK" -eq 0 ]]; then
  log "启动厂商 bringup（依赖 / 相机 / 舵机控制器；不含 start_functions.launch）"

  PID_DEP="$(start_bringup "${LAUNCH_FILES[0]}" dependence)"
  PID_CAM="$(start_bringup "${LAUNCH_FILES[1]}" camera)"
  PID_SRV="$(start_bringup "${LAUNCH_FILES[2]}" servo)"
  BRINGUP_STARTED=1
  log "  roslaunch pid：dependence=$PID_DEP camera=$PID_CAM servo=$PID_SRV"
  log "  日志：$LOG_DIR/{dependence,camera,servo}.log"

  ok=0
  if wait_topic /usb_cam/image_raw "相机出帧" --frames; then
    if wait_topic /servo_controllers/port_id_1/servo_states "舵机控制器回读"; then
      ok=1
    fi
  fi
  if [[ "$ok" -ne 1 ]]; then
    warn "厂商节点 ${TOPIC_TIMEOUT}s 内没就绪 —— 逐个看日志定位：
    tail -n 50 $LOG_DIR/camera.log
    tail -n 50 $LOG_DIR/servo.log
    tail -n 50 $LOG_DIR/dependence.log
  常见原因：/dev/ttyS0 被占（上次的节点没退干净）/ 摄像头没插好 / 编译产物缺失"
    exit 4
  fi
  log "厂商链路就绪（相机 + 舵机控制器都在出数据）"
elif [[ "$SKIP_BRINGUP" -eq 1 ]]; then
  log "--skip-bringup：假定厂商节点已由外部启动"
fi

# ---------------------------------------------------------------- 2) 只读自检
log "运行启动自检（模型 / 标定 / 放置点 / 相机；只读，不动机械臂）"
PRECHECK_ARGS=(--runtime-only)
[[ "$MOCK" -eq 1 ]] && PRECHECK_ARGS=(--mock --runtime-only)
[[ -n "$CONFIG" ]] && PRECHECK_ARGS+=(--config "$CONFIG")

if "$PY" "$GRASP/tools/precheck.py" "${PRECHECK_ARGS[@]}"; then
  log "自检通过"
else
  rc=$?
  warn "自检未通过（退出码 $rc）—— 按上面每行的提示补齐后重跑本脚本"
  warn "作业台（采集/标定/示教/部署）：http://<板子IP>:8090"
  exit 2
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  log "--check：只做检查，不启动拾取"
  exit 0
fi

# ---------------------------------------------------------------- 3) supervisor
SUP_ARGS=()
[[ "$MOCK" -eq 1 ]] && SUP_ARGS+=(--mock)
[[ "$ONCE" -eq 1 ]] && SUP_ARGS+=(--once)
[[ -n "$CONFIG" ]] && SUP_ARGS+=(--config "$CONFIG")

log "启动常驻自捡 supervisor：${PY} -m grasp.supervisor ${SUP_ARGS[*]}"
log "⚠ 本进程与 arm_bridge 互斥（都会驱动同一台机械臂），同一时刻只能跑一个"

# 不 exec：本脚本必须活着才能在退出时把厂商 ROS 节点一起带走
PYTHONUNBUFFERED=1 "$PY" -m grasp.supervisor "${SUP_ARGS[@]}" &
SUP_PID=$!

# 转发停止信号给 supervisor（systemd stop → 优雅收尾 → 再收 ROS 节点）
forward_stop() {
  log "收到停止信号，转给 supervisor（PID $SUP_PID）"
  kill -TERM "$SUP_PID" 2>/dev/null || true
}
trap 'forward_stop' INT TERM

wait "$SUP_PID"
RC=$?
trap cleanup EXIT
log "supervisor 退出，退出码 $RC"
exit "$RC"
