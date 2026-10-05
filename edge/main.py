#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实边缘感知程序：取流 → 检测 → 时序校验 → MQTT 上报。

与 `simulator/` 的区别
---------------------
`simulator/simulator.py` **自己造假检测框**（用于没有摄像头时驱动平台联调）；
本程序跑**真实检测**：从 RTSP / 视频文件 / 摄像头取帧，交给检测器
做检测，再走同一套时序校验与上报节流。

检测通道（`detector.backend`，默认 `cv`）
----------------------------------------
- `cv`    —— `detector.CvDetector`，OpenCV 传统视觉，零数据依赖，**主通道**；
- `world` —— `detector.world_detector.WorldDetector`，开放词汇零样本通道，
  用于演示与对照（现场可加词），**默认不进主告警链路**。

两个通道的**输出契约完全一致**，所以时序校验、上报节流、MQTT 报文的代码一行都不用改；
但它们的**置信度尺度不同**，时序门限必须各用各的（见 `_effective_min_confidence`），
否则 world 通道的检测会在时序环节被静默丢掉。

两者产出的 MQTT 报文**结构完全一致**，平台侧无法区分。

用法
----
    # 1) 无摄像头自检：用内置合成海面跑 200 帧，只打印不联网
    python main.py --source synthetic --dry-run --max-frames 200

    # 2) 视频文件（录好的码头片段）
    python main.py --source file --file samples/shore.mp4 --dry-run

    # 3) 真实 RTSP 摄像头，上报到平台
    python main.py --source rtsp

    # 4) 调试时把检测框画出来看
    python main.py --source synthetic --show

    # 5) 开放词汇通道 + 现场加词（演示用）
    python main.py --source synthetic --backend world --show \
        --world-prompt "foam buoy" --world-prompt "plastic bag"
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "detector"))
sys.path.insert(0, str(HERE / "simulator"))

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    print("[错误] 缺少依赖：pip install opencv-python-headless numpy")
    raise SystemExit(1)

from detector import CvDetector  # noqa: E402
from simulator import OutboxBuffer, TemporalValidator  # noqa: E402

DEFAULT_CONFIG = HERE / "config.yaml"
STATE_FILE = HERE / ".edge_state.json"
TOPIC_PREFIX = "marine"


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("[错误] 缺少 PyYAML：pip install pyyaml")
        raise SystemExit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] 状态写入失败：{exc}")


# ----------------------------------------------------------------------
# 取流
# ----------------------------------------------------------------------
class SyntheticSea:
    """合成海面帧序列 —— 没有摄像头时也能端到端自检。

    为什么要有它：真实视频依赖出海采集，而"这条链路通不通"
    （检测 → 时序 → 报文 → 上报）不应该等到那时才验证。
    它同时也是现场演示的兜底：摄像头坏了也能把流程演完。
    """

    def __init__(self, seed: int = 42, size: tuple[int, int] = (360, 640)) -> None:
        self.rng = np.random.default_rng(seed)
        self.size = size
        self.index = 0
        # 三块"漂浮泡沫"：缓慢横向漂移 + 上下浮动（0.1~0.5 m/s 的量级）
        self.blobs = [
            {"x": 120.0, "y": 150.0, "r": 16, "vx": 1.2, "vy": 0.2},
            {"x": 400.0, "y": 240.0, "r": 13, "vx": -0.9, "vy": 0.15},
            {"x": 560.0, "y": 110.0, "r": 18, "vx": 0.6, "vy": -0.1},
        ]

    def __iter__(self) -> Iterator[np.ndarray]:
        return self

    def __next__(self) -> np.ndarray:
        h, w = self.size
        frame = np.zeros((h, w, 3), np.uint8)
        frame[:] = (110, 100, 82)
        noise = self.rng.normal(0, 4, frame.shape)
        frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)

        for b in self.blobs:
            b["x"] += b["vx"]
            b["y"] += b["vy"]
            if b["x"] > w - 30 or b["x"] < 30:
                b["vx"] *= -1
            cv2.circle(
                frame,
                (int(b["x"]), int(b["y"])),
                b["r"],
                (232, 235, 238),
                -1,
            )

        # 每 40 帧来一次"水花"（瞬态噪声，不该被确认成事件）
        if self.index % 40 == 7:
            x = int(self.rng.integers(60, w - 60))
            y = int(self.rng.integers(60, h - 60))
            cv2.circle(frame, (x, y), 10, (250, 250, 250), -1)

        self.index += 1
        return frame


