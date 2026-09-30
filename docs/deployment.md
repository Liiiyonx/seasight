# 部署指南

> 生产服务器使用 `docker-compose.prod.yml` + 宿主机 Nginx；本地开发可使用
> 开发版 Compose，无 Docker 时也可裸机运行。

---

## 一、环境要求

### 1.1 Docker 模式

| 组件 | 最低 | 推荐 |
| --- | --- | --- |
| Docker | 20.10 | 最新稳定版 |
| Docker Compose | v2（`docker compose` 子命令） | v2.20+ |
| 内存 | 6 GB | 8 GB |
| 磁盘 | 20 GB | 40 GB |
| CPU | 2 核 | 4 核 |

### 1.2 裸机模式

| 组件 | 版本 | 备注 |
| --- | --- | --- |
| Python | 3.11 ~ 3.13 | 3.11 最稳（依赖轮子齐全） |
| Node.js | 20 LTS / 22 | 前端构建 |
| PostgreSQL | 14+ | **必须带 PostGIS 扩展** |
| Redis | 7+ | 需要 Streams 支持（5.0+ 即可，7 更好） |
| EMQX | 5.x | 或任意 MQTT 3.1.1+ broker（Mosquitto 也行） |
| MinIO | 任意 | 可选，不上传证据帧时不需要 |
| go2rtc | 最新 | 可选，没有真实摄像头时不需要 |

**Windows 裸机注意**：PostGIS 官方不提供 Windows 安装包，最省事的做法是只把 PostgreSQL 跑在 Docker 里，其余跑裸机。

---

## 二、Docker Compose 部署（推荐）

### 2.0 生产服务器部署（docker-compose.prod.yml）

生产编排与开发编排完全分离。生产配置有以下硬约束：

- 只有前端容器绑定宿主机 `127.0.0.1:8080`，由宿主机 Nginx 终止 TLS 并反向代理。
- PostgreSQL、Redis、MinIO、后端、AI 服务只在 Compose 私有网络内通信。
- EMQX 默认只绑定回环地址；真实设备接入时，把 `MQTT_BIND_ADDR` 改成受防火墙
  或 VPN 保护的设备网卡地址。
- PostgreSQL 首启只执行 `01_schema.sql`，**绝不执行 `02_seed.sql`**。
- MinIO bucket 为私有，不开放匿名下载。
- 生产启动会拒绝占位密钥、默认密码、`DEBUG=true` 和 `CORS_ORIGINS=*`。

```bash
# 1. 准备生产环境变量
cp .env.production.example .env.production
# 替换全部 CHANGE_ME；域名、数据库、MQTT、MinIO、EMQX 都使用独立强密码。
# 地图功能还需填写 VITE_TMAP_PROXY_URL，见下方说明

# 2. 校验 Compose 与环境变量
make prod-config

# 3. 生成 EMQX 首次启动凭据（输出文件已 gitignore）
make prod-mqtt-bootstrap

# 3.1 机械臂若接同一 broker，先确认 .env.production 已配置
#     MQTT_ROBOT_USERNAME / MQTT_ROBOT_PASSWORD，再重新生成 bootstrap。
#     已经跑起来的 EMQX 不会因改 bootstrap 文件而生效：开发环境删
#     emqx_data 卷重导，生产环境进 Dashboard 在线补账号与 ACL。

# 4. 构建并启动
make prod-up
make prod-ps

# 5. 新库将 01_schema.sql 对齐到当前 Alembic head
make prod-migrate-stamp

# 6. 创建正式管理员（交互式输入至少 12 位密码）
make prod-create-admin

# 7. 创建 t_track 未来 14 天分区（运维兜底；应用侧已能自愈，见 2.0.2-②）
make prod-track-partitions
```

### 2.0.1 把本机改动同步到服务器（已改过代码后）

本仓库**没有 git remote**，也没有 rsync，所以"同步"走的是打包上传，三个脚本串起来：

```bash
# ① 本机：生成源码包 + SHA256 清单（artifacts/deploy/*.tar.gz）
python scripts/deploy_manifest.py

# ② 本机 → 服务器：上传这一个文件即可
scp artifacts/deploy/seasight-src-<时间戳>.tar.gz <user>@<host>:/tmp/

# ③ 服务器上：解包到仓库根 + 校验传输完整性
tar -xzf /tmp/seasight-src-<时间戳>.tar.gz -C /path/to/seahawk
cd /path/to/seahawk && sha256sum -c /tmp/seasight-src-<时间戳>.MANIFEST.sha256

# ④ 服务器上：重建（前端在容器内构建，改了代码必须重建，只重启无效）
make prod-up

# ⑤ 回本机核对线上是否真的生效（只读，不改服务器）
python scripts/deploy_verify.py
```

**包里刻意没有的东西**（`scripts/deploy_manifest.py` 的排除清单，不要放宽）：

- `.env` / `.env.production` —— 服务器上的真实密钥**绝不能被本机覆盖**；
- `artifacts/` —— 运行产物由服务器自己生成（评测产物如需覆盖用 `--include-artifacts` 显式声明）；
- `node_modules/` `.venv*/` `dist/` `__pycache__/` `.git/` `*.pt` —— 服务器端自行构建。

### 2.0.2 三处最容易漏的部署前置（2026-09-27 线上验收实测踩到）

这三条都属于「配置不写全 → 功能静默失效 / 直接 500」，而现象都不在自己那一层，
所以单列出来。

**① 图片分析要 opencv + `edge/` 挂到容器**

`POST /ai/analyze-image`（以及助手工具 `image.analyze`）会在容器内 import
`edge/detector` 里的 OpenCV 检测器。两件事必须同时成立：

```bash
# a. 镜像里有 cv2/numpy —— 已写进 backend/requirements.txt，重新 make prod-up 即可
grep -n "opencv-python-headless\|numpy" backend/requirements.txt

# b. edge/ 挂到了容器里（docker-compose.prod.yml 已声明 ./edge:/edge:ro）
docker compose -f docker-compose.prod.yml exec backend ls /edge/detector
```

