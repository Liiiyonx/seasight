"""9 点标定：图像像素 ↔ 工作平面坐标。

## 标定口径（答辩要能讲清）

相机装在夹爪上（**眼在手上**），且工作平面（水面/台面）在很小的 z 范围内
起伏，因此不需要完整 3D 手眼标定 —— 只需要在**一个固定观测姿态**下，
建立「平面上任意点」的像素坐标 (u,v) 与其工作坐标 (x,y) 之间的 2D 仿射映射。

    [x, y] = [u, v, 1] · M      （M 为 3×2，最小二乘拟合）

标定的**已知量从哪来**：操作员把可检测目标（塑料袋）放在工作平面 9 个
已知网格点上，系统在固定观测姿态下记录各自像素中心。9 点覆盖整个工作
区域，比 3 点更能摊平镜头畸变带来的残余误差。

## 为什么不用追求一次性精度

手眼关系随臂姿变化，仿射映射只在观测姿态附近严格成立。所以标定值只用于
**粗定位**（给出抓取目标的初始世界坐标），真正的精度由视觉伺服闭环逐帧
吸收：逼近过程中持续重检测、按仿射雅可比（仿射映射的雅可比是常数）
逐步修正偏差，直到目标落到图像中心区。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("oceanus.grasp.calibration")

OBSERVATION_DWELL_S = 1.6

#: 标定文件默认落点：edge/grasp/calibration.json（驱动与作业台共用一份）
DEFAULT_CALIBRATION_PATH = Path(__file__).resolve().parent / "calibration.json"


class CalibrationError(RuntimeError):
    """标定数据不足或不可解。"""


def load_calibration(path: str | Path | None = None,
                     plane_z: float | None = None) -> "Calibration":
    """读标定文件；不存在或损坏时返回空标定（``ready`` 为 False）。

    不抛异常：现场未标定时，调用方要能拿到一个「未就绪」的对象并给出
    可执行的提示，而不是在启动阶段直接崩掉。

    ``plane_z``：**还没有标定文件时**用的水面/台面高度（来自 config 的
    ``arm.plane_z``）。已有文件就以文件里的值为准 —— 那才是现场量出来的。
    """
    p = Path(path) if path else DEFAULT_CALIBRATION_PATH
    if not p.exists():
        return Calibration(plane_z=float(plane_z) if plane_z is not None else 0.02)
    try:
        return Calibration.load(p)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("标定文件不可用 %s: %s", p, exc)
        return Calibration(plane_z=float(plane_z) if plane_z is not None else 0.02)


def save_calibration(cal: "Calibration", path: str | Path | None = None) -> Path:
    p = Path(path) if path else DEFAULT_CALIBRATION_PATH
    cal.save(p)
    return p


def config_plane_z(cfg: dict[str, Any]) -> float | None:
    """从 ``config.yaml`` 的 ``arm.plane_z`` 取水面/台面高度（m）。

    未设置或非法返回 ``None``（由调用方决定退回 0.02 还是标定文件里的值）。
    这个值就是「水面相对臂座安装面有多高」，船上是负的。
    """
    arm = (cfg or {}).get("arm") or {}
    raw = arm.get("plane_z")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("arm.plane_z 不是数字（%r），忽略", raw)
        return None


def demo_calibration(plane_z: float = 0.02) -> "Calibration":
    """mock 专用演示标定：线性映射，像素中心附近 ≈ 世界原点。

    用途是让整条六态闭环在没有真机标定的机器上也能跑通（验证 UI 与状态机）。
    **它不是真标定**，``ready`` 为 True 只是为了不把闭环挡在前置检查外；
    真机必须跑完 9 点标定再作业。

    同时装一个 `source="demo"` 的**演示放置点**，好让「拾取 → 放入料槽」这段
    在无硬件时也能整条走完。真机上它会被标定/示教值覆盖，且 precheck 会明确
    把演示值判为未示教（同 RMSE 恰为 0 的演示标定指纹一个思路）。
    """
    cal = Calibration(plane_z=plane_z)
    s = 0.0004
    cal.matrix = [[s, 0.0], [0.0, s], [-s * 320.0, -s * 240.0]]
    cal.pairs = [
        {"pixel": [0.0, 0.0], "world": [-s * 320, -s * 240]},
        {"pixel": [320.0, 240.0], "world": [0.0, 0.0]},
        {"pixel": [640.0, 480.0], "world": [s * 320, s * 240]},
    ]
    cal.rmse = 0.0
    cal.place = {"coord": [0.10, 0.0], "source": "demo"}
    return cal


def describe_place(place: dict[str, Any] | None) -> dict[str, Any]:
    """把放置点配置收敛成一份**只读描述**，给状态接口 / 自检 / 驱动共用。

    两种给法（coord 优先）：
      * ``coord`` —— 工作平面坐标 [x, y]（m，标定坐标系），由 IK 解算过去；
      * ``pose``  —— 示教舵机脉宽 {servo_id: 0..1000}，直接回放该位姿。

    ``kind`` 为 ``"none"`` 表示**没有放置点**：此时闭环会明确报错，而不是
    把袋子随手丢在原地（"没放进去"和"放进去了"必须能区分）。
    """
    place = place or {}
    coord = place.get("coord")
    pose = place.get("pose")
    if isinstance(coord, (list, tuple)) and len(coord) >= 2:
        kind, target = "coord", [float(coord[0]), float(coord[1])]
    elif isinstance(pose, dict) and pose:
        kind, target = "pose", {int(k): int(v) for k, v in pose.items()}
    else:
        kind, target = "none", None
    return {
        "kind": kind,
        "target": target,
        "ready": kind != "none",
        "source": str(place.get("source") or ("config" if kind != "none" else "none")),
        "saved_at": place.get("saved_at"),
    }


@dataclass
class Calibration:
    """像素↔工作坐标仿射映射，可存盘复用。"""

    plane_z: float = 0.02
    pairs: list[dict[str, list[float]]] = field(default_factory=list)
    matrix: list[list[float]] | None = None      # 3×2
    rmse: float | None = None

    #: 料槽放置点。与标定**同寿命、同文件**：放置点是「相对标定原点的工位」，
    #: 换机器/重标定后必须重新示教，所以放这里而不是放共享 config.yaml
    #: （config 里只留兜底值）。结构见 :func:`describe_place`。
    place: dict[str, Any] | None = None

    # ---------- 采点 ----------
    def add_point(self, pixel: tuple[float, float], world: tuple[float, float]) -> None:
        self.pairs.append({"pixel": [float(pixel[0]), float(pixel[1])],
                           "world": [float(world[0]), float(world[1])]})
        self.matrix = None      # 数据变了，旧解失效
        self.rmse = None

    def clear(self) -> None:
        self.pairs.clear()
        self.matrix = None
        self.rmse = None

    # ---------- 拟合 ----------
    def fit(self) -> list[list[float]]:
        """最小二乘拟合仿射映射。至少 3 点（更多点更能摊平畸变误差）。"""
        import numpy as np

        if len(self.pairs) < 3:
            raise CalibrationError(f"仿射标定至少需要 3 个点，当前 {len(self.pairs)}")
        a = np.array([[p["pixel"][0], p["pixel"][1], 1.0] for p in self.pairs])
        b = np.array([[p["world"][0], p["world"][1]] for p in self.pairs])
        sol, *_ = np.linalg.lstsq(a, b, rcond=None)
        resid = a @ sol - b
        self.rmse = float(np.sqrt((resid ** 2).sum(axis=1).mean()))
        self.matrix = sol.tolist()
        return self.matrix

    def _matrix(self) -> Any:
        import numpy as np

        if self.matrix is None:
            self.fit()
        return np.array(self.matrix, dtype=float)

    # ---------- 换算 ----------
    def pixel_to_world(self, u: float, v: float, z: float | None = None) -> tuple[float, float, float]:
        import numpy as np

        m = self._matrix()
        x, y = np.array([float(u), float(v), 1.0]) @ m
        return float(x), float(y), float(self.plane_z if z is None else z)

    def world_to_pixel(self, x: float, y: float) -> tuple[float, float]:
        """仿射逆变换；用于把当前抓取点投影回图像做叠加显示。"""
        import numpy as np

        m = self._matrix()
        linear = m[:2, :]                       # 2×2
        offset = m[2, :]                        # 1×2
        try:
            uv = np.linalg.solve(linear.T, np.array([x, y]) - offset)
        except np.linalg.LinAlgError as exc:
            raise CalibrationError("标定矩阵不可逆，点位可能共线") from exc
        return float(uv[0]), float(uv[1])

    def pixel_jacobian(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """d(world)/d(pixel) 的 2×2 雅可比（仿射映射下为常数）。

        视觉伺服用它把「目标偏离图像中心多少像素」直接换算成
        「世界坐标该修正多少米」，一步到位，无需迭代试凑。
        """
        m = self._matrix()
        return ((float(m[0][0]), float(m[1][0])),
                (float(m[0][1]), float(m[1][1])))

    # ---------- 存盘 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "plane_z": self.plane_z,
            "pairs": self.pairs,
            "matrix": self.matrix,
            "rmse": self.rmse,
            "place": self.place,
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Calibration":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cal = cls(plane_z=float(raw.get("plane_z", 0.02)))
        cal.pairs = list(raw.get("pairs", []))
        cal.matrix = raw.get("matrix")
        cal.rmse = raw.get("rmse")
        place = raw.get("place")
        cal.place = dict(place) if isinstance(place, dict) else None
        return cal

    @property
    def ready(self) -> bool:
        return bool(self.pairs) and (self.matrix is not None or len(self.pairs) >= 3)


def capture_at_observation(
    io: Any,
    detector: Any,
    grid: list[tuple[float, float]],
    *,
    marker_class: str,
    dwell_s: float = OBSERVATION_DWELL_S,
    plane_z: float | None = None,
    on_step: Callable[[int, tuple[float, float], tuple[float, float] | None], None] | None = None,
) -> Calibration:
    """固定观测姿态下采集 9 点标定数据。

    操作流程（作业台页面会引导）：

    1. 机械臂先回到观测姿态（待命位 + 稍抬高的俯视角度）。
    2. 页面依次提示操作员把塑料袋放到 9 个网格点之一。
    3. 每放好一个点，本函数等待若干秒，用检测器取该目标的像素中心，
       与已知的世界坐标配成一对。
    4. 9 点采完即拟合仿射映射。

    某个点没检测到就跳过（不写脏数据）—— 少一个点不影响拟合，但凑不满
    3 个点会由 ``fit()`` 明确报错。

    ★ ``plane_z`` 必须显式传进来：标定只解出像素↔(x, y)，**z 是另一件事**
      （水面相对臂座安装面的高度）。不传就等于把水面当成 z=0.02 的水平面；
      船上甲板高于水面，漏了这一步爪子会压不到袋子。
    """
    cal = Calibration(plane_z=float(plane_z) if plane_z is not None else 0.02)
    io.home(duration_ms=1500)
    for idx, (wx, wy) in enumerate(grid, start=1):
        time.sleep(dwell_s)
        frame = io.latest_frame()
        pixel: tuple[float, float] | None = None
        if frame is not None:
            dets = [d for d in detector.infer(frame) if d.cls == marker_class]
            if dets:
                best = max(dets, key=lambda d: d.conf)
                pixel = best.center
        if pixel is not None:
            cal.add_point(pixel, (wx, wy))
        if on_step is not None:
            on_step(idx, (wx, wy), pixel)
    cal.fit()
    return cal
