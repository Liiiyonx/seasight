"""探海灵眸 SeaSight — 应用配置。

所有配置从环境变量读取（支持 .env 文件），集中在此处管理。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。字段名小写，环境变量大写（自动匹配）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_name: str = "SeaSight"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    secret_key: str = Field(default="dev-only-change-me", min_length=8)
    access_token_expire_minutes: int = 1440
    timezone: str = "Asia/Shanghai"

    # ---------- 数据库 ----------
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "seasight"
    postgres_user: str = "seasight"
    postgres_password: str = "seasight"
    postgres_connect_timeout_seconds: float = 2.0

    @property
    def database_url(self) -> str:
        """异步数据库连接串（asyncpg 驱动）。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """同步连接串（Alembic 迁移用）。"""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ---------- Redis ----------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # ---------- MQTT ----------
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = "seasight"
    mqtt_password: str = "seasight"
    mqtt_client_id: str = "seasight-backend"
    mqtt_topic_prefix: str = "marine"
    mqtt_keepalive: int = 60

    # ---------- MinIO ----------
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket_events: str = "events"
    minio_secure: bool = False

    # ---------- 流媒体 ----------
    # 容器内后端访问 go2rtc 的地址；仅服务端使用，绝不能直接返回给浏览器。
    stream_base_url: str = "http://localhost:1984"
    # 浏览器访问视频流的外部基地址。留空时返回同源相对路径，由 Nginx 的
    # /stream/ 反向代理转发到 go2rtc，避免泄露容器内主机名。
    stream_public_base_url: str = ""

    # ---------- AI 服务 ----------
    ai_service_url: str = "http://localhost:8081"
    ai_model_version: str = "det_v0.1.0"
    ai_timeout_seconds: int = 10

    # ---------- Agent 模型适配层（默认关闭，规则模式始终可用） ----------
    agent_model_adapter_enabled: bool = False
    agent_model_adapter_timeout_ms: int = 5000
    agent_model_adapter_max_steps: int = 10
    # OpenAI-compatible Chat Completions 端点；仅在上面的开关开启时使用。
    # base_url 可指向服务根路径或 /v1，也可直接给到 /chat/completions。
    agent_model_base_url: str = ""
    agent_model_api_key: str = ""
    agent_model_name: str = ""
    agent_model_max_output_tokens: int = 1024

    # ---------- Agent 持久化仓储（WP-10；默认内存，数据库不可用时自动回退内存） ----------
    agent_persistent_repository_enabled: bool = False
    # 验收/高安全环境可显式要求 WRITE 工具人工审批；默认保持生产策略不变。
    agent_require_approval_for_write: bool = False

    # ---------- 对话助手（默认启用规则兜底；配齐模型三要素才走真实 LLM） ----------
    # 与 agent_model_* 相互独立：那是派单规划用途，这里是对话问答用途。
    assistant_enabled: bool = True
    # OpenAI-compatible Chat Completions 端点；三者任一缺省则规则兜底、绝不假装。
    assistant_model_base_url: str = ""
    assistant_model_api_key: str = ""
    assistant_model_name: str = ""
    assistant_model_timeout_ms: int = 8000
    # 工具编排最大轮数，防止模型在工具调用间死循环。
    assistant_model_max_tool_rounds: int = 4
    assistant_model_max_output_tokens: int = 1024
    # 送入模型的历史消息条数（多轮上下文窗口）。
    assistant_history_limit: int = 12
    # 单张上传图片大小上限（MB）。
    assistant_max_image_mb: int = 8

    # ---------- 通知外发 ----------
    # 企业微信机器人 webhook；为空 = 不启用告警外发（只落日志）。
    # 这是刻意的降级：告警推送失败绝不能阻塞事件派单主流程。
    wecom_webhook: str = ""
    wecom_mention_mobile: str = ""     # 告警 @ 的手机号（逗号分隔），可选

    # ---------- CORS ----------
    # Keep the union for pydantic-settings: it otherwise tries to JSON-decode
    # comma-separated environment values before the validator below runs.
    cors_origins: list[str] | str = ["http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """支持逗号分隔字符串（环境变量）或列表。"""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    # ---------- 派单引擎参数 ----------
    dispatch_min_battery: int = 30            # 最低电量百分比
    dispatch_max_bin_usage: float = 0.8       # 最大仓容占用
    dispatch_ack_timeout_seconds: int = 15    # ACK 超时（秒）
    dispatch_merge_window_minutes: int = 10   # 同区域事件合并窗口
    dispatch_merge_radius_meters: int = 200   # 同区域事件合并半径（米）
    dispatch_range_meters: int = 5000         # 派单搜索半径（米）

    # ---------- 事件队列 ----------
    redis_stream_events: str = "stream:events"       # 事件队列
    redis_stream_dispatch: str = "stream:dispatch"   # 派单队列
    redis_consumer_group: str = "dispatch-group"
    redis_dead_letter: str = "stream:dead_letter"    # 死信队列
    # 默认启动消费者与定时补派；验收/单请求进程可关闭，避免后台任务
    # 与显式 Agent 调用竞争同一事件。
    background_workers_enabled: bool = True


def validate_production_settings(settings_obj: Settings) -> None:
    """Reject unsafe defaults before a production process accepts traffic.

    The development defaults are intentionally permissive so the project can
    start without infrastructure. Production must fail closed instead of
    silently running with public demo credentials.
    """
    if settings_obj.app_env != "production":
        return

    errors: list[str] = []
    if (
        len(settings_obj.secret_key) < 32
        or "change_me" in settings_obj.secret_key.lower()
        or settings_obj.secret_key == "dev-only-change-me"
    ):
        errors.append("SECRET_KEY must be a non-placeholder value of at least 32 characters")
    if settings_obj.debug:
        errors.append("DEBUG must be false in production")
    if _is_placeholder_secret(settings_obj.postgres_password, {"seasight"}):
        errors.append("POSTGRES_PASSWORD must be changed")
    if _is_placeholder_secret(settings_obj.redis_password, {"redis"}):
        errors.append("REDIS_PASSWORD must be a non-placeholder value")
    if _is_placeholder_secret(settings_obj.mqtt_password, {"seasight"}):
        errors.append("MQTT_PASSWORD must be changed")
    if _is_placeholder_secret(settings_obj.minio_access_key, {"minioadmin"}):
        errors.append("MINIO_ACCESS_KEY must be changed")
    if _is_placeholder_secret(settings_obj.minio_secret_key, {"minioadmin"}):
        errors.append("MINIO_SECRET_KEY must be changed")
    if "*" in settings_obj.cors_origins:
        errors.append("CORS_ORIGINS must not contain '*'")

    if errors:
        raise RuntimeError(
            "Unsafe production configuration: " + "; ".join(errors)
        )


def _is_placeholder_secret(value: str, unsafe_defaults: set[str]) -> bool:
    """Treat blank, known defaults and CHANGE_ME variants as unsafe."""
    normalized = value.strip().lower()
    return (
        not normalized
        or normalized in unsafe_defaults
        or "change_me" in normalized
    )


@lru_cache
def get_settings() -> Settings:
    """获取配置单例（缓存，避免重复解析 .env）。"""
    return Settings()


settings = get_settings()