少了 a 或 b，接口不会崩，而是返回业务码 **5001** 并说明缺什么
（`app/api/v1/ai.py` 已把 ImportError 变成这条可读报错，不再是裸 500）——
看到 5001 就按上面的顺序查，不要去翻模型权重。

> 注意：**不要把图片分析改道走 ai-service**。ai-service 在模型缺失时会返回
> 哈希生成的**假检测框**（响应里带 `note: "stub 模式模拟结果"`），
> 与本项目「识别可以不准，但绝不能假」的口径冲突。

**② `t_track` 是按日分区表，缺分区遥测整条写不进去**

报错长相是 `CheckViolationError: no partition of relation "t_track" found for row`，
症状是「点工单仿真 → 弹出红色异常」。历史上依赖人工跑 SQL 建分区，
漏一天就炸。

现在有**两道保障**：

| 保障 | 位置 | 何时生效 |
| --- | --- | --- |
| 应用侧自愈（主） | `backend/app/db/partitions.py`，在 MQTT 遥测写库路径上调用 | 每次遥测写入前，自动补「今天 ~ 今天+2 天」分区 |
| 运维兜底 | **`make prod-track-partitions`** | 手工执行，或挂 pg_cron |

> `make ensure-partitions` 是**开发环境**入口，走的是 `docker-compose.yml`，
> 且依赖容器内挂载的 `/sql` 目录（两个 compose 都挂了 `./deploy/postgres:/sql:ro`）。
> 服务器上只有 `.env.production` + `docker-compose.prod.yml`，**用它必失败** ——
> 生产一律用 `make prod-track-partitions`（它改走 `-f /dev/stdin`，不依赖挂载点）。
> 两个入口的 `##` 说明里已分别标注「开发环境 / 生产」，就是防止在服务器上挑错。
> 刻意**不**挂到 `docker-entrypoint-initdb.d`：那目录只在数据卷为空的首次启动才执行，
> 「补下个月的分区」这种持续需求它担不了。

**③ 演示账号：登录页展示了 4 个，目标库里必须真有 4 个**

生产首启**只跑 `01_schema.sql`，绝不跑 `02_seed.sql`**（`02_seed.sql` 会 TRUNCATE `t_user`），
所以线上的 `approver` / `viewer` 等演示账号**不存在**，需要显式补建：

```bash
# 服务器上（幂等，可反复执行）
make prod-bootstrap-demo-users
```

> **★ 这里必须用 `prod-bootstrap-demo-users`，不要用 `bootstrap-demo-users`。**（2026-09-27 在服务器上实测踩到，两条雷连着炸。）
>
> **雷 1 —— compose 选错：** `bootstrap-demo-users` 是**开发**入口，走的是
> `docker-compose.yml` + 仓库根的 `.env`；服务器上只有 `.env.production`，
> 于是插值阶段就死：
>
> ```
> error while interpolating services.postgres.environment.POSTGRES_PASSWORD:
>   required variable POSTGRES_PASSWORD is missing a value: 请在 .env 中设置 POSTGRES_PASSWORD
> make: *** [Makefile:165: bootstrap-demo-users] Error 1
> ```
>
> **雷 2 —— 调用形式错：** 容器 `WORKDIR=/app`，脚本必须用 `python -m scripts.x`。
> 写成 `python scripts/x.py` 时 Python 把 `sys.path[0]` 设成**脚本所在目录**
> `/app/scripts`，脚本首行的 `from app.core.config import settings` 直接：
>
> ```
> ModuleNotFoundError: No module named 'app'
> ```
>
> **雷 3 —— 生产闸门：** `bootstrap_demo_users.py` 在 `APP_ENV=production` 下
> 必须显式传 `--confirm-production`，否则 `SystemExit`。这个闸门是**有意设计**的
> （防止误改生产用户表），不要靠去掉它来绕过。
>
> 两个入口现在都写成 `-m`，生产入口额外带 `--confirm-production`；
> 镜像里也补了 `PYTHONPATH=/app` 兜底，让人手敲的 `python scripts/xxx.py` 也不炸。
> 形状由 `backend/tests/test_container_script_invocation.py`（16 条，含扫描器自证与变异验证）
> 与 `backend/tests/test_demo_approval_probe_scope.py`（4 条，钉住「开发环境项不得在远端目标上判失败」）钉住。

登录页一键填入的四个账号（`frontend/src/components/LoginDemoPanel.vue`）与
`backend/scripts/bootstrap_demo_users.py`、`02_seed.sql` 三份清单必须一致。
静态核对 + 真实登录探测：

```bash
make check-demo-approval                                      # 静态：三份清单是否一致
make check-demo-accounts PROBE_URL=https://<host>/seasight    # 真实：四个账号真的能登进去
```

> **★ 在服务器上跑 make 的两个前提**（2026-09-27 实测踩到，都是 127/not found 一类）：
>
> 1. **`python` 在服务器上不存在。** Ubuntu 只有 `python3`；而 Windows 开发机上反过来。
>    Makefile 里 23 个宿主机侧目标原先都裸写 `python`，于是在服务器上齐刷刷
>    `/bin/bash: line 1: python: command not found` + `Error 127` ——
>    偏偏交付文档让运维敲的正是这几条。现已统一抽成 `PYTHON ?=`（`command -v python3` 探测）。
>    → 容器内的 `docker compose exec backend python ...` **不在此列**，那是镜像里的解释器。
> 2. **生产相关目标必须带 `--env-file .env.production -f docker-compose.prod.yml`**
>    （即 `$(PROD_COMPOSE)`）。裸 `docker compose` 走的是开发 compose + 仓库根 `.env`，
>    服务器上两者都没有。

