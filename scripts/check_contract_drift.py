#!/usr/bin/env python
"""契约漂移检查：MQTT 主题树 与 类别枚举。

存在理由 —— 这个脚本要防的是一类**不会报错的缺陷**。

在实际工程里踩到过的三种表现：
  1. `robot/{id}/cmd/ack` 的路由分支用总段数做判据，但该主题与
     `robot/{id}/task/progress` 段数相同 → 分支永不命中 → 机器人 ACK
     被静默丢弃 → 任务永久停在 assigned。**没有异常，没有日志。**
  2. 作业状态映射表漏了 `done` → `dict.get` 返回 None → 被 if 静默吞掉
     → 任务永久卡在 collecting、事件永不 resolved。**同样无声无息。**
  3. schema 的类别字段用裸 str → 非法类别一路放行 → 前端配色回退成灰、
     派单白名单不命中、入库被 DB enum 拒绝而整个事件丢失。

这三种都是「文档声明了某集合，代码只实现了其中一部分」。抽样测试抓不到
（抽到的恰好是实现了的那几个），只有**穷举比对两个集合**才抓得到。

本脚本从 docs/ 解析出「声明集合」，从代码解析出「实现集合」，逐一对照。
退出码非 0 表示存在漂移。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK：✅ / ⚠️ 等字符直接 print 会抛 UnicodeEncodeError 并中断
# 校验输出。统一把 stdout/stderr 重配置为 UTF-8（errors='replace' 兜底），
# 只修输出编码，不改变任何契约校验语义；管道 / 文件重定向下同样生效。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
DOCS = ROOT / "docs"

PASS = 0
FAIL = 0
WARN = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  \033[32m[OK]\033[0m   {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  \033[31m[FAIL]\033[0m {msg}")


def warn(msg: str) -> None:
    global WARN
    WARN += 1
    print(f"  \033[33m[WARN]\033[0m {msg}")


def section(title: str) -> None:
    print(f"\n\033[36m== {title} ==\033[0m")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_platform_subscriptions(doc: str) -> set[str] | None:
    """从 §7.2 授权矩阵解析「平台后端」允许订阅的主题模式。

    ★ 为什么判据取这张表而不是 §3 的主题总表：
      §3 是**主题树全貌**（谁可以用这条主题、什么方向、什么 QoS），
      其中「设备 → 平台」只说明方向，不代表**平台后端**订阅了它。
      例如 `marine/{site}/{dev}/cmd/ack` 方向确实是上行，但平台并不订阅
      —— 通用指令下发当前无实际使用方，回执自然无人消费。
      权威清单是 §7.2 的 ACL 表：它写的就是"平台实际订阅什么"。
    """
    lines = doc.splitlines()
    for i, line in enumerate(lines):
        # 定位表头
        if "允许 subscribe" in line and "clientid 前缀" in line:
            for row in lines[i + 1:]:
                if not row.strip().startswith("|"):
                    break
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                if len(cells) < 4:
                    continue
                if "seasight-backend" in cells[1]:
                    # 第 4 列：以顿号分隔的若干 `主题模式`
                    patterns = set(re.findall(r"`([^`]+)`", cells[3]))
                    return patterns or None
            break
    return None


# ====================================================================
# 1. MQTT 主题树
# ====================================================================
def check_topic_tree() -> None:
    """平台订阅集合必须与实现一致，且每条订阅都要能路由到处理器。"""
    section("MQTT 主题树")

    doc = read(DOCS / "mqtt-topics.md")

    # ---- 权威声明集合：§7.2 授权矩阵里平台后端允许订阅的主题模式 ----
    subscribed = parse_platform_subscriptions(doc)
    if not subscribed:
        bad("没能从 §7.2 授权矩阵解析出平台订阅清单 —— 文档格式变了，检查脚本已失效")
        return

    # ---- 实现集合 ----
    sys.path.insert(0, str(BACKEND))
    try:
        from app.mqtt.client import MqttClient
        from app.mqtt.topics import SUBSCRIBE_PLAN, Topics
    except Exception as exc:  # pragma: no cover
        bad(f"无法导入 app.mqtt：{exc}")
        return

    implemented = set(SUBSCRIBE_PLAN)

    # ---- 逐条核对：文档订阅了但没实现 / 实现了但文档没声明 ----
    print(f"     文档 §7.2 声明平台订阅 {len(subscribed)} 条，实现 {len(implemented)} 条")

    not_implemented = subscribed - implemented
    undeclared = implemented - subscribed

    if not_implemented:
        bad(
            f"文档声明平台应订阅但代码未订阅：{sorted(not_implemented)}"
            "（消息会被 broker 挡在门外，设备侧表现为「发了没人理」）"
        )
    else:
        ok(f"文档声明的 {len(subscribed)} 条订阅全部已在 SUBSCRIBE_PLAN 实现")

    if undeclared:
        bad(f"代码订阅了但文档未声明：{sorted(undeclared)}（越权订阅或文档漏更新）")
    else:
        ok("没有未声明的订阅")

    # ---- 每条订阅都要能路由到处理器 ----
    router = MqttClient.__new__(MqttClient)
    plan_values = set(SUBSCRIBE_PLAN.values())

    for pattern in sorted(implemented):
        # 把通配符 + 换成具体值，得到一条可路由的实际主题
        parts = pattern.split("/")
        concrete = "/".join(
            "SITE01" if p == "+" and i == 1 else
            ("DEV01" if p == "+" else p)
            for i, p in enumerate(parts)
        )
        # robot/{id}/... 的第 2 段用 RBT01
        if parts[0] == "robot" and len(parts) > 1 and parts[1] == "+":
            concrete = "/".join("RBT01" if i == 1 else p for i, p in enumerate(parts))

        handler = router._match_handler(concrete)
        if handler is None:
            bad(f"{pattern} → 实际主题 {concrete} 路由不到任何处理器（消息会被静默丢弃）")
            continue
        if handler not in plan_values:
            bad(f"{pattern} 路由到 {handler}，但该处理器不在 SUBSCRIBE_PLAN 里")
            continue
        ok(f"{pattern:<28} → {handler}")

    # ---- ★ 根因守卫：robot 上行主题段数相同，判据必须是「结构」 ----
    robot_patterns = [p for p in implemented if p.startswith("robot/")]
    counts = {len(p.split("/")) for p in robot_patterns}
    if len(counts) == 1 and len(robot_patterns) > 1:
        print(
            f"     ★ robot 族 {len(robot_patterns)} 条订阅段数全部为 "
            f"{counts.pop()} —— 判据必须用第 2/3 段的值，不能用总段数"
        )


# ====================================================================
# 2. 主题 → 处理器注册表
# ====================================================================
def check_handler_registry() -> None:
    """SUBSCRIBE_PLAN 声明的每个处理器都要真的存在且是协程。"""
    section("主题 → 处理器注册表")

    sys.path.insert(0, str(BACKEND))
    from app.mqtt.handlers import HANDLERS
    from app.mqtt.topics import SUBSCRIBE_PLAN

    plan = set(SUBSCRIBE_PLAN.values())
    reg = set(HANDLERS)

    missing = plan - reg
    orphans = reg - plan

    if missing:
        bad(f"SUBSCRIBE_PLAN 声明了但未注册的处理器：{sorted(missing)}")
    else:
        ok(f"SUBSCRIBE_PLAN 的 {len(plan)} 个处理器全部已注册")

    if orphans:
        bad(f"已注册但无任何订阅会触发的孤儿处理器：{sorted(orphans)}")
    else:
        ok("没有孤儿处理器")

    # 签名统一性：全部 async (topic, payload)
    import inspect

    bad_sig = []
    for name, fn in HANDLERS.items():
        if not inspect.iscoroutinefunction(fn):
            bad_sig.append(f"{name}（不是 async）")
            continue
        params = list(inspect.signature(fn).parameters)
        if params[:2] != ["topic", "payload"]:
            bad_sig.append(f"{name}（签名 {params}）")
    if bad_sig:
        bad(f"处理器签名不统一：{bad_sig}")
    else:
        ok(f"全部 {len(reg)} 个处理器均为 async (topic, payload)")


# ====================================================================
# 3. 机器人作业状态映射
# ====================================================================
def check_robot_status_map() -> None:
    """mqtt-topics.md §5.6 声明的状态，ROBOT_STATUS_MAP 必须全部覆盖。"""
    section("机器人作业状态映射")

    doc = read(DOCS / "mqtt-topics.md")

    # ---- 只在 §5.6 小节内解析，避免抓到 §5.1 的字段表（那张表第 2 列也含反引号） ----
    lines = doc.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if line.startswith("### 5.6"):
            start = i
        elif start is not None and line.startswith("### 5.7"):
            end = i
            break
    if start is None:
        warn("找不到 §5.6 小节 —— 文档结构可能变了，跳过状态映射检查")
        return
    scope = lines[start:end if end else start + 60]

    # ---- 声明集合：§5.6 的映射表，形如 ----
    #   | `status` | 平台动作 |
    #   | `navigating` | `assigned` → `navigating` |
    #   | `returning`  | 映射为 `collecting`（返航仍属作业中） |
    #   第 1 列 = 机器人上报的状态（声明集合）
    #   第 2 列里出现的箭头后状态 = 期望的映射目标
    declared: set[str] = set()
    expected_target: dict[str, str] = {}
    for line in scope:
        m = re.match(r"^\|\s*`([a-z_]+)`\s*\|\s*(.+?)\s*\|\s*$", line.strip())
        if not m:
            continue
        report_status, action = m.group(1), m.group(2)
        if report_status in {"status", "robot_status", "review_result"}:
            continue
        if "`" not in action:
            continue
        declared.add(report_status)

        # 目标状态：动作列若是「`A` → `B`」，目标取箭头后的 B；
        # 若是「映射为 `B`」，目标取 B。
        arrow = re.search(r"→\s*`([a-z_]+)`", action)
        mapped = re.search(r"映射为\s*`([a-z_]+)`", action)
        if arrow:
            expected_target[report_status] = arrow.group(1)
        elif mapped:
            expected_target[report_status] = mapped.group(1)

    if not declared:
        warn("没能从文档解析出作业状态（格式可能变了），本轮跳过状态映射检查")
        return

    print(f"     文档声明 {len(declared)} 个状态：{sorted(declared)}")

    sys.path.insert(0, str(BACKEND))
    from app.mqtt.handlers import ROBOT_STATUS_MAP
    from app.models.task import TaskStatus

    implemented = set(ROBOT_STATUS_MAP)

    missing = declared - implemented
    extra = implemented - declared

    if missing:
        bad(
            f"文档声明但映射表缺失：{sorted(missing)}"
            "（★ 最恶劣的一类缺陷：dict.get 返 None 被 if 静默吞掉，任务永久卡住且无日志）"
        )
    else:
        ok(f"文档声明的 {len(declared)} 个状态全部有映射")

    if extra:
        warn(f"映射表有文档未声明的状态：{sorted(extra)}（确认是否文档漏更新）")

    # 映射目标必须是合法的 TaskStatus
    valid = {
        v for k, v in vars(TaskStatus).items()
        if not k.startswith("_") and isinstance(v, str)
    }
    invalid = {k: v for k, v in ROBOT_STATUS_MAP.items() if v not in valid}
    if invalid:
        bad(f"映射目标不是合法 TaskStatus：{invalid}；合法值 {sorted(valid)}")
    else:
        ok(f"全部映射目标均为合法 TaskStatus：{sorted(set(ROBOT_STATUS_MAP.values()))}")

    # ---- ★ 逐格核对：文档写「navigating → navigating」就要真的是这个结果 ----
    #    只查"有没有"是不够的：returning→collecting 这种**反直觉**映射
    #    （返航仍属作业中）极易被后人"顺手改正"成 returning→returning。
    mismatched = []
    for report_status, want in sorted(expected_target.items()):
        got = ROBOT_STATUS_MAP.get(report_status)
        if got == want:
            ok(f"{report_status:<12} → {got}")
        elif got is None:
            mismatched.append(f"{report_status}（文档要求 → {want}，但映射表里没有）")
        else:
            mismatched.append(f"{report_status}（文档要求 → {want}，实现 → {got}）")

    if mismatched:
        for item in mismatched:
            bad(f"映射目标与文档不符：{item}")
    elif expected_target:
        ok(f"全部 {len(expected_target)} 条映射的目标值与文档逐格一致")


# ====================================================================
# 4. 类别枚举一致性
# ====================================================================
def check_waste_class_enum() -> None:
    """WasteClass.ALL 必须与 DDL / simulator / 前端 / schema 逐层一致。"""
    section("类别枚举一致性")

    sys.path.insert(0, str(BACKEND))
    from app.models.event import WasteClass

    truth = tuple(WasteClass.ALL)
    print(f"     真源 WasteClass.ALL = {truth}")

    # ---- (a) 数据库 DDL ----
    ddl = read(BACKEND / "db" / "init" / "01_schema.sql")
    m = re.search(r"CREATE TYPE\s+waste_class_enum\s+AS\s+ENUM\s*\(([^)]*)\)", ddl, re.I)
    if not m:
        bad("01_schema.sql 里找不到 waste_class_enum 定义")
    else:
        vals = tuple(re.findall(r"'([^']+)'", m.group(1)))
        if vals == truth:
            ok(f"db/init/01_schema.sql waste_class_enum 一致")
        else:
            bad(f"db/init/01_schema.sql waste_class_enum 不一致：{vals}")

    # ---- (b) 边缘端 simulator ----
    sim = read(ROOT / "edge" / "simulator" / "simulator.py")
    m = re.search(r"CLASS_LABELS\s*=\s*\{(.*?)\}", sim, re.S)
    if not m:
        bad("edge/simulator/simulator.py 找不到 CLASS_LABELS")
    else:
        vals = tuple(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))
        if set(vals) == set(truth):
            ok("edge/simulator/simulator.py CLASS_LABELS 一致")
        else:
            bad(f"edge/simulator/simulator.py CLASS_LABELS 不一致：{vals}")

    # ---- (c) AI 推理服务 ----
    ai = read(BACKEND / "app" / "services" / "ai" / "server.py")
    m = re.search(r"CLASS_NAMES\s*=\s*\[([^\]]*)\]", ai)
    if not m:
        bad("app/services/ai/server.py 找不到 CLASS_NAMES")
    else:
        vals = tuple(re.findall(r'"([a-z_]+)"', m.group(1)))
        if vals == truth:
            ok("app/services/ai/server.py CLASS_NAMES 一致（且顺序相同）")
        elif set(vals) == set(truth):
            warn(f"CLASS_NAMES 集合一致但顺序不同：{vals}（顺序即模型输出索引，需确认）")
        else:
            bad(f"app/services/ai/server.py CLASS_NAMES 不一致：{vals}")

    # ---- (d) 前端 ----
    fe = ROOT / "frontend" / "src" / "utils" / "constants.js"
    if not fe.exists():
        warn("找不到 frontend/src/utils/constants.js，跳过前端检查")
    else:
        m = re.search(r"CLASS_ORDER\s*=\s*\[([^\]]*)\]", read(fe))
        if not m:
            bad("constants.js 找不到 CLASS_ORDER")
        else:
            vals = tuple(re.findall(r"'([a-z_]+)'", m.group(1)))
            if vals == truth:
                ok("frontend/src/utils/constants.js CLASS_ORDER 一致")
            else:
                bad(f"frontend/src/utils/constants.js CLASS_ORDER 不一致：{vals}")

    # ---- (e) schema 层必须做枚举约束（★ 曾漏） ----
    try:
        from pydantic import ValidationError

        from app.schemas import DetectionItem, EventAggregate

        probes = [
            ("EventAggregate.main_class",
             lambda: EventAggregate(main_class="__bogus__", count=1, max_confidence=0.9)),
            ("DetectionItem.class",
             lambda: DetectionItem(**{"class": "__bogus__", "confidence": 0.9, "bbox": [0, 0, 1, 1]})),
        ]
        for label, make in probes:
            try:
                make()
                bad(f"{label} 放行了非法类别 —— 缺枚举约束（前端配色/派单/入库会同时静默失效）")
            except ValidationError:
                ok(f"{label} 正确拒绝非法类别")
    except ImportError as exc:  # pragma: no cover
        bad(f"无法导入 app.schemas：{exc}")

    # ---- (f) 高优先级白名单不得硬编码 ----
    src = read(BACKEND / "app" / "mqtt" / "handlers.py")
    tree = ast.parse(src)
    found_hardcode = False
    for node in ast.walk(tree):
        # 找形如 in ("foam", "fishing_gear") 的比较
        if isinstance(node, ast.Compare) and len(node.comparators) == 1:
            comp = node.comparators[0]
            if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                vals = [
                    e.value for e in comp.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if set(vals) and set(vals) <= set(truth):
                    # 只有整份白名单都硬编码才算问题
                    if set(vals) == set(WasteClass.HIGH_PRIORITY):
                        found_hardcode = True
                        print(f"          handlers.py:{node.lineno} 硬编码白名单 {sorted(vals)}")

    if found_hardcode:
        bad(
            "派单白名单在 handlers.py 里硬编码 —— 应改用 WasteClass.HIGH_PRIORITY"
            "（api/v1/events.py 用的是常量，两处各写一份必然漂移）"
        )
    else:
        ok("派单白名单取自 WasteClass.HIGH_PRIORITY，无硬编码副本")


# ====================================================================
# 5. 事件上报必填字段
# ====================================================================
def check_event_payload_fields() -> None:
    """文档标记为必填(✅)的字段，schema 里必须确实是必填。"""
    section("事件上报必填字段")

    doc = read(DOCS / "mqtt-topics.md")

    declared_required: set[str] = set()
    for line in doc.splitlines():
        # | `aggregate.main_class` | enum | ✅ | ... |
        m = re.match(r"^\|\s*`([a-zA-Z_.\[\]]+)`\s*\|[^|]*\|\s*(✅|⚠️|❌)\s*\|", line.strip())
        if m and m.group(2) == "✅":
            name = m.group(1)
            # 只取顶层字段（不含 . 和 []）
            if "." not in name and "[" not in name:
                declared_required.add(name)

    if not declared_required:
        warn("没能解析出必填字段表，跳过")
        return

    print(f"     文档标记必填的顶层字段：{sorted(declared_required)}")

    sys.path.insert(0, str(BACKEND))
    from app.schemas import EventIngest

    required_in_schema = {
        name
        for name, f in EventIngest.model_fields.items()
        if f.is_required()
    }

    # device_type 在 schema 里有默认值（shore_camera），但文档标 ✅。
    # ★ 这是**有意为之**而非缺陷：平台侧给默认值是为了让老固件
    #   （不带上报类型）也能入库，handler 另有 .get 兜底。
    #   故只作提示，不计入警告 —— 长期挂着的 WARN 会导致告警疲劳，
    #   真正的问题反而被淹没。
    missing = declared_required - required_in_schema
    tolerable = {"device_type"}
    real_missing = missing - tolerable

    for name in sorted(missing & tolerable):
        print(f"     [提示] `{name}` 文档标 ✅ 但 schema 有默认值 —— 有意宽容，非缺陷")
    for name in sorted(real_missing):
        bad(f"`{name}` 文档标必填，但 schema 里非必填")

    if not real_missing:
        ok(f"文档标记的 {len(declared_required)} 个必填字段与 schema 一致")

    # 反向：schema 必填但文档没标 —— 会导致边缘端漏发
    doc_only = required_in_schema - declared_required
    if doc_only:
        warn(f"schema 必填但文档未标记 ✅：{sorted(doc_only)}（边缘端实现者可能漏发）")
    else:
        ok("schema 必填字段均在文档中有标记")


# ====================================================================
def main() -> int:
    print("\033[1m契约漂移检查 —— MQTT 主题树与类别枚举\033[0m")
    print(f"仓库根：{ROOT}")

    check_topic_tree()
    check_handler_registry()
    check_robot_status_map()
    check_waste_class_enum()
    check_event_payload_fields()

    print(f"\n{'=' * 62}")
    print(f"  通过 {PASS} · 警告 {WARN} · 失败 {FAIL}")
    print(f"{'=' * 62}")

    if FAIL:
        print("\033[31m★ 存在契约漂移，必须修复后再提交。\033[0m")
        print("  提示：这类缺陷的共同特征是**不抛异常也不打日志**，")
        print("  只表现为「业务不动了」。修完请同时补一条守卫测试。")
        return 1

    print("\033[32m契约一致。\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
