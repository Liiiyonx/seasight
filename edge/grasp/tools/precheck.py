"""启动自检：上真机前，把「必须为真的四件事」逐项验一遍。

## 为什么单独写一个

现场最贵的成本是「以为链路通了，其实卡在某一步」：作业台起来了、日志也没报错，
机械臂却什么都不做 —— 因为标定文件没生成、或者相机根本没出图。本脚本把这四个
前置条件摊开明说，缺哪一项、怎么补，一眼可见。

## 检查项与口径

1. **配置** —— config.yaml 能读、site/device/输入边长打印出来
2. **模型** —— active 目录里的 best.onnx / best.ncnn / best.pt 能真正 load
   （★ 即使 ``--mock`` 也按**真模型**检查，别让合成模式掩盖"没部署模型"）
3. **标定** —— calibration.json 存在、``ready``、点数与 RMSE（真机为必需项）
4. **放置点** —— 料槽放置点已示教（真机为必需项）
   （★ 与标定并列的原因：闭环最后一步是"放进槽里"，没设放置点时闭环直接拒绝
   开工 —— 又是一个"以为能跑、其实第一步就停"的高发点）
5. **相机 / ROS** —— ArmIo 能起来，且若干秒内真的拿到一帧画面

本脚本**只读**：不训练、不部署、不动机械臂、不写任何文件。

## 用法

::

    python edge/grasp/tools/precheck.py            # 真机（先 source 厂商工作空间）
    python edge/grasp/tools/precheck.py --mock     # 单机自检（合成画面，不碰 ROS）

退出码：全部必需项通过 0；有必需项未过 1。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GRASP = HERE.parent
EDGE = GRASP.parent
for _p in (str(EDGE), str(GRASP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grasp.arm_io import build_arm_io                             # noqa: E402
from grasp.calibration import (                                   # noqa: E402
    DEFAULT_CALIBRATION_PATH,
    describe_place,
    load_calibration,
)
from grasp.config import load_config                              # noqa: E402
from grasp.vision.detector import Detector                       # noqa: E402

FRAME_WAIT_S = 8.0


def check_config(cfg: dict[str, Any]) -> tuple[bool, str]:
    web = cfg.get("web", {}) or {}
    vision = cfg.get("vision", {}) or {}
    return True, (f"site={cfg.get('site_id')} device={cfg.get('device_id')} "
                  f"input={vision.get('input_size')} conf={vision.get('conf_threshold')} "
                  f"web.mock={web.get('mock')}")


def check_model(cfg: dict[str, Any]) -> tuple[bool, str]:
    det = Detector(cfg, mock=False)          # 永远按真模型查，不受 --mock 影响
    backend = det.load()
    if backend == "none":
        return False, ("active 目录里没有 best.onnx / best.ncnn / best.pt"
                       " —— 先在作业台「4 · 部署与回滚」部署一次模型")
    return True, f"后端 {backend} · {det.model_path} · conf {det.conf_threshold}"


def check_calibration() -> tuple[bool, str]:
    path = Path(DEFAULT_CALIBRATION_PATH)
    if not path.exists():
        return False, (f"{path} 不存在 —— 打开作业台 http://<IP>:8090/#calib "
                       f"跑一次「开始标定」并「保存标定」")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"{path} 读不出来：{exc}"
    cal = load_calibration()
    pairs = list(raw.get("pairs") or [])
    rmse = raw.get("rmse")
    if not cal.ready:
        return False, f"只有 {len(pairs)} 个点（仿射标定至少要 3 个），未就绪"
    # ★ RMSE 恰好为 0 是 runtime 演示标定（demo_calibration）的指纹：真机上
    #   9 个点用检测框中心拟合，不可能一点误差都没有。这条兜底是为了挡住
    #   「在 mock 模式点过保存标定、又把整个目录拷到树莓派」这种局面 ——
    #   那种文件 ready=True，光看 ready 会误判为已标定。
    if rmse is not None and float(rmse) < 1e-9:
        return False, (f"{len(pairs)} 点但 RMSE 恰为 0 —— 疑似演示标定（非真机标定）。"
                       f"请在真机上重跑 9 点标定")
    rmse_txt = "未拟合" if rmse is None else f"{float(rmse) * 1000:.1f} mm"
    z = float(cal.plane_z)
    z_txt = f"水面 z={z:.3f}m"
    # ★ 船上甲板高于水面 → 水面 z 应为负值。为 0 或正时提醒核对（不是硬失败：
    #   台面联调时它就是正的），但这个值错了爪子一定压不到袋子。
    if z >= 0:
        z_txt += "（★ 船上甲板高于水面，z 应为负值，请实测核对）"
    return True, f"{len(pairs)} 点 · RMSE {rmse_txt} · {z_txt}"


def check_place(cfg: dict[str, Any], mock: bool) -> tuple[bool, str]:
    """放置点（臂座旁边的料槽）。

    ★ 为什么和标定并列成必需项：抓取闭环的最后一步就是"放进槽里"。没有放置点
      时闭环**直接拒绝开工**（夹起来没地方放比不抓更糟），所以它和标定一样是
      「以为能跑、其实第一步就停」的高发点。
    """
    place = (load_calibration().place) or None
    desc = describe_place(place)
    if not desc["ready"]:
        # 退回 config 兜底值（agent.place.coord / pose）
        agent = cfg.get("agent", {}) or {}
        fallback = agent.get("place") or {}
        if isinstance(fallback.get("coord"), (list, tuple)) and len(fallback["coord"]) >= 2:
            desc = describe_place({"coord": list(fallback["coord"]), "source": "config"})
        elif isinstance(fallback.get("pose"), dict) and fallback["pose"]:
            desc = describe_place({"pose": fallback["pose"], "source": "config"})
    if not desc["ready"]:
        if mock:
            # mock 下闭环会自动装演示放置点（同演示标定），离线联调可用
            return True, "mock 模式用演示放置点（合成值）· 真机必须示教一次"
        return False, ("未设置料槽放置点 —— 抓取闭环会拒绝开工。"
                       "打开作业台 http://<IP>:8090/#calib，用「手动点动」把臂摆到槽口"
                       "上方后点「用当前姿态作放置点」，或填 config.yaml 的 agent.place.coord")
    if desc["source"] == "demo":
        if mock:
            return True, "演示放置点（mock 合成值）· 离线联调可用，真机必须重新示教"
        return False, ("标定文件里是**演示放置点**（source=demo）—— 真机上必须重新示教："
                       "作业台「手动点动」摆到槽口上方 → 「用当前姿态作放置点」")
    if desc["kind"] == "coord":
        t = desc["target"]
        return True, (f"坐标 ({float(t[0]):.3f}, {float(t[1]):.3f}) m · 来源 {desc['source']}"
                      + (f" · {desc['saved_at']}" if desc.get("saved_at") else ""))
    return True, f"示教位姿 {len(desc['target'])} 个舵机 · 来源 {desc['source']}"


def check_camera(cfg: dict[str, Any], mock: bool) -> tuple[bool, str]:
    # ★ 同 supervisor：远端模式下自检也要走板上代理，否则开发机上永远 ✗ 相机/ROS
    io = build_arm_io(cfg, mock=mock)
    try:
        io.start()
    except Exception as exc:                             # noqa: BLE001
        return False, f"ArmIo 起不来：{exc}"
    try:
        if not mock and getattr(io, "ros_error", None):
            return False, f"ROS 链路不可用：{io.ros_error}"
        deadline = time.time() + FRAME_WAIT_S
        while time.time() < deadline:
            frame = io.latest_frame()
            if frame is not None:
                h, w = frame.shape[:2]
                return True, f"{w}×{h} · 话题 {io.camera_topic}"
            time.sleep(0.25)
        return False, (f"{FRAME_WAIT_S:.0f}s 内没拿到画面 —— 相机没出图 / "
                       f"话题 {io.camera_topic} 不对 / 没 source 厂商工作空间")
    finally:
        io.stop()


def check_training_deps() -> tuple[bool, str]:
    """现场再训练/免训练校准的依赖。

    ★ 为什么单独列一项：作业台「3 训练任务」的 local / lan 档与「免训练校准」
      都建立在 ultralytics 上（`training_available()` 为 False 时接口直接 503）。
      这是"把模型特化到演示现场"这条路的前提，最容易被漏装而到现场才发现。
    """
    from grasp.vision.train import TIERS, training_available

    if training_available():
        return True, f"ultralytics 可用 · 档位 {sorted(TIERS)}"
    return False, ("未安装 ultralytics —— 「3 训练任务」的 local/lan 档与"
                   "「免训练校准」都会返回 503。现场特化前先装："
                   "pip install ultralytics（或只做自动拾取可用 --runtime-only 跳过本项检查）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Oceanus 机械臂启动自检（只读）")
    parser.add_argument("--config", default=None, help="默认读 edge/grasp/config.yaml")
    parser.add_argument("--mock", action="store_true",
                        help="合成画面，不碰 ROS（在笔记本上验证脚本本身用）")
    parser.add_argument("--runtime-only", action="store_true",
                        help="只检查自动拾取所需项，跳过「训练依赖」")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[✗] 配置      {exc}", file=sys.stderr)
        return 1

    mock = bool(args.mock or (cfg.get("web", {}) or {}).get("mock"))

    rows: list[tuple[str, bool, str, bool]] = []   # (名称, 通过, 说明, 是否必需)
    for name, fn, required in (
        ("配置", lambda: check_config(cfg), True),
        ("模型", lambda: check_model(cfg), True),
        ("标定", check_calibration, not mock),
        ("放置点", lambda: check_place(cfg, mock), not mock),
        ("相机/ROS", lambda: check_camera(cfg, mock), True),
        ("训练依赖", check_training_deps, not args.runtime_only),
    ):
        try:
            ok, detail = fn()
        except Exception as exc:                              # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        rows.append((name, ok, detail, required))

    print(f"\nOceanus 机械臂 · 启动自检（{'mock 合成模式' if mock else '真机模式'}）")
    print("─" * 62)
    failed_required: list[str] = []
    for name, ok, detail, required in rows:
        mark = "✓" if ok else "✗"
        tail = "" if required or ok else "（非必需项：本模式下不阻塞）"
        print(f"[{mark}] {name:<8} {detail}{tail}")
        if not ok and required:
            failed_required.append(name)
    print("─" * 62)

    if failed_required:
        print(f"未通过：{'、'.join(failed_required)} —— 按上面的提示补齐后重跑本脚本")
        return 1
    print("全部必需项通过。自动拾取可起 supervisor（常驻自捡）或 arm_bridge（派单驱动），"
          "注意二者互斥。")
    print("要把模型特化到演示现场，走作业台：1 采集 → 2 标注 → 3 「一键场景校准」"
          "（秒级）或 local 档微调 → 4 部署。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