`check-demo-accounts` 会逐个账号打登录接口、核对 `/auth/me` 返回的角色，
并用「不存在的审批 id」探一次审批门禁（admin/approver 放行、operator/viewer 拒绝），
**全程只读，不写任何数据**。这一步能查出静态核对查不出来的问题 ——
2026-09-27 的 P0-5（approver 登不上，401）就是这样发现的。

2026-09-28 线上复验：`approver` 账号已存在，四个演示账号登录全部 `200`，
角色与审批门禁均符合预期，登录验收报告 `passed: true`
（产物见 `artifacts/prod-login-verification/latest.json`）。

**三个已知的坑**：

1. **这台服务器还跑着聆心**（同一张 `lingxin.crt` 证书、同一个 443 server 块，
   `include .../snippets/seasight.conf`）。改 `/etc/nginx/**` 前先想清楚影响面，
   任何 `nginx -t` 失败都可能把两个项目一起带下线。
2. **生产编排没有透传 `AGENT_*` / `ASSISTANT_*`**（与"生产默认与生产策略一致"相符），
   所以线上**不会**触发审批高光，也不会自动建演示账号（生产首启只跑 `01_schema.sql`，
   绝不跑 `02_seed.sql`）。账号可以按需显式补建（见 2.0.2-③），
   但审批开关不在生产开。→ **录"审批流"这段镜头请在本地/演示机做**，不要在生产上录。
3. `deploy_verify.py` 默认按 `https://<host>/seasight/` 探测；路径前缀改过的话用 `--url` 传。
   证书是共享域名的自签证书，脚本默认跳过校验，加 `--no-insecure` 则严格校验。

宿主机 Nginx 直接安装本项目提供的完整配置：

```bash
sudo cp deploy/nginx/seasight.conf /etc/nginx/conf.d/seasight.conf
sudo editor /etc/nginx/conf.d/seasight.conf   # 改 server_name 与证书
sudo nginx -t
sudo systemctl reload nginx
```

如果 SeaSight 与其他应用共用域名，例如挂载在 `https://example.cn/seasight/`，
不要覆盖宿主机已有的 HTTPS server 块。改为把 `deploy/nginx/seasight-path.conf`
安装到 `/etc/nginx/snippets/seasight.conf`，再在现有 HTTPS server 块中 include：

```nginx
include /etc/nginx/snippets/seasight.conf;
```

同时把生产环境改为以下三项并重新构建前端：

```dotenv
VITE_BASE_PATH=/seasight/
STREAM_PUBLIC_BASE_URL=/seasight
CORS_ORIGINS=https://example.cn
```

`VITE_BASE_PATH` 是前端构建参数，修改后必须重新执行 `make prod-up`，只重启容器
不会生效。`deploy/nginx/host-lingxin.conf` 是当前共享域名部署的参考 server 块。

对公网只开放 80/443。不要直接放通 `8000`、`1984`、`9000`、`18083`、
`5432`、`6379` 或 `1883`。EMQX Dashboard 仅在服务器本机通过
`http://127.0.0.1:18083` 访问，需要时使用 SSH 隧道。

`VITE_TMAP_PROXY_URL` 是腾讯地图密钥代理地址，由 Vite 在**前端镜像构建时**
写入静态产物。未配置时页面仍可运行，但地图面板会明确提示“地图服务未配置”。
因此修改该值后必须重新执行 `make prod-build`（或 `make prod-up`）重建前端，
仅重启容器不会生效。

> 本机没有 Docker CLI 时无法代替服务器执行 `prod-config/prod-up`。这属于
> 部署环境验收项，必须在目标服务器上实际跑一遍，不能只看静态配置。

### 2.0.3 机械臂接入的 EMQX 前置

真机械臂到货前，下面这些准备就能做完，不需要任何物理拾取动作：

1. 在 `.env.production` 增加并替换强密码：
   `MQTT_ROBOT_USERNAME=robot_device`、`MQTT_ROBOT_PASSWORD=<强密码>`。
2. 重新生成 bootstrap：`make prod-mqtt-bootstrap`。已经跑起来的 EMQX 不会因
   改文件自动生效：开发环境删除 `emqx_data` 卷重导；生产环境进 Dashboard
   在线补账号与 ACL。
3. ACL 需要放行：订阅 `robot/+/task`、`robot/+/cmd`；发布
   `robot/+/cmd/ack`、`robot/+/task/progress`、
   `marine/+/+/telemetry`、`marine/+/+/status`；其余 `deny all #`。
   完整主题表见 `docs/mqtt-topics.md` §7.2。
4. 在 `t_device` 注册机械臂，`device_type` 沿用 `robot`，不要新增枚举。
5. 真机到货前用平台工单仿真页或 `mosquitto_pub` 做协议级自测；到货后只替换
   设备侧回包程序，平台侧契约不需要改。完整清单见
   [机械臂对接准备](../项目文档/探海灵眸_机械臂对接准备.md)。

### 2.1 本地开发：准备环境变量

```bash
cp .env.example .env
```

编辑 `.env`，**以下几项必须修改**：

```bash
# 生成一个随机密钥（不要用示例值）
SECRET_KEY=<openssl rand -hex 32 的输出>

# 数据库密码
POSTGRES_PASSWORD=<强密码>

# MQTT 本地开发账号（默认 backend_service / CHANGE_ME）
MQTT_USERNAME=backend_service
MQTT_PASSWORD=CHANGE_ME

# MinIO 密码
MINIO_SECRET_KEY=<强密码>

# EMQX Dashboard 密码
MQTT_DASHBOARD_PASSWORD=<强密码>
```

