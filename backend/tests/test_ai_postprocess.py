"""AI 推理服务后处理单元测试。

★ 守三件事：
1. NMS 实现正确（去重干净、不误杀相邻目标）
2. stub 模式确定性（同一张图结果一致 —— 否则平台组写不了断言）
3. **两种导出格式都能解析 + letterbox 还原准确**（见文末 TestPostprocessFormats）
"""

from __future__ import annotations

import hashlib

import pytest

from app.services.ai.server import CLASS_NAMES, Detector, _letterbox, _nms


class TestNMS:
    def test_empty_input(self) -> None:
        assert _nms([], [], 0.5) == []

    def test_single_box_kept(self) -> None:
        keep = _nms([[0, 0, 10, 10]], [0.9], 0.5)
        assert keep == [0]

    def test_identical_boxes_deduped(self) -> None:
        """完全重叠的两个框只保留一个（高分的那个）。"""
        boxes = [[0, 0, 10, 10], [0, 0, 10, 10]]
        scores = [0.9, 0.7]
        keep = _nms(boxes, scores, 0.5)
        assert len(keep) == 1
        assert keep[0] == 0     # 保留高分框（索引 0）

    def test_high_overlap_suppressed(self) -> None:
        """IoU > 阈值应被抑制。"""
        boxes = [[0, 0, 10, 10], [1, 1, 11, 11]]   # IoU ≈ 0.68
        scores = [0.9, 0.8]
        keep = _nms(boxes, scores, 0.5)
        assert len(keep) == 1

    def test_low_overlap_both_kept(self) -> None:
        """IoU < 阈值应都保留 —— 相邻的不同垃圾不能被误杀。"""
        boxes = [[0, 0, 10, 10], [50, 50, 60, 60]]   # IoU = 0
        scores = [0.9, 0.8]
        keep = _nms(boxes, scores, 0.5)
        assert len(keep) == 2

    def test_three_boxes_chain_suppression(self) -> None:
        """三个高度重叠的框 → 只保留最高分那个。

        实测 IoU：A-B=0.68、A-C=0.47、B-C=0.68，阈值取 0.5。
        A 压掉 B，但 A-C=0.47 未过线，所以 C 被保留 —— 结果是 [0, 2]。
        这正是经典 NMS 的既定行为：**只与已保留框比，被压掉的框不再参与**。
        """
        boxes = [[0, 0, 10, 10], [1, 1, 11, 11], [2, 2, 12, 12]]
        scores = [0.9, 0.8, 0.7]
        keep = _nms(boxes, scores, 0.5)
        assert keep == [0, 2], f"实测应为 [0, 2]，实际 {keep}"

    def test_chain_suppression_via_lower_threshold(self) -> None:
        """阈值放低到 0.45 → A 直接压住 B 和 C，只留 1 个。

        这是上一条的对照：证明「链式残留」不是实现 bug，
        而是阈值边界问题 —— 调低阈值即可全压。
        """
        boxes = [[0, 0, 10, 10], [1, 1, 11, 11], [2, 2, 12, 12]]
        scores = [0.9, 0.8, 0.7]
        keep = _nms(boxes, scores, 0.45)
        assert keep == [0], f"阈值 0.45 时应只剩最高分框，实际 {keep}"

    def test_nms_does_not_suppress_distinct_boxes(self) -> None:
        """位置明显分离的三个框（IoU 均 < 0.33）应全部保留。

        实测 IoU：A-B=0.325、A-C=0.047、B-C=0.22，全部低于阈值 0.5。
        这条守住的是「NMS 不能误杀真实目标」—— 相邻但不同的垃圾
        必须各自上报，否则一次识别会少报好几处。
        """
        boxes = [[0, 0, 10, 10], [3, 3, 13, 13], [7, 7, 17, 17]]
        scores = [0.9, 0.8, 0.7]
        keep = _nms(boxes, scores, 0.5)
        assert keep == [0, 1, 2], f"三个弱重叠框都应保留，实际 {keep}"
        assert keep[0] == 0

    def test_keep_order_follows_score_desc(self) -> None:
        """返回顺序应按分数降序（高分在前）。"""
        boxes = [[0, 0, 10, 10], [50, 50, 60, 60], [100, 100, 110, 110]]
        scores = [0.5, 0.9, 0.7]
        keep = _nms(boxes, scores, 0.5)
        assert keep == [1, 2, 0]

    def test_threshold_one_keeps_all(self) -> None:
        """阈值=1 时任何重叠都不超阈 —— 全部保留。"""
        boxes = [[0, 0, 10, 10], [0, 0, 10, 10]]
        scores = [0.9, 0.8]
        keep = _nms(boxes, scores, 1.0)
        assert len(keep) == 2

    def test_threshold_zero_keeps_only_max(self) -> None:
        """阈值=0 时只要有重叠就抑制。"""
        boxes = [[0, 0, 10, 10], [1, 1, 11, 11]]
        keep = _nms(boxes, [0.9, 0.8], 0.0)
        assert len(keep) == 1

    def test_touching_boxes_not_suppressed(self) -> None:
        """仅边界相接（IoU=0）不应被抑制。"""
        boxes = [[0, 0, 10, 10], [10, 0, 20, 10]]
        keep = _nms(boxes, [0.9, 0.8], 0.5)
        assert len(keep) == 2


