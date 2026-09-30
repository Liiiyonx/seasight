-- ============================================================
-- 探海灵眸 SeaSight — 种子数据
-- 坐标取自连江县真实沿海乡镇位置（用于演示贴近实际）
-- ============================================================
-- 连江沿海乡镇参考经纬度：
--   马鼻镇   ~ (119.652, 26.386)
--   黄岐镇   ~ (119.904, 26.316)
--   筱埕镇   ~ (119.836, 26.352)
--   苔菉镇   ~ (120.010, 26.293)
--   安凯镇   ~ (119.760, 26.420)
--   下宫镇   ~ (119.887, 26.374)
-- ============================================================

-- ---------- 清理旧数据（幂等重跑用） ----------
TRUNCATE TABLE t_track CASCADE;
TRUNCATE TABLE t_task CASCADE;
TRUNCATE TABLE t_event CASCADE;
TRUNCATE TABLE t_device CASCADE;
TRUNCATE TABLE t_report_daily CASCADE;
TRUNCATE TABLE t_user CASCADE;

-- ---------- 用户（密码明文见下方注释，首次登录后请立即修改） ----------
-- admin    / admin123456
-- operator / operator123456
-- approver / approver123456   ← 审批员独立账号（见下）
-- viewer   / viewer123456
--
-- ★ approver 必须单独存在，不能拿 admin 兼职：
--   Agent 的 WRITE 工具（task.create_or_merge）在演示环境需要人工审批，
--   「高风险动作由第二个自然人放行」这句话只有在拉起两个不同账号时才成立。
--   这里补上这一行，是为了让 `make db-init` 自己就能把四个账号备齐 ——
--   否则只跑 db-init 的人会拿不到 approver（本文件曾是三个账号的版本），
--   演示现场才发现审批队列永远空着。
--   bootstrap_demo_users.py 仍是权威重播脚本：它用的哈希由 passlib 现算，
--   本行的哈希同样是 passlib bcrypt 现算并自验过的，两者结果等价。
INSERT INTO t_user (username, hashed_password, full_name, role, township_scope) VALUES
('admin',    '$2b$12$LQv3c1yqBWVHxkd0LHAkCO.U6bDAa4EBUbumwcycDzQl2ZqIdY7lq', '系统管理员', 'admin',    NULL),
('operator', '$2b$12$EixZaYVK1fsbw1ZfbX3OXeTxCsjZtpnZVxtFMV8DXtj28NthlR7f6', '乡镇操作员', 'operator', '马鼻镇'),
('approver', '$2b$12$shvgOfXHARMvu4UFVF.MFeTTRkX2SkaLHi2AnytHvzkSiHcud/JJO', '值班审批员', 'approver', '马鼻镇'),
('viewer',   '$2b$12$Vc6V8u7BqC5rP7p3mLjXtu9xgYs2cbcAO9bhUzXKdBTc.i9LUGizi', '访客账号',   'viewer',   NULL)
ON CONFLICT (username) DO NOTHING;

-- ---------- 设备 ----------
-- 岸基摄像头（6 个连江沿海点位）
INSERT INTO t_device (device_id, device_type, name, location, status, last_heartbeat, stream_url, meta) VALUES
('CAM-MABI-01',  'shore_camera', '马鼻码头摄像头',   ST_SetSRID(ST_MakePoint(119.6521, 26.3864), 4326), 'online', now(), '/stream/api/stream.flv?src=CAM-MABI-01', '{"model":"HK-DS2CD","resolution":"1920x1080","stream_key":"CAM-MABI-01"}'),
('CAM-HUANGQI-01','shore_camera','黄岐渔港摄像头',   ST_SetSRID(ST_MakePoint(119.9042, 26.3158), 4326), 'online', now(), '/stream/api/stream.flv?src=CAM-HUANGQI-01', '{"model":"HK-DS2CD","resolution":"1920x1080","stream_key":"CAM-HUANGQI-01"}'),
('CAM-XIAOCHENG-01','shore_camera','筱埕养殖区摄像头',ST_SetSRID(ST_MakePoint(119.8362, 26.3517), 4326), 'online', now(), '/stream/api/stream.flv?src=CAM-XIAOCHENG-01', '{"model":"DH-IPC","resolution":"2560x1440","stream_key":"CAM-XIAOCHENG-01"}'),
('CAM-TAILU-01', 'shore_camera', '苔菉航道摄像头',   ST_SetSRID(ST_MakePoint(120.0098, 26.2931), 4326), 'online', now(), '/stream/api/stream.flv?src=CAM-TAILU-01', '{"model":"DH-IPC","resolution":"1920x1080","stream_key":"CAM-TAILU-01"}'),
('CAM-ANKAI-01', 'shore_camera', '安凯渔排区摄像头', ST_SetSRID(ST_MakePoint(119.7605, 26.4198), 4326), 'online', now(), '/stream/api/stream.flv?src=CAM-ANKAI-01', '{"model":"HK-DS2CD","resolution":"1920x1080","stream_key":"CAM-ANKAI-01"}'),
('CAM-XIAGONG-01','shore_camera','下宫回收点摄像头', ST_SetSRID(ST_MakePoint(119.8871, 26.3742), 4326), 'online', now(), '/stream/api/stream.flv?src=CAM-XIAGONG-01', '{"model":"HK-DS2CD","resolution":"1920x1080","stream_key":"CAM-XIAGONG-01"}')
ON CONFLICT (device_id) DO NOTHING;

