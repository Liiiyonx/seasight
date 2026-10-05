"""已废弃 —— 不要用这个脚本（2026-10-05）。

它生成 bootstrap.csv，指望 EMQX 启动时导入。但 **EMQX 5.8 没有这个机制**
（那是 6.x 才有的数据导入功能）。实测：

    $ docker compose exec emqx emqx ctl import data <file>
    Error: unknown command      # 只列出 gateway-* 命令

日志零导入记录；etc/*.conf 无任何 authenticator 配置。叠加
``allow_anonymous=false`` 的后果是**所有 MQTT 连接被拒**。

正确做法::

    python scripts/setup_emqx_auth.py --env-file .env.production \
        --dashboard-url http://127.0.0.1:18083

详见 docs/competitions/cloud-mqtt-wss-deploy.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _require(env: dict[str, str], key: str) -> str:
    value = env.get(key, "")
    if len(value) < 12 or "change_me" in value.lower():
        raise SystemExit(f"{key} must be set to a strong non-placeholder value")
    return value


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _write_csv(path: Path, env: dict[str, str]) -> None:
    _require(env, "MQTT_DASHBOARD_PASSWORD")
    backend_user = env.get("MQTT_BACKEND_USERNAME", "backend_service")
    edge_user = env.get("MQTT_EDGE_USERNAME", "edge_device")
    viewer_user = env.get("MQTT_VIEWER_USERNAME", "dashboard_viewer")
    robot_user = env.get("MQTT_ROBOT_USERNAME", "robot_device")
    passwords = {
        backend_user: _require(env, "MQTT_BACKEND_PASSWORD"),
        edge_user: _require(env, "MQTT_EDGE_PASSWORD"),
        viewer_user: _require(env, "MQTT_VIEWER_PASSWORD"),
        robot_user: _require(env, "MQTT_ROBOT_PASSWORD"),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["mqtt_user", "user_id", "password_hash", "salt", "is_superuser"])
        for username, password in passwords.items():
            writer.writerow(["mqtt_user", username, _hash(password), "", "false"])

        acl_headers = ["mqtt_acl", "username", "permission", "action", "topic"]
        writer.writerow(acl_headers)
        for topic in (
            "marine/+/+/event",
            "marine/+/+/telemetry",
            "marine/+/+/status",
            "robot/+/cmd/ack",
            "robot/+/task/progress",
        ):
            writer.writerow(["mqtt_acl", backend_user, "allow", "subscribe", topic])
        for topic in (
            "robot/+/task",
            "robot/+/cmd",
            "marine/+/backend-+/status",
        ):
            writer.writerow(["mqtt_acl", backend_user, "allow", "publish", topic])

        for topic in (
            "marine/+/+/event",
            "marine/+/+/telemetry",
            "marine/+/+/status",
        ):
            writer.writerow(["mqtt_acl", edge_user, "allow", "publish", topic])
        for topic in ("robot/+/task", "robot/+/cmd"):
            writer.writerow(["mqtt_acl", edge_user, "allow", "subscribe", topic])
        writer.writerow(["mqtt_acl", edge_user, "deny", "all", "#"])

        for topic in ("robot/+/task", "robot/+/cmd"):
            writer.writerow(["mqtt_acl", robot_user, "allow", "subscribe", topic])
        for topic in (
            "robot/+/cmd/ack",
            "robot/+/task/progress",
            "marine/+/+/telemetry",
            "marine/+/+/status",
        ):
            writer.writerow(["mqtt_acl", robot_user, "allow", "publish", topic])
        writer.writerow(["mqtt_acl", robot_user, "deny", "all", "#"])

        for topic in (
            "marine/+/+/status",
            "marine/+/+/telemetry",
            "robot/+/task/progress",
        ):
            writer.writerow(["mqtt_acl", viewer_user, "allow", "subscribe", topic])
        writer.writerow(["mqtt_acl", viewer_user, "deny", "all", "#"])
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.production")
    parser.add_argument(
        "--output",
        default="deploy/emqx/bootstrap.production.csv",
    )
    args = parser.parse_args()
    env_path = Path(args.env_file)
    if not env_path.exists():
        raise SystemExit(f"Environment file not found: {env_path}")
    output = Path(args.output)
    _write_csv(output, _load_env(env_path))
    print(f"Generated {output}. It contains password hashes and must not be committed.")


if __name__ == "__main__":
    main()
