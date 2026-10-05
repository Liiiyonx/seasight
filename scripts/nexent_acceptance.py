#!/usr/bin/env python3
"""End-to-end acceptance for the Oceanus MCP server used by Nexent.

The script starts an isolated Streamable HTTP MCP process and verifies:

* HTTP transport refuses to start without an inbound Bearer token;
* unauthenticated protocol requests receive HTTP 401;
* Nexent-style authenticated requests can initialize and list tools;
* all required knowledge-domain tools are registered;
* all five Skill files have valid Nexent front matter.

Pass ``--live-api`` to additionally call ``knowledge_list_assets`` through the
MCP server against a real Oceanus backend. This requires
``SEASIGHT_API_BASE_URL`` and ``SEASIGHT_API_TOKEN`` in the current environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "integrations" / "nexent" / "mcp_server" / "server.py"
SKILLS_ROOT = REPO_ROOT / "integrations" / "nexent" / "skills"
INBOUND_TOKEN = "nexent-acceptance-inbound-token-0123456789abcdef"
OUTBOUND_TOKEN = "nexent-acceptance-outbound-token"
AUTO_LOGIN_USERNAME = "nexent-acceptance"
AUTO_LOGIN_PASSWORD = "nexent-acceptance-password"

REQUIRED_READ_TOOLS = {
    "knowledge_list_assets",
    "knowledge_asset_detail",
    "knowledge_list_ontology_versions",
    "knowledge_ontology_version_detail",
    "knowledge_list_ontology_nodes",
    "knowledge_list_ontology_relations",
    "knowledge_search",
    "knowledge_list_decisions",
    "knowledge_decision_evidence",
}
REQUIRED_WRITE_TOOLS = {
    "knowledge_create_asset",
    "knowledge_append_asset_version",
    "knowledge_create_ontology_version",
    "knowledge_extract_ontology",
    "knowledge_review_node",
    "knowledge_review_relation",
    "knowledge_publish_ontology",
    "knowledge_create_decision",
}
EXPECTED_SKILLS = {
    "cross-document-decision",
    "decision-trace-audit",
    "dispatch-work-order-orchestration",
    "marine-event-assessment",
    "policy-evidence-qa",
}

FRONT_MATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


class AcceptanceError(RuntimeError):
    """A failed acceptance condition."""


class _MockOceanusHandler(BaseHTTPRequestHandler):
    """Minimal Oceanus API used to verify login and token refresh."""

    protocol_version = "HTTP/1.1"
    login_count = 0
    assets_count = 0
    reject_next_token = False
    _state_lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if urlsplit(self.path).path != "/api/v1/auth/login":
            self._write_json(
                404,
                {"code": 404, "message": "not found", "data": None},
            )
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._write_json(
                400,
                {"code": 400, "message": "invalid json", "data": None},
            )
            return

        if (
            request.get("username") != AUTO_LOGIN_USERNAME
            or request.get("password") != AUTO_LOGIN_PASSWORD
        ):
            self._write_json(
                401,
                {"code": 401, "message": "invalid credentials", "data": None},
            )
            return

        with self._state_lock:
            type(self).login_count += 1
            login_count = type(self).login_count
        self._write_json(
            200,
            {
                "code": 0,
                "message": "ok",
                "data": {
                    "access_token": f"mock-token-{login_count}",
                    "token_type": "bearer",
                    "expires_in": 1,
                    "role": "viewer",
                    "full_name": "Nexent acceptance",
                    "township_scope": None,
                },
                "trace_id": "mock-login",
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if urlsplit(self.path).path != "/api/v1/knowledge/assets":
            self._write_json(
                404,
                {"code": 404, "message": "not found", "data": None},
            )
            return

        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer mock-token-"):
            self._write_json(
                401,
                {"code": 401, "message": "unauthorized", "data": None},
            )
            return
        with self._state_lock:
            reject_token = type(self).reject_next_token
            type(self).reject_next_token = False
        if reject_token:
            self._write_json(
                401,
                {"code": 401, "message": "token revoked", "data": None},
            )
            return

        with self._state_lock:
            type(self).assets_count += 1
        self._write_json(
            200,
            {
                "code": 0,
                "message": "ok",
                "data": {"items": [], "total": 0, "page": 1, "page_size": 1},
                "trace_id": "mock-assets",
            },
        )


class _MockOceanus:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _MockOceanusHandler,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mock-seasight",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/api/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[str], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AcceptanceError(
                f"MCP process exited before listening (code {process.returncode})"
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AcceptanceError(f"MCP process did not listen on port {port} within {timeout}s")


def _parse_front_matter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = FRONT_MATTER_RE.match(text)
    if not match:
        raise AcceptanceError(f"{path}: missing YAML front matter")

    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            raise AcceptanceError(f"{path}: unsupported front-matter line: {line!r}")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip("\"'")
    return fields


def _validate_skills() -> list[dict[str, str]]:
    skill_files = sorted(SKILLS_ROOT.glob("*/SKILL.md"))
    if len(skill_files) != len(EXPECTED_SKILLS):
        raise AcceptanceError(
            f"expected {len(EXPECTED_SKILLS)} Skill files, found {len(skill_files)}"
        )

    result: list[dict[str, str]] = []
    found_names: set[str] = set()
    for path in skill_files:
        fields = _parse_front_matter(path)
        name = fields.get("name", "")
        description = fields.get("description", "")
        if name != path.parent.name:
            raise AcceptanceError(
                f"{path}: front-matter name {name!r} does not match directory "
                f"{path.parent.name!r}"
            )
        if len(description) < 40:
            raise AcceptanceError(f"{path}: description is missing or too short")
        found_names.add(name)
        result.append({"name": name, "path": str(path.relative_to(REPO_ROOT))})

    if found_names != EXPECTED_SKILLS:
        raise AcceptanceError(
            "Skill names do not match the expected set: "
            f"missing={sorted(EXPECTED_SKILLS - found_names)}, "
            f"unexpected={sorted(found_names - EXPECTED_SKILLS)}"
        )
    return result


def _assert_http_startup_is_fail_closed(python: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "SEASIGHT_MCP_TRANSPORT": "streamable-http",
            "SEASIGHT_MCP_HOST": "127.0.0.1",
            "SEASIGHT_MCP_PORT": str(_free_port()),
            "SEASIGHT_MCP_PUBLIC_URL": "http://127.0.0.1:9/mcp",
        }
    )
    env.pop("SEASIGHT_MCP_SERVER_TOKEN", None)
    completed = subprocess.run(
        [python, str(SERVER_PATH), "--check"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or "SEASIGHT_MCP_SERVER_TOKEN" not in output:
        raise AcceptanceError(
            "HTTP transport did not fail closed when the inbound token was absent"
        )


def _assert_unauthenticated_request_is_rejected(port: int) -> None:
    response = httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "unauthorized-probe", "version": "1.0"},
            },
        },
        timeout=5,
        trust_env=False,
    )
    if response.status_code != 401:
        raise AcceptanceError(
            "unauthenticated MCP request was not rejected with HTTP 401 "
            f"(got {response.status_code})"
        )


async def _exercise_authenticated_mcp(
    port: int,
    *,
    live_api: bool,
) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {INBOUND_TOKEN}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}

            missing_read = REQUIRED_READ_TOOLS - tool_names
            missing_write = REQUIRED_WRITE_TOOLS - tool_names
            if missing_read:
                raise AcceptanceError(
                    f"missing required read tools: {sorted(missing_read)}"
                )
            if missing_write:
                raise AcceptanceError(
                    f"missing required write tools: {sorted(missing_write)}"
                )

            live_result: dict[str, Any] | None = None
            if live_api:
                call_result = await session.call_tool(
                    "knowledge_list_assets",
                    {"page": 1, "page_size": 1},
                )
                if call_result.isError:
                    raise AcceptanceError(
                        f"live knowledge_list_assets call failed: {call_result.content}"
                    )
                live_result = {
                    "tool": "knowledge_list_assets",
                    "is_error": False,
                }

            return {
                "server_name": initialized.serverInfo.name,
                "server_version": initialized.serverInfo.version,
                "tool_count": len(tool_names),
                "read_tools_present": sorted(REQUIRED_READ_TOOLS),
                "write_tools_present": sorted(REQUIRED_WRITE_TOOLS),
                "live_api_check": live_result,
            }


async def _exercise_token_refresh(port: int) -> dict[str, Any]:
    """Verify one forced 401 retry and one proactive expiry refresh."""

    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {INBOUND_TOKEN}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            first = await session.call_tool(
                "knowledge_list_assets",
                {"page": 1, "page_size": 1},
            )
            if first.isError:
                raise AcceptanceError(
                    f"first auto-login MCP call failed: {first.content}"
                )

            _MockOceanusHandler.reject_next_token = True
            retried = await session.call_tool(
                "knowledge_list_assets",
                {"page": 1, "page_size": 1},
            )
            if retried.isError:
                raise AcceptanceError(
                    f"HTTP 401 retry MCP call failed: {retried.content}"
                )

            await asyncio.sleep(1.2)
            refreshed = await session.call_tool(
                "knowledge_list_assets",
                {"page": 1, "page_size": 1},
            )
            if refreshed.isError:
                raise AcceptanceError(
                    f"proactively refreshed MCP call failed: {refreshed.content}"
                )

    login_count = _MockOceanusHandler.login_count
    assets_count = _MockOceanusHandler.assets_count
    if login_count < 3:
        raise AcceptanceError(
            f"expected at least three logins, got {login_count}"
        )
    if assets_count < 3:
        raise AcceptanceError(
            f"expected three protected API calls, got {assets_count}"
        )
    return {
        "login_count": login_count,
        "protected_api_call_count": assets_count,
        "http_401_retry_verified": True,
        "token_refreshed_after_expiry": True,
    }


def _start_server(
    python: str,
    port: int,
    *,
    live_api: bool,
    api_base_url: str | None = None,
    api_token: str | None = None,
    api_username: str = "",
    api_password: str = "",
    refresh_skew_seconds: float | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    resolved_base_url = api_base_url or os.getenv(
        "SEASIGHT_API_BASE_URL",
        "http://127.0.0.1:9/api/v1",
    )
    resolved_token = api_token
    if resolved_token is None:
        resolved_token = os.getenv("SEASIGHT_API_TOKEN", OUTBOUND_TOKEN)
    if live_api:
        has_static = bool(resolved_token)
        has_credentials = bool(api_username and api_password)
        if not resolved_base_url or not (has_static or has_credentials):
            raise AcceptanceError(
                "--live-api requires SEASIGHT_API_BASE_URL and either "
                "SEASIGHT_API_TOKEN or SEASIGHT_API_USERNAME/"
                "SEASIGHT_API_PASSWORD in the current environment"
            )
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "SEASIGHT_API_BASE_URL": resolved_base_url,
            "SEASIGHT_API_TOKEN": resolved_token,
            "SEASIGHT_API_USERNAME": api_username,
            "SEASIGHT_API_PASSWORD": api_password,
            "SEASIGHT_MCP_ALLOW_WRITES": "true",
            "SEASIGHT_MCP_TRANSPORT": "streamable-http",
            "SEASIGHT_MCP_HOST": "127.0.0.1",
            "SEASIGHT_MCP_PORT": str(port),
            "SEASIGHT_MCP_SERVER_TOKEN": INBOUND_TOKEN,
            "SEASIGHT_MCP_PUBLIC_URL": f"http://127.0.0.1:{port}/mcp",
        }
    )
    if refresh_skew_seconds is not None:
        env["SEASIGHT_API_REFRESH_SKEW_SECONDS"] = str(refresh_skew_seconds)
    return subprocess.Popen(
        [python, "-u", str(SERVER_PATH)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _stop_server(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is None:
        return ""
    return process.stdout.read()


def _assert_token_refresh(python: str) -> dict[str, Any]:
    mock_api = _MockOceanus()
    process: subprocess.Popen[str] | None = None
    try:
        _MockOceanusHandler.login_count = 0
        _MockOceanusHandler.assets_count = 0
        _MockOceanusHandler.reject_next_token = False
        mock_api.start()
        port = _free_port()
        process = _start_server(
            python,
            port,
            live_api=False,
            api_base_url=mock_api.base_url,
            api_token="",
            api_username=AUTO_LOGIN_USERNAME,
            api_password=AUTO_LOGIN_PASSWORD,
            refresh_skew_seconds=0,
        )
        _wait_for_port(port, process)
        return asyncio.run(_exercise_token_refresh(port))
    except Exception:
        logs = _stop_server(process) if process is not None else ""
        if logs:
            print("[nexent-acceptance] token-refresh server logs:", file=sys.stderr)
            print(logs, file=sys.stderr)
        raise
    finally:
        if process is not None:
            _stop_server(process)
        mock_api.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-api",
        action="store_true",
        help="also call knowledge_list_assets against the configured Oceanus backend",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="write the acceptance report JSON to this path (default: stdout only)",
    )
    args = parser.parse_args()

    python = sys.executable
    process: subprocess.Popen[str] | None = None
    try:
        skills = _validate_skills()
        _assert_http_startup_is_fail_closed(python)

        port = _free_port()
        process = _start_server(
            python,
            port,
            live_api=args.live_api,
            api_username=os.getenv("SEASIGHT_API_USERNAME", ""),
            api_password=os.getenv("SEASIGHT_API_PASSWORD", ""),
        )
        _wait_for_port(port, process)
        _assert_unauthenticated_request_is_rejected(port)
        mcp_result = asyncio.run(
            _exercise_authenticated_mcp(port, live_api=args.live_api)
        )
        token_refresh = _assert_token_refresh(python)
    except Exception as exc:  # noqa: BLE001
        logs = _stop_server(process) if process is not None else ""
        print(f"[nexent-acceptance] FAILED: {exc}", file=sys.stderr)
        if logs:
            print("[nexent-acceptance] server logs:", file=sys.stderr)
            print(logs, file=sys.stderr)
        return 1
    else:
        _stop_server(process)

    report = {
        "status": "passed",
        "transport": "streamable-http",
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "acceptance_script": "scripts/nexent_acceptance.py",
        "authentication": {
            "missing_token_fails_closed": True,
            "unauthenticated_request_status": 401,
            "authenticated_initialize": True,
        },
        "skills": skills,
        "mcp": mcp_result,
        "outbound_token_refresh": token_refresh,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.report_path is not None:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(encoded + "\n", encoding="utf-8")
        print(f"[nexent-acceptance] report written: {args.report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
