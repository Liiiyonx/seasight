#!/usr/bin/env python3
"""在树莓派上实测厂商逆运动学（**纯计算，不驱动任何关节**）。

★ 安全边界
----------
本脚本只调用厂商 IK 的**纯计算接口**：
  - ``ik.getRotationAngle(coords, alpha)``  返回角度，不下发
  - ``ik.getLinkLength()``                   返回连杆尺寸
  - ``ArmIK.setPitchRanges(...)``            返回 (result, servos, alpha)
它**不** import rospy、**不**创建 publisher、**不**调用 bus_servo_control。
厂商原版示例会驱动舵机，本脚本把那部分全部去掉了。

用途
----
第 13 项「标定可信抓取姿态」里，人工示教（把机械臂摆到对的位置）必须人做，
但**"某个坐标能不能反解出可达姿态"是纯离线计算** —— 本脚本就把这一半做掉，
让人工示教时不必盲试：先离线筛出可达候选，人再从中挑一个摆出来。

厂商源码位置：``~/armpi_fpv/src/armpi_fpv_kinematics/kinematics/``
  - ``ik_transform.py``      Python 封装（本脚本的依据）
  - ``inverse_kinematics.so``  C++ 闭式解核心（**闭式精确解，无迭代**）
"""

import json
import math
import sys

# 厂商的 Python 封装会 import numpy 与 inverse_kinematics（本地 C++ 扩展），
# 所以必须从树莓派上跑，且要把 kinematics 目录放进 sys.path。
sys.path.insert(0, "/home/ubuntu/armpi_fpv/src/armpi_fpv_kinematics/kinematics")

import inverse_kinematics  # noqa: E402  厂商 C++ 扩展

IK = inverse_kinematics.IK()


def dump_link_length():
    print("=== 连杆尺寸（厂商实测值，非标称）===")
    try:
        ll = IK.getLinkLength()
        print("  getLinkLength() =", ll)
        # 常见返回是 dict；逐项打印
        if isinstance(ll, dict):
            for k, v in sorted(ll.items()):
                print("    %-12s %s m" % (k, v))
        return ll
    except Exception as exc:  # noqa: BLE001
        print("  读取失败:", type(exc).__name__, exc)
        return None


def dump_one(coords, alpha):
    """单个 (坐标, 俯仰角) 的反解。纯计算。"""
    try:
        r = IK.getRotationAngle(coords, alpha)
        return r
    except Exception as exc:  # noqa: BLE001
        return {"_error": "%s: %s" % (type(exc).__name__, exc)}


def main():
    print("厂商逆运动学离线验证（**不驱动任何关节**）")
    print("=" * 66)
    link = dump_link_length()

    # ── 1. 厂商示例坐标作为 sanity check ──
    # 厂商 ik_transform.py 的 __main__ 用的是 (0, 0.26, 0.04)，能解出即说明
    # 本机 IK 与厂商一致（我们用的是同一个 .so）。
    print()
    print("=== 1) 厂商示例坐标 sanity check ===")
    vendor = (0, 0.26, 0.04)
    r = dump_one(vendor, 0)
    print("  coords=%s alpha=0" % (vendor,))
    if r and not r.get("_error") and r.get("theta3") is not None:
        print("    ✓ 有解: theta3=%.4f theta4=%.4f theta5=%.4f theta6=%.4f"
              % (r["theta3"], r["theta4"], r["theta5"], r["theta6"]))
    else:
        print("    ✗ 无解或报错:", r)

    # ── 2. 网格扫描：哪些坐标是可达的 ──
    # 抓取姿态一般在这个量级（厂商示例 y=0.26 是前向伸展）。
    # 步长取 2cm / 5°，覆盖作业区，全部是纯计算。
    print()
    print("=== 2) 网格扫描（找可达区域）===")
    xs = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10]
    ys = [0.20, 0.22, 0.24, 0.26, 0.28, 0.30]
    zs = [0.0, 0.02, 0.04, 0.06]
    alphas = [-90, -60, -45, -30, 0, 30, 45, 60, 90]

    total = 0
    ok = 0
    rows = []
    for x in xs:
        for y in ys:
            for z in zs:
                for a in alphas:
                    total += 1
                    r = dump_one((x, y, z), a)
                    if r and not r.get("_error") and r.get("theta3") is not None:
                        ok += 1
                        rows.append({
                            "coords": [x, y, z], "alpha": a,
                            "theta": [round(r["theta3"], 4), round(r["theta4"], 4),
                                      round(r["theta5"], 4), round(r["theta6"], 4)],
                        })
    print("  扫描 %d 个组合，有解 %d 个（%.0f%%）"
          % (total, ok, 100.0 * ok / total if total else 0))

    # ── 3. 找出离"正前方、桌面高度"最近的可达姿态 ──
    # 抓取最可能的工作区：x 小（正前方）、y 中等、z 略低（桌面）。
    print()
    print("=== 3. 推荐候选（正前方 x≤0.06，y∈[0.22,0.28]）===")
    near = [r for r in rows
            if r["coords"][0] <= 0.06 and 0.22 <= r["coords"][1] <= 0.28]
    near.sort(key=lambda r: (abs(r["coords"][0] - 0.04)
                             + abs(r["coords"][1] - 0.25)
                             + abs(r["coords"][2] - 0.02)))
    for r in near[:8]:
        c = r["coords"]
        t = r["theta"]
        print("  coords=(%.2f, %.2f, %.2f) alpha=%4d  ->  "
              "theta=(%.3f, %.3f, %.3f, %.3f) deg"
              % (c[0], c[1], c[2], r["alpha"], t[0], t[1], t[2], t[3]))
    if not near:
        print("  （该区域无可达解，建议扩大 y 或调 z）")

    # ── 4. 导出给后续用 ──
    out = {
        "link_length": link if isinstance(link, dict) else str(link),
        "vendor_sample": {"coords": list(vendor), "alpha": 0, "result": r},
        "reachable_count": ok,
        "total_tested": total,
        "candidates": rows[:50],
        "note": "theta 单位为度；本结果仅供人工示教参考，"
                "**不代表已验证实际抓取**。",
    }
    path = "/home/ubuntu/oceanus_bridge/ik_probe_out.json"
    try:
        with open(path, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print()
        print("结果已写入: %s" % path)
    except OSError as exc:
        print("写文件失败: %s" % exc)

    print()
    print("=" * 66)
    print("★ 本脚本**没有驱动任何关节**（不 import rospy、不发布舵机话题）")
    print("★ 结果只是「该坐标在数学上可反解」，实际能否抓到物体仍需人工示教验证")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
