#!/usr/bin/env bash
# ============================================================
# Oceanus 机械臂边缘服务 · systemd 开机自启安装脚本（在树莓派上运行）
# ============================================================
# 用法：
#   sudo bash install.sh --repo /home/pi/oceanus --broker 192.168.137.1 --mode autopick
#
# 必填：
#   --repo      本仓库在树莓派上的根目录（形如 /home/pi/oceanus）
#   --broker    平台那侧 MQTT broker 的地址（笔记本 ICS 地址 / 服务器 IP）
#
# 可选：
#   --mode      autopick（默认）| dispatch | console | all
#                 autopick  开机后自己反复找目标抓，不依赖平台派单
#                 dispatch  等平台派单，派一次抓一个
#                 console   只起作业台网页 :8090（采集 / 标注 / 训练 / 部署 / 标定都在这做）
#                 all       三套单元文件都装，但只 enable console；真机作业模式自己二选一
#   --pi-user   运行服务的用户（默认 pi）
#   --ros-setup 厂商工作空间的 setup.bash（默认 ~/armpi_fpv/devel/setup.bash）
#   --ros-master ROS master 地址（默认 http://127.0.0.1:11311，即板子本机）
#   --ros-ip     本机对外的 ROS_IP（默认取 hostname -I 的第一个地址）
#
# ★ autopick 与 dispatch 互斥：两者都会驱动同一台机械臂，脚本会自动 disable 另一个，
#   避免两个进程同时下发舵机指令抢总线。
#
# 装完看状态：
#   systemctl status oceanus-arm-autopick
#   journalctl -u oceanus-arm-autopick -f
#
# 上真机前先自检（模型 / 标定 / 相机 / 放置点四项前置）：
#   cd <repo> && bash edge/grasp/deploy/run_autopick.sh --check
#
# ★ autopick 单元跑的是 run_autopick.sh（不是直接跑 supervisor）：
#   它负责起厂商三组 launch、等话题就绪、自检、再起 supervisor 常驻自捡，
#   退出时按 launch 名回收厂商节点（避免串口被上一次的节点占着）。
#   完整说明见 edge/grasp/deploy/README.md。
set -euo pipefail

REPO=""
BROKER=""
MODE="autopick"
PI_USER="pi"
ROS_SETUP="$HOME/armpi_fpv/devel/setup.bash"
# ROS master：留空则填本机（roslaunch 会自分 roscore）。
# master 跑在另一台机器时用 --ros-master http://<host>:11311 显式指定。
ROS_MASTER=""
ROS_IP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)       REPO="${2:-}"; shift 2 ;;
    --broker)     BROKER="${2:-}"; shift 2 ;;
    --mode)       MODE="${2:-}"; shift 2 ;;
    --pi-user)    PI_USER="${2:-}"; shift 2 ;;
    --ros-setup)  ROS_SETUP="${2:-}"; shift 2 ;;
    --ros-master) ROS_MASTER="${2:-}"; shift 2 ;;
    --ros-ip)     ROS_IP="${2:-}"; shift 2 ;;
    -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$REPO" ]]   || { echo "缺少 --repo" >&2; exit 2; }
[[ -n "$BROKER" ]] || { echo "缺少 --broker" >&2; exit 2; }
[[ -d "$REPO/edge" ]] || { echo "$REPO/edge 不存在：--repo 要指向仓库根目录" >&2; exit 2; }
[[ "$(id -u)" -eq 0 ]] || { echo "请用 sudo 运行" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/etc/systemd/system
UNITS=(oceanus-grasp-console oceanus-arm-bridge oceanus-arm-autopick)

# master 默认本机：roslaunch 起 roscore 时就是 127.0.0.1:11311
[[ -n "$ROS_MASTER" ]] || ROS_MASTER="http://127.0.0.1:11311"
# __AUTO__ = 让脚本自己推断本机地址（部署脚本从 Windows 传空值时会转成它）
[[ "$ROS_IP" == "__AUTO__" ]] && ROS_IP=""
if [[ -z "$ROS_IP" ]]; then
  # 取本机在局域网里的地址给 ROS_IP：板子与平台可能跨机通信，
  # 留空时 rospy 会用 hostname 解析，容器/多网卡环境下常解析不到。
  ROS_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -n "$ROS_IP" ]] || ROS_IP="127.0.0.1"
fi

install_unit() {
  local name="$1"
  sed -e "s|__REPO__|$REPO|g" \
      -e "s|__PI_USER__|$PI_USER|g" \
      -e "s|__BROKER__|$BROKER|g" \
      -e "s|__ROS_SETUP__|$ROS_SETUP|g" \
      -e "s|__ROS_MASTER__|$ROS_MASTER|g" \
      -e "s|__ROS_IP__|$ROS_IP|g" \
      "$HERE/$name.service" > "$DEST/$name.service"
  echo "  写出 $DEST/$name.service"
}

echo "安装 systemd 单元 → $DEST"
for u in "${UNITS[@]}"; do install_unit "$u"; done
systemctl daemon-reload

# autopick 由 run_autopick.sh 托管厂商 ROS 节点，脚本会把 roslaunch 日志写到
# /var/log/oceanus-autopick。以 __PI_USER__ 身份运行却写 /var/log 需要目录先归它，
# 否则日志静默写不进去、排障时「一条 launch 日志都没有」。
LOG_DIR=/var/log/oceanus-autopick
mkdir -p "$LOG_DIR"
chown "$PI_USER:$PI_USER" "$LOG_DIR"
echo "  日志目录 $LOG_DIR 已归 $PI_USER"

# 启动脚本必须可执行（从 Windows 拷过来时常丢可执行位）
RUN_SH="$REPO/edge/grasp/deploy/run_autopick.sh"
if [[ -f "$RUN_SH" ]]; then
  chmod +x "$RUN_SH"
  echo "  已置可执行位：$RUN_SH"
else
  echo "  ⚠ 找不到 $RUN_SH —— autopick 起不来，检查 --repo 是否指向仓库根目录" >&2
fi

enable_only() {
  local target="$1"
  for u in "${UNITS[@]}"; do
    if [[ "$u" == "$target" ]]; then
      systemctl enable --now "$u"
      echo "已启用并启动：$u"
    else
      systemctl disable --now "$u" >/dev/null 2>&1 || true
    fi
  done
}

case "$MODE" in
  autopick) enable_only oceanus-arm-autopick ;;
  dispatch) enable_only oceanus-arm-bridge ;;
  console)  enable_only oceanus-grasp-console ;;
  all)
    for u in "${UNITS[@]}"; do
      systemctl disable --now "$u" >/dev/null 2>&1 || true
    done
    systemctl enable --now oceanus-grasp-console
    echo "三套单元都已装好；当前只启用 oceanus-grasp-console（作业台 :8090）。"
    echo "真机作业模式二选一（互斥，别同时开）："
    echo "  sudo systemctl enable --now oceanus-arm-autopick   # 常驻自捡"
    echo "  sudo systemctl enable --now oceanus-arm-bridge     # 平台派单"
    ;;
  *) echo "未知 --mode：$MODE" >&2; exit 2 ;;
esac

echo
echo "下一步：先自检四项前置，再让它跑"
echo "  cd $REPO && bash edge/grasp/deploy/run_autopick.sh --check"
echo "  自检通过后启用常驻自捡：sudo systemctl enable --now oceanus-arm-autopick"
echo "  看作业实况：journalctl -u oceanus-arm-autopick -f"
