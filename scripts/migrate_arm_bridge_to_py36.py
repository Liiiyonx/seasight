"""把 edge/arm_bridge 下的 3.7+ 类型注解改写成 3.6 安全形式。

机械替换，幂等：重复运行不会二次改写。转换规则：

    dict[str, X]      → Dict[str, X]
    list[X]           → List[X]
    tuple[X, ...]     → Tuple[X, ...]
    set[X]            → Set[X]
    X | None          → Optional[X]
    from __future__ import annotations   → 删除（3.6 不支持）
    from typing import ...               → 补齐需要的新名字

同时给每个改过的文件补 ``from .py36_compat import ...``（或平级 import，
取决于该文件是否在 arm_bridge 包内）并把 ``dataclass``/``Protocol``
换成兼容层版本。

⚠️ 这是一次性迁移脚本，保留在仓库里是为了让"为什么这些注解长这样"
可追溯；日常开发不需要运行。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDGE = ROOT / "edge"

# 需要动的是**会上传到树莓派**的文件（部署清单里的那些）
TARGETS = [
    EDGE / "arm_bridge" / "drivers.py",
    EDGE / "arm_bridge" / "bridge.py",
    EDGE / "arm_bridge" / "ros_driver.py",
    EDGE / "arm_bridge" / "mqtt_transport.py",
    EDGE / "arm_bridge" / "main.py",
    EDGE / "arm_bridge" / "tools" / "teach_hiwonder_sequence.py",
    # ★ device_sim 要带全：``import device_sim.protocol`` 会先执行
    #   device_sim/__init__.py，它在模块级 import 了 device / faults /
    #   transport。少做迁移任何一个，部署到树莓派都会 SyntaxError。
    EDGE / "device_sim" / "__init__.py",
    EDGE / "device_sim" / "device.py",
    EDGE / "device_sim" / "protocol.py",
    EDGE / "device_sim" / "transport.py",
    EDGE / "device_sim" / "faults.py",
]

GENERIC = {
    "dict": "Dict",
    "list": "List",
    "tuple": "Tuple",
    "set": "Set",
    "frozenset": "FrozenSet",
    "type": "Type",
}

NEEDED_TYPING = {"Dict", "List", "Tuple", "Set", "FrozenSet", "Type",
                  "Optional", "Union", "Any", "Callable", "Iterator",
                  "Iterable", "Sequence", "Mapping"}


def strip_future_import(text: str) -> str:
    return re.sub(
        r"^from __future__ import annotations\n+", "", text, flags=re.M
    )


def convert_generics(text: str) -> str:
    """把内建泛型下标写法换成 typing 的等价形式。

    只替换**类型注解位置**：冒号后到逗号/右括号/等号/换行为止。
    避免误伤字符串字面量与字典字面量。
    """
    # dict[str, float] -> Dict[str, float]   （含嵌套）
    pattern = re.compile(
        r"(?<![\w.\"'])\b(" + "|".join(GENERIC) + r")\["
    )
    out = []
    i = 0
    while True:
        m = pattern.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        # 找到配对的右方括号，处理嵌套
        start = m.end() - 1
        depth = 0
        j = start
        while j < len(text):
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(text):
            out.append(text[m.start():])
            break
        inner = text[start + 1:j]
        out.append(GENERIC[m.group(1)] + "[" + convert_generics(inner) + "]")
        i = j + 1
    return "".join(out)


def convert_optional(text: str) -> str:
    """``X | None`` → ``Optional[X]``，支持**任意层嵌套**的下标泛型。

    前两版都栽在嵌套上：第一版只认单个 ``Name``，第二版的 ``[^\\[\\]]*``
    匹配不到 ``List[Dict[str, Any]] | None`` 里的第二个 ``[``。
    这里改成**手工扫描配对括号**，逐字符找左侧完整表达式：
    遇到 ``[`` 就跳到配对的 ``]``，再判断后面是不是 ``| ... | None``。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        # 找到一个标识符或下标起点
        if not (text[i].isalpha() or text[i] == "_"):
            out.append(text[i])
            i += 1
            continue
        start = i
        while i < n and (text[i].isalnum() or text[i] in "._"):
            i += 1
        # 吃掉任意层下标
        while i < n and text[i] == "[":
            depth = 0
            j = i
            while j < n:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                break
            i = j + 1
        expr = text[start:i]
        # ★ 关键：`|` 与标识符之间通常有空格（PEP8 风格），
        #   必须先跳过空白再判断，否则 `int | None` 永远匹配不上。
        j = i
        while j < n and text[j] in " \t":
            j += 1
        if j < n and text[j] == "|":
            # 收集连续的 `| X` 项，看是否以 None 结尾
            items = [expr]
            while j < n and text[j] == "|":
                k = j + 1
                while k < n and text[k] in " \t":
                    k += 1
                if k < n and (text[k].isalpha() or text[k] == "_"):
                    e = k
                    while e < n and (text[e].isalnum() or text[e] in "._"):
                        e += 1
                    while e < n and text[e] == "[":
                        depth = 0
                        m2 = e
                        while m2 < n:
                            if text[m2] == "[":
                                depth += 1
                            elif text[m2] == "]":
                                depth -= 1
                                if depth == 0:
                                    break
                            m2 += 1
                        if m2 >= n:
                            break
                        e = m2 + 1
                    items.append(text[k:e])
                    j = e
                else:
                    break
            if items[-1] == "None":
                rest = items[:-1]
                repl = ("Optional[%s]" % rest[0] if len(rest) == 1
                        else "Optional[Union[%s]]" % ", ".join(rest))
                out.append(repl)
                i = j
                continue
            # ★ 非 Optional 的联合类型：`str | os.PathLike` 之类。
            #   3.6 在定义时就会 TypeError（'type' 不支持 '|'），必须转 Union。
            #   （2026-10-05 实机部署时踩到，报
            #   "unsupported operand type(s) for |: 'type' and 'ABCMeta'"）
            repl = ("Union[%s]" % ", ".join(items) if len(items) > 1
                    else items[0])
            out.append(repl)
            i = j
            continue
        out.append(expr)
        i = i  # 空白留给下一轮原样输出
    return "".join(out)


def fix_typing_import(text: str) -> str:
    """确保 typing 导入包含所有用到的名字。"""
    used = set()
    for name in NEEDED_TYPING:
        if re.search(r"\b%s\[" % name, text) or re.search(r"\b%s\b" % name, text):
            used.add(name)
    if not used:
        return text
    m = re.search(r"^from typing import (.+)$", text, flags=re.M)
    if m:
        have = {p.strip() for p in m.group(1).split(",")}
        add = sorted(used - have)
        if add:
            new = "from typing import %s" % ", ".join(sorted(have | used))
            text = text[:m.start()] + new + text[m.end():]
        return text
    # 没有 typing 导入就加一行（插在最后一个 import 之后）
    lines = text.split("\n")
    last = 0
    for idx, line in enumerate(lines[:80]):
        if line.startswith(("import ", "from ")):
            last = idx
    lines.insert(last + 1, "from typing import %s" % ", ".join(sorted(used)))
    return "\n".join(lines)


def swap_stdlib_imports(text: str) -> str:
    """把标准库的 3.7+ 构造换成本地兼容层。

    两个包各自持有 py36_compat 副本（为了能独立部署到树莓派），
    所以都用**相对导入**，写法一致。
    """
    text = re.sub(
        r"^from dataclasses import dataclass, field$",
        "from .py36_compat import dataclass, field",
        text, flags=re.M,
    )
    # 若只 import 了其中之一，按实际用到的补
    if "from dataclasses import" in text:
        names = re.search(r"^from dataclasses import (.+)$", text, flags=re.M)
        if names:
            wanted = [n.strip() for n in names.group(1).split(",")]
            keep = [n for n in wanted if n != "field"]
            repl = "from .py36_compat import %s" % ", ".join(
                (keep + ["field"]) if "field" in wanted else keep
            )
            text = text.replace(names.group(0), repl)
    # typing.Protocol (3.8+) → 兼容层
    def _strip_protocol(m: "re.Match[str]") -> str:
        raw = m.group(1)
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if "Protocol" not in names:
            return m.group(0)  # 不含 Protocol，原样返回
        rest = [n for n in names if n != "Protocol"]
        lines = []
        if rest:
            lines.append("from typing import %s" % ", ".join(rest))
        lines.append("from .py36_compat import Protocol")
        return "\n".join(lines)

    text = re.sub(r"^from typing import (.*)$", _strip_protocol, text, flags=re.M)
    return text


def process(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    text = original
    text = strip_future_import(text)
    text = convert_generics(text)
    text = convert_optional(text)
    # ★ 顺序要紧：swap_stdlib_imports 会重写 typing 那一行，
    #   所以补齐 typing 名字必须放在它**之后**，否则刚补的 Union/Optional
    #   会被随后的正则抹掉（2026-10-05 实测：NameError: Union not defined）。
    text = swap_stdlib_imports(text)
    text = fix_typing_import(text)
    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def audit(paths: list) -> list:
    """AST 复查：抓出**仍然残留**的 3.7+ 构造。

    ★ 为什么必须有这一步：第一版脚本只用正则，跑完"看起来成功"，
    但 AST 检查仍找出 12 处残留（`List[int] | None` 这类嵌套泛型、
    未被正则覆盖的 `from dataclasses import`）。**正则做迁移，
    AST 做验收** —— 两者都要。

    ★ 只扫**注解位置**：`X | Y` 在 3.6 只有出现在注解里才是问题；
      运行时代码里的集合并集（`event_times | {end_at}`）、字典合并之类
      完全合法，早期版本把它误报成"联合类型"。所以这里判断依据是
      **该 BinOp 是否处在 AnnAssign/arg/returns 等注解子树的上下文里**。
    """
    import ast

    builtin_generics = {"list", "dict", "tuple", "set", "frozenset", "type"}
    problems: list = []

    for path in paths:
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            problems.append((rel, exc.lineno or 0, "语法错误: %s" % exc))
            continue

        # ① 收集所有**注解**子树的节点 id —— 只有落在里面的 `|`
        #    才是 3.6 不支持的类型联合；运行时代码里的集合并集不算。
        annotation_ids: set = set()
        for node in ast.walk(tree):
            roots = []
            if isinstance(node, ast.AnnAssign):
                roots.append(node.annotation)
            elif isinstance(node, ast.arg):
                roots.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef,
                                   ast.AsyncFunctionDef)):
                roots.append(node.returns)
            for root in roots:
                if root is None:
                    continue
                for sub in ast.walk(root):
                    annotation_ids.add(id(sub))

        # ② 逐项检查
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in builtin_generics):
                problems.append(
                    (rel, node.lineno, "下标泛型 %s[...]" % node.value.id)
                )
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
                    and id(node) in annotation_ids):
                if (isinstance(node.right, ast.Constant)
                        and node.right.value is None):
                    problems.append((rel, node.lineno, "X | None 联合类型"))
                else:
                    problems.append(
                        (rel, node.lineno, "X | Y 联合类型（非 Optional）")
                    )
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                if "dataclasses" in mod:
                    problems.append((rel, node.lineno, "from dataclasses import"))
                if "Protocol" in names and mod == "typing":
                    problems.append(
                        (rel, node.lineno, "from typing import Protocol（3.8+）")
                    )
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                problems.append(
                    (rel, node.lineno, "from __future__ import（3.6 语法层）")
                )
    return problems


def main() -> int:
    changed = []
    for target in TARGETS:
        if not target.is_file():
            print("skip (missing): %s" % target)
            continue
        if process(target):
            changed.append(target)
            print("changed:   %s" % target.relative_to(ROOT))
        else:
            print("unchanged: %s" % target.relative_to(ROOT))

    # ★ AST 验收：正则改完必须复查，否则"看起来成功"是假的
    print()
    problems = audit(TARGETS)
    if problems:
        print("AST 复查发现 %d 处 3.7+ 残留：" % len(problems))
        for rel, line, kind in problems:
            print("  %s:%s  %s" % (rel, line, kind))
        return 1
    print("AST 复查通过：目标文件均无 3.7+ 构造")
    print("\n%d file(s) rewritten" % len(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
