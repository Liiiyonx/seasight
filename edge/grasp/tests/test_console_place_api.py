"""作业台（:8090）放置点接口 + 「开始作业」真跑到入槽的接口级自测（mock）。

为什么还要测一层 HTTP：放置点这一步的现场操作路径是**浏览器**——操作员在
「手动点动」把臂摆到槽口上方，再点「用当前姿态作放置点」。如果只测闭环内部，
接口名字对不上、参数名写错、页面按钮指向别的端点，这些都不会被发现。

所以这里锁三件事：

1. 放置点三种用法（coord / pose="here" / clear）的接口行为；
2. **没设放置点时 `/api/grasp/start` 必须 400**（提前拦住，而不是跑到一半失败）；
3. 设好放置点后从接口发起一次作业，最终 `job.result.placed` 必须为 True。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
GRASP = HERE.parent
EDGE = GRASP.parent
for _p in (str(EDGE), str(GRASP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grasp.config import load_config            # noqa: E402
from grasp.web.server import create_app         # noqa: E402


@pytest.fixture()
def client(monkeypatch, tmp_path) -> TestClient:
    # ★ 隔离标定文件路径。默认路径是 edge/grasp/calibration.json（.gitignore 的本地文件）：
    #   开发者本机残留一份真标定，会让作业台把它判成「真机标定」而不是演示标定，
    #   本文件的演示/不落盘断言就会**误失败**；反过来，这里调 /api/calibrate/plane_z
    #   也会**写坏开发者本机那份标定**（实测踩到）。指到临时目录后两边都干净。
    from grasp import calibration as calib_mod
    monkeypatch.setattr(calib_mod, "DEFAULT_CALIBRATION_PATH",
                        tmp_path / "calibration.json")
    # 用仓库里**真实的那份** config.yaml：顺带校验 place 段落能被解析
    cfg = load_config()
    assert "place" in (cfg.get("agent") or {}), "config.yaml 缺 agent.place 段落"
    # 只把节拍调快（真机是 250ms/3 帧），其余一律用仓库真值
    cfg["agent"]["frame_period_ms"] = 1
    cfg["agent"]["lock_min_hits"] = 1
    cfg["agent"]["place"]["settle_s"] = 0.0
    app = create_app(cfg, mock=True)
    with TestClient(app) as c:
        yield c


def _wait_job_done(client: TestClient, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + timeout_s
    job: dict = {}
    while time.time() < deadline:
        job = client.get("/api/status").json()["job"]
        if not job.get("running"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"作业超时未结束：{job}")


def test_status_exposes_demo_place_in_mock(client: TestClient) -> None:
    place = client.get("/api/status").json()["place"]
    assert place["ready"] is True
    assert place["kind"] == "coord"
    assert place["source"] == "demo"          # mock 用的是演示值，界面要能看出来


def test_place_endpoint_coord_pose_clear(client: TestClient) -> None:
    r = client.post("/api/place", json={"coord": [0.16, 0.02]}).json()
    assert r["ok"] is True and r["persisted"] is False      # 演示标定不落盘
    assert r["place"]["kind"] == "coord"
    assert r["place"]["target"] == [0.16, 0.02]
    assert r["place"]["source"] == "console"

    r = client.post("/api/place", json={"pose": "here"}).json()
    assert r["place"]["kind"] == "pose"
    # 夹爪 + 腕部 + 4 个 IK 关节 = 6 个舵机
    assert len(r["place"]["target"]) == 6

    r = client.post("/api/place", json={"clear": True}).json()
    assert r["place"]["ready"] is False
    assert r["place"]["kind"] == "none"


def test_grasp_start_is_refused_without_place_point(client: TestClient) -> None:
    """连 config 兜底都没有时（真机上没示教）→ 直接 400，不让它跑起来。"""
    from grasp.web import server as srv

    client.post("/api/place", json={"clear": True})
    # 清掉示教值后，config.yaml 里的 place.coord 仍是兜底；这里把兜底也去掉，
    # 模拟「真机上既没示教、配置也没填」这台机器。
    srv.STATE.loop.config_place = None
    resp = client.post("/api/grasp/start", json={"task_id": "tsk_no_place"})
    assert resp.status_code == 400
    assert "料槽放置点" in resp.json()["detail"]


def test_config_place_coord_is_fallback_before_teaching(client: TestClient) -> None:
    """没示教时，config.yaml 的 place.coord 就是可用兜底（不必先跑示教）。"""
    from grasp.web import server as srv

    srv.STATE.cal.place = None
    info = client.get("/api/status").json()["place"]
    assert info["ready"] is True
    assert info["source"] == "config"
    assert info["target"] == pytest.approx([0.275, 0.0])       # 厂商货架同口径


def test_plane_z_endpoint_sets_water_height(client: TestClient) -> None:
    """水面高度单独设置：船上甲板高于水面 → 负值。"""
    r = client.post("/api/calibrate/plane_z", json={"plane_z": -0.08}).json()
    assert r["ok"] is True and r["plane_z"] == pytest.approx(-0.08)
    assert r["persisted"] is False            # 演示标定不落盘
    assert client.get("/api/status").json()["calibration"]["plane_z"] == pytest.approx(-0.08)
    # 越界值必须被挡住（-0.5..0.5）
    assert client.post("/api/calibrate/plane_z", json={"plane_z": -3.0}).status_code == 422


def test_console_grasp_job_ends_with_placed_true(client: TestClient) -> None:
    client.post("/api/place", json={"coord": [0.16, 0.02]})
    assert client.post("/api/grasp/start", json={"task_id": "tsk_console_1"}).status_code == 200

    job = _wait_job_done(client)
    assert job["stage"] == "done", job
    assert job["result"]["ok"] is True
    assert job["result"]["placed"] is True, job
    assert "放入料槽" in job["result"]["message"]
    assert any("放入料槽" in line for line in job["log"])


def test_page_has_place_card_wired_to_api(client: TestClient) -> None:
    html = client.get("/").text
    assert "放置点（料槽）" in html
    assert "/api/place" in html
    assert "用当前姿态作放置点" in html