-- 无人机
INSERT INTO t_device (device_id, device_type, name, location, status, last_heartbeat, meta) VALUES
('UAV-001', 'drone', '巡查无人机 01', ST_SetSRID(ST_MakePoint(119.8700, 26.3600), 4326), 'online', now(), '{"model":"DJI-M3E","endurance_min":45}')
ON CONFLICT (device_id) DO NOTHING;

-- 水面打捞机器人（3 台，含初始状态）
INSERT INTO t_device (device_id, device_type, name, location, status, last_heartbeat, meta) VALUES
('RBT-001', 'robot', '打捞机器人 01 号', ST_SetSRID(ST_MakePoint(119.6540, 26.3870), 4326), 'online', now(), '{"hull":"双体","battery":92,"bins":{"foam":0.05,"plastic":0.02,"mixed":0.01}}'),
('RBT-002', 'robot', '打捞机器人 02 号', ST_SetSRID(ST_MakePoint(119.9050, 26.3165), 4326), 'online', now(), '{"hull":"双体","battery":78,"bins":{"foam":0.35,"plastic":0.10,"mixed":0.05}}'),
('RBT-003', 'robot', '打捞机器人 03 号', ST_SetSRID(ST_MakePoint(119.8370, 26.3525), 4326), 'offline', now() - interval '2 hours', '{"hull":"双体","battery":15,"bins":{"foam":0.60,"plastic":0.40,"mixed":0.20}}')
ON CONFLICT (device_id) DO NOTHING;

