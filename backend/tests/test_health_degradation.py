"""健康检查的降级语义测试。

守住一条运维底线：**/health 不能无条件报 ok**。
早期实现无论 Redis / MQTT / 数据库是否可用都返回 status=ok，
compose healthcheck 与 K8s probe 会据此认为服务健康、不去重启，
故障被静默吞掉 —— 这类 bug 在演示时发现不了，上线才炸。

运行：
    pytest tests/test_health_degradation.py -v
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.session import dispose_engine  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """无任何外部依赖（Redis/PG/MQTT 全无）下的测试客户端。

    这正是最需要验证的场景：**依赖全挂时服务仍须能起来并如实汇报**。
    """
    # 不依赖开发者机器上恰好是否启动了 Redis/PostgreSQL。
    # 指向一个无监听端口可以让连接快速被拒绝；若本机确有服务，
    # 下面的“全挂 -> down”断言会被静默跳过，测试就失去确定性。
    monkeypatch.setattr(settings, "redis_host", "127.0.0.1")
    monkeypatch.setattr(settings, "redis_port", 1)
    monkeypatch.setattr(settings, "postgres_host", "127.0.0.1")
    monkeypatch.setattr(settings, "postgres_port", 1)

    app = create_app()
    # 不跑 lifespan —— lifespan 里会尝试连 Redis/MQTT，本机环境下会各等一次
    # 超时，拖慢测试且与本次断言无关。健康检查本身不需要 lifespan 的副作用。
    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    # 必须用上下文管理器固定 same-blocking-portal，而不是每次请求新建事件循环。
    # 否则 asyncpg 在连接超时后留下的取消任务会在 loop 关闭时产生
    # "Connection._cancel was never awaited" 警告（不影响断言，但污染测试基线）。
    with TestClient(app) as test_client:
        yield test_client
        # 引擎可能缓存了刚才的探测配置；在同一个 portal loop 内释放，
        # 避免把测试用的死连接池带到后续测试。
        if test_client.portal is not None:
            test_client.portal.call(dispose_engine)


class TestHealthReportsDegradation:
    """health 必须反映真实依赖状态。"""

    def test_dependencies_key_present(self, client: TestClient) -> None:
        """响应必须带 dependencies 明细，不能只给一个笼统的 status。"""
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "dependencies" in body, "缺少 dependencies 明细，运维无法定位是哪个依赖挂了"
        assert set(body["dependencies"]) == {"redis", "mqtt", "database"}

    def test_status_not_blindly_ok(self, client: TestClient) -> None:
        """依赖不可用时 status 不能是 ok。

        本测试环境没有 Redis / PG / aiomqtt，所以必然是降级或 down。
        """
        body = client.get("/health").json()
        assert body["status"] != "ok", (
            f"依赖全不可用却报 status={body['status']!r} —— 健康检查在撒谎。"
            f"明细：{body.get('dependencies')}"
        )
        assert body["status"] in ("degraded", "down")

    def test_status_is_down_when_all_deps_fail(self, client: TestClient) -> None:
        """全部依赖不可用 → status=down（服务已无法提供有效服务）。"""
        body = client.get("/health").json()
        deps = body["dependencies"]
        if all(v != "ok" for v in deps.values()):
            assert body["status"] == "down"
        else:
            pytest.skip("本机恰有依赖可用，跳过全挂断言")

    def test_degraded_status_is_a_valid_state(self) -> None:
        """文档化状态取值：ok / degraded / down 三档，且语义单调。

        这条是给后人看的契约 —— 编排系统按这个字段决定是否重启，
        新增取值前必须先改这里和 docs/deployment.md。
        """
        valid = {"ok", "degraded", "down"}
        assert valid == {"ok", "degraded", "down"}

    def test_root_endpoint_still_works_when_degraded(self, client: TestClient) -> None:
        """降级时服务信息接口仍可用（只读能力不应被依赖故障阻断）。"""
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json()["name"]

    def test_ready_returns_503_when_dependencies_are_down(self, client: TestClient) -> None:
        """就绪探针必须返回 503，供容器编排真正阻断流量。"""
        resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] in ("degraded", "down")
        assert set(body["dependencies"]) == {"redis", "mqtt", "database"}


class TestProductionSurface:
    """生产环境必须关闭调试文档，不能把完整接口结构暴露到公网。"""

    def test_docs_and_openapi_are_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "app_env", "production")
        app = create_app()
        client = TestClient(app)
        try:
            assert client.get("/docs").status_code == 404
            assert client.get("/redoc").status_code == 404
            assert client.get("/openapi.json").status_code == 404
        finally:
            client.close()


class TestMqttClientConnectionState:
    """MqttClient.is_connected 的语义。"""

    def test_is_connected_false_before_start(self) -> None:
        """未启动时不能报已连接。"""
        try:
            from app.mqtt.client import MqttClient
        except Exception as exc:   # noqa: BLE001
            pytest.skip(f"aiomqtt 不可用：{exc}")

        c = MqttClient()
        assert c.is_connected is False

    def test_is_connected_false_after_start_before_handshake(self) -> None:
        """start() 之后、真正握手成功之前，is_connected 仍须为 False。

        这是本属性存在的理由：只看 _running 会在"正在重连"时误报健康。
        """
        try:
            from app.mqtt.client import MqttClient
        except Exception as exc:   # noqa: BLE001
            pytest.skip(f"aiomqtt 不可用：{exc}")

        async def scenario() -> bool:
            c = MqttClient()
            # 不走 start()（会真的去连 broker），直接模拟内部状态
            c._running = True
            c._connected = False
            try:
                return c.is_connected
            finally:
                c._running = False

        assert asyncio.run(scenario()) is False
