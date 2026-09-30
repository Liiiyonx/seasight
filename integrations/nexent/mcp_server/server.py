#!/usr/bin/env python3
"""SeaSight MCP server for Nexent.

This process supports both stdio and remote HTTP transports. It never imports
the SeaSight database models or opens an application session. All reads and
writes go through the public FastAPI contract and therefore retain the
backend's authentication, role checks, township scope, audit trail, and
idempotency behavior.

Outbound authentication supports either a static ``SEASIGHT_API_TOKEN`` or a
least-privilege ``SEASIGHT_API_USERNAME`` / ``SEASIGHT_API_PASSWORD`` pair. The
credential mode logs in through the public API, caches the token, refreshes it
before expiry, and retries once after an authentication failure.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import anyio
import httpx
from dotenv import load_dotenv
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PACKAGE_ROOT / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


API_BASE_URL = os.getenv(
    "SEASIGHT_API_BASE_URL",
    "http://127.0.0.1:8000/api/v1",
).rstrip("/")
API_TOKEN = os.getenv("SEASIGHT_API_TOKEN", "").strip()
API_USERNAME = os.getenv("SEASIGHT_API_USERNAME", "").strip()
API_PASSWORD = os.getenv("SEASIGHT_API_PASSWORD", "")
API_REFRESH_SKEW_SECONDS = float(
    os.getenv("SEASIGHT_API_REFRESH_SKEW_SECONDS", "300")
)
ALLOW_WRITES = _env_bool("SEASIGHT_MCP_ALLOW_WRITES", False)
TIMEOUT_SECONDS = float(os.getenv("SEASIGHT_MCP_TIMEOUT_SECONDS", "30"))
TRANSPORT = os.getenv("SEASIGHT_MCP_TRANSPORT", "stdio").strip() or "stdio"
MCP_HOST = os.getenv("SEASIGHT_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
MCP_PORT = int(os.getenv("SEASIGHT_MCP_PORT", "8100"))
MCP_SERVER_TOKEN = os.getenv("SEASIGHT_MCP_SERVER_TOKEN", "").strip()
MCP_PUBLIC_URL = os.getenv(
    "SEASIGHT_MCP_PUBLIC_URL",
    f"http://127.0.0.1:{MCP_PORT}/mcp",
).strip()
HTTP_TRANSPORTS = {"sse", "streamable-http"}


class SeaSightAPIError(RuntimeError):
    """A structured error returned by the SeaSight HTTP API."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        trace_id: Optional[str] = None,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.trace_id = trace_id
        self.http_status = http_status


