"""最小 JSON Schema 校验器与确定性哈希。

为什么自研而不是引 jsonschema：
- 内核不允许依赖外部基础设施/模型，依赖也必须可离线、可确定性复现；
- 环境未安装 jsonschema（验收命令同样不安装任何新依赖）；
- 工具契约只需要实用子集：type / properties / required / items / enum /
  const / minLength / maxLength / pattern / minimum / maximum / minItems /
  maxItems。超出子集的关键字按 JSON Schema 语义忽略（宽松向前兼容）。

哈希统一用 `sha256_hex(canonical_json(value))`：
同一输入在任何机器/进程上产出同一哈希，供幂等键与轨迹摘要使用。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def canonical_json(value: Any) -> str:
    """把任意可 JSON 序列化对象压成确定性字符串（键排序、紧凑分隔）。"""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def sha256_hex(value: Any) -> str:
    """对值求确定性 sha256（输入/输出哈希，轨迹摘要用）。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _type_name(value: Any) -> str:
    """JSON 语义的类型名（bool 不是 integer）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_ok(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "array":
        return isinstance(value, (list, tuple))
    if type_name == "object":
        return isinstance(value, dict)
    return False


def validate_schema(value: Any, schema: dict[str, Any]) -> list[str]:
    """校验 value 是否满足 schema，返回错误列表；空列表表示通过。"""
    if not isinstance(schema, dict):
        return []

    errors: list[str] = []

    type_name = schema.get("type")
    if type_name is not None:
        if not _type_ok(value, str(type_name)):
            errors.append(f"期望类型 {type_name}，实际 {_type_name(value)}")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"值不在枚举 {schema['enum']} 中")
    if "const" in schema and value != schema["const"]:
        errors.append(f"值不等于 const {schema['const']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"字符串长度 {len(value)} 小于 minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"字符串长度 {len(value)} 大于 maxLength {schema['maxLength']}")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(str(pattern), value) is None:
            errors.append(f"字符串不匹配 pattern {pattern}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{value} 小于 minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{value} 大于 maximum {schema['maximum']}")

    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"数组长度 {len(value)} 小于 minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"数组长度 {len(value)} 大于 maxItems {schema['maxItems']}")
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for i, item in enumerate(value):
                for e in validate_schema(item, items_schema):
                    errors.append(f"items[{i}]: {e}")

    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"缺少必填字段 {req}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, sub in value.items():
                if key in properties:
                    for e in validate_schema(sub, properties[key]):
                        errors.append(f"{key}: {e}")

    return errors


def is_valid(value: Any, schema: dict[str, Any]) -> bool:
    """校验是否通过。"""
    return not validate_schema(value, schema)