> **安全红线**：`.env` 已在 `.gitignore` 中排除，**永远不要提交**。若已在本地存有敏感凭据文件，放到仓库之外的固定目录（如 `D:\VeriCall_data\secrets\`），用 `set -a; source <文件>; set +a` 注入环境。

> `deploy/emqx/bootstrap.csv` 是开发用数据，密码就是 `CHANGE_ME`。如果改了
> `MQTT_PASSWORD`，必须同步重新生成 bootstrap 并删除 `emqx_data` 卷，否则
> EMQX 内已导入的旧凭据不会更新。生产环境不要复用开发凭据，统一走
> `.env.production` + `make prod-mqtt-bootstrap`。

### 2.2 启动

```bash
# 启动全部依赖服务（postgres / redis / emqx / minio / go2rtc）
docker compose up -d

# 查看状态，等 healthcheck 全部变 healthy
docker compose ps
```

首次启动 `postgres` 会自动执行 `backend/db/init/` 下的 SQL（按文件名顺序）：

1. `01_schema.sql` —— 建表、枚举、索引、触发器、分区
2. `02_seed.sql` —— 种子数据（6 摄像头 + 1 无人机 + 3 机器人 + 14 条事件）

> **注意**：`docker-entrypoint-initdb.d` 只在数据卷**为空**时执行。若已启动过一次再改 SQL，需要 `make db-reset`（会丢数据）或手动执行。

### 2.3 初始化数据库（数据卷已存在时）

```bash
make db-init
```

### 2.3.1 数据库迁移（Alembic）—— schema 是「双轨制」

先说清架构，否则下面每条命令都会用错：

| 轨道 | 负责什么 | 工具 | 何时执行 |
| --- | --- | --- | --- |
| **A** | 初始建表、PostGIS 扩展、`t_event` 按月分区、触发器函数 | `backend/db/init/01_schema.sql` | postgres 容器**首次**启动时由 `docker-entrypoint-initdb.d` 自动执行 |
| **B** | 之后的一切增量变更（加字段、加索引、改约束） | Alembic | 由开发者显式执行 |

**为什么不全用 Alembic？** 因为 `01_schema.sql` 里有三样 Alembic 表达不了或表达起来很别扭的东西：`CREATE EXTENSION postgis`、`PARTITION OF` 分区子句、以及一组触发器函数。硬塞进 Alembic 会变成一大坨 `op.execute(...)` 原生 SQL，既失去 autogenerate 的价值，又多一层间接。

**双轨的对齐点**是那条空迁移 `20260918_1000_baseline`。当前 head 是
`20260919_1900_knowledge_assets`，`01_schema.sql` 已与 head 同步。新环境按以下顺序走：

```bash
# 1. 起数据库（自动跑 01_schema.sql + 02_seed.sql）
docker compose up -d postgres

# 2. ★ 把 baseline 标记为「已应用」——不执行任何 SQL，只是写一条版本记录
make migrate-stamp

# 3. 验证：应输出 20260919_1900_knowledge_assets (head)
make migrate-current
```

> **跳过第 2 步会怎样？** 第一次 `make migration` 时 autogenerate 会把
> `01_schema.sql` 建好的表当成「模型里有、数据库里没有」，于是生成一整串
> `op.create_table('t_event', ...)`。一执行就报 `relation "t_event" already exists`。
> 这是双轨制唯一容易踩的坑。

**日常变更流程**：

```bash
# 改完 app/models/ 下的模型后，按差异生成迁移
make migration m="给 t_event 加 severity 字段"

# 应用迁移
make migrate

# 查看历史
make migrate-history
```

> ⚠️ **autogenerate 的结果必须人工审一遍**。它对字段类型变更、默认值变更、
> 以及 PostGIS 几何列（`Geometry`/`Geography`）的判断并不总是可靠，
> 涉及分区表时更是完全不认。生成后请打开 `backend/alembic/versions/` 下的
> 新文件逐行确认，尤其是 `op.drop_*` 之类的破坏性语句 —— autogenerate
> 有时会把它当成「清理多余对象」而你没有察觉。

**给 DBA 审核用（离线导出，不连库）**：

```bash
make migrate-sql > migration.sql    # 产出纯 SQL，可交 DBA 审完再执行
```

**回退**：

```bash
cd backend && python -m alembic downgrade -1   # 回退一步
```

注意 baseline 的 `downgrade()` 是**刻意留空**的 —— 回退到建表之前意味着
`DROP` 掉全部业务表，那是灾难性操作，不该出现在随手敲的 `downgrade base` 里。
真要清库请用 `make db-reset`（有二次确认）。

#### ⚠️ 中文 Windows 上的 Alembic 编码坑（已绕过，但务必了解）

Alembic 读 `.ini` 文件时硬编码了 `encoding="locale"`：

```python
# alembic/util/compat.py:130
return file_config.read(file_argument, encoding="locale")
```

而 Python 的 `open(encoding="locale")` 直接取 `locale.getencoding()`，
**绕过 UTF-8 模式**。中文 Windows 上实测：

| 表达式 | 结果 |
| --- | --- |
| `locale.getencoding()` | `cp936`（GBK） |
| `open(encoding="locale").encoding` | `cp936` ← Alembic 走这条 |
| `open(encoding=None).encoding` | `utf-8`（因为设了 `PYTHONUTF8=1`） |

于是 `alembic.ini` 里只要有一个非 ASCII 字节（哪怕只是中文注释），
所有 `alembic` 子命令都会死在：

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0x80 in position 21
```

**因此本项目最终把 Alembic 主配置放在 `backend/alembic.ini`，并强制保持纯
ASCII**。曾经尝试把配置放进 `pyproject.toml`，但 Alembic 1.14 不会自动发现
TOML 配置，显式 `-c pyproject.toml` 仍会走 GBK configparser；该方案已废弃。
所有迁移命令必须使用 `python -m alembic -c alembic.ini`，Makefile 已封装。

如果哪天需要改 Alembic 配置，请记住两条硬约束：

1. `alembic.ini` 里不要写任何中文（含注释）。
2. `script_location`、`version_locations` 等路径必须相对 `backend/` 正确解析，
   不能把 ini 路径写成相对仓库根目录。

### 2.4 启动应用

