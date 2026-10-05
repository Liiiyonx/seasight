"""WP-11 可选大模型适配层与规则兜底（backend/app/services/agents/model_adapter.py）。

证据等级：E1/E2 —— 本模块只提供「模型作为可选规划增强」的适配与校验能力，
不绑定任何厂商、不访问公网、不宣称真实部署。任何真实网络实现都必须默认关闭。

设计目标（docs/agent-program-wave3.md 第 2、3.2、5 节）：
    大模型是可选规划增强，而不是控制面的单点依赖。模型不可用、超时、
    乱输出或越权时，确定性规则模式（现有 RulePlanner）继续运行，
    原有派单闭环必须继续工作。

公开接口（冻结，不可改名）：
    JsonModelClient      可注入传输协议（不绑定厂商），只接收已脱敏的
                         结构化请求，返回 JSON 对象或字符串。
    ModelStepProposal    模型提出的单步提案（只含 tool_name / arguments /
                         decision_summary / confidence）。
    ModelPlanProposal    规划结果：source="model" 或 source="rule_fallback"，
                         带结构化原因与错误码，可直接被 Runtime 消费。
    ModelAdapterPlanner  接收 任务 / 业务上下文 / 工具摘要 / 角色 / Schema，
                         校验模型输出，非法时回退 RulePlanner。
    ModelAdapterError / ModelUnavailableError / ModelTimeoutError /
    ModelSchemaError     适配层结构化异常（语义清晰，见各错误码）。

★ 纪律（与 Harness 手册 3.7 解密预算一致）：
- 只允许模型输出 tool_name / arguments / decision_summary / confidence，
  其余字段（含 chain_of_thought / reasoning）一律拒绝。
- 未知工具、未允许参数、缺字段、非法类型、超长字段、敏感工具越权
  → 回退规则规划（可观测的明确结果，绝不假装模型成功）。
- 超时 / 连接失败 / HTTP 错误 / JSON 解析失败 → 统一回退规则规划。
- 日志只记录 摘要 / 耗时 / 响应哈希 / 错误码，绝不记录完整 Prompt
  或模型原始回复。
- 配置关闭时完全不调用模型客户端（默认关闭）。
- 模型输出只作为「待执行提案」（is_proposal=True），任何情况下
  都不代表事实；必须经过严格 Schema、工具存在性、角色权限和
  风险级别校验后才可进入执行计划。
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.services.agents.errors import AgentError
from app.services.agents.model import AgentRunRequest, RiskLevel, RuleStep, RuntimeConfig
from app.services.agents.planner import RulePlanner
from app.services.agents.schema import canonical_json, sha256_hex, validate_schema

# ----------------------------------------------------------------------
# 模块级常量
# ----------------------------------------------------------------------

# 结果来源标记
SOURCE_MODEL = "model"
SOURCE_RULE_FALLBACK = "rule_fallback"

# 适配层结构化错误码（适配层专用，不属于手册 3.3 的 Runtime 冻结错误码；
# 集成后也不会写入 run 的 termination_reason / step.error_code）
MODEL_DISABLED = "model_disabled"                  # 配置关闭（默认）
MODEL_NOT_CONFIGURED = "model_not_configured"      # 未注入 client
MODEL_TIMEOUT = "model_timeout"                    # 模型调用超时
MODEL_CONNECTION_FAILED = "model_connection_failed"  # 连接失败
MODEL_HTTP_ERROR = "model_http_error"              # HTTP 错误
MODEL_INVALID_JSON = "model_invalid_json"          # JSON 解析失败
MODEL_SCHEMA_ERROR = "model_schema_error"          # 缺字段 / 类型错 / 超长 / 顶层结构错
MODEL_UNKNOWN_TOOL = "model_unknown_tool"          # 未知工具
MODEL_DISALLOWED_ARGUMENT = "model_disallowed_argument"  # 参数不在白名单 / 不满足工具 Schema
MODEL_ROLE_DENIED = "model_role_denied"            # 角色越权（含敏感工具越权）
MODEL_FORBIDDEN_FIELD = "model_forbidden_field"    # 包含思维链等被禁止字段
MODEL_INTERNAL_ERROR = "model_internal_error"      # 客户端抛出意外异常

# 模型输出中永久禁止出现的字段名（大小写不敏感递归检测）。
# 注意：错误消息中**不得**回显这些字段名（日志与结果必须保持干净）。
FORBIDDEN_FIELD_NAMES: frozenset[str] = frozenset({"chain_of_thought", "reasoning"})

# 请求脱敏：payload 里遇到这些键直接丢弃（大小写不敏感），
# 同时覆盖敏感载荷与思维链字段，双保险。
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "authorization",
        "token",
        "password",
        "secret",
        "private_key",
        "credential",
        "cookie",
        "session_id",
        "chain_of_thought",
        "reasoning",
    }
)

# 长度 / 数量上限（超限即按「超长字段 / 非法类型」拒绝并回退）
MAX_PLAN_STEPS = 10
MAX_TOOL_NAME_LEN = 64
MAX_SUMMARY_LEN = 200
MAX_ARGUMENTS_JSON_LEN = 1024
MAX_PROMPT_STR_LEN = 200
MAX_COLLECTION_ITEMS = 20
MAX_DEPTH = 3
MAX_SCHEMA_STR_LEN = 500  # Schema 是契约：只截断超长描述，不做深度/数量截断

# 单个模型步骤的严格 Schema（4 个字段全必填；additionalProperties
# 语义由代码显式强制，因为 validate_schema 不支持该关键字）
STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tool_name", "arguments", "decision_summary", "confidence"],
    "properties": {
        "tool_name": {"type": "string", "minLength": 1, "maxLength": MAX_TOOL_NAME_LEN},
        "arguments": {"type": "object"},
        "decision_summary": {"type": "string", "maxLength": MAX_SUMMARY_LEN},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}

# 模型输出顶层严格 Schema（只允许 plan 列表）
DEFAULT_MODEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["plan"],
    "properties": {
        "plan": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_PLAN_STEPS,
            "items": STEP_SCHEMA,
        }
    },
}

_LOGGER = logging.getLogger("oceanus.agents.model_adapter")


# ----------------------------------------------------------------------
# 适配层结构化错误（冻结公开类名）
# ----------------------------------------------------------------------


class ModelAdapterError(Exception):
    """模型适配层异常基类。

    `code` 取本模块的 MODEL_* 错误码；错误只用于「回退规则规划」的
    可观测原因，绝不进入 Runtime 的冻结错误码集合。
    """

    def __init__(self, message: str, *, code: str = MODEL_INTERNAL_ERROR) -> None:
        self.code = code
        super().__init__(message)


class ModelUnavailableError(ModelAdapterError):
    """模型不可用：连接失败 / 服务不可达 / 未配置传输。"""

    def __init__(self, message: str, *, code: str = MODEL_CONNECTION_FAILED) -> None:
        super().__init__(message, code=code)


class ModelTimeoutError(ModelAdapterError):
    """模型调用超时。"""

    def __init__(self, message: str, *, code: str = MODEL_TIMEOUT) -> None:
        super().__init__(message, code=code)


class ModelSchemaError(ModelAdapterError):
    """模型输出未通过严格校验：非法 JSON / 缺字段 / 类型错 / 超长 /
    未知工具 / 未允许参数 / 角色越权 / 含被禁止字段。"""

    def __init__(self, message: str, *, code: str = MODEL_SCHEMA_ERROR) -> None:
        super().__init__(message, code=code)


# ----------------------------------------------------------------------
# 可注入传输协议（冻结公开类名，不绑定厂商）
# ----------------------------------------------------------------------


class JsonModelClient(Protocol):
    """可注入模型传输协议（冻结）。

    只接收**已脱敏**的结构化请求（dict），返回 JSON 对象（dict）或
    JSON 字符串。不绑定具体厂商；任何真实网络实现都必须默认关闭
    （由调用方控制开关），测试只能注入假客户端，不得访问公网。

    实现约定（由实现方保证）：
    - 超时抛 `ModelTimeoutError`；
    - 连接失败 / 服务不可达抛 `ModelUnavailableError`；
    - HTTP / 传输错误抛 `ModelAdapterError`（可带 code）；
    - 其余一律返回 dict 或 str。
    """

    def complete(self, payload: dict[str, Any]) -> dict[str, Any] | str:
        if not isinstance(payload, dict):
            raise TypeError(f"payload 必须是 dict：{type(payload).__name__}")
        raise NotImplementedError("JsonModelClient.complete 未实现")


# ----------------------------------------------------------------------
# OpenAI-compatible Chat Completions 传输（默认不装配）
# ----------------------------------------------------------------------


DEFAULT_MODEL_SYSTEM_PROMPT = (
    "You are the planning component of a marine-litter dispatch agent. "
    "Use only the tools and argument schemas supplied by the user. "
    "Return one JSON object that matches output_schema exactly. "
    "Do not include markdown, commentary, reasoning, or chain-of-thought."
)


class OpenAICompatibleModelClient:
    """OpenAI-compatible Chat Completions transport.

    This is a deploy-time adapter, not a default dependency. It only runs
    when the caller explicitly enables the model adapter and provides a
    base URL plus model name. Tests inject a fake opener and never use the
    network.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_ms: int = 5000,
        max_output_tokens: int = 1024,
        system_prompt: str = DEFAULT_MODEL_SYSTEM_PROMPT,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        endpoint = str(base_url or "").strip().rstrip("/")
        model_name = str(model or "").strip()
        if not endpoint:
            raise ValueError("base_url 不能为空")
        if not model_name:
            raise ValueError("model 不能为空")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms 必须大于 0")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens 必须大于 0")

        if endpoint.endswith("/chat/completions"):
            request_url = endpoint
        else:
            request_url = f"{endpoint}/chat/completions"

        self.base_url = endpoint
        self.request_url = request_url
        self.model = model_name
        self.api_key = str(api_key or "").strip()
        self.timeout_ms = int(timeout_ms)
        self.max_output_tokens = int(max_output_tokens)
        self.system_prompt = str(system_prompt)
        self._opener = opener or urlopen

    def complete(self, payload: dict[str, Any]) -> str:
        """Send a sanitized planner payload and return the model JSON text."""
        if not isinstance(payload, dict):
            raise TypeError(f"payload 必须是 dict：{type(payload).__name__}")

        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    },
                ],
                "temperature": 0,
                "max_tokens": self.max_output_tokens,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Oceanus-Agent/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.request_url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with self._opener(request, timeout=self.timeout_ms / 1000) as response:
                raw_response = response.read()
        except HTTPError as exc:
            raise ModelAdapterError(
                f"模型服务返回 HTTP {exc.code}",
                code=MODEL_HTTP_ERROR,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ModelTimeoutError("模型调用超时") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ModelTimeoutError("模型调用超时") from exc
            raise ModelUnavailableError("模型服务连接失败") from exc
        except OSError as exc:
            raise ModelUnavailableError("模型服务连接失败") from exc
        except ValueError as exc:
            raise ModelUnavailableError("模型服务地址配置无效") from exc

        if isinstance(raw_response, bytes):
            try:
                response_text = raw_response.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ModelAdapterError(
                    "模型服务响应不是 UTF-8",
                    code=MODEL_INVALID_JSON,
                ) from exc
        elif isinstance(raw_response, str):
            response_text = raw_response
        else:
            raise ModelAdapterError(
                "模型服务响应类型非法",
                code=MODEL_INVALID_JSON,
            )

        try:
            envelope = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ModelAdapterError(
                "模型服务响应不是合法 JSON",
                code=MODEL_INVALID_JSON,
            ) from exc

        content = self._extract_content(envelope)
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        raise ModelSchemaError(
            "模型服务响应缺少 message.content",
            code=MODEL_SCHEMA_ERROR,
        )

    @staticmethod
    def _extract_content(envelope: Any) -> Any:
        if not isinstance(envelope, dict):
            raise ModelSchemaError(
                "模型服务响应必须是 JSON 对象",
                code=MODEL_SCHEMA_ERROR,
            )
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelSchemaError(
                "模型服务响应缺少 choices",
                code=MODEL_SCHEMA_ERROR,
            )
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelSchemaError(
                "模型服务 choices[0] 必须是对象",
                code=MODEL_SCHEMA_ERROR,
            )
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = first.get("text")

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return content


# ----------------------------------------------------------------------
# 提案对象（冻结公开类名）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ModelStepProposal:
    """模型提出的单步提案（只含 4 个结构化字段，冻结）。

    只承载决策摘要与工具输入，不承载思维链；`arguments` 必须已通过
    工具白名单与工具 Schema 校验。
    """

    tool_name: str
    arguments: dict[str, Any]
    decision_summary: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class ModelPlanProposal:
    """一次规划的结果（source 二选一，冻结）。

    - source="model"：模型输出通过全部校验后转化的可执行计划；
      `model_steps` 为校验通过的原始提案，`steps` 为 Runtime 可消费的
      RuleStep 列表。模型输出只作为待执行提案（is_proposal=True），
      不代表事实。
    - source="rule_fallback"：模型不可用 / 超时 / 乱输出 / 越权时
      确定性回退 `RulePlanner` 的规则计划；`error_code` 与
      `fallback_reason` 给出结构化、可观测的回退原因，
      绝不假装模型成功。
    """

    source: str  # SOURCE_MODEL | SOURCE_RULE_FALLBACK
    steps: tuple[RuleStep, ...]
    model_steps: tuple[ModelStepProposal, ...] = ()
    decision_summary: str = ""
    confidence: float = 0.0
    fallback_reason: str | None = None
    error_code: str | None = None
    latency_ms: int = 0
    response_hash: str | None = None
    is_proposal: bool = True  # 模型输出只是提案，任何情况下都不是事实


# ----------------------------------------------------------------------
# 辅助：把 ToolRegistry 压成脱敏的工具摘要（供 payload 与校验共用）
# ----------------------------------------------------------------------


def summarize_tools(registry: Any) -> list[dict[str, Any]]:
    """把 ToolRegistry 压缩为脱敏工具摘要列表（无 handler、无真实数据）。

    摘要字段：name / version / description / risk_level / timeout_ms /
    idempotent / allowed_roles / input_schema。供 ModelAdapterPlanner
    做工具存在性与参数白名单校验，也作为发给模型的工具描述。
    """
    summary: list[dict[str, Any]] = []
    for tool in registry.list():
        risk = tool.risk_level
        summary.append(
            {
                "name": tool.name,
                "version": tool.version,
                "description": tool.description,
                "risk_level": risk.value if isinstance(risk, RiskLevel) else str(risk),
                "timeout_ms": tool.timeout_ms,
                "idempotent": bool(tool.idempotent),
                "allowed_roles": list(tool.allowed_roles),
                "input_schema": tool.input_schema,
            }
        )
    return summary


# ----------------------------------------------------------------------
# 规划适配器（冻结公开类名）
# ----------------------------------------------------------------------


class ModelAdapterPlanner:
    """模型规划适配器：模型优先、规则兜底。

    接收 任务（AgentRunRequest）/ 业务上下文（dict，已脱敏）/
    工具摘要（list[dict]）/ 角色（str）/ Schema（dict，缺省用严格默认）。

    行为：
    1. 配置关闭（默认）或未注入 client → 不调用模型，直接回退规则规划。
    2. 调用 client（只传已脱敏 payload）→ 解析 JSON → 严格校验 →
       转化为 RuleStep 列表。
    3. 任何失败（超时 / 连接 / HTTP / 非法 JSON / Schema / 未知工具 /
       越权 / 被禁止字段）→ 回退 RulePlanner，结果带 source=
       "rule_fallback" 与结构化原因。
    """

    def __init__(
        self,
        *,
        client: JsonModelClient | None = None,
        enabled: bool = False,
        rule_planner: RulePlanner | None = None,
        config: RuntimeConfig | None = None,
        timeout_ms: int = 5000,
        max_steps: int = MAX_PLAN_STEPS,
        blocked_tools: tuple[str, ...] = (),
        clock: Any = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.enabled = bool(enabled)
        self.config = config or RuntimeConfig()
        self.rule_planner = rule_planner or RulePlanner(self.config)
        self.timeout_ms = int(timeout_ms)
        self.max_steps = int(max_steps)
        self.blocked_tools = frozenset(blocked_tools)
        self.clock = clock if clock is not None else _MonotonicClock()
        self.logger = log if log is not None else _LOGGER

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def plan(
        self,
        task: AgentRunRequest,
        business_context: dict[str, Any] | None = None,
        tool_summary: list[dict[str, Any]] | None = None,
        role: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> ModelPlanProposal:
        """规划入口：返回 ModelPlanProposal（model 或 rule_fallback）。

        - task：触发 Agent 运行的请求（AgentRunRequest）。
        - business_context：触发源脱敏后的业务上下文（本模块会再次脱敏）。
        - tool_summary：已注册工具摘要（summarize_tools 产出或等价结构）。
        - role：发起人角色；缺省取 task.role。
        - schema：模型输出严格 Schema；缺省 DEFAULT_MODEL_SCHEMA。
        """
        if not isinstance(task, AgentRunRequest):
            raise TypeError(f"task 必须是 AgentRunRequest：{type(task).__name__}")
        started = self.clock.now_ms()
        effective_role = role if role is not None else task.role
        if not isinstance(effective_role, str) or not effective_role:
            raise TypeError(f"role 必须是非空 str：{effective_role!r}")
        effective_schema = schema if schema is not None else DEFAULT_MODEL_SCHEMA
        tools = list(tool_summary or [])
        tool_map = {
            str(item.get("name")): item for item in tools if isinstance(item, dict) and item.get("name")
        }

        # 1. 配置关闭 / 未注入 client：完全不调用模型客户端
        if not self.enabled:
            return self._fallback(
                task, MODEL_DISABLED, "模型适配层配置关闭（默认），回退规则规划", started
            )
        if self.client is None:
            return self._fallback(
                task, MODEL_NOT_CONFIGURED, "未注入 JsonModelClient，无法调用模型，回退规则规划", started
            )

        # 2. 调用模型（只传已脱敏 payload）
        payload = self._build_payload(task, business_context or {}, tools, effective_role, effective_schema)
        try:
            raw = self.client.complete(payload)
        except ModelTimeoutError as exc:
            return self._fallback(task, exc.code or MODEL_TIMEOUT, "模型调用超时", started)
        except ModelUnavailableError as exc:
            return self._fallback(task, exc.code or MODEL_CONNECTION_FAILED, "模型不可用", started)
        except ModelAdapterError as exc:
            return self._fallback(task, exc.code or MODEL_HTTP_ERROR, "模型传输/服务错误", started)
        except TimeoutError as exc:
            return self._fallback(task, MODEL_TIMEOUT, "模型调用超时（内置超时异常）", started)
        except (ConnectionError, OSError) as exc:
            return self._fallback(task, MODEL_CONNECTION_FAILED, "模型连接失败", started)
        except Exception as exc:  # noqa: BLE001 —— 客户端意外异常统一回退，不让规划崩溃
            self.logger.warning(
                "[ModelAdapter] source=rule_fallback code=%s exc_type=%s",
                MODEL_INTERNAL_ERROR,
                type(exc).__name__,
            )
            return self._fallback(task, MODEL_INTERNAL_ERROR, "模型客户端抛出意外异常", started)

        # 3. 解析与严格校验（失败 → 回退）
        response_hash = sha256_hex(raw)
        try:
            parsed = self._parse_response(raw)
        except ModelSchemaError as exc:
            return self._fallback(task, exc.code, "模型输出 JSON 解析失败", started)
        try:
            model_steps = self._validate_output(parsed, tool_map, effective_role, effective_schema)
        except ModelSchemaError as exc:
            return self._fallback(task, exc.code, str(exc), started)

        # 4. 校验通过 → 转化为 Runtime 可消费的 RuleStep 计划
        rule_steps = tuple(self._to_rule_step(step) for step in model_steps)
        latency = self.clock.now_ms() - started
        summary = "模型计划 {n} 步（{tools}）".format(
            n=len(rule_steps), tools=", ".join(s.tool for s in rule_steps[:5])
        )
        self.logger.info(
            "[ModelAdapter] source=model steps=%d latency=%dms response_hash=%s",
            len(rule_steps),
            latency,
            response_hash,
        )
        return ModelPlanProposal(
            source=SOURCE_MODEL,
            steps=rule_steps,
            model_steps=model_steps,
            decision_summary=summary,
            confidence=sum(s.confidence for s in model_steps) / len(model_steps),
            latency_ms=latency,
            response_hash=response_hash,
        )

    # ------------------------------------------------------------------
    # 回退（可观测的明确结果，绝不假装模型成功）
    # ------------------------------------------------------------------

    def _fallback(
        self,
        task: AgentRunRequest,
        code: str,
        detail: str,
        started_ms: int,
    ) -> ModelPlanProposal:
        latency = self.clock.now_ms() - started_ms
        try:
            rule_steps = self.rule_planner.build(task)
        except AgentError as exc:
            # 规则计划本身配置错误（与 Runtime 行为一致地暴露，不让兜底静默空转）
            raise ModelAdapterError(
                f"规则规划器回退失败：{exc}", code=MODEL_INTERNAL_ERROR
            ) from exc
        self.logger.info(
            "[ModelAdapter] source=rule_fallback code=%s latency=%dms steps=%d",
            code,
            latency,
            len(rule_steps),
        )
        return ModelPlanProposal(
            source=SOURCE_RULE_FALLBACK,
            steps=tuple(rule_steps),
            model_steps=(),
            decision_summary="规则兜底 {n} 步（原因 {code}）".format(n=len(rule_steps), code=code),
            confidence=0.0,
            fallback_reason=f"{code}: {detail}",
            error_code=code,
            latency_ms=latency,
            response_hash=None,
        )

    # ------------------------------------------------------------------
    # 请求构建（只发已脱敏的结构化请求）
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        task: AgentRunRequest,
        business_context: dict[str, Any],
        tool_summary: list[dict[str, Any]],
        role: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "task": {
                "trigger_type": task.trigger_type,
                "objective": self._clip_text(task.objective),
                "role": role,
                "param_keys": sorted((task.params or {}).keys()),
                "params": self._sanitize(task.params or {}),
            },
            "business_context": self._sanitize(business_context),
            "tools": [self._summarize_tool(item) for item in tool_summary],
            "output_schema": self._sanitize_schema(schema),
            "hints": {"timeout_ms": self.timeout_ms, "max_steps": self.max_steps},
        }

    def _summarize_tool(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": str(entry.get("name") or ""),
            "version": str(entry.get("version") or ""),
            "description": self._clip_text(str(entry.get("description") or "")),
            "risk_level": str(entry.get("risk_level") or ""),
            "timeout_ms": entry.get("timeout_ms"),
            "idempotent": bool(entry.get("idempotent")),
            "allowed_roles": [str(r) for r in (entry.get("allowed_roles") or ())],
            "input_schema": self._sanitize_schema(entry.get("input_schema") or {}),
        }

    def _sanitize(self, value: Any, depth: int = 0) -> Any:
        """递归脱敏：丢弃敏感键、截断长字符串、限制集合大小与深度。"""
        if depth > MAX_DEPTH:
            return "<depth>"
        if isinstance(value, str):
            return self._clip_text(value)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, sub in value.items():
                if not isinstance(key, str) or key.lower() in SENSITIVE_KEYS:
                    continue
                if len(out) >= MAX_COLLECTION_ITEMS:
                    out["<truncated>"] = True
                    break
                out[key] = self._sanitize(sub, depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            items = [self._sanitize(item, depth + 1) for item in value[:MAX_COLLECTION_ITEMS]]
            if len(value) > MAX_COLLECTION_ITEMS:
                items.append("<truncated>")
            return items
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return f"<{type(value).__name__}>"

    @staticmethod
    def _clip_text(text: str, limit: int = MAX_PROMPT_STR_LEN) -> str:
        if not isinstance(text, str):
            text = str(text)
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _sanitize_schema(self, value: Any) -> Any:
        """Schema 是契约：只做敏感键清洗与超长字符串截断。

        不做深度 / 数量截断，保证发给模型的 output_schema 与
        本地校验所用的 Schema 完全一致。
        """
        if isinstance(value, str):
            return self._clip_text(value, limit=MAX_SCHEMA_STR_LEN)
        if isinstance(value, dict):
            return {
                key: self._sanitize_schema(sub)
                for key, sub in value.items()
                if isinstance(key, str) and key.lower() not in SENSITIVE_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize_schema(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return f"<{type(value).__name__}>"

    # ------------------------------------------------------------------
    # 解析与严格校验
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(raw: dict[str, Any] | str) -> Any:
        """客户端返回 dict 或 str；str 必须能解析为 JSON。"""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                raise ModelSchemaError("模型返回空响应", code=MODEL_INVALID_JSON)
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ModelSchemaError(
                    "模型返回内容不是合法 JSON",
                    code=MODEL_INVALID_JSON,
                ) from exc
        raise ModelSchemaError(
            f"模型返回类型必须是 dict 或 str，实际 {type(raw).__name__}",
            code=MODEL_SCHEMA_ERROR,
        )

    def _validate_output(
        self,
        parsed: Any,
        tool_map: dict[str, dict[str, Any]],
        role: str,
        schema: dict[str, Any],
    ) -> tuple[ModelStepProposal, ...]:
        self._reject_forbidden_fields(parsed)
        if not isinstance(parsed, dict):
            raise ModelSchemaError("模型输出必须是 JSON 对象", code=MODEL_SCHEMA_ERROR)
        extra = set(parsed) - {"plan"}
        if extra:
            raise ModelSchemaError(
                f"模型输出顶层含未允许字段：{sorted(extra)}",
                code=MODEL_SCHEMA_ERROR,
            )
        schema_errors = validate_schema(parsed, schema)
        if schema_errors:
            raise ModelSchemaError(
                "模型输出未通过严格 Schema：" + "; ".join(schema_errors),
                code=MODEL_SCHEMA_ERROR,
            )
        plan = parsed["plan"]
        if len(plan) > self.max_steps:
            raise ModelSchemaError(
                f"模型计划步数 {len(plan)} 超过上限 {self.max_steps}",
                code=MODEL_SCHEMA_ERROR,
            )
        return tuple(self._validate_step(item, tool_map, role) for item in plan)

    def _validate_step(
        self,
        step: Any,
        tool_map: dict[str, dict[str, Any]],
        role: str,
    ) -> ModelStepProposal:
        if not isinstance(step, dict):
            raise ModelSchemaError("模型步骤必须是 JSON 对象", code=MODEL_SCHEMA_ERROR)
        self._reject_forbidden_fields(step)
        extra = set(step) - {"tool_name", "arguments", "decision_summary", "confidence"}
        if extra:
            raise ModelSchemaError(
                f"模型步骤含未允许字段：{sorted(extra)}",
                code=MODEL_SCHEMA_ERROR,
            )
        schema_errors = validate_schema(step, STEP_SCHEMA)
        if schema_errors:
            raise ModelSchemaError(
                "模型步骤未通过严格 Schema：" + "; ".join(schema_errors),
                code=MODEL_SCHEMA_ERROR,
            )
        tool_name = step["tool_name"]
        entry = tool_map.get(tool_name)
        if entry is None:
            raise ModelSchemaError(f"未知工具：{tool_name}", code=MODEL_UNKNOWN_TOOL)
        if tool_name in self.blocked_tools:
            raise ModelSchemaError(f"工具被策略禁止：{tool_name}", code=MODEL_ROLE_DENIED)

        # 角色权限 + 风险级别校验（敏感工具越权在此拒绝）
        allowed_roles = tuple(entry.get("allowed_roles") or ())
        if role not in allowed_roles:
            risk = str(entry.get("risk_level") or "")
            prefix = "敏感/高风险工具越权" if risk in ("sensitive", "device_command") else "角色越权"
            raise ModelSchemaError(
                f"{prefix}：角色 {role} 无权调用 {tool_name}（允许 {list(allowed_roles)}）",
                code=MODEL_ROLE_DENIED,
            )
        risk_level = entry.get("risk_level")
        if risk_level is not None and str(risk_level) not in {
            "read_only", "write", "device_command", "sensitive",
        }:
            raise ModelSchemaError(
                f"工具 {tool_name} 声明了未知风险级别：{risk_level}",
                code=MODEL_SCHEMA_ERROR,
            )

        # 参数白名单 + 工具 Schema 校验（只允许模型提出白名单参数）
        arguments = step["arguments"]
        input_schema = entry.get("input_schema")
        properties = input_schema.get("properties") if isinstance(input_schema, dict) else None
        if not isinstance(properties, dict):
            raise ModelSchemaError(
                f"工具 {tool_name} 缺少参数白名单（input_schema.properties）",
                code=MODEL_DISALLOWED_ARGUMENT,
            )
        unknown_args = sorted(key for key in arguments if key not in properties)
        if unknown_args:
            raise ModelSchemaError(
                f"参数不在工具白名单：{tool_name} -> {unknown_args}",
                code=MODEL_DISALLOWED_ARGUMENT,
            )
        arg_errors = validate_schema(arguments, input_schema)
        if arg_errors:
            raise ModelSchemaError(
                f"参数未通过工具 Schema：{tool_name} -> " + "; ".join(arg_errors),
                code=MODEL_DISALLOWED_ARGUMENT,
            )
        if len(canonical_json(arguments)) > MAX_ARGUMENTS_JSON_LEN:
            raise ModelSchemaError(
                f"工具 {tool_name} 参数过大（超过 {MAX_ARGUMENTS_JSON_LEN} 字符）",
                code=MODEL_SCHEMA_ERROR,
            )
        return ModelStepProposal(
            tool_name=tool_name,
            arguments=dict(arguments),
            decision_summary=step.get("decision_summary") or "",
            confidence=float(step.get("confidence") or 0.0),
        )

    @staticmethod
    def _reject_forbidden_fields(value: Any) -> None:
        """递归拒绝 chain_of_thought / reasoning 字段（大小写不敏感）。

        ★ 错误消息不回显字段名，保证日志与结果中不出现思维链字段。
        """
        if isinstance(value, dict):
            for key, sub in value.items():
                if isinstance(key, str) and key.lower() in FORBIDDEN_FIELD_NAMES:
                    raise ModelSchemaError(
                        "模型输出包含被禁止字段（思维链/推理）",
                        code=MODEL_FORBIDDEN_FIELD,
                    )
                ModelAdapterPlanner._reject_forbidden_fields(sub)
        elif isinstance(value, (list, tuple)):
            for item in value:
                ModelAdapterPlanner._reject_forbidden_fields(item)

    # ------------------------------------------------------------------
    # 转化为 Runtime 可消费的计划
    # ------------------------------------------------------------------

    @staticmethod
    def _to_rule_step(step: ModelStepProposal) -> RuleStep:
        """把模型提案转化为 RuleStep（参数已具体绑定，expect=None）。

        Runtime 的 _stage_execute 会再次执行工具 Schema / 策略守卫 /
        审批，因此模型参数即使通过本层校验，仍受确定性控制面约束。
        """
        return RuleStep(
            tool=step.tool_name,
            input=dict(step.arguments),
            summary=step.decision_summary or f"模型提议调用 {step.tool_name}",
            expect=None,
            on_error="terminate",
        )


class _MonotonicClock:
    """单调毫秒时钟（默认；测试可注入 FakeClock 保证确定性）。"""

    def now_ms(self) -> int:
        return int(time.monotonic() * 1000)


__all__ = [
    # 冻结公开接口（docs/agent-program-wave3.md 3.2）
    "JsonModelClient",
    "OpenAICompatibleModelClient",
    "ModelStepProposal",
    "ModelPlanProposal",
    "ModelAdapterPlanner",
    "ModelAdapterError",
    "ModelUnavailableError",
    "ModelTimeoutError",
    "ModelSchemaError",
    # 扩展
    "summarize_tools",
    "DEFAULT_MODEL_SCHEMA",
    "STEP_SCHEMA",
    "SOURCE_MODEL",
    "SOURCE_RULE_FALLBACK",
    "DEFAULT_MODEL_SYSTEM_PROMPT",
]
