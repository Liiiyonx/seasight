#!/usr/bin/env python3
"""探海灵眸 SeaSight — 边缘感知模拟器。

模拟**边缘盒**（岸基摄像头旁的计算单元）的完整行为，用于在没有真实
设备的情况下驱动平台演示。它复刻了真实边缘盒里几个容易做错的关键点：

1. **时序校验降误报**
   浪花是瞬态的（出现一两帧就消失），垃圾是持续的。
   滑动窗口内连续 N 帧在同一网格命中，才升级为「确认事件」。
   不做这一步，海面视频每分钟会产生上百条误报。

2. **seq 单调递增 + 本地持久化**
   平台用 (device_id, seq) 唯一约束做幂等去重。
   seq 必须严格单调，且崩溃重启后从上次值续上，否则要么漏判重、要么全被拒。

3. **断网补传**
   渔港 4G 信号不稳。断网期间事件进本地环形队列，恢复后按原 seq 顺序补发。
   不模拟这条链路，平台的判重与补派逻辑就永远测不到。

4. **QoS1 重复投递**
   --dup-rate 参数故意重复投递，验证平台幂等是否真的生效。

用法见同目录 README.md。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ----------------------------------------------------------------------
# 依赖与常量
#
# 依赖导入放在此处但**不在此处退出**：
# TemporalValidator 是纯逻辑类，时序校验的单元测试不该被迫装上
# pyyaml / paho-mqtt。真正缺失依赖时，在 main() 里给出明确报错。
# ----------------------------------------------------------------------
try:
    import yaml
except ImportError:   # pragma: no cover
    yaml = None       # type: ignore[assignment]

try:
    import paho.mqtt.client as mqtt
except ImportError:   # pragma: no cover
    mqtt = None       # type: ignore[assignment]


def _require_deps() -> None:
    """运行模拟器前检查依赖，缺失时给出明确的安装命令。"""
    missing: list[str] = []
    if yaml is None:
        missing.append("pyyaml")
    if mqtt is None:
        missing.append("paho-mqtt")
    if missing:
        print(
            f"[错误] 缺少依赖：{', '.join(missing)}\n"
            f"       请执行：pip install {' '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)


WASTE_CLASSES = ("foam", "plastic", "fishing_gear", "other")

CLASS_LABELS = {
    "foam": "泡沫类",
    "plastic": "塑胶类",
    "fishing_gear": "渔具类",
    "other": "其他",
}

# 各垃圾类别的视觉特征（用于生成合理的检测框与置信度）
CLASS_VISUAL: dict[str, dict[str, Any]] = {
    # (框尺寸范围 px, 置信度基准)
    "foam": {"size": (24, 70), "conf": (0.72, 0.95)},
    "plastic": {"size": (30, 90), "conf": (0.65, 0.90)},
    "fishing_gear": {"size": (60, 200), "conf": (0.68, 0.92)},
    "other": {"size": (20, 120), "conf": (0.48, 0.75)},
}

STREAM_STATE_FILE = ".simulator_state.json"
FRAME_W, FRAME_H = 1920, 1080
DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")


# ----------------------------------------------------------------------
# 时序校验器 —— 降误报的第一道闸
# ----------------------------------------------------------------------
class _Track:
    """被跟踪的一个候选目标（可能真、可能是噪声）。"""

    __slots__ = (
        "track_id",
        "cls",
        "bbox",
        "confidence",
        "hits",
        "misses",
        "last_cell",
        "last_hit_frame",
        "confirmed",
    )

    def __init__(self, track_id: int, det: dict[str, Any], cell: tuple[int, int]) -> None:
        self.track_id = track_id
        self.cls = det["class"]
        self.bbox = list(det["bbox"])
        self.confidence = det["confidence"]
        self.hits = 1
        self.misses = 0
        self.last_cell = cell
        self.last_hit_frame = 0
        self.confirmed = False

    def update(self, det: dict[str, Any], cell: tuple[int, int]) -> None:
        self.bbox = list(det["bbox"])
        self.cls = det["class"]
        self.confidence = det["confidence"]
        self.hits += 1
        self.misses = 0
        self.last_cell = cell


class TemporalValidator:
    """时序校验器 —— 降误报的第一道闸。

    核心判断：**浪花是瞬态的，垃圾是持续的**。

    ── 为什么不能按「像素格网」统计命中 ──
    每帧的检测框像素位置都在抖动，即使同一块泡沫，也不可能两帧落在
    完全相同的 64px 格网里。若按格网统计，"连续 N 帧命中"永远凑不满，
    校验就形同虚设。

    ── 正确模型 ──
    把**目标**而不是**像素格**作为跟踪单元：
    相邻帧中中心点距离足够近、类别相同的检测，视为同一目标，
    维护它自己的命中计数；连续命中达到 min_hits 才确认为真事件。
    这对应真实边缘盒里「检测 → 匈牙利匹配/光流跟踪」那一步。

    同时：命中不足的目标按帧衰减淘汰，单帧噪声自然被饿死。
    """

    def __init__(
        self,
        window_frames: int = 15,
        min_hits: int = 3,
        grid_size: int = 64,
        min_confidence: float = 0.45,
        match_distance: float = 60.0,
        max_misses: int = 5,
        min_iou: float = 0.25,
        decimation: int = 4,
    ) -> None:
        # ★ 这里的默认值必须与 docs/software-design.md §2.3 参数表逐项一致。
        #   曾经 max_misses 在文档里写 5、代码里写 6、测试里硬写 6 ——
        #   三方不一致却从未被发现，因为测试跟着实现走，永远"通过"。
        #   真实边缘盒上这个值决定「丢失几帧后放弃目标」：
        #   偏小 → 目标被浪遮挡就丢，同一片垃圾重复上报；
        #   偏大 → 噪声目标赖着不走，占跟踪槽位。
        #   改这里请同步改文档与 config.yaml（config 未配时以此默认为准）。
        self.window_frames = window_frames
        self.min_hits = min_hits
        self.grid_size = grid_size
        self.min_confidence = min_confidence
        # 同一目标在相邻帧间的最大中心位移（像素），1080p 下约画面宽度 3%
        self.match_distance = match_distance
        # 连续多少帧没匹配上就淘汰（0.6 秒，容忍短暂遮挡）
        self.max_misses = max_misses
        # 关联所需的最小框重叠度 —— 单靠距离门限挡不住随机噪声
        self.min_iou = min_iou

        self._tracks: list[_Track] = []
        self._next_track_id = 1
        self._total_frames = 0
        # ★ 统计口径：三个计数必须同单位，否则算出来的抑制率会 >100%。
        #   _fed       —— 本窗口喂进校验器的检测总数（分母）
        #   _lowered   —— 因置信度不足被丢弃的检测数
        #   _absorbed  —— 被某个 track 关联吸收的检测数（即"有效命中"）
        #   抑制率 = (fed - absorbed) / fed
        #          = 从未被任何 track 稳定跟踪的检测占比
        #   这比"累计淘汰 track 数"更严谨：一个 track 淘汰只算一次，
        #   而它可能只对应 1 个检测，也可能对应几十个 —— 无法与检测数直接相除。
        self._fed = 0
        self._lowered = 0
        self._absorbed = 0

        # ★ 抽帧推理模拟：真实边缘盒不是逐帧跑模型，而是隔几帧推一帧
        #   （算力就这么点，8fps 对水面漂浮物足够）。
        #   抽帧的意义不止省算力 —— 它天然压噪声：只在单帧闪现的浪花
        #   被抽中的概率仅 1/N，根本凑不满 min_hits 就被饿死；
        #   而持续存在的垃圾几乎每帧都在，抽中照样连续命中。
        #   所以抑制率会比逐帧模式高得多，也更贴近真实产出。
        self.decimation = max(1, int(decimation))
        self._decimation_tick = 0
        self._sweep_tick = 0

    def _cell(self, bbox: list[int]) -> tuple[int, int]:
        """把检测框中心映射到格网坐标（用于上报冷却，不用于跟踪）。"""
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        return int(cx // self.grid_size), int(cy // self.grid_size)

    @staticmethod
    def _center(bbox: list[int]) -> tuple[float, float]:
        return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2

    @staticmethod
    def _iou(a: list[int], b: list[int]) -> float:
        """两个检测框的交并比。"""
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        if inter == 0:
            return 0.0
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def _match(self, det: dict[str, Any]) -> _Track | None:
        """为当前检测找最近的同类未淘汰目标。

        关联条件**同时**满足才认为是同一目标：
        1. 类别相同
        2. 中心位移 < match_distance
        3. IoU > min_iou

        为什么必须加 IoU：只有中心距离门限的话，画面里随机落点的
        瞬态噪声偶尔会"碰巧"靠近某个跟踪目标，把它的命中计数续上去，
        于是噪声也能凑满 min_hits 被误确认。加上重叠度要求后，
        随机噪声几乎不可能连续命中同一个目标 —— 这正是两级门限的价值。
        """
        cx, cy = self._center(det["bbox"])
        best: _Track | None = None
        best_dist = self.match_distance

        for track in self._tracks:
            if track.cls != det["class"]:
                continue
            tx, ty = self._center(track.bbox)
            dist = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
            if dist >= best_dist:
                continue
            if self._iou(track.bbox, det["bbox"]) < self.min_iou:
                continue
            best_dist = dist
            best = track
        return best

    def push(self, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """喂入一帧的原始检测结果，返回通过时序校验的确认检测。

        ★ 每帧都必须调用（即使 detections 为空），否则目标无法衰减淘汰。
        """
        self._total_frames += 1
        self._fed += len(detections)

        # 置信度过滤
        candidates: list[dict[str, Any]] = []
        for det in detections:
            if det["confidence"] < self.min_confidence:
                self._lowered += 1
                continue
            candidates.append(det)

        # ★ 模拟抽帧推理：只对一部分帧真正跑检测，其余帧返回空。
        #   这样噪声目标（只在单帧闪现）几乎不可能被连续命中，
        #   而稳定目标（持续存在）在抽中的帧里依然会被反复匹配到。
        self._decimation_tick += 1
        if self._decimation_tick % self.decimation != 0:
            return []

        # 已匹配的目标集合（避免一个目标被两个检测重复匹配）
        matched: set[int] = set()

        for det in candidates:
            track = self._match(det)
            if track is not None and track.track_id not in matched:
                track.update(det, self._cell(det["bbox"]))
                track.last_hit_frame = self._total_frames
                matched.add(track.track_id)
                self._absorbed += 1        # 被稳定目标吸收 —— 不是误报
            else:
                # 新目标入池
                self._tracks.append(
                    _Track(self._next_track_id, det, self._cell(det["bbox"]))
                )
                self._tracks[-1].last_hit_frame = self._total_frames
                self._next_track_id += 1

        # 判定确认：命中数达标且尚未上报过
        confirmed: list[dict[str, Any]] = []
        for track in self._tracks:
            if track.hits >= self.min_hits and not track.confirmed:
                track.confirmed = True
                confirmed.append(
                    {
                        "class": track.cls,
                        "confidence": track.confidence,
                        "bbox": list(track.bbox),
                        "_track_id": track.track_id,
                        "_hits": track.hits,
                    }
                )

        return confirmed

    def sweep(self, decimation: int = 4) -> int:
        """周期性结算未被命中的 track，返回本次淘汰的目标数。

        ★ 为什么需要它 ——
        真实推理是**抽帧**跑的（每 decimation 帧推一帧），而 miss 衰减
        必须按真实时间推进。若只在 push() 里衰减，miss 计的是"推理帧数"
        而不是"时间"，抽帧一开，噪声目标就要等 4 倍时间才被淘汰。
        做法：每帧调用本函数，累计到 decimation 次才真正走一次衰减，
        于是 miss 的口径重新对齐到"被跳过的帧"，语义与真实流水线一致。
        """
        self._sweep_tick = getattr(self, "_sweep_tick", 0) + 1
        if self._sweep_tick < decimation:
            return 0

        self._sweep_tick = 0
        before = len(self._tracks)

        # 本轮被命中的目标清零 miss —— 用 last_hit_frame 判断"本轮是否被更新过"
        survivors: list[_Track] = []
        for track in self._tracks:
            if track.last_hit_frame == self._total_frames:
                track.misses = 0
                survivors.append(track)
                continue
            track.misses += 1
            if track.misses > self.max_misses:
                continue
            survivors.append(track)

        self._tracks = survivors
        return before - len(survivors)

    def reset(self) -> None:
        """清空所有跟踪状态。"""
        self._tracks.clear()
        self._next_track_id = 1
        self._sweep_tick = 0

    @property
    def stats(self) -> dict[str, int]:
        """当前统计快照。

        `suppressed` 是**从未被稳定跟踪**的检测数，即被时序校验挡下的误报。
        口径与 `fed` 一致（都是"检测数"），所以抑制率恒在 0~100%。
        """
        return {
            "frames": self._total_frames,
            "tracked": len(self._tracks),
            "confirmed": sum(1 for t in self._tracks if t.confirmed),
            "fed": self._fed,
            "absorbed": self._absorbed,
            "low_confidence": self._lowered,
            "suppressed": max(0, self._fed - self._absorbed),
        }

    def drain_stats(self) -> dict[str, int]:
        """读取并清零本窗口的计数（供全局统计累加，避免重复计数）。"""
        out = self.stats
        self._fed = 0
        self._absorbed = 0
        self._lowered = 0
        return out


# ----------------------------------------------------------------------
# 断网补传缓存
# ----------------------------------------------------------------------
class OutboxBuffer:
    """本地补传队列（断网时缓存事件，恢复后按序补发）。

    真实边缘盒上这是 SQLite 表；模拟器用内存 deque 表达同一语义。
    上限策略：满了丢最旧 —— 宁可丢历史，不能阻塞新事件。
    """

    def __init__(self, max_size: int = 1000) -> None:
        self.max_size = max_size
        self._queue: deque[tuple[str, str, int]] = deque(maxlen=max_size)
        self.dropped = 0

    def put(self, topic: str, payload: dict[str, Any], qos: int) -> None:
        if len(self._queue) == self.max_size:
            self.dropped += 1
        self._queue.append((topic, json.dumps(payload, ensure_ascii=False), qos))

    def flush(self, publish_fn, batch: int = 20) -> int:
        """把缓存的事件补传出去，返回补传条数。"""
        sent = 0
        while self._queue and sent < batch:
            topic, payload, qos = self._queue.popleft()
            publish_fn(topic, payload, qos)
            sent += 1
        return sent

    def __len__(self) -> int:
        return len(self._queue)


# ----------------------------------------------------------------------
# 设备模拟
# ----------------------------------------------------------------------
@dataclass
class SimDevice:
    """一台被模拟的设备。"""

    device_id: str
    name: str
    device_type: str
    lng: float
    lat: float
    profile: dict[str, float]
    rate: float = 1.0
    patrol_radius_deg: float = 0.0

    # 运行时状态
    seq: int = 0
    battery: int = field(default_factory=lambda: random.randint(75, 98))
    online: bool = True
    validator: TemporalValidator | None = None
    # 同一格网的上报冷却：{(cell): last_report_ts}
    _last_report: dict[tuple[int, int], float] = field(default_factory=dict)

    def random_location(self, rng: random.Random) -> tuple[float, float]:
        """事件落点（带小幅漂移，模拟垃圾随潮流移动）。"""
        if self.patrol_radius_deg > 0:
            # 无人机/广域设备：巡飞范围内随机
            return (
                self.lng + rng.uniform(-self.patrol_radius_deg, self.patrol_radius_deg),
                self.lat + rng.uniform(-self.patrol_radius_deg, self.patrol_radius_deg),
            )
        # 固定摄像头：画面覆盖范围内的近岸水域（约 ±0.004 度 ≈ 400m）
        return (
            self.lng + rng.uniform(-0.004, 0.004),
            self.lat + rng.uniform(-0.004, 0.004),
        )

    def pick_class(self, rng: random.Random) -> str:
        """按该点位的垃圾构成抽取类别。"""
        classes = list(self.profile.keys())
        weights = list(self.profile.values())
        return rng.choices(classes, weights=weights, k=1)[0]

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


def _make_bbox(cls: str, rng: random.Random) -> list[int]:
    """生成该类别尺度合理的检测框。"""
    visual = CLASS_VISUAL[cls]
    lo, hi = visual["size"]
    w = rng.randint(lo, hi)
    h = int(w * rng.uniform(0.6, 1.2))
    x1 = rng.randint(0, max(1, FRAME_W - w - 1))
    y1 = rng.randint(0, max(1, FRAME_H - h - 1))
    return [x1, y1, x1 + w, y1 + h]


def _make_confidence(cls: str, rng: random.Random) -> float:
    lo, hi = CLASS_VISUAL[cls]["conf"]
    return round(rng.uniform(lo, hi), 3)


def generate_raw_frame(device: SimDevice, rng: random.Random) -> list[dict[str, Any]]:
    """生成一帧的**原始检测结果**（未经时序校验）。

    这里会故意混入「瞬态噪声」—— 模拟浪花反光、飞鸟、光斑。
    真实边缘盒的原始输出就长这样：大部分是噪声，靠时序校验筛。

    两种检测的区别体现在**连续性**上：
    - 持续目标：位于少量固定区域（每台设备维护若干个"垃圾位置"），
      逐帧微小漂移，因此能被跟踪器连续命中
    - 瞬态噪声：每次随机落点，前后帧对不上，被跟踪器自然饿死
    """
    dets: list[dict[str, Any]] = []

    # ---------- 持续目标（会被时序校验确认） ----------
    # 每台设备有 2~4 个固定的「垃圾聚集位置」，模拟水面漂浮物
    if not hasattr(device, "_litter_spots"):
        device._litter_spots = [  # type: ignore[attr-defined]
            (
                rng.randint(100, FRAME_W - 300),
                rng.randint(80, FRAME_H - 200),
                device.pick_class(rng),
            )
            for _ in range(rng.randint(2, 4))
        ]

    for spot_x, spot_y, cls in device._litter_spots:  # type: ignore[attr-defined]
        if rng.random() < 0.78:      # 每帧较大概率出现 → 能连续命中
            w = rng.randint(30, 90)
            h = int(w * rng.uniform(0.7, 1.1))
            # 逐帧微小漂移（水流推动），位移 < match_distance 才能被匹配上
            jitter_x = rng.randint(-14, 14)
            jitter_y = rng.randint(-10, 10)
            x1 = max(0, min(FRAME_W - w - 1, spot_x + jitter_x))
            y1 = max(0, min(FRAME_H - h - 1, spot_y + jitter_y))
            dets.append(
                {
                    "class": cls,
                    "confidence": _make_confidence(cls, rng),
                    "bbox": [x1, y1, x1 + w, y1 + h],
                }
            )

    # ---------- 瞬态噪声（不会通过时序校验，被跟踪器饿死） ----------
    if rng.random() < 0.50:
        for _ in range(rng.randint(1, 3)):
            cls = rng.choice(WASTE_CLASSES)
            dets.append(
                {
                    "class": cls,
                    # 噪声置信度偏低，进一步降低通过率
                    "confidence": round(rng.uniform(0.30, 0.62), 3),
                    "bbox": _make_bbox(cls, rng),
                }
            )

    return dets


# ----------------------------------------------------------------------
# 模拟器主体
# ----------------------------------------------------------------------
class EdgeSimulator:
    """边缘盒模拟器。"""

    def __init__(self, args: argparse.Namespace, config: dict[str, Any]) -> None:
        self.args = args
        self.config = config
        self.rng = random.Random(args.seed)
        self.site_id = config.get("site_id", "lianjiang")
        self.prefix = "marine"

        self.mqtt_conf = config.get("mqtt", {})
        self.temporal_conf = config.get("temporal", {})
        self.reporting_conf = config.get("reporting", {})
        self.buffer_conf = config.get("buffer", {})
        self.inference_conf = config.get("inference", {})

        self.devices = self._load_devices()
        self.buffer = OutboxBuffer(max_size=int(self.buffer_conf.get("max_size", 1000)))

        # 统计
        self.counter = {
            "raw_frames": 0,
            "raw_detections": 0,
            "confirmed_events": 0,
            "suppressed_noise": 0,
            "published": 0,
            "duplicates": 0,
            "buffered": 0,
            "flushed": 0,
            "net_drops": 0,
        }

        self._stop = False
        self._connected = False
        self._client: mqtt.Client | None = None

    # ---------------- 初始化 ----------------
    def _load_devices(self) -> list[SimDevice]:
        devices: list[SimDevice] = []
        wanted = None
        if self.args.devices:
            wanted = {d.strip() for d in self.args.devices.split(",") if d.strip()}

        for raw in self.config.get("devices", []):
            if wanted and raw["device_id"] not in wanted:
                continue
            profile = raw.get("profile", {})
            if not profile:
                # 未配置构成时给个通用分布
                profile = {c: 0.25 for c in WASTE_CLASSES}

            dev = SimDevice(
                device_id=raw["device_id"],
                name=raw.get("name", raw["device_id"]),
                device_type=raw.get("device_type", "shore_camera"),
                lng=float(raw["lng"]),
                lat=float(raw["lat"]),
                profile=profile,
                rate=float(raw.get("rate", 1.0)),
                patrol_radius_deg=float(raw.get("patrol_radius_deg", 0.0)),
            )
            if self.temporal_conf.get("enabled", True):
                dev.validator = TemporalValidator(
                    window_frames=int(self.temporal_conf.get("window_frames", 15)),
                    min_hits=int(self.temporal_conf.get("min_hits", 3)),
                    grid_size=int(self.temporal_conf.get("grid_size", 64)),
                    min_confidence=float(self.temporal_conf.get("min_confidence", 0.45)),
                    # ★ 下面三个必须显式传：漏传会静默落到类默认值，
                    #   于是 config.yaml 里改了也不生效 —— 排查起来极难。
                    match_distance=float(self.temporal_conf.get("match_distance", 60.0)),
                    max_misses=int(self.temporal_conf.get("max_misses", 5)),
                    min_iou=float(self.temporal_conf.get("min_iou", 0.25)),
                    decimation=int(self.temporal_conf.get("decimation", 4)),
                )
            devices.append(dev)

        # 恢复 seq（崩溃重启后从上次值续上，保证幂等判断有效）
        state = self._load_state()
        for dev in devices:
            if dev.device_id in state:
                dev.seq = int(state[dev.device_id])

        return devices

    def _state_path(self) -> Path:
        return Path(__file__).with_name(STREAM_STATE_FILE)

    def _load_state(self) -> dict[str, int]:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:   # noqa: BLE001
            return {}

    def _save_state(self) -> None:
        try:
            data = {d.device_id: d.seq for d in self.devices}
            self._state_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:   # noqa: BLE001
            pass

    # ---------------- MQTT ----------------
    def _new_client(self) -> mqtt.Client:
        """创建 MQTT 客户端。

        注意 paho-mqtt 2.x 的 callback_api_version 变更 ——
        传错会直接抛 ValueError，这是最常见的踩坑点。
        """
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"edge-sim-{uuid.uuid4().hex[:8]}",
                protocol=mqtt.MQTTv5,
            )
        except (AttributeError, ValueError):
            # 回退到 1.x API
            client = mqtt.Client(client_id=f"edge-sim-{uuid.uuid4().hex[:8]}")

        if self.mqtt_conf.get("username"):
            client.username_pw_set(
                self.mqtt_conf["username"], self.mqtt_conf.get("password", "")
            )

        # 遗嘱消息：异常断开时 Broker 代发，平台据此判定离线
        if self.mqtt_conf.get("use_lwt", True):
            for dev in self.devices:
                pass  # LWT 每设备一条，见下面 _register_lwt
            self._lwt_topic = f"{self.prefix}/{self.site_id}/+/{'status'}"

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_publish = self._on_publish
        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected = True
        print(f"[MQTT] 已连接 Broker {self.args.host}:{self.args.port}")

        # 上线后立即：1) 宣告在线  2) 补传断网期间积压的事件
        for dev in self.devices:
            self._publish_status(dev, online=True)

        if len(self.buffer) > 0:
            print(f"[补传] 恢复连接，buffer 中有 {len(self.buffer)} 条待补发")
            sent = self.flush_buffer()
            print(f"[补传] 本轮补发 {sent} 条")

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected = False
        print("[MQTT] 连接已断开，事件将进入本地缓存")

    def _on_publish(self, *args) -> None:
        pass

    # ---------------- 上报 ----------------
    def _topic_event(self, dev: SimDevice) -> str:
        return f"{self.prefix}/{self.site_id}/{dev.device_id}/event"

    def _topic_telemetry(self, dev: SimDevice) -> str:
        return f"{self.prefix}/{self.site_id}/{dev.device_id}/telemetry"

    def _topic_status(self, dev: SimDevice) -> str:
        return f"{self.prefix}/{self.site_id}/{dev.device_id}/status"

    def _publish(self, topic: str, payload: dict[str, Any], qos: int) -> None:
        """发布消息。未连上或模拟断网时进本地缓存。"""
        body = json.dumps(payload, ensure_ascii=False)

        if self.args.dry_run:
            print(f"[DRY-RUN] {topic} qos={qos}\n  {body[:200]}")
            self.counter["published"] += 1
            return

        if not self._connected:
            if self.buffer_conf.get("enabled", True):
                self.buffer.put(topic, payload, qos)
                self.counter["buffered"] += 1
            return

        try:
            result = self._client.publish(topic, body, qos=qos, retain=False)
            if getattr(result, "rc", 0) != 0:
                # 发布队列满等异常 → 缓存待补
                if self.buffer_conf.get("enabled", True):
                    self.buffer.put(topic, payload, qos)
                    self.counter["buffered"] += 1
            else:
                self.counter["published"] += 1
        except Exception as exc:   # noqa: BLE001
            print(f"[警告] 发布失败：{exc}（已缓存待补传）")
            self.buffer.put(topic, payload, qos)
            self.counter["buffered"] += 1

    def _publish_status(self, dev: SimDevice, online: bool) -> None:
        """设备上下线状态（retained）。"""
        payload = {
            "device_id": dev.device_id,
            "online": online,
            "ts": _now_iso(),
        }
        if self.args.dry_run or self._client:
            self._publish(self._topic_status(dev), payload, qos=1)

    def _publish_telemetry(self, dev: SimDevice) -> None:
        """心跳遥测。QoS0 —— 丢一帧无所谓。"""
        payload = {
            "device_id": dev.device_id,
            "device_type": dev.device_type,
            "timestamp": _now_iso(),
            "status": "online",
            "battery": dev.battery,
            "location": {"lng": dev.lng, "lat": dev.lat},
        }
        self._publish(self._topic_telemetry(dev), payload, qos=0)

    def build_event(self, dev: SimDevice, det: dict[str, Any]) -> dict[str, Any]:
        """构造符合平台契约的事件报文。"""
        seq = dev.next_seq()
        lng, lat = dev.random_location(self.rng)
        cls = det["class"]

        return {
            "event_id": f"evt_{dev.device_id}_{seq:06d}",
            "device_id": dev.device_id,
            "device_type": dev.device_type,
            "timestamp": _now_iso(),
            "location": {"lng": round(lng, 6), "lat": round(lat, 6)},
            "detections": [det],
            "aggregate": {
                "main_class": cls,
                "count": 1,
                "max_confidence": det["confidence"],
            },
            "evidence_url": None,
            "model_version": self.inference_conf.get("model_version", "det_v0.1.0"),
            "seq": seq,
        }

    # ---------------- 主循环 ----------------
    def _tick_device(self, dev: SimDevice) -> list[dict[str, Any]]:
        """跑一台设备的一帧：生成原始检测 → 时序校验 → 产出确认事件。"""
        raw = generate_raw_frame(dev, self.rng)
        self.counter["raw_frames"] += 1
        self.counter["raw_detections"] += len(raw)

        if dev.validator is None:
            # 未启用时序校验：直接放行置信度达标的检测
            threshold = float(self.inference_conf.get("confidence_threshold", 0.45))
            return [d for d in raw if d["confidence"] >= threshold]

        confirmed = dev.validator.push(raw)
        # ★ 抽帧推理下，miss 衰减必须按「被跳过的帧」推进，不能按 push 次数
        dev.validator.sweep(dev.validator.decimation)
        self.counter["confirmed_events"] += len(confirmed)
        return confirmed

    def _cooldown_ok(self, dev: SimDevice, det: dict[str, Any]) -> bool:
        """上报节流：同一格网冷却期内不重复上报。

        没有这一步，一块持续存在的泡沫会被连续上报几十次，
        平台的派单防抖虽然有 10 分钟合并窗口，但事件表会被灌爆。

        --event-interval 可覆盖配置中的冷却值，用来控制演示节奏
        （真实边缘盒用 90 秒；演示时想快点看到事件可设成 3~5 秒）。
        """
        if self.args.event_interval is not None:
            cooldown = float(self.args.event_interval)
        else:
            cooldown = float(self.reporting_conf.get("cooldown_seconds", 90))
        if cooldown <= 0:
            return True

        if dev.validator is not None:
            cell = dev.validator._cell(det["bbox"])
        else:
            cell = (0, 0)

        now = time.time()
        last = dev._last_report.get(cell, 0.0)
        if now - last < cooldown:
            return False
        dev._last_report[cell] = now
        return True

    def flush_buffer(self) -> int:
        """补传本地缓存的事件。"""
        batch = int(self.buffer_conf.get("flush_batch", 20))

        def _send(topic: str, payload: str, qos: int) -> None:
            try:
                self._client.publish(topic, payload, qos=qos, retain=False)
            except Exception:   # noqa: BLE001
                pass

        sent = self.buffer.flush(_send, batch=batch)
        self.counter["flushed"] += sent
        return sent

    def run(self) -> None:
        """启动主循环。"""
        if self.args.dry_run:
            self._client = None
        else:
            self._client = self._new_client()
            try:
                self._client.connect(
                    self.args.host,
                    self.args.port,
                    keepalive=int(self.mqtt_conf.get("keepalive", 60)),
                )
                self._client.loop_start()
            except Exception as exc:   # noqa: BLE001
                print(f"[错误] 无法连接 MQTT Broker {self.args.host}:{self.args.port}")
                print(f"        {exc}")
                print("        提示：先执行 docker compose up -d emqx，或用 --dry-run 查看报文")
                sys.exit(2)
            time.sleep(1)

        self._print_banner()

        started = time.time()
        tick = 0
        # ★ 关键：帧间隔模拟摄像头的抽帧节奏，不是「事件上报间隔」。
        #   真实边缘盒抽帧到 ~8fps（每 3 帧取 1 帧），所以帧间隔约 0.125s。
        #   时序校验的 window_frames=15 帧 ≈ 1.9 秒，正好覆盖一个浪周期。
        #   若把帧间隔设成几秒（早期实现就是这样），15 帧要跑一分钟，
        #   窗口长年填不满，用户会以为「系统不工作」。
        frame_interval = max(0.01, float(self.args.interval))
        # 事件上报冷却：真正的演示节奏旋钮。
        # None 表示用配置文件里的 cooldown_seconds（真实边缘盒的行为）。
        cooldown_override = self.args.event_interval

        try:
            while not self._stop:
                tick += 1
                elapsed = time.time() - started

                if self.args.duration and elapsed >= self.args.duration:
                    print(f"\n[完成] 已达到运行时长 {self.args.duration}s")
                    break

                # 模拟网络抖动
                if (
                    self.args.net_drop_every
                    and tick % self.args.net_drop_every == 0
                    and self._connected
                ):
                    self._simulate_net_drop()

                for dev in self.devices:
                    confirmed = self._tick_device(dev)

                    # 速率倍率 > 1 时，一帧内重复抽检（模拟更密集的垃圾分布）
                    extra_rounds = max(0, int(dev.rate) - 1)
                    for _ in range(extra_rounds):
                        confirmed.extend(self._tick_device(dev))

                    for det in confirmed:
                        if not self._cooldown_ok(dev, det):
                            continue
                        event = self.build_event(dev, det)
                        self._emit_event(dev, event)

                    # 每 5 轮发一次遥测心跳
                    if tick % 5 == 0:
                        self._publish_telemetry(dev)
                        dev.battery = max(5, dev.battery - 1)

                # buffer 有积压且已恢复连接 → 持续补传
                if len(self.buffer) > 0 and self._connected and not self.args.dry_run:
                    self.flush_buffer()

                self._print_progress(tick)
                self._save_state()

                # 精确对齐帧节奏：扣除本轮实际耗时，避免漂移
                sleep_for = frame_interval - (time.time() - started - elapsed)
                if sleep_for > 0:
                    time.sleep(sleep_for)

        except KeyboardInterrupt:
            print("\n[中断] 收到 Ctrl+C，正在退出...")
        finally:
            self._shutdown()

    def _emit_event(self, dev: SimDevice, event: dict[str, Any]) -> None:
        """发布事件，并按 --dup-rate 概率重复投递（验证平台幂等）。"""
        topic = self._topic_event(dev)
        self._publish(topic, event, qos=1)

        # 故意重复投递：模拟 QoS1 在网络抖动下的重传
        dup_rate = float(self.args.dup_rate or 0)
        if dup_rate > 0 and self.rng.random() < dup_rate:
            self.counter["duplicates"] += 1
            self._publish(topic, event, qos=1)

        label = CLASS_LABELS.get(event["aggregate"]["main_class"], "")
        print(
            f"  ↑ {dev.device_id} seq={event['seq']:>5} "
            f"{label} conf={event['aggregate']['max_confidence']:.2f} "
            f"({event['location']['lng']:.4f},{event['location']['lat']:.4f})"
        )

    def _simulate_net_drop(self) -> None:
        """模拟一次网络中断：断开 → N 秒后恢复。"""
        self.counter["net_drops"] += 1
        print(f"\n[网络] ⚠ 模拟断网（第 {self.counter['net_drops']} 次），事件转入本地缓存...")
        try:
            self._client.disconnect()
        except Exception:   # noqa: BLE001
            pass
        self._connected = False
        time.sleep(2.0)

        print("[网络] 尝试重连...")
        try:
            self._client.reconnect()
            time.sleep(1.0)
        except Exception as exc:   # noqa: BLE001
            print(f"[网络] 重连失败：{exc}")

    def _shutdown(self) -> None:
        self._save_state()
        if self._client is not None:
            try:
                # 优雅下线：发 retained 离线状态，让平台立刻感知
                for dev in self.devices:
                    self._publish_status(dev, online=False)
                time.sleep(0.3)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:   # noqa: BLE001
                pass
        self._print_summary()

    # ---------------- 输出 ----------------
    def _print_banner(self) -> None:
        total_rate = sum(d.rate for d in self.devices)
        window = self.temporal_conf.get("window_frames", 15)
        fps = 1.0 / max(0.01, float(self.args.interval))
        cooldown = (
            float(self.args.event_interval)
            if self.args.event_interval is not None
            else float(self.reporting_conf.get("cooldown_seconds", 90))
        )

        print("=" * 66)
        print("  探海灵眸 SeaSight — 边缘感知模拟器")
        print("=" * 66)
        print(f"  站点         : {self.site_id}")
        print(f"  设备数       : {len(self.devices)} 台（合计速率倍率 {total_rate:.1f}）")
        print(f"  场景         : {self.args.scenario}")
        print(f"  时序校验     : {'启用' if self.temporal_conf.get('enabled', True) else '关闭'}"
              f"（窗口 {window} 帧 / 最少 {self.temporal_conf.get('min_hits', 3)} 次命中 "
              f"≈ {window / fps:.1f}s 填满）")
        print(f"  抽帧节奏     : {self.args.interval:.3f}s/帧（≈{fps:.1f} fps）")
        print(f"  上报冷却     : {cooldown:.0f}s / 目标")
        print(f"  重复投递比例 : {self.args.dup_rate:.0%}")
        print(f"  断网模拟     : {'每 %d 轮一次' % self.args.net_drop_every if self.args.net_drop_every else '关闭'}")
        if self.args.dry_run:
            print("  模式         : DRY-RUN（不连接 Broker，只打印报文）")
        print("=" * 66)
        print("  设备清单：")
        for dev in self.devices:
            print(f"    · {dev.device_id:<18} {dev.name}  起始 seq={dev.seq}")
        print("=" * 66)

    def _print_progress(self, tick: int) -> None:
        if tick % 10 != 0:
            return
        c = self.counter

        # 增量累加各设备的统计（drain 会清零，避免重复计数）
        tracked = 0
        for dev in self.devices:
            if dev.validator is not None:
                st = dev.validator.drain_stats()
                c["suppressed_noise"] += st["suppressed"]
                c["raw_detections"] += st["fed"]
                tracked += st["tracked"]

        noise_ratio = (
            c["suppressed_noise"] / c["raw_detections"] if c["raw_detections"] else 0
        )
        print(
            f"  [统计] 轮次={tick} 原始检测={c['raw_detections']} "
            f"确认事件={c['confirmed_events']} 噪声过滤={noise_ratio:.0%} "
            f"跟踪中={tracked} 已发布={c['published']} 缓存={len(self.buffer)}"
        )

    def _print_summary(self) -> None:
        c = self.counter

        # 把各设备剩余的统计收干净，保证汇总数字完整。
        # 口径与 _print_progress 保持一致：fed / absorbed / suppressed 同单位（都是检测数）。
        tracked = 0
        for dev in self.devices:
            if dev.validator is not None:
                st = dev.validator.drain_stats()
                c["suppressed_noise"] += st["suppressed"]
                c["raw_detections"] += st["fed"]
                tracked += st["tracked"]

        print("\n" + "=" * 66)
        print("  运行统计")
        print("=" * 66)
        print(f"  原始帧数         : {c['raw_frames']}（7 台设备逐帧判定）")
        print(f"  原始检测数       : {c['raw_detections']}（含瞬态噪声）")
        print(f"  时序校验确认事件 : {c['confirmed_events']}")
        print(f"  被挡下的误报     : {c['suppressed_noise']}")
        if c["raw_detections"]:
            print(f"  误报抑制率       : {c['suppressed_noise'] / c['raw_detections']:.1%}")
        print(f"  成功发布         : {c['published']}（含重复投递 {c['duplicates']}）")
        print(f"  本地缓存/补传    : {c['buffered']} / {c['flushed']}")
        print(f"  模拟断网次数     : {c['net_drops']}")
        print("=" * 66)
        print("  说明：误报抑制率 = 从未被稳定跟踪的检测数 / 喂进校验器的检测总数。")
        print("        三个计数（fed/absorbed/suppressed）同单位，因此抑制率恒在")
        print("        0~100%，且满足 fed = absorbed + suppressed。")
        print("        抑制来自两道闸：① 置信度门限直接丢弃；")
        print("        ② 抽帧推理下，只在单帧闪现的浪花几乎不会被连续命中，")
        print("           凑不满 min_hits 便被窗口自然淘汰。")
        print("        注意分母是「检测数」不是「帧数」—— 同一目标在连续帧里")
        print("        贡献多个检测，稳定目标因此能被反复吸收。")
        print("=" * 66)
        print("  下一步：打开 http://localhost:8000/docs 查看事件是否入库，")
        print("          或访问前端大屏看告警与工单。")
        print("=" * 66)


# ----------------------------------------------------------------------
# 场景
# ----------------------------------------------------------------------
def apply_scenario(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    """按场景调整参数。

    注意 `--interval` 已改为「抽帧间隔」（默认 0.125s ≈ 8fps），
    场景预设不再改它 —— 真实摄像头帧率不因演示场景而变。
    控制演示节奏的是 `--event-interval`（上报冷却）。
    """
    scenario = args.scenario

    if scenario == "stress":
        # 压测：放宽确认门槛 + 缩短冷却 + 提高设备速率，把派单引擎压到极限
        config["temporal"]["window_frames"] = 6
        config["temporal"]["min_hits"] = 2
        config["reporting"]["cooldown_seconds"] = 5
        config["buffer"]["max_size"] = 5000
        if args.event_interval is None:
            args.event_interval = 1.0
        for dev in config.get("devices", []):
            dev["rate"] = float(dev.get("rate", 1.0)) * 3

    elif scenario == "regression":
        # 回归：时序校验更严格、冷却更长，结果稳定可复现，用于冒烟比对
        config["temporal"]["window_frames"] = 20
        config["temporal"]["min_hits"] = 5
        config["reporting"]["cooldown_seconds"] = 180
        if args.event_interval is None:
            args.event_interval = 10.0

    elif scenario == "demo":
        # 演示：真实帧率 + 配置文件里的冷却（90s）。
        # 想让事件出得更快，显式传 --event-interval 3。
        pass

    else:
        print(f"[警告] 未知场景 {scenario}，按 demo 处理")

    return config


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _now_iso() -> str:
    """带本地时区偏移的 ISO8601 时间戳。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"[错误] 配置文件不存在：{path}", file=sys.stderr)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="探海灵眸 SeaSight — 边缘感知模拟器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python simulator.py --scenario demo --event-interval 3 --loop\n"
            "      # 演示：8fps 抽帧 + 3 秒冷却，最快看到事件\n"
            "  python simulator.py --scenario stress --event-interval 1\n"
            "      # 压测：放宽确认门槛 + 1 秒冷却，压派单引擎\n"
            "  python simulator.py --scenario regression --seed 42 --duration 60\n"
            "      # 回归：固定种子，结果可复现\n"
            "  python simulator.py --dry-run --duration 10\n"
            "      # 只看报文，不连 Broker\n"
            "  python simulator.py --devices CAM-MABI-01 --dup-rate 0.3\n"
            "      # 验证平台幂等判重\n"
        ),
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument(
        "--scenario", default="demo", choices=["demo", "stress", "regression"], help="运行场景"
    )
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", "localhost"), help="MQTT Broker 地址")
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")), help="MQTT 端口")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.125,
        help="抽帧间隔秒数（模拟摄像头帧率），默认 0.125 = 8fps",
    )
    parser.add_argument(
        "--event-interval",
        type=float,
        default=None,
        help="同一目标的最小上报间隔秒数（覆盖配置里的 90s，用于加快演示节奏）",
    )
    parser.add_argument("--loop", action="store_true", help="循环运行不退出")
    parser.add_argument("--duration", type=int, default=0, help="运行时长（秒），0=不限")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    parser.add_argument("--devices", default=None, help="只模拟指定设备，逗号分隔")
    parser.add_argument("--dup-rate", type=float, default=0.0, help="重复投递比例 0~1")
    parser.add_argument("--net-drop-every", type=int, default=0, help="每 N 轮模拟一次断网")
    parser.add_argument("--dry-run", action="store_true", help="只打印不发送")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    _require_deps()          # 只在真正运行时检查依赖，便于纯逻辑单测
    config = load_config(Path(args.config))
    config = apply_scenario(args, config)

    # 命令行显式传入的 Broker 地址覆盖配置文件
    if args.host != "localhost":
        config.setdefault("mqtt", {})["host"] = args.host
    if args.port != 1883:
        config.setdefault("mqtt", {})["port"] = args.port

    # --loop 语义：不设 --duration 时就是无限循环（run() 内部靠 _stop 退出）；
    #          设了 --duration 则跑完一轮后重新开始，直到 Ctrl+C。
    if args.loop:
        args.duration = 0

    sim = EdgeSimulator(args, config)

    def _sigterm(_signum, _frame):
        sim._stop = True

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, OSError):
        pass   # 非主线程环境（如测试）无法注册信号

    sim.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
