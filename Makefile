# 探海灵眸 SeaSight — 常用命令
# 用法：make <target>     查看全部：make help

.PHONY: help up down restart logs ps db-init knowledge-demo-seed check-demo-approval bootstrap-demo-users demo-approval-up demo-approval-down deploy-package deploy-verify db-reset migrate migrate-stamp migration migrate-history migrate-sql upgrade-pending dev-backend dev-frontend nexent-install nexent-mcp nexent-check nexent-acceptance knowledge-evolution-demo modelarts-smoke simulate demo smoke check check-api check-gitignore check-contract check-contract-selftest check-events-selftest check-dispatch-selftest check-finalize-selftest check-pel-selftest check-evidence-scripts-selftest check-data check-data-stats test test-edge test-cv-selftest vision-compare-export run-edge run-edge-demo test-all test-browser fault-acceptance prod-config prod-build prod-up prod-down prod-logs prod-ps prod-mqtt-bootstrap prod-migrate-stamp prod-migrate prod-create-admin prod-bootstrap-demo-users prod-track-partitions ensure-partitions check-demo-accounts check-public-repo-privacy clean

SHELL := /bin/bash

# 宿主机 Python 解释器。Linux（服务器）上通常**只有 python3**，没有 `python`；
# Windows 开发机上反过来。不探测的话，下面 23 个 target 在服务器上会齐刷刷
# 报 `/bin/bash: line 1: python: command not found` + `Error 127`
# （2026-09-27 实测：`make check-demo-accounts` 就是这么在服务器上挂掉的）。
# ★ 容器内的调用（`docker compose exec backend python ...`）**不要**用它 ——
#   那是镜像里的解释器，与宿主机无关。
PYTHON ?= $(shell command -v python3 >/dev/null 2>&1 && echo python3 || echo python)

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------- 容器编排 ----------
up:  ## 启动全部依赖服务（后台）
	docker compose up -d
	@echo "等待服务就绪..."
	@sleep 5
	@docker compose ps

down:  ## 停止全部服务
	docker compose down

restart:  ## 重启全部服务
	docker compose restart

logs:  ## 查看全部服务日志（跟随）
	docker compose logs -f

ps:  ## 查看服务状态
	docker compose ps

# ---------- 数据库 ----------
db-init:  ## 初始化数据库（建表 + 空间索引 + 种子数据）
	@echo "[db-init] 执行建表脚本..."
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-seasight} -d $${POSTGRES_DB:-seasight} \
		-f /docker-entrypoint-initdb.d/01_schema.sql
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-seasight} -d $${POSTGRES_DB:-seasight} \
		-f /docker-entrypoint-initdb.d/02_seed.sql
	@echo "[db-init] 完成"

knowledge-demo-seed:  ## 显式写入知识智能体演示数据（可重复执行）
	@echo "[knowledge-demo-seed] 写入资产、本体、决策与证据链演示数据..."
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-seasight} -d $${POSTGRES_DB:-seasight} \
		-f /docker-entrypoint-initdb.d/03_seed_knowledge_demo.sql
	@echo "[knowledge-demo-seed] 完成"

ensure-partitions:  ## 开发环境：补建 t_track 当日及未来每日分区（幂等）—— 生产请用 prod-track-partitions
	@echo "[ensure-partitions] 补建 t_track 按日分区（开发 compose）..."
	@# t_track 是按日分区表，缺分区时机器人遥测整条写不进去
	@# （CheckViolationError: no partition of relation "t_track" found）。
	@# 后端已在写入路径里自愈（app/db/partitions.py），这个 target 是开发机上的
	@# 显式兜底。★ 它走的是开发 compose，且 /sql 这个挂载点只存在于
	@#   docker-compose.prod.yml —— 所以**在服务器上跑会失败**。
	@# 生产请用 `make prod-track-partitions`。
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-seasight} -d $${POSTGRES_DB:-seasight} \
		-f /sql/ensure_track_partitions.sql
	@echo "[ensure-partitions] 完成"

db-reset:  ## 危险：删除数据卷重建（会丢数据，仅开发用）
	@read -p "确定要清空数据库吗？[y/N] " ans; [ "$$ans" = "y" ] || exit 1
	docker compose down -v
	docker compose up -d postgres
	@sleep 8
	$(MAKE) db-init

