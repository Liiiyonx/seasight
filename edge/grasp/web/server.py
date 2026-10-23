"""树莓派上的训练作业台（web 层）。

页面职责（对应实施计划书 §4.1）：工单上下文 / 实时画面 / 数据采集 /
标注台 / 训练任务 / 部署验证，外加手动点动与标定引导。

**页面跑在树莓派上、不在平台里**：浏览器直连 ``http://<IP>:8090``，
采集、训练、部署、作业控制全部在 Pi 本地闭环；平台只做入口与看板，
MQTT 契约一行不用改（平台侧把环境变量配成 ``http://{ip}:8090`` 即可）。

启动：

    # 真机（先 source 厂商工作空间）
    source ~/armpi_fpv/devel/setup.bash && python3 -m grasp.web.server

    # 开发机（无 ROS，合成画面 + 模拟舵机跑通 UI）
    python -m grasp.web.server --mock

训练进度用**轮询**（``/api/train/status``）而不是 SSE：轮询实现更短、
断线自恢复、也不占长连接；演示场景下 0.5s 粒度完全够用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
GRASP_DIR = HERE.parent
EDGE_DIR = GRASP_DIR.parent
for path in (str(EDGE_DIR), str(GRASP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from grasp.arm_io import ArmIo, ArmIoError, build_arm_io        # noqa: E402
from grasp.calibration import (                                 # noqa: E402
    Calibration,
    capture_at_observation,
    config_plane_z,
    demo_calibration,
    describe_place,
    load_calibration,
    save_calibration,
)
from grasp.config import load_config                            # noqa: E402
from grasp.grasp_loop import GraspLoop                          # noqa: E402
from grasp.presets import PresetError, PresetStore, normalize_name  # noqa: E402
from grasp.vision.dataset import Box, Dataset, DatasetError     # noqa: E402
from grasp.vision.deploy import DeployError, ModelStore         # noqa: E402
from grasp.vision.detector import Detector, DetectorError      # noqa: E402
from grasp.vision.evaluate import evaluate, passes              # noqa: E402
from grasp.vision.mix import MixError, mix_datasets              # noqa: E402
from grasp.vision.sweep import run_deployment_sweep              # noqa: E402
from grasp.vision.train import TIERS, TrainingError, train, training_available  # noqa: E402

logger = logging.getLogger("oceanus.grasp.web")

#: 最前端关节（腕部旋转）的**实测安全行程**。
#: 这几个数不是照抄默认值：在真机上小步走过 380~680 全程，跟随时差 ±2；
#: 更外面（<300 / >700）没验证过，越界会让夹爪带着相机一起甩，
#: 可能撞到自身或台面 —— 所以界面与接口都按这段硬约束。
WRIST_MIN, WRIST_MAX, WRIST_CENTER = 300, 700, 500

#: 骨架关节（id3~id6）的**实测可用行程**与界面放开的「跟随可靠内段」。
#: 实测方法见 _work/probe_joint_limits.py：从当前位置每步 40 脉宽双向试走，
#: 比较下发值与回读值，偏差 >30 判定到限位。实测结论：
#:   id3 151~627（全程误差 ≤5）
#:   id4 514~1001（顶到量程上限 1000，所以界面留余量）
#:   id5 344~808（靠下限时误差涨到 13）
#:   id6 275~733（靠下限时误差涨到 16）
#: ★ 为什么不用实测极值当行程：这些是 IK 耦合关节，落在「跟不动」的区间时
#:   舵机会不到位但**不报错**，末端位置就与预期不符，抓取会瞄错地方。
#:   宁可拒绝，也不要给一个看起来有效、实际失真的行程。
JOINT_ENVELOPE: dict[str, dict[str, Any]] = {
    "J3": {"servo_id": 3, "min": 170, "max": 620, "label": "升降大臂",
           "measured": [151, 627]},
    "J4": {"servo_id": 4, "min": 520, "max": 980, "label": "肘/前臂",
           "measured": [514, 1001]},
    "J5": {"servo_id": 5, "min": 370, "max": 790, "label": "腕部俯仰",
           "measured": [344, 808]},
    "J6": {"servo_id": 6, "min": 300, "max": 720, "label": "末端旋转",
           "measured": [275, 733]},
}


# ======================================================================
# 运行时状态
# ======================================================================
@dataclass
class Job:
    """一个后台任务（采集 / 训练 / 标定 / 抓取）的进度快照。"""

    kind: str = ""
    running: bool = False
    stage: str = "idle"
    detail: dict[str, Any] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float = 0.0
    #: 软停止（``/api/stop``）置位。作业体必须**自己**查它才会提前退出 ——
    #  ``running`` 只在整个作业结束时才变 False，拿它当取消信号等于「停」永不生效。
    #  目前只有采集循环接了这个信号（见 ``_job_capture``）；抓取闭环走自己的
    #  ``GraspLoop.request_stop()``，训练/扫描在长跑步骤里无法就地中断。
    cancel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "running": self.running,
            "stage": self.stage,
            "detail": dict(self.detail),
            "log": list(self.log[-40:]),
            "result": self.result,
            "error": self.error,
            "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
        }


class TaskListener:
    """只读订阅平台派单，供页面显示工单上下文（不改平台契约）。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        mqtt = cfg.get("mqtt", {}) or {}
        self.enabled = bool(mqtt.get("enabled", True))
        # 与 arm_bridge/main.py 共用同一组环境变量：作业台在树莓派上跑时 broker
        # 在平台那侧（不是 localhost），现场由 systemd 注入。两边共用一个变量名，
        # 避免「桥接层连 A、作业台连 B」这种最难查的配置漂移。
        self.host = os.environ.get("OCEANUS_MQTT_HOST") or str(mqtt.get("host", "localhost"))
        self.port = int(os.environ.get("OCEANUS_MQTT_PORT") or mqtt.get("port", 1883))
        self.username = os.environ.get("OCEANUS_MQTT_USER") or str(mqtt.get("username", "robot_device"))
        self.password = os.environ.get("OCEANUS_MQTT_PASSWORD") or str(mqtt.get("password", "CHANGE_ME"))
        self.client_id = str(mqtt.get("client_id", "oceanus_grasp_web"))
        self.device_id = str(cfg.get("device_id", "RBT-ARM-01"))
        self.site_id = str(cfg.get("site_id", "lianjiang"))
        self.context: dict[str, Any] = {"status": "未连接"}
        self._client: Any = None

    @property
    def topic(self) -> str:
        return f"robot/{self.device_id}/task"

    def start(self) -> None:
        if not self.enabled:
            self.context = {"status": "已禁用（配置 mqtt.enabled=false）"}
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.context = {"status": "未安装 paho-mqtt，无法订阅派单"}
            return

        def _on_connect(client: Any, _userdata: Any, _flags: Any, rc: int) -> None:
            if rc == 0:
                client.subscribe(self.topic, qos=1)
                self.context = {"status": f"已订阅 {self.topic}"}
            else:
                self.context = {"status": f"MQTT 连接失败 rc={rc}"}

        def _on_message(_client: Any, _userdata: Any, msg: Any) -> None:
            import json

            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except ValueError:
                payload = {"raw": msg.payload.decode("utf-8", "ignore")}
            params = payload.get("params") or {}
            self.context = {
                "status": "已收到派单",
                "task_id": params.get("task_id"),
                "target": params.get("target"),
                "priority": params.get("priority"),
                "command_id": payload.get("command_id"),
                "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

        try:
            client = mqtt.Client(client_id=self.client_id)
            if self.username:
                client.username_pw_set(self.username, self.password)
            client.on_connect = _on_connect
            client.on_message = _on_message
            client.connect_async(self.host, self.port, 30)
            client.loop_start()
            self._client = client
        except Exception as exc:  # noqa: BLE001
            self.context = {"status": f"MQTT 启动失败：{exc}"}
            logger.warning("MQTT 订阅启动失败: %s", exc)

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass


class AppState:
    """进程内共享态（单进程服务，一个实例足够）。"""

    def __init__(self, cfg: dict[str, Any], mock: bool) -> None:
        self.cfg = cfg
        self.mock = mock
        # 执行端三选一：板上代理（arm.remote.enabled）/ 本机 ROS / mock。
        # 远端模式下本机不需要 ROS：画面与舵机都过那一条 TCP 连接。
        self.io = build_arm_io(cfg, mock)
        # ★ 有真实帧源（USB 摄像头）时用**真模型**检测：合成检测（HSV）是为
        #   合成画面设计的，喂真摄像头画面会产出一堆垃圾框。帧源真伪只取决于
        #   usb_camera，与执行端是否 mock 无关 —— 于是「真画面 + 真检测 +
        #   模拟舵机」这个开发机组合成立。
        real_frames = getattr(self.io, "usb_camera", None) is not None
        self.detector = Detector(cfg, mock=(mock and not real_frames))
        # 水面/台面高度：文件名里有就用文件里的（现场量的），否则用 config 的
        self.plane_z_default = config_plane_z(cfg)
        self.cal: Calibration = load_calibration(plane_z=self.plane_z_default)
        # mock 模式没有真机标定 —— 用演示映射，让状态接口与闭环口径一致
        self.cal_is_demo = False
        if mock and not self.cal.ready:
            self.cal = demo_calibration(
                plane_z=self.plane_z_default if self.plane_z_default is not None
                else self.cal.plane_z
            )
            self.cal_is_demo = True
        self.dataset = Dataset.from_config(cfg)
        self.store = ModelStore.from_config(cfg)
        self.loop = GraspLoop(self.io, self.detector, self.cal, cfg)
        self.listener = TaskListener(cfg)

        self.job = Job()
        self.capture_session: str = ""
        self.dets: list[Any] = []
        self.dets_ts: float = 0.0
        #: 最近一次「部署链路扫描」的结果快照。只在内存里、不落盘 —— 它是一份
        #: **证据快照**（页面刷新后还要看得见），与 tuning.json 里那份「生效中的
        #: 调参」职责不同：后者必须持久化，重启后重扫即可。
        self.last_sweep: dict[str, Any] | None = None
        # 姿态预设（一键切回某个已知姿态）
        self.presets = PresetStore()
        # 手动位置控制的目标位姿。用途有二：
        #   1) 方向键增量移动要有个「当前在哪」的起点，否则每次都得重填绝对坐标；
        #   2) 界面回显，避免操作员靠猜。
        # ★ 这是**下发目标**，不是测量值 —— 舵机没编码器反馈到笛卡尔空间，
        #   真实位姿只能靠读 6 个舵机脉宽间接判断（见 /api/status 的 servos）。
        #   初始值取 config 的待命位，与 io.home() 保持一致。
        _home = ((cfg.get("arm") or {}).get("home") or {})
        _coord = list(_home.get("coord") or [0.0, 0.17, 0.30])
        self.manual_target: dict[str, float] = {
            "x": float(_coord[0]), "y": float(_coord[1]), "z": float(_coord[2]),
            "pitch": float(_home.get("pitch", -65)),
        }
        self._lock = threading.RLock()
        self._infer_thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._stop_infer = threading.Event()

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.io.start()
        backend = self.detector.load()
        logger.info("作业台启动：mock=%s 检测后端=%s 标定=%s",
                    self.mock, backend, self.cal.ready)
        self.listener.start()
        self._stop_infer.clear()
        self._infer_thread = threading.Thread(target=self._infer_loop, daemon=True)
        self._infer_thread.start()

    def stop(self) -> None:
        self._stop_infer.set()
        self.listener.stop()
        self.io.stop()

    def _infer_loop(self) -> None:
        """后台推理：4fps 产出检测框，供 MJPEG 叠加与状态上报。

        为什么不放在 MJPEG 循环里逐帧推理：Pi 上单帧推理几十到几百毫秒，
        会把推流帧率拖到个位数。分离后画面流畅、检测框略微滞后即可。
        """
        while not self._stop_infer.is_set():
            try:
                frame = self.io.latest_frame()
                dets = self.detector.infer(frame) if frame is not None else []
                with self._lock:
                    self.dets = dets
                    self.dets_ts = time.time()
            except Exception:  # noqa: BLE001
                logger.exception("推理循环异常")
            time.sleep(0.25)

    def latest_dets(self) -> list[Any]:
        with self._lock:
            return list(self.dets)

    # ------------------------------------------------------------------
    def start_job(self, kind: str, target: Any, *args: Any, **kwargs: Any) -> Job:
        """启动一个后台任务；已有任务在跑时拒绝（409 由调用方抛）。"""
        with self._lock:
            if self.job.running:
                raise RuntimeError(f"已有任务在运行：{self.job.kind}")
            self.job = Job(kind=kind, running=True, stage="starting", started_at=time.time())
            job_ref = self.job

        def _runner() -> None:
            try:
                target(job_ref, *args, **kwargs)
                job_ref.stage = "done"
            except Exception as exc:  # noqa: BLE001
                job_ref.error = str(exc)
                job_ref.stage = "error"
                logger.exception("后台任务 %s 失败", kind)
            finally:
                job_ref.running = False

        self._worker = threading.Thread(target=_runner, daemon=True)
        self._worker.start()
        return self.job


STATE: AppState | None = None


def _state() -> AppState:
    if STATE is None:
        raise HTTPException(status_code=503, detail="服务未初始化")
    return STATE


# ======================================================================
# 放置点（旁边的料槽）
# ======================================================================
def _current_pose(st: AppState) -> dict[int, int]:
    """把当前舵机位置读成一组 ``{舵机ID: 脉宽}``，用作示教放置位姿。

    优先取回读 ``position``（机械臂**实际**在哪），取不到才退回 ``goal``
    （我们刚下发的目标）—— 示教要记录的是"摆在哪里"，不是"命令它去哪"。
    """
    tele = st.io.servo_telemetry() or {}
    pose: dict[int, int] = {}
    for sid in (st.io.gripper_servo, st.io.wrist_servo, *st.io.ik_servos):
        row = tele.get(str(sid)) or {}
        value = row.get("position")
        if value is None:
            value = row.get("goal")
        if value is not None:
            pose[int(sid)] = int(value)
    return pose


def _persist_calibration(st: AppState) -> bool:
    """把标定文件落盘（放置点、水面高度都记在里面）。

    ★ 演示标定（mock）与未就绪的标定**不落盘** —— 与「保存标定」同一条纪律：
      不能让一个「看起来合法」的 calibration.json 被拷到真机上当真值用。
      此时只存在内存里（mock 联调够用），响应里 ``persisted=false``。
    """
    if st.cal_is_demo or not st.cal.ready:
        return False
    try:
        save_calibration(st.cal)
    except OSError:
        logger.exception("标定文件落盘失败")
        return False
    return True


# ======================================================================
# 后台任务体
# ======================================================================
def _job_capture(job: Job, frames: int, tag: str, augment: bool) -> None:
    st = _state()
    session = st.dataset.new_session(tag or "capture")
    st.capture_session = session
    job.detail["session"] = session
    job.detail["target_frames"] = frames
    # 采集姿态增广：机械臂在 3–4 个观测姿态间轮换，视角多样性比连拍有用
    poses = [(0.0, 0.17, 0.30), (-0.05, 0.15, 0.26), (0.05, 0.15, 0.26), (0.0, 0.13, 0.24)]
    saved = 0
    i = 0
    while saved < frames:
        # 软停止（/api/stop）会置 job.cancel；采集是唯一能在帧间安全退出的作业体，
        # 不看这个信号的话「停」对采集完全无效（running 只在作业结束时才变 False）。
        if job.cancel:
            break
        if augment and i % max(1, frames // len(poses)) == 0:
            px, py, pz = poses[(i // max(1, frames // len(poses))) % len(poses)]
            st.io.move_to(px, py, pz)
            time.sleep(0.8)
        frame = st.io.latest_frame()
        if frame is not None:
            import cv2

            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok:
                st.dataset.add_frame(session, buf.tobytes())
                saved += 1
                job.detail["saved"] = saved
        i += 1
        time.sleep(0.12)
    if job.cancel:
        # 如实报「中止 + 已存几帧」而不是当成采满：done·12/200 一眼就能看出没跑完
        job.log.append(f"已软停止中止：{saved}/{frames} 帧 → {session}")
        job.result = {"session": session, "frames": saved, "cancelled": True}
    else:
        job.log.append(f"采集完成：{saved} 帧 → {session}")
        job.result = {"session": session, "frames": saved}


def _job_calibrate(job: Job, grid: list[tuple[float, float]]) -> None:
    st = _state()

    def _on_step(idx: int, world: tuple[float, float], pixel: Any) -> None:
        job.detail["step"] = idx
        job.detail["world"] = [round(world[0], 4), round(world[1], 4)]
        job.detail["pixel"] = [round(pixel[0], 1), round(pixel[1], 1)] if pixel else None
        job.log.append(f"第 {idx}/{len(grid)} 点：" +
                       (f"像素 {pixel[0]:.0f},{pixel[1]:.0f}" if pixel else "未检测到目标，跳过"))

    cal = capture_at_observation(st.io, st.detector, grid,
                                marker_class=st.detector.target_class,
                                plane_z=st.cal.plane_z, on_step=_on_step)
    st.cal = cal
    st.loop.cal = cal
    path = save_calibration(cal)
    job.log.append(f"标定完成：{len(cal.pairs)} 点，RMSE={cal.rmse:.4f}m → {path}")
    job.result = {"points": len(cal.pairs), "rmse": cal.rmse, "path": str(path)}


def _job_grasp(job: Job, task_id: str) -> None:
    st = _state()

    def _on_phase(phase: str, info: dict[str, Any]) -> None:
        job.stage = phase
        job.detail.update(info)
        job.log.append(f"{phase} {info or ''}")

    # 用临时的 on_phase 复跑同一个 GraspLoop：一份实现、两处入口
    loop = GraspLoop(st.io, st.detector, st.cal, st.cfg, on_phase=_on_phase)
    outcome = loop.run(task_id=task_id)
    job.result = outcome.to_dict()
    if not outcome.ok:
        job.log.append(f"未成功：{outcome.message}")
    elif outcome.placed:
        job.log.append(f"抓取成功并放入料槽（第 {outcome.attempts} 次尝试）")
    else:
        job.log.append(f"抓取成功但未放入料槽：{outcome.message}")
    job.stage = "done" if outcome.ok else "error"
    if not outcome.ok:
        job.error = outcome.message


def _compose_dataset(job: Job, sessions: list[str], group_splits: bool,
                     mix_yamls: list[str]) -> tuple[Path, dict[str, Any]]:
    """组装（并按需混采）训练集，返回 ``(data.yaml, 组装统计)``。

    ★ 为什么从 ``_job_train`` 里拆出来：这一步是纯数据操作、**不依赖 ultralytics**，
    所以本机（没装训练栈）也能直接单测。留在 ``_job_train`` 里的话，「多会话 /
    整组切分 / 混采」这三种最关键的输入口径就只有装了训练栈的机器才跑得到
    —— 等于输入面完全没有覆盖。
    """
    st = _state()
    stats = st.dataset.build(session=(sessions or None), group_splits=group_splits)
    yaml_path = Path(stats["data_yaml"])
    job.detail.update({k: v for k, v in stats.items() if k != "data_yaml"})
    job.detail["data_yaml"] = str(yaml_path)
    job.detail["sessions"] = list(sessions)
    job.log.append(f"数据集组装：train={stats['train']} val={stats['val']} "
                   f"背景帧={stats.get('background', 0)} 类别分布={stats['per_class']}"
                   f"（会话={'/'.join(sessions) if sessions else '全部'}）")
    if group_splits:
        job.log.append(f"整组切分：val 单元={stats.get('val_units')}")
    if mix_yamls:
        # 失败是 MixError（普通 Exception）：后台运行器只捕 Exception，用 SystemExit
        # 会让这个任务停在 starting 且不报错。
        try:
            mixed = mix_datasets(yaml_path, [Path(p) for p in mix_yamls],
                                 st.dataset.root / "built_mixed")
        except MixError as exc:
            raise RuntimeError(f"混采不可行：{exc}") from exc
        yaml_path = Path(mixed["data_yaml"])
        job.detail["mixed"] = mixed
        job.log.append(f"混采（replay）：train={mixed['train']} val={mixed['val']} "
                       f"← {mixed['sources']}")
        job.log.append("⚠ 混采 val 是两域合并的，不能当泳池域指标读")
    return yaml_path, stats


def _job_train(job: Job, tier: str, sessions: list[str], group_splits: bool,
               mix_yamls: list[str], min_map50: float, min_precision: float) -> None:
    st = _state()
    job.detail.update({"tier": tier, "group_splits": group_splits,
                       "mix": list(mix_yamls), "min_map50": min_map50,
                       "min_precision": min_precision})

    def _on_progress(stage: str, info: dict[str, Any]) -> None:
        job.stage = stage
        job.detail.update({k: v for k, v in info.items() if k != "result"})
        if stage == "epoch":
            # 一律 float(x or 0)：回调传 None 时格式化会抛异常，把整个训练作业打死
            # （原先依赖 train.py 保证有值，属于隐式依赖）。
            job.log.append(
                f"epoch {info.get('epoch')}/{info.get('total_epochs')} "
                f"loss={float(info.get('loss') or 0):.4f} "
                f"mAP50={float(info.get('map50') or 0):.4f}"
            )
        elif stage == "sweep":
            job.log.append(f"conf {info.get('conf')} → P {info.get('precision')} "
                           f"R {info.get('recall')} F1 {info.get('f1')}")
        elif stage in ("export", "done", "eval"):
            job.log.append(f"{stage}: {info.get('message') or info}")

    yaml_path, stats = _compose_dataset(job, sessions, group_splits, mix_yamls)
    result = train(st.cfg, tier, dataset_yaml=yaml_path, progress_cb=_on_progress)
    job.log.append(f"训练产物：{result.get('model_path')}"
                   f"（训练 imgsz={result.get('train_imgsz')} / "
                   f"导出 imgsz={result.get('export_imgsz')}）")

    # 免训练校准档没有模型产物；其余档训完立刻在留出集上量一次，把 P/R 与门禁摊开。
    # ★ 门禁**不阻塞产物落盘**：训练已经花掉十几分钟，产物本身有价值；真正拦人的
    #   位置是「4 部署与回滚」那一步。
    if result.get("onnx") or result.get("best_pt"):
        try:
            model_path = result.get("onnx") or result.get("best_pt")
            metrics = evaluate(st.cfg, model_path=model_path, dataset_yaml=yaml_path)
            ok, reason = passes(metrics, min_map50, 0.0, min_precision)
            result["metrics"] = metrics
            result["gate"] = {"passed": ok, "reason": reason,
                              "min_map50": min_map50, "min_precision": min_precision}
            job.log.append(f"留出集：mAP50={metrics['map50']} P={metrics['precision']} "
                           f"R={metrics['recall']}")
            job.log.append(f"[门禁] {'通过' if ok else '未过'} —— {reason}"
                           + ("" if ok else "（产物已落盘；是否部署由你在「4 部署与回滚」决定）"))
            if mix_yamls:
                # 混采时上面那个数是两域合并的；泳池域自己的数要单独量一次，否则
                # 「换域后到底行不行」会被合并 val 平均掉（与 CLI 的告警同一条纪律）。
                pool = evaluate(st.cfg, model_path=model_path,
                                dataset_yaml=stats["data_yaml"])
                result["pool_metrics"] = pool
                job.log.append(f"泳池域 val（未混）：mAP50={pool['map50']} "
                               f"P={pool['precision']} R={pool['recall']}")
        except Exception as exc:  # noqa: BLE001
            job.log.append(f"留出集评估不可用，训练产物仍已落盘：{exc}")

    job.result = result
    # ★ 必须重载检测器：免训练校准会把新阈值/ROI 写进 tuning.json，但 Detector 是
    #   长驻对象，不 load() 就仍在用旧值 —— 会出现「校准跑完了、界面也显示新数字、
    #   实际推理还是旧阈值」这种最难发现的假成功。其它档重载也无副作用（active
    #   模型没变，只是把 tuning 再叠加一次）。
    st.detector.load()
    job.log.append(f"已重载检测后端：conf={st.detector.conf_threshold} "
                   f"iou={st.detector.iou_threshold} roi={st.detector.roi}")


def _job_sweep(job: Job, split: str, limit: int, iou: float, model_path: str,
               thresholds: list[float], min_precision: float,
               min_recall: float) -> None:
    """部署链路阈值扫描（cv2.dnn + Detector）跑在留出集上。"""
    st = _state()
    # ★ 必须显式设 stage：start_job 只把它初始化成 "starting"，作业体不设的话整个
    #   扫描过程界面都会显示 "starting"，看起来像卡住了（进度条在动、文字说没动）。
    job.stage = "collecting"
    job.detail.update({"split": split, "limit": limit, "iou": iou,
                       "model": model_path or "active"})

    def _on_collect(i: int, total: int) -> None:
        # 取图/前向是耗时大头（每张一次 cv2.dnn forward），必须单独报进度
        job.detail["collect_index"] = i
        job.detail["collect_total"] = total

    def _on_step(idx: int, total: int, row: dict[str, Any]) -> None:
        job.stage = "sweeping"
        job.detail["threshold_index"] = idx
        job.detail["thresholds_total"] = total
        job.log.append(f"conf {row['conf']:.2f}：P={row['precision']:.3f} "
                       f"R={row['recall']:.3f} F1={row['f1']:.3f}"
                       f"（tp={row['tp']} fp={row['fp']} fn={row['fn']}）")

    try:
        result = run_deployment_sweep(
            st.cfg, split=split, limit=limit, iou=iou,
            model_path=model_path or None,
            thresholds=tuple(thresholds) or None,
            min_precision=min_precision, min_recall=min_recall,
            progress_cb=_on_step, collect_cb=_on_collect,
        )
    except DetectorError as exc:
        # 刻意硬失败：扫描测的就是部署链路，退化成 mock/ultralytics 产出的表
        # 「看起来是部署结论、其实是另一条路」。
        raise RuntimeError(f"{exc}") from exc
    st.last_sweep = result
    job.result = result
    cur = result["current"]
    job.log.append(f"判定：{result['verdict']}（当前 conf={result['current_conf']:.2f} "
                   f"P={cur['precision']:.3f} R={cur['recall']:.3f}）")


# ======================================================================
# 请求模型
# ======================================================================
class JogBody(BaseModel):
    servo_id: int = Field(ge=0, le=32)
    position: int = Field(ge=0, le=1000)
    duration_ms: int = Field(default=600, ge=50, le=10000)


class GripperBody(BaseModel):
    action: str


class PlaceBody(BaseModel):
    """放置点（臂座旁边的料槽）的三种用法 —— 三者互斥，按 clear > coord > pose 处理。

    * ``coord=[x, y]``：直接给工作平面坐标（m，标定坐标系）；
    * ``pose="here"``：把**当前舵机姿态**记为放置位姿（示教：先手动点动到位再点保存）；
    * ``clear=True``：清掉放置点（清掉后抓取闭环会拒绝开工）。
    """

    coord: list[float] | None = None
    pose: str | None = None
    clear: bool = False


class GraspBody(BaseModel):
    task_id: str = ""


class CaptureBody(BaseModel):
    frames: int = Field(default=200, ge=10, le=2000)
    tag: str = ""
    augment: bool = True


class LabelBody(BaseModel):
    session: str
    name: str
    boxes: list[dict[str, Any]] = Field(default_factory=list)


class TrainBody(BaseModel):
    tier: str = "local"
    #: 只用这个采集会话组装训练集；空 = 全部会话。
    #  ★ 场景特化时必须指定：否则会把历史会话（比如早期那批合成演示帧）一起
    #    拉进来稀释，训练/扫出来的阈值就不是当前场景的了。
    session: str = ""
    #: 多会话（与 session 二选一，同时非空 → 400）。现场按**组**采集是常态：
    #  每组一个会话，训练时勾若干组。要按整组切 train/val 就得走这条。
    sessions: list[str] = Field(default_factory=list)
    #: 按**采集单元**（会话目录）整组切 train/val，而不是逐帧洗牌。
    #  ★ 现场画面是视频流：逐帧洗牌会把同一袋同一位置的相邻帧同时送进 train 与
    #    val —— 那是泄漏，指标会虚高。合成帧相互独立，勾不勾都行。
    group_splits: bool = False
    #: replay 混采：把这些 data.yaml 的图像一起混进训练（如 TACO 真实照片底料）。
    #  ★ 只在合成域上微调会把底料学到的真实域特征**灾难性遗忘**掉（实测 TACO val
    #    0.397 → 0.014）；混采（replay）是保住它的标准做法。
    mix_dataset_yaml: list[str] = Field(default_factory=list)
    #: 训练后立刻评估用的门禁（0 = 不卡）。门禁**不阻塞产物落盘**，只写进结果与日志；
    #  真正拦人的位置是部署那一步。
    min_map50: float = Field(default=0.0, ge=0.0, le=1.0)
    min_precision: float = Field(default=0.0, ge=0.0, le=1.0)


class DeployBody(BaseModel):
    note: str = ""
    min_map50: float = Field(default=0.5, ge=0.0, le=1.0)
    #: 精确率门禁。默认 0.0（不卡）—— 机械臂场景误抓代价高，但「默认卡」会改变
    #   既有部署行为与测试口径；想要就显式填，不做静默决策。
    min_precision: float = Field(default=0.0, ge=0.0, le=1.0)


class EvalBody(BaseModel):
    """评估门禁参数。默认值与 /api/evaluate 原先硬编码的 0.5 一致（行为不变）。"""

    min_map50: float = Field(default=0.5, ge=0.0, le=1.0)
    min_precision: float = Field(default=0.0, ge=0.0, le=1.0)


class BackgroundBody(BaseModel):
    """整组背景负样本标记。``background=False`` = 撤销标记。"""

    session: str
    note: str = ""
    background: bool = True


class SweepBody(BaseModel):
    """部署链路（cv2.dnn）阈值扫描的参数。"""

    model_path: str = ""        # 空 = 用 models/active 里那份
    split: str = "val"
    limit: int = Field(default=0, ge=0)
    iou: float = Field(default=0.5, gt=0.0, le=1.0)
    thresholds: list[float] = Field(default_factory=list)     # 空 = 用默认 11 档
    min_precision: float = Field(default=0.90, ge=0.0, le=1.0)
    min_recall: float = Field(default=0.80, ge=0.0, le=1.0)


class RollbackBody(BaseModel):
    version: str = ""


class CalibrateBody(BaseModel):
    grid_size: int = Field(default=3, ge=2, le=5)
    span: float = Field(default=0.10, gt=0.0, le=0.4)


class MoveBody(BaseModel):
    """手动位置控制：给工作平面坐标 + 俯仰角，由厂商 IK 现算脉宽。

    两种用法：
      * 绝对：直接给 x/y/z（``delta`` 语义由调用方决定，见 /api/move_to）
      * 增量：给 ``dx``/``dy``/``dz``/``dpitch``，叠加在**当前记录的目标位姿**上
        （方向键走这条），不必每次重填绝对坐标。

    坐标口径与标定一致：原点 = 臂座安装面在地面的投影，单位 m；
    ``pitch`` 区间 [-180, 0]，-180 = 竖直朝下，-65 = 待命位。
    """

    x: float | None = None
    y: float | None = None
    z: float | None = None
    pitch: float | None = None
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    dpitch: float = 0.0
    duration_ms: int = Field(default=800, ge=50, le=10000)
    #: 目标不可达时是否报错。方向键连按希望「悄悄停住」而不是弹一堆错误
    strict: bool = False


class PresetBody(BaseModel):
    """姿态预设增删。``pose`` 为空 = 用当前舵机回读值存为预设。"""

    name: str = ""
    pose: dict[str, int] | None = None
    delete: bool = False
    #: 设/清「开机默认姿态」：板子每次启动会下发它
    boot_default: bool = False


class BootDefaultBody(BaseModel):
    """设置开机默认姿态。``name`` 为空 = 清掉。"""

    name: str = ""


class WristBody(BaseModel):
    """最前端关节（腕部旋转）单关节控制。

    为什么单开一个接口而不是复用 /api/jog：腕部是「对准方向」最常用的一个关节，
    需要**行程硬约束**（防止甩到台面）。/api/jog 是给调试用的自由点动，不设限。
    """

    position: int = Field(ge=0, le=1000)
    duration_ms: int = Field(default=600, ge=50, le=10000)


class JointBody(BaseModel):
    """骨架关节（id3~id6）单关节控制，按 J3~J6 选择。

    行程在服务端**硬约束**到实测可靠内段，理由见 ``JOINT_ENVELOPE`` 的注释。
    """

    joint: str = "J3"
    position: int = Field(ge=0, le=1000)
    duration_ms: int = Field(default=600, ge=50, le=10000)


class PlaneZBody(BaseModel):
    """水面/台面高度（绝对 z，m；原点 = 臂座安装面）。

    标定只解出像素↔(x, y)，**z 是独立的一件事**：船上甲板高于水面，水面 z 是
    负值（甲板高出水面 8cm → -0.08）。填错这一步，爪子要么压不到袋子、要么撞下去。
    """

    plane_z: float = Field(ge=-0.5, le=0.5)


class TuningBody(BaseModel):
    """场景微调调参（写 models/active/tuning.json；缺省字段保持原值）。"""

    conf_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    iou_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    #: 归一化 ROI ``[x, y, w, h]``；只保留中心落在其中的检出
    roi: list[float] | None = None
    clear_roi: bool = False


# ======================================================================
# 画面叠加
# ======================================================================
def _no_signal_frame(w: int = 640, h: int = 480) -> Any:
    """无相机信号时的占位帧。

    ★ 为什么必须有：``/stream`` 是 multipart 流，若一个字节都不产出，
      ``<img src="/stream">`` 会**永远处于加载中** —— 浏览器的 load 事件
      因此永不触发，整个页面卡在 loading（现场 ROS 没起来时就是这样）。
      先送一帧占位，既解除挂起，又给现场一句明确的排障提示。
      文字用固定浅色画在深色底上（不随主题反转）。
    """
    import cv2
    import numpy as np

    img = np.full((h, w, 3), 28, dtype=np.uint8)
    for x in range(-h, w, 44):
        cv2.line(img, (x, 0), (x + h, h), (44, 44, 44), 2)
    cv2.putText(img, "NO CAMERA SIGNAL", (96, h // 2 - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (240, 240, 240), 2, cv2.LINE_AA)
    cv2.putText(img, "check ROS / usb_cam on the Pi", (128, h // 2 + 34),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, (150, 190, 210), 1, cv2.LINE_AA)
    return img


def _draw(frame: Any, dets: list[Any], target_class: str) -> Any:
    """在画面上画检测框：目标类高亮、其他类（浮标）灰色忽略框。"""
    import cv2

    for det in dets:
        is_target = det.cls == target_class
        color = (0, 200, 120) if is_target else (150, 150, 150)
        x1, y1, x2, y2 = (int(det.x1), int(det.y1), int(det.x2), int(det.y2))
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        # ★ 叠加文字一律用 ASCII：cv2 的 Hershey 字体不带中文字形，写中文只会
        #   渲染成一串方块（实测「忽略」→「????」）。类别名本身即英文，故写 ignored。
        tag = f"{det.cls} {det.conf:.2f}" + ("" if is_target else " ignored")
        # 固定浅色文字 + 深色底衬：画面是深色水面，用固定色保证可读
        cv2.putText(frame, tag, (x1 + 2, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        if is_target:
            gu, gv = det.grasp_point
            cv2.drawMarker(frame, (int(gu), int(gv)), color, cv2.MARKER_CROSS, 16, 2)
    return frame


# ======================================================================
# 应用
# ======================================================================
def create_app(cfg: dict[str, Any], mock: bool) -> FastAPI:
    global STATE
    STATE = AppState(cfg, mock)
    app_state = STATE

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        # 新版 FastAPI 已移除 add_event_handler，改用 lifespan 托管启停。
        # 行为与旧写法一致：启动起相机/推理/MQTT，退出时逐项收尸。
        app_state.start()
        try:
            yield
        finally:
            app_state.stop()

    app = FastAPI(title="Oceanus 机械臂训练作业台", version="0.1.0", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        page = HERE / "static" / "index.html"
        if not page.exists():
            raise HTTPException(status_code=500, detail="index.html 缺失")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        st = _state()
        target = st.detector.target_class

        async def gen() -> Any:
            import cv2

            placeholder_sent = False
            while True:
                frame = st.io.latest_frame()
                if frame is None:
                    # 无相机：先送一帧占位解除 <img> 的加载挂起，之后每 0.3s 重试
                    if not placeholder_sent:
                        ok, buf = cv2.imencode(
                            ".jpg", _no_signal_frame(),
                            [int(cv2.IMWRITE_JPEG_QUALITY), 72],
                        )
                        if ok:
                            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                   + buf.tobytes() + b"\r\n")
                            placeholder_sent = True
                    await asyncio.sleep(0.3)
                    continue
                placeholder_sent = False
                frame = _draw(frame, st.latest_dets(), target)
                ok, buf = cv2.imencode(".jpg", frame,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), 72])
                if ok:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + buf.tobytes() + b"\r\n")
                await asyncio.sleep(0.06)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    # ---------------- 状态 ----------------
    @app.get("/api/status")
    def status() -> dict[str, Any]:
        st = _state()
        return {
            "mock": st.mock,
            # ROS 未通时非空：页面据此显示排障提示（服务本身照常可用）
            "ros_error": st.io.ros_error,
            "detector": st.detector.info(),
            "calibration": {
                "ready": bool(st.cal.ready),
                "demo": bool(st.cal_is_demo),
                "points": len(st.cal.pairs),
                "rmse": st.cal.rmse,
                "plane_z": st.cal.plane_z,
            },
            # 放置点（料槽）：抓取闭环的硬前置，"放哪儿"要和"抓哪儿"一样可见
            "place": st.loop.place_info(),
            "servos": st.io.servo_telemetry(),
            "detections": [d.to_dict() for d in st.latest_dets()],
            "dets_age": round(time.time() - st.dets_ts, 2) if st.dets_ts else None,
            "phase": st.loop.phase,
            "job": st.job.to_dict(),
            "task": st.listener.context,
            # 手动控制的目标位姿（None = 基准失效，需重新给绝对坐标）。
            # ★ 这是**下发目标**，不是测量位姿 —— 界面必须如实标注，
            #   不能让操作员以为它是回读值（回读只有 6 个舵机脉宽，见 servos）。
            "manual_target": (None if not st.manual_target
                              else {k: round(float(v), 4)
                                    for k, v in st.manual_target.items()}),
            "presets": st.presets.describe(),
            "tiers": sorted(TIERS),
            "training_available": training_available(),
            "frames": len(st.dataset.list_frames()),
            "labeled": len(st.dataset.labeled_frames()),
            "current_model": st.store.current(),
        }

    @app.get("/api/task")
    def task_context() -> dict[str, Any]:
        return _state().listener.context

    @app.get("/api/telemetry")
    def telemetry() -> dict[str, Any]:
        return _state().io.servo_telemetry()

    # ---------------- 手动点动 ----------------
    @app.post("/api/jog")
    def jog(body: JogBody) -> dict[str, Any]:
        st = _state()
        try:
            st.io.set_servo(body.servo_id, body.position, body.duration_ms)
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "servo_id": body.servo_id, "position": body.position}

    @app.post("/api/home")
    def home() -> dict[str, Any]:
        st = _state()
        try:
            ok = st.io.home()
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": bool(ok)}

    @app.post("/api/wrist")
    def wrist(body: WristBody) -> dict[str, Any]:
        """最前端关节（腕部旋转）—— 决定夹爪朝哪边。

        行程被**硬约束**在 300~700：这是在真机上实测过的安全区
        （小步走过 380~680，跟随误差 ±2）。越界会让夹爪带着相机一起甩，
        可能撞到自身或台面，所以宁可拒绝也不放开。

        注意：本接口**不**改动笛卡尔目标基准 —— 腕转不改变末端位置，
        只是绕臂轴转向，所以方向键的位置控制仍然有效。
        """
        st = _state()
        servo_id = int(getattr(st.io, "wrist_servo", 2))
        if not WRIST_MIN <= body.position <= WRIST_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"腕部脉宽须在 {WRIST_MIN}~{WRIST_MAX}（实测安全区），"
                       f"当前 {body.position}；中位 {WRIST_CENTER}",
            )
        try:
            st.io.set_servo(servo_id, body.position, body.duration_ms)
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "servo_id": servo_id, "position": body.position,
                "range": [WRIST_MIN, WRIST_MAX], "center": WRIST_CENTER}

    @app.post("/api/joint")
    def joint(body: JointBody) -> dict[str, Any]:
        """骨架关节 id3~id6 单关节控制（J3~J6）。

        ★ 与 /api/wrist 的差别：这四个是**逆运动学耦合关节**，单独动一个会改变
        夹爪在空间中的位置（腕转只转向、不移动末端）。所以：
          * 行程硬约束到 ``JOINT_ENVELOPE`` 的实测可靠内段；
          * 动完之后**笛卡尔目标基准作废**（末端位置已经不等于记录值了）。
            不清掉的话，方向键会从一个错误位置继续增量，越走越偏。
        """
        st = _state()
        spec = JOINT_ENVELOPE.get((body.joint or "").upper())
        if spec is None:
            raise HTTPException(
                status_code=400,
                detail=f"joint 必须是 {'/'.join(JOINT_ENVELOPE)} 之一，收到 {body.joint!r}",
            )
        lo, hi = int(spec["min"]), int(spec["max"])
        if not lo <= body.position <= hi:
            raise HTTPException(
                status_code=400,
                detail=(f"{body.joint}（id{spec['servo_id']}，{spec['label']}）脉宽须在 "
                        f"{lo}~{hi}；实测可用 {spec['measured'][0]}~{spec['measured'][1]}，"
                        f"界面用的是「舵机跟得准」的内段"),
            )
        try:
            st.io.set_servo(int(spec["servo_id"]), body.position, body.duration_ms)
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 耦合关节动过 → 末端位置不再等于记录值，基准作废
        with st._lock:
            st.manual_target = None
        return {"ok": True, "joint": body.joint.upper(),
                "servo_id": int(spec["servo_id"]), "position": body.position,
                "range": [lo, hi], "label": spec["label"],
                "target_known": False}

    @app.get("/api/joints")
    def joints() -> dict[str, Any]:
        """各骨架关节的行程口径 —— 界面据此渲染滑块与按钮，避免两边各写一份数字。"""
        return {
            "ok": True,
            "joints": [
                {"joint": key, "servo_id": int(v["servo_id"]),
                 "min": int(v["min"]), "max": int(v["max"]),
                 "label": v["label"], "measured": list(v["measured"])}
                for key, v in JOINT_ENVELOPE.items()
            ],
        }

    @app.post("/api/gripper")
    def gripper(body: GripperBody) -> dict[str, Any]:
        st = _state()
        try:
            if body.action == "open":
                st.io.gripper_open()
                return {"ok": True, "action": "open"}
            if body.action == "close":
                pulse = st.io.gripper_close_adaptive()
                return {"ok": True, "action": "close", "pulse": pulse}
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail="action 必须是 open / close")

    # ---------------- 手动位置控制（给坐标，IK 现算） ----------------
    @app.post("/api/move_to")
    def move_to(body: MoveBody) -> dict[str, Any]:
        """按工作平面坐标 + 俯仰角移动（厂商 IK 现算，无预设动作）。

        为什么要有这个接口：直接点动 4 个耦合关节（id3~id6）很难摆出想要的
        姿态，而「去 (x, y, z)、爪子朝下多少度」才是操作员脑子里想的事。
        """
        st = _state()
        with st._lock:
            cur = dict(st.manual_target) if st.manual_target else None

        # 基准位姿未知（刚做过软停止 / 切换过预设）时，**只接受绝对坐标**。
        # 为什么不退回待命位当基准：那会悄悄把机械臂从当前位置拽到一个
        # 操作员没预期的点，属于"好心办坏事"。宁可明确拒绝。
        if cur is None:
            if None in (body.x, body.y, body.z):
                raise HTTPException(
                    status_code=409,
                    detail="当前位置基准已失效（做过软停止或切换过预设）——"
                           "请先填完整的 x / y / z，或点「回待命位」恢复基准",
                )
            if body.pitch is None:
                raise HTTPException(
                    status_code=409,
                    detail="当前位置基准已失效 —— 请同时指定俯仰角（pitch）",
                )
            cur = {"x": float(body.x), "y": float(body.y), "z": float(body.z),
                   "pitch": float(body.pitch)}

        # 绝对与增量可以混用：先取基准（给了绝对值就用绝对值，否则用记录值），
        # 再叠加增量。这样方向键（纯增量）和输入框（纯绝对）走同一个接口。
        target = {
            "x": float(body.x) if body.x is not None else cur["x"],
            "y": float(body.y) if body.y is not None else cur["y"],
            "z": float(body.z) if body.z is not None else cur["z"],
            "pitch": float(body.pitch) if body.pitch is not None else cur["pitch"],
        }
        target["x"] += float(body.dx)
        target["y"] += float(body.dy)
        target["z"] += float(body.dz)
        target["pitch"] += float(body.dpitch)

        # 俯仰角硬约束到厂商区间 [-180, 0]：越界不会报错，IK 会静默解出一个
        # 完全不同的姿态 —— 那比直接夹住更危险。
        if not -180.0 <= target["pitch"] <= 0.0:
            raise HTTPException(
                status_code=400,
                detail=f"俯仰角须在 [-180, 0]，当前 {target['pitch']:.1f}"
                       f"（-180 竖直朝下，-65 待命位）",
            )
        # 工作半径上限：与 config 的 agent.work_radius 同口径，防止把臂伸到极限
        radius = (target["x"] ** 2 + target["y"] ** 2) ** 0.5
        limit = float(((st.cfg.get("agent") or {}).get("work_radius", 0.20)))
        if radius > limit:
            raise HTTPException(
                status_code=400,
                detail=f"目标水平半径 {radius:.3f}m 超出工作半径上限 {limit:.3f}m"
                       f"（config: agent.work_radius）",
            )

        try:
            reachable = st.io.move_to(
                target["x"], target["y"], target["z"],
                pitch=target["pitch"], duration_ms=body.duration_ms,
            )
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if not reachable:
            # 不可达：**不更新**记录位姿（否则方向键连按会把目标越推越远，
            # 之后所有动作都失败，而操作员只看到「没反应」）
            if body.strict:
                raise HTTPException(
                    status_code=409,
                    detail=f"目标不可达：({target['x']:.3f}, {target['y']:.3f}, "
                           f"{target['z']:.3f}) pitch={target['pitch']:.0f}",
                )
            return {"ok": True, "reachable": False, "target": cur,
                    "requested": {k: round(v, 4) for k, v in target.items()}}

        with st._lock:
            st.manual_target = target
        return {"ok": True, "reachable": True,
                "target": {k: round(v, 4) for k, v in target.items()}}

    @app.post("/api/stop")
    def stop_motion() -> dict[str, Any]:
        """软停止：中止动作类作业 + 把舵机锁在当前回读位置。

        ★ 口径必须说清楚：厂商的 ROS 消息集里**只有** ``MultiRawIdPosDur``
        （见 hiwonder_servo_msgs），**没有急停/卸力消息**，所以这里做不出
        「断开舵机力矩」那种物理急停。本接口做的是：
          1) 中止正在跑的作业 —— 采集（帧间查 ``job.cancel``）与抓取（走
             ``GraspLoop.request_stop()``）能立刻退；**训练与扫描在长跑步骤里
             无法就地中断**，只是被标记，跑到哪算哪，别按「停下来了」理解；
          2) 读回 6 个舵机的**当前位置**，再以当前位置为目标重发一遍，
             让机械臂停在原地而不是继续走完剩余行程。
        真急停只能靠断电或拍下电源。
        """
        st = _state()
        stopped_job = False
        if st.job.running:
            st.job.cancel = True
            stopped_job = True
        # 让正在跑的闭环尽快退出当前相位
        try:
            st.loop.request_stop()
        except Exception:                                    # noqa: BLE001
            pass

        held: dict[str, int] = {}
        try:
            tel = st.io.servo_telemetry() or {}
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        pairs: list[tuple[int, int]] = []
        for sid, row in tel.items():
            pos = row.get("position")
            if pos is None:
                continue
            pairs.append((int(sid), int(pos)))
            held[str(sid)] = int(pos)

        if pairs:
            try:
                # 时长短一点，尽量"就地刹住"；太短会触发舵机丢步，400ms 是折中
                st.io.set_servos(400, pairs)
            except ArmIoError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        # 舵机被原地锁住后，之前记录的笛卡尔目标已不再等于真实位姿，
        # 把记录值清空（None），避免方向键从一个错误基准继续增量。
        # ★ 不能用 nan：它不是合法 JSON（json.dumps 会写出 NaN，前端
        #   JSON.parse 直接报错），会让整个 /api/status 挂掉。
        with st._lock:
            st.manual_target = None
        logger.info("软停止：中止作业=%s，锁位舵机 %s", stopped_job, sorted(held))
        return {"ok": True, "stopped_job": stopped_job, "held": held,
                "target_known": False,
                "note": "已锁在当前位置；笛卡尔目标基准已失效，"
                        "请用「回待命位」或重新输入坐标"}

    # ---------------- 姿态预设 ----------------
    @app.get("/api/presets")
    def presets_list() -> dict[str, Any]:
        return _state().presets.describe()

    @app.post("/api/presets")
    def presets_put(body: PresetBody) -> dict[str, Any]:
        """存/删预设。``pose`` 留空 = 把当前舵机回读位置存为该预设。"""
        st = _state()
        try:
            if body.delete:
                result = st.presets.delete(body.name)
                return {"ok": True, "deleted": True, **result,
                        **st.presets.describe()}
            pose = body.pose
            if not pose:
                tel = st.io.servo_telemetry() or {}
                pose = {str(k): int(v["position"]) for k, v in tel.items()
                        if v.get("position") is not None}
                if not pose:
                    raise PresetError("拿不到舵机回读，无法把当前姿态存为预设")
            result = st.presets.put(body.name, pose)
            return {"ok": True, "deleted": False, **result,
                    **st.presets.describe()}
        except PresetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/presets/apply")
    def presets_apply(body: PresetBody) -> dict[str, Any]:
        """切到某个预设姿态：按名字取脉宽，一次性下发。"""
        st = _state()
        try:
            name = normalize_name(body.name)
        except PresetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        presets = st.presets.load()
        if name not in presets:
            raise HTTPException(status_code=404, detail=f"没有这个预设：{name}")
        pose = presets[name]
        pairs = [(int(sid), int(pulse)) for sid, pulse in pose.items()]
        try:
            st.io.set_servos(1000, pairs)
        except ArmIoError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 预设是「舵机脉宽」，不能反推笛卡尔坐标 —— 把目标基准清空，
        # 免得方向键基于一个错误的位置继续走（宁可要求重填，也不要乱动）
        with st._lock:
            st.manual_target = None
        return {"ok": True, "name": name, "pose": pose, "target_known": False}

    @app.post("/api/presets/boot")
    def presets_boot(body: BootDefaultBody) -> dict[str, Any]:
        """设 / 清「开机默认姿态」。

        板子的 ``oceanus-board-agent.service`` 在启动后会读 ``presets.json`` 的
        ``boot_default`` 并把该姿态下发一次，这样每次开机机械臂都停到同一位置。
        ``name`` 为空 = 清掉（下次开机不再自动摆位）。
        """
        st = _state()
        try:
            if not (body.name or "").strip():
                result = st.presets.clear_boot_default()
                return {"ok": True, "cleared": True, **result, **st.presets.describe()}
            result = st.presets.set_boot_default(body.name)
            return {"ok": True, "cleared": False, **result, **st.presets.describe()}
        except PresetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---------------- 放置点（旁边的料槽） ----------------
    @app.post("/api/place")
    def place_set(body: PlaceBody) -> dict[str, Any]:
        """示教 / 手填 / 清除料槽放置点。写进 calibration.json（与设备绑定）。"""
        st = _state()
        if body.clear:
            st.cal.place = None
            saved = _persist_calibration(st)
            return {"ok": True, "place": describe_place(None), "persisted": saved}

        if body.coord is not None:
            if len(body.coord) < 2:
                raise HTTPException(status_code=400, detail="coord 需要 [x, y] 两个数（m）")
            place = {"coord": [float(body.coord[0]), float(body.coord[1])],
                     "source": "console"}
        elif body.pose == "here":
            pose = _current_pose(st)
            if not pose:
                raise HTTPException(
                    status_code=400,
                    detail="读不到舵机位置，无法记录当前姿态：确认真机 ROS 已接入"
                           "（mock 模式下读到的是最近一次下发值）",
                )
            place = {"pose": pose, "source": "taught"}
        else:
            raise HTTPException(status_code=400, detail="需要 coord / pose='here' / clear 之一")

        place["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        st.cal.place = place
        saved = _persist_calibration(st)
        return {"ok": True, "place": describe_place(place), "persisted": saved}

    # ---------------- 抓取作业 ----------------
    @app.post("/api/grasp/start")
    def grasp_start(body: GraspBody) -> dict[str, Any]:
        st = _state()
        # 没有放置点就别开工：夹起来了没地方放，比不抓更糟（同闭环内的前置判断）
        if not st.loop.place_info()["ready"]:
            raise HTTPException(
                status_code=400,
                detail="未配置料槽放置点：先在「放置点（料槽）」示教一次"
                       "（手动点动到槽口上方 → 「用当前姿态作放置点」），"
                       "或填 config.yaml 的 agent.place.coord",
            )
        try:
            st.start_job("grasp", _job_grasp, body.task_id or st.listener.context.get("task_id", ""))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "job": st.job.to_dict()}

    @app.post("/api/grasp/stop")
    def grasp_stop() -> dict[str, Any]:
        st = _state()
        st.loop.request_stop()
        return {"ok": True}

    # ---------------- 采集 ----------------
    @app.post("/api/capture/start")
    def capture_start(body: CaptureBody) -> dict[str, Any]:
        st = _state()
        try:
            st.start_job("capture", _job_capture, body.frames, body.tag, body.augment)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "job": st.job.to_dict()}

    @app.get("/api/sessions")
    def sessions() -> dict[str, Any]:
        """所有采集会话及其可用样本数。

        ★ 为什么单独一个端点、而不是塞进 1 Hz 轮询的 /api/status：
        ``describe_sessions`` 会逐帧读标签文件（几百次小文件读），挂进状态轮询等于
        每秒扫一遍磁盘。**也不是继续从 /api/frames 反推**：那里只回最近 60 帧，
        早于 60 帧的会话在界面上根本不存在（等于采完的旧组没法标注、没法进训练）。
        """
        st = _state()
        items = st.dataset.describe_sessions()
        return {"count": len(items), "sessions": items}

    @app.post("/api/background")
    def set_background(body: BackgroundBody) -> dict[str, Any]:
        """整组标/撤「背景负样本」。撤不掉的话，一个标错的会话会把每一帧都当成
        「这里什么都没有」喂给训练 —— 所以这里两条路都必须通。"""
        st = _state()
        name = (body.session or "").strip()
        # ★ 必须拒绝不存在的会话：mark_background 内部是 mkdir(parents=True,
        #   exist_ok=True)，手打错名字会**凭空建出一个空会话目录**。
        if not name or not (st.dataset.raw_dir / name).is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"没有这个采集会话：{name or '（空）'}（先确认会话名，别手打错）",
            )
        if body.background:
            marker = st.dataset.mark_background(name, note=body.note)
        else:
            st.dataset.unmark_background(name)
            marker = None
        item = next((s for s in st.dataset.describe_sessions()
                     if s["session"] == name), {})
        return {"ok": True, "session": name, "background": bool(body.background),
                "marker": str(marker) if marker else None, **item}

    @app.get("/api/frames")
    def frames(session: str = "", limit: int = 60) -> dict[str, Any]:
        """帧列表。``limit=0`` = 不截断。

        ★ 默认 60 是给「全部会话」总览用的（300+ 帧全铺到页面会把右栏撑成几千像素）；
        但**按会话查时必须传 0** —— 否则早于最近 60 帧的会话在标注台里一帧都看不见，
        等于那个会话根本没法标注。响应回带 ``returned``/``limit``，把截断这事显性化。
        """
        st = _state()
        items: list[dict[str, Any]] = []
        for frame in st.dataset.list_frames(session or None):
            rel = frame.relative_to(st.dataset.raw_dir)
            parts = rel.parts
            sess = parts[0] if len(parts) > 1 else session
            boxes = st.dataset.read_label(frame)
            items.append({
                "session": sess,
                "name": frame.name,
                "url": f"/api/frames/{sess}/{frame.name}",
                "labels": [{"cls": b.cls, "cx": b.cx, "cy": b.cy, "w": b.w, "h": b.h}
                           for b in boxes],
            })
        shown = items if limit <= 0 else items[-limit:]
        return {"count": len(items), "returned": len(shown), "limit": limit,
                "frames": shown}

    @app.get("/api/frames/{session}/{name}")
    def frame_image(session: str, name: str) -> Any:
        st = _state()
        path = (st.dataset.raw_dir / session / name).resolve()
        if not str(path).startswith(str(st.dataset.raw_dir.resolve())) or not path.exists():
            raise HTTPException(status_code=404, detail="帧不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/frames/prelabel")
    def prelabel(body: LabelBody) -> dict[str, Any]:
        """用当前模型给一帧预标注，交给页面画框修正。"""
        st = _state()
        frame_path = st.dataset.raw_dir / body.session / body.name
        if not frame_path.exists():
            raise HTTPException(status_code=404, detail="帧不存在")
        import cv2

        img = cv2.imread(str(frame_path))
        if img is None:
            raise HTTPException(status_code=400, detail="帧不可读")
        h, w = img.shape[:2]
        boxes: list[dict[str, Any]] = []
        for det in st.detector.infer(img):
            boxes.append({
                "cls": det.cls,
                "cx": ((det.x1 + det.x2) / 2) / w,
                "cy": ((det.y1 + det.y2) / 2) / h,
                "w": (det.x2 - det.x1) / w,
                "h": (det.y2 - det.y1) / h,
                "conf": round(det.conf, 3),
            })
        return {"boxes": boxes, "width": w, "height": h}

    @app.post("/api/label")
    def save_label(body: LabelBody) -> dict[str, Any]:
        st = _state()
        frame_path = st.dataset.raw_dir / body.session / body.name
        if not frame_path.exists():
            raise HTTPException(status_code=404, detail="帧不存在")
        try:
            boxes = [Box(str(b["cls"]), float(b["cx"]), float(b["cy"]),
                         float(b["w"]), float(b["h"]),
                         float(b.get("conf", 1.0))) for b in body.boxes]
            path = st.dataset.write_label(frame_path, boxes)
        except (DatasetError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"标注非法：{exc}") from exc
        return {"ok": True, "path": str(path), "count": len(boxes)}

    # ---------------- 标定 ----------------
    @app.post("/api/calibrate/start")
    def calibrate_start(body: CalibrateBody) -> dict[str, Any]:
        st = _state()
        half = body.span
        n = body.grid_size
        grid: list[tuple[float, float]] = []
        for iy in range(n):
            for ix in range(n):
                grid.append((
                    -half + 2 * half * (ix / max(1, n - 1)),
                    -half + 2 * half * (iy / max(1, n - 1)),
                ))
        try:
            st.start_job("calibrate", _job_calibrate, grid)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "grid": grid, "job": st.job.to_dict()}

    @app.post("/api/calibrate/save")
    def calibrate_save() -> dict[str, Any]:
        st = _state()
        if not st.cal.ready:
            raise HTTPException(status_code=400, detail="标定未就绪，无法保存")
        # ★ mock 模式下的 st.cal 是演示映射（demo_calibration），ready 只是为了
        #   让离线联调不被前置检查挡住。把它落盘会留下一个「看起来合法」的
        #   calibration.json（RMSE 正好 0、3 个假点对），而真机侧 precheck /
        #   supervisor 只看 ready，就会当成真标定用 —— 这正是"假装成功"。
        if st.cal_is_demo:
            raise HTTPException(
                status_code=409,
                detail="当前是 mock 演示标定（非真机标定），拒绝落盘："
                       "请上真机、跑完 9 点标定后再保存",
            )
        path = save_calibration(st.cal)
        return {"ok": True, "path": str(path)}

    @app.post("/api/calibrate/plane_z")
    def calibrate_plane_z(body: PlaneZBody) -> dict[str, Any]:
        """设置水面/台面高度（z）。标定只管 (x, y)，z 是独立的一件事。

        船上甲板高于水面，这个值是**负数**；不填准，爪子要么压不到袋子、
        要么直接撞下去。写入 calibration.json（演示标定不落盘，同其它接口）。
        """
        st = _state()
        st.cal.plane_z = float(body.plane_z)
        st.loop.cal.plane_z = float(body.plane_z)
        persisted = _persist_calibration(st)
        return {"ok": True, "plane_z": st.cal.plane_z, "persisted": persisted}

    # ---------------- 场景微调（tuning.json） ----------------
    @app.get("/api/tuning")
    def get_tuning() -> dict[str, Any]:
        st = _state()
        det = st.detector
        path = det.model_dir / "tuning.json"
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
        return {
            "path": str(path),
            "exists": path.exists(),
            # 这三个是「当前生效值」（可能来自 tuning.json，也可能来自 config.yaml）
            "conf_threshold": det.conf_threshold,
            "iou_threshold": det.iou_threshold,
            "roi": list(det.roi) if det.roi else None,
            "sweep": raw.get("sweep"),
            "chosen_by": raw.get("chosen_by"),
        }

    @app.post("/api/tuning")
    def set_tuning(body: TuningBody) -> dict[str, Any]:
        """手动写 tuning.json 并立刻生效（固定演示场景的 ROI / 阈值微调）。"""
        st = _state()
        det = st.detector
        path = det.model_dir / "tuning.json"
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
        if body.conf_threshold is not None:
            raw["conf_threshold"] = float(body.conf_threshold)
        if body.iou_threshold is not None:
            raw["iou_threshold"] = float(body.iou_threshold)
        if body.clear_roi:
            raw.pop("roi", None)
        elif body.roi is not None:
            roi = [float(x) for x in body.roi]
            if len(roi) != 4:
                raise HTTPException(
                    status_code=400, detail="roi 必须是 4 个数：x y w h（归一化 0~1）")
            if not all(0.0 <= v <= 1.0 for v in roi):
                raise HTTPException(status_code=400, detail="roi 的每个值都要落在 0~1")
            raw["roi"] = roi
        raw["chosen_by"] = "manual_console"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        det.load()          # 立刻生效：load() 会重读 tuning.json
        return {"ok": True, "path": str(path),
                "conf_threshold": det.conf_threshold,
                "iou_threshold": det.iou_threshold,
                "roi": list(det.roi) if det.roi else None}

    # ---------------- 训练与部署 ----------------
    @app.post("/api/train")
    def train_start(body: TrainBody) -> dict[str, Any]:
        st = _state()
        if body.tier not in TIERS:
            raise HTTPException(status_code=400, detail=f"未知档位，可选 {sorted(TIERS)}")
        if not training_available() and body.tier != "calibrate":
            raise HTTPException(
                status_code=503,
                detail="训练档需要 ultralytics；请改选 calibrate 档或用局域网协同档",
            )
        # ★ 以下校验全部在**起作业之前**做完：训练要跑十几分钟，让它在十分钟后才
        #   因为参数不对失败，代价太大。
        if body.session and body.sessions:
            raise HTTPException(
                status_code=400,
                detail="session 与 sessions 只能二选一（单会话 / 多会话）",
            )
        sessions = [s for s in body.sessions if s] or ([body.session] if body.session else [])
        # ★ 只在**显式给了会话清单**时校验条数：清单为空 = 「全部会话」，磁盘上有几个
        #   采集单元此刻并不知道（要扫盘才知道）—— 一律按 "<2" 拒绝会把「全部 + 整组
        #   切分」这个完全合法的组合挡掉，提示还文不对题（人明明选的是全部）。
        #   全部会话这条路由组装阶段判定：dataset._pick_val_units 在单元不足时报的
        #   那条错信息才是对的（它会报出实际有几个单元）。
        if body.group_splits and sessions and len(sessions) < 2:
            raise HTTPException(
                status_code=400,
                detail="整组切分至少要 2 个采集会话（每个会话是一个采集单元）；"
                       "只有一个会话请改用默认的逐帧切分",
            )
        for p in body.mix_dataset_yaml:
            mix_path = Path(p)
            if mix_path.suffix.lower() not in (".yaml", ".yml") or not mix_path.exists():
                raise HTTPException(
                    status_code=400,
                    detail=f"混采底料的 data.yaml 不存在或后缀不对：{p}",
                )
        try:
            st.start_job("train", _job_train, body.tier, sessions, body.group_splits,
                         body.mix_dataset_yaml, body.min_map50, body.min_precision)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "tier": body.tier, "sessions": sessions,
                "group_splits": body.group_splits,
                "mix": list(body.mix_dataset_yaml), "job": st.job.to_dict()}

    @app.get("/api/train/status")
    def train_status() -> dict[str, Any]:
        return _state().job.to_dict()

    @app.post("/api/evaluate")
    def do_evaluate(body: EvalBody | None = None) -> dict[str, Any]:
        """用 ultralytics 口径评估 active 模型并过门禁。

        ★ 注意口径：这里走的是 **ultralytics 自带推理**；现场真正跑的是 cv2.dnn
        那条路（见 /api/sweep），两者前后处理不同、P/R 会有差别。别拿这里的数字
        当部署结论。``body`` 允许为空（空请求体仍要 200，前端现写法不许改）。
        """
        st = _state()
        gate_map = 0.5 if body is None else body.min_map50
        gate_prec = 0.0 if body is None else body.min_precision
        try:
            metrics = evaluate(st.cfg)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        ok, reason = passes(metrics, gate_map, 0.0, gate_prec)
        return {"ok": ok, "reason": reason, "metrics": metrics,
                "gate": {"min_map50": gate_map, "min_precision": gate_prec}}

    @app.post("/api/sweep")
    def sweep_start(body: SweepBody) -> dict[str, Any]:
        """起一次「部署链路扫描」（cv2.dnn + Detector）。

        ★ 为什么走后台作业而不是同步请求：11 档阈值 × 整个留出集的 cv2.dnn 前向，
        是分钟级的活，不能挂在 HTTP 请求上（与 train/calibrate 同一口径）。
        ★ 为什么与 /api/evaluate 并存而不是合并：两条推理链不是一回事，必须都看得见
        且分别标名。
        """
        st = _state()
        # 候选与 Detector.load() 对齐（best.onnx → best.ncnn → best.pt）：
        # ★ 只认 best.onnx 会把「部署的是 ncnn」的机器挡在门外 —— 而扫描支持 ncnn
        #   （由 load 选后端）。.pt 也放行：真正的拒绝理由（后端不是 cv2.dnn/ncnn）
        #   由 load_deployment_detector 给出，比这里的粗筛准确得多。
        model_dir = st.detector.model_dir
        if not body.model_path and not any((model_dir / n).exists()
                                           for n in ("best.onnx", "best.ncnn.param", "best.pt")):
            raise HTTPException(
                status_code=409,
                detail="没有可扫的模型：请先在「4 部署与回滚」部署一份模型（best.onnx），"
                       "或填「模型路径」指向某个训练产物",
            )
        try:
            st.start_job("sweep", _job_sweep, body.split, body.limit, body.iou,
                         body.model_path, body.thresholds, body.min_precision,
                         body.min_recall)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # ★ 清旧表必须在**起作业成功之后**：反过来的话，一个被 409（已有作业在跑）
        #   拒掉的请求会顺手把上一次扫描的证据擦掉 —— 什么都没跑，证据却没了。
        st.last_sweep = None
        return {"ok": True, "job": st.job.to_dict()}

    @app.get("/api/sweep")
    def sweep_last() -> dict[str, Any]:
        """只回最近一次扫描结果（页面刷新后仍看得见）。不落盘：重扫即可。"""
        st = _state()
        return {"exists": st.last_sweep is not None, "last": st.last_sweep}

    @app.post("/api/deploy")
    def do_deploy(body: DeployBody) -> dict[str, Any]:
        st = _state()
        model_dir = st.detector.model_dir
        candidates = [model_dir / n for n in ("best.onnx", "best.ncnn.param", "best.pt")]
        model_path = next((p for p in candidates if p.exists()), None)
        if model_path is None:
            raise HTTPException(
                status_code=409,
                detail="active 目录没有可部署的模型。请先训练（产物在 runs/ 下，"
                       "用 /api/deploy-from 指定路径，或把模型拷进 active 后重试）",
            )
        # 部署门禁：先用 active 里的模型算指标
        metrics: dict[str, Any] = {}
        try:
            metrics = evaluate(st.cfg, model_path=model_path)
            ok, reason = passes(metrics, body.min_map50, 0.0, body.min_precision)
        except Exception as exc:  # noqa: BLE001
            logger.warning("部署前评估失败，跳过门禁：%s", exc)
            ok, reason = True, f"评估不可用（{exc}），跳过门禁"
        if not ok:
            raise HTTPException(status_code=409, detail=f"未通过部署门禁：{reason}")
        try:
            meta = st.store.deploy(model_path, metrics=metrics, note=body.note)
        except DeployError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        st.detector.load()
        return {"ok": True, "gate": reason, "version": meta}

    @app.post("/api/deploy-from")
    def deploy_from(body: dict[str, Any]) -> dict[str, Any]:
        """把训练产物（runs/.../best.onnx）登记为新版本并切换 active。"""
        st = _state()
        path = str(body.get("model_path", "")).strip()
        if not path:
            raise HTTPException(status_code=400, detail="缺少 model_path")
        src = Path(path)
        if not src.exists():
            raise HTTPException(status_code=404, detail=f"模型不存在: {src}")
        metrics: dict[str, Any] = {}
        min_map50 = float(body.get("min_map50", 0.5))
        min_precision = float(body.get("min_precision", 0.0))
        try:
            metrics = evaluate(st.cfg, model_path=src)
            ok, reason = passes(metrics, min_map50, 0.0, min_precision)
        except Exception as exc:  # noqa: BLE001
            ok, reason = True, f"评估不可用（{exc}），跳过门禁"
        if not ok:
            raise HTTPException(status_code=409, detail=f"未通过部署门禁：{reason}")
        try:
            meta = st.store.deploy(src, metrics=metrics, note=str(body.get("note", "")))
        except DeployError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        st.detector.load()
        return {"ok": True, "gate": reason, "version": meta}

    @app.get("/api/versions")
    def versions() -> dict[str, Any]:
        st = _state()
        return {"current": st.store.current(), "versions": st.store.versions()}

    @app.post("/api/rollback")
    def rollback(body: RollbackBody) -> dict[str, Any]:
        st = _state()
        try:
            meta = st.store.activate(body.version) if body.version else st.store.rollback()
        except DeployError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        st.detector.load()
        return {"ok": True, "version": meta}

    return app


