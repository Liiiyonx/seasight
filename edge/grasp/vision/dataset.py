"""YOLO 格式数据集组织：采集帧 → 标注 → 留出集划分。

目录约定（``data.root`` 之下）::

    data/
      raw/<session>/frame_0001.jpg     采集的原始帧（按采集会话分组）
      labels/<session>/frame_0001.txt  与帧同名的 YOLO 标注
      built/                           组装好的训练集（ultralytics 直接吃）
        images/train/*.jpg
        images/val/*.jpg
        labels/train/*.txt
        labels/val/*.txt
        data.yaml

标注行格式与 YOLO 一致：``cls_id cx cy w h``，归一化到 0..1。
类别索引由 ``vision.classes`` 决定 —— **顺序即语义**，改顺序必须重训。
"""

from __future__ import annotations

import logging
import json
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("oceanus.grasp.dataset")

#: 会话级「整组都是背景负样本」标记文件名（放在该会话的 raw 目录里）。
#: 为什么需要它：``labeled_frames`` 按「标注文件非空」筛帧，那是为了不把**尚未
#: 标注**的帧当成负样本。但现场泳池域有一类帧是**刻意的纯负样本**（空池、臂体
#: 入画、手入画、水面反光）—— 按规程它们「整张不标」，若不额外标记就会被静默
#: 丢掉，而负样本恰恰是机械臂场景里最值钱的那批数据（决定精确率）。
#: 因此用会话级标记把「刻意负样本」与「还没标」区分开。
BACKGROUND_MARKER = ".background"


class DatasetError(RuntimeError):
    """数据集不完整或标注格式非法。"""


@dataclass
class Box:
    """一个归一化标注框。"""

    cls: str
    cx: float
    cy: float
    w: float
    h: float
    conf: float = 1.0

    def to_line(self, class_index: dict[str, int]) -> str:
        if self.cls not in class_index:
            raise DatasetError(f"未知类别 {self.cls!r}；已注册 {sorted(class_index)}")
        return (f"{class_index[self.cls]} {self.cx:.6f} {self.cy:.6f} "
                f"{self.w:.6f} {self.h:.6f}")