# ---------- 数据库迁移（Alembic）----------
# schema 管理是「双轨制」：
#   轨道 A —— 初始建表走 backend/db/init/01_schema.sql（容器首次启动自动执行）
#   轨道 B —— 后续增量变更走 Alembic
# 所以新环境必须先 `make migrate-stamp` 打桩，否则第一次 autogenerate
# 会生成一堆 create_table，一执行就报表已存在。
#
# ⚠️ 配置在 backend/alembic.ini（唯一主配置，ASCII-only）。
#    [tool.alembic]（pyproject.toml）已废弃：Alembic 1.14 不自动发现
#    pyproject.toml，`-c pyproject.toml` 仍按 locale(GBK) 走 configparser，
#    文件里的中文注释必然 UnicodeDecodeError（已独立复现）。
#    所有 alembic 命令必须显式 `-c alembic.ini`。

migrate-stamp:  ## 新环境首次：把 baseline 标记为已应用（不执行 SQL，不建表）
	@echo "[migrate-stamp] 把当前 head 标记为已应用（schema 已由 01_schema.sql 建好）"
	cd backend && python -m alembic -c alembic.ini stamp head
	@echo "[migrate-stamp] 完成。可执行 make migrate-history 验证"

migrate:  ## 应用全部待执行迁移（upgrade head）
	cd backend && python -m alembic -c alembic.ini upgrade head

migration:  ## 按模型差异生成迁移，用法：make migration m="add xxx column"
	@if [ -z "$(m)" ]; then \
		echo "用法：make migration m=\"描述这次变更\""; \
		exit 1; \
	fi
	cd backend && python -m alembic -c alembic.ini revision --autogenerate -m "$(m)"
	@echo ""
	@echo "★ 生成后请人工检查 backend/alembic/versions/ 下的新文件："
	@echo "  autogenerate 对字段类型/默认值变更的判断不总是可靠，"
	@echo "  尤其涉及 PostGIS 几何列与分区表时，务必肉眼过一遍再提交。"

migrate-history:  ## 查看迁移历史
	cd backend && python -m alembic -c alembic.ini history

migrate-current:  ## 查看数据库当前版本（需数据库在线）
	cd backend && python -m alembic -c alembic.ini current

migrate-sql:  ## 导出待执行迁移的 SQL（离线，不连库；便于 DBA 审核）
	cd backend && python -m alembic -c alembic.ini upgrade head --sql

# ---------- 本地开发 ----------
dev-backend:  ## 本地启动后端（热重载，需先 make up）
	cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend:  ## 本地启动前端开发服务器
	cd frontend && npm run dev

# ---------- Nexent MCP（可选，独立于机器人） ----------
ifeq ($(OS),Windows_NT)
NEXENT_PYTHON ?= .venv-nexent/Scripts/python.exe
else
NEXENT_PYTHON ?= .venv-nexent/bin/python
endif

nexent-install:  ## 安装 Nexent MCP 独立运行环境
	$(PYTHON) -m venv .venv-nexent
	$(NEXENT_PYTHON) -m pip install -r integrations/nexent/mcp_server/requirements.txt
	@echo "[nexent-install] 完成。请复制 integrations/nexent/.env.example 为 .env 并填写令牌"

nexent-mcp:  ## 启动 SeaSight Nexent MCP（默认 stdio）
	$(NEXENT_PYTHON) integrations/nexent/mcp_server/server.py

nexent-check:  ## 检查 Nexent MCP 配置与可导入性
	$(NEXENT_PYTHON) integrations/nexent/mcp_server/server.py --check

nexent-acceptance:  ## 端到端验收：HTTP 鉴权、MCP 工具面和 5 个 Skills
	$(NEXENT_PYTHON) scripts/nexent_acceptance.py --report-path artifacts/nexent-acceptance/latest.json

knowledge-evolution-demo:  ## 真实后端知识进化闭环：资产→本体→检索→决策证据链
	@# 需配置 SEASIGHT_API_BASE_URL / SEASIGHT_API_USERNAME / SEASIGHT_API_PASSWORD，
	@# 推荐 admin 账号一次跑通 operator + 本体审核两类权限。
	$(PYTHON) scripts/knowledge_evolution_demo.py