```bash
# 后端（容器内热重载）
docker compose up -d backend

# 前端
docker compose up -d frontend
```

或本地开发模式（改代码即时生效，无需重建镜像）：

```bash
make dev-backend    # uvicorn --reload，端口 8000
make dev-frontend   # vite dev，端口 5173
```

### 2.4.1 接入 Nexent MCP（可选）

`integrations/nexent/` 是独立进程，不依赖机器人，也不直接连接数据库。
它只调用 SeaSight 后端的 `/api/v1`，因此后端认证、角色、辖区范围、审批、幂等与审计
仍然生效。

```bash
# 1. 安装独立运行环境
make nexent-install

# 2. 配置服务地址和最小权限账号
cp integrations/nexent/.env.example integrations/nexent/.env
# 编辑 SEASIGHT_API_BASE_URL、SEASIGHT_API_USERNAME、SEASIGHT_API_PASSWORD

# 3. 生产 Compose 中创建或更新专用账号（默认 viewer，只读）
set -a
. ./.env.production
set +a
docker compose --env-file .env.production -f docker-compose.prod.yml run \
  --rm --no-deps -e PYTHONPATH=/app -v "$PWD:/workspace" backend \
  python /workspace/scripts/create_nexent_service_account.py
# 仅在 SEASIGHT_MCP_ALLOW_WRITES=true 时，才改用 --role operator

# 4. 检查依赖、配置与工具注册模式
make nexent-check

# 5. 协议级端到端验收：HTTP 鉴权、MCP 初始化、工具面、Skills、令牌刷新
make nexent-acceptance

# 6. 本地 Nexent 注册 stdio 命令（由 Nexent 进程启动）
make nexent-mcp
```

生产环境建议让 Nexent 通过受 TLS 保护的域名访问后端，例如
`https://<domain>/api/v1`。若 Nexent 与后端位于同一容器网络，可使用
`http://<backend-service>:8000/api/v1`。默认
`SEASIGHT_MCP_ALLOW_WRITES=false`，此时只注册只读工具；确需登记资产、
审核本体或创建决策时，再为专用账号开启写入并授予最小角色。

生产环境应使用专用账号认证。MCP 通过 `/api/v1/auth/login` 获取短期令牌，
在到期前自动刷新，并在 HTTP 401 时重新登录后只重试一次。
`SEASIGHT_API_TOKEN` 仅作为静态覆盖方式，设置后不会自动刷新。

远程 Nexent 使用 Streamable HTTP。生产 Compose 默认只把 MCP 端口绑定到
宿主机 `127.0.0.1:8100`：

```dotenv
SEASIGHT_MCP_TRANSPORT=streamable-http
SEASIGHT_MCP_HOST=0.0.0.0
SEASIGHT_MCP_PORT=8100
SEASIGHT_MCP_SERVER_TOKEN=<至少 32 个随机字符，且不同于出站密码/令牌>
SEASIGHT_MCP_PUBLIC_URL=https://<mcp-domain>/mcp
```

在 Nexent 中注册 `http://host.docker.internal:8100/mcp`，并配置请求头
`Authorization: Bearer <SEASIGHT_MCP_SERVER_TOKEN>`。SSE 模式的路径是
`/sse`，Streamable HTTP 模式是 `/mcp`。若 Nexent 不在同一宿主机，只通过
受防火墙或 VPN 保护的地址暴露端口，公网必须部署 TLS。

5 个 Skills 位于 `integrations/nexent/skills/`，可分别导入 Nexent：
政策证据问答、海洋事件研判、跨文档决策、工单编排和决策轨迹审计。
完整赛题映射见 `docs/competitions/huawei-nexent.md`。

### 2.5 验证

| 服务 | 地址 | 默认凭据（仅本地开发种子） |
| --- | --- | --- |
| 前端大屏 | http://localhost:5173 | admin / admin123456 |
| 后端 API 文档 | http://localhost:8000/docs | — |
| 存活/观测 | http://localhost:8000/health | — |
| 就绪检查 | http://localhost:8000/ready | 任一依赖故障返回 HTTP 503 |
| EMQX Dashboard | http://localhost:18083 | admin / 见 `.env` |
| MinIO 控制台 | http://localhost:9001 | 见 `.env` |
| go2rtc Web UI | http://localhost:1984 | — |

#### 健康检查的语义（编排系统依赖它）

`/health` 不只是"进程还活着"，它会**逐个探测依赖**并给出三档状态：

| status | 含义 | 编排系统应如何处理 |
| --- | --- | --- |
| `ok` | redis / mqtt / database 全部正常 | 无需干预 |
| `degraded` | 部分依赖不可用，服务仍能提供只读接口 | 告警，但**不要**重启 —— 重启解决不了下游故障 |
| `down` | 全部依赖不可用 | 告警并可考虑摘流量 |

`/health` 始终返回 HTTP 200，适合观测与告警；`/ready` 在任一依赖故障时返回
HTTP 503，容器 healthcheck 与负载均衡必须使用 `/ready`。响应中的
`ack_recovery` 是启动时从 `t_task_ack` 重建的 ACK 判重观测值。

```json
{
  "status": "down",
  "app": "SeaSight",
  "env": "production",
  "ws_connections": 0,
  "dependencies": {
    "redis": "down: TimeoutError",
    "mqtt": "down: ModuleNotFoundError",
    "database": "down: TimeoutError"
  }
}
```

> **为什么不用简单的 `{"status":"ok"}`** ——
> 早期实现就是无条件返回 ok。结果是：Redis、PostgreSQL、MQTT 全挂，
> 进程仍报健康，compose healthcheck 与 K8s probe 都不会重启容器，
> 故障被静默吞掉，直到有人打开前端发现一片空白。
> 这个坑由 `backend/tests/test_health_degradation.py` 守住。

