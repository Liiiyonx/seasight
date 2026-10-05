"""Python 3.6 compatibility shims.

为什么需要这个模块
------------------
本项目要在两类机器上跑同一份代码：

* 开发机 / CI：Python 3.13（语法随便用）
* 树莓派（ArmPi FPV 出厂镜像）：**Python 3.6.9**，且**没有 3.7+ 可选**

2026-10-05 实测树莓派环境：

======================================  ==================
系统 python3                            3.6.9（唯一）
``dataclasses``                         MISSING（3.7+ 内置）
``typing.Protocol``                     不存在（3.8+）
``from __future__ import annotations``   语法层，需 3.7+
PyYAML                                  5.3.1 OK
======================================  ==================

melodic 的 rospy 装在 ``/opt/ros/melodic/lib/python2.7/dist-packages``
—— 那只是 ROS 的历史遗留路径，不代表要用 Python 2 跑我们的代码；
``source /opt/ros/melodic/setup.bash`` 之后 python3 就能 import 到 rospy。

做法：**新代码用 3.6 安全语法，需要 3.7+ 特性的地方从这里取。**
两端跑同一份文件，不需要维护分支。

三个差异
--------
1. ``from __future__ import annotations`` —— 3.6 不支持，别用。
2. ``dataclasses`` —— 3.6 需 backport，但树莓派**不能出网**装不了。
   :func:`make_dataclass` 用 ``collections.namedtuple``（3.6 自带）等价实现。
3. ``typing.Protocol`` —— 3.6 没有。退化为 ``abc.ABC``。

实现方式
--------
不用 ``__slots__``（会和类变量默认值冲突），改用 namedtuple 子类 +
显式 ``__new__`` 填默认值。这样 frozen 语义、相等性、哈希都由
namedtuple 免费提供，行为与标准 dataclass 一致。

类型注解里的下标泛型（``dict[str, float]``）在 3.6 定义时就会求值失败，
所以本项目统一用 ``Dict[...]``/``Optional[...]``，见 :data:`DictStrFloat`。
"""

import abc
import sys
from collections import namedtuple
from typing import Any, Callable, Dict, Optional

PY36 = sys.version_info[:2] == (3, 6)

# ★ 树莓派 Python 3.6 的运行时兼容坑（2026-10-05 一天内连踩三个，
#   都属于「语法能过、在 3.13 上完全正常、在树莓派上直接 TypeError」）：
#
#   1. ``subprocess.run(..., capture_output=True)`` —— 3.7+ 才有。
#      3.6 必须 ``Popen(...)`` + ``communicate()``。
#   2. ``subprocess.run(..., input=...)`` —— 同样 3.7+ 才有。
#   3. ``from __future__ import annotations`` —— 3.7+ 语法，直接 SyntaxError。
#   4. ``dataclasses`` —— 3.7+ 内置，树莓派没有且装不了 backport。
#   5. ``typing.Protocol`` —— 3.8+ 才有。
#   6. 下标泛型 ``dict[str, X]`` —— 3.6 定义时求值即失败，须用 ``Dict[str, X]``。
#
# → 写任何要上传到树莓派的代码前，先看这一段。
#   test_py36_compat.py 会对上传清单里的文件做 AST 静态检查，
#   但**运行期**的 3.7+ API 调用（如上面的 subprocess）静态查不出来，
#   只能靠实机跑。故涉及树莓派的任务，完成后务必实跑验证。

# 下标泛型在 3.6 不能用于注解求值，统一用这些别名。
DictStrFloat = Dict[str, float]
DictStrAny = Dict[str, Any]
OptStr = Optional[str]
OptInt = Optional[int]


class Protocol(abc.ABC):  # noqa: N801
    """``typing.Protocol`` 的 3.6 退化实现。

    本项目只用它表达"ArmDriver 应有哪些方法"这类**结构化类型**，
    不做运行期 isinstance 检查，因此退化成抽象基类语义足够。
    """

    @classmethod
    def __subclasshook__(cls, other: Any) -> Any:
        if cls is Protocol:
            return NotImplemented
        return NotImplemented


