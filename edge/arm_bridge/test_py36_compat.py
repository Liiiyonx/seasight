"""Python 3.6 兼容性守护测试。

为什么需要这个测试
----------------
本项目要在**开发机（3.13）**和**树莓派（3.6.9）**上跑同一份代码。
树莓派是 Python 3.6，装不了新版本、也不能出网装 backport
（2026-10-05 实测：无默认路由、dataclasses MISSING、typing.Protocol 不存在），
所以只能用兼容层。

**这个测试的意义**：防止将来有人顺手写回 `dict[str, float]` 或
`from __future__ import annotations` —— 在开发机上完全正常，
一部署到树莓派就 SyntaxError，而那时可能已经过了好几天。
让这类错误在**本机测试阶段**就暴露。

与 ``scripts/migrate_arm_bridge_to_py36.py`` 的关系
--------------------------------------------------
迁移脚本负责"改"，本测试负责"防回归"。两者都要有：
只有迁移脚本的话，下次写新代码又会破；只有测试的话，
存量代码里的问题又不会被清理。
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EDGE = ROOT / "edge"

# ★ 必须覆盖**会上传到树莓派**的全部文件（与 deploy_bridge_to_pi.py
#   的 INCLUDE_FILES 保持一致）。漏一个，那个文件就可能悄悄破掉。
DEPLOYED_FILES = (
    "arm_bridge/__init__.py",
    "arm_bridge/py36_compat.py",
    "arm_bridge/bridge.py",
    "arm_bridge/drivers.py",
    "arm_bridge/mqtt_transport.py",
    "arm_bridge/ros_driver.py",
    "arm_bridge/tools/__init__.py",
    "arm_bridge/tools/teach_hiwonder_sequence.py",
    "device_sim/__init__.py",
    "device_sim/py36_compat.py",
    "device_sim/protocol.py",
    "device_sim/transport.py",
    "device_sim/device.py",
    "device_sim/faults.py",
)

#: 3.6 没有的内建泛型下标写法（``dict[str, float]`` 这类）
BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}


def _annotation_node_ids(tree):
    """收集所有**注解**子树里的节点 id。

    只有落在注解里的 ``X | Y`` 才是 3.6 的问题；
    运行时代码里的集合并集（``a | b``）完全合法，不能误报。
    """
    ids = set()
    for node in ast.walk(tree):
        roots = []
        if isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        elif isinstance(node, ast.arg):
            roots.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            roots.append(node.returns)
        for root in roots:
            if root is None:
                continue
            for sub in ast.walk(root):
                ids.add(id(sub))
    return ids


class TestPy36Compatibility(unittest.TestCase):
    """树莓派 Python 3.6.9 兼容性守护。"""

    def test_deployed_files_exist(self):
        """上传清单里的每个文件都必须真实存在。

        少一个 → 部署到树莓派时 ModuleNotFoundError（实测踩过）。
        """
        missing = [
            rel for rel in DEPLOYED_FILES if not (EDGE / rel).is_file()
        ]
        self.assertEqual(
            missing, [], "上传清单里的文件不存在：%s" % ", ".join(missing)
        )

    def test_no_future_annotations_import(self):
        """``from __future__ import annotations`` 是 3.7+ 语法，3.6 直接 SyntaxError。"""
        offenders = []
        for rel in DEPLOYED_FILES:
            path = EDGE / rel
            if not path.is_file():
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    offenders.append("%s:%s" % (rel, node.lineno))
        self.assertEqual(
            offenders, [],
            "以下文件用了 __future__ 导入（3.6 不支持）：%s"
            % ", ".join(offenders),
        )

    def test_no_stdlib_dataclasses(self):
        """``dataclasses`` 是 3.7+ 内置，树莓派没有且装不了。"""
        offenders = []
        for rel in DEPLOYED_FILES:
            path = EDGE / rel
            if not path.is_file():
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    mod = getattr(node, "module", None) or ""
                    if "dataclasses" in mod:
                        offenders.append("%s:%s" % (rel, node.lineno))
        self.assertEqual(
            offenders, [],
            "以下文件 import 了 dataclasses：%s" % ", ".join(offenders),
        )

    def test_no_typing_protocol(self):
        """``typing.Protocol`` 是 3.8+，树莓派实测 hasattr 为 False。"""
        offenders = []
        for rel in DEPLOYED_FILES:
            path = EDGE / rel
            if not path.is_file():
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    mod = getattr(node, "module", None) or ""
                    names = [a.name for a in node.names]
                    if "Protocol" in names and mod == "typing":
                        offenders.append("%s:%s" % (rel, node.lineno))
        self.assertEqual(
            offenders, [],
            "以下文件 from typing import Protocol：%s" % ", ".join(offenders),
        )

    def test_no_builtin_generic_subscript(self):
        """``dict[str, float]`` 在 3.6 定义时即求值失败，须用 ``Dict[str, float]``。"""
        offenders = []
        for rel in DEPLOYED_FILES:
            path = EDGE / rel
            if not path.is_file():
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in BUILTIN_GENERICS):
                    offenders.append(
                        "%s:%s (%s[...])" % (rel, node.lineno, node.value.id)
                    )
        self.assertEqual(
            offenders, [],
            "以下文件用了内建泛型下标：%s" % ", ".join(offenders),
        )

    def test_no_union_in_annotations(self):
        """``X | Y`` 在注解里 3.6 会 TypeError，须用 ``Optional`` / ``Union``。"""
        offenders = []
        for rel in DEPLOYED_FILES:
            path = EDGE / rel
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            ann_ids = _annotation_node_ids(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.BinOp)
                        and isinstance(node.op, ast.BitOr)
                        and id(node) in ann_ids):
                    offenders.append("%s:%s" % (rel, node.lineno))
        self.assertEqual(
            offenders, [],
            "以下文件在注解里用了 | 联合类型：%s" % ", ".join(offenders),
        )


class TestPy36CompatLayer(unittest.TestCase):
    """兼容层自身的行为。"""

    def _layer(self):
        sys.path.insert(0, str(EDGE))
        sys.path.insert(0, str(EDGE / "arm_bridge"))
        from arm_bridge import py36_compat

        return py36_compat

    def test_dataclass_defaults_and_factory(self):
        """默认工厂必须每次构造都独立调用（否则多个实例共享同一个 dict）。"""
        mod = self._layer()
        Shape = mod.make_dataclass(
            "Shape",
            [
                ("name", "x"),
                ("tags", mod.field(default_factory=lambda: {"a": 1})),
            ],
        )
        a, b = Shape(), Shape()
        a.tags["k"] = 9
        self.assertEqual(b.tags, {"a": 1}, "default_factory 被共享了")
        self.assertEqual(a.name, "x")

    def test_dataclass_preserves_methods(self):
        """原地增强式 dataclass 必须保留类里已定义的方法。"""
        mod = self._layer()

        @mod.dataclass
        class Point:
            x: int = 0
            y: int = 0

            def norm2(self):
                return self.x * self.x + self.y * self.y

        self.assertEqual(Point(3, 4).norm2(), 25)

    def test_frozen_blocks_assignment(self):
        """frozen 类不能改字段。"""
        mod = self._layer()

        @mod.dataclass(frozen=True)
        class Frozen:
            v: int = 0

        with self.assertRaises(AttributeError):
            Frozen().v = 1

    def test_hashes_with_dict_field(self):
        """含 dict 字段的对象仍要可哈希（namedtuple 默认会失败）。"""
        mod = self._layer()
        WithDict = mod.make_dataclass(
            "WithDict",
            [("a", 1), ("d", mod.field(default_factory=dict))],
        )
        obj = WithDict()
        self.assertIsInstance(hash(obj), int)
        self.assertEqual(obj, WithDict())

    def test_rejects_unknown_kwarg(self):
        """拼错关键字要报错，不能静默吞掉。"""
        mod = self._layer()
        S = mod.make_dataclass("S", [("a", 1)])
        with self.assertRaises(TypeError):
            S(bogus=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMqttWebSocketTransport(unittest.TestCase):
    """MQTT-over-WebSocket 路演通道的守护。

    为什么需要这个守护
    ----------------
    云端 EMQX 的 1883 只绑 127.0.0.1（正确的安全设计），
    路演现场设备只能走 **WSS/443**。这条通道的配置有四个极易踩的点
    （2026-10-05 全部实测踩过），任何一处写错都表现为「连不上」
    而不报错，因此必须有测试锁住。

    四个坑：
      1. 传输名是 ``"websockets"``，不是 ``"ws"``
         （paho 2.x: ValueError transport must be "websockets", "tcp" or "unix"）
      2. 传给 ``Client(transport=...)``，**不是** ``connect(transport=...)``
         （connect() 不接受该参数）
      3. EMQX 5.8 要求子协议 ``mqtt``（``Sec-WebSocket-Protocol``），
         缺了返回 **HTTP 400**（路径对也拒绝）
      4. 树莓派（paho 1.6.1）需要外部 ``websocket-client``，
         三个 WS 库全缺时要离线装
    """

    def _transport(self):
        import sys
        edge = ROOT / "edge"
        for p in (str(edge), str(edge / "arm_bridge")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from arm_bridge import mqtt_transport
        return mqtt_transport

    def test_transport_name_is_websockets(self):
        """必须用 'websockets'；写成 'ws' 会被 paho 直接拒。"""
        mod = self._transport()
        import inspect
        src = inspect.getsource(mod.MqttTransport.connect)
        self.assertIn('"websockets"', src,
                      "WebSocket 传输名必须是 'websockets'（paho 的合法值）")
        # 不应出现 transport='ws' 这种错误值
        self.assertNotIn('transport="ws"', src)
        self.assertNotIn("transport='ws'", src)

    def test_transport_goes_to_client_not_connect(self):
        """transport 必须传给 Client()，connect() 不接受该参数。"""
        mod = self._transport()
        import inspect
        src = inspect.getsource(mod.MqttTransport.connect)
        # transport 应出现在 client_kwargs 构造里
        self.assertIn('client_kwargs', src)
        # connect(...) 调用里不能带 transport=
        connect_call = src.split("client.connect(")[-1] if "client.connect(" in src else ""
        self.assertNotIn("transport=", connect_call,
                         "connect() 不接受 transport= 参数（paho 2.x 会 TypeError）")

    def test_ws_opts_sends_subprotocol(self):
        """EMQX 5.8 要求 Sec-WebSocket-Protocol: mqtt，否则 HTTP 400。"""
        mod = self._transport()
        opts = mod.MqttTransport._ws_opts()
        self.assertIn("headers", opts)
        self.assertEqual(
            opts["headers"].get("Sec-WebSocket-Protocol"), "mqtt",
            "EMQX 5.8 的 WS 监听校验子协议，缺了返回 HTTP 400")

    def test_default_path_is_mqtt(self):
        """默认路径 /mqtt，需与 nginx 的 location 保持一致。"""
        mod = self._transport()
        t = mod.MqttTransport()
        self.assertEqual(t.path, "/mqtt")
        self.assertEqual(t.transport, "tcp", "默认应是 tcp，显式指定才用 ws")

    def test_ws_mode_sets_options(self):
        """transport='ws' 时必须调用 ws_set_options。"""
        mod = self._transport()
        import inspect
        src = inspect.getsource(mod.MqttTransport.connect)
        self.assertIn("ws_set_options", src)
