#!/usr/bin/env bash
# 启动本机平台后端（Oceanus / 海漂监测）
#
# ★ 首次运行**必须清卷**（down -v），否则 EMQX 的 bootstrap.csv 不生效 ——
#   它只在「内置数据库为空时」导入。之前设的密码会继续用旧的
#   （或全是 CHANGE_ME），然后你会花两小时排查"为什么密码没变"。
#
# 用法：
#   bash scripts/start_local_platform.sh          # 首次（或改过 bootstrap.csv）
#   bash scripts/start_local_platform.sh --keep   # 日常启动，保留数据卷
#   bash scripts/start_local_platform.sh --status # 只看状态

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:-first}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. 前置检查 ──────────────────────────────────────────────
say "检查 Docker 是否在运行"
if ! docker info >/dev/null 2>&1; then
  die "Docker 守护进程未运行。
  请**手动启动 Docker Desktop**（我无法从这里启动 GUI 应用），
  等托盘图标显示容器已就绪后重跑本脚本。"
fi
docker version --format '  Docker Server {{.Server.Version}}'

# ── 2. 凭据 ─────────────────────────────────────────────────
if [ ! -f .env ]; then
  warn "未找到 .env，正在生成凭据…"
  python scripts/gen_local_secrets.py >/dev/null
  say "凭据已生成（明文只落 .env，已 gitignore）"
  # 新生成的凭据必须配清卷，否则 EMQX 还是用旧哈希
  MODE="first"
  warn "检测到新凭据 → 强制使用清卷模式"
else
  say ".env 已存在，跳过凭据生成"
  # 校验 bootstrap 里的哈希与 .env 是否一致
  python - <<'PY'
import hashlib, io, re, sys
env = io.open('.env', encoding='utf-8').read()
boot = io.open('deploy/emqx/bootstrap.csv', encoding='utf-8').read()
def val(k):
    m = re.search(r'^%s=(.+)$' % re.escape(k), env, re.M)
    return m.group(1).strip() if m else ''
pairs = [('backend_service', 'MQTT_PASSWORD'),
         ('robot_device', 'ARM_MQTT_PASSWORD')]
bad = []
for acct, key in pairs:
    pw = val(key)
    if not pw:
        continue
    h = hashlib.sha256(pw.encode()).hexdigest()
    m = re.search(r'^mqtt_user,%s,([0-9a-f]{64})' % acct, boot, re.M)
    if m and m.group(1) != h:
        bad.append(acct)
if bad:
    print('  [!] 以下账号的 bootstrap 哈希与 .env 不一致: %s' % ', '.join(bad))
    print('      → 必须清卷重启，否则 EMQX 不导入新哈希')
    sys.exit(3)
print('  bootstrap.csv 哈希与 .env 一致')
PY
  rc=$?
  if [ "$rc" = "3" ]; then
    MODE="first"
    warn "凭据与 EMQX 初始化数据不一致 → 强制使用清卷模式"
  fi
fi

# ── 3. 启动 ─────────────────────────────────────────────────
case "$MODE" in
  --status|status)
    docker compose ps
    exit 0
    ;;
  --keep|keep)
    say "启动（保留数据卷）"
    docker compose up -d
    ;;
  first|"")
    warn "清卷启动 —— EMQX 的 bootstrap 只在库空时导入"
    docker compose down -v
    say "启动"
    docker compose up -d
    ;;
  *)
    die "未知参数：$MODE（用 --keep 或 --status）"
    ;;
esac

# ── 4. 等待就绪并报告 ───────────────────────────────────────
say "等待服务就绪（最多 120 秒）…"
deadline=$((SECONDS + 120))
while [ $SECONDS -lt $deadline ]; do
  if docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null \
       | grep -q '^emqx .*healthy'; then
    break
  fi
  sleep 3
done

echo
say "当前状态"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Ports}}' 2>/dev/null \
  || docker compose ps

echo
say "关键端口"
for pair in "1883:MQTT" "8083:MQTT-WS" "18083:EMQX-Dashboard" \
            "18000:后端API" "15432:PostgreSQL" "9000:MinIO"; do
  port="${pair%%:*}"; name="${pair##*:}"
  if timeout 2 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port" 2>/dev/null; then
    printf '  \033[1;32m✓\033[0m %-18s %s OPEN\n' "$port" "$name"
  else
    printf '  \033[1;33m·\033[0m %-18s %s 未就绪\n' "$port" "$name"
  fi
done

cat <<'EOF'

下一步：
  1) MQTT 测试（用 .env 里的账号，别用 CHANGE_ME）：
       docker compose exec emqx /opt/emqx/bin/emqx ctl clients list
  2) 机械臂 bridge 上树莓派（需树莓派能出网装 paho-mqtt）：
       PI_PASSWORD=xxx python scripts/deploy_bridge_to_pi.py --backend ros_arm_control
  3) EMQX Dashboard（账号见 .env 的 MQTT_DASHBOARD_*）：
       http://localhost:18083

注意：EMQX 的 bootstrap.csv 改密码后**必须** down -v 才生效。
EOF