def field(default: Any = None, default_factory: Optional[Callable] = None) -> Any:
    """``dataclasses.field`` 的 3.6 等价物。

    返回值是**哨兵对象**：:func:`make_dataclass` 识别它并决定
    "用 ``default``" 还是"每次构造调用 ``default_factory()``"。

    裸值（含 ``None``）直接传给 :func:`make_dataclass` 时视为
    "该字段的默认值"，而不是"必填"—— 与标准 dataclass 的行为一致。
    需要真正的必填字段请不给出该项。
    """
    if default_factory is not None:
        return _Factory(default_factory)
    return _Default(default)


class _Default(object):
    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "field(default=%r)" % (self.value,)


class _Factory(object):
    __slots__ = ("factory",)

    def __init__(self, factory: Callable) -> None:
        self.factory = factory

    def __repr__(self) -> str:
        return "field(default_factory=%r)" % (self.factory,)


def make_dataclass(name: str, fields: list, doc: Optional[str] = None,
                   namespace: Optional[Dict[str, Any]] = None) -> type:
    """``@dataclass(frozen=True)`` 的 3.6 等价实现。

    相比标准 dataclass 的**限制**（本项目够用）：
    * 字段必须按顺序显式列出（3.6 的 ``__annotations__`` 不保序）
    * 不支持可变 dataclass（本项目全是 frozen，安全）
    * 不支持 ``InitVar`` / ``ClassVar`` / ``__post_init__``

    参数
    ----
    name    类名
    fields  ``(字段名, 默认值)`` 列表；默认值可以是 :func:`field` 的返回值，
            也可以是裸值（None 表示必填）
    doc     文档字符串
    namespace 额外类属性（如方法）
    """
    if not fields:
        raise ValueError("make_dataclass 至少要一个字段")

    field_names = []
    defaults = []
    for fname, default in fields:
        if fname in field_names:
            raise ValueError("重复字段: %s" % fname)
        field_names.append(fname)
        defaults.append(default)

    base = namedtuple("%sBase" % name, field_names)

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        if len(args) > len(field_names):
            raise TypeError(
                "%s() takes at most %d arguments (%d given)"
                % (name, len(field_names), len(args))
            )
        values = list(args)
        if kwargs:
            known = set(field_names)
            for key in kwargs:
                if key not in known:
                    raise TypeError(
                        "%s() got an unexpected keyword argument %r"
                        % (name, key)
                    )
            provided = set(kwargs)
            for i, fname in enumerate(field_names):
                if i < len(values):
                    if fname in provided:
                        raise TypeError(
                            "%s() got multiple values for argument %r"
                            % (name, fname)
                        )
                    continue
                if fname in provided:
                    values.append(kwargs[fname])
                else:
                    values.append(_resolve(defaults[i]))
        # 用默认值补齐尾部
        while len(values) < len(field_names):
            values.append(_resolve(defaults[len(values)]))

        obj = base.__new__(cls, *values)
        return obj

    ns = {"__new__": __new__, "__slots__": ()}
    if doc:
        ns["__doc__"] = doc
    if namespace:
        ns.update(namespace)
    made = type(name, (base,), ns)

    # namedtuple 的 __eq__ 会比较所有字段；本项目的 ArmStatus/PickResult
    # 含 dict 字段，直接继承会变成 unhashable，且相等语义也不该被 dict
    # 的同一性干扰。定义一个基于"可比较字段"的 __eq__/__hash__：
    # 只比较标量字段，dict 字段用其内容做稳定摘要。
    def _hashable(value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(sorted(
                (k, _hashable(v)) for k, v in value.items()
            ))
        if isinstance(value, (list, tuple)):
            return tuple(_hashable(v) for v in value)
        return value

    def __eq__(self: Any, other: Any) -> Any:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return all(
            _hashable(getattr(self, f, None)) == _hashable(getattr(other, f, None))
            for f in field_names
        )

    def __hash__(self: Any) -> int:
        return hash(tuple(
            _hashable(getattr(self, f, None)) for f in field_names
        ))

    made.__eq__ = __eq__
    made.__hash__ = __hash__
    # 显式声明可变性（namedtuple 本身不可变，slots 已保证）
    return made


def _resolve(spec: Any) -> Any:
    """把字段默认值哨兵解析成实际默认值。"""
    if isinstance(spec, _Factory):
        return spec.factory()
    if isinstance(spec, _Default):
        return spec.value
    return spec


def dataclass(cls: Any = None, frozen: bool = True, **kwargs: Any) -> Any:
    """``@dataclass`` 的 3.6 等价实现。

    ★ 与标准库的关键差异：**原地增强类，不生成新类**。
      这样类里已定义的方法（``to_dict`` 等）、docstring、类属性全部保留。
      标准 dataclass 也是原地增强，但它的字段顺序来自 ``__annotations__``；
      3.6 下 ``__annotations__`` 是普通 dict、**不保证插入序**，
      所以本实现改用**类体赋值顺序**（``vars(cls)`` 保序，3.6 CPython 实测可靠）。

    处理规则
    --------
    * 名字出现在 ``__annotations__`` 或类体赋值里的 → 字段
    * 名字以下划线开头、或可调用（方法/classmethod）→ 保留不动
    * 值是 :func:`field` 返回的哨兵 → 构造时用其默认/工厂
    * ``frozen=True`` 时阻止赋值（抛 AttributeError）

    不支持 ``InitVar`` / ``ClassVar`` / ``__post_init__`` 自动调用链。
    """
    if cls is None:
        return lambda c: dataclass(c, frozen=frozen, **kwargs)

    order: list = []
    defaults: Dict[str, Any] = {}
    factories: Dict[str, Any] = {}

    for key, value in list(vars(cls).items()):
        if key.startswith("__"):
            continue
        if callable(value) or isinstance(value, (property, classmethod,
                                                   staticmethod)):
            continue  # 方法与描述符，不是字段
        order.append(key)
        if isinstance(value, _Factory):
            factories[key] = value.factory
            defaults[key] = None
        elif isinstance(value, _Default):
            defaults[key] = value.value
        else:
            defaults[key] = value
            factories[key] = None

    # 只在注解里出现、类体没赋值的字段（必填）
    for key in getattr(cls, "__annotations__", {}):
        if key not in order:
            order.append(key)
            defaults.setdefault(key, None)
            factories.setdefault(key, None)

    def __init__(self: Any, *args: Any, **kw: Any) -> None:
        if len(args) > len(order):
            raise TypeError(
                "%s() takes at most %d arguments (%d given)"
                % (cls.__name__, len(order), len(args))
            )
        values: Dict[str, Any] = {}
        for i, key in enumerate(order):
            factory = factories.get(key)
            if i < len(args):
                if key in kw:
                    raise TypeError(
                        "%s() got multiple values for argument %r"
                        % (cls.__name__, key)
                    )
                values[key] = args[i]
            elif key in kw:
                values[key] = kw[key]
            elif factory is not None:
                values[key] = factory()
            else:
                values[key] = defaults.get(key)
        for key in order:
            if frozen:
                object.__setattr__(self, key, values[key])
            else:
                setattr(self, key, values[key])

    def __repr__(self: Any) -> str:
        inner = ", ".join("%s=%r" % (k, getattr(self, k, None)) for k in order)
        return "%s(%s)" % (cls.__name__, inner)

    def __eq__(self: Any, other: Any) -> Any:
        if other.__class__ is not self.__class__:
            return NotImplemented

        def norm(v: Any) -> Any:
            if isinstance(v, dict):
                return tuple(sorted((k, norm(x)) for k, x in v.items()))
            if isinstance(v, (list, tuple)):
                return tuple(norm(x) for x in v)
            return v

        return all(
            norm(getattr(self, k, None)) == norm(getattr(other, k, None))
            for k in order
        )

    def __hash__(self: Any) -> int:
        def norm(v: Any) -> Any:
            if isinstance(v, dict):
                return tuple(sorted((k, norm(x)) for k, x in v.items()))
            if isinstance(v, (list, tuple)):
                return tuple(norm(x) for x in v)
            return v

        return hash(tuple(norm(getattr(self, k, None)) for k in order))

    cls.__init__ = __init__
    cls.__repr__ = __repr__
    cls.__eq__ = __eq__
    cls.__hash__ = __hash__
    if frozen:
        # __slots__ 无法在这里加（类已建好），改用 __setattr__ 拦截
        def _deny(self: Any, key: str, value: Any) -> None:
            raise AttributeError(
                "%s 是 frozen dataclass，不能给 %r 赋值" % (cls.__name__, key)
            )

        cls.__setattr__ = _deny
        cls.__delattr__ = _deny  # type: ignore[assignment]
    return cls
