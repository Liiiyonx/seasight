#!/usr/bin/env python3
"""探海灵眸 SeaSight — 相机标定与平面映射工具（WP-13）。

功能：
  1. `chessboard`  —— 棋盘格内参标定（cv2.calibrateCamera）+ 畸变系数 +
                     简单平面映射（棋盘角点像素↔米制平面单应矩阵）。
  2. `plane-map`   —— 由 ≥4 组显式对应点计算平面单应矩阵（cv2.findHomography）。

纪律（冻结）：
  - 本工具是 **OpenCV 传统视觉标定**，不是 YOLO、不是视觉大模型、不是已训练深度模型。
  - **没有可用图像时明确失败（退出码 1），不输出任何默认假参数**。
  - 合成棋盘图 evidence_level 最高 E2；E3 需要 `--real-source`（真实采集说明）；
    E4 一律拒绝（脚本不自动授予）。
  - 报告固定包含：命令、日期、代码版本、输入文件哈希、图像数、可用/跳过数、
    跳过原因、重投影误差、相机矩阵、畸变系数、单应矩阵、evidence_level。

退出码：
  0 = 标定成功
  1 = 标定失败（没有图像 / 可用图像不足 / 无法收敛）——不写输出文件
  2 = 用法或配置错误（pattern 非法、对应点不足、E4 越级等）
  3 = 依赖缺失（opencv-python / numpy）

用法：
  python ml/scripts/calibrate_camera.py chessboard \
      --images <dir_or_glob> --pattern 9x6 --square-size 0.03 \
      --output artifacts/calibration/cam_01.json
  python ml/scripts/calibrate_camera.py plane-map \
      --points correspondences.json --output artifacts/calibration/plane_01.json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
ML = THIS_FILE.parent.parent
ROOT = ML.parent
CODE_TAG = "seasight-wp13-20260919"

IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# evidence 纪律（与 docs/perception-data-protocol.md §4 一致）
ALLOWED_CALIBRATION_EVIDENCE = ("E1", "E2", "E3")  # E3 需 --real-source


class DependencyError(Exception):
    pass


class ConfigError(Exception):
    pass


class CalibrationError(Exception):
    pass


def _check_dependencies() -> dict[str, str]:
    versions: dict[str, str] = {}
    missing: list[str] = []
    try:
        import cv2  # type: ignore[import-not-found]

        versions["cv2"] = cv2.__version__
    except ImportError:
        missing.append("opencv-python-headless（或 opencv-python），提供 cv2")
    try:
        import numpy  # type: ignore[import-not-found]

        versions["numpy"] = numpy.__version__
    except ImportError:
        missing.append("numpy")
    if missing:
        raise DependencyError("缺少依赖：" + "、".join(missing) + "。请先安装再重跑。")
    return versions


def gather_images(source: str) -> list[Path]:
    """解析 --images：目录 → 递归扫描；含通配符 → glob；否则按单文件。"""
    p = Path(source)
    if p.is_dir():
        return sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMG_SUFFIX)
    if any(ch in source for ch in "*?["):
        return sorted(
            Path(f) for f in glob.glob(source) if Path(f).suffix.lower() in IMG_SUFFIX
        )
    if p.is_file() and p.suffix.lower() in IMG_SUFFIX:
        return [p]
    return []


def parse_pattern(text: str) -> tuple[int, int]:
    """'9x6' / '9,6' → (9, 6)（内角点列×行）。"""
    parts = text.lower().replace(",", "x").split("x")
    if len(parts) != 2:
        raise ConfigError(f"pattern 应为 <列>x<行>，如 9x6，实际 {text!r}")
    try:
        cols, rows = (int(v) for v in parts)
    except ValueError:
        raise ConfigError(f"pattern 含非数字: {text!r}") from None
    if cols < 2 or rows < 2:
        raise ConfigError(f"pattern 至少 2x2，实际 {text!r}")
    return cols, rows


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_chessboard_corners(gray, pattern: tuple[int, int]):
    """返回 (cols, rows) 内角点；找不到返回 None。"""
    import cv2

    ret, corners = cv2.findChessboardCorners(gray, pattern, None)
    if not ret:
        return None
    return corners


def calibrate_chessboard(
    image_paths: list[Path],
    pattern: tuple[int, int],
    square_size_m: float,
    evidence_level: str = "E1",
    real_source: str = "",
    max_images: int | None = None,
) -> dict[str, Any]:
    """棋盘格内参 + 畸变 + 平面映射标定。

    没有可用图像（或可用 < 2）→ 抛 CalibrationError，绝不返回默认假参数。
    """
    import cv2
    import numpy as np

    if evidence_level not in ALLOWED_CALIBRATION_EVIDENCE:
        raise ConfigError(
            f"evidence_level={evidence_level!r} 非法；标定允许 {ALLOWED_CALIBRATION_EVIDENCE}"
            "（合成最高 E2；E3 需 --real-source；脚本不授予 E4）"
        )
    if evidence_level == "E3" and not real_source.strip():
        raise ConfigError("E3 需要 --real-source（真实采集说明），否则只能标 E1/E2")
    if square_size_m <= 0:
        raise ConfigError(f"square_size 必须为正数，实际 {square_size_m!r}")
    if max_images is not None:
        image_paths = image_paths[:max_images]

    paths = [p for p in image_paths if p.is_file()]
    if not paths:
        raise CalibrationError(
            f"没有可用标定图像（{len(image_paths)} 个候选均不可读）。"
            "标定必须基于真实图像，拒绝输出默认假参数。"
        )

    cols, rows = pattern
    objp = np.zeros((cols * rows, 3), np.float32)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square_size_m

    obj_points: list[np.ndarray] = []
    img_points: list[np.ndarray] = []
    used: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for p in paths:
        gray = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            skipped.append({"path": str(p), "reason": "无法读取为灰度图"})
            continue
        corners = find_chessboard_corners(gray, pattern)
        if corners is None:
            skipped.append({"path": str(p), "reason": f"未检测到 {cols}x{rows} 棋盘内角点"})
            continue
        obj_points.append(objp)
        img_points.append(corners)
        used.append({"path": str(p), "corners": int(corners.shape[0])})

    if len(used) < 2:
        raise CalibrationError(
            f"可用标定图像 {len(used)} 张 < 2，无法求内参。"
            "标定必须基于足够多视图，拒绝输出默认假参数。"
        )

    try:
        rms, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
            obj_points, img_points, gray.shape[::-1], None, None
        )
    except cv2.error as e:  # type: ignore[attr-defined]
        raise CalibrationError(f"cv2.calibrateCamera 失败（无法收敛）: {e}") from e

    per_image_errors: list[dict[str, Any]] = []
    for i, (p, rvec, tvec) in enumerate(zip(used, rvecs, tvecs)):
        proj, _ = cv2.projectPoints(obj_points[i], rvec, tvec, mtx, dist)
        err = float(cv2.norm(img_points[i], proj, cv2.NORM_L2) / len(proj))
        per_image_errors.append({"image": p["path"], "reprojection_error_px": round(err, 6)})

    # 简单平面映射：取重投影误差最小的视图，像素角点 ↔ 米制平面角点
    homography = None
    plane_residual = None
    if per_image_errors:
        best = min(range(len(per_image_errors)), key=lambda i: per_image_errors[i]["reprojection_error_px"])
        H, res = compute_plane_homography(
            img_points[best].reshape(-1, 2).tolist(),
            obj_points[best][:, :2].tolist(),
        )
        homography = [row for row in H]
        plane_residual = res

    report = {
        "status": "calibrated",
        "command": "calibrate_camera.py chessboard",
        "date": datetime.now(timezone.utc).isoformat(),
        "code_tag": CODE_TAG,
        "evidence_level": evidence_level,
        "real_source": real_source if real_source.strip() else None,
        "pattern": {"cols": cols, "rows": rows},
        "square_size_m": square_size_m,
        "input_files": [{"path": u["path"], "sha256": _sha256(Path(u["path"]))} for u in used],
        "image_count": len(paths),
        "used_images": used,
        "skipped_images": skipped,
        "rms_error_px": round(float(rms), 6),
        "camera_matrix": [[float(v) for v in row] for row in mtx.tolist()],
        "dist_coeffs": [float(v) for v in dist.ravel()],
        "per_image_errors": per_image_errors,
        "homography": homography,
        "plane_residual": plane_residual,
    }
    return report


def compute_plane_homography(src_pts: list[list[float]], dst_pts: list[list[float]]) -> tuple[list[list[float]], dict[str, Any]]:
    """像素坐标 ↔ 平面坐标单应矩阵（cv2.findHomography，最小二乘）。

    返回 (H 3x3, 残差报告)。对应点 < 4 抛 ConfigError。
    """
    import cv2
    import numpy as np

    src = np.asarray(src_pts, dtype=np.float32)
    dst = np.asarray(dst_pts, dtype=np.float32)
    if src.shape != dst.shape or src.shape[0] < 4:
        raise ConfigError(
            f"平面映射至少需要 4 组对应点，实际 {src.shape[0] if src.ndim == 2 else 0} 组"
        )
    H, _mask = cv2.findHomography(src, dst, method=0)  # 最小二乘（非 RANSAC）
    proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
    errs = np.linalg.norm(proj - dst, axis=1)
    return (
        [[float(v) for v in row] for row in H.tolist()],
        {
            "count": int(len(errs)),
            "unit": "plane",  # 残差位于目标坐标空间（平面映射时为米制）
            "mean": round(float(errs.mean()), 6),
            "max": round(float(errs.max()), 6),
        },
    )


def plane_map_from_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    """由显式对应点计算平面单应矩阵；不需要图像。"""
    pairs = []
    for i, pt in enumerate(points):
        pix = pt.get("pixel")
        pla = pt.get("plane")
        if not (isinstance(pix, (list, tuple)) and len(pix) == 2
                and isinstance(pla, (list, tuple)) and len(pla) == 2):
            raise ConfigError(f"对应点 {i} 必须包含 pixel=[x,y] 与 plane=[X,Y]")
        pairs.append(([float(v) for v in pix], [float(v) for v in pla]))
    if len(pairs) < 4:
        raise ConfigError(f"平面映射至少需要 4 组对应点，实际 {len(pairs)} 组")
    H, res = compute_plane_homography([p[0] for p in pairs], [p[1] for p in pairs])
    return {
        "status": "calibrated",
        "command": "calibrate_camera.py plane-map",
        "date": datetime.now(timezone.utc).isoformat(),
        "code_tag": CODE_TAG,
        "homography": H,
        "residual": res,
        "point_count": len(pairs),
        "input_files": [],
    }


def _load_points(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"找不到对应点文件 {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p} 不是合法 JSON: {e}") from e
    pts = data.get("points") if isinstance(data, dict) else data
    if not isinstance(pts, list):
        raise ConfigError(f'{p} 需要 {{"points": [...]}} 结构')
    return pts


def _print_report(r: dict[str, Any]) -> None:
    print(f"状态            : {r['status']}")
    print(f"evidence_level  : {r.get('evidence_level')}")
    if r.get("real_source"):
        print(f"real_source     : {r['real_source']}")
    if r.get("pattern"):
        print(f"棋盘格          : {r['pattern']['cols']}x{r['pattern']['rows']} 内角点，"
              f"方格 {r['square_size_m']} m")
    print(f"图像数          : {r.get('image_count')}，可用 {len(r.get('used_images', []))}，"
          f"跳过 {len(r.get('skipped_images', []))}")
    for s in r.get("skipped_images", []):
        print(f"  - 跳过 {s['path']}: {s['reason']}")
    if r.get("rms_error_px") is not None:
        print(f"重投影误差 RMS  : {r['rms_error_px']} px")
    if r.get("camera_matrix"):
        print(f"相机矩阵        : {r['camera_matrix']}")
    if r.get("dist_coeffs"):
        print(f"畸变系数        : {r['dist_coeffs']}")
    if r.get("per_image_errors"):
        worst = max(r["per_image_errors"], key=lambda e: e["reprojection_error_px"])
        print(f"单图误差范围    : 最大 {worst['reprojection_error_px']} px ({worst['image']})")
    if r.get("homography"):
        print(f"平面单应矩阵    : {r['homography']}")
    if r.get("plane_residual"):
        print(f"平面映射残差    : {r['plane_residual']}")
    if r.get("residual"):
        print(f"平面映射残差    : {r['residual']}")
    print(f"输入文件哈希    : {[f['sha256'][:12] for f in r.get('input_files', [])]}")


def main(argv: list[str] | None = None) -> int:
    try:
        versions = _check_dependencies()
    except DependencyError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 3

    ap = argparse.ArgumentParser(
        prog="calibrate_camera",
        description="SeaSight 相机标定（OpenCV 传统视觉，非训练模型）— WP-13",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_cb = sub.add_parser("chessboard", help="棋盘格内参+畸变+平面映射")
    p_cb.add_argument("--images", required=True, help="图像目录 / glob / 单文件")
    p_cb.add_argument("--pattern", default="9x6", help="内角点 列x行，如 9x6")
    p_cb.add_argument("--square-size", type=float, default=0.03, help="方格边长（米）")
    p_cb.add_argument("--output", required=True, help="标定报告 JSON 输出路径")
    p_cb.add_argument("--evidence-level", default="E1", help="E1|E2|E3（E3 需 --real-source）")
    p_cb.add_argument("--real-source", default="", help="真实采集说明（E3 必填）")
    p_cb.add_argument("--max-images", type=int, default=None)
    p_cb.set_defaults(func=_cmd_chessboard)

    p_pm = sub.add_parser("plane-map", help="由对应点计算平面单应矩阵")
    p_pm.add_argument("--points", required=True, help="对应点 JSON: {\"points\":[{pixel,plane},...]}")
    p_pm.add_argument("--output", required=True)
    p_pm.add_argument("--evidence-level", default="E1")
    p_pm.add_argument("--real-source", default="")
    p_pm.set_defaults(func=_cmd_plane_map)

    args = ap.parse_args(argv)
    return args.func(args, versions)


def _write_output(path: str, report: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _cmd_chessboard(args: argparse.Namespace, versions: dict[str, str]) -> int:
    try:
        pattern = parse_pattern(args.pattern)
    except ConfigError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    images = gather_images(args.images)
    if not images:
        print(
            "[错误] 没有找到任何标定图像。标定必须基于真实图像，"
            "拒绝输出默认假参数。",
            file=sys.stderr,
        )
        return 1
    try:
        report = calibrate_chessboard(
            images,
            pattern,
            args.square_size,
            evidence_level=args.evidence_level,
            real_source=args.real_source,
            max_images=args.max_images,
        )
    except CalibrationError as e:
        print(f"[错误] {e}", file=sys.stderr)
        print(f"[错误] 未写入 {args.output}（不输出假参数）", file=sys.stderr)
        return 1
    except ConfigError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    _write_output(args.output, report)
    print(f"依赖版本        : {versions}")
    _print_report(report)
    print(f"输出            : {args.output}")
    return 0


def _cmd_plane_map(args: argparse.Namespace, versions: dict[str, str]) -> int:
    try:
        if args.evidence_level not in ALLOWED_CALIBRATION_EVIDENCE:
            raise ConfigError(
                f"evidence_level={args.evidence_level!r} 非法；允许 {ALLOWED_CALIBRATION_EVIDENCE}"
            )
        if args.evidence_level == "E3" and not args.real_source.strip():
            raise ConfigError("E3 需要 --real-source（真实采集说明）")
        points = _load_points(args.points)
        report = plane_map_from_points(points)
    except ConfigError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    report["evidence_level"] = args.evidence_level
    report["real_source"] = args.real_source if args.real_source.strip() else None
    _write_output(args.output, report)
    print(f"依赖版本        : {versions}")
    _print_report(report)
    print(f"输出            : {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