# ======================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Oceanus 机械臂训练作业台")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 edge/grasp/config.yaml）")
    ap.add_argument("--mock", action="store_true", help="不接 ROS，用合成画面跑通 UI")
    ap.add_argument("--camera", type=int, default=None,
                    help="用第 N 号 USB 摄像头作为实时画面帧源（开发机/无 ROS 联调；"
                         "真机请留空，走 arm.camera_topic 的 ROS 相机话题）")
    ap.add_argument("--remote", default=None,
                    help="把执行端指向树莓派板上代理，格式 HOST 或 HOST:PORT"
                         "（端口默认取 config 的 arm.remote.port）；等价于把 "
                         "config.yaml 的 arm.remote.enabled 置 true。此时本机不需要 ROS。")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    # --camera 覆盖配置里的 arm.usb_camera。真机部署时配置与命令行都留空，
    # 行为与改动前完全一致（走 ROS 相机话题）。
    if args.camera is not None:
        cfg.setdefault("arm", {})["usb_camera"] = int(args.camera)
    # --remote 覆盖 arm.remote：命令行给了就以命令行为准（host/port 就地写进 cfg）
    if args.remote:
        host, _, port = args.remote.partition(":")
        remote = cfg.setdefault("arm", {}).setdefault("remote", {})
        remote["enabled"] = True
        remote["host"] = host or remote.get("host")
        if port:
            remote["port"] = int(port)
    mock = args.mock or bool((cfg.get("web") or {}).get("mock"))
    host = args.host or str((cfg.get("web") or {}).get("host", "0.0.0.0"))
    port = args.port or int((cfg.get("web") or {}).get("port", 8090))

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("需要 fastapi + uvicorn：pip install -r edge/grasp/requirements.txt") from exc

    app = create_app(cfg, mock)
    logger.info("作业台启动：http://%s:%s （mock=%s）", host, port, mock)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
