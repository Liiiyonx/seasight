"""配置加载器：web / vision / vision_driver 三处共用一个入口。

为什么单独放一个模块：三处都要读同一份 ``config.yaml``，各自的
默认值不应该各写一遍 —— 一旦漂移就会出现「页面按 A 跑、驱动按 B 跑」
这种最难查的现场故障。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读入配置；缺省读同目录的 config.yaml。

    返回值始终是 dict，且顶层段落缺失时补成空 dict —— 调用方不必层层
    写 ``cfg.get("arm", {}) or {}``。
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"配置读取失败 {cfg_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"配置不是合法 YAML {cfg_path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"配置顶层必须是映射 {cfg_path}")
    for section in ("web", "arm", "vision", "data", "mqtt", "agent"):
        raw.setdefault(section, {})
    return raw


def deep_get(cfg: dict[str, Any], path: str, default: Any = None) -> Any:
    """点号路径取值：``deep_get(cfg, "arm.home.pitch", -65)``。"""
    node: Any = cfg
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