def open_source(cfg: dict[str, Any], args: argparse.Namespace):
    """按配置/参数打开视频源。"""
    src = args.source or cfg.get("source", {}).get("type", "file")
    if src == "synthetic":
        return SyntheticSea(seed=args.seed)
    if src == "camera":
        return cv2.VideoCapture(int(cfg.get("source", {}).get("camera", 0)))
    if src == "rtsp":
        return cv2.VideoCapture(args.rtsp or cfg["source"]["rtsp"])
    path = args.file or cfg.get("source", {}).get("file")
    if not path:
        print("[错误] 未指定视频文件路径（--file 或 source.file）")
        raise SystemExit(1)
    return cv2.VideoCapture(str(HERE / path if not Path(path).is_absolute() else path))


def next_frame(source) -> np.ndarray | None:
    """统一取帧接口（生成器 / VideoCapture 都支持）。"""
    if isinstance(source, SyntheticSea):
        return next(source, None)
    ok, frame = source.read()
    return frame if ok else None


# ----------------------------------------------------------------------
# 边缘程序主体
# ----------------------------------------------------------------------
class EdgeRuntime:
    def __init__(self, cfg: dict[str, Any], args: argparse.Namespace) -> None:
        self.cfg = cfg
        self.args = args
        self.detector = self._build_detector(cfg, args)
        self.backend_name = str(getattr(self.detector, "backend_name", "cv"))

        t = cfg.get("temporal", {})
        # ★ 时序门限按通道取，两个通道各用各的（原因见 _effective_min_confidence）
        self.min_confidence = self._effective_min_confidence(cfg)
        self.validator = TemporalValidator(
            window_frames=int(t.get("window_frames", 15)),
            min_hits=int(t.get("min_hits", 3)),
            grid_size=int(t.get("grid_size", 64)),
            min_confidence=self.min_confidence,
            match_distance=float(t.get("match_distance", 60.0)),
            max_misses=int(t.get("max_misses", 5)),
            min_iou=float(t.get("min_iou", 0.25)),
            decimation=int(t.get("decimation", 4)),
        ) if t.get("enabled", True) else None

        rep = cfg.get("reporting", {})
        # 演示时用 --cooldown 3 把 90 秒冷却压到 3 秒，几秒就能看到连续告警；
        # 真实边缘盒保持 90 秒（同一片垃圾不该被反复派单）。
        self.cooldown = float(
            args.cooldown if args.cooldown is not None
            else rep.get("cooldown_seconds", 90)
        )
        self.min_interval = float(
            args.min_interval if args.min_interval is not None
            else rep.get("min_interval_seconds", 2)
        )
        self.max_detections = int(rep.get("max_detections", 20))

        buf = cfg.get("buffer", {})
        self.buffer = OutboxBuffer(max_size=int(buf.get("max_size", 1000)))

        self.state = _load_state()
        self.seq = int(self.state.get("seq", 0))
        self._last_report: dict[tuple, float] = {}
        self._last_any = 0.0
        self.counters = {"frames": 0, "raw": 0, "confirmed": 0, "published": 0,
                         "buffered": 0, "throttled": 0}
        self._client = None

    # ---------------- MQTT ----------------
    def connect(self) -> None:
        if self.args.dry_run:
            print("[DRY-RUN] 不连接 Broker，报文只打印")
            return
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        m = self.cfg.get("mqtt", {})
        dev = self.cfg.get("device", {})
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"edge-{dev.get('device_id', 'unknown')}",
        )
        client.username_pw_set(m.get("username"), m.get("password"))
        if m.get("use_lwt", True):
            client.will_set(self._topic("status"), json.dumps(
                {"device_id": dev.get("device_id"), "online": False, "ts": _now_iso()}
            ), qos=1, retain=True)
        client.connect(m.get("host", "localhost"), int(m.get("port", 1883)),
                       int(m.get("keepalive", 60)))
        client.loop_start()
        self._client = client
        self._publish_status(True)

    def _topic(self, kind: str) -> str:
        dev = self.cfg.get("device", {})
        return (f"{TOPIC_PREFIX}/{self.cfg.get('site_id', 'lianjiang')}"
                f"/{dev.get('device_id')}/{kind}")

    def _publish_status(self, online: bool) -> None:
        dev = self.cfg.get("device", {})
        self._publish(self._topic("status"), {
            "device_id": dev.get("device_id"),
            "online": online,
            "ts": _now_iso(),
        }, qos=1)

    def _publish(self, topic: str, payload: dict[str, Any], qos: int) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        if self.args.dry_run or self._client is None:
            if self.args.dry_run:
                agg = payload.get("aggregate", {})
                print(f"[DRY-RUN] {topic}  seq={payload['seq']}  "
                      f"class={agg.get('main_class')} "
                      f"conf={agg.get('max_confidence')} bbox={payload['detections'][0]['bbox']}")
                self.counters["published"] += 1
                return
            self.counters["buffered"] += 1
            return
        try:
            result = self._client.publish(topic, body, qos=qos)
            if getattr(result, "rc", 0) != 0:
                self.buffer.put(topic, payload, qos)
                self.counters["buffered"] += 1
            else:
                self.counters["published"] += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 发布失败：{exc}（已缓存）")
            self.buffer.put(topic, payload, qos)
            self.counters["buffered"] += 1

    # ---------------- 主循环 ----------------
    def build_event(self, det: dict[str, Any]) -> dict[str, Any]:
        """构造与 simulator 完全一致的事件报文。"""
        self.seq += 1
        dev = self.cfg.get("device", {})
        cls = det["class"]
        return {
            "event_id": f"evt_{dev.get('device_id')}_{self.seq:06d}",
            "device_id": dev.get("device_id"),
            "device_type": dev.get("device_type", "shore_camera"),
            "timestamp": _now_iso(),
            "location": {"lng": dev.get("lng"), "lat": dev.get("lat")},
            "detections": [det],
            "aggregate": {
                "main_class": cls,
                "count": 1,
                "max_confidence": det["confidence"],
            },
            "evidence_url": None,
            "model_version": "cv-opencv-v0.1.0",
            "seq": self.seq,
        }

    def _cooldown_ok(self, det: dict[str, Any]) -> bool:
        now = time.time()
        if now - self._last_any < self.min_interval:
            return False
        cell = (self.validator._cell(det["bbox"]) if self.validator else (0, 0))
        key = (det["class"], cell)
        if now - self._last_report.get(key, 0.0) < self.cooldown:
            return False
        self._last_report[key] = now
        self._last_any = now
        return True

    # ------------------------------------------------------------------
    # 检测通道选择
    # ------------------------------------------------------------------
    @staticmethod
    def _build_detector(cfg: dict[str, Any], args: argparse.Namespace) -> Any:
        """按 `detector.backend` 实例化检测器（命令行 `--backend` 优先）。

        ★ 设计约束：只在这里加分支，**不碰任何检测器内部的实现**。
        `CvDetector` 的代码路径（主通道、线上只读挂载、后端 3 处 import）
        必须保持一次都没被改过 —— 这样本改动对现网是零影响。
        """
        detector_cfg = cfg.get("detector") or {}
        backend = str(
            getattr(args, "backend", None) or detector_cfg.get("backend") or "cv"
        ).strip().lower()

        if backend == "cv":
            return CvDetector(config=detector_cfg, roi=cfg.get("roi") or [])

        if backend != "world":
            raise SystemExit(f"[配置错误] 未知的检测通道 {backend!r}（可选：cv / world）")

        # 延迟导入：cv 通道的代码路径不因 world 通道的存在而多一个导入点
        from world_detector import WorldDetector  # noqa: PLC0415

        world_cfg = dict(detector_cfg.get("world") or {})
        extra_prompts = [str(p) for p in (getattr(args, "world_prompt", None) or [])]
        if extra_prompts:
            world_cfg["prompt_classes"] = list(
                world_cfg.get("prompt_classes") or []
            ) + extra_prompts
        return WorldDetector(config=world_cfg, roi=cfg.get("roi") or [])

    def _effective_min_confidence(self, cfg: dict[str, Any]) -> float:
        """当前通道应当使用的时序门限。

        ★ 这是接入开放词汇通道时最容易埋雷的一处：
        `temporal.min_confidence` 的 0.45 是给 cv 通道的**伪置信度**
        （范围 0.30~0.95）标定的；开放词汇模型的输出分数低一个量级
        （本仓库历史实测最高 0.184）。若共用 0.45，
        world 通道的检测会在时序环节被**全部丢掉**，而且不报任何错。

        做法：谁有自己标定值就用谁的（`WorldDetector.temporal_min_confidence`），
        否则回落配置里的 temporal 门限 —— 也就是 cv 通道的现网行为。
        """
        world_gate = getattr(self.detector, "temporal_min_confidence", None)
        if world_gate is not None:
            return float(world_gate)
        return float(cfg.get("temporal", {}).get("min_confidence", 0.45))

    def run(self) -> int:
        # ★ 先确认检测通道真的能跑，再连 Broker。
        #   不可用时**必须明确失败**：本通道不可用的现象与"画面里没有垃圾"
        #   完全一样（都是零检测），静默继续会让人以为系统在正常工作。
        #
        #   两步都要问，缺一不可：
        #   1) available —— 依赖与权重在不在（便宜的存在性探测，不加载模型）；
        #   2) load()    —— 模型真的建得起来吗。
        #   只问第 1 步会漏掉"依赖齐、权重在，但模型加载失败"这一类
        #   （实测案例：torch<2.6 时 ultralytics 拒载 .pt，此时 available 为 True）。
        if not bool(getattr(self.detector, "available", True)):
            print(f"[启动失败] 检测通道 {self.backend_name} 不可用："
                  f"{getattr(self.detector, 'last_error', '未知原因')}")
            print("           这不是「画面里没有垃圾」，而是这一路根本没跑起来。")
            print("           请补齐依赖/权重，或把 detector.backend 改回 cv。")
            return 3

        loader = getattr(self.detector, "load", None)
        if callable(loader) and not loader():
            print(f"[启动失败] 检测通道 {self.backend_name} 的模型加载失败："
                  f"{getattr(self.detector, 'last_error', '未知原因')}")
            print("           依赖与权重都在，但模型本身没能建起来 —— "
                  "这同样不是「画面里没有垃圾」。")
            print("           请按上面的原因修运行时，或把 detector.backend 改回 cv。")
            return 3

        src_cfg = self.cfg.get("source", {})
        log_every = int(src_cfg.get("log_every", 100))
        source = open_source(self.cfg, self.args)
        self.connect()

        print(f"[启动] 设备={self.cfg.get('device', {}).get('device_id')} "
              f"源={self.args.source or src_cfg.get('type')} "
              f"检测器={self.backend_name} 时序门限={self.min_confidence}")
        try:
            while True:
                if self.args.max_frames and self.counters["frames"] >= self.args.max_frames:
                    break
                frame = next_frame(source)
                if frame is None:
                    break

                self.counters["frames"] += 1
                detections = self.detector.detect(frame)
                self.counters["raw"] += len(detections)

                if self.validator is None:
                    confirmed = [
                        d for d in detections
                        if d["confidence"] >= self.min_confidence
                    ]
                else:
                    confirmed = self.validator.push(detections)
                    # ★ 抽帧推理下 miss 衰减必须按被跳过的帧推进
                    self.validator.sweep(self.validator.decimation)

                for det in confirmed[: self.max_detections]:
                    if not self._cooldown_ok(det):
                        self.counters["throttled"] += 1
                        continue
                    self.counters["confirmed"] += 1
                    self._publish(self._topic("event"), self.build_event(det), qos=1)

                if self.args.show:
                    for d in detections:
                        x1, y1, x2, y2 = d["bbox"]
                        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                        cv2.putText(frame, f"{d['class']} {d['confidence']:.2f}",
                                    (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.45, (0, 255, 0), 1)
                    cv2.imshow("seasight-edge", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

                if log_every and self.counters["frames"] % log_every == 0:
                    print(f"  帧={self.counters['frames']} 原始检测={self.counters['raw']} "
                          f"确认={self.counters['confirmed']} 节流={self.counters['throttled']}")
        except KeyboardInterrupt:
            print("\n[中断] 收到 Ctrl+C")
        finally:
            if isinstance(source, cv2.VideoCapture):
                source.release()
            if self._client is not None:
                self._publish_status(False)
                self._client.loop_stop()
                self._client.disconnect()
            self.state["seq"] = self.seq
            _save_state(self.state)
            if self.args.show:
                cv2.destroyAllWindows()

        st = self.validator.stats if self.validator else {}
        suppressed = st.get("suppressed", 0)
        fed = st.get("fed", 0)
        print("\n[结束]")
        print(f"  处理帧数   : {self.counters['frames']}")
        print(f"  原始检测   : {self.counters['raw']}")
        print(f"  确认事件   : {self.counters['confirmed']}")
        print(f"  节流丢弃   : {self.counters['throttled']}")
        print(f"  已发布     : {self.counters['published']}")
        if fed:
            print(f"  误报抑制率 : {suppressed / fed:.1%}")
        # 开放词汇通道的可解释计数：把"为什么少了检测框"一次说清。
        # 没有这组数字，'提示词没归宿'与'门限太高'的现象一模一样（某类恒为 0）。
        if hasattr(self.detector, "stats"):
            ds = self.detector.stats()
            print(f"  [{self.backend_name} 通道] 调用={ds['calls']} 原始={ds['raw']} "
                  f"保留={ds['kept']} 低置信丢弃={ds['dropped_low_conf']} "
                  f"ROI丢弃={ds['dropped_roi']} 非法框={ds['dropped_bad_box']} "
                  f"回落other={ds['fallback_other']}")
            if ds.get("unmapped_prompts"):
                print(f"  [注意] 这些提示词没有归宿、会回落 other：{ds['unmapped_prompts']}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="探海灵眸边缘感知程序（真实检测：cv / world 双通道）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--source", choices=["rtsp", "file", "camera", "synthetic"],
                        help="覆盖配置里的视频源类型")
    parser.add_argument("--file", help="视频文件路径（--source file 时用）")
    parser.add_argument("--rtsp", help="RTSP 地址（--source rtsp 时用）")
    parser.add_argument("--max-frames", type=int, help="最多处理多少帧后退出（自检用）")
    parser.add_argument("--seed", type=int, default=42, help="合成海面的随机种子")
    parser.add_argument("--cooldown", type=float,
                        help="覆盖上报冷却秒数（演示时设 3~5，默认读配置 90）")
    parser.add_argument("--min-interval", type=float,
                        help="覆盖任意两次上报的最小间隔秒数（默认读配置 2）")
    parser.add_argument("--dry-run", action="store_true", help="不连 Broker，只打印报文")
    parser.add_argument("--show", action="store_true", help="弹窗显示检测框（调试用）")
    parser.add_argument("--backend", choices=["cv", "world"],
                        help="覆盖检测通道（默认读 detector.backend，现网为 cv）")
    parser.add_argument("--world-prompt", action="append", metavar="PROMPT",
                        help="给开放词汇通道追加提示词（可重复），现场加词演示用")
    args = parser.parse_args()

    cfg = _load_config(Path(args.config))
    return EdgeRuntime(cfg, args).run()


if __name__ == "__main__":
    raise SystemExit(main())