@dataclass
class Dataset:
    """数据集管理（会话采集 + 标注读写 + 组装）。"""

    root: Path
    classes: list[str]
    capture_frames: int = 200
    val_ratio: float = 0.2
    sessions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.class_index = {name: i for i, name in enumerate(self.classes)}

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Dataset":
        data = cfg.get("data", {}) or {}
        vision = cfg.get("vision", {}) or {}
        base = Path(str(data.get("root", "data")))
        if not base.is_absolute():
            base = Path(__file__).resolve().parent.parent / base
        return cls(
            root=base,
            classes=list(vision.get("classes") or ["plastic_bag", "buoy"]),
            capture_frames=int(data.get("capture_frames", 200)),
            val_ratio=float(data.get("val_ratio", 0.2)),
        )

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def labels_dir(self) -> Path:
        return self.root / "labels"

    @property
    def built_dir(self) -> Path:
        return self.root / "built"

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    def new_session(self, tag: str = "") -> str:
        """开一个采集会话，返回会话名。"""
        name = f"{datetime.now():%Y%m%d_%H%M%S}" + (f"_{tag}" if tag else "")
        (self.raw_dir / name).mkdir(parents=True, exist_ok=True)
        (self.labels_dir / name).mkdir(parents=True, exist_ok=True)
        self.sessions.append(name)
        return name

    def add_frame(self, session: str, jpeg: bytes) -> Path:
        """把一帧 JPEG 落盘，返回帧路径。"""
        if not jpeg:
            raise DatasetError("空帧，拒绝落盘")
        folder = self.raw_dir / session
        folder.mkdir(parents=True, exist_ok=True)
        n = len(list(folder.glob("frame_*.jpg"))) + 1
        path = folder / f"frame_{n:04d}.jpg"
        path.write_bytes(jpeg)
        return path

    def list_frames(self, session: str | list[str] | None = None) -> list[Path]:
        """列出采集帧；``session`` 支持单个会话名、会话名列表或 None（全部）。"""
        if session is None:
            return sorted(self.raw_dir.rglob("frame_*.jpg"))
        names = [session] if isinstance(session, str) else list(session)
        out: list[Path] = []
        for name in names:
            out.extend(sorted((self.raw_dir / name).rglob("frame_*.jpg")))
        return sorted(out)

    @staticmethod
    def frame_label_path(frame: Path, raw_dir: Path, labels_dir: Path) -> Path:
        """帧路径 → 对应标注路径（保持会话子目录结构）。"""
        rel = frame.relative_to(raw_dir)
        return (labels_dir / rel).with_suffix(".txt")

    # ------------------------------------------------------------------
    # 标注
    # ------------------------------------------------------------------
    def write_label(self, frame: Path, boxes: list[Box | dict[str, Any]]) -> Path:
        label_path = self.frame_label_path(frame, self.raw_dir, self.labels_dir)
        label_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for box in boxes:
            if isinstance(box, dict):
                box = Box(str(box["cls"]), float(box["cx"]), float(box["cy"]),
                          float(box["w"]), float(box["h"]), float(box.get("conf", 1.0)))
            lines.append(box.to_line(self.class_index))
        label_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return label_path

    def read_label(self, frame: Path) -> list[Box]:
        label_path = self.frame_label_path(frame, self.raw_dir, self.labels_dir)
        if not label_path.exists():
            return []
        boxes: list[Box] = []
        for line in label_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) != 5:
                continue
            idx = int(parts[0])
            name = self.classes[idx] if 0 <= idx < len(self.classes) else f"cls{idx}"
            boxes.append(Box(name, float(parts[1]), float(parts[2]),
                             float(parts[3]), float(parts[4])))
        return boxes

    def labeled_frames(self, session: str | list[str] | None = None) -> list[Path]:
        """返回「可用作训练样本」的帧。

        = 标注非空的帧 ∪ **被显式标记为背景负样本的会话**里的帧。
        前者避免把尚未标注的帧当成负样本；后者让刻意采集的纯负样本（空池、
        臂体入画）真正进入训练 —— 否则它们会被静默丢掉（见 BACKGROUND_MARKER）。
        """
        return [f for f in self.list_frames(session)
                if self.read_label(f) or self.is_background(f)]

    # ------------------------------------------------------------------
    # 背景负样本标记
    # ------------------------------------------------------------------
    def mark_background(self, session: str, note: str = "") -> Path:
        """把整个会话标为「背景负样本」，返回标记文件路径。"""
        folder = self.raw_dir / session
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / BACKGROUND_MARKER
        payload = {"session": session, "note": note,
                   "marked_at": datetime.now().isoformat(timespec="seconds")}
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return marker

    def is_background(self, frame: Path) -> bool:
        """该帧所属会话是否被标为背景负样本。"""
        try:
            rel = frame.relative_to(self.raw_dir)
        except ValueError:
            return False
        return (self.raw_dir / rel.parent.name / BACKGROUND_MARKER).exists()

    def unmark_background(self, session: str) -> bool:
        """撤销会话的整组背景标记，返回是否真的删掉了标记文件。

        ★ 必须能撤：标错一个会话后，它的每一帧都会以「空标签背景帧」进训练
        （等于教模型「这场景里什么都没有」）。不可撤销的错误标记比不加标记危险得多。
        """
        marker = self.raw_dir / session / BACKGROUND_MARKER
        if not marker.exists():
            return False
        try:
            marker.unlink()
        except OSError as exc:
            logger.warning("背景标记删除失败 %s: %s", marker, exc)
            return False
        return True

    def describe_sessions(self) -> list[dict[str, Any]]:
        """列出**磁盘上**所有采集会话及其可用样本数。

        ★ 为什么必须由 dataset 提供、且不能用内存里的 ``self.sessions``：
          ① 「哪些帧算样本」= 标注非空 ∪ 会话被标为背景（见 ``labeled_frames``）是
             本类自己的语义。在 web 层重写一遍必然漂移 —— 界面显示「采了 200 帧」、
             训练却只吃 12 帧，这种不一致最难查。所以 ``usable`` 在这里算。
          ② 会话枚举以**磁盘**为准：``tools/capture_pool.py``（另一个进程）采的组
             不在本进程的 ``self.sessions`` 里，用内存列表会漏掉它们。
        """
        out: list[dict[str, Any]] = []
        if not self.raw_dir.exists():
            return out
        for folder in sorted(p for p in self.raw_dir.iterdir() if p.is_dir()):
            frames = sorted(folder.rglob("frame_*.jpg"))
            if not frames:
                continue
            labeled = sum(1 for f in frames if self.read_label(f))
            marker = folder / BACKGROUND_MARKER
            background = marker.exists()
            note = marked_at = None
            if background:
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    note = payload.get("note") or None
                    marked_at = payload.get("marked_at") or None
                except (OSError, ValueError):
                    pass
            out.append({
                "session": folder.name,
                "frames": len(frames),
                "labeled": labeled,
                "background": background,
                "background_note": note,
                "marked_at": marked_at,
                # 可用样本数：背景会话整组都算样本；否则只有标注非空的那批
                "usable": len(frames) if background else labeled,
            })
        return out

    def built_stem(self, frame: Path) -> str:
        """组装训练集时的文件主名：用「会话名_原名」消歧。

        ★ 各会话的帧名都叫 ``frame_0001.jpg``…；多会话一起组装时若按原名平铺，
          后一个会话的同名帧会**静默覆盖**前一个（实测 282 帧缩水到 153 帧，
          且不报任何错）。带上会话名后同名即不再同名。
        """
        try:
            rel = frame.relative_to(self.raw_dir)
        except ValueError:
            return frame.stem
        return f"{rel.parent.name}_{rel.stem}" if rel.parent.name else frame.stem

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------
    def build(self, session: str | list[str] | None = None, seed: int = 42,
              group_splits: bool = False) -> dict[str, Any]:
        """组装 ultralytics 可用的数据集目录，返回统计信息。

        ``group_splits``：按**采集单元**（会话目录）整组切分，而不是逐帧洗牌。
        现场泳池采集是视频流，同一组内相邻帧几乎相同；逐帧洗牌会把同一袋在同一
        位置的相邻帧拆到 train 与 val 两侧，就是泄漏（见采集规程 §4）。整组切分
        后 val 完全由**未进过训练**的采集组构成，指标才有意义。
        合成数据帧相互独立，用默认的逐帧洗牌即可（同一分布内采样，不构成泄漏）。
        """
        frames = self.labeled_frames(session)
        if len(frames) < 5:
            raise DatasetError(f"有标注的帧太少（{len(frames)}），至少需要 5 帧才能组装")

        if self.built_dir.exists():
            shutil.rmtree(self.built_dir)
        for sub in ("images/train", "images/val", "labels/train", "labels/val"):
            (self.built_dir / sub).mkdir(parents=True, exist_ok=True)

        if group_splits:
            val_set = self._pick_val_units(frames, seed)
            train_set = set(frames) - val_set
        else:
            shuffled = list(frames)
            random.Random(seed).shuffle(shuffled)
            n_val = max(1, int(round(len(shuffled) * self.val_ratio)))
            val_set, train_set = set(shuffled[:n_val]), set(shuffled[n_val:])
        shuffled = sorted(frames)

        counts: dict[str, int] = {name: 0 for name in self.classes}
        n_background = 0
        for frame in shuffled:
            split = "val" if frame in val_set else "train"
            stem = self.built_stem(frame)
            shutil.copy2(frame, self.built_dir / "images" / split / f"{stem}{frame.suffix}")
            label = self.frame_label_path(frame, self.raw_dir, self.labels_dir)
            if label.exists():
                shutil.copy2(label, self.built_dir / "labels" / split / f"{stem}.txt")
            else:
                # 背景负样本：不写标签文件。ultralytics 把「有图无标签」当背景图
                # （日志里计入 backgrounds），这正是我们要的 —— 模型在这些帧上
                # 学到的是「什么都别检」，而不是被跳过。
                n_background += 1
            for box in self.read_label(frame):
                counts[box.cls] = counts.get(box.cls, 0) + 1

        yaml_path = self.built_dir / "data.yaml"
        names = "\n".join(f"  {i}: {name}" for i, name in enumerate(self.classes))
        yaml_path.write_text(
            f"# 由 edge/grasp/vision/dataset.py 生成，请勿手工编辑\n"
            f"path: {self.built_dir.as_posix()}\n"
            f"train: images/train\n"
            f"val: images/val\n"
            f"names:\n{names}\n",
            encoding="utf-8",
        )
        stats = {
            "train": len(train_set),
            "val": len(val_set),
            "total": len(shuffled),
            "background": n_background,
            "per_class": counts,
            "data_yaml": str(yaml_path),
            "group_splits": group_splits,
        }
        if group_splits:
            stats["val_units"] = sorted({f.relative_to(self.raw_dir).parent.name
                                         for f in val_set})
        logger.info("数据集组装完成：train=%d val=%d 背景=%d 类别分布=%s", stats["train"],
                    stats["val"], n_background, counts)
        return stats

    def _pick_val_units(self, frames: list[Path], seed: int) -> set[Path]:
        """把采集单元（会话目录）整体分给 val，直到 val 帧数接近 ``val_ratio``。

        单元不可拆 —— 拆了就泄漏（见 ``build`` 的说明）。单元数不足两个时无法
        整组切分，直接报错而不是悄悄退化成逐帧洗牌：静默降级会产生"看起来有 val
        集、其实已泄漏"的最坏情况。
        """
        units: dict[str, list[Path]] = {}
        for frame in frames:
            units.setdefault(frame.relative_to(self.raw_dir).parent.name, []).append(frame)
        if len(units) < 2:
            raise DatasetError(
                f"group_splits 需要至少 2 个采集单元（会话），当前只有 {len(units)} 个："
                f"{sorted(units)}；请分多次采集（每次一个组）或用默认逐帧切分"
            )
        names = sorted(units)
        random.Random(seed).shuffle(names)
        target = max(1, int(round(len(frames) * self.val_ratio)))
        val_names: list[str] = []
        picked = 0
        for name in names:
            if val_names and picked >= target:
                break
            # 至少留一个单元给 train，val 才不至于把整批吃掉
            if len(val_names) >= len(names) - 1:
                break
            val_names.append(name)
            picked += len(units[name])
        val_set = {f for name in val_names for f in units[name]}
        logger.info("整组切分：val 单元=%s（%d 帧）", val_names, len(val_set))
        return val_set
