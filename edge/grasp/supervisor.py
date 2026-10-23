"""常驻作业监督器：开机后不用等派单，自己反复找目标并抓。

## 为什么要有这一个模块

``GraspLoop.run()`` 是「抓一个就收工」—— 抓到、双证据验证通过、放置完成就
立刻 return（见 grasp_loop.py）。两个既有入口都是一次调一次：

* ``vision_driver.OceanusVisionArmDriver.pick()`` —— 平台派一次单抓一个
* ``web/server.py`` 的「开始作业」按钮 —— 点一次抓一个

要做成「开机后自动识别、持续拾取」，必须在外面套一层循环。本模块就是那层
循环：抓成功 → 冷却 → 继续找；视野里没目标 → 短退避轮询；连续失败超限 →
停止并退出（交给 systemd 决定是否重启）。

## ⚠️ 与桥接层互斥

``arm_bridge``（派单驱动）和本模块都会调用同一个 ``GraspLoop`` 驱动同一台
机械臂。**同一时刻只能跑一个** —— 两个进程同时下发舵机指令会互相抢总线。
systemd 部署脚本里二选一启用（见 ``deploy/systemd/install.sh``）。

## 启动

::

    # 真机（先 source 厂商工作空间）
    source ~/armpi_fpv/devel/setup.bash && python3 -m grasp.supervisor

    # 单机验证（无 ROS，合成画面 + 模拟舵机，只跑一轮）
    python -m grasp.supervisor --mock --once
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
for _p in (str(EDGE), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grasp.arm_io import build_arm_io               # noqa: E402
from grasp.calibration import demo_calibration, load_calibration  # noqa: E402
from grasp.config import load_config                # noqa: E402
from grasp.grasp_loop import GraspLoop              # noqa: E402
from grasp.vision.detector import Detector          # noqa: E402

logger = logging.getLogger("oceanus.grasp.supervisor")

#: 退出码：2=前置不满足（缺标定/缺模型），3=连续失败超限
EXIT_PREREQ = 2
EXIT_TOO_MANY_FAILURES = 3


@dataclass
class Stats:
    rounds: int = 0
    picked: int = 0          # 抓成功
    misses: int = 0          # 视野里没目标（正常空转，不算失败）
    failures: int = 0        # 有目标但没抓成
    consecutive_failures: int = 0


def classify(outcome: Any) -> str:
    """从 run() 的日志里分出「视野无目标」与「真失败」。

    为什么要分：两者都返回 ``ok=False``，但处理方式完全不同 —— 没目标是
    正常空转（短间隔再看一眼即可），抓失败说明标定/夹爪/伺服有问题（要计数、
    要退避、要报错）。只靠 ``ok`` 布尔值会把这两种情况混成一种。
    """
    if getattr(outcome, "ok", False):
        return "ok"
    results = {str(e.get("result")) for e in (getattr(outcome, "log", None) or [])}
    if results and results <= {"no_target"}:
        return "no_target"
    return "failed"


def _sleep_responsive(seconds: float, stop: dict[str, bool]) -> None:
    """可被停止信号打断的睡眠：systemd stop 时不用等满一个冷却周期。"""
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline and not stop["flag"]:
        time.sleep(0.2)


def run_forever(
    cfg: dict[str, Any],
    mock: bool,
    *,
    idle_interval: float,
    cooldown: float,
    max_failures: int,
    once: bool = False,
) -> int:
    # ★ build_arm_io 而非直接 ArmIo：开发机跑常驻自捡时，执行端在树莓派上
    #   （arm.remote.enabled=true → ArmIoRemote）；板上原生跑时行为与改动前一模一样。
    io = build_arm_io(cfg, mock=mock)
    io.start()

    detector = Detector(cfg, mock=mock)
    backend = detector.load()

    cal = load_calibration()
    # mock 模式没有真机标定：用演示映射，让整条闭环可离线跑通（口径同 GraspLoop）
    if mock and not cal.ready:
        cal = demo_calibration()

    loop = GraspLoop(io, detector, cal, cfg)
    info = detector.info()
    logger.info("监督器启动：mock=%s 检测后端=%s 标定=%s 相机=%s",
                mock, backend, cal.ready, io.camera_topic)
    logger.warning("本进程与 arm_bridge 互斥（都会驱动同一台机械臂），同一时刻只能跑一个")

    # ---------- 前置检查：不满足就直接说清楚，别空转 ----------
    if not cal.ready:
        logger.error("未完成 9 点标定 —— 打开作业台 http://<IP>:8090/#calib "
                     "跑一次「开始标定」并「保存标定」，然后重启本服务")
        io.stop()
        return EXIT_PREREQ
    if not info.get("ready"):
        logger.error("active 目录里没有可用模型（best.onnx / best.ncnn / best.pt）"
                     "—— 先在作业台「部署与回滚」里部署一次模型")
        io.stop()
        return EXIT_PREREQ

    stop: dict[str, bool] = {"flag": False}

    def _on_stop(_signum: int, _frame: Any) -> None:
        stop["flag"] = True
        loop.request_stop()      # 让正在跑的闭环尽快从当前相位退出
        logger.info("收到停止信号，等当前这一轮收尾…")

    signal.signal(signal.SIGINT, _on_stop)
    signal.signal(signal.SIGTERM, _on_stop)

    stats = Stats()
    wiring_error = False
    try:
        while not stop["flag"]:
            stats.rounds += 1
            task_id = f"auto-{time.strftime('%Y%m%d%H%M%S')}-{stats.rounds:04d}"
            try:
                outcome = loop.run(task_id=task_id)
                kind = classify(outcome)
            except Exception:                     # noqa: BLE001
                logger.exception("闭环抛出异常（本轮记为失败）")
                kind = "failed"
                wiring_error = True
                outcome = None

            if kind == "ok":
                stats.picked += 1
                stats.consecutive_failures = 0
                logger.info("抓取成功（累计 %d 个）—— 冷却 %.1fs 后继续",
                            stats.picked, cooldown)
                wait = cooldown
            elif kind == "no_target":
                stats.misses += 1
                stats.consecutive_failures = 0
                logger.info("视野内无目标 —— %.1fs 后再看", idle_interval)
                wait = idle_interval
            else:
                stats.failures += 1
                stats.consecutive_failures += 1
                logger.warning("本轮失败（连续第 %d 次）：%s",
                               stats.consecutive_failures,
                               getattr(outcome, "message", "") or "见闭环日志")
                wait = idle_interval

            if once:
                break
            if stats.consecutive_failures >= max_failures:
                logger.error("连续失败已达 %d 次，停止作业 —— "
                             "请依次查：标定是否失效 / 相机是否出图 / 夹爪能否闭合 / 舵机是否使能",
                             max_failures)
                break

            _sleep_responsive(wait, stop)

        logger.info("监督器收尾：轮次 %d · 抓取 %d · 空转 %d · 失败 %d",
                    stats.rounds, stats.picked, stats.misses, stats.failures)
    finally:
        io.stop()

    if once:
        # 单机验证：只要求"能跑完一轮且没抛异常"，不要求物理抓取成功
        return 1 if wiring_error else 0
    return EXIT_TOO_MANY_FAILURES if stats.consecutive_failures >= max_failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="常驻作业监督器：反复跑视觉闭环，直到停止",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=None, help="默认读 edge/grasp/config.yaml")
    parser.add_argument("--mock", action="store_true",
                        help="合成画面 + 模拟舵机，不接 ROS（单机验证用）")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    parser.add_argument("--idle-interval", type=float, default=None,
                        help="视野内无目标时的轮询间隔（秒）")
    parser.add_argument("--cooldown", type=float, default=None,
                        help="抓成功后的冷却（秒）")
    parser.add_argument("--max-failures", type=int, default=None,
                        help="连续失败多少次后停止")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config(args.config)
    sup = cfg.get("supervisor", {}) or {}
    mock = bool(args.mock or (cfg.get("web", {}) or {}).get("mock"))

    idle = args.idle_interval if args.idle_interval is not None \
        else float(sup.get("idle_interval_s", 5.0))
    cooldown = args.cooldown if args.cooldown is not None \
        else float(sup.get("cooldown_s", 3.0))
    max_failures = args.max_failures if args.max_failures is not None \
        else int(sup.get("max_consecutive_failures", 5))

    return run_forever(
        cfg, mock,
        idle_interval=max(0.5, idle),
        cooldown=max(0.0, cooldown),
        max_failures=max(1, max_failures),
        once=args.once,
    )


if __name__ == "__main__":
    raise SystemExit(main())