class SeaSightTokenProvider:
    """Resolve, cache and refresh the outbound SeaSight access token."""

    def __init__(
        self,
        *,
        base_url: str,
        static_token: str,
        username: str,
        password: str,
        refresh_skew_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self._base_url = base_url
        self._static_token = static_token
        self._username = username
        self._password = password
        self._refresh_skew_seconds = max(0.0, refresh_skew_seconds)
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._token = static_token
        self._refresh_at = math.inf if static_token else 0.0

    @property
    def mode(self) -> str:
        if self._static_token:
            return "static_token"
        if self._username and self._password:
            return "username_password"
        return "unconfigured"

    @property
    def can_refresh(self) -> bool:
        return bool(self._username and self._password)

    def headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "seasight-nexent-mcp/1.0",
        }
        token = self._get_token(force_refresh=force_refresh)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def invalidate(self) -> bool:
        """Discard a refreshable token after an HTTP 401 response."""

        if not self.can_refresh:
            return False
        with self._lock:
            self._token = ""
            self._refresh_at = 0.0
        return True

    def _get_token(self, *, force_refresh: bool) -> str:
        if self._static_token:
            return self._static_token
        if not self.can_refresh:
            return ""
        if not force_refresh and self._token and time.monotonic() < self._refresh_at:
            return self._token

        with self._lock:
            if (
                not force_refresh
                and self._token
                and time.monotonic() < self._refresh_at
            ):
                return self._token
            self._token = self._login()
            return self._token

    def _login(self) -> str:
        url = f"{self._base_url}/auth/login"
        try:
            response = httpx.post(
                url,
                json={"username": self._username, "password": self._password},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "seasight-nexent-mcp/1.0",
                },
                timeout=self._timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise SeaSightAPIError(
                f"SeaSight login request failed: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise SeaSightAPIError(
                f"SeaSight login returned non-JSON response "
                f"(HTTP {response.status_code})",
                http_status=response.status_code,
            ) from exc

        if not isinstance(payload, dict) or "code" not in payload:
            raise SeaSightAPIError(
                "SeaSight login response is not an ApiResponse envelope",
                http_status=response.status_code,
            )
        if response.status_code >= 400 or payload.get("code") != 0:
            raise SeaSightAPIError(
                f"SeaSight login failed (HTTP {response.status_code}): "
                f"[{payload.get('code')}] "
                f"{payload.get('message') or 'authentication error'}",
                code=payload.get("code")
                if isinstance(payload.get("code"), int)
                else None,
                trace_id=payload.get("trace_id"),
                http_status=response.status_code,
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise SeaSightAPIError(
                "SeaSight login response did not contain token data",
                http_status=response.status_code,
            )
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise SeaSightAPIError(
                "SeaSight login response did not contain access_token",
                http_status=response.status_code,
            )

        try:
            expires_in = max(1.0, float(data.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        effective_skew = min(
            self._refresh_skew_seconds,
            max(expires_in * 0.1, 0.5),
        )
        self._refresh_at = time.monotonic() + max(
            0.5,
            expires_in - effective_skew,
        )
        return token


token_provider = SeaSightTokenProvider(
    base_url=API_BASE_URL,
    static_token=API_TOKEN,
    username=API_USERNAME,
    password=API_PASSWORD,
    refresh_skew_seconds=API_REFRESH_SKEW_SECONDS,
    timeout_seconds=TIMEOUT_SECONDS,
)


def _clean_params(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


def _call(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
) -> Any:
    """Call SeaSight and unwrap the ``ApiResponse`` envelope.

    ``code != 0`` is always an error. Returning the envelope as data would let
    an agent silently treat a permission or not-found response as success.
    """

    url = f"{API_BASE_URL}/{path.lstrip('/')}"
    try:
        with httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as client:
            response = client.request(
                method,
                url,
                params=params,
                json=body,
                headers=token_provider.headers(),
            )
            if response.status_code == 401 and token_provider.invalidate():
                response = client.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers=token_provider.headers(force_refresh=True),
                )
    except httpx.HTTPError as exc:
        raise SeaSightAPIError(f"SeaSight HTTP request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise SeaSightAPIError(
            f"SeaSight returned non-JSON response (HTTP {response.status_code})",
            http_status=response.status_code,
        ) from exc

    if not isinstance(payload, dict) or "code" not in payload:
        raise SeaSightAPIError(
            "SeaSight response is not an ApiResponse envelope",
            http_status=response.status_code,
        )

    code = payload.get("code")
    if code != 0:
        message = str(payload.get("message") or "SeaSight business error")
        raise SeaSightAPIError(
            f"[{code}] {message}",
            code=code if isinstance(code, int) else None,
            trace_id=payload.get("trace_id"),
            http_status=response.status_code,
        )

    if response.status_code >= 400:
        raise SeaSightAPIError(
            f"SeaSight returned HTTP {response.status_code}",
            trace_id=payload.get("trace_id"),
            http_status=response.status_code,
        )
    return payload.get("data")


def _require_writes_enabled() -> None:
    if not ALLOW_WRITES:
        raise SeaSightAPIError(
            "SeaSight MCP writes are disabled; set "
            "SEASIGHT_MCP_ALLOW_WRITES=true to register write tools"
        )


class StaticBearerTokenVerifier(TokenVerifier):
    """Validate the single Bearer token configured for Nexent inbound access."""

    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self._expected_token or not hmac.compare_digest(token, self._expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="nexent",
            scopes=["seasight.mcp"],
            subject="nexent-mcp-client",
        )


_auth_settings: AuthSettings | None = None
_token_verifier: StaticBearerTokenVerifier | None = None
if TRANSPORT in HTTP_TRANSPORTS and MCP_SERVER_TOKEN:
    _auth_settings = AuthSettings(
        issuer_url=MCP_PUBLIC_URL,
        resource_server_url=None,
        required_scopes=["seasight.mcp"],
    )
    _token_verifier = StaticBearerTokenVerifier(MCP_SERVER_TOKEN)


mcp = FastMCP(
    "SeaSight Domain Cognition MCP",
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path="/mcp",
    auth=_auth_settings,
    token_verifier=_token_verifier,
)


# ---------------------------------------------------------------------------
# Read-only knowledge tools
# ---------------------------------------------------------------------------


@mcp.tool()
def knowledge_list_assets(
    query: Optional[str] = None,
    asset_type: Optional[str] = None,
    status: Optional[str] = None,
    region: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """List registered domain assets with optional text and metadata filters."""

    return _call(
        "GET",
        "/knowledge/assets",
        params=_clean_params(
            query=query,
            asset_type=asset_type,
            status=status,
            region=region,
            page=page,
            page_size=page_size,
        ),
    )


@mcp.tool()
def knowledge_asset_detail(asset_id: str) -> dict[str, Any]:
    """Read one asset and all immutable content versions."""

    return _call("GET", f"/knowledge/assets/{quote(asset_id, safe='')}")


@mcp.tool()
def knowledge_list_ontology_versions() -> list[dict[str, Any]]:
    """List ontology versions and their review or publication status."""

    return _call("GET", "/knowledge/ontology/versions")


@mcp.tool()
def knowledge_ontology_version_detail(version_id: str) -> dict[str, Any]:
    """Read one ontology version."""

    return _call(
        "GET",
        f"/knowledge/ontology/versions/{quote(version_id, safe='')}",
    )


@mcp.tool()
def knowledge_list_ontology_nodes(version_id: str) -> list[dict[str, Any]]:
    """List ontology nodes and their human-review decisions."""

    return _call(
        "GET",
        f"/knowledge/ontology/versions/{quote(version_id, safe='')}/nodes",
    )


@mcp.tool()
def knowledge_list_ontology_relations(version_id: str) -> list[dict[str, Any]]:
    """List ontology relations and their evidence and review decisions."""

    return _call(
        "GET",
        f"/knowledge/ontology/versions/{quote(version_id, safe='')}/relations",
    )


@mcp.tool()
def knowledge_search(
    query: str,
    ontology_version_id: Optional[str] = None,
    hop_depth: int = 2,
    asset_types: Optional[list[str]] = None,
    standard_codes: Optional[list[str]] = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search assets with ontology-guided, cross-document multi-hop retrieval."""

    return _call(
        "POST",
        "/knowledge/search",
        body={
            "query": query,
            "ontology_version_id": ontology_version_id,
            "hop_depth": hop_depth,
            "asset_types": asset_types or [],
            "standard_codes": standard_codes or [],
            "limit": limit,
        },
    )


@mcp.tool()
def knowledge_list_decisions(
    run_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """List decision traces, optionally scoped to one agent run."""

    return _call(
        "GET",
        "/knowledge/decisions",
        params=_clean_params(run_id=run_id, page=page, page_size=page_size),
    )


@mcp.tool()
def knowledge_decision_evidence(trace_id: str) -> list[dict[str, Any]]:
    """Read the ordered evidence chain for one decision trace."""

    return _call(
        "GET",
        f"/knowledge/decisions/{quote(trace_id, safe='')}/evidence",
    )


# ---------------------------------------------------------------------------
# Read-only operational tools
# ---------------------------------------------------------------------------


@mcp.tool()
def event_get(event_id: str) -> dict[str, Any]:
    """Read one event, its status, category, evidence and coordinates."""

    return _call("GET", f"/events/{quote(event_id, safe='')}")


@mcp.tool()
def event_list(
    hours: int = 24,
    main_class: Optional[str] = None,
    status: Optional[str] = None,
    device_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """List environmental events in a time window."""

    return _call(
        "GET",
        "/events",
        params=_clean_params(
            hours=hours,
            main_class=main_class,
            status=status,
            device_id=device_id,
            page=page,
            page_size=page_size,
        ),
    )


@mcp.tool()
def task_get(task_id: str) -> dict[str, Any]:
    """Read one work order and its lifecycle timestamps."""

    return _call("GET", f"/tasks/{quote(task_id, safe='')}")


@mcp.tool()
def task_list(
    status: Optional[str] = None,
    robot_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List work orders with optional status or assigned-resource filters."""

    return _call(
        "GET",
        "/tasks",
        params=_clean_params(
            status=status,
            robot_id=robot_id,
            page=page,
            page_size=page_size,
        ),
    )


@mcp.tool()
def task_ack_history(
    task_id: str,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Read the durable ACK audit history for one work order."""

    return _call(
        "GET",
        f"/tasks/{quote(task_id, safe='')}/acks",
        params={"page": page, "page_size": page_size},
    )


@mcp.tool()
def dashboard_get() -> dict[str, Any]:
    """Read the current governance dashboard metrics."""

    return _call("GET", "/stats/dashboard")


@mcp.tool()
def agent_runtime_status() -> dict[str, Any]:
    """Read the current SeaSight agent runtime state and tool count."""

    return _call("GET", "/agents/runtime/status")


@mcp.tool()
def agent_list_runs(
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """List agent runs using the backend's PageResult envelope data."""

    return _call(
        "GET",
        "/agents/runs",
        params=_clean_params(status=status, page=page, page_size=page_size),
    )


@mcp.tool()
def agent_run_detail(run_id: str) -> dict[str, Any]:
    """Read one agent run, including pending approval identifiers."""

    return _call("GET", f"/agents/runs/{quote(run_id, safe='')}")


@mcp.tool()
def agent_run_steps(run_id: str) -> list[dict[str, Any]]:
    """Read the ordered decision and tool-execution trace of an agent run."""

    return _call("GET", f"/agents/runs/{quote(run_id, safe='')}/steps")


@mcp.tool()
def agent_list_tools() -> list[dict[str, Any]]:
    """Read the backend agent tool catalog, risk levels and schemas."""

    return _call("GET", "/agents/tools")


@mcp.tool()
def agent_list_approvals() -> list[dict[str, Any]]:
    """Read pending and decided human-approval records."""

    return _call("GET", "/agents/approvals")


# ---------------------------------------------------------------------------
# Optional write tools. Registration is gated at import time.
# ---------------------------------------------------------------------------


def knowledge_create_asset(
    asset_type: str,
    title: str,
    description: Optional[str] = None,
    source_uri: Optional[str] = None,
    source_system: Optional[str] = None,
    mime_type: Optional[str] = None,
    region: Optional[str] = None,
    township: Optional[str] = None,
    security_level: str = "internal",
    standard_codes: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    attributes: Optional[dict[str, Any]] = None,
    content_text: Optional[str] = None,
    content_json: Optional[dict[str, Any] | list[Any]] = None,
    extraction_method: str = "manual",
    language: Optional[str] = None,
) -> dict[str, Any]:
    """Register an asset and its first immutable version."""

    _require_writes_enabled()
    initial_content = {
        "content_text": content_text,
        "content_json": content_json,
        "extraction_method": extraction_method,
        "language": language,
        "metadata": {},
    }
    initial_content = {
        key: value for key, value in initial_content.items() if value is not None
    }
    return _call(
        "POST",
        "/knowledge/assets",
        body={
            "asset_type": asset_type,
            "title": title,
            "description": description,
            "source_uri": source_uri,
            "source_system": source_system,
            "mime_type": mime_type,
            "region": region,
            "township": township,
            "security_level": security_level,
            "status": "active",
            "standard_codes": standard_codes or [],
            "tags": tags or [],
            "attributes": attributes or {},
            "initial_content": initial_content,
        },
    )


def knowledge_append_asset_version(
    asset_id: str,
    content_text: Optional[str] = None,
    content_json: Optional[dict[str, Any] | list[Any]] = None,
    extraction_method: str = "manual",
    extraction_confidence: Optional[float] = None,
    language: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Append a new immutable version to an existing asset."""

    _require_writes_enabled()
    return _call(
        "POST",
        f"/knowledge/assets/{quote(asset_id, safe='')}/versions",
        body={
            "content_text": content_text,
            "content_json": content_json,
            "extraction_method": extraction_method,
            "extraction_confidence": extraction_confidence,
            "language": language,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "metadata": metadata or {},
        },
    )


def knowledge_create_ontology_version(
    name: str,
    description: Optional[str] = None,
    version_no: Optional[int] = None,
    parent_version_id: Optional[str] = None,
    standard_codes: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a draft ontology version for candidate extraction."""

    _require_writes_enabled()
    return _call(
        "POST",
        "/knowledge/ontology/versions",
        body={
            "name": name,
            "description": description,
            "version_no": version_no,
            "parent_version_id": parent_version_id,
            "standard_codes": standard_codes or [],
            "metadata": metadata or {},
        },
    )


def knowledge_extract_ontology(
    ontology_version_id: str,
    asset_version_ids: list[str],
    max_nodes: int = 20,
    max_relations: int = 40,
    min_term_length: int = 2,
) -> dict[str, Any]:
    """Generate reviewable ontology node and relation candidates."""

    _require_writes_enabled()
    return _call(
        "POST",
        "/knowledge/ontology/extract",
        body={
            "ontology_version_id": ontology_version_id,
            "asset_version_ids": asset_version_ids,
            "max_nodes": max_nodes,
            "max_relations": max_relations,
            "min_term_length": min_term_length,
        },
    )


def knowledge_review_node(
    node_id: str,
    decision: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Approve or reject one ontology node as an admin or approver."""

    _require_writes_enabled()
    return _call(
        "POST",
        f"/knowledge/ontology/nodes/{quote(node_id, safe='')}/review",
        body={"decision": decision, "reason": reason},
    )


def knowledge_review_relation(
    relation_id: str,
    decision: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Approve or reject one ontology relation as an admin or approver."""

    _require_writes_enabled()
    return _call(
        "POST",
        f"/knowledge/ontology/relations/{quote(relation_id, safe='')}/review",
        body={"decision": decision, "reason": reason},
    )


def knowledge_publish_ontology(version_id: str) -> dict[str, Any]:
    """Publish a fully reviewed ontology version."""

    _require_writes_enabled()
    return _call(
        "POST",
        f"/knowledge/ontology/versions/{quote(version_id, safe='')}/publish",
    )


def knowledge_create_decision(
    question: str,
    answer_summary: Optional[str] = None,
    run_id: Optional[str] = None,
    ontology_version_id: Optional[str] = None,
    policy_version: Optional[str] = None,
    hop_depth: int = 2,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a decision trace; the backend performs evidence retrieval."""

    _require_writes_enabled()
    return _call(
        "POST",
        "/knowledge/decisions",
        body={
            "question": question,
            "answer_summary": answer_summary,
            "run_id": run_id,
            "ontology_version_id": ontology_version_id,
            "policy_version": policy_version,
            "hop_depth": hop_depth,
            "evidence": [],
            "metadata": metadata or {},
        },
    )


def agent_create_run(
    event_id: str,
    idempotency_key: Optional[str] = None,
    objective: Optional[str] = None,
) -> dict[str, Any]:
    """Start an event-dispatch Agent run as an operator or admin."""

    _require_writes_enabled()
    return _call(
        "POST",
        "/agents/runs",
        body={
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "objective": objective,
        },
    )


def agent_cancel_run(run_id: str, reason: str = "MCP operator cancel") -> dict[str, Any]:
    """Cancel a non-terminal Agent run as an operator or admin."""

    _require_writes_enabled()
    return _call(
        "POST",
        f"/agents/runs/{quote(run_id, safe='')}/cancel",
        body={"reason": reason},
    )


def agent_decide_approval(
    approval_id: str,
    decision: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Approve or reject one Agent action as an admin or approver."""

    _require_writes_enabled()
    return _call(
        "POST",
        f"/agents/approvals/{quote(approval_id, safe='')}/decide",
        body={"decision": decision, "reason": reason},
    )


WRITE_TOOLS = (
    knowledge_create_asset,
    knowledge_append_asset_version,
    knowledge_create_ontology_version,
    knowledge_extract_ontology,
    knowledge_review_node,
    knowledge_review_relation,
    knowledge_publish_ontology,
    knowledge_create_decision,
    agent_create_run,
    agent_cancel_run,
    agent_decide_approval,
)

if ALLOW_WRITES:
    for _tool in WRITE_TOOLS:
        mcp.tool()(_tool)


def _configuration_errors() -> list[str]:
    errors: list[str] = []
    if TRANSPORT not in {"stdio", *HTTP_TRANSPORTS}:
        errors.append(
            "SEASIGHT_MCP_TRANSPORT must be stdio, sse, or streamable-http"
        )
    if TRANSPORT in HTTP_TRANSPORTS and not MCP_SERVER_TOKEN:
        errors.append(
            "SEASIGHT_MCP_SERVER_TOKEN is required for HTTP transports"
        )
    elif TRANSPORT in HTTP_TRANSPORTS and len(MCP_SERVER_TOKEN) < 32:
        errors.append(
            "SEASIGHT_MCP_SERVER_TOKEN must contain at least 32 characters"
        )
    if not 1 <= MCP_PORT <= 65535:
        errors.append("SEASIGHT_MCP_PORT must be between 1 and 65535")
    if bool(API_USERNAME) != bool(API_PASSWORD):
        errors.append(
            "SEASIGHT_API_USERNAME and SEASIGHT_API_PASSWORD must be set together"
        )
    if token_provider.mode == "unconfigured":
        errors.append(
            "configure SEASIGHT_API_TOKEN or "
            "SEASIGHT_API_USERNAME/SEASIGHT_API_PASSWORD"
        )
    if API_REFRESH_SKEW_SECONDS < 0:
        errors.append("SEASIGHT_API_REFRESH_SKEW_SECONDS must be non-negative")
    return errors


def _endpoint_path() -> str:
    return "/sse" if TRANSPORT == "sse" else "/mcp"


def _check_payload() -> dict[str, Any]:
    registered_tools = sorted(tool.name for tool in anyio.run(mcp.list_tools))
    errors = _configuration_errors()
    return {
        "name": "SeaSight Domain Cognition MCP",
        "valid": not errors,
        "configuration_errors": errors,
        "api_base_url": API_BASE_URL,
        "outbound_auth_mode": token_provider.mode,
        "outbound_token_configured": token_provider.mode != "unconfigured",
        "outbound_auto_refresh": token_provider.can_refresh
        and not bool(API_TOKEN),
        "outbound_username_configured": bool(API_USERNAME),
        "outbound_password_configured": bool(API_PASSWORD),
        "inbound_token_configured": bool(MCP_SERVER_TOKEN),
        "allow_writes": ALLOW_WRITES,
        "transport": TRANSPORT,
        "host": MCP_HOST,
        "port": MCP_PORT,
        "path": _endpoint_path(),
        "public_url": MCP_PUBLIC_URL,
        "registered_tool_count": len(registered_tools),
        "registered_tools": registered_tools,
        "write_tools_registered": len(WRITE_TOOLS) if ALLOW_WRITES else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="SeaSight MCP server for Nexent")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and imports without opening a transport",
    )
    args = parser.parse_args()

    if args.check:
        payload = _check_payload()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if not payload["valid"]:
            raise SystemExit(1)
        return

    errors = _configuration_errors()
    if errors:
        for error in errors:
            print(f"[nexent-mcp] configuration error: {error}", file=sys.stderr)
        raise SystemExit(2)

    mcp.run(transport=TRANSPORT)


if __name__ == "__main__":
    main()
