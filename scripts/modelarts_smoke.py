#!/usr/bin/env python3
"""Call one real OpenAI-compatible model endpoint and write an evidence record.

The endpoint is configured through the same environment variables as the
SeaSight planner adapter:

    AGENT_MODEL_BASE_URL
    AGENT_MODEL_API_KEY
    AGENT_MODEL_NAME
    AGENT_MODEL_ADAPTER_TIMEOUT_MS

This is a smoke test for the ModelArts online-service path. A verified call
records ``source=model`` and proves that the configured endpoint accepted a
Chat Completions request. It does not prove field detection accuracy, does
not replace E3 evidence, and does not imply a production SLA.

When configuration is missing, the script writes ``not_configured`` and
exits non-zero instead of pretending a call happened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "artifacts" / "modelarts-real-call" / "latest.json"

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_FAILED = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("AGENT_MODEL_BASE_URL", ""),
        help="ModelArts online-service or OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("AGENT_MODEL_NAME", ""),
        help="model name reported by the endpoint",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("AGENT_MODEL_API_KEY", ""),
        help="API key; never written to the evidence file",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=int(os.getenv("AGENT_MODEL_ADAPTER_TIMEOUT_MS", "5000")),
        help="request timeout in milliseconds",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=64,
        help="max_tokens sent to the endpoint",
    )
    parser.add_argument(
        "--prompt",
        default="请用不超过 50 字概括海漂垃圾治理闭环，并以 JSON 返回 {\"answer\": \"...\"}。",
        help="prompt sent to the endpoint",
    )
    parser.add_argument(
        "--label",
        default="modelarts-smoke",
        help="short label stored in the evidence record",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="JSON report path",
    )
    return parser.parse_args(argv)


def _chat_completions_url(base_url: str) -> str:
    endpoint = str(base_url or "").strip().rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    return f"{endpoint}/chat/completions"


def _extract_content(envelope: Any) -> str:
    if not isinstance(envelope, dict):
        raise ValueError("response is not a JSON object")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("choices[0] is not an object")
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = first.get("text")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "".join(parts)
    raise ValueError("response content is empty or unsupported")


def run_smoke(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    base_url = str(args.base_url or "").strip()
    model = str(args.model or "").strip()
    output = Path(args.output)

    if not base_url or not model:
        report = {
            "status": "not_configured",
            "generated_at": utc_now(),
            "evidence_level": "E1/E2",
            "error": "AGENT_MODEL_BASE_URL and AGENT_MODEL_NAME are required",
            "required": [
                "AGENT_MODEL_BASE_URL",
                "AGENT_MODEL_API_KEY",
                "AGENT_MODEL_NAME",
            ],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return EXIT_NOT_CONFIGURED, report

    request_url = _chat_completions_url(base_url)
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": args.prompt}],
            "temperature": 0,
            "max_tokens": args.max_tokens,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "SeaSight-ModelArtsSmoke/1.0",
    }
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    request = urllib.request.Request(
        request_url,
        data=body,
        headers=headers,
        method="POST",
    )

    started = time.monotonic()
    status: str | None = None
    http_status: int | None = None
    error: str | None = None
    content: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=args.timeout_ms / 1000) as response:
            raw = response.read()
            http_status = int(response.status)
        envelope = json.loads(raw.decode("utf-8"))
        content = _extract_content(envelope)
        status = "verified"
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        error = f"HTTP {exc.code}"
        status = "http_error"
    except (TimeoutError, socket.timeout):
        error = "timeout"
        status = "timeout"
    except urllib.error.URLError as exc:
        error = f"connection_failed: {exc.reason}"
        status = "connection_failed"
    except OSError as exc:
        error = f"connection_failed: {exc}"
        status = "connection_failed"
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        error = f"invalid_response: {exc}"
        status = "invalid_response"

    latency_ms = round((time.monotonic() - started) * 1000, 3)
    report: dict[str, Any] = {
        "status": status,
        "generated_at": utc_now(),
        "evidence_level": "E1/E2",
        "source": "model" if status == "verified" else None,
        "model": model,
        "label": args.label,
        "latency_ms": latency_ms,
        "http_status": http_status,
        "request": {
            "endpoint": request_url,
            "prompt_sha256": hashlib.sha256(
                args.prompt.encode("utf-8")
            ).hexdigest(),
            "max_tokens": args.max_tokens,
        },
        "response": {
            "completion_preview": (
                (content[:200] + "..." if len(content) > 200 else content)
                if content is not None
                else None
            ),
            "completion_sha256": (
                hashlib.sha256(content.encode("utf-8")).hexdigest()
                if content is not None
                else None
            ),
        },
        "error": error,
        "note": (
            "真实 Chat Completions 调用记录；不是海域验证，不代表检测精度，"
            "不代表生产 SLA"
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return EXIT_OK if status == "verified" else EXIT_FAILED, report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    code, report = run_smoke(args)
    print(f"[modelarts-smoke] status={report['status']} source={report.get('source')}")
    if code != EXIT_OK:
        print(f"[modelarts-smoke] error={report.get('error')}", file=sys.stderr)
    else:
        print(f"[modelarts-smoke] latency_ms={report['latency_ms']}")
        print(f"[modelarts-smoke] report={args.output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
