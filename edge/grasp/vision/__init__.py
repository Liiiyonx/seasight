"""现场再训练管线（数据集 / 训练 / 评估 / 原子部署）。

分层：

* ``detector``  推理封装（onnx / ncnn / mock 三种后端自动降级）
* ``dataset``   YOLO 格式数据集组织（采集帧 → 标注 → 划分留出集）
* ``train``     三档训练（免训练校准 / Pi 本地微调 / 局域网协同）
* ``evaluate``  留出集评估门禁（不达标不许部署）
* ``deploy``    权重版本化原子切换与一键回滚

设计原则：**推理不依赖训练栈**。ultralytics 只在 train/evaluate 出现，
检测器只依赖 opencv + numpy，这样 Pi 上跑作业时不会被训练依赖拖累。
"""
