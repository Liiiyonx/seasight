-- Idempotently create daily t_track partitions around the current date.
-- Run daily from cron/systemd, or schedule it through pg_cron.
--
-- 现在有两道保障，本脚本只是其中一道：
--   1) 应用侧自愈（推荐）：backend/app/db/partitions.py 在每条机器人遥测
--      落库前确保当天分区存在。这是主路径，不依赖任何外部调度。
--   2) 本脚本：给运维/演示前的显式兜底，也是挂 pg_cron 的现成入口。
--      入口：make ensure-partitions（容器内路径 /sql/ensure_track_partitions.sql）
--
-- 为什么必须补：t_track 是按日分区表，父表不存数据。缺当天分区时
-- Postgres 直接拒绝 INSERT（CheckViolationError: no partition of relation
-- "t_track" found for row），机器人轨迹全部写不进去 ——
-- 线上表现是「新建工单点仿真 → 红色异常」，而日期在初始化窗口内的
-- 老工单却正常，极易被误判成"新工单坏了"。

DO $$
DECLARE
    d date;
BEGIN
    FOR d IN
        SELECT generate_series(
            CURRENT_DATE - 1,
            CURRENT_DATE + 14,
            interval '1 day'
        )::date
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS t_track_%s '
            'PARTITION OF t_track FOR VALUES FROM (%L) TO (%L)',
            to_char(d, 'YYYYMMDD'),
            d,
            d + 1
        );
    END LOOP;
END $$;
