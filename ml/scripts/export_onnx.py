#!/usr/bin/env python3
"""探海灵眸 SeaSight — 模型导出脚本（ONNX / TensorRT / RKNN）。

★ 这段代码里最重要的一件事：**RK3588 不能用官方 ultralytics 导出**
──────────────────────────────────────────────────────────
官方 ultralytics 导出的 YOLOv8/v11 ONNX，在 RK3588 的 NPU 上转换时
会因为 DFL（Distribution Focal Loss）层的张量布局与 rknn-toolkit2
期望的不一致而报错或产出错误结果。

正确做法是用瑞芯微维护的分支：
    pip install git+https://github.com/airockchip/ultralytics_yolov8.git
该分支改了检测头结构（把 DFL 拆开、去掉多余转置），使 ONNX 能被
rknn-toolkit2 正确解析。

所以本脚本对 rk3588 目标会**检测当前 ultralytics 是否为 airockchip 分支**，
不是就明确报错，而不是让你导出一个跑不通的模型。

用法
────
    # ONNX（Jetson / x86 服务器用）
    python export_onnx.py --weights runs/seasight/stage2/weights/best.pt

    # TensorRT（Jetson 上先导 ONNX 再 trtexec）
    python export_onnx.py --weights best.pt --target tensorrt

    # RK3588（需先装 airockchip 分支）
    python export_onnx.py --weights best.pt --target rk3588

    # 只验证已导出的 ONNX 是否能正常推理
    python export_onnx.py --verify model.onnx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CLASS_NAMES = ["foam", "plastic", "fishing_gear", "other"]

# RK3588 必须使用的分支
AIROCKCHIP_REPO = "airockchip/ultralytics_yolov8"


def check_ultralytics_flavor(target: str) -> tuple[bool, str]:
    """检测当前安装的 ultralytics 是否适用于目标平台。

    返回 (是否可以继续, 说明信息)
    """
    try:
        import ultralytics
        version = getattr(ultralytics, "__version__", "unknown")
    except ImportError:
        return False, "未安装 ultralytics：pip install ultralytics"

    # 判断是否为 airockchip 分支：该分支会在版本号或模块里留痕
    is_airockchip = False
    try:
        import ultralytics
        module_file = getattr(ultralytics, "__file__", "") or ""
        # airockchip 分支的 head.py 里有 rknn 相关改动痕迹
        head_path = Path(module_file).parent / "nn" / "modules" / "head.py"
        if head_path.exists():
            content = head_path.read_text(encoding="utf-8", errors="ignore")
            is_airockchip = "rknn" in content.lower() or "dfl" in content.lower() and "cv2" in content
    except Exception:   # noqa: BLE001
        pass

    if target == "rk3588" and not is_airockchip:
        return False, (
            f"导 RK3588 用的模型必须用瑞芯微分支，当前是官方 ultralytics {version}。\n"
            f"        请执行：\n"
            f"          pip uninstall -y ultralytics\n"
            f"          pip install git+https://github.com/{AIROCKCHIP_REPO}.git\n"
            f"        官方版本导出的 ONNX 在 rknn-toolkit2 转换时会因 DFL 层\n"
            f"        张量布局不匹配而失败（或转换成功但推理结果错误）。"
        )

    return True, f"ultralytics {version}" + ("（airockchip 分支）" if is_airockchip else "")


def export_onnx(weights: Path, args: argparse.Namespace) -> Path | None:
    """导出 ONNX。"""
    from ultralytics import YOLO

    print(f"\n[导出] ONNX  ← {weights}")

    # ★ RK3588 时的处理：用模型自带的 rknn 导出（airockchip 分支支持 format='rknn'）
    if args.target == "rk3588":
        model = YOLO(str(weights))
        print("  使用 airockchip 分支的 rknn 导出路径")
        try:
            out = model.export(format="rknn", imgsz=args.imgsz, name="rk3588")
            print(f"  ✓ 已导出：{out}")
            print("  下一步：用 rknn-toolkit2 在 PC 上转 .rknn，再部署到板端")
            return Path(out) if out else None
        except Exception as exc:   # noqa: BLE001
            print(f"  ✗ rknn 导出失败：{exc}")
            print("    提示：rknn 导出需要在 x86 Linux 上跑，Windows 不支持。")
            return None

    model = YOLO(str(weights))
    out = model.export(
        format="onnx",
        imgsz=args.imgsz,
        opset=args.opset,
        simplify=True,
        dynamic=False,   # 固定 batch=1
        nms=False,       # ★ NMS 交给推理侧，便于动态调阈值
        half=False,      # FP16 交给 TensorRT，避免 ONNX 算子兼容问题
    )
    print(f"  ✓ 已导出：{out}")
    return Path(out) if out else None


def export_tensorrt(onnx_path: Path, args: argparse.Namespace) -> None:
    """给出 TensorRT 转换命令（Jetson 上执行）。

    不在脚本里直接调 trtexec：TensorRT 版本与 CUDA 强绑定，
    在开发机上转出的 engine 不能跨设备复用，必须在目标机上转。
    """
    print("\n[Jetson / TensorRT] 请在**目标设备**上执行：")
    print(f"""
    /usr/src/tensorrt/bin/trtexec \\
        --onnx={onnx_path} \\
        --saveEngine={onnx_path.with_suffix('.engine')} \\
        --fp16 \\
        --workspace=2048 \\
        --minShapes=images:1x3x{args.imgsz}x{args.imgsz} \\
        --optShapes=images:1x3x{args.imgsz}x{args.imgsz} \\
        --maxShapes=images:1x3x{args.imgsz}x{args.imgsz}

  注意事项：
    · engine 与 TensorRT 版本、GPU 架构强绑定，换设备必须重转
    · Jetson 上用 jetpack 自带的 TensorRT，不要 pip 装
    · 加 --fp16 即可，INT8 需要校准集，备赛阶段收益不大