modelarts-smoke:  ## 真实 OpenAI-compatible 模型端点冒烟（ModelArts 路径）
	@# 需配置 AGENT_MODEL_BASE_URL / AGENT_MODEL_API_KEY / AGENT_MODEL_NAME。
	$(PYTHON) scripts/modelarts_smoke.py

# ---------- 演示与测试 ----------
simulate:  ## 运行边缘盒模拟器（真实抽帧节奏，上报冷却 90s）
	cd edge/simulator && python simulator.py --scenario demo --loop

demo:  ## 演示专用：3 秒上报冷却，几秒就能看到连续告警
	cd edge/simulator && python simulator.py --scenario demo --event-interval 3 --loop

# ---------- 演示模式（P0：审批高光） ----------
# 开关只有**一个真源**：本地 .env 里的 AGENT_REQUIRE_APPROVAL_FOR_WRITE。
# 不再用 compose 叠加层 —— 两个地方都能改同一个开关时，演示现场就会出现
# "我明明关了它还在拦"这类无法定位的问题。仓库默认（.env.example / config.py）
# 始终保持保守，只有本地 .env 是开的。
demo-approval-up:  ## 演示模式启动：WRITE 工具需人工审批（拍「人机协同」镜头）
	@echo "[demo-approval-up] 打开本地 .env 的审批开关并启动服务"
	$(PYTHON) scripts/check_demo_approval.py --set true
	docker compose up -d --build
	@echo "[demo-approval-up] 静态自检："
	$(PYTHON) scripts/check_demo_approval.py
	@echo "[demo-approval-up] 运行时确认（内核实际生效的策略）："
	@echo "  make bootstrap-demo-users    # 备齐 4 个账号（尤其 approver）"
	@echo "  make demo-approval-check     # 看 require_approval_for_write"
	@echo "  make check-demo-accounts     # 真登一次四个账号，确认都上得去"

demo-approval-down:  ## 恢复默认策略（WRITE 工具不再需要人工审批）
	@echo "[demo-approval-down] 关掉本地 .env 的审批开关并重建后端"
	$(PYTHON) scripts/check_demo_approval.py --set false
	docker compose up -d --force-recreate backend
	@echo "[demo-approval-down] 完成（.env 其余配置未动）"

demo-approval-check:  ## 打印后端当前生效的审批策略（演示前 30 秒自检）
	@curl -s localhost:8000/api/v1/agents/runtime/status | \
		python -c "import json,sys;d=json.load(sys.stdin).get('data',{});print('require_approval_for_write =',d.get('require_approval_for_write'),'| approval_risk_levels =',d.get('approval_risk_levels'))"

check-demo-approval:  ## 录屏前静态自检：审批链路三环节 + 账号清单三方一致（.env / compose / 账号）
	@$(PYTHON) scripts/check_demo_approval.py

