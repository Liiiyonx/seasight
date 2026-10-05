"""对话助手出入参 Schema（API 契约）。

独立于「事件处置 Agent」（app.schemas.agent）的自由对话契约：
那是派单运行状态机的出入参，这里是对话问答的会话/消息/回复。

命名沿用全局约定：XxxCreate=请求体、XxxOut=响应体。

★ 结构化展示块用统一 ``{type, data}`` 信封：type 区分前端渲染方式，
  data 为该 block 类型的自由载荷。展示块是**渲染约定**而非存储契约——
  若逐字段收紧，每加一种卡片就要同步改 schema/前端/测试三处；
  后端只保证 type 合法、data 是对象，具体键由前端按 type 解读。

★ 隐私/纪律（与 app.models.chat 一致）：block 不承载模型思维链、
  原始 Prompt 或密钥；脱敏在服务层完成后才入库/回传。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 块类型取值冻结在本模块（前端渲染约定的单一真源）：
#   text             纯文本气泡
#   detection        图片检测结果（data: image_ref/detections/count/width/height）
#   event_card       单个事件卡（data: event）
#   event_list       事件列表（data: items/total）
#   stats            指标卡（data: 各统计字段）
#   dispatch_suggest 派单建议卡（data: event_id/candidates/hint）
#   error            错误提示卡（data: message）
ChatBlockType = Literal[
    "text",
    "detection",
    "event_card",
    "event_list",
    "stats",
    "dispatch_suggest",
    "error",
]


class ChatBlock(BaseModel):
    """结构化展示块（统一信封：type 区分渲染方式，data 为自由载荷）。"""

    type: ChatBlockType
    data: dict[str, Any] = Field(default_factory=dict)


class ChatSessionCreate(BaseModel):
    """新建会话请求；title 可空，服务端给默认值。"""

    title: str | None = Field(default=None, max_length=200)


class ChatSessionOut(BaseModel):
    """会话响应体。"""

    model_config = ConfigDict(from_attributes=True)

    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    """历史消息响应体（透传存储格式：payload 为自由 dict，前端按 content_type 解读）。"""

    model_config = ConfigDict(from_attributes=True)

    message_id: str
    session_id: str
    role: str
    content: str
    content_type: str
    payload: dict[str, Any] | None = None
    run_id: str | None = None
    created_at: datetime


class AssistantReplyOut(BaseModel):
    """POST /assistant/chat 的实时响应（assistant 消息已落库）。"""

    message_id: str
    session_id: str
    text: str
    blocks: list[ChatBlock] = Field(default_factory=list)
    model_used: str = Field(..., description="实际使用的模型名；规则兜底为 rule-fallback")
    fallback: bool = Field(..., description="是否走了规则兜底（未配模型或模型失败）")
