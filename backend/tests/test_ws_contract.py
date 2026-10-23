"""WebSocket 推送契约测试。

★ 为什么需要这一层
──────────────────
WS 推送负载是**手工拼的 dict**，散落在 mqtt/handlers.py、api/v1/tasks.py、
services/consumer.py 等多处。手工拼装必然漂移。

实测踩到的缺陷：同一份 `task_update` 契约（api.md 声明 data 含
`task_id`/`event_id`/`robot_id`/`status` 四个字段）在四个推送点里，
`handlers.handle_robot_progress` 那处**漏了 `event_id`**。

后果的隐蔽程度值得记录：前端 `pushTask` 用 `{ ...data }` 展开，
缺字段静默变成 `undefined`。而当前前端只把 taskFeed 当
「有更新就重拉全量」的触发器，不直接渲染其字段 ——
所以**这个缺陷在界面上完全看不出来**。

真正危险的是将来：有人一旦开始直接渲染 taskFeed 里的字段，
就会拿到 undefined 而不知道为什么。契约是四个字段就得给四个。

本文件用 **AST 静态扫描**找出所有推送调用点，逐个校验负载字段
—— 不是抽测某几处，而是穷举全部。新增推送点时会被自动纳入。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.api.v1.auth import _issue_token  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.main import create_app  # noqa: E402

from app.ws.manager import (
    WS_MESSAGE_CONTRACT,
    WS_OPTIONAL_FIELDS,
    validate_payload,
)

# 推送方法名 → WS 消息类型
PUSH_METHOD_TO_TYPE = {
    "push_new_event": "new_event",
    "push_task_update": "task_update",
    "push_robot_status": "robot_status",
}

APP_DIR = "backend/app"


# ======================================================================
# 一、契约常量本身的完整性
# ======================================================================
class TestContractDeclaration:
    """契约常量必须覆盖文档声明的全部消息类型。"""

    def test_covers_all_documented_types(self) -> None:
        """api.md §服务端推送消息表声明 6 种 type，契约常量必须全覆盖。"""
        documented = {
            "connected",
            "new_event",
            "task_update",
            "robot_status",
            "pong",
            "heartbeat",
        }
        missing = documented - set(WS_MESSAGE_CONTRACT)
        assert not missing, (
            f"契约常量缺少文档声明的消息类型 {sorted(missing)} —— "
            "前端会收到它但契约里查不到，字段校验形同虚设"
        )

    def test_no_undeclared_types(self) -> None:
        """契约里不能有文档没声明的类型（否则是实现方的臆造）。"""
        documented = {
            "connected",
            "new_event",
            "task_update",
            "robot_status",
            "pong",
            "heartbeat",
        }
        extra = set(WS_MESSAGE_CONTRACT) - documented
        assert not extra, f"契约里有文档未声明的消息类型：{sorted(extra)}"

    @pytest.mark.parametrize(
        ("mtype", "expected_min_fields"),
        [
            ("new_event", 8),      # api.md 列出 8 个字段
            ("task_update", 4),    # api.md 列出 4 个字段
            ("robot_status", 9),   # api.md 列出 9 个字段
        ],
    )
    def test_field_counts_match_doc(self, mtype: str, expected_min_fields: int) -> None:
        """★ 字段数量守卫：文档列了几个就得声明几个。

        这条防的是「文档写 4 个字段，契约常量只写 3 个」——
        那样字段校验会漏掉真正缺失的那一个。
        """
        got = len(WS_MESSAGE_CONTRACT[mtype])
        assert got >= expected_min_fields, (
            f"{mtype} 契约只声明了 {got} 个字段，文档要求至少 "
            f"{expected_min_fields} 个：{WS_MESSAGE_CONTRACT[mtype]}"
        )

    def test_optional_fields_not_in_required(self) -> None:
        """可选字段与必带字段不能重叠，否则语义混乱。"""
        required_all: set[str] = set()
        for fields in WS_MESSAGE_CONTRACT.values():
            required_all |= set(fields)
        overlap = required_all & WS_OPTIONAL_FIELDS
        assert not overlap, f"这些字段同时被标为必带与可选：{sorted(overlap)}"


# ======================================================================
# 二、运行期校验函数的行为
# ======================================================================
class TestValidatePayload:
    """validate_payload 只报「该有的没有」，不报「多余的可不可以」。"""

    def test_complete_task_update_passes(self) -> None:
        ok = {
            "task_id": "tsk_1",
            "event_id": "evt_1",
            "robot_id": "RBT-001",
            "status": "navigating",
        }
        assert validate_payload("task_update", ok) == []

    def test_missing_event_id_is_reported(self) -> None:
        """★ 复现历史缺陷：漏 event_id 必须被报出来。"""
        bad = {
            "task_id": "tsk_1",
            "robot_id": "RBT-001",
            "status": "done",
            "finished_at": "2026-09-18T01:53:45+08:00",
        }
        problems = validate_payload("task_update", bad)
        assert any("event_id" in p for p in problems), (
            f"漏 event_id 却未被报出，校验失效：{problems}"
        )

    def test_extra_fields_allowed(self) -> None:
        """附带额外字段（如 finished_at）是允许的，不应报错。"""
        with_extra = {
            "task_id": "tsk_1",
            "event_id": "evt_1",
            "robot_id": "RBT-001",
            "status": "done",
            "finished_at": "2026-09-18T01:53:45+08:00",
            "collected_weight": 12.5,
        }
        assert validate_payload("task_update", with_extra) == []

    def test_none_value_still_counts_as_present(self) -> None:
        """★ 键存在但值为 None 算「已携带」。

        理由：契约关注的是**字段形状**，不是取值。
        设备可能确实没有坐标（lng=None），这不该算契约违规。
        """
        with_none = {
            "robot_id": "RBT-001",
            "battery": None,
            "status": "idle",
            "lng": None,
            "lat": None,
            "task_id": None,
            "bins": {},
            "heading": None,
            "speed": None,
        }
        assert validate_payload("robot_status", with_none) == []

    def test_unknown_type_is_reported(self) -> None:
        problems = validate_payload("not_a_real_type", {"x": 1})
        assert problems, "未声明的消息类型必须被报出"

    def test_empty_data_for_no_payload_types(self) -> None:
        """pong / heartbeat 无 data，空 dict 应合格。"""
        assert validate_payload("pong", {}) == []
        assert validate_payload("heartbeat", {}) == []


# ======================================================================
# 三、★ 穷举：静态扫描所有推送调用点
# ======================================================================
class TestAllPushCallSites:
    """用 AST 找出代码里每一处推送调用，逐个校验负载字段。

    这是本文件的核心 —— 不是抽测，是穷举。
    新增推送点时会被自动纳入检查，不需要有人记得来补测试。
    """

    def _iter_push_calls(self, project_root: Path):
        """产出 (文件相对路径, 行号, 消息类型, 负载节点)。"""
        app_dir = project_root / APP_DIR
        for py in sorted(app_dir.rglob("*.py")):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not isinstance(fn, ast.Attribute):
                    continue
                mtype = PUSH_METHOD_TO_TYPE.get(fn.attr)
                if mtype is None:
                    continue
                args = node.args
                payload = args[0] if args else None
                yield (
                    py.relative_to(project_root).as_posix(),
                    node.lineno,
                    mtype,
                    payload,
                )

    def test_found_expected_number_of_push_calls(self, project_root: Path) -> None:
        """★ 自证：确认扫描器真的找到了推送点。

        如果扫描器因为 AST 写法变化而一个都找不到，
        下面那条穷举校验就会「零问题通过」—— 那是最坏的情况。
        所以先断言扫描结果非空。
        """
        calls = list(self._iter_push_calls(project_root))
        assert calls, (
            "AST 扫描没找到任何推送调用点 —— 扫描器已失效，"
            "后续校验是空转。请检查 PUSH_METHOD_TO_TYPE 与调用写法。"
        )
        # 当前有 4 处推送（3×task_update + 1×new_event + 1×robot_status）
        assert len(calls) >= 4, (
            f"只找到 {len(calls)} 处推送调用，少于预期的 4 处 —— 扫描器可能漏了"
        )

    def test_every_dict_payload_has_required_fields(self, project_root: Path) -> None:
        """★ 核心断言：每一处推送的字面量 dict 都必须含契约要求的字段。"""
        offenders: list[str] = []
        checked = 0

        for relpath, lineno, mtype, payload in self._iter_push_calls(project_root):
            if not isinstance(payload, ast.Dict):
                # 非字面量（如 out.model_dump()）无法静态分析，
                # 由 test_model_dump_payloads_are_complete 覆盖
                continue
            checked += 1

            keys = {
                k.value for k in payload.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            required = set(WS_MESSAGE_CONTRACT[mtype])
            missing = required - keys
            if missing:
                offenders.append(
                    f"{relpath}:{lineno} 的 {mtype} 负载缺少 {sorted(missing)}"
                )

        assert checked > 0, "没有可静态分析的 dict 负载 —— 扫描器已失效"
        assert not offenders, (
            "推送负载字段不完整（前端会静默拿到 undefined）：\n  "
            + "\n  ".join(offenders)
        )

    def test_no_undeclared_push_helpers(self, project_root: Path) -> None:
        """manager 里不能有契约未声明的 push_* 方法（防止绕过校验新增类型）。"""
        src = (project_root / APP_DIR / "ws" / "manager.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("push_"):
                assert node.name in PUSH_METHOD_TO_TYPE, (
                    f"manager.py 新增了未在契约里声明的推送方法 {node.name}() —— "
                    "请先在 WS_MESSAGE_CONTRACT 里声明它的字段，并加入本文件的映射表"
                )

    def test_model_dump_payloads_reference_contract_fields(self, project_root: Path) -> None:
        """走 model_dump() 的推送，其模型字段必须覆盖契约要求。

        tasks.py 推的是完整 TaskOut.model_dump()，字段比契约要求的多，
        这没问题；但必须**包含**契约要求的全部字段。
        """
        from app.schemas import TaskOut

        model_fields = set(TaskOut.model_fields)
        required = set(WS_MESSAGE_CONTRACT["task_update"])
        missing = required - model_fields
        assert not missing, (
            f"TaskOut 缺少 task_update 契约要求的字段 {sorted(missing)} —— "
            "tasks.py 推的是 TaskOut.model_dump()，少了它前端就拿到 undefined"
        )


# ======================================================================
# 四、前端消费端对齐
# ======================================================================
class TestFrontendConsumesKnownTypes:
    """前端处理的消息类型必须都在契约里，反之亦然。

    防的是「后端推了一种 type，前端 if-else 链里没有分支」
    —— 前端不会报错，只是那条消息被无声丢弃。
    """

    def _frontend_handled_types(self, project_root: Path) -> set[str]:
        import re

        js = (project_root / "frontend" / "src" / "stores" / "realtime.js").read_text(
            encoding="utf-8"
        )
        return set(re.findall(r"msg\.type\s*===\s*'([a-z_]+)'", js))

    def test_frontend_handles_all_pushed_types(self, project_root: Path) -> None:
        """后端会主动推的 3 种类型，前端必须有对应分支。"""
        fe = self._frontend_handled_types(project_root)
        assert fe, "没能从前端解析出任何消息类型 —— 正则失效，本检查已空转"

        pushed = {"new_event", "task_update", "robot_status"}
        missing = pushed - fe
        assert not missing, (
            f"后端会推送但这些类型前端没有处理分支：{sorted(missing)}"
            "（消息会被无声丢弃，界面不更新也不报错）"
        )

    def test_frontend_types_all_declared(self, project_root: Path) -> None:
        """前端处理的类型都必须在后端契约里有声明。"""
        fe = self._frontend_handled_types(project_root)
        undeclared = fe - set(WS_MESSAGE_CONTRACT)
        assert not undeclared, (
            f"前端在处理后端契约未声明的类型：{sorted(undeclared)}"
        )


class TestProductionWebSocketAuth:
    """Production WebSocket requires a signed token; development stays permissive."""

    def test_forged_token_is_rejected_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "app_env", "production")
        client = TestClient(create_app())
        try:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(
                    "/api/v1/ws/alerts?token=forged-token"
                ):
                    pass
            assert exc_info.value.code == 4401
        finally:
            client.close()

    def test_valid_token_is_accepted_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "app_env", "production")
        token = _issue_token("viewer", "viewer", None)
        client = TestClient(create_app())
        try:
            with client.websocket_connect(
                f"/api/v1/ws/alerts?token={token}"
            ) as websocket:
                welcome = websocket.receive_json()
                assert welcome["type"] == "connected"
        finally:
            client.close()