**降级启动是设计允许的**：任何一个依赖缺失都不阻断应用启动
（见第 3 节的降级能力表），所以本地没起 Docker 也能 `uvicorn` 起后端调接口。
但此时 `/health` 会如实报 `degraded`/`down`，冒烟测试也会明确提示。

### 2.6 跑通演示

```bash
# 终端 1：模拟边缘盒上报（无真实摄像头时）
make simulate

# 终端 2：观察后端日志，应能看到
#   [事件] 入库 evt_... 类别=泡沫类 设备=CAM-MABI-01 数量=3
#   [派单] 事件 evt_... → 任务 tsk_... → 机器人 RBT-001
make logs
```

打开 http://localhost:5173 应看到：告警流滚动、地图出现标记、工单看板出现新卡片。

### 2.7 服务依赖关系

```
postgres ──┐
redis ─────┼──► backend ──► frontend
emqx ──────┤       ▲
minio ─────┤       │
go2rtc ────┘    ai-service
```

`backend` 在 `postgres`/`redis`/`emqx` 全部 healthy 后才启动。若某项迟迟不 healthy，先单独排查该项。

---

## 三、本地裸机模式（无 Docker）

本机若无 Docker，可在 Windows/macOS/Linux 上分别跑各组件。以下是 Windows + 托管 venv 的完整流程。

### 3.1 前端

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173

# 构建产物
npm run build        # 输出到 frontend/dist/
npm run preview      # 本地预览构建产物
```

### 3.2 后端（托管 venv）

**不要污染全局环境**，用独立 venv：

```bash
# 创建 venv
"<托管 Python 路径>" -m venv "<venv 路径>"

# 激活（Git Bash）
source "<venv 路径>/Scripts/activate"

# 安装依赖
cd backend
pip install -r requirements.txt
```

**依赖缺失时的降级能力**（本项目有意设计）：

| 缺少的组件 | 后果 | 服务能否启动 |
| --- | --- | --- |
| Redis | 派单队列失效，但 `pending_dispatcher` 定时扫表补派 | ✅ 能 |
| MQTT Broker | 设备上报通道不可用，可用 HTTP `/events` 备用通道 | ✅ 能 |
| MinIO | 证据帧不上传 | ✅ 能 |
| 模型文件 | AI 服务进入 **stub 模式**，返回确定性模拟结果 | ✅ 能 |
| passlib/bcrypt | 登录降级为演示账号比对 | ✅ 能 |

这是刻意的：**任何单一依赖缺失都不应阻断整个系统启动**，否则演示现场一个组件挂掉就全盘皆输。

启动：

```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 3.3 数据库（裸机）

需要 PostgreSQL 14+ 且安装 PostGIS：

```bash
# 建库建用户
psql -U postgres -c "CREATE USER seasight WITH PASSWORD '你的密码';"
psql -U postgres -c "CREATE DATABASE seasight OWNER seasight;"
psql -U postgres -d seasight -c "CREATE EXTENSION IF NOT EXISTS postgis;"

# 执行初始化脚本
psql -U seasight -d seasight -f backend/db/init/01_schema.sql
psql -U seasight -d seasight -f backend/db/init/02_seed.sql
```

**验证 PostGIS 可用**：

```bash
psql -U seasight -d seasight -c "SELECT PostGIS_Version();"
```

若报 `type "geometry" does not exist`，说明 PostGIS 未装或扩展未创建。

### 3.4 前端生产构建 + Nginx

```bash
cd frontend && npm run build
```

容器部署应直接使用 `deploy/nginx/seasight.conf`。宿主机 Nginx 只需反代到
`127.0.0.1:8080` 的前端容器，SPA 回退、API、WebSocket 和视频流都由前端容器
内部 Nginx 处理。关键约束：

- WebSocket 精确路径是 `/api/v1/ws/alerts`，令牌通过 query 参数传递；
- `/stream/` 必须关闭外层 proxy buffering；
- `proxy_read_timeout 3600s` 不能省，否则长连接会被断开并反复重连；
- 只对 `127.0.0.1:8080` 建 upstream，不直连后端 `8000` 或 go2rtc `1984`。

如果用裸机部署且不使用前端容器，可保留自己的 Nginx 静态站点配置，但 API
路径必须使用 `/api/`，WebSocket 必须精确匹配 `/api/v1/ws/alerts`，视频流必须
代理到 `/stream/`。

---

## 四、AI 推理服务

### 4.1 独立部署

```bash
cd backend
pip install -r requirements-ai.txt
AI_MODEL_PATH=/path/to/best.onnx \
AI_MODEL_VERSION=det_v0.2.0 \
uvicorn app.services.ai.server:app --host 0.0.0.0 --port 8081
```

`requirements-ai.txt` 与主 `requirements.txt` 分开的原因：边缘部署时不需要装 FastAPI 全家桶，只需 onnxruntime + opencv + numpy。

### 4.2 模型准备

在仓库根目录（`seahawk/`）下执行：

```bash
python ml/scripts/export_onnx.py \
    --weights ml/runs/detect/train/weights/best.pt \
    --output  ml/models/best.onnx

python ml/scripts/export_onnx.py --verify ml/models/best.onnx    # 校验输出通道数
```

> 说明路径口径：`ml/` 是仓库根目录下的一级目录（与 `backend/`、`frontend/` 同级）。
> 导出脚本内部会把 ONNX 写到 `--output` 指定位置；模型权重不入 git
> （见 `.gitignore` 的 `ml/datasets/**` 与 `*.onnx` 规则），
> 部署时用对象存储或镜像构建阶段灌入。

> **RK3588 部署的坑**：必须使用 `airockchip/ultralytics_yolov8` 分支。官方 ultralytics 导出的 ONNX 在 RKNN 工具链上会因 DFL 层布局报错。`export_onnx.py` 会自动检测当前 ultralytics 是否来自该分支，不是则拒绝导出并打印安装命令。

