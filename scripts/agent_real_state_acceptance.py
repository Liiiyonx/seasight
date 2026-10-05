#!/usr/bin/env python3
"""WP-18 real Agent state end-to-end acceptance.

This runner starts a dedicated backend on port 8011, then exercises:

1. real HTTP event ingestion and authentication;
2. real PostgreSQL-backed Agent repositories and API reads;
3. operator/admin/viewer permission boundaries;
4. idempotent replay before and after a process restart;
5. WRITE approval handoff and task creation;
6. read-only restart recovery from the t_agent_run_state mirror.

The event and model inputs are synthetic. The HTTP stack, permissions,
PostgreSQL state, task rows and process restarts are real. Background
workers are disabled for this process so the Redis consumer and pending
dispatcher cannot race the explicit Agent call.

Evidence level: E2 software integration, not E3/E4 field evidence.
Exit code is 0 only when every mandatory check passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "artifacts" / "agent-real-state-acceptance"
RESULT_PATH = OUT_DIR / "latest.json"
LOG_DIR = OUT_DIR / "logs"
DEFAULT_PYTHON = ROOT / ".venv-analysis" / "Scripts" / "python.exe"

HOST = "127.0.0.1"
PORT = 8011
BASE_URL = f"http://{HOST}:{PORT}"
CHINA_TZ = timezone(timedelta(hours=8))

DB_CONFIG = {
    "host": os.environ.get("POSTGRES_HOST", "localhost"),
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "database": os.environ.get("POSTGRES_DB", "seasight"),
    "user": os.environ.get("POSTGRES_USER", "seasight"),
    "password": os.environ.get("POSTGRES_PASSWORD", "seasight"),
}

REQUIRED_TABLES = (
    "t_agent_run",
    "t_agent_run_state",
    "t_agent_step",
    "t_agent_approval",
    "t_agent_memory",
    "t_task",
    "t_event",
)

EXPECTED_TRAJECTORY = {
    "plan",
    "policy",
    "tool_call",
    "observation",
    "verification",
    "terminal",
}


class AcceptanceFailure(RuntimeError):
    """A mandatory acceptance assertion failed."""


class Runtime:
    def __init__(self) -> None:
        nonce = uuid.uuid4()
        token = nonce.hex[:12]
        self.scenario_id = f"wp18-{token}"
        # Keep each scenario inside the camera's dispatch range while avoiding
        # the exact-coordinate merge behavior against earlier acceptance rows.
        offset_lng = ((nonce.int & 0x0FFF) - 0x0800) / 1_000_000
        offset_lat = (((nonce.int >> 12) & 0x0FFF) - 0x0800) / 1_000_000
        self.baseline_location = (119.6521 + offset_lng, 26.3864 + offset_lat)
        self.approval_location = (
            self.baseline_location[0] + 0.0004,
            self.baseline_location[1] + 0.0004,
        )
        self.started = time.perf_counter()
        self.checks: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.phases: list[dict[str, Any]] = []
        self.current_phase: dict[str, Any] | None = None
        self.process: subprocess.Popen[str] | None = None
        self.log_handle: Any = None
        self.python = Path(os.environ.get("SEASIGHT_PYTHON", str(DEFAULT_PYTHON)))

    def begin_phase(
        self,
        name: str,
        *,
        env: dict[str, str],
        command: list[str],关于我这个智能体的形式
        cwd: Path,
    ) -> None:
        self.current_phase = {
            "name": name,
            "started_at": utc_now(),
            "command": " ".join(command),
            "cwd": str(cwd),
            "environment": env,
            "run_ids": [],
            "event_ids": [],
            "task_ids": [],
        }
        self.phases.append(self.current_phase)

    def finish_phase(self, status: str) -> None:
        if self.current_phase is None:
            return
        self.current_phase["finished_at"] = utc_now()
        self.current_phase["status"] = status
        self.current_phase = None

    def check(self, name: str, passed: bool, detail: Any) -> None:
        phase = self.current_phase["name"] if self.current_phase else "global"
        self.checks.append(
            {
                "phase": phase,
                "name": name,
                "passed": bool(passed),
                "detail": detail,
            }
        )
        if not passed:
            raise AcceptanceFailure(f"{phase}: {name}: {detail}")

    def record(
        self,
        *,
        method: str,
        path: str,
        status: int | None,
        request_body: Any,
        response_body: Any,
        elapsed_ms: float,
        error: str | None = None,
    ) -> None:
        phase = self.current_phase["name"] if self.current_phase else "global"
        self.requests.append(
            {
                "phase": phase,
                "at": utc_now(),
                "method": method,
                "path": path,
                "status": status,
                "elapsed_ms": round(elapsed_ms, 3),
                "request": redact(request_body),
                "response": redact(response_body),
                "error": error,
            }
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> Any:
    """Remove credentials while keeping response structure auditable."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "access_token",
                "authorization",
                "password",
                "secret",
                "token",
            }:
                output[key] = "<redacted>"
            else:
                output[key] = redact(item)
        return output
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 20_000:
        return value[:20_000] + "...<truncated>"
    return value


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def wait_for_port_free(
    host: str,
    port: int,
    *,
    timeout: float,
) -> bool:
    """Wait for a terminated test server to release its listening socket."""
    deadline = time.monotonic() + timeout
    while True:
        if not port_in_use(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def parse_json(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def http_json(
    runtime: Runtime,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    url = BASE_URL + path
    payload = None
    headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            parsed = parse_json(response.read())
        runtime.record(
            method=method,
            path=path,
            status=status,
            request_body=body,
            response_body=parsed,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        return status, parsed
    except urllib.error.HTTPError as exc:
        parsed = parse_json(exc.read())
        runtime.record(
            method=method,
            path=path,
            status=int(exc.code),
            request_body=body,
            response_body=parsed,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        return int(exc.code), parsed
    except Exception as exc:  # noqa: BLE001
        runtime.record(
            method=method,
            path=path,
            status=None,
            request_body=body,
            response_body=None,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def api_data(response: Any, check_name: str, runtime: Runtime) -> dict[str, Any]:
    runtime.check(
        f"{check_name}.envelope",
        isinstance(response, dict) and response.get("code") == 0,
        response,
    )
    data = response.get("data") if isinstance(response, dict) else None
    runtime.check(
        f"{check_name}.data",
        isinstance(data, dict),
        {"response_type": type(data).__name__},
    )
    return data


def login(runtime: Runtime, username: str, password: str) -> str:
    status, response = http_json(
        runtime,
        "POST",
        "/api/v1/auth/login",
        body={"username": username, "password": password},
    )
    runtime.check(f"login.{username}.http", status == 200, status)
    data = api_data(response, f"login.{username}", runtime)
    runtime.check(
        f"login.{username}.role",
        data.get("role") == username or username == "admin",
        {"role": data.get("role"), "expected_username": username},
    )
    token = data.get("access_token")
    runtime.check(
        f"login.{username}.token",
        isinstance(token, str) and len(token) > 20,
        {"token_length": len(token) if isinstance(token, str) else 0},
    )
    return token


def create_event(
    runtime: Runtime,
    *,
    label: str,
    lng: float,
    lat: float,
    seq: int,
) -> str:
    event_id = f"evt_{runtime.scenario_id}_{label}"
    payload = {
        "event_id": event_id,
        "device_id": "CAM-MABI-01",
        "device_type": "shore_camera",
        "timestamp": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
        "location": {"lng": lng, "lat": lat},
        "detections": [
            {
                "class": "plastic",
                "confidence": 0.9,
                "bbox": [1, 1, 10, 10],
            }
        ],
        "aggregate": {
            "main_class": "plastic",
            "count": 1,
            "max_confidence": 0.9,
        },
        "seq": seq,
    }
    status, response = http_json(
        runtime,
        "POST",
        "/api/v1/events",
        body=payload,
    )
    runtime.check(f"event.{label}.http", status == 200, status)
    data = api_data(response, f"event.{label}", runtime)
    runtime.check(
        f"event.{label}.accepted",
        data.get("accepted") is True and data.get("duplicate") is False,
        data,
    )
    runtime.check(
        f"event.{label}.id",
        data.get("event_id") == event_id,
        {"expected": event_id, "actual": data.get("event_id")},
    )
    if runtime.current_phase is not None:
        runtime.current_phase["event_ids"].append(event_id)
    return event_id


def start_agent(
    runtime: Runtime,
    *,
    event_id: str,
    idempotency_key: str,
    token: str,
    label: str,
) -> dict[str, Any]:
    status, response = http_json(
        runtime,
        "POST",
        "/api/v1/agents/runs",
        body={
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "objective": f"WP-18 acceptance {label}",
        },
        token=token,
    )
    runtime.check(f"agent.{label}.start_http", status == 200, status)
    data = api_data(response, f"agent.{label}.start", runtime)
    run_id = data.get("run_id")
    runtime.check(
        f"agent.{label}.run_id",
        isinstance(run_id, str) and run_id.startswith("run_"),
        run_id,
    )
    if runtime.current_phase is not None:
        runtime.current_phase["run_ids"].append(run_id)
    return data


def get_run(runtime: Runtime, run_id: str, token: str, label: str) -> dict[str, Any]:
    status, response = http_json(
        runtime,
        "GET",
        f"/api/v1/agents/runs/{run_id}",
        token=token,
    )
    runtime.check(f"agent.{label}.get_http", status == 200, status)
    return api_data(response, f"agent.{label}.get", runtime)


def get_steps(runtime: Runtime, run_id: str, token: str, label: str) -> list[dict[str, Any]]:
    status, response = http_json(
        runtime,
        "GET",
        f"/api/v1/agents/runs/{run_id}/steps",
        token=token,
    )
    runtime.check(f"agent.{label}.steps_http", status == 200, status)
    runtime.check(
        f"agent.{label}.steps_envelope",
        isinstance(response, dict) and response.get("code") == 0,
        response,
    )
    steps = response.get("data") if isinstance(response, dict) else None
    runtime.check(
        f"agent.{label}.steps_list",
        isinstance(steps, list) and bool(steps),
        {"type": type(steps).__name__, "length": len(steps) if isinstance(steps, list) else None},
    )
    return steps


def assert_trajectory(
    runtime: Runtime,
    steps: list[dict[str, Any]],
    *,
    label: str,
) -> list[str]:
    step_types = [
        str(step.get("step_type"))
        for step in steps
        if isinstance(step, dict) and step.get("step_type")
    ]
    runtime.check(
        f"agent.{label}.trajectory.required",
        EXPECTED_TRAJECTORY.issubset(set(step_types)),
        {
            "expected": sorted(EXPECTED_TRAJECTORY),
            "actual": step_types,
        },
    )
    runtime.check(
        f"agent.{label}.trajectory.bounds",
        step_types[0] == "plan" and step_types[-1] == "terminal",
        {"first": step_types[0] if step_types else None, "last": step_types[-1] if step_types else None},
    )
    runtime.check(
        f"agent.{label}.trajectory.tool_calls",
        step_types.count("tool_call") >= 4,
        {"tool_call_count": step_types.count("tool_call")},
    )
    return step_types


def assert_task(
    runtime: Runtime,
    *,
    task_id: str,
    token: str,
    event_id: str,
    label: str,
) -> None:
    status, response = http_json(
        runtime,
        "GET",
        f"/api/v1/tasks/{task_id}",
        token=token,
    )
    runtime.check(f"task.{label}.http", status == 200, status)
    data = api_data(response, f"task.{label}", runtime)
    runtime.check(
        f"task.{label}.identity",
        data.get("task_id") == task_id and data.get("event_id") == event_id,
        {
            "task_id": data.get("task_id"),
            "event_id": data.get("event_id"),
            "expected_task_id": task_id,
            "expected_event_id": event_id,
        },
    )
    runtime.check(
        f"task.{label}.robot",
        isinstance(data.get("robot_id"), str) and bool(data.get("robot_id")),
        data.get("robot_id"),
    )


def assert_event_dispatched(
    runtime: Runtime,
    *,
    event_id: str,
    label: str,
) -> None:
    status, response = http_json(runtime, "GET", f"/api/v1/events/{event_id}")
    runtime.check(f"event.{label}.get_http", status == 200, status)
    data = api_data(response, f"event.{label}.get", runtime)
    runtime.check(
        f"event.{label}.dispatched",
        data.get("status") == "dispatched",
        data.get("status"),
    )


def start_server(runtime: Runtime, phase_name: str, *, approval: bool) -> None:
    runtime.check(
        "server.port.free_before_start",
        wait_for_port_free(HOST, PORT, timeout=15.0),
        {"host": HOST, "port": PORT},
    )

    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "development",
            "DEBUG": "false",
            "PYTHONIOENCODING": "utf-8",
            "POSTGRES_HOST": str(DB_CONFIG["host"]),
            "POSTGRES_PORT": str(DB_CONFIG["port"]),
            "POSTGRES_DB": str(DB_CONFIG["database"]),
            "POSTGRES_USER": str(DB_CONFIG["user"]),
            "POSTGRES_PASSWORD": str(DB_CONFIG["password"]),
            "AGENT_PERSISTENT_REPOSITORY_ENABLED": "true",
            "AGENT_REQUIRE_APPROVAL_FOR_WRITE": "true" if approval else "false",
            "BACKGROUND_WORKERS_ENABLED": "false",
            "DISPATCH_MERGE_RADIUS_METERS": "0",
            "REDIS_DB": "15",
            "REDIS_STREAM_EVENTS": f"stream:events:{runtime.scenario_id}",
            "REDIS_STREAM_DISPATCH": f"stream:dispatch:{runtime.scenario_id}",
            "REDIS_CONSUMER_GROUP": f"dispatch-group:{runtime.scenario_id}",
            "REDIS_DEAD_LETTER": f"stream:dead_letter:{runtime.scenario_id}",
        }
    )
    command = [
        str(runtime.python),
        "run_server.py",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    cwd = ROOT / "backend"
    runtime.begin_phase(
        phase_name,
        env={
            key: value
            for key, value in env.items()
            if key.startswith(("POSTGRES_", "REDIS_", "AGENT_", "BACKGROUND_", "DISPATCH_"))
        },
        command=command,
        cwd=cwd,
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{phase_name}.log"
    runtime.log_handle = log_path.open("w", encoding="utf-8", errors="replace")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    runtime.process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=runtime.log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )

    deadline = time.monotonic() + 45.0
    last_error: Any = None
    while time.monotonic() < deadline:
        if runtime.process.poll() is not None:
            runtime.check(
                "server.started",
                False,
                {
                    "returncode": runtime.process.returncode,
                    "log": tail_text(log_path),
                },
            )
        try:
            status, response = http_json(runtime, "GET", "/health", timeout=2.0)
            if status == 200:
                runtime.check(
                    "server.health_http",
                    True,
                    {"status": status, "body_status": response.get("status") if isinstance(response, dict) else None},
                )
                return
            last_error = {"status": status, "response": response}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)

    runtime.check(
        "server.started",
        False,
        {"last_error": last_error, "log": tail_text(log_path)},
    )


def stop_server(runtime: Runtime) -> None:
    process = runtime.process
    if process is not None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        runtime.process = None
    if runtime.log_handle is not None:
        runtime.log_handle.close()
        runtime.log_handle = None


def tail_text(path: Path, limit: int = 6000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


async def preflight_database() -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(
        host=str(DB_CONFIG["host"]),
        port=int(DB_CONFIG["port"]),
        database=str(DB_CONFIG["database"]),
        user=str(DB_CONFIG["user"]),
        password=str(DB_CONFIG["password"]),
        timeout=5,
    )
    try:
        identity = await conn.fetchrow(
            """
            SELECT current_database() AS database,
                   current_user AS username,
                   version() AS server_version
            """
        )
        table_rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            ORDER BY table_name
            """,
            list(REQUIRED_TABLES),
        )
        migration = await conn.fetchrow(
            "SELECT version_num FROM alembic_version LIMIT 1"
        )
        return {
            "identity": dict(identity),
            "tables": [row["table_name"] for row in table_rows],
            "migration": migration["version_num"] if migration else None,
        }
    finally:
        await conn.close()


async def database_evidence(
    run_ids: list[str],
    event_ids: list[str],
    approval_id: str,
) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(
        host=str(DB_CONFIG["host"]),
        port=int(DB_CONFIG["port"]),
        database=str(DB_CONFIG["database"]),
        user=str(DB_CONFIG["user"]),
        password=str(DB_CONFIG["password"]),
        timeout=5,
    )
    try:
        run_rows = await conn.fetch(
            """
            SELECT run_id, status, trigger_type, objective, trace_id,
                   termination_reason, created_at, updated_at
            FROM t_agent_run
            WHERE run_id = ANY($1::text[])
            ORDER BY created_at
            """,
            run_ids,
        )
        step_rows = await conn.fetch(
            """
            SELECT run_id, step_no, step_type, status, tool_name,
                   input_hash, output_hash, error_code
            FROM t_agent_step
            WHERE run_id = ANY($1::text[])
            ORDER BY run_id, step_no
            """,
            run_ids,
        )
        state_rows = await conn.fetch(
            """
            SELECT run_id, idempotency_key, state_version, runtime_state_json
            FROM t_agent_run_state
            WHERE run_id = ANY($1::text[])
            ORDER BY run_id
            """,
            run_ids,
        )
        task_rows = await conn.fetch(
            """
            SELECT task_id, event_id, robot_id, status, priority, township
            FROM t_task
            WHERE event_id = ANY($1::text[])
            ORDER BY created_at
            """,
            event_ids,
        )
        event_rows = await conn.fetch(
            """
            SELECT event_id, main_class, status
            FROM t_event
            WHERE event_id = ANY($1::text[])
            ORDER BY event_id
            """,
            event_ids,
        )
        approval_rows = await conn.fetch(
            """
            SELECT approval_id, run_id, risk_level, decision, decided_by, reason
            FROM t_agent_approval
            WHERE approval_id = $1
            """,
            approval_id,
        )

        runtime_states: list[dict[str, Any]] = []
        for row in state_rows:
            record = dict(row)
            try:
                record["runtime_state"] = json.loads(record.pop("runtime_state_json"))
            except (TypeError, ValueError):
                record["runtime_state"] = None
            runtime_states.append(record)

        return {
            "runs": [dict(row) for row in run_rows],
            "steps": [dict(row) for row in step_rows],
            "run_states": runtime_states,
            "tasks": [dict(row) for row in task_rows],
            "events": [dict(row) for row in event_rows],
            "approvals": [dict(row) for row in approval_rows],
        }
    finally:
        await conn.close()


def assert_database(
    runtime: Runtime,
    evidence: dict[str, Any],
    *,
    success_runs: list[str],
    event_ids: list[str],
    approval_id: str,
) -> None:
    runs = {row["run_id"]: row for row in evidence["runs"]}
    states = {row["run_id"]: row for row in evidence["run_states"]}
    tasks = {row["event_id"]: row for row in evidence["tasks"]}
    events = {row["event_id"]: row for row in evidence["events"]}
    approvals = {row["approval_id"]: row for row in evidence["approvals"]}

    runtime.check(
        "db.agent_runs",
        all(run_id in runs and runs[run_id]["status"] == "succeeded" for run_id in success_runs),
        {"expected": success_runs, "actual": sorted(runs)},
    )
    runtime.check(
        "db.agent_steps",
        all(
            len([row for row in evidence["steps"] if row["run_id"] == run_id]) >= 8
            for run_id in success_runs
        ),
        {
            run_id: len([row for row in evidence["steps"] if row["run_id"] == run_id])
            for run_id in success_runs
        },
    )
    runtime.check(
        "db.state_task_result",
        all(
            isinstance(states.get(run_id, {}).get("runtime_state"), dict)
            and isinstance(
                states[run_id]["runtime_state"].get("task_result"),
                dict,
            )
            and states[run_id]["runtime_state"]["task_result"].get("task_id")
            for run_id in success_runs
        ),
        {
            run_id: states.get(run_id, {}).get("runtime_state", {}).get("task_result")
            for run_id in success_runs
        },
    )
    runtime.check(
        "db.tasks",
        all(event_id in tasks and tasks[event_id]["status"] == "assigned" for event_id in event_ids),
        {event_id: tasks.get(event_id) for event_id in event_ids},
    )
    runtime.check(
        "db.events_dispatched",
        all(events.get(event_id, {}).get("status") == "dispatched" for event_id in event_ids),
        {event_id: events.get(event_id) for event_id in event_ids},
    )
    runtime.check(
        "db.approval_decided",
        approval_id in approvals
        and approvals[approval_id]["decision"] == "approved"
        and approvals[approval_id]["decided_by"] == "admin",
        approvals.get(approval_id),
    )


def write_result(runtime: Runtime, *, status: str, failure: str | None) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    passed = len([item for item in runtime.checks if item["passed"]])
    failed = len([item for item in runtime.checks if not item["passed"]])
    result = {
        "schema_version": "wp18-real-state-acceptance.1",
        "scenario_id": runtime.scenario_id,
        "generated_at": utc_now(),
        "status": status,
        "evidence_level": "E2",
        "evidence_scope": (
            "Real HTTP API, authentication/authorization, PostgreSQL persistence, "
            "task creation, WRITE approval, idempotent replay and process-restart "
            "read-only replay using synthetic plastic events."
        ),
        "duration_ms": round((time.perf_counter() - runtime.started) * 1000, 3),
        "summary": {
            "checks_total": len(runtime.checks),
            "checks_passed": passed,
            "checks_failed": failed,
        },
        "failure": failure,
        "phases": runtime.phases,
        "requests": runtime.requests,
        "checks": runtime.checks,
        "preflight": getattr(runtime, "preflight", None),
        "database_evidence": getattr(runtime, "database_evidence", None),
        "limitations": [
            "Synthetic HTTP events are used; no real camera, sea area, user or hardware evidence.",
            "AgentRuntime executes in-process. t_agent_run_state is an audit/read-only restart replay mirror.",
            "Unfinished runs are not claimed to resume after restart.",
            "Background workers are disabled for this acceptance process to prevent automatic dispatch racing the explicit Agent call.",
            "No real order, payment, field pilot, performance or production-readiness claim is made.",
        ],
        "commands": {
            "acceptance": (
                f"{sys.executable} scripts/agent_real_state_acceptance.py"
            ),
            "server": (
                "AGENT_PERSISTENT_REPOSITORY_ENABLED=true "
                "BACKGROUND_WORKERS_ENABLED=false "
                ".venv-analysis/Scripts/python.exe backend/run_server.py "
                "--host 127.0.0.1 --port 8011"
            ),
        },
    }
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def run() -> int:
    runtime = Runtime()
    failure: str | None = None
    status = "failed"

    operator_token = ""
    viewer_token = ""
    admin_token = ""
    first_run = ""
    second_run = ""
    first_event = ""
    second_event = ""
    first_task = ""
    second_task = ""
    approval_id = ""

    try:
        runtime.check(
            "python.exists",
            runtime.python.exists(),
            str(runtime.python),
        )
        runtime.check(
            "port.free",
            wait_for_port_free(HOST, PORT, timeout=0.0),
            {"host": HOST, "port": PORT},
        )
        runtime.preflight = asyncio.run(preflight_database())
        missing_tables = sorted(
            set(REQUIRED_TABLES) - set(runtime.preflight["tables"])
        )
        runtime.check(
            "db.connected",
            bool(runtime.preflight["identity"]["database"]),
            runtime.preflight["identity"],
        )
        runtime.check(
            "db.required_tables",
            not missing_tables,
            {"missing": missing_tables, "present": runtime.preflight["tables"]},
        )

        # Phase 1: baseline success, permissions and replay.
        start_server(runtime, "phase1-baseline", approval=False)
        first_event = create_event(
            runtime,
            label="baseline",
            lng=runtime.baseline_location[0],
            lat=runtime.baseline_location[1],
            seq=int(time.time_ns() % 8_000_000_000) + 1001,
        )
        admin_token = login(runtime, "admin", "admin123456")
        operator_token = login(runtime, "operator", "operator123456")
        viewer_token = login(runtime, "viewer", "viewer123456")

        first_key = f"{runtime.scenario_id}-baseline"
        first = start_agent(
            runtime,
            event_id=first_event,
            idempotency_key=first_key,
            token=operator_token,
            label="baseline",
        )
        first_run = str(first.get("run_id"))
        runtime.check(
            "agent.baseline.succeeded",
            first.get("status") == "succeeded",
            first,
        )
        first_task = str(first.get("task_id") or "")
        runtime.check(
            "agent.baseline.task",
            first.get("task_action") == "created" and bool(first_task),
            {
                "task_id": first_task,
                "task_action": first.get("task_action"),
            },
        )
        first_steps = get_steps(runtime, first_run, operator_token, "baseline")
        assert_trajectory(runtime, first_steps, label="baseline")
        assert_task(
            runtime,
            task_id=first_task,
            token=operator_token,
            event_id=first_event,
            label="baseline",
        )
        assert_event_dispatched(runtime, event_id=first_event, label="baseline")

        replay = start_agent(
            runtime,
            event_id=first_event,
            idempotency_key=first_key,
            token=operator_token,
            label="baseline-replay",
        )
        runtime.check(
            "agent.baseline.replay_same_run",
            replay.get("run_id") == first_run
            and replay.get("idempotent_replay") is True,
            {
                "first_run_id": first_run,
                "replay_run_id": replay.get("run_id"),
                "idempotent_replay": replay.get("idempotent_replay"),
            },
        )

        viewer_status, viewer_response = http_json(
            runtime,
            "POST",
            "/api/v1/agents/runs",
            body={
                "event_id": first_event,
                "idempotency_key": f"{runtime.scenario_id}-viewer-denied",
            },
            token=viewer_token,
        )
        runtime.check("viewer.write.http_403", viewer_status == 403, viewer_status)
        runtime.check(
            "viewer.write.code_1004",
            isinstance(viewer_response, dict) and viewer_response.get("code") == 1004,
            viewer_response,
        )
        runtime.finish_phase("passed")
        stop_server(runtime)

        # Phase 2: WRITE approval request and admin decision.
        start_server(runtime, "phase2-approval", approval=True)
        second_event = create_event(
            runtime,
            label="approval",
            lng=runtime.approval_location[0],
            lat=runtime.approval_location[1],
            seq=int(time.time_ns() % 8_000_000_000) + 2002,
        )
        admin_token = login(runtime, "admin", "admin123456")
        operator_token = login(runtime, "operator", "operator123456")
        second_key = f"{runtime.scenario_id}-approval"
        second = start_agent(
            runtime,
            event_id=second_event,
            idempotency_key=second_key,
            token=operator_token,
            label="approval",
        )
        second_run = str(second.get("run_id"))
        pending_ids = second.get("pending_approval_ids")
        runtime.check(
            "agent.approval.waiting",
            second.get("status") == "waiting_approval"
            and isinstance(pending_ids, list)
            and len(pending_ids) == 1,
            {
                "status": second.get("status"),
                "pending_approval_ids": pending_ids,
            },
        )
        approval_id = str(pending_ids[0])
        second_steps = get_steps(runtime, second_run, operator_token, "approval")
        second_types = [step.get("step_type") for step in second_steps]
        runtime.check(
            "agent.approval.trajectory",
            "approval_request" in second_types,
            second_types,
        )

        approval_status, approval_response = http_json(
            runtime,
            "POST",
            f"/api/v1/agents/approvals/{approval_id}/decide",
            body={"decision": "approved", "reason": "WP-18 acceptance"},
            token=admin_token,
        )
        runtime.check(
            "agent.approval.decide_http",
            approval_status == 200,
            approval_status,
        )
        approved = api_data(approval_response, "agent.approval.decide", runtime)
        second_task = str(approved.get("task_id") or "")
        runtime.check(
            "agent.approval.succeeded",
            approved.get("status") == "succeeded"
            and approved.get("task_action") == "created"
            and bool(second_task),
            approved,
        )
        approved_steps = get_steps(runtime, second_run, admin_token, "approved")
        assert_trajectory(runtime, approved_steps, label="approved")
        assert_task(
            runtime,
            task_id=second_task,
            token=admin_token,
            event_id=second_event,
            label="approval",
        )
        assert_event_dispatched(runtime, event_id=second_event, label="approval")
        runtime.finish_phase("passed")
        stop_server(runtime)

        # Phase 3: fresh process must read the completed run from PostgreSQL.
        start_server(runtime, "phase3-restart-readback", approval=False)
        admin_token = login(runtime, "admin", "admin123456")
        recovered = get_run(runtime, second_run, admin_token, "restart")
        runtime.check(
            "restart.run_status",
            recovered.get("status") == "succeeded",
            recovered,
        )
        runtime.check(
            "restart.task_restored",
            recovered.get("task_id") == second_task
            and recovered.get("task_action") == "created",
            {
                "expected_task_id": second_task,
                "actual_task_id": recovered.get("task_id"),
                "task_action": recovered.get("task_action"),
            },
        )
        recovered_steps = get_steps(
            runtime,
            second_run,
            admin_token,
            "restart",
        )
        recovered_types = assert_trajectory(
            runtime,
            recovered_steps,
            label="restart",
        )
        runtime.check(
            "restart.trajectory_matches",
            recovered_types == [step.get("step_type") for step in approved_steps],
            {
                "before_restart": [step.get("step_type") for step in approved_steps],
                "after_restart": recovered_types,
            },
        )

        restart_replay = start_agent(
            runtime,
            event_id=second_event,
            idempotency_key=second_key,
            token=admin_token,
            label="restart-replay",
        )
        runtime.check(
            "restart.idempotency_from_db",
            restart_replay.get("run_id") == second_run
            and restart_replay.get("idempotent_replay") is True
            and restart_replay.get("task_id") == second_task,
            {
                "expected_run_id": second_run,
                "actual_run_id": restart_replay.get("run_id"),
                "idempotent_replay": restart_replay.get("idempotent_replay"),
                "task_id": restart_replay.get("task_id"),
            },
        )

        runtime.database_evidence = asyncio.run(
            database_evidence(
                [first_run, second_run],
                [first_event, second_event],
                approval_id,
            )
        )
        assert_database(
            runtime,
            runtime.database_evidence,
            success_runs=[first_run, second_run],
            event_ids=[first_event, second_event],
            approval_id=approval_id,
        )
        runtime.finish_phase("passed")
        stop_server(runtime)

        status = "passed"
        print(
            f"[WP-18] PASS checks={len(runtime.checks)} "
            f"run1={first_run} run2={second_run}"
        )
        print(f"[WP-18] evidence={RESULT_PATH}")
        return 0
    except Exception as exc:  # noqa: BLE001
        failure = f"{type(exc).__name__}: {exc}"
        runtime.checks.append(
            {
                "phase": runtime.current_phase["name"] if runtime.current_phase else "global",
                "name": "acceptance.completed",
                "passed": False,
                "detail": failure,
            }
        )
        return 1
    finally:
        if runtime.current_phase is not None:
            runtime.finish_phase("failed")
        stop_server(runtime)
        write_result(runtime, status=status, failure=failure)
        if status != "passed":
            print(f"[WP-18] FAIL {failure}", file=sys.stderr)
            print(f"[WP-18] evidence={RESULT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run())