check-demo-accounts:  ## 真实登录探测：逐个演示账号登录 + 校验角色与审批门禁（只读，不写数据）
	@# 静态核对证明不了「目标库里真的存在这些账号」——它只能证明
	@# 「bootstrap 脚本里写了」。线上 2026-09-27 的 P0-5（登录页展示了
	@# approver，但生产库里没有，点它只会 401）正是静态核对查不出来的那一层。
	@# 默认探本地；探线上用：
	@#   make check-demo-accounts PROBE_URL=https://<PROD_SERVER>/seasight
	@$(PYTHON) scripts/check_demo_approval.py --probe-url $(or $(PROBE_URL),http://localhost:8000)

# ---------- 同步到服务器（本仓库没有 git remote，走打包上传） ----------
deploy-package:  ## 生成本机源码部署包 + SHA256 清单（artifacts/deploy/*.tar.gz）
	@$(PYTHON) scripts/deploy_manifest.py

deploy-verify:  ## 只读核对线上构建（需设 SEASIGHT_PRODUCTION_URL；未设置按 127.0.0.1 示例）
	@$(PYTHON) scripts/deploy_verify.py

bootstrap-demo-users:  ## 开发环境：写入登录页四个演示账号 —— 生产请用 prod-bootstrap-demo-users
	@# ★ 一律用 `python -m scripts.xxx`，不要写 `python scripts/xxx.py`：
	@#   后者会把 sys.path[0] 设成 /app/scripts，`import app` 必报
	@#   ModuleNotFoundError（容器 WORKDIR=/app 但脚本目录不是包根）。
	docker compose exec -T backend python -m scripts.bootstrap_demo_users

smoke:  ## 端到端冒烟测试（构造事件 → 验证派单 → 检查状态流转）
	$(PYTHON) scripts/smoke_test.py

check-api:  ## 契约校验：前端调用的 API 路径在后端是否都有实现
	$(PYTHON) scripts/check_api_contract.py

check-gitignore:  ## 检查是否有该忽略却没忽略的文件（运行时状态/权重/密钥）
	$(PYTHON) scripts/check_gitignore.py

check-public-repo-privacy:  ## 开源前检查：本机路径、生产地址、敏感验收证据
	@echo "[check-public-repo-privacy] 只扫 git 已跟踪文件，私有仓库命中属预期；公开前必须退出码 0"
	$(PYTHON) scripts/check_public_repo_privacy.py

check-contract:  ## 契约漂移检查：MQTT 主题树、状态映射、类别枚举
	$(PYTHON) scripts/check_contract_drift.py

check-contract-selftest:  ## 自证：注入 12 种已知缺陷，确认上面的检查真的会红
	$(PYTHON) scripts/selftest_contract_drift.py

check-events-selftest:  ## 自证：注入事件上报契约的 5 种缺陷，确认测试会红
	$(PYTHON) scripts/selftest_events_contract.py

check-dispatch-selftest:  ## 自证：注入派单引擎的 5 种缺陷，确认测试会红
	$(PYTHON) scripts/selftest_dispatch_contract.py

check-finalize-selftest:  ## 自证：注入派单收尾的 7 种缺陷，确认测试会红
	$(PYTHON) scripts/selftest_dispatch_finalize.py

check-pel-selftest:  ## 自证：注入消费者 PEL 回收的 4 种缺陷，确认测试会红
	$(PYTHON) scripts/selftest_consumer_pel.py

check-evidence-scripts-selftest:  ## 自证：华为 ICT 证据脚本未配置时不伪造成功
	@echo "[check-evidence-scripts-selftest] 内部证据脚本不随开源快照发布"

check: check-api check-gitignore check-contract  ## 跑全部静态检查（不需要基础设施）
	@echo "[check] 全部通过"

check-data:  ## 数据集体检：图片/标签配对、类别索引、标签格式
	$(PYTHON) ml/scripts/check_dataset.py

check-data-stats:  ## 数据集体检并把统计写回 seasight.yaml 的 stats 段
	@echo "[check-data-stats] 体检 + 写回统计（供训练脚本做负样本/均衡检查）"
	$(PYTHON) ml/scripts/check_dataset.py --write-stats
	@echo "[check-data-stats] 完成 —— 请 git diff 确认只改了数字，没动注释"

# ---------- 生产部署 ----------
PROD_COMPOSE = docker compose --env-file .env.production -f docker-compose.prod.yml

prod-config:  ## 校验生产 compose 与环境变量
	$(PROD_COMPOSE) config

prod-build:  ## 构建生产镜像
	$(PROD_COMPOSE) build

prod-up:  ## 构建并启动生产服务（前端仅绑定 127.0.0.1:${HTTP_PORT}）
	$(PROD_COMPOSE) up -d --build
	$(PROD_COMPOSE) ps

prod-down:  ## 停止生产服务（保留数据卷）
	$(PROD_COMPOSE) down

prod-logs:  ## 查看生产日志
	$(PROD_COMPOSE) logs -f --tail=200

prod-ps:  ## 查看生产服务状态
	$(PROD_COMPOSE) ps

prod-mqtt-bootstrap:  ## 从 .env.production 生成 EMQX 首次启动凭据
	$(PYTHON) scripts/generate_emqx_bootstrap.py

prod-migrate-stamp:  ## 生产新库首次部署：将当前 01_schema.sql 标记到 Alembic head
	$(PROD_COMPOSE) exec -T backend python -m alembic -c alembic.ini stamp head

prod-migrate:  ## 应用生产数据库待执行迁移
	$(PROD_COMPOSE) exec -T backend python -m alembic -c alembic.ini upgrade head

prod-create-admin:  ## 创建正式管理员（密码从终端读取）
	$(PROD_COMPOSE) exec backend python -m scripts.create_user --username admin --role admin

prod-bootstrap-demo-users:  ## 生产：写入登录页四个演示账号（含 approver，可重复执行）
	@# 生产专用入口。不要用 `make bootstrap-demo-users` —— 那个走的是开发 compose
	@# （docker-compose.yml + 仓库根的 .env），服务器上既没有 .env，
	@# 也起不到 seasight-prod-* 那套容器，会直接报
	@#   error while interpolating services.postgres.environment.POSTGRES_PASSWORD
	@# 另外脚本自身有生产闸门：APP_ENV=production 时必须显式 --confirm-production，
	@# 少了它直接 SystemExit（这是有意的，防止误改生产用户表）。
	$(PROD_COMPOSE) exec -T backend python -m scripts.bootstrap_demo_users --confirm-production

prod-track-partitions:  ## 生产：滚动创建 t_track 未来 14 天分区（运维兜底入口）
	@# 这是**服务器上唯一可用**的分区补建入口。
	@# 应用侧已在写入路径自愈（app/db/partitions.py），这里是运维/演示前的显式兜底。
	@# 需要 postgres 容器里挂到 /sql 的 deploy/postgres/（见 docker-compose.prod.yml）；
	@# 这里改走 stdin 是为了不依赖挂载点，两边都能用。
	$(PROD_COMPOSE) exec -T postgres psql -U $${POSTGRES_USER:-seasight} -d $${POSTGRES_DB:-seasight} \
		-v ON_ERROR_STOP=1 -f /dev/stdin < deploy/postgres/ensure_track_partitions.sql

test-edge:  ## 边缘逻辑测试：时序校验 + 检测器（cv 与开放词汇双通道，不需要摄像头与 Broker）
	cd edge/simulator && python test_temporal.py
	# ★ 测试文件必须逐个列出：这里写的是 pytest 的文件名参数，不是目录。
	#   新增测试文件如果不加进这一行，`make test-edge` 照样"全绿"——
	#   只是它根本没跑。加文件时请同时改这里。
	cd edge/arm_bridge && python -m pytest test_bridge.py -q
	cd edge/detector && python -m pytest test_detector.py test_world_detector.py -q

test-cv-selftest:  ## 真实边缘程序自检：合成海面跑通「检测→时序→报文」全链路
	$(PYTHON) edge/main.py --source synthetic --dry-run --max-frames 200 --cooldown 0.3 --min-interval 0

vision-compare-export:  ## 导出前端「双通道对照」页数据（定性，需要 torch/ultralytics）
	$(PYTHON) scripts/export_channel_compare.py

run-edge:  ## 启动真实边缘感知程序（RTSP 取流，读 edge/config.yaml）
	$(PYTHON) edge/main.py --source rtsp

run-edge-demo:  ## 无摄像头时的演示：合成海面 + 弹窗看检测框
	$(PYTHON) edge/main.py --source synthetic --show --cooldown 5 --min-interval 1

test:  ## 运行全量测试（backend + edge + ml）
	pytest -v

test-all: test  ## 兼容入口：跑仓库全量测试
	@echo "[test-all] 全部测试通过"
	@echo ""
	@echo "提示：契约与卫生检查请跑 make check；端到端验证需基础设施，请跑 make smoke（需先 make up）"
	@echo "      怀疑检查脚本本身失效时，跑 make check-contract-selftest"

# ---------- WP-17 集成验收 ----------
test-browser: ## 浏览器集成验收（E1：真实登录 + 主业务链路 E2E，需前端 5174 / 后端 8001 在线）
	@echo "[test-browser] 运行 scripts/browser_acceptance.mjs（PLAYWRIGHT_CORE_PATH=$${PLAYWRIGHT_CORE_PATH:-未设置}）"
	node scripts/browser_acceptance.mjs

fault-acceptance: ## 受控故障演练包（E1：逐组复跑健康降级 / ACK 启动恢复 / 孪生闭环确定性用例）
	@echo "[fault-acceptance] 运行 scripts/fault_acceptance.py"
	$(PYTHON) scripts/fault_acceptance.py

# ---------- 清理 ----------
clean:  ## 清理 Python 缓存与构建产物
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "[clean] 完成"