> **TensorRT engine 不可跨设备复用**。engine 与 GPU 架构、CUDA/TensorRT 版本强绑定，换机器必须重新 `trtexec` 构建。

### 4.3 推理后端优先级

服务自动探测：`TensorrtExecutionProvider` → `CUDAExecutionProvider` → `CPUExecutionProvider`。

模型文件不存在时进入 **stub 模式**，返回基于图片字节哈希的确定性模拟结果（同一张图结果一致，便于写断言）。

---

## 五、边缘盒部署

真实边缘盒上要跑三件事：拉流、推理、MQTT 上报。

### 5.1 配置

复制 `edge/simulator/config.yaml` 作为模板，放到边缘盒 `/opt/seasight/config.yaml`：

```yaml
site_id: "lianjiang"

mqtt:
  host: "<平台 IP>"
  port: 1883
  username: "edge-cam-mabi-01"   # 每台独立凭据，便于单台吊销
  password: "<强密码>"
  keepalive: 60
  use_lwt: true                  # 必须开启

temporal:
  enabled: true
  window_frames: 15
  min_hits: 3
  grid_size: 64
  min_confidence: 0.45

reporting:
  cooldown_seconds: 90

buffer:
  enabled: true
  max_size: 1000
```

> **`device_id` 必须与平台 `t_device` 表一致**，否则平台会因"设备未注册"直接拒收（返回 `code=2001`）。这是最常见的联调失败原因。

### 5.2 抽帧策略

`每 3 帧取 1 帧` —— 25fps 降到 ~8fps。理由：垃圾漂浮速度约 0.1~0.5 m/s，8fps 足以形成连续轨迹。全帧率推理只是徒耗算力，不提升检出率。

### 5.3 系统服务（systemd）

```ini
[Unit]
Description=SeaSight Edge Inference
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=seasight
WorkingDirectory=/opt/seasight
ExecStart=/opt/seasight/venv/bin/python -m edge.main --config /opt/seasight/config.yaml
Restart=always
RestartSec=5
# 崩溃重启后 seq 能续上（状态文件持久化）
Environment=SEASIGHT_STATE_FILE=/var/lib/seasight/state.json

[Install]
WantedBy=multi-user.target
```

**`Restart=always` 是必需的**。边缘盒部署在无人值守的渔港杆子上，崩溃必须自愈。

---

## 六、摄像头流接入

### 6.1 go2rtc 配置

`deploy/go2rtc/go2rtc.yaml`：

```yaml
log:
  level: info

api:
  listen: ":1984"

rtsp:
  listen: ":8554"

webrtc:
  listen: ":8555"

streams:
  CAM-MABI-01: rtsp://admin:<密码>@192.168.1.101:554/Streaming/Channels/101
  CAM-HUANGQI-01: rtsp://admin:<密码>@192.168.1.102:554/Streaming/Channels/101
  CAM-XIAOCHENG-01: rtsp://admin:<密码>@192.168.1.103:554/Streaming/Channels/101
```

流 key 必须与 `t_device.meta.stream_key` 完全一致；本项目种子数据使用
`CAM-*`，后端接口默认生成同源 `/stream/api/stream.flv?src=CAM-MABI-01`。
加一路摄像头 = 加一行，同时把对应设备元数据写入数据库。

### 6.2 三条取流链路

| 链路 | 源 | 目标 | 说明 |
| --- | --- | --- | --- |
| ① 转发 | 摄像头 RTSP | go2rtc → 前端 FLV | 给人看 |
| ② 推理 | 摄像头 RTSP | 边缘盒直接拉 | 给模型算 |
| ③ 留证 | 边缘盒抽帧 | MinIO | 证据帧 |

**② 必须独立于 ①**。如果让边缘盒转流给前端，一路 4Mbps 的转发会持续挤占 CPU，推理帧率掉下去——而推理帧率是这套系统唯一的实质产出。

摄像头需要支持多路并发拉流（主流 IP 摄像头支持 4~8 路）。若摄像头只支持单路，就用 go2rtc 的 `exec` 源或加一个便宜的中继。

### 6.3 前端播放

用 **mpegts.js** 播放 HTTP-FLV，关键参数：

```js
mpegts.createPlayer({
  type: 'flv',
  isLive: true,                  // 直播模式，禁用 seek
  url: flvUrl,
}, {
  enableWorker: true,
  liveBufferLatencyChasing: true, // 自动追帧，防止延迟累积
  liveBufferLatencyMaxLatency: 3.0,
  liveBufferLatencyMinRemain: 0.5,
})
```

> **不要用 flv.js**。该项目已停止维护，在长时间直播流上会出现延迟持续累积（跑几小时后延迟到几十秒），且无修复。

---

## 七、数据库运维

### 7.1 分区维护

`t_track` 按日分区。生产环境用仓库提供的幂等脚本滚动创建未来 14 天分区：

```bash
make prod-track-partitions
```

建议每天由 cron 或 systemd timer 调用一次。**分区缺失时轨迹写入会直接失败**，
这是必须放进运维 checklist 的一项。

### 7.2 备份

```bash
# 每日全量备份
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T postgres pg_dump -U seasight -d seasight -Fc > backup_$(date +%F).dump

# 恢复
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T postgres pg_restore -U seasight -d seasight --clean < backup_2026-09-18.dump
```

**别只备份主表而漏了分区**。`pg_dump` 会包含分区子表，但若用了 `-t t_track` 只备份父表则数据会丢。

### 7.3 常用运维 SQL

```sql
-- 表大小排名
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;

-- 索引使用率（找出有没有白建/漏建的索引）
SELECT indexrelname, idx_scan, idx_tup_read
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC LIMIT 20;

-- 热力图索引是否真的被用上
EXPLAIN ANALYZE
SELECT COUNT(*) FROM t_event
WHERE event_time > now() - interval '24 hours'
  AND ST_DWithin(location::geography, ST_MakePoint(119.65, 26.39)::geography, 3000);
-- 应出现 "Index Scan using idx_event_location"
```