class TestClassNames:
    def test_four_classes(self) -> None:
        assert len(CLASS_NAMES) == 4

    def test_order_matches_training_config(self) -> None:
        """★ 类别顺序必须与训练配置一致 —— 改了模型输出会全部错位。"""
        assert CLASS_NAMES == ["foam", "plastic", "fishing_gear", "other"]


class TestStubMode:
    def test_model_missing_falls_back_to_stub(self, tmp_path) -> None:
        """模型文件不存在时应降级 stub，而不是抛异常。

        这条保证平台组在没有模型的情况下也能独立调试全链路 ——
        是三组并行开发能成立的关键。
        """
        detector = Detector()
        loaded = detector.load(str(tmp_path / "nonexistent.onnx"))
        assert loaded is False
        assert detector.backend == "stub"
        assert detector.loaded_at is not None

    def test_stub_infer_is_deterministic(self) -> None:
        """同一张图必须返回相同结果 —— 否则平台组写不了断言。"""
        detector = Detector()
        detector.load("/definitely/not/a/real/path.onnx")   # 落入 stub 模式

        image_bytes = b"fake jpeg content for testing"
        first = detector.infer(image_bytes)
        second = detector.infer(image_bytes)
        assert first == second

    def test_stub_infer_different_input_differs(self) -> None:
        """不同输入应返回不同结果（否则 stub 就退化成常量了）。"""
        detector = Detector()
        detector.load("/definitely/not/a/real/path.onnx")

        a = detector.infer(b"image A")
        b = detector.infer(b"image B")
        assert a != b

    def test_stub_returns_at_least_one_detection(self) -> None:
        detector = Detector()
        detector.load("/definitely/not/a/real/path.onnx")

        results = detector.infer(b"some image")
        assert len(results) >= 1
        assert len(results) <= 4

    def test_stub_result_schema_matches_contract(self) -> None:
        """★ stub 返回的结构必须与真实推理一致，否则平台组会被误导。

        必须含 class / confidence / bbox 三个字段，
        与 docs/mqtt-topics.md 的 detections 契约一致。
        """
        detector = Detector()
        detector.load("/definitely/not/a/real/path.onnx")

        for det in detector.infer(b"test image"):
            assert "class" in det
            assert "confidence" in det
            assert "bbox" in det
            assert det["class"] in CLASS_NAMES
            assert 0 <= det["confidence"] <= 1
            assert len(det["bbox"]) == 4

    def test_stub_reproducible_across_instances(self) -> None:
        """跨实例也确定 —— 用实例属性做种子会导致结果不一致。"""
        d1 = Detector()
        d1.load("/not/real.onnx")
        d2 = Detector()
        d2.load("/not/real.onnx")

        payload = b"same image bytes"
        assert d1.infer(payload) == d2.infer(payload)

    def test_stub_class_derived_from_hash(self) -> None:
        """验证类别确实由内容哈希决定（而非随机）。"""
        detector = Detector()
        detector.load("/not/real.onnx")

        payload = b"deterministic test payload"
        digest = int(hashlib.md5(payload).hexdigest()[:8], 16)
        expected_cls = CLASS_NAMES[digest % len(CLASS_NAMES)]

        results = detector.infer(payload)
        assert results[0]["class"] == expected_cls


