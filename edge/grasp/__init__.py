"""Oceanus 机械臂智能拾取子系统（edge/grasp）。

组件：

* ``arm_io``        树莓派侧 ROS 接入层（相机 / IK / 舵机下发 / 舵机回读）
* ``calibration``   9 点像素↔工作区坐标标定
* ``vision_driver`` 视觉抓取驱动（``ArmDriver`` 实现，注册名 ``oceanus_vision``）
* ``vision/``       现场再训练管线（数据集 / 训练 / 评估 / 原子部署）
* ``web/``          树莓派上的训练作业台（FastAPI :8090）

本包与 ``edge/arm_bridge`` 同层：``arm_bridge/bridge.py`` 已把 ``edge/``
加入 ``sys.path``，所以 ``build_arm_driver(backend="oceanus_vision")``
能惰性加载 ``grasp.vision_driver`` 并注册进驱动表 —— 平台侧 MQTT 契约零改动。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
