-- ============================================================
-- 探海灵眸 Oceanus — 数据库 Schema
-- PostgreSQL 14 + PostGIS 3.3
-- 设计原则：平台是中枢，核心表是「事件表」与「工单表」
-- ============================================================

-- ---------- 扩展 ----------
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- 名称模糊搜索

-- ---------- 枚举类型 ----------
DO $$ BEGIN
    CREATE TYPE device_type_enum AS ENUM ('shore_camera', 'drone', 'robot');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE device_status_enum AS ENUM ('online', 'offline', 'fault');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE waste_class_enum AS ENUM ('foam', 'plastic', 'fishing_gear', 'other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE event_status_enum AS ENUM ('new', 'dispatched', 'resolved', 'ignored');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE task_status_enum AS ENUM ('pending', 'assigned', 'navigating', 'collecting', 'done', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE review_result_enum AS ENUM ('confirmed', 'not_found', 'recheck', 'pending');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE user_role_enum AS ENUM ('admin', 'operator', 'approver', 'viewer');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ============================================================
-- （1）设备表 t_device
-- ============================================================
CREATE TABLE IF NOT EXISTS t_device (
    id              BIGSERIAL PRIMARY KEY,
    device_id       VARCHAR(64)  NOT NULL UNIQUE,          -- 业务编号，如 CAM-MABI-01
    device_type     device_type_enum NOT NULL,
    name            VARCHAR(128) NOT NULL,
    location        geometry(Point, 4326) NOT NULL,        -- 安装/初始位置
    status          device_status_enum NOT NULL DEFAULT 'offline',
    last_heartbeat  TIMESTAMPTZ,
    stream_url      TEXT,                                  -- 视频流地址（go2rtc 转发）
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,    -- 型号、分辨率等扩展
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  t_device IS '设备表：岸基摄像头 / 无人机 / 水面机器人';
COMMENT ON COLUMN t_device.device_id IS '业务编号，边缘端上报时使用此 ID';

-- 空间索引：按区域查设备
CREATE INDEX IF NOT EXISTS idx_device_location ON t_device USING GIST (location);
CREATE INDEX IF NOT EXISTS idx_device_status   ON t_device (status);
CREATE INDEX IF NOT EXISTS idx_device_type     ON t_device (device_type);


-- ============================================================
-- （2）识别事件表 t_event  ★核心表
-- ============================================================
CREATE TABLE IF NOT EXISTS t_event (
    id              BIGSERIAL PRIMARY KEY,
    event_id        VARCHAR(64)  NOT NULL UNIQUE,          -- 事件编号 evt_xxx
    device_id       VARCHAR(64)  NOT NULL,
    event_time      TIMESTAMPTZ  NOT NULL,                 -- 事件发生时间（设备时间）
    location        geometry(Point, 4326) NOT NULL,
    main_class      waste_class_enum NOT NULL,
    det_count       SMALLINT     NOT NULL DEFAULT 1,
    max_confidence  NUMERIC(5,4) NOT NULL,                 -- 0.0000 ~ 1.0000
    evidence_url    TEXT,                                  -- 证据帧 MinIO 地址
    model_version   VARCHAR(32),                           -- 产出该事件的模型版本
    seq             BIGINT       NOT NULL,                 -- 边缘端单调序号（判重用）
    status          event_status_enum NOT NULL DEFAULT 'new',
    township        VARCHAR(64),                           -- 乡镇归属（入库时按 location 最近邻计算）
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- 防重复派单：同一设备同一序号只能有一条
    CONSTRAINT uq_event_device_seq UNIQUE (device_id, seq),
    CONSTRAINT chk_event_confidence CHECK (max_confidence >= 0 AND max_confidence <= 1),
    CONSTRAINT chk_event_det_count  CHECK (det_count > 0)
);

COMMENT ON TABLE  t_event IS '识别事件表：平台核心表，所有业务从事件驱动';
COMMENT ON COLUMN t_event.seq IS '边缘端单调递增序号，配合 device_id 建唯一约束，杜绝 MQTT 重传导致的重复派单';

-- 热力图查询主用索引：时间 + 空间
CREATE INDEX IF NOT EXISTS idx_event_time     ON t_event (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_event_location ON t_event USING GIST (location);
CREATE INDEX IF NOT EXISTS idx_event_status   ON t_event (status);
CREATE INDEX IF NOT EXISTS idx_event_class    ON t_event (main_class);
CREATE INDEX IF NOT EXISTS idx_event_township ON t_event (township);
-- 复合索引：热力图按时间窗 + 类别筛选
CREATE INDEX IF NOT EXISTS idx_event_time_class ON t_event (event_time DESC, main_class);


-- ============================================================
-- （3）清理任务表 t_task  ★工单表
-- ============================================================
CREATE TABLE IF NOT EXISTS t_task (
    id                BIGSERIAL PRIMARY KEY,
    task_id           VARCHAR(64) NOT NULL UNIQUE,         -- 任务编号 tsk_xxx
    event_id          VARCHAR(64) REFERENCES t_event(event_id) ON DELETE SET NULL,
    robot_id          VARCHAR(64),                         -- 执行机器人（未派单时为空）
    target_location   geometry(Point, 4326) NOT NULL,
    status            task_status_enum NOT NULL DEFAULT 'pending',
    priority          SMALLINT NOT NULL DEFAULT 5,         -- 1=最高 9=最低
    township          VARCHAR(64),                         -- 乡镇归属（按 target_location 最近邻计算）

    -- 时间戳链：完整记录任务生命周期
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_at       TIMESTAMPTZ,
    ack_at            TIMESTAMPTZ,                         -- 机器人确认接收
    started_at        TIMESTAMPTZ,                         -- 开始作业
    finished_at       TIMESTAMPTZ,                         -- 作业完成

    collected_weight  NUMERIC(8,3),                        -- 清理量 kg（作业后补录）
    review_result     review_result_enum DEFAULT 'pending',-- 机载复核结果
    remark            TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_task_priority CHECK (priority BETWEEN 1 AND 9)
);

COMMENT ON TABLE  t_task IS '清理任务表（工单表）：状态变更必须走服务层，禁止直接 UPDATE';
COMMENT ON COLUMN t_task.status IS '状态机：pending→assigned→navigating→collecting→done；任意态可→cancelled';

CREATE INDEX IF NOT EXISTS idx_task_status  ON t_task (status);
CREATE INDEX IF NOT EXISTS idx_task_robot   ON t_task (robot_id);
CREATE INDEX IF NOT EXISTS idx_task_event   ON t_task (event_id);
CREATE INDEX IF NOT EXISTS idx_task_created ON t_task (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_target  ON t_task USING GIST (target_location);
CREATE INDEX IF NOT EXISTS idx_task_township ON t_task (township);

-- 防抖关键：同一事件在「未完成任务」状态下不重复派单（部分唯一索引）
CREATE UNIQUE INDEX IF NOT EXISTS uq_task_active_event
    ON t_task (event_id)
    WHERE status NOT IN ('done', 'cancelled') AND event_id IS NOT NULL;


-- ============================================================
-- （4）作业轨迹表 t_track（按日分区，数据量大）
-- ============================================================
CREATE TABLE IF NOT EXISTS t_track (
    id          BIGSERIAL,
    robot_id    VARCHAR(64) NOT NULL,
    task_id     VARCHAR(64),
    location    geometry(Point, 4326) NOT NULL,
    battery     SMALLINT,                                  -- 电量百分比
    bin_foam    NUMERIC(4,3),                              -- 泡沫仓占用 0~1
    bin_plastic NUMERIC(4,3),                              -- 塑胶仓占用
    bin_mixed   NUMERIC(4,3),                              -- 混合仓占用
    speed       NUMERIC(5,2),                              -- m/s
    -- 逐舵机遥测（电压/温度/位置）。真机执行时由驱动回读写入，
    -- 是「机械臂真的动了」的硬证据（status 字段是平台自报）。
    -- 非真机（仿真 / 无舵机）时为 NULL。见 20260905_0100_servo_telemetry 迁移。
    servo_telemetry JSONB,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, recorded_at)
) PARTITION BY RANGE (recorded_at);

COMMENT ON TABLE t_track IS '作业轨迹表：按日分区，超期数据降采样归档';

-- 创建初始分区（当日 + 未来 7 天），生产环境用 pg_partman 自动管理
DO $$
DECLARE
    d date;
BEGIN
    FOR i IN -1..7 LOOP
        d := CURRENT_DATE + i;
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS t_track_%s PARTITION OF t_track FOR VALUES FROM (%L) TO (%L)',
            to_char(d, 'YYYYMMDD'), d, d + 1
        );
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_track_robot_time ON t_track (robot_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_track_location   ON t_track USING GIST (location);


-- ============================================================
-- （5）统计宽表 t_report_daily（预聚合，报表页只读这张表）
-- ============================================================
CREATE TABLE IF NOT EXISTS t_report_daily (
    id             BIGSERIAL PRIMARY KEY,
    stat_date      DATE NOT NULL,
    township       VARCHAR(64),                            -- 乡镇：马鼻/黄岐/筱埕...
    main_class     waste_class_enum,
    event_count    INTEGER NOT NULL DEFAULT 0,
    task_count     INTEGER NOT NULL DEFAULT 0,
    done_count     INTEGER NOT NULL DEFAULT 0,
    collected_kg   NUMERIC(10,3) NOT NULL DEFAULT 0,
    -- WP-07：coverage_area 可空 —— NULL=未统计（无可靠数据源），与「真实 0」区分；
    -- 未统计禁止以 0 冒充实测；可用性三态见 coverage_availability 列
    coverage_area  NUMERIC(12,2),                           -- 覆盖面积 m²（NULL=未统计）
    coverage_availability VARCHAR(16) NOT NULL DEFAULT 'not_available',  -- available/partial/not_available
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_report_daily UNIQUE (stat_date, township, main_class)
);

COMMENT ON TABLE t_report_daily IS '统计宽表：每日凌晨预聚合，用存储换查询速度';

CREATE INDEX IF NOT EXISTS idx_report_date     ON t_report_daily (stat_date DESC);
CREATE INDEX IF NOT EXISTS idx_report_township ON t_report_daily (township);


-- ============================================================
-- （6）用户与权限 t_user / t_role
-- ============================================================
CREATE TABLE IF NOT EXISTS t_user (
    id             BIGSERIAL PRIMARY KEY,
    username       VARCHAR(64)  NOT NULL UNIQUE,
    hashed_password VARCHAR(255) NOT NULL,
    full_name      VARCHAR(64),
    role           user_role_enum NOT NULL DEFAULT 'viewer',
    township_scope VARCHAR(64),                            -- 操作员的数据辖区（乡镇）
    is_active      BOOLEAN NOT NULL DEFAULT true,
    last_login_at  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE t_user IS '用户表：admin=全部 / operator=本辖区写 / approver=审批 / viewer=只读';

CREATE INDEX IF NOT EXISTS idx_user_role ON t_user (role);


-- ============================================================
-- （7）操作审计日志 t_audit_log（政务交付的可追溯性）
-- ============================================================
CREATE TABLE IF NOT EXISTS t_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    username    VARCHAR(64) NOT NULL,
    role        VARCHAR(16) NOT NULL,
    action      VARCHAR(64) NOT NULL,      -- login / task_create / task_status_update / event_status_update
    target_type VARCHAR(32),               -- task / event / auth
    target_id   VARCHAR(64),
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE t_audit_log IS '操作审计：谁在何时对什么做了什么（政务交付的可追溯性）';

CREATE INDEX IF NOT EXISTS idx_audit_created ON t_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user    ON t_audit_log (username);


-- ============================================================
-- 通用触发器：自动维护 updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_updated_at_device ON t_device;
CREATE TRIGGER set_updated_at_device
    BEFORE UPDATE ON t_device FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

DROP TRIGGER IF EXISTS set_updated_at_task ON t_task;
CREATE TRIGGER set_updated_at_task
    BEFORE UPDATE ON t_task FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();


-- ============================================================
-- 示例查询：热力图网格聚合（供参考，接口调用）
-- ============================================================
-- 按 500m 网格聚合最近 24 小时事件，输出网格中心与密度
-- 注意：必须投影到 3857（米制），否则 ST_SnapToGrid 的单位是「度」
--
-- SELECT
--     ST_Y(ST_Transform(grid, 4326)) AS lat,
--     ST_X(ST_Transform(grid, 4326)) AS lng,
--     cnt,
--     density,
--     main_class
-- FROM (
--     SELECT ST_SnapToGrid(ST_Transform(location::geometry, 3857), 500) AS grid,
--            COUNT(*) AS cnt,
--            COUNT(*) / 250000.0 AS density,          -- 500*500 m²
--            MODE() WITHIN GROUP (ORDER BY main_class) AS main_class
--     FROM t_event
--     WHERE event_time > now() - interval '24 hours'
--       AND status <> 'ignored'
--     GROUP BY grid
-- ) t
-- ORDER BY cnt DESC
-- LIMIT 500;

-- ============================================================
-- 示例查询：KNN 就近派单（找最近的可用机器人）
-- ============================================================
-- SELECT r.device_id, r.name,
--        ST_Distance(r.location::geography, e.location::geography) AS dist_m
-- FROM t_device r
-- JOIN t_event e ON e.event_id = $1
-- WHERE r.device_type = 'robot'
--   AND r.status = 'online'
--   AND ST_DWithin(r.location::geography, e.location::geography, 3000)  -- 米制粗筛，走索引
-- ORDER BY r.location <-> e.location                                     -- KNN 算子，走 GiST
-- LIMIT 1;


-- ============================================================
-- （8）Agent 运行契约四表（WP-02）
--      t_agent_run / t_agent_step / t_agent_memory / t_agent_approval
-- 冻结契约：表名/字段见《Harness 分工执行手册》3.7；
--          枚举语义见手册 3.1（运行状态）/3.2（步骤类型）/
--                   3.3（错误码）/3.5（风险级别）。
-- 幂等写法：类型用 duplicate_object 守卫、表用 IF NOT EXISTS、
--          索引用 IF NOT EXISTS —— 本段可重复执行，不破坏既有表。
-- 增量迁移：backend/alembic/versions/20260918_1100_agent_runtime.py
-- ============================================================

-- ---------- Agent 枚举类型 ----------
DO $$ BEGIN
    CREATE TYPE agent_run_status_enum AS ENUM (
        'created', 'planning', 'waiting_policy', 'waiting_approval',
        'executing', 'observing', 'verifying',
        'succeeded', 'failed', 'cancelled', 'expired');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE agent_step_type_enum AS ENUM (
        'plan', 'policy', 'approval_request', 'tool_call',
        'observation', 'verification', 'replan', 'terminal');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE agent_error_code_enum AS ENUM (
        'no_robot_available', 'tool_timeout', 'tool_failed',
        'policy_denied', 'approval_rejected', 'approval_timeout',
        'task_conflict', 'invalid_tool_input', 'invalid_tool_output',
        'max_steps_exceeded', 'run_expired', 'internal_error');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE agent_memory_type_enum AS ENUM (
        'working', 'episodic', 'semantic', 'policy', 'eval');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE agent_risk_level_enum AS ENUM (
        'read_only', 'write', 'device_command', 'sensitive');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE agent_decision_enum AS ENUM (
        'approved', 'rejected', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 注：步骤状态 t_agent_step.status、记忆作用域 t_agent_memory.scope_type、
--     记忆来源 t_agent_memory.source_type 为自由 VARCHAR（与 WP-01 内存模型
--     对齐，见下方列定义），不设数据库枚举。


-- ============================================================
-- （8.1）运行记录表 t_agent_run
-- ============================================================
CREATE TABLE IF NOT EXISTS t_agent_run (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             VARCHAR(64) NOT NULL,          -- 业务编号 run_xxx（唯一）
    trigger_type       VARCHAR(32) NOT NULL,          -- event / manual / scheduled ...
    objective          TEXT        NOT NULL,          -- 目标（决策摘要，不存私有思维链）
    status             agent_run_status_enum NOT NULL DEFAULT 'created',
    policy_version     VARCHAR(32),                   -- 规则/策略版本（规则模式可为空）
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    termination_reason TEXT,                          -- 终止原因（结构化错误说明/业务摘要）
    trace_id           VARCHAR(64),                   -- 链路追踪 ID
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_agent_run_run_id UNIQUE (run_id)
);

COMMENT ON TABLE t_agent_run IS 'Agent 运行记录表：一次触发到终止的完整 run（手册 3.1 状态机）';
COMMENT ON COLUMN t_agent_run.status IS '终态 succeeded/failed/cancelled/expired 不得再次迁移';

CREATE INDEX IF NOT EXISTS idx_agent_run_status  ON t_agent_run (status);
CREATE INDEX IF NOT EXISTS idx_agent_run_created ON t_agent_run (created_at DESC);


-- ============================================================
-- （8.2）步骤记录表 t_agent_step
-- ============================================================
CREATE TABLE IF NOT EXISTS t_agent_step (
    id               BIGSERIAL PRIMARY KEY,
    step_id          VARCHAR(64) NOT NULL,            -- 业务编号 stp_xxx（唯一）
    run_id           VARCHAR(64) NOT NULL,            -- 关联 t_agent_run.run_id
    step_no          INTEGER     NOT NULL,            -- 步骤序号（重规划不覆盖历史）
    step_type        agent_step_type_enum NOT NULL,
    decision_summary TEXT        NOT NULL,            -- 决策摘要（不存私有思维链）
    tool_name        VARCHAR(128),                    -- 仅 tool_call 步骤有值
    tool_version     VARCHAR(32),
    input_hash       VARCHAR(64),                     -- 工具输入摘要哈希
    output_hash      VARCHAR(64),                     -- 工具输出摘要哈希
    status           VARCHAR(16) NOT NULL DEFAULT 'pending',  -- WP-01 写入 ok/failed；终态步骤写 succeeded/failed/cancelled/expired
    latency_ms       INTEGER,
    error_code       agent_error_code_enum,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_agent_step_step_id  UNIQUE (step_id),
    CONSTRAINT uq_agent_step_run_no   UNIQUE (run_id, step_no),
    CONSTRAINT fk_agent_step_run      FOREIGN KEY (run_id)
        REFERENCES t_agent_run (run_id) ON DELETE CASCADE
);

COMMENT ON TABLE t_agent_step IS 'Agent 步骤记录表：(run_id, step_no) 唯一，重规划必须新增 replan 与后续步骤';
COMMENT ON COLUMN t_agent_step.input_hash  IS '工具输入摘要的哈希，不保存原始输入';
COMMENT ON COLUMN t_agent_step.output_hash IS '工具输出摘要的哈希，不保存原始输出';


-- ============================================================
-- （8.3）记忆表 t_agent_memory
-- ============================================================
CREATE TABLE IF NOT EXISTS t_agent_memory (
    id           BIGSERIAL PRIMARY KEY,
    memory_id    VARCHAR(64) NOT NULL,                -- 业务编号 mem_xxx（唯一）
    memory_type  agent_memory_type_enum NOT NULL,     -- working/episodic/semantic/policy/eval
    scope_type   VARCHAR(32) NOT NULL,                 -- run/robot/township/... 自由取值
    scope_id     VARCHAR(64),                         -- run_id / step_id；global 时为空
    content      TEXT        NOT NULL,                -- 事实/摘要，不存模型私有思维链
    confidence   NUMERIC(5,4),                        -- 0.0000 ~ 1.0000
    source_type  VARCHAR(32),                         -- tool_call/policy/manual/... 可空（区分事实/推断/人工确认）
    source_id    VARCHAR(64),
    valid_from   TIMESTAMPTZ,
    valid_to     TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_agent_memory_memory_id UNIQUE (memory_id),
    CONSTRAINT chk_agent_memory_confidence CHECK (confidence >= 0 AND confidence <= 1)
);

COMMENT ON TABLE t_agent_memory IS 'Agent 分层记忆表：按 (scope_type, scope_id) 检索，禁止把模型猜测写成事实';

CREATE INDEX IF NOT EXISTS idx_agent_memory_scope ON t_agent_memory (scope_type, scope_id);


-- ============================================================
-- （8.4）人工审批表 t_agent_approval
-- ============================================================
CREATE TABLE IF NOT EXISTS t_agent_approval (
    id               BIGSERIAL PRIMARY KEY,
    approval_id      VARCHAR(64) NOT NULL,            -- 业务编号 apr_xxx（唯一）
    run_id           VARCHAR(64) NOT NULL,            -- 关联 t_agent_run.run_id
    requested_action TEXT        NOT NULL,            -- 请求的动作摘要（供人工判断）
    risk_level       agent_risk_level_enum NOT NULL,  -- read_only/write/device_command/sensitive
    requested_by     VARCHAR(64) NOT NULL,            -- 提出方（agent 角色/工具）
    decided_by       VARCHAR(64),                     -- 决定人（人工）
    decision         agent_decision_enum,             -- approved/rejected/cancelled；空=待审批
    reason           TEXT,                            -- 决定理由
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,

    CONSTRAINT uq_agent_approval_approval_id UNIQUE (approval_id),
    CONSTRAINT fk_agent_approval_run FOREIGN KEY (run_id)
        REFERENCES t_agent_run (run_id) ON DELETE CASCADE
);

COMMENT ON TABLE t_agent_approval IS 'Agent 人工审批表：decision 为空 = 待审批；拒绝后 run 终止且不发送设备指令';

CREATE INDEX IF NOT EXISTS idx_agent_approval_run       ON t_agent_approval (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_approval_decision  ON t_agent_approval (decision);
CREATE INDEX IF NOT EXISTS idx_agent_approval_requested ON t_agent_approval (requested_at DESC);


-- ============================================================
-- （8.5）持久化续跑状态表 t_agent_run_state
-- ============================================================
CREATE TABLE IF NOT EXISTS t_agent_run_state (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             VARCHAR(64) NOT NULL,          -- 与 t_agent_run.run_id 一一对应
    request_json       TEXT        NOT NULL,          -- 已脱敏的续跑请求摘要
    runtime_state_json TEXT        NOT NULL,          -- 已脱敏的续跑运行状态
    idempotency_key    VARCHAR(128),                  -- NULL 不参与唯一，确保并发幂等
    state_version      INTEGER     NOT NULL DEFAULT 1,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_agent_run_state_run_id UNIQUE (run_id),
    CONSTRAINT fk_agent_run_state_run FOREIGN KEY (run_id)
        REFERENCES t_agent_run (run_id) ON DELETE CASCADE
);

COMMENT ON TABLE t_agent_run_state IS 'Agent 持久化续跑状态：只保存脱敏摘要，不保存思维链、密钥或原始 Prompt';
COMMENT ON COLUMN t_agent_run_state.state_version IS '乐观并发版本号；保存时版本不匹配必须拒绝覆盖';

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_run_state_idem_key
    ON t_agent_run_state (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_run_state_version
    ON t_agent_run_state (run_id, state_version);


-- ============================================================
-- （9）ACK 审计账本 t_task_ack（WP-14D）
--      冻结契约见《Harness 分工执行手册》WP-14D。
--      一 command_id 一条规范回执；重复到达只累计，不覆盖首次。
-- ============================================================
CREATE TABLE IF NOT EXISTS t_task_ack (
    id                BIGSERIAL PRIMARY KEY,
    command_id        VARCHAR(96)  NOT NULL,             -- 一命令一条规范回执
    task_id           VARCHAR(64)  NOT NULL REFERENCES t_task(task_id) ON DELETE CASCADE,
    device_id         VARCHAR(64)  NOT NULL,
    seq               BIGINT       NOT NULL,
    outcome           VARCHAR(16)  NOT NULL,             -- new/duplicate/late/out_of_order
    accepted          BOOLEAN      NOT NULL,
    reason            VARCHAR(128),                      -- accepted=false 时的拒绝原因
    mode              VARCHAR(32),                       -- 设备模式（冻结信封字段）
    received_at       TIMESTAMPTZ  NOT NULL,             -- 设备回执时间
    received_wall_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),  -- 平台首次落库时间
    duplicate_count   INTEGER      NOT NULL DEFAULT 0,   -- 后续重复次数
    last_duplicate_at TIMESTAMPTZ,                       -- 最近一次重复到达时间
    raw_payload       JSONB        NOT NULL,             -- 首次规范回执原文（审计留档，接口不回传）
    last_payload      JSONB,                             -- 最近一次重复回执原文
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_task_ack_command_id UNIQUE (command_id)
);

COMMENT ON TABLE  t_task_ack IS 'ACK 审计账本：一 command_id 一条规范回执 + 重复统计（WP-14D）';
COMMENT ON COLUMN t_task_ack.outcome IS '首次判定：new/duplicate/late/out_of_order（WP-14C AckTracker）';
COMMENT ON COLUMN t_task_ack.raw_payload IS '首次规范回执原文（审计留档，查询接口不回传）';

CREATE INDEX IF NOT EXISTS idx_task_ack_task
    ON t_task_ack (task_id, received_wall_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_ack_device_seq
    ON t_task_ack (device_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_task_ack_outcome
    ON t_task_ack (outcome, received_wall_at DESC);

-- updated_at 自动维护（与 t_device / t_task 同款触发器）
DROP TRIGGER IF EXISTS set_updated_at_task_ack ON t_task_ack;
CREATE TRIGGER set_updated_at_task_ack
    BEFORE UPDATE ON t_task_ack FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();


-- ============================================================
-- （10）知识资产、动态本体与决策证据
--      华为 ICT / Nexent 赛道的增量能力，不改变机器人事件派单链路。
--      新库由本段建表；存量库由 Alembic
--      20260919_1900_knowledge_assets 建表。
-- ============================================================

CREATE TABLE IF NOT EXISTS t_knowledge_asset (
    id                 BIGSERIAL PRIMARY KEY,
    asset_id           VARCHAR(64)  NOT NULL,
    asset_type         VARCHAR(32)  NOT NULL,
    title              VARCHAR(256) NOT NULL,
    description        TEXT,
    source_uri         VARCHAR(1024),
    source_system      VARCHAR(128),
    mime_type          VARCHAR(128),
    region             VARCHAR(128),
    township           VARCHAR(64),
    security_level     VARCHAR(32)  NOT NULL DEFAULT 'internal',
    status             VARCHAR(32)  NOT NULL DEFAULT 'active',
    current_version    INTEGER      NOT NULL DEFAULT 1,
    standard_codes     JSONB        NOT NULL,
    tags               JSONB        NOT NULL,
    attributes_json    JSONB        NOT NULL,
    created_by         VARCHAR(64)  NOT NULL,
    updated_by         VARCHAR(64),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_knowledge_asset_asset_id UNIQUE (asset_id),
    CONSTRAINT chk_knowledge_asset_type CHECK (
        asset_type IN ('document','table','image','event','telemetry','dataset')
    ),
    CONSTRAINT chk_knowledge_asset_status CHECK (
        status IN ('draft','active','archived')
    ),
    CONSTRAINT chk_knowledge_asset_version CHECK (current_version >= 1)
);

COMMENT ON TABLE t_knowledge_asset IS '多模态行业知识资产登记表：稳定资产标识与治理元数据';
COMMENT ON COLUMN t_knowledge_asset.standard_codes IS '与行业规范/标准体系对齐的标准编号数组';

CREATE INDEX IF NOT EXISTS idx_knowledge_asset_type_status
    ON t_knowledge_asset (asset_type, status);
CREATE INDEX IF NOT EXISTS idx_knowledge_asset_region
    ON t_knowledge_asset (region, township);
CREATE INDEX IF NOT EXISTS idx_knowledge_asset_updated
    ON t_knowledge_asset (updated_at DESC);

DROP TRIGGER IF EXISTS set_updated_at_knowledge_asset ON t_knowledge_asset;
CREATE TRIGGER set_updated_at_knowledge_asset
    BEFORE UPDATE ON t_knowledge_asset
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();


CREATE TABLE IF NOT EXISTS t_knowledge_asset_version (
    id                     BIGSERIAL PRIMARY KEY,
    version_id             VARCHAR(64) NOT NULL,
    asset_id               VARCHAR(64) NOT NULL REFERENCES t_knowledge_asset(asset_id) ON DELETE CASCADE,
    version_no             INTEGER     NOT NULL,
    status                 VARCHAR(32) NOT NULL DEFAULT 'active',
    content_hash           VARCHAR(64) NOT NULL,
    content_text           TEXT,
    content_json           JSONB,
    extraction_method      VARCHAR(64) NOT NULL DEFAULT 'manual',
    extraction_confidence  NUMERIC(5,4),
    language               VARCHAR(32),
    valid_from             TIMESTAMPTZ,
    valid_to               TIMESTAMPTZ,
    metadata_json          JSONB       NOT NULL,
    created_by             VARCHAR(64) NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_knowledge_version_version_id UNIQUE (version_id),
    CONSTRAINT uq_knowledge_version_asset_no UNIQUE (asset_id, version_no),
    CONSTRAINT chk_knowledge_version_no CHECK (version_no >= 1),
    CONSTRAINT chk_knowledge_version_status CHECK (
        status IN ('active','superseded','retired')
    ),
    CONSTRAINT chk_knowledge_version_confidence CHECK (
        extraction_confidence IS NULL
        OR (extraction_confidence >= 0 AND extraction_confidence <= 1)
    )
);

COMMENT ON TABLE t_knowledge_asset_version IS '知识资产内容版本：正文、表格/图片抽取结构、校验和与有效期';

CREATE INDEX IF NOT EXISTS idx_knowledge_version_asset
    ON t_knowledge_asset_version (asset_id, version_no);


CREATE TABLE IF NOT EXISTS t_ontology_version (
    id                 BIGSERIAL PRIMARY KEY,
    version_id         VARCHAR(64)  NOT NULL,
    name               VARCHAR(128) NOT NULL,
    version_no         INTEGER      NOT NULL,
    status             VARCHAR(32)  NOT NULL DEFAULT 'draft',
    parent_version_id  VARCHAR(64),
    description        TEXT,
    standard_codes     JSONB        NOT NULL,
    metadata_json      JSONB        NOT NULL,
    created_by         VARCHAR(64)  NOT NULL,
    reviewed_by        VARCHAR(64),
    reviewed_at        TIMESTAMPTZ,
    published_by       VARCHAR(64),
    published_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_ontology_version_version_id UNIQUE (version_id),
    CONSTRAINT uq_ontology_version_name_no UNIQUE (name, version_no),
    CONSTRAINT chk_ontology_version_no CHECK (version_no >= 1),
    CONSTRAINT chk_ontology_version_status CHECK (
        status IN ('draft','in_review','published','retired')
    )
);

COMMENT ON TABLE t_ontology_version IS '动态领域本体版本：草稿、审核、发布、退役全生命周期';

CREATE INDEX IF NOT EXISTS idx_ontology_version_status
    ON t_ontology_version (name, status);

DROP TRIGGER IF EXISTS set_updated_at_ontology_version ON t_ontology_version;
CREATE TRIGGER set_updated_at_ontology_version
    BEFORE UPDATE ON t_ontology_version
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();


CREATE TABLE IF NOT EXISTS t_ontology_node (
    id                   BIGSERIAL PRIMARY KEY,
    node_id              VARCHAR(64)  NOT NULL,
    ontology_version_id  VARCHAR(64)  NOT NULL REFERENCES t_ontology_version(version_id) ON DELETE CASCADE,
    entity_type          VARCHAR(64)  NOT NULL,
    name                 VARCHAR(256) NOT NULL,
    canonical_name       VARCHAR(256) NOT NULL,
    description          TEXT,
    aliases              JSONB        NOT NULL,
    properties_json      JSONB        NOT NULL,
    source_asset_id      VARCHAR(64),
    source_version_id    VARCHAR(64),
    confidence           NUMERIC(5,4) NOT NULL DEFAULT 0.5000,
    review_status        VARCHAR(32)  NOT NULL DEFAULT 'proposed',
    reviewed_by          VARCHAR(64),
    reviewed_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_ontology_node_node_id UNIQUE (node_id),
    CONSTRAINT uq_ontology_node_version_name UNIQUE (ontology_version_id, canonical_name),
    CONSTRAINT chk_ontology_node_confidence CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT chk_ontology_node_review_status CHECK (
        review_status IN ('proposed','approved','rejected')
    )
);

COMMENT ON TABLE t_ontology_node IS '本体节点：半自动候选必须经人工审核后才可在发布版本中检索';

CREATE INDEX IF NOT EXISTS idx_ontology_node_name
    ON t_ontology_node (name);
CREATE INDEX IF NOT EXISTS idx_ontology_node_review
    ON t_ontology_node (ontology_version_id, review_status);


CREATE TABLE IF NOT EXISTS t_ontology_relation (
    id                   BIGSERIAL PRIMARY KEY,
    relation_id          VARCHAR(64) NOT NULL,
    ontology_version_id  VARCHAR(64) NOT NULL REFERENCES t_ontology_version(version_id) ON DELETE CASCADE,
    source_node_id       VARCHAR(64) NOT NULL,
    target_node_id       VARCHAR(64) NOT NULL,
    relation_type        VARCHAR(64) NOT NULL,
    description          TEXT,
    properties_json      JSONB       NOT NULL,
    evidence_json        JSONB       NOT NULL,
    confidence           NUMERIC(5,4) NOT NULL DEFAULT 0.5000,
    review_status        VARCHAR(32) NOT NULL DEFAULT 'proposed',
    reviewed_by          VARCHAR(64),
    reviewed_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_ontology_relation_relation_id UNIQUE (relation_id),
    CONSTRAINT uq_ontology_relation_edge UNIQUE (
        ontology_version_id, source_node_id, target_node_id, relation_type
    ),
    CONSTRAINT chk_ontology_relation_confidence CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT chk_ontology_relation_review_status CHECK (
        review_status IN ('proposed','approved','rejected')
    ),
    CONSTRAINT chk_ontology_relation_distinct_nodes CHECK (
        source_node_id <> target_node_id
    )
);

COMMENT ON TABLE t_ontology_relation IS '本体关系：保留共现证据、审核状态与置信度，支持多跳图遍历';

CREATE INDEX IF NOT EXISTS idx_ontology_relation_source
    ON t_ontology_relation (ontology_version_id, source_node_id);
CREATE INDEX IF NOT EXISTS idx_ontology_relation_target
    ON t_ontology_relation (ontology_version_id, target_node_id);


CREATE TABLE IF NOT EXISTS t_decision_trace (
    id                   BIGSERIAL PRIMARY KEY,
    trace_id             VARCHAR(64) NOT NULL,
    run_id               VARCHAR(64),
    question             TEXT        NOT NULL,
    answer_summary       TEXT,
    status               VARCHAR(32) NOT NULL DEFAULT 'completed',
    ontology_version_id  VARCHAR(64),
    policy_version       VARCHAR(64),
    metadata_json        JSONB       NOT NULL,
    created_by           VARCHAR(64) NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_decision_trace_trace_id UNIQUE (trace_id),
    CONSTRAINT chk_decision_trace_status CHECK (
        status IN ('completed','insufficient_evidence','failed')
    )
);

COMMENT ON TABLE t_decision_trace IS '决策轨迹：问题、摘要、状态、运行编号与本体版本';

CREATE INDEX IF NOT EXISTS idx_decision_trace_run
    ON t_decision_trace (run_id);
CREATE INDEX IF NOT EXISTS idx_decision_trace_created
    ON t_decision_trace (created_at DESC);


CREATE TABLE IF NOT EXISTS t_decision_evidence (
    id                BIGSERIAL PRIMARY KEY,
    evidence_id       VARCHAR(64) NOT NULL,
    trace_id          VARCHAR(64) NOT NULL REFERENCES t_decision_trace(trace_id) ON DELETE CASCADE,
    rank_no           INTEGER     NOT NULL,
    asset_id          VARCHAR(64),
    asset_version_id  VARCHAR(64),
    node_id           VARCHAR(64),
    relation_id       VARCHAR(64),
    hop_no            INTEGER     NOT NULL DEFAULT 0,
    citation_text     TEXT        NOT NULL,
    source_uri        VARCHAR(1024),
    score             NUMERIC(10,6) NOT NULL DEFAULT 0,
    metadata_json     JSONB       NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_decision_evidence_evidence_id UNIQUE (evidence_id),
    CONSTRAINT uq_decision_evidence_trace_rank UNIQUE (trace_id, rank_no),
    CONSTRAINT chk_decision_evidence_rank CHECK (rank_no >= 1),
    CONSTRAINT chk_decision_evidence_hop CHECK (hop_no >= 0)
);

COMMENT ON TABLE t_decision_evidence IS '决策证据链：资产/版本、节点/关系、引用片段、跳数与排序';

CREATE INDEX IF NOT EXISTS idx_decision_evidence_trace
    ON t_decision_evidence (trace_id, rank_no);
CREATE INDEX IF NOT EXISTS idx_decision_evidence_asset
    ON t_decision_evidence (asset_id, asset_version_id);


-- ============================================================
-- （11）对话助手 t_chat_session / t_chat_message
--      独立于「事件处置 Agent」的自由对话持久化；只存结构化展示块，
--      不存模型思维链、密钥或原始 Prompt。
-- ============================================================
CREATE TABLE IF NOT EXISTS t_chat_session (
    id                 BIGSERIAL PRIMARY KEY,
    session_id         VARCHAR(64)  NOT NULL,
    username           VARCHAR(64)  NOT NULL,
    title              VARCHAR(200) NOT NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_chat_session_session_id UNIQUE (session_id)
);

COMMENT ON TABLE t_chat_session IS '对话助手会话：属主隔离，按 updated_at 倒序排列';

CREATE INDEX IF NOT EXISTS idx_chat_session_username ON t_chat_session (username);
CREATE INDEX IF NOT EXISTS idx_chat_session_updated  ON t_chat_session (updated_at DESC);


-- 消息表：role 用 CHECK 约束而非 PG 枚举，避免枚举三处同步。
-- content_type: text|image|detection|event_list|stats|dispatch_suggest|error
-- payload 只存检测框/事件卡/统计卡/派单建议卡，不存思维链。
CREATE TABLE IF NOT EXISTS t_chat_message (
    id                 BIGSERIAL PRIMARY KEY,
    message_id         VARCHAR(64)  NOT NULL,
    session_id         VARCHAR(64)  NOT NULL,
    role               VARCHAR(16)  NOT NULL,
    content            TEXT         NOT NULL,
    content_type       VARCHAR(32)  NOT NULL DEFAULT 'text',
    payload            JSONB,
    run_id             VARCHAR(64),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_chat_message_message_id UNIQUE (message_id),
    CONSTRAINT chk_chat_message_role CHECK (role IN ('user', 'assistant')),
    CONSTRAINT fk_chat_message_session FOREIGN KEY (session_id)
        REFERENCES t_chat_session (session_id) ON DELETE CASCADE
);

COMMENT ON TABLE t_chat_message IS '对话助手消息：payload 只存结构化展示块，不存思维链';

CREATE INDEX IF NOT EXISTS idx_chat_message_session ON t_chat_message (session_id, id);