-- ---------- 识别事件（模拟最近 48 小时的真实分布） ----------
-- 主要聚集在马鼻、黄岐附近（对应真实海漂垃圾高发区）
-- ★ township 是「operator 辖区隔离」的关键字段：operator(马鼻镇) 登录后
--   只看到马鼻镇的事件，admin/viewer 看全部 —— 三个账号互不干扰。
INSERT INTO t_event (event_id, device_id, event_time, location, main_class, det_count, max_confidence, evidence_url, model_version, seq, status, township) VALUES
-- 马鼻镇附近（泡沫类为主，高优先级；operator 辖区）
('evt_demo_0001', 'CAM-MABI-01',   now() - interval '2 hours',  ST_SetSRID(ST_MakePoint(119.6530, 26.3870), 4326), 'foam',        5, 0.91, NULL, 'det_v0.1.0', 1001, 'new', '马鼻镇'),
('evt_demo_0002', 'CAM-MABI-01',   now() - interval '3 hours',  ST_SetSRID(ST_MakePoint(119.6535, 26.3875), 4326), 'foam',        3, 0.87, NULL, 'det_v0.1.0', 1002, 'new', '马鼻镇'),
('evt_demo_0003', 'CAM-MABI-01',   now() - interval '5 hours',  ST_SetSRID(ST_MakePoint(119.6525, 26.3862), 4326), 'plastic',     2, 0.79, NULL, 'det_v0.1.0', 1003, 'new', '马鼻镇'),
('evt_demo_0004', 'CAM-MABI-01',   now() - interval '8 hours',  ST_SetSRID(ST_MakePoint(119.6545, 26.3880), 4326), 'fishing_gear',4, 0.84, NULL, 'det_v0.1.0', 1004, 'new', '马鼻镇'),
('evt_demo_0005', 'CAM-MABI-01',   now() - interval '12 hours', ST_SetSRID(ST_MakePoint(119.6518, 26.3858), 4326), 'foam',        7, 0.93, NULL, 'det_v0.1.0', 1005, 'new', '马鼻镇'),
('evt_demo_0015', 'CAM-MABI-01',   now() - interval '1 hours',  ST_SetSRID(ST_MakePoint(119.6538, 26.3866), 4326), 'plastic',     3, 0.82, NULL, 'det_v0.1.0', 1006, 'new', '马鼻镇'),
('evt_demo_0016', 'CAM-MABI-01',   now() - interval '6 hours',  ST_SetSRID(ST_MakePoint(119.6512, 26.3872), 4326), 'foam',        4, 0.89, NULL, 'det_v0.1.0', 1007, 'new', '马鼻镇'),
-- 黄岐镇附近
('evt_demo_0006', 'CAM-HUANGQI-01',now() - interval '4 hours',  ST_SetSRID(ST_MakePoint(119.9050, 26.3165), 4326), 'foam',        4, 0.88, NULL, 'det_v0.1.0', 2001, 'new', '黄岐镇'),
('evt_demo_0007', 'CAM-HUANGQI-01',now() - interval '7 hours',  ST_SetSRID(ST_MakePoint(119.9055, 26.3160), 4326), 'plastic',     3, 0.76, NULL, 'det_v0.1.0', 2002, 'new', '黄岐镇'),
('evt_demo_0008', 'CAM-HUANGQI-01',now() - interval '20 hours', ST_SetSRID(ST_MakePoint(119.9045, 26.3170), 4326), 'foam',        6, 0.90, NULL, 'det_v0.1.0', 2003, 'new', '黄岐镇'),
('evt_demo_0017', 'CAM-HUANGQI-01',now() - interval '13 hours', ST_SetSRID(ST_MakePoint(119.9058, 26.3168), 4326), 'fishing_gear',2, 0.80, NULL, 'det_v0.1.0', 2004, 'new', '黄岐镇'),
-- 筱埕镇附近
('evt_demo_0009', 'CAM-XIAOCHENG-01', now() - interval '6 hours', ST_SetSRID(ST_MakePoint(119.8365, 26.3520), 4326), 'fishing_gear',2, 0.81, NULL, 'det_v0.1.0', 3001, 'new', '筱埕镇'),
('evt_demo_0010', 'CAM-XIAOCHENG-01', now() - interval '16 hours',ST_SetSRID(ST_MakePoint(119.8360, 26.3515), 4326), 'foam',        3, 0.86, NULL, 'det_v0.1.0', 3002, 'new', '筱埕镇'),
-- 苔菉 / 安凯 / 下宫（零星）
('evt_demo_0011', 'CAM-TAILU-01',  now() - interval '9 hours',  ST_SetSRID(ST_MakePoint(120.0100, 26.2935), 4326), 'other',       1, 0.68, NULL, 'det_v0.1.0', 4001, 'new', '苔菉镇'),
('evt_demo_0012', 'CAM-ANKAI-01',  now() - interval '11 hours', ST_SetSRID(ST_MakePoint(119.7600, 26.4200), 4326), 'foam',        2, 0.83, NULL, 'det_v0.1.0', 5001, 'new', '安凯镇'),
('evt_demo_0013', 'CAM-XIAGONG-01',now() - interval '14 hours', ST_SetSRID(ST_MakePoint(119.8875, 26.3745), 4326), 'plastic',     2, 0.74, NULL, 'det_v0.1.0', 6001, 'new', '下宫镇'),
-- 无人机巡查发现（筱埕附近）
('evt_demo_0014', 'UAV-001',       now() - interval '10 hours', ST_SetSRID(ST_MakePoint(119.8700, 26.3600), 4326), 'foam',        8, 0.89, NULL, 'det_v0.1.0', 7001, 'new', '筱埕镇')
ON CONFLICT (event_id) DO NOTHING;

-- 模拟已完成的工单（让看板有历史数据）+ 一个马鼻镇待派单工单
INSERT INTO t_task (task_id, event_id, robot_id, target_location, status, priority,
                    created_at, assigned_at, ack_at, started_at, finished_at,
                    collected_weight, review_result, township)
