"""地理工具：连江沿海乡镇中心点与最近乡镇归属。

这是「坐标 → 乡镇」的**唯一实现**，事件入库、任务创建、报表聚合都要经过
`nearest_township`，不允许任何地方另写一套判断 —— 否则乡镇归属会漂移。

独立成模块的原因：事件服务、派单引擎、报表聚合三处都要用，
放在 services/report.py 里会形成循环 import（report 依赖 models，
而 event/dispatch 也依赖 models）。
"""

from __future__ import annotations

import math

# 连江沿海乡镇中心点（lng, lat）—— 与 02_seed.sql 注释、前端 constants.js 对账。
TOWNSHIPS: tuple[tuple[str, float, float], ...] = (
    ("马鼻镇", 119.652, 26.386),
    ("黄岐镇", 119.904, 26.316),
    ("筱埕镇", 119.836, 26.352),
    ("苔菉镇", 120.010, 26.293),
    ("安凯镇", 119.760, 26.420),
    ("下宫镇", 119.887, 26.374),
)


def nearest_township(lng: float, lat: float) -> str:
    """返回距离 (lng, lat) 最近的乡镇名（haversine 大圆距离）。"""
    best_name = TOWNSHIPS[0][0]
    best_dist = float("inf")
    for name, tlng, tlat in TOWNSHIPS:
        d = _haversine(lng, lat, tlng, tlat)
        if d < best_dist:
            best_dist = d
            best_name = name
    return best_name


def _haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点大圆距离（米）。连江范围极小，但用 haversine 避免经纬度单位混用。"""
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
