"""SeaSight 相机标定与平面映射测试（WP-13）。

覆盖：
  - 没有图像 / 可用图像不足 → 明确失败，不输出默认假参数
  - 合成棋盘格多视图 → 内参 / 畸变 / 重投影误差 / 平面单应矩阵
  - 平面映射：≥4 组对应点；不足 4 组报错
  - 证据纪律：E4 拒绝；E3 需 --real-source
  - CLI 退出码与「失败不写输出文件」

全部离线：棋盘图由 OpenCV 在测试内绘制生成，不访问公网、不调用模型。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml" / "scripts"))

import calibrate_camera as cc  # noqa: E402


# ----------------------------------------------------------------------
# 合成棋盘视图生成（OpenCV 绘制，非训练模型）
# ----------------------------------------------------------------------

def _base_chessboard(cols: int, rows: int, square_px: int) -> np.ndarray:
    w = (cols + 1) * square_px
    h = (rows + 1) * square_px
    base = np.zeros((h, w), np.uint8)
    for i in range(rows + 1):
        for j in range(cols + 1):
            if (i + j) % 2 == 0:
                base[i * square_px:(i + 1) * square_px, j * square_px:(j + 1) * square_px] = 255
    return base


def _warped_view(base: np.ndarray, seed: int, margin: float = 0.25,
                 amp_frac: float = 0.15) -> np.ndarray:
    """棋盘放在带白色留白的画布上，做轻微透视扰动 → 内角点可稳定检出。"""
    import cv2

    rng = np.random.RandomState(seed)
    h, w = base.shape
    canvas_h, canvas_w = int(h * (1 + 2 * margin)), int(w * (1 + 2 * margin))
    canvas = np.full((canvas_h, canvas_w), 255, np.uint8)
    ox, oy = int(w * margin), int(h * margin)
    canvas[oy:oy + h, ox:ox + w] = base
    src = np.float32([[ox, oy], [ox + w - 1, oy], [ox + w - 1, oy + h - 1], [ox, oy + h - 1]])
    amp = amp_frac * min(w, h)
    dst = np.float32([
        [ox + rng.uniform(-amp, amp), oy + rng.uniform(-amp, amp)],
        [ox + w - 1 + rng.uniform(-amp, amp), oy + rng.uniform(-amp, amp)],
        [ox + w - 1 + rng.uniform(-amp, amp), oy + h - 1 + rng.uniform(-amp, amp)],
        [ox + rng.uniform(-amp, amp), oy + h - 1 + rng.uniform(-amp, amp)],
    ])
    H = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(canvas, H, (canvas_w, canvas_h), borderValue=255)


def make_synthetic_views(tmp_path: Path, n: int = 6, pattern=(9, 6),
                         square_px: int = 60) -> list[Path]:
    import cv2

    cols, rows = pattern
    base = _base_chessboard(cols, rows, square_px)
    paths: list[Path] = []
    img_dir = tmp_path / "chess"
    img_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        view = _warped_view(base, seed=100 + i)
        p = img_dir / f"view_{i:02d}.png"
        cv2.imwrite(str(p), view)
        paths.append(p)
    return paths


# ----------------------------------------------------------------------
# 失败路径：没有图像 / 图像不足
# ----------------------------------------------------------------------

def test_no_images_fails_explicitly(tmp_path: Path) -> None:
    """没有图像 → CalibrationError，绝不返回默认假参数。"""
    with pytest.raises(cc.CalibrationError, match="没有可用标定图像"):
        cc.calibrate_chessboard([], pattern=(9, 6), square_size_m=0.03)


def test_single_image_fails(tmp_path: Path) -> None:
    """只有 1 张可用视图 → 无法求内参，明确失败。"""
    paths = make_synthetic_views(tmp_path, n=1)
    with pytest.raises(cc.CalibrationError, match="< 2"):
        cc.calibrate_chessboard(paths, pattern=(9, 6), square_size_m=0.03)


def test_cli_no_images_returns_1_and_writes_nothing(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "calib.json"
    rc = cc.main(["chessboard", "--images", str(empty),
                  "--pattern", "9x6", "--square-size", "0.03",
                  "--output", str(out)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "拒绝输出默认假参数" in err or "没有找到任何标定图像" in err
    assert not out.exists(), "失败时不得写出标定文件（不输出假参数）"


def test_parse_pattern() -> None:
    assert cc.parse_pattern("9x6") == (9, 6)
    assert cc.parse_pattern("9,6") == (9, 6)
    with pytest.raises(cc.ConfigError):
        cc.parse_pattern("9")
    with pytest.raises(cc.ConfigError):
        cc.parse_pattern("1x1")


# ----------------------------------------------------------------------
# 合成棋盘格标定
# ----------------------------------------------------------------------

def test_synthetic_chessboard_calibration(tmp_path: Path) -> None:
    paths = make_synthetic_views(tmp_path, n=6)
    report = cc.calibrate_chessboard(
        paths, pattern=(9, 6), square_size_m=0.03, evidence_level="E1",
    )
    assert report["status"] == "calibrated"
    assert report["evidence_level"] == "E1"
    assert report["image_count"] == 6
    assert report["used_images"], "应有可用棋盘视图"
    # 输入文件哈希
    assert report["input_files"]
    assert all(len(f["sha256"]) == 64 for f in report["input_files"])
    # 相机矩阵
    mtx = np.asarray(report["camera_matrix"], dtype=float)
    assert mtx.shape == (3, 3)
    assert np.all(np.isfinite(mtx))
    assert mtx[0][0] > 100.0          # 焦距为正且合理
    assert mtx[2][2] == pytest.approx(1.0)
    # 畸变系数
    dist = np.asarray(report["dist_coeffs"], dtype=float)
    assert dist.size == 5
    assert np.all(np.isfinite(dist))
    # 重投影误差：合成视图为同一平面的轻微透视，RMS 有限且合理（非亚像素要求）
    assert 0.0 <= report["rms_error_px"] <= 20.0
    assert len(report["per_image_errors"]) == len(report["used_images"])
    # 平面映射（米制）
    assert report["homography"] is not None
    H = np.asarray(report["homography"], dtype=float)
    assert H.shape == (3, 3)
    assert report["plane_residual"]["count"] == 9 * 6
    assert report["plane_residual"]["max"] < 1e-2  # 米制残差


def test_evidence_level_gating(tmp_path: Path) -> None:
    paths = make_synthetic_views(tmp_path, n=3)
    # E4 一律拒绝
    with pytest.raises(cc.ConfigError, match="E4"):
        cc.calibrate_chessboard(paths, (9, 6), 0.03, evidence_level="E4")
    # E3 必须真实来源说明
    with pytest.raises(cc.ConfigError, match="E3"):
        cc.calibrate_chessboard(paths, (9, 6), 0.03, evidence_level="E3")
    # 合成棋盘最多 E2：E2 合法
    r = cc.calibrate_chessboard(paths, (9, 6), 0.03, evidence_level="E2")
    assert r["status"] == "calibrated"


def test_plane_homography_from_points(tmp_path: Path) -> None:
    # 已知变换：pixel = plane * scale + offset（4+ 组对应点）
    scale, ox, oy = 50.0, 30.0, 20.0
    plane_pts = [(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)]
    pixel_pts = [(p[0] * scale + ox, p[1] * scale + oy) for p in plane_pts]
    points = [
        {"pixel": list(pix), "plane": list(pla)}
        for pix, pla in zip(pixel_pts, plane_pts)
    ]
    r = cc.plane_map_from_points(points)
    assert r["status"] == "calibrated"
    assert r["point_count"] == 5
    H = np.asarray(r["homography"], dtype=float)
    # 训练点残差应极小（仿射精确解）
    assert r["residual"]["max"] < 1e-4


def test_plane_homography_requires_4_points() -> None:
    with pytest.raises(cc.ConfigError, match="至少需要 4 组"):
        cc.plane_map_from_points(
            [{"pixel": [0, 0], "plane": [0, 0]},
             {"pixel": [1, 0], "plane": [1, 0]},
             {"pixel": [0, 1], "plane": [0, 1]}]
        )


def test_cli_chessboard_writes_output(tmp_path: Path) -> None:
    paths = make_synthetic_views(tmp_path, n=6)
    out = tmp_path / "calib.json"
    rc = cc.main(["chessboard", "--images", str(paths[0].parent),
                  "--pattern", "9x6", "--square-size", "0.03",
                  "--evidence-level", "E1",
                  "--output", str(out)])
    assert rc == 0
    assert out.exists()
    import json

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "calibrated"
    assert report["evidence_level"] == "E1"
    assert report["rms_error_px"] >= 0
    assert report["camera_matrix"][2][2] == 1.0


def test_cli_plane_map(tmp_path: Path) -> None:
    pts_file = tmp_path / "points.json"
    pts_file.write_text(
        '{"points": ['
        '{"pixel": [0, 0], "plane": [0, 0]},'
        '{"pixel": [50, 0], "plane": [1, 0]},'
        '{"pixel": [50, 50], "plane": [1, 1]},'
        '{"pixel": [0, 50], "plane": [0, 1]}'
        "]}",
        encoding="utf-8",
    )
    out = tmp_path / "plane.json"
    rc = cc.main(["plane-map", "--points", str(pts_file), "--output", str(out)])
    assert rc == 0
    import json

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "calibrated"
    assert report["evidence_level"] == "E1"
    assert report["point_count"] == 4


def test_cli_plane_map_rejects_e4(tmp_path: Path) -> None:
    pts_file = tmp_path / "points.json"
    pts_file.write_text(
        '{"points": [{"pixel": [0, 0], "plane": [0, 0]},'
        '{"pixel": [1, 0], "plane": [1, 0]},'
        '{"pixel": [1, 1], "plane": [1, 1]},'
        '{"pixel": [0, 1], "plane": [0, 1]}]}',
        encoding="utf-8",
    )
    out = tmp_path / "plane.json"
    rc = cc.main(["plane-map", "--points", str(pts_file), "--output", str(out),
                  "--evidence-level", "E4"])
    assert rc == 2
    assert not out.exists()