class TestDetectorState:
    def test_initial_state(self) -> None:
        detector = Detector()
        assert detector.session is None
        assert detector.backend == "stub"
        assert detector.loaded_at is None

    def test_load_records_timestamp(self, tmp_path) -> None:
        detector = Detector()
        detector.load(str(tmp_path / "missing.onnx"))
        assert detector.loaded_at is not None

    def test_reload_switches_backend(self, tmp_path) -> None:
        """重复 load 不应报错（热加载接口会反复调用）。"""
        detector = Detector()
        detector.load(str(tmp_path / "missing.onnx"))
        detector.load(str(tmp_path / "also_missing.onnx"))
        assert detector.backend == "stub"


# ======================================================================
# 后处理格式解析 —— 守的是一类「没有真实模型就永远测不出来」的缺陷
# ======================================================================
np = pytest.importorskip("numpy")


def _raw_output(dets: list[tuple[float, float, float, float, int, float]], n_cls: int = 4):
    """构造 nms=False 导出的原始输出张量 (1, 4+nc, anchors)。

    每项：(cx, cy, w, h, class_id, confidence)

    ★ 锚点数必须**明显大于** 4+nc，否则转置判断会失效。
      真实 YOLO 在 640×640 下有 8400 个锚点，这里用 100 代表。
    """
    anchors = max(len(dets), 100)
    arr = np.zeros((1, 4 + n_cls, anchors), dtype=np.float32)
    for i, (cx, cy, w, h, cid, conf) in enumerate(dets):
        arr[0, 0, i] = cx
        arr[0, 1, i] = cy
        arr[0, 2, i] = w
        arr[0, 3, i] = h
        arr[0, 4 + cid, i] = conf
    return arr


def _nms_baked_output(dets: list[tuple[float, float, float, float, float, int]], n: int | None = None):
    """构造 nms=True（EfficientNMS 烘进图）的输出张量 (1, N, 6)。

    每项：(x1, y1, x2, y2, score, class_id)
    """
    rows = n if n is not None else max(len(dets), 1)
    arr = np.zeros((1, rows, 6), dtype=np.float32)
    for i, (x1, y1, x2, y2, score, cid) in enumerate(dets):
        arr[0, i] = [x1, y1, x2, y2, score, cid]
    return arr


class TestLetterbox:
    """letterbox 参数计算 —— 还原框位置全靠这三个返回值。"""

    def test_16_9_image_pads_vertically(self) -> None:
        """1920×1080 → 640 时宽满高不满，上下各填 140。"""
        from PIL import Image

        img = Image.new("RGB", (1920, 1080))
        canvas, scale, pad_x, pad_y = _letterbox(img, 640)
        assert canvas.size == (640, 640)
        assert scale == pytest.approx(640 / 1920, rel=1e-6)
        assert pad_x == 0
        assert pad_y == 140

    def test_portrait_image_pads_horizontally(self) -> None:
        """1080×1920 → 640 时高满宽不满，左右各填 140。"""
        from PIL import Image

        img = Image.new("RGB", (1080, 1920))
        canvas, scale, pad_x, pad_y = _letterbox(img, 640)
        assert canvas.size == (640, 640)
        assert scale == pytest.approx(640 / 1920, rel=1e-6)
        assert pad_x == 140
        assert pad_y == 0

    def test_square_image_no_padding(self) -> None:
        from PIL import Image

        img = Image.new("RGB", (1000, 1000))
        canvas, scale, pad_x, pad_y = _letterbox(img, 640)
        assert canvas.size == (640, 640)
        assert scale == pytest.approx(0.64, rel=1e-6)
        assert pad_x == 0
        assert pad_y == 0

    def test_already_target_size_is_identity(self) -> None:
        from PIL import Image

        img = Image.new("RGB", (640, 640))
        canvas, scale, pad_x, pad_y = _letterbox(img, 640)
        assert canvas.size == (640, 640)
        assert scale == pytest.approx(1.0)
        assert (pad_x, pad_y) == (0, 0)

    def test_padding_colour_is_114_grey(self) -> None:
        """★ 填充色必须是 114 灰：训练端 Ultralytics 默认值，必须一致。

        换成纯黑会让模型在图像边界看到训练时没见过的分布，
        边缘目标的置信度会莫名偏低 —— 这类问题极难归因。
        """
        from PIL import Image

        img = Image.new("RGB", (1920, 1080), (255, 0, 0))
        canvas, _, _, pad_y = _letterbox(img, 640)
        assert pad_y > 0
        # 取最上面一行中间像素，应落在 padding 区
        assert canvas.getpixel((320, 0)) == (114, 114, 114)