### 7.4 数据保留策略

| 数据 | 保留期 | 处理 |
| --- | --- | --- |
| `t_event` | 永久 | 每年约 29 万条，可忽略 |
| `t_task` | 永久 | 工单是治理凭证，不可删 |
| `t_track` | 90 天 | 超期降采样为 1 点/分钟，再超期丢弃 |
| 证据帧（MinIO） | 180 天 | 用生命周期规则自动过期 |
| `t_report_daily` | 永久 | 宽表，体积极小 |

### 7.5 比赛/验收演示数据（不用于真实生产库）

生产编排只执行 `01_schema.sql`，不会自动导入 `02_seed.sql`。只有专用演示库或
已经确认可以清空的验收环境，才允许执行以下命令：

```bash
cd /opt/seasight
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T postgres psql -U seasight -d seasight -v ON_ERROR_STOP=1 \
  -f /dev/stdin < backend/db/init/02_seed.sql
```

`02_seed.sql` 会 `TRUNCATE` 用户、设备、事件、工单、轨迹和报表数据，**不能**
对保存真实业务数据的库执行。导入后需要重新创建管理员：

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec backend python scripts/create_user.py --username admin --role admin
```

本次服务器演示数据已执行该导入：10 台设备、17 条事件、5 条工单、20 个轨迹点。
如果重建 PostgreSQL 数据卷，这些演示数据不会自动恢复。

---

## 八、故障排查

| 现象 | 排查 | 常见原因 |
| --- | --- | --- |
| 后端启动报连接错误 | `docker compose --env-file .env.production -f docker-compose.prod.yml logs postgres` | `POSTGRES_PASSWORD` 未设 |
| 事件上报返回 `code=2001` | 查 `t_device` | `device_id` 未注册，或与种子数据不一致 |
| 热力图返回空数组 | `SELECT count(*) FROM t_event` | 数据卷已存在但未跑 seed；或时间窗口无数据 |
| 热力图格子大得离谱 | 检查 grid_size 单位 | 误以为单位是"度"；实际是"米" |
| 派单一直不触发 | 查事件 `main_class` | 只有 `foam`/`fishing_gear` 会触发自动派单 |
| 派单报"无可用机器人" | 查 `t_device` 中 robot 的 `status` 与 `meta.battery` | 机器人离线 / 电量 <30% / 三仓总占用 ≥80% |
| 任务卡在 `assigned` | 查机器人是否回 ACK | ACK 超时 15 秒后会自动回退重派 |
| 状态更新报 `code=4002` | 对照状态机迁移表 | 非法跳转，例如 `pending` 直接跳 `done` |
| 前端大屏无数据 | `curl -i localhost:8080/ready` | 返回 503 时按 `dependencies` 排查；CORS 或后端未启动 |
| 前端刷新子页面 404 | 检查 Nginx `try_files` | 缺少 SPA 路由回退 |
| WebSocket 反复重连 | 检查 `/api/v1/ws/alerts` 代理与 `proxy_read_timeout` | 生产无有效令牌返回 4401；60 秒超时会掐断空闲连接 |
| 视频一直转圈 | 浏览器 F12 看 FLV 请求 | go2rtc 未启动；或 `src` 参数与 streams key 不匹配 |
| 地图空白 | F12 控制台看 SDK 加载 | 腾讯地图需走 `_TMapSecurityConfig` 代理；SDK URL 不能带 key |
| 模拟器报 seq 冲突 | 删 `edge/simulator/.simulator_state.json` | 状态文件与数据库不同步 |

### 日志查看

```bash
docker compose logs -f backend      # 后端全部日志
docker compose logs -f backend | grep '\[派单\]'    # 只看派单
docker compose logs -f backend | grep '\[状态机\]'  # 只看状态流转

# 死信队列（处理失败的消息）
docker compose exec redis redis-cli XRANGE stream:dead_letter - + COUNT 10
```

---

## 九、上线检查清单

- [ ] `.env.production` 中所有 `CHANGE_ME` 已替换为强密码
- [ ] `SECRET_KEY` 用 `openssl rand -hex 32` 生成（不是示例值）
- [ ] `.env.production` 与 `deploy/emqx/bootstrap.production.csv` 未被提交
- [ ] EMQX Dashboard 默认密码已改，匿名访问已关闭
- [ ] `make prod-config` 与 `make prod-mqtt-bootstrap` 成功
- [ ] 机器人 MQTT 账号与 ACL 已配好（发布 ACK / progress / 遥测，订阅任务）
- [ ] 宿主机只开放 80/443，Nginx 仅反代到 `127.0.0.1:8080`
- [ ] `TMapSecurityConfig` 代理模式生效，前端源码中**搜不到任何地图密钥**
- [ ] 生产环境 `APP_ENV=production`、`DEBUG=false`
- [ ] `CORS_ORIGINS` 只放实际域名，不用 `*`
- [ ] 全站 HTTPS / WSS 已配置
- [ ] Nginx `proxy_read_timeout` 已加到 3600s
- [ ] 数据库备份任务已配置并**实际演练过一次恢复**
- [ ] `t_track` 分区滚动创建任务已配置（或 pg_partman 已装）
- [ ] MinIO 生命周期规则已配置（证据帧 180 天过期）
- [ ] 演示用默认账号密码已修改
- [ ] Nexent MCP 使用专用最小权限账号，出站认证已启用自动刷新
- [ ] `SEASIGHT_MCP_SERVER_TOKEN` 至少 32 字符且未复用于其他凭据
- [ ] `make nexent-acceptance` 通过，实际 Nexent 已完成注册与工具调用
- [ ] `python scripts/smoke_test.py` 全绿

---

相关文档：`architecture.md`（架构与数据流）· `api.md`（接口契约）· `mqtt-topics.md`（MQTT 主题树）· `development.md`（协作）
