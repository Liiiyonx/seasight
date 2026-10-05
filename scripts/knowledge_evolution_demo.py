#!/usr/bin/env python3
"""Run the knowledge evolution loop against a real Oceanus /api/v1 backend.

The script exercises the complete internal loop:

    register assets -> append versions -> create ontology version
    -> extract candidates -> review nodes/relations -> publish
    -> multi-hop search -> create decision -> read evidence chain

Evidence level: E1/E2. The loop is a software integration demo on the
project backend, not a real deployment or field acceptance result. It does
not imply detection accuracy or any E3/E4 evidence.

When no credentials or backend are available, the script writes an explicit
``not_configured`` / ``backend_unavailable`` result and exits non-zero. It
never fabricates a successful run.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "artifacts" / "evolution-demo" / "latest.json"
DEFAULT_BASE_URL = os.getenv(
    "SEASIGHT_API_BASE_URL", "http://127.0.0.1:8000/api/v1"
).rstrip("/")

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_FAILED = 3


class DemoFailure(RuntimeError):
    """A mandatory step of the evolution loop failed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_stamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


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


class ApiClient:
    """Minimal JSON client for the Oceanus /api/v1 HTTP contract."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = token
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        body = None
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Oceanus-KnowledgeEvolutionDemo/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        started = time.monotonic()
        error: str | None = None
        status: int | None = None
        parsed: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(request, timeout=15.0) as response:
                raw = response.read()
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read()
            except OSError:
                raw = b""
            error = f"HTTP {exc.code}"
        except (TimeoutError, socket.timeout) as exc:
            error = f"timeout: {exc}"
        except urllib.error.URLError as exc:
            error = f"connection_failed: {exc.reason}"
        except OSError as exc:
            error = f"connection_failed: {exc}"

        if raw and not error:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                error = f"invalid_json: {exc}"

        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        self.requests.append(
            {
                "at": utc_now(),
                "method": method,
                "path": path,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "request": redact(payload),
                "response": redact(parsed),
                "error": error,
            }
        )
        if error:
            raise DemoFailure(f"{method} {path}: {error}")
        return status or 0, parsed

    def api(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        status, body = self.request(method, path, payload)
        if not isinstance(body, dict) or body.get("code", 0) != 0:
            raise DemoFailure(
                f"{method} {path}: business error code={body.get('code')} "
                f"message={body.get('message')} status={status}"
            )
        return body.get("data")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Oceanus API base URL, for example http://127.0.0.1:8000/api/v1",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("SEASIGHT_API_USERNAME", "admin"),
        help="account for the full loop (needs operator + ontology review rights)",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("SEASIGHT_API_PASSWORD", ""),
        help="account password; prefer SEASIGHT_API_PASSWORD",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("SEASIGHT_API_TOKEN", ""),
        help="optional pre-issued bearer token; skips login",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="JSON report path",
    )
    parser.add_argument(
        "--asset-prefix",
        default="EVO",
        help="prefix for demo asset IDs",
    )
    return parser.parse_args(argv)


def build_demo_texts(asset_id: str) -> tuple[str, str]:
    """Two small governance snippets with enough shared terms for extraction."""
    document_text = (
        f"连江县海漂垃圾治理工作方案（{asset_id}）明确：岸基摄像头发现泡沫、塑料、"
        "渔网等海漂垃圾后，由值班研判人员复核，再通过派单平台调度打捞机器人或机械臂执行拾取。"
        "重点区域包括马鼻镇、黄岐镇、筱埕镇、苔菉镇、安凯镇和下宫镇。"
        "拾取完成后回传照片与称重数据，形成处置闭环。"
    )
    table_text = (
        f"连江县重点区域海漂垃圾月度台账（{asset_id}）显示：马鼻镇泡沫聚集次数最多，"
        "黄岐镇塑料和渔网占比高，筱埕镇养殖区存在网绳缠绕风险。"
        "处置资源包括打捞机器人、岸基机械臂和人工回收队伍；"
        "每条任务记录关联摄像头编号、值班审批人、派单时间和拾取回执。"
    )
    return document_text, table_text


def run_demo(
    client: ApiClient,
    output: Path,
    *,
    asset_prefix: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    stamp = run_stamp()
    nonce = uuid.uuid4().hex[:8]
    asset_doc = f"{asset_prefix}-{stamp}-{nonce}-POLICY"
    asset_tbl = f"{asset_prefix}-{stamp}-{nonce}-LEDGER"
    if len(asset_doc) > 64 or len(asset_tbl) > 64:
        raise DemoFailure("generated asset_id exceeds the 64-char contract limit")

    steps: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "status": "running",
        "generated_at": utc_now(),
        "evidence_level": "E1/E2",
        "scope_note": (
            "软件内部闭环演示；不构成真实部署、真实海域验证或感知精度证据"
        ),
        "base_url": client.base_url,
        "asset_prefix": asset_prefix,
        "steps": steps,
    }

    def step(name: str, detail: dict[str, Any], ids: list[str] | None = None) -> dict[str, Any]:
        entry = {
            "name": name,
            "at": utc_now(),
            "status": "ok",
            "ids": ids or [],
            "detail": detail,
        }
        steps.append(entry)
        return entry

    try:
        document_text, table_text = build_demo_texts(asset_doc)

        if client.token:
            step("authenticate", {"source": "pre_issued_token"})
        else:
            login_data = client.api(
                "POST",
                "/auth/login",
                {"username": username, "password": password},
            )
            client.token = str(login_data["access_token"])
            step(
                "login",
                {
                    "role": login_data.get("role"),
                    "token_type": login_data.get("token_type"),
                    "expires_in": login_data.get("expires_in"),
                },
                ids=[username],
            )

        doc_asset = client.api(
            "POST",
            "/knowledge/assets",
            {
                "asset_id": asset_doc,
                "asset_type": "document",
                "title": "连江海漂垃圾治理工作方案（演示）",
                "description": "知识进化闭环演示用治理方案资产",
                "region": "连江县",
                "township": "马鼻镇",
                "security_level": "internal",
                "tags": ["治理方案", "闭环演示"],
                "initial_content": {
                    "content_text": document_text,
                    "extraction_method": "manual",
                    "metadata": {"demo_role": "policy"},
                },
            },
        )
        doc_asset_id = doc_asset["asset"]["asset_id"]
        doc_v1 = doc_asset["versions"][0]["version_id"]
        step(
            "register_policy_asset",
            {"asset_type": "document", "current_version": doc_asset["asset"]["current_version"]},
            ids=[doc_asset_id, doc_v1],
        )

        tbl_asset = client.api(
            "POST",
            "/knowledge/assets",
            {
                "asset_id": asset_tbl,
                "asset_type": "table",
                "title": "连江重点区域海漂垃圾月度台账（演示）",
                "description": "知识进化闭环演示用台账资产",
                "region": "连江县",
                "township": "马鼻镇",
                "security_level": "internal",
                "tags": ["月度台账", "闭环演示"],
                "initial_content": {
                    "content_text": table_text,
                    "extraction_method": "manual",
                    "metadata": {"demo_role": "ledger"},
                },
            },
        )
        tbl_asset_id = tbl_asset["asset"]["asset_id"]
        tbl_v1 = tbl_asset["versions"][0]["version_id"]
        step(
            "register_ledger_asset",
            {"asset_type": "table", "current_version": tbl_asset["asset"]["current_version"]},
            ids=[tbl_asset_id, tbl_v1],
        )

        doc_v2 = client.api(
            "POST",
            f"/knowledge/assets/{doc_asset_id}/versions",
            {
                "content_text": document_text + " 修订：新增岸基机械臂作为可插拔执行末端。",
                "extraction_method": "manual",
                "metadata": {"demo_role": "policy", "revision": 2},
            },
        )
        doc_v2_id = doc_v2["version_id"]
        step(
            "append_policy_version",
            {"version_no": doc_v2["version_no"], "content_hash": doc_v2["content_hash"]},
            ids=[doc_v2_id],
        )

        tbl_v2 = client.api(
            "POST",
            f"/knowledge/assets/{tbl_asset_id}/versions",
            {
                "content_text": table_text + " 修订：补充机械臂拾取回执统计口径。",
                "extraction_method": "manual",
                "metadata": {"demo_role": "ledger", "revision": 2},
            },
        )
        tbl_v2_id = tbl_v2["version_id"]
        step(
            "append_ledger_version",
            {"version_no": tbl_v2["version_no"], "content_hash": tbl_v2["content_hash"]},
            ids=[tbl_v2_id],
        )

        ontology = client.api(
            "POST",
            "/knowledge/ontology/versions",
            {
                "name": "连江海漂治理本体（演示）",
                "description": "知识进化闭环演示本体",
                "standard_codes": ["marine-litter", "lianjiang"],
                "metadata": {"demo_role": "ontology"},
            },
        )
        ontology_version_id = ontology["version_id"]
        step(
            "create_ontology_version",
            {"version_no": ontology["version_no"], "status": ontology["status"]},
            ids=[ontology_version_id],
        )

        extracted = client.api(
            "POST",
            "/knowledge/ontology/extract",
            {
                "ontology_version_id": ontology_version_id,
                "asset_version_ids": [doc_v2_id, tbl_v2_id],
                "max_nodes": 20,
                "max_relations": 40,
                "min_term_length": 2,
            },
        )
        nodes = extracted["nodes"]
        relations = extracted["relations"]
        step(
            "extract_ontology_candidates",
            {
                "created_nodes": extracted["created_nodes"],
                "created_relations": extracted["created_relations"],
                "all_candidates_unreviewed": all(
                    node["review_status"] == "proposed" for node in nodes
                )
                and all(
                    relation["review_status"] == "proposed" for relation in relations
                ),
            },
            ids=[ontology_version_id],
        )
        if not nodes:
            raise DemoFailure("ontology extract returned zero nodes; cannot publish")

        for node in nodes:
            reviewed = client.api(
                "POST",
                f"/knowledge/ontology/nodes/{node['node_id']}/review",
                {"decision": "approved", "reason": "evolution demo reviewer"},
            )
            if reviewed["review_status"] != "approved":
                raise DemoFailure(f"node review failed: {node['node_id']}")
        step(
            "review_ontology_nodes",
            {"reviewed": len(nodes), "decision": "approved"},
            ids=[node["node_id"] for node in nodes],
        )

        for relation in relations:
            reviewed = client.api(
                "POST",
                f"/knowledge/ontology/relations/{relation['relation_id']}/review",
                {"decision": "approved", "reason": "evolution demo reviewer"},
            )
            if reviewed["review_status"] != "approved":
                raise DemoFailure(f"relation review failed: {relation['relation_id']}")
        step(
            "review_ontology_relations",
            {"reviewed": len(relations), "decision": "approved"},
            ids=[relation["relation_id"] for relation in relations],
        )

        published = client.api(
            "POST",
            f"/knowledge/ontology/versions/{ontology_version_id}/publish",
        )
        step(
            "publish_ontology",
            {
                "status": published["version"]["status"],
                "approved_nodes": published["approved_nodes"],
                "rejected_nodes": published["rejected_nodes"],
                "approved_relations": published["approved_relations"],
                "rejected_relations": published["rejected_relations"],
            },
            ids=[ontology_version_id],
        )

        search = client.api(
            "POST",
            "/knowledge/search",
            {
                "query": "马鼻镇泡沫垃圾机械臂拾取处置闭环",
                "ontology_version_id": ontology_version_id,
                "hop_depth": 2,
                "asset_types": ["document", "table"],
                "limit": 10,
            },
        )
        step(
            "multi_hop_search",
            {
                "mode": search["mode"],
                "total": search["total"],
                "hit_count": len(search["results"]),
                "path_evidence_count": sum(
                    1 for hit in search["results"] if hit.get("path")
                ),
            },
            ids=[ontology_version_id],
        )
        if not search["results"]:
            raise DemoFailure("multi-hop search returned no hits")

        evidence: list[dict[str, Any]] = []
        for hit in search["results"][:5]:
            evidence.append(
                {
                    "asset_id": hit["asset_id"],
                    "asset_version_id": hit["asset_version_id"],
                    "node_id": hit["matched_node_ids"][0] if hit["matched_node_ids"] else None,
                    "relation_id": hit["matched_relation_ids"][0] if hit["matched_relation_ids"] else None,
                    "hop_no": hit["hop_count"],
                    "citation_text": (
                        hit["citations"][0] if hit["citations"] else hit["snippet"] or hit["title"]
                    ),
                    "source_uri": hit["source_uri"],
                    "score": hit["score"],
                    "metadata": {
                        "path": hit["path"],
                        "citations": hit["citations"],
                        "matched_node_ids": hit["matched_node_ids"],
                    },
                }
            )

        decision = client.api(
            "POST",
            "/knowledge/decisions",
            {
                "question": "马鼻镇出现泡沫聚集时，应如何处置并留存证据链？",
                "answer_summary": (
                    "根据治理方案与月度台账，由值班研判人员复核后派单，"
                    "调度岸基机械臂或打捞机器人拾取，并回传拾取回执形成闭环。"
                ),
                "run_id": f"evo-{stamp}-{nonce}",
                "ontology_version_id": ontology_version_id,
                "policy_version": "policy-demo-v2",
                "hop_depth": 2,
                "evidence": evidence,
                "metadata": {"demo_role": "evolution-loop"},
            },
        )
        trace_id = decision["trace_id"]
        step(
            "create_decision",
            {
                "status": decision["status"],
                "evidence_in_decision": len(decision.get("evidence") or []),
            },
            ids=[trace_id],
        )

        evidence_chain = client.api(
            "GET",
            f"/knowledge/decisions/{trace_id}/evidence",
        )
        step(
            "read_decision_evidence",
            {
                "evidence_count": len(evidence_chain),
                "rank_sequence_ok": [item["rank_no"] for item in evidence_chain]
                == list(range(1, len(evidence_chain) + 1)),
            },
            ids=[trace_id],
        )

        result["status"] = "verified"
        result["ids"] = {
            "asset_document": doc_asset_id,
            "asset_ledger": tbl_asset_id,
            "asset_versions": [doc_v1, doc_v2_id, tbl_v1, tbl_v2_id],
            "ontology_version_id": ontology_version_id,
            "trace_id": trace_id,
        }
        result["counts"] = {
            "assets": 2,
            "asset_versions": 4,
            "ontology_candidates": len(nodes) + len(relations),
            "search_hits": len(search["results"]),
            "evidence_items": len(evidence_chain),
        }
        result["requests"] = client.requests
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[knowledge-evolution] status=verified trace={trace_id}")
        print(f"[knowledge-evolution] report={output}")
        return result
    except DemoFailure as exc:
        if "connection_failed" in str(exc) or "timeout" in str(exc):
            result["status"] = "backend_unavailable"
        else:
            result["status"] = "failed"
        result["error"] = str(exc)
        result["requests"] = client.requests
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    output = Path(args.output)
    if not args.token and not (args.username and args.password):
        report = {
            "status": "not_configured",
            "generated_at": utc_now(),
            "evidence_level": "E1/E2",
            "error": "SEASIGHT_API_USERNAME/SEASIGHT_API_PASSWORD or --token required",
            "required": [
                "SEASIGHT_API_BASE_URL",
                "SEASIGHT_API_USERNAME",
                "SEASIGHT_API_PASSWORD",
            ],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print("[knowledge-evolution] not_configured", file=sys.stderr)
        print(
            "        set SEASIGHT_API_BASE_URL, SEASIGHT_API_USERNAME, "
            "SEASIGHT_API_PASSWORD or pass --token",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    client = ApiClient(args.base_url, args.token)
    try:
        run_demo(
            client,
            output,
            asset_prefix=args.asset_prefix,
            username=args.username,
            password=args.password,
        )
    except DemoFailure as exc:
        print(f"[knowledge-evolution] failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK

if __name__ == "__main__":
    raise SystemExit(main())