VALUES
('tsk_demo_0001', 'evt_demo_0005', 'RBT-001',
 ST_SetSRID(ST_MakePoint(119.6518, 26.3858), 4326), 'done', 1,
 now() - interval '12 hours', now() - interval '11 hours 55 minutes',
 now() - interval '11 hours 54 minutes', now() - interval '11 hours 50 minutes',
 now() - interval '11 hours 30 minutes', 12.500, 'confirmed', '马鼻镇'),
('tsk_demo_0002', 'evt_demo_0016', NULL,
 ST_SetSRID(ST_MakePoint(119.6512, 26.3872), 4326), 'pending', 1,
 now() - interval '6 hours', NULL, NULL, NULL, NULL, NULL, 'pending', '马鼻镇'),
('tsk_demo_0003', 'evt_demo_0006', 'RBT-002',
 ST_SetSRID(ST_MakePoint(119.9050, 26.3165), 4326), 'collecting', 1,
 now() - interval '4 hours', now() - interval '3 hours 55 minutes',
 now() - interval '3 hours 54 minutes', now() - interval '3 hours 50 minutes',
 NULL, NULL, 'pending', '黄岐镇')
ON CONFLICT (task_id) DO NOTHING;

-- 事件状态同步为已处理
UPDATE t_event SET status = 'resolved' WHERE event_id = 'evt_demo_0005';

-- ---------- 作业轨迹（模拟已完成工单的轨迹） ----------
INSERT INTO t_track (robot_id, task_id, location, battery, bin_foam, bin_plastic, bin_mixed, speed, recorded_at)
SELECT
    'RBT-001',
    'tsk_demo_0001',
    ST_SetSRID(ST_MakePoint(119.6518 + (i * 0.0001), 26.3858 + (i * 0.0001)), 4326),
    92 - i,
    0.01 * i,
    0.005 * i,
    0.002 * i,
    0.8,
    now() - interval '11 hours 50 minutes' + (i || ' minutes')::interval
FROM generate_series(1, 20) AS i;

-- ---------- 统计宽表（预聚合最近 3 天） ----------
-- WP-07 数据完整性：coverage_area 没有可靠真实数据源（机器人清扫面积上报
-- 链路未实现），种子一律 NULL + not_available —— 禁止用编造面积冒充实测。
-- 结构区分：NULL=未统计；「真实 0」只有在有记录但合计为 0 时才出现（届时
-- availability 应为 available/partial 且值为 0.00）。
INSERT INTO t_report_daily (stat_date, township, main_class, event_count, task_count, done_count, collected_kg, coverage_area, coverage_availability)
VALUES
(CURRENT_DATE - 2, '马鼻镇', 'foam',         12, 3, 3, 28.500, NULL, 'not_available'),
(CURRENT_DATE - 2, '马鼻镇', 'plastic',       5, 1, 1,  6.200, NULL, 'not_available'),
(CURRENT_DATE - 1, '马鼻镇', 'foam',          9, 2, 2, 21.000, NULL, 'not_available'),
(CURRENT_DATE - 1, '黄岐镇', 'foam',          7, 2, 1, 15.500, NULL, 'not_available'),
(CURRENT_DATE,     '马鼻镇', 'foam',          5, 1, 1, 12.500, NULL, 'not_available'),
(CURRENT_DATE,     '黄岐镇', 'fishing_gear',  4, 1, 0,  0.000, NULL, 'not_available')
ON CONFLICT (stat_date, township, main_class) DO NOTHING;

-- ---------- 校验输出 ----------
DO $$
DECLARE
    dev_cnt INT; evt_cnt INT; tsk_cnt INT; trk_cnt INT;
BEGIN
    SELECT COUNT(*) INTO dev_cnt FROM t_device;
    SELECT COUNT(*) INTO evt_cnt FROM t_event;
    SELECT COUNT(*) INTO tsk_cnt FROM t_task;
    SELECT COUNT(*) INTO trk_cnt FROM t_track;
    RAISE NOTICE '[seed] 设备 % 台 | 事件 % 条 | 工单 % 条 | 轨迹 % 点',
        dev_cnt, evt_cnt, tsk_cnt, trk_cnt;
END $$;