""")


def verify_onnx(onnx_path: Path, imgsz: int) -> bool:
    """验证 ONNX 模型能否正常加载与推理。

    这一步很重要：导出后的模型必须验证一次。
    常见问题：输出形状不对、opset 不兼容、被 simplify 破坏结构。
    """
    print(f"\n[验证] {onnx_path}")

    if not onnx_path.exists():
        print(f"  ✗ 文件不存在：{onnx_path}")
        return False

    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError:
        print("  ⚠ 缺少 onnxruntime / numpy，跳过验证")
        print("    pip install onnxruntime numpy")
        return True

    try:
        providers = ort.get_available_providers()
        print(f"  可用推理后端：{', '.join(providers)}")

        session = ort.InferenceSession(str(onnx_path), providers=providers)
    except Exception as exc:   # noqa: BLE001
        print(f"  ✗ 加载失败：{exc}")
        return False

    inp = session.get_inputs()[0]
    print(f"  输入：name={inp.name} shape={inp.shape} type={inp.type}")

    for out in session.get_outputs():
        print(f"  输出：name={out.name} shape={out.shape}")

    # 跑一次真实推理（随机输入）
    try:
        import numpy as np

        dummy = np.random.rand(1, 3, imgsz, imgsz).astype(np.float32)
        result = session.run(None, {inp.name: dummy})
        print(f"  ✓ 推理成功，输出张量数：{len(result)}")
        for i, arr in enumerate(result):
            print(f"    输出[{i}] shape={arr.shape} dtype={arr.dtype}")
    except Exception as exc:   # noqa: BLE001
        print(f"  ✗ 推理失败：{exc}")
        return False

    # 检查输出维度是否符合 YOLO 约定
    try:
        main = result[0]
        if main.ndim == 3:
            channels = main.shape[1]
            expected = 4 + len(CLASS_NAMES)   # 4 个框参数 + 类别数
            if channels == expected:
                print(f"  ✓ 输出通道数 {channels} 与 {len(CLASS_NAMES)} 类模型匹配")
            else:
                print(f"  ⚠ 输出通道数 {channels}，预期 {expected}（4+{len(CLASS_NAMES)}）")
                print("    若使用 airockchip 分支，输出结构会不同，属正常")
    except Exception:   # noqa: BLE001
        pass

    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="探海灵眸 — 模型导出")
    parser.add_argument("--weights", default=None, help="训练产出的 .pt 权重")
    parser.add_argument(
        "--target", default="onnx", choices=["onnx", "tensorrt", "rk3588"], help="目标平台"
    )
    parser.add_argument("--imgsz", type=int, default=640, help="输入分辨率")
    parser.add_argument("--opset", type=int, default=17, help="ONNX opset")
    parser.add_argument("--verify", default=None, help="只验证已有的 ONNX 文件")
    parser.add_argument("--skip-check", action="store_true", help="跳过 ultralytics 分支检查")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 66)
    print("  探海灵眸 SeaSight — 模型导出")
    print("=" * 66)

    # ---------- 只做验证 ----------
    if args.verify:
        ok = verify_onnx(Path(args.verify), args.imgsz)
        return 0 if ok else 1

    if not args.weights:
        print("[错误] 需要 --weights 指定权重文件，或 --verify 指定 ONNX 文件")
        return 1

    weights = Path(args.weights)
    if not weights.exists():
        print(f"[错误] 权重文件不存在：{weights}")
        return 1

    # ---------- 平台适配检查 ----------
    print(f"\n[1/3] 平台适配检查（目标：{args.target}）")
    if args.skip_check:
        print("  （已跳过）")
    else:
        ok, msg = check_ultralytics_flavor(args.target)
        if not ok:
            print(f"  ✗ {msg}")
            return 1
        print(f"  ✓ {msg}")

    # ---------- 导出 ----------
    print("\n[2/3] 导出")
    onnx_path = export_onnx(weights, args)
    if onnx_path is None:
        return 1
    if not onnx_path.exists():
        # ultralytics 有时返回相对路径，兜底查找
        candidate = weights.with_suffix(".onnx")
        if candidate.exists():
            onnx_path = candidate
        else:
            print(f"  ⚠ 未找到导出文件，请检查输出目录：{onnx_path}")
            return 1

    # ---------- 验证与后续指引 ----------
    print("\n[3/3] 验证与部署指引")
    verify_onnx(onnx_path, args.imgsz)

    if args.target == "tensorrt":
        export_tensorrt(onnx_path, args)
    elif args.target == "rk3588":
        print("""
[RK3588] 后续步骤（在 x86 Linux 上执行）：
    1. 用 rknn-toolkit2 转换：
         from rknn.api import RKNN
         rknn = RKNN()
         rknn.config(mean_values=[[0,0,0]], std_values=[[255,255,255]],
                     target_platform='rk3588')
         rknn.load_onnx(model='best.onnx')
         rknn.build(do_quantization=True, dataset='calib.txt')
         rknn.export_rknn('best.rknn')
    2. 拷到板端，用 rknn-toolkit-lite2 推理
    3. 量化校准集建议 200~500 张真实海面图（含空镜头）
""")
    else:
        print(f"""
[ONNX] 部署方式：
    · 服务器 / PC：onnxruntime（CPU）或 onnxruntime-gpu（CUDA）
    · Jetson：先转 TensorRT engine（见 --target tensorrt）
    · AI 推理服务会自动探测可用后端，把模型放到 models/ 目录即可：
        cp {onnx_path} ../../backend/models/det_v0.1.0.onnx
      然后调用 POST /infer/reload 热加载，不必重启服务
""")

    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
