"""事件报文 schema 校验测试。

★ 这组测试守的是「三组并行开发的接口契约」。
报文字段的类型与约束一旦松动，算法组发的东西平台就收不了，
而且故障表现为"某条消息莫名其妙丢了"，排查成本极高。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models.event import EventStatus, WasteClass
from app.schemas import DetectionItem, EventAggregate, EventIngest, GeoPoint


# ----------------------------------------------------------------------
# 构造合法报文
# ----------------------------------------------------------------------
def valid_payload(**overrides) -> dict:
    base = {
        "event_id": "evt_CAM-MABI-01_000123",
        "device_id": "CAM-MABI-01",
        "device_type": "shore_camera",
        "timestamp": datetime.now(timezone(timedelta(hours=8))),
        "location": {"lng": 119.6531, "lat": 26.3867},
        "detections": [
            {"class": "foam", "confidence": 0.91, "bbox": [412, 288, 468, 331]},
        ],
        "aggregate": {"main_class": "foam", "count": 1, "max_confidence": 0.91},
        "evidence_url": None,
        "model_version": "det_v0.1.0",
        "seq": 123,
    }
    base.update(overrides)
    return base


class TestHappyPath:
    def test_minimal_valid_payload(self) -> None:
        event = EventIngest(**valid_payload())
        assert event.event_id == "evt_CAM-MABI-01_000123"
        assert event.aggregate.main_class == "foam"
        assert len(event.detections) == 1

    def test_empty_detections_allowed(self) -> None:
        """允许空 detections —— 边缘端可能只给聚合结论不给明细。"""
        event = EventIngest(**valid_payload(detections=[]))
        assert event.detections == []

    def test_evidence_url_optional(self) -> None:
        event = EventIngest(**valid_payload(evidence_url=None))
        assert event.evidence_url is None

    def test_model_version_optional(self) -> None:
        payload = valid_payload()
        del payload["model_version"]
        event = EventIngest(**payload)
        assert event.model_version is None

    def test_device_type_defaults_to_shore_camera(self) -> None:
        payload = valid_payload()
        del payload["device_type"]
        event = EventIngest(**payload)
        assert event.device_type == "shore_camera"

    @pytest.mark.parametrize("cls", ["foam", "plastic", "fishing_gear", "other"])
    def test_all_four_classes_accepted(self, cls: str) -> None:
        """四个类别都必须能通过校验 —— 少一个就有一类垃圾无法上报。"""
        payload = valid_payload()
        payload["aggregate"]["main_class"] = cls
        payload["detections"][0]["class"] = cls
        event = EventIngest(**payload)
        assert event.aggregate.main_class == cls


class TestRequiredFields:
    @pytest.mark.parametrize(
        "missing",
        ["event_id", "device_id", "timestamp", "location", "aggregate", "seq"],
    )
    def test_missing_required_field_rejected(self, missing: str) -> None:
        payload = valid_payload()
        del payload[missing]
        with pytest.raises(ValidationError):
            EventIngest(**payload)

    def test_class_alias_works(self) -> None:
        """`class` 是 Python 关键字，模型里用 alias 映射 —— 两种写法都要能解析。"""
        item = DetectionItem(**{"class": "foam", "confidence": 0.9, "bbox": [0, 0, 10, 10]})
        assert item.cls == "foam"


class TestFieldConstraints:
    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DetectionItem(**{"class": "foam", "confidence": 1.5, "bbox": [0, 0, 10, 10]})

    def test_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DetectionItem(**{"class": "foam", "confidence": -0.1, "bbox": [0, 0, 10, 10]})

    def test_bbox_must_be_four_values(self) -> None:
        """bbox 必须是 [x1,y1,x2,y2] 四个值 —— 前端按此做百分比叠加。"""
        with pytest.raises(ValidationError):
            DetectionItem(**{"class": "foam", "confidence": 0.9, "bbox": [0, 0, 10]})
        with pytest.raises(ValidationError):
            DetectionItem(**{"class": "foam", "confidence": 0.9, "bbox": [0, 0, 10, 10, 20]})

    def test_aggregate_count_must_be_positive(self) -> None:
        """count=0 无意义（既然报了就至少有一个）—— 拒绝能早发现边缘端逻辑 bug。"""
        with pytest.raises(ValidationError):
            EventAggregate(main_class="foam", count=0, max_confidence=0.9)

    def test_seq_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            EventIngest(**valid_payload(seq=-1))

    def test_seq_zero_allowed(self) -> None:
        """seq 从 0 开始是合法的（首次上报）。"""
        event = EventIngest(**valid_payload(seq=0))
        assert event.seq == 0

    def test_too_many_detections_rejected(self) -> None:
        """单帧超过 100 个目标 —— 更可能是模型异常而非真实场景。"""
        many = [
            {"class": "foam", "confidence": 0.9, "bbox": [i, i, i + 10, i + 10]}
            for i in range(101)
        ]
        with pytest.raises(ValidationError):
            EventIngest(**valid_payload(detections=many))

    def test_exactly_100_detections_allowed(self) -> None:
        """边界值：正好 100 个应通过。"""
        many = [
            {"class": "foam", "confidence": 0.9, "bbox": [i, i, i + 10, i + 10]}
            for i in range(100)
        ]
        event = EventIngest(**valid_payload(detections=many))
        assert len(event.detections) == 100


class TestGeoPoint:
    def test_longitude_bounds(self) -> None:
        with pytest.raises(ValidationError):
            GeoPoint(lng=181, lat=26)
        with pytest.raises(ValidationError):
            GeoPoint(lng=-181, lat=26)

    def test_latitude_bounds(self) -> None:
        with pytest.raises(ValidationError):
            GeoPoint(lng=119, lat=91)
        with pytest.raises(ValidationError):
            GeoPoint(lng=119, lat=-91)

    def test_lianjiang_coordinates_valid(self) -> None:
        """连江沿海坐标必须能通过。"""
        p = GeoPoint(lng=119.6521, lat=26.3864)
        assert p.lng == 119.6521


class TestWasteClassConstants:
    def test_four_classes_defined(self) -> None:
        assert len(WasteClass.ALL) == 4

    def test_high_priority_subset(self) -> None:
        """高优先级类别必须都是合法类别 —— 只有它们触发自动派单。"""
        for cls in WasteClass.HIGH_PRIORITY:
            assert cls in WasteClass.ALL

    def test_foam_is_high_priority(self) -> None:
        """泡沫类是连江最突出的垃圾，必须是高优先级。"""
        assert WasteClass.FOAM in WasteClass.HIGH_PRIORITY

    def test_fishing_gear_is_high_priority(self) -> None:
        assert WasteClass.FISHING_GEAR in WasteClass.HIGH_PRIORITY

    def test_labels_cover_all(self) -> None:
        for cls in WasteClass.ALL:
            assert cls in WasteClass.LABELS, f"类别 {cls} 缺少中文标签"

    def test_class_order_is_frozen(self) -> None:
        """★ 类别顺序即模型输出的类别索引，绝不能改（改了要重训模型）。

        这个顺序必须与 ml/configs/seasight.yaml 的 names 映射完全一致。
        """
        assert WasteClass.ALL == ("foam", "plastic", "fishing_gear", "other")


class TestEventStatus:
    def test_four_statuses(self) -> None:
        assert len(EventStatus.ALL) == 4

    def test_resolved_exists(self) -> None:
        """任务完成时要回写事件为 resolved —— 这个状态不能删。"""
        assert EventStatus.RESOLVED in EventStatus.ALL

    def test_ignored_exists(self) -> None:
        """人工忽略误报需要这个状态；且热力图查询会排除它。"""
        assert EventStatus.IGNORED in EventStatus.ALL
