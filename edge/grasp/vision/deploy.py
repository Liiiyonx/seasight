"""权重版本化与原子切换（含一键回滚）。

## 为什么需要它

演示日最怕的是「刚训完的模型不如上一版」。没有版本化就只能硬着头皮用
新模型；有了版本化，一条命令回到上一版即可。

## 原子性怎么保证

模型文件本身用 ``os.replace(tmp, final)`` 切换 —— 同文件系统上的 rename
是原子操作（Windows 与 POSIX 都成立），因此**任何时刻 active 目录里要么
是完整旧模型、要么是完整新模型，不存在半截状态**。版本历史另存于
``versions/<v>/``，回滚就是把旧版本的文件重新 replace 回去。

## 与评估门禁的关系

``deploy`` 接受 ``metrics``，并把它们写进版本元数据。调用方（作业台的
部署接口）负责在调用前跑 ``evaluate`` 并卡住不达标的模型 ——
deploy 本身不做门禁判断，只做「安全落盘 + 可回滚」。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("oceanus.grasp.deploy")

#: 部署时从训练产物里取走的文件后缀（按后端优先级）
MODEL_SUFFIXES = (".onnx", ".param", ".bin", ".pt")


class DeployError(RuntimeError):
    """模型文件缺失或切换失败。"""


class ModelStore:
    """模型仓库：``models/versions/<v>/`` 存历史，``models/active/`` 是当前。"""

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.versions_dir = self.base / "versions"
        self.active_dir = self.base / "active"
        self.index_path = self.base / "index.json"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "ModelStore":
        vision = cfg.get("vision", {}) or {}
        model_dir = Path(str(vision.get("model_dir", "models/active")))
        # 配置里的 model_dir 指向 active；仓库根就是它的父目录
        base = model_dir.parent if model_dir.name == "active" else model_dir
        if not base.is_absolute():
            base = Path(__file__).resolve().parent.parent / base
        return cls(base)

    # ------------------------------------------------------------------
    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"current": None, "history": []}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"current": None, "history": []}
        if not isinstance(data, dict):
            return {"current": None, "history": []}
        data.setdefault("current", None)
        data.setdefault("history", [])
        return data

    def _write_index(self, data: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    # ------------------------------------------------------------------
    def versions(self) -> list[dict[str, Any]]:
        index = self._read_index()
        current = index.get("current")
        out: list[dict[str, Any]] = []
        for entry in index.get("history", []):
            item = dict(entry)
            item["active"] = item.get("version") == current
            out.append(item)
        return out

    def current(self) -> dict[str, Any] | None:
        index = self._read_index()
        name = index.get("current")
        if not name:
            return None
        for entry in index.get("history", []):
            if entry.get("version") == name:
                return dict(entry)
        return None

    def active_model_dir(self) -> Path:
        return self.active_dir

    # ------------------------------------------------------------------
    def deploy(
        self,
        model_path: str | Path,
        *,
        metrics: dict[str, Any] | None = None,
        note: str = "",
        extra_files: list[str | Path] | None = None,
    ) -> dict[str, Any]:
        """把一份模型登记为新版本并切为 active，返回版本元数据。"""
        src = Path(model_path)
        if not src.exists():
            raise DeployError(f"模型文件不存在: {src}")
        if not src.is_file():
            raise DeployError(f"模型路径不是文件: {src}")

        vdir = self._create_version_dir()

        # 主模型文件（含同名的 .param/.bin 兄弟文件）
        copied: list[str] = []
        for path in self._sibling_files(src):
            shutil.copy2(path, vdir / path.name)
            copied.append(path.name)
        for extra in extra_files or []:
            epath = Path(extra)
            if epath.exists():
                shutil.copy2(epath, vdir / epath.name)
                copied.append(epath.name)

        meta = {
            "version": vdir.name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": str(src),
            "files": sorted(copied),
            "metrics": dict(metrics or {}),
            "note": note,
        }
        (vdir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self._activate(vdir, meta)
        # ★ 登记 history 只在 deploy 里做：activate() 只是切换 active，
        #   如果它也跟着 append，回滚一次就会在历史里多出一条重复版本记录
        #   （versions() 的条数与 history 对不上，页面会显示幽灵版本）。
        index = self._read_index()
        index["current"] = meta["version"]
        index.setdefault("history", []).append(meta)
        self._write_index(index)

        logger.info("在用模型版本 %s（文件 %s）", meta["version"], meta["files"])
        return meta

    def _create_version_dir(self) -> Path:
        """建一个**唯一**的版本目录。

        ★ 版本号只精确到秒，同一秒内连续部署两次会撞名 —— 撞名后第二次
          会写进同一个目录，既覆盖上一版的权重文件，又让 index 里出现两条
          同版本记录（历史直接失真）。这里用 mkdir(exist_ok=False) 抢名字，
          占用失败就加序号重试，从机制上杜绝。
        """
        stamp = f"v{datetime.now():%Y%m%d_%H%M%S}"
        for attempt in range(100):
            name = stamp if attempt == 0 else f"{stamp}_{attempt + 1}"
            try:
                vdir = self.versions_dir / name
                vdir.mkdir(parents=True, exist_ok=False)
                return vdir
            except FileExistsError:
                continue
        raise DeployError(f"无法分配唯一版本目录（前缀 {stamp}）")

    def activate(self, version: str) -> dict[str, Any]:
        """把某个历史版本切回 active（回滚用）。"""
        vdir = self.versions_dir / version
        if not vdir.exists():
            raise DeployError(f"版本不存在: {version}")
        meta_path = vdir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {
            "version": version
        }
        self._activate(vdir, meta)
        logger.info("已回滚到模型版本 %s", version)
        return meta

    def rollback(self) -> dict[str, Any]:
        """回滚到 active 之前的那个版本。"""
        index = self._read_index()
        history = index.get("history", [])
        current = index.get("current")
        others = [e["version"] for e in history if e.get("version") != current]
        if not others:
            raise DeployError("没有可回滚的历史版本")
        return self.activate(others[-1])

    # ------------------------------------------------------------------
    def _sibling_files(self, src: Path) -> list[Path]:
        """NCNN 的 .param/.bin 是一对，同名的兄弟文件一起搬。"""
        out = [src]
        if src.suffix == ".param":
            for cand in (src.with_suffix(".bin"), src.with_suffix(".param")):
                if cand.exists() and cand != src:
                    out.append(cand)
        return out

    def _activate(self, vdir: Path, meta: dict[str, Any]) -> None:
        """把 vdir 里的模型文件原子替换到 active 目录。"""
        self.active_dir.mkdir(parents=True, exist_ok=True)
        files = list(meta.get("files") or [])
        primary = [f for f in files if f.endswith((".onnx", ".param", ".pt"))]
        if not primary:
            raise DeployError(f"版本 {vdir.name} 里没有可用的模型文件（{files}）")

        # 先把新文件写进临时名，全部就位后再逐个 replace ——
        # 任一步失败都不会破坏当前 active。
        staged: list[tuple[Path, Path]] = []
        for name in files:
            target = self.active_dir / name
            tmp = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(vdir / name, tmp)
            staged.append((tmp, target))
        for tmp, target in staged:
            os.replace(tmp, target)

        # 清掉 active 里不属于本版本、且不是模型的残留（如旧格式 best.pt）
        keep = set(files)
        for existing in self.active_dir.iterdir():
            if existing.name not in keep and existing.suffix in MODEL_SUFFIXES:
                try:
                    existing.unlink()
                except OSError:
                    pass

        index = self._read_index()
        index["current"] = meta.get("version")
        self._write_index(index)
