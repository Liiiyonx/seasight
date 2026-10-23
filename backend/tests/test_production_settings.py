"""Fail-closed production configuration checks."""

from __future__ import annotations

import pytest

from app.core.config import Settings, validate_production_settings


def _production_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "debug": False,
        "secret_key": "a" * 64,
        "postgres_password": "strong-postgres-password",
        "redis_password": "strong-redis-password",
        "mqtt_password": "strong-mqtt-password",
        "minio_access_key": "strong-minio-access-key",
        "minio_secret_key": "strong-minio-password",
        "cors_origins": ["https://seasight.example.cn"],
    }
    values.update(overrides)
    return Settings(**values)


def test_safe_production_settings_pass() -> None:
    validate_production_settings(_production_settings())


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_key": "dev-only-change-me"},
        {"debug": True},
        {"postgres_password": "seasight"},
        {"redis_password": ""},
        {"mqtt_password": "CHANGE_ME"},
        {"postgres_password": "CHANGE_ME_STRONG_POSTGRES_PASSWORD"},
        {"redis_password": "CHANGE_ME_STRONG_REDIS_PASSWORD"},
        {"mqtt_password": "CHANGE_ME_STRONG_BACKEND_MQTT_PASSWORD"},
        {"minio_access_key": "CHANGE_ME_MINIO_ACCESS_KEY"},
        {"minio_secret_key": "CHANGE_ME_STRONG_MINIO_SECRET_KEY"},
        {"minio_secret_key": "minioadmin"},
        {"cors_origins": ["*"]},
    ],
)
def test_unsafe_production_settings_fail(overrides: dict) -> None:
    with pytest.raises(RuntimeError):
        validate_production_settings(_production_settings(**overrides))


def test_development_defaults_are_still_allowed() -> None:
    validate_production_settings(Settings(app_env="development"))


def test_comma_separated_cors_origins_from_environment_are_split() -> None:
    settings = Settings(cors_origins="https://a.example.cn,https://b.example.cn")
    assert settings.cors_origins == [
        "https://a.example.cn",
        "https://b.example.cn",
    ]
