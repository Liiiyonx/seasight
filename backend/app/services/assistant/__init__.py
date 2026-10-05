"""对话助手子系统（自由对话问答 + 图片拖入即分析）。

与「事件处置 Agent」（app.services.agents，派单运行状态机）**并行**：
那是 10 端点 + 冻结契约的运维控制台用途；这里是对话框用途——
打字问答 / 拖图检测 / 派单建议卡，互不干扰、互不改契约。

包结构：
    tools.py   异步工具集（只读为主；image.analyze 由引擎直调，不暴露给模型）
    engine.py  对话引擎：会话管理 + 模型工具编排 + 规则兜底

★ 错误码启用 **8xxx 新段**（不碰 6xxx Agent / 7xxx 知识两个冻结段）；
  图片推理失败复用 5xxx 段的 5002 AI_INFERENCE_FAILED。
"""

# 对话助手错误码（8xxx；冻结在本模块，避免与 ErrorCode 通用段混淆）
CHAT_SESSION_NOT_FOUND = 8001    # 会话不存在（或不属于当前用户——不泄露他人会话存在性）
CHAT_MESSAGE_NOT_FOUND = 8002    # 消息不存在（预留）
CHAT_IMAGE_INVALID = 8003        # 图片无法解析 / 超限 / 为空
CHAT_ASSISTANT_DISABLED = 8004   # 对话助手被配置关闭

__all__ = [
    "CHAT_SESSION_NOT_FOUND",
    "CHAT_MESSAGE_NOT_FOUND",
    "CHAT_IMAGE_INVALID",
    "CHAT_ASSISTANT_DISABLED",
]