class TestPostprocessRawFormat:
    """nms=False 导出：(1, 4+nc, anchors)，cx/cy/w/h + 类别分数。"""

    def test_single_detection_restored(self) -> None:
        """cx/cy/w/h 应正确转成 x1y1x2y2。"""
        d = Detector()
        # 640 空间中心 (320,320) 尺寸 100×80 → [270,280,370,360]
        out = d._postprocess(
            [_raw_output([(320, 320, 100, 80, 0, 0.9)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert len(out) == 1
        assert out[0]["class"] == "foam"
        assert out[0]["confidence"] == pytest.approx(0.9, abs=1e-4)
        assert out[0]["bbox"] == [270, 280, 370, 360]

    def test_letterbox_restore_applied(self) -> None:
        """★ 必须减掉 padding 再除以 scale —— 少了这步所有框整体平移。

        1920×1080 的图，scale=1/3、pad_y=140。
        640 空间里的框 [270,280,370,360] 应对应到原图的
        [810, 420, 1110, 660]。
        """
        d = Detector()
        out = d._postprocess(
            [_raw_output([(320, 320, 100, 80, 0, 0.9)])],
            1920, 1080, 0.25, 0.45,
            scale=640 / 1920, pad_x=0.0, pad_y=140.0,
        )
        x1, y1, x2, y2 = out[0]["bbox"]
        assert x1 == pytest.approx(810, abs=1)
        assert y1 == pytest.approx(420, abs=1)     # (280-140)/(1/3) = 420
        assert x2 == pytest.approx(1110, abs=1)
        assert y2 == pytest.approx(660, abs=1)     # (360-140)/(1/3) = 660

    def test_below_threshold_dropped(self) -> None:
        d = Detector()
        out = d._postprocess(
            [_raw_output([(320, 320, 100, 80, 0, 0.10)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert out == []

    def test_class_index_maps_to_name(self) -> None:
        """类别索引必须映射成名字，且顺序与训练配置一致。"""
        d = Detector()
        for cid, name in enumerate(CLASS_NAMES):
            out = d._postprocess(
                [_raw_output([(320, 320, 100, 80, cid, 0.9)])],
                640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
            )
            assert out[0]["class"] == name, f"索引 {cid} 应映射到 {name}"

    def test_nms_applied_within_raw_path(self) -> None:
        """nms=False 的路径里必须自己做 NMS，两个重叠框只留一个。"""
        d = Detector()
        out = d._postprocess(
            [_raw_output([
                (320, 320, 100, 100, 0, 0.9),
                (322, 322, 100, 100, 0, 0.8),   # 与上一个高度重叠
            ])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert len(out) == 1
        assert out[0]["confidence"] == pytest.approx(0.9, abs=1e-4)

    def test_transposed_input_handled(self) -> None:
        """输出若已是 (anchors, 4+nc) 也应能正确识别（不能硬转置）。"""
        d = Detector()
        arr = _raw_output([(320, 320, 100, 80, 0, 0.9)]).transpose(0, 2, 1)
        out = d._postprocess(
            [arr], 640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0
        )
        assert len(out) == 1
        assert out[0]["bbox"] == [270, 280, 370, 360]


class TestPostprocessNmsBakedFormat:
    """nms=True 导出：(1, N, 6) = [x1, y1, x2, y2, score, class_id]。

    ★ 这组测试是回归防线。修复前，这类输出会让 _postprocess
      拿 6 个元素当 4+nc 拆，cls_scores 取空 → ValueError。
      因为一直没挂真实模型（走 stub），缺陷长期潜伏。
    """

    def test_nms_baked_no_longer_raises(self) -> None:
        """最核心的一条：不再抛异常。"""
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(100, 200, 300, 400, 0.88, 0)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert len(out) == 1

    def test_xyxy_coords_used_directly(self) -> None:
        """已烘 NMS 的坐标是 x1y1x2y2，不能再当 cx/cy/w/h 换算。"""
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(100, 200, 300, 400, 0.88, 0)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert out[0]["bbox"] == [100, 200, 300, 400]

    def test_multiple_classes_parsed(self) -> None:
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([
                (100, 200, 300, 400, 0.88, 0),    # foam
                (500, 600, 700, 800, 0.77, 1),    # plastic
                (10, 10, 60, 60, 0.66, 2),        # fishing_gear
            ])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert [r["class"] for r in out] == ["foam", "plastic", "fishing_gear"]

    def test_letterbox_restore_in_nms_baked_path(self) -> None:
        """★ 两条路径都必须做 letterbox 还原，不能只有一条做。"""
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(270, 280, 370, 360, 0.88, 0)])],
            1920, 1080, 0.25, 0.45,
            scale=640 / 1920, pad_x=0.0, pad_y=140.0,
        )
        x1, y1, x2, y2 = out[0]["bbox"]
        assert x1 == pytest.approx(810, abs=1)
        assert y1 == pytest.approx(420, abs=1)
        assert x2 == pytest.approx(1110, abs=1)
        assert y2 == pytest.approx(660, abs=1)

    def test_low_score_row_dropped(self) -> None:
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(100, 200, 300, 400, 0.05, 0)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert out == []

    def test_zero_padded_rows_ignored(self) -> None:
        """EfficientNMS 用全零行填充到固定 N，这些行不能被当成目标。

        score=0 低于阈值，所以自然被过滤掉 —— 这条锁住该行为。
        """
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(100, 200, 300, 400, 0.88, 0)], n=5)],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert len(out) == 1


class TestBboxClipping:
    """框必须裁剪进原图范围 —— 契约要求 bbox 是原分辨率像素坐标。"""

    def test_negative_coords_clipped_to_zero(self) -> None:
        """padding 区里的预测还原后会是负数，必须夹到 0。"""
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(-50, -30, 200, 100, 0.9, 0)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert out[0]["bbox"] == [0, 0, 200, 100]

    def test_beyond_image_clipped(self) -> None:
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(500, 500, 2000, 3000, 0.9, 0)])],
            1920, 1080, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert out[0]["bbox"] == [500, 500, 1920, 1080]

    def test_fully_outside_box_discarded(self) -> None:
        """整个框都在画布外 → 裁剪后无面积 → 丢弃，不是返回一个点。"""
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(-100, -100, -50, -50, 0.9, 0)])],
            1920, 1080, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert out == []

    def test_all_output_boxes_within_image(self) -> None:
        """综合：任意输入下，输出框都必须在 [0, 尺寸] 内。"""
        d = Detector()
        cases = [
            (-50, -30, 200, 100),
            (600, 600, 700, 700),
            (100, 100, 300, 300),
            (0, 0, 640, 640),
        ]
        out = d._postprocess(
            [_nms_baked_output([( *c, 0.9, 0) for c in cases])],
            1920, 1080, 0.25, 0.45,
            scale=640 / 1920, pad_x=0.0, pad_y=140.0,
        )
        for r in out:
            x1, y1, x2, y2 = r["bbox"]
            assert 0 <= x1 < x2 <= 1920, r
            assert 0 <= y1 < y2 <= 1080, r


class TestOutputContract:
    """输出必须严格符合 mqtt-topics.md 的 detections 契约。"""

    def test_bbox_is_four_ints(self) -> None:
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(100, 200, 300, 400, 0.8888, 0)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert len(out[0]["bbox"]) == 4
        assert all(isinstance(v, int) for v in out[0]["bbox"]), out[0]["bbox"]

    def test_confidence_in_zero_one(self) -> None:
        d = Detector()
        out = d._postprocess(
            [_nms_baked_output([(100, 200, 300, 400, 0.8888, 0)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert 0.0 <= out[0]["confidence"] <= 1.0

    def test_class_always_in_enum(self) -> None:
        """类别名必须在四类枚举内 —— 契约里 class 是 enum 字段。"""
        d = Detector()
        # class_id=99 越界，应回落 "other" 而不是崩
        out = d._postprocess(
            [_nms_baked_output([(100, 200, 300, 400, 0.9, 99)])],
            640, 640, 0.25, 0.45, scale=1.0, pad_x=0.0, pad_y=0.0,
        )
        assert out[0]["class"] in CLASS_NAMES
