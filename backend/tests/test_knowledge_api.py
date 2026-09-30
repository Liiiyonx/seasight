"""HTTP contract and role-boundary tests for the knowledge domain."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.knowledge import router
from app.core.deps import CurrentUser, get_current_user
from app.db.session import Base, get_session
from app.middleware.response import register_exception_handlers
from app.models.knowledge import (
    DecisionEvidence,
    DecisionTrace,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    OntologyNode,
    OntologyRelation,
    OntologyVersion,
)
from app.models.misc import AuditLog


KNOWLEDGE_TABLES = [
    KnowledgeAsset.__table__,
    KnowledgeAssetVersion.__table__,
    OntologyVersion.__table__,
    OntologyNode.__table__,
    OntologyRelation.__table__,
    DecisionTrace.__table__,
    DecisionEvidence.__table__,
    AuditLog.__table__,
]


@pytest_asyncio.fixture
async def knowledge_api() -> AsyncGenerator[
    tuple[AsyncClient, dict[str, Any]],
    None,
]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=KNOWLEDGE_TABLES,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1/knowledge")
    identity: dict[str, Any] = {
        "username": "admin",
        "role": "admin",
        "township_scope": None,
    }

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username=identity["username"],
        role=identity["role"],
        township_scope=identity["township_scope"],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client, identity

    app.dependency_overrides.clear()
    await engine.dispose()


def act_as(
    identity: dict[str, Any],
    *,
    username: str,
    role: str,
    township_scope: str | None = None,
) -> None:
    identity.update(
        username=username,
        role=role,
        township_scope=township_scope,
    )


async def create_asset(
    client: AsyncClient,
    *,
    title: str,
    township: str | None,
    content: str,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/knowledge/assets",
        json={
            "asset_type": "document",
            "title": title,
            "township": township,
            "initial_content": {"content_text": content},
        },
    )
    body = response.json()
    assert response.status_code == 200, body
    assert body["code"] == 0, body
    return body["data"]


@pytest.mark.asyncio
async def test_operator_scope_on_asset_extract_and_decision(
    knowledge_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, identity = knowledge_api
    admin_asset = await create_asset(
        client,
        title="马鼻镇红树林资料",
        township="马鼻镇",
        content="红树林保护和清理记录。",
    )

    act_as(identity, username="operator-b", role="operator", township_scope="苔菉镇")
    denied = await client.post(
        "/api/v1/knowledge/assets",
        json={
            "asset_type": "document",
            "title": "越权资产",
            "township": "马鼻镇",
            "initial_content": {"content_text": "不应创建"},
        },
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == 1004

    own_asset = await create_asset(
        client,
        title="苔菉镇海漂垃圾资料",
        township=None,
        content="苔菉镇辖区清理记录。",
    )
    assert own_asset["asset"]["township"] == "苔菉镇"

    listed = await client.get("/api/v1/knowledge/assets")
    assert listed.status_code == 200
    assert [item["asset_id"] for item in listed.json()["data"]["items"]] == [
        own_asset["asset"]["asset_id"]
    ]

    ontology = await client.post(
        "/api/v1/knowledge/ontology/versions",
        json={"name": "scope-domain"},
    )
    assert ontology.json()["code"] == 0
    ontology_version_id = ontology.json()["data"]["version_id"]

    extract = await client.post(
        "/api/v1/knowledge/ontology/extract",
        json={
            "ontology_version_id": ontology_version_id,
            "asset_version_ids": [admin_asset["versions"][0]["version_id"]],
        },
    )
    assert extract.status_code == 403
    assert extract.json()["code"] == 1004

    decision = await client.post(
        "/api/v1/knowledge/decisions",
        json={
            "question": "能否引用其他辖区资料？",
            "evidence": [
                {
                    "asset_id": admin_asset["asset"]["asset_id"],
                    "asset_version_id": admin_asset["versions"][0]["version_id"],
                    "citation_text": "越权证据",
                }
            ],
        },
    )
    assert decision.status_code == 403
    assert decision.json()["code"] == 1004


@pytest.mark.asyncio
async def test_review_publication_and_search_role_matrix(
    knowledge_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, identity = knowledge_api
    asset = await create_asset(
        client,
        title="红树林生态修复规范",
        township="马鼻镇",
        content="红树林生态修复需要监测水质。红树林生态修复需要保留潮沟。",
    )
    ontology = await client.post(
        "/api/v1/knowledge/ontology/versions",
        json={"name": "review-domain"},
    )
    ontology_version_id = ontology.json()["data"]["version_id"]
    extracted = await client.post(
        "/api/v1/knowledge/ontology/extract",
        json={
            "ontology_version_id": ontology_version_id,
            "asset_version_ids": [asset["versions"][0]["version_id"]],
            "max_nodes": 10,
            "max_relations": 10,
        },
    )
    assert extracted.status_code == 200
    extraction = extracted.json()
    assert extraction["code"] == 0
    assert extraction["data"]["created_nodes"] > 0
    assert all(
        node["review_status"] == "proposed"
        for node in extraction["data"]["nodes"]
    )

    act_as(identity, username="viewer", role="viewer")
    denied_create = await client.post(
        "/api/v1/knowledge/assets",
        json={
            "asset_type": "document",
            "title": "viewer asset",
            "initial_content": {"content_text": "denied"},
        },
    )
    assert denied_create.status_code == 403
    assert denied_create.json()["code"] == 1004

    act_as(identity, username="approver", role="approver")
    denied_approver_write = await client.post(
        "/api/v1/knowledge/assets",
        json={
            "asset_type": "document",
            "title": "approver asset",
            "initial_content": {"content_text": "denied"},
        },
    )
    assert denied_approver_write.status_code == 403
    assert denied_approver_write.json()["code"] == 1004

    unresolved_publish = await client.post(
        f"/api/v1/knowledge/ontology/versions/{ontology_version_id}/publish"
    )
    assert unresolved_publish.status_code == 200
    assert unresolved_publish.json()["code"] == 7007

    for node in extraction["data"]["nodes"]:
        reviewed = await client.post(
            f"/api/v1/knowledge/ontology/nodes/{node['node_id']}/review",
            json={"decision": "approved", "reason": "domain expert verified"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["code"] == 0
    for relation in extraction["data"]["relations"]:
        reviewed = await client.post(
            f"/api/v1/knowledge/ontology/relations/{relation['relation_id']}/review",
            json={"decision": "approved", "reason": "source evidence verified"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["code"] == 0

    act_as(identity, username="operator", role="operator", township_scope="马鼻镇")
    denied_publish = await client.post(
        f"/api/v1/knowledge/ontology/versions/{ontology_version_id}/publish"
    )
    assert denied_publish.status_code == 403
    assert denied_publish.json()["code"] == 1004

    act_as(identity, username="approver", role="approver")
    published = await client.post(
        f"/api/v1/knowledge/ontology/versions/{ontology_version_id}/publish"
    )
    assert published.status_code == 200
    assert published.json()["code"] == 0
    assert published.json()["data"]["version"]["status"] == "published"
    assert published.json()["data"]["approved_nodes"] > 0

    act_as(identity, username="viewer", role="viewer")
    searched = await client.post(
        "/api/v1/knowledge/search",
        json={
            "query": "红树林",
            "ontology_version_id": ontology_version_id,
            "hop_depth": 2,
        },
    )
    assert searched.status_code == 200
    assert searched.json()["code"] == 0
    assert searched.json()["data"]["results"]


@pytest.mark.asyncio
async def test_ontology_version_list_is_browsable_after_restart(
    knowledge_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, _identity = knowledge_api
    first = await client.post(
        "/api/v1/knowledge/ontology/versions",
        json={"name": "marine-domain", "standard_codes": ["HY/T 1234"]},
    )
    second = await client.post(
        "/api/v1/knowledge/ontology/versions",
        json={
            "name": "marine-domain",
            "version_no": 2,
            "parent_version_id": first.json()["data"]["version_id"],
        },
    )
    assert first.json()["code"] == 0
    assert second.json()["code"] == 0

    listed = await client.get("/api/v1/knowledge/ontology/versions")
    body = listed.json()
    assert listed.status_code == 200
    assert body["code"] == 0
    versions = body["data"]
    assert {item["version_id"] for item in versions} == {
        first.json()["data"]["version_id"],
        second.json()["data"]["version_id"],
    }
    assert versions[0]["name"] == "marine-domain"
    assert versions[0]["version_no"] == 2
    assert versions[0]["parent_version_id"] == first.json()["data"]["version_id"]
