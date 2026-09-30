"""自证：注入已知缺陷，确认契约守卫真的会红。

★ 为什么必须做这件事：
  本项目的教训是「校验工具自身会静默失效」——
    · AST 分析漏掉 AsyncFunctionDef，差点误判源码结构
    · `len(parts)==5` 判据本身写错，让脚本自造假缺陷
    · 主题方向列让脚本把「文档声明存在」误读成「平台必须订阅」
    · 自证脚本的注入锚点写错（DDL 实为单行写法），该项静默 SKIP
    · 探针只看响应顶层，而业务字段包在 ApiResponse 信封里，全部误报"缺失"
  一个永远返回「全绿」的检查脚本比没有脚本更糟，因为它给出虚假的安全感。
  所以对每个检查项，都要注入一次已知缺陷，确认它真的会红。

★ 自证覆盖两类守卫
──────────────────
  1. 脚本型：scripts/check_contract_drift.py（MQTT 主题树、状态映射、枚举）
  2. 测试型：backend/tests/test_ws_contract.py（WS 推送负载的 AST 穷举扫描）
  两类都要验，不能只验脚本 —— 测试型守卫同样可能因为
  「扫描器失效导致零问题通过」而空转。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK：自证日志会回显捕获输出（含 ✅ / ⚠️ 等字符），
# 先重配置 stdout/stderr 为 UTF-8，避免 print 抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 子进程（pytest / 检查脚本）必须显式以 UTF-8 输出：
# 仅靠父进程 decoding="utf-8" 不够 —— 若控制台是 GBK 且未设
# PYTHONIOENCODING，子进程会按 GBK 编码写出字节，父进程按 UTF-8
# 解码得到乱码，证据行就匹配不上了。这里在子进程环境里强制 UTF-8。
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# 捕获的子进程输出带 ANSI 颜色码，写进结果文件前剥掉，保持可复核文本干净。
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text)


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
PY = sys.executable
CHECKER = ROOT / "scripts" / "check_contract_drift.py"
WS_TEST = "tests/test_ws_contract.py"

OUT = ROOT / "scripts" / "selftest_result.txt"
lines: list[str] = []
skipped: list[str] = []
missed: list[str] = []
total = 0


def log(s: str = "") -> None:
    lines.append(s)
    print(s)


def run_checker() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, str(CHECKER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_UTF8_ENV,
        cwd=str(ROOT),
    )
    return proc.returncode, _clean((proc.stdout or "") + (proc.stderr or ""))


def run_pytest(rel: str) -> tuple[int, str]:
    """跑指定测试文件，返回 (退出码, 输出)。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", rel, "-q", "--no-header", "-x"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_UTF8_ENV,
        cwd=str(BACKEND),
    )
    return proc.returncode, _clean((proc.stdout or "") + (proc.stderr or ""))


def inject(
    path: Path,
    old: str,
    new: str,
    label: str,
    guard=None,
    evidence_marker: str = "[FAIL]",
) -> None:
    """临时改一处代码，跑守卫，确认变红，然后**字节级原样还原**。

    guard            ：守卫运行器，默认跑 check_contract_drift.py；
                       传 run_pytest 的偏函数可验证测试型守卫。
    evidence_marker  ：从输出里挑证据行时用的标记。

    ★ 字节级还原：Windows 上文本模式 write_text 会把 '\n' 翻成 '\r\n'，
    还原后文件字节与原始不一致（git 出现假 M、mtime 搅动）。
    因此这里始终以 read_bytes 读原始字节、write_bytes 写回，
    锚点匹配与替换在内存中的 UTF-8 文本上进行，落盘字节前后完全一致。
    """
    global total
    total += 1
    run_guard = guard or run_checker
    raw = path.read_bytes()
    # 锚点匹配统一在 LF 文本上进行：Windows 检出文件多为 CRLF，
    # 锚点字符串用 '\n' 书写，需先归一化，避免 CRLF 导致锚点失效。
    original = raw.decode("utf-8").replace("\r\n", "\n")
    if old not in original:
        # ★ 锚点失效必须显式记账，不能静默跳过：
        #   否则源码一改，自证就悄悄少覆盖一项，而报告还是"看起来全过"。
        skipped.append(label)
        log(f"  [SKIP] {label}")
        log(f"         原因：在 {path.name} 里找不到注入锚点 —— 源码已变动，")
        log("         请更新本脚本的锚点字符串，否则该项自证已失效")
        return
    try:
        path.write_bytes(original.replace(old, new, 1).encode("utf-8"))
        code, out = run_guard()
        if code != 0:
            evidence = next(
                (ln.strip() for ln in out.splitlines() if evidence_marker in ln),
                "(未找到证据行，但退出码非 0)",
            )
            log(f"  [PASS] {label}")
            log(f"         注入后退出码 {code}，证据：{evidence[:104]}")
        else:
            missed.append(label)
            log(f"  [FAIL] {label} —— 注入缺陷后守卫仍全绿，检查项失效！")
    finally:
        path.write_bytes(raw)


log("=" * 70)
log("检查脚本自证 —— 逐个注入已知缺陷，确认能被抓到")
log("=" * 70)

code, out = run_checker()
log(f"\n[基线] 当前退出码 = {code}（应为 0）")
if code != 0:
    log("  基线不是全绿，后续自证无意义。输出尾部：")
    log("\n".join(out.splitlines()[-15:]))
    OUT.write_text("\n".join(lines), encoding="utf-8")
    sys.exit(1)

log("\n--- 逐个注入 ---")

# A：路由判据退回「总段数」
inject(
    BACKEND / "app" / "mqtt" / "client.py",
    '            if parts[2] == "cmd" and parts[3] == "ack":\n'
    '                return "handle_robot_ack"',
    '        if len(parts) == 5 and parts[0] == "robot":\n'
    '            if parts[3] == "cmd" and parts[4] == "ack":\n'
    '                return "handle_robot_ack"',
    "缺陷A：robot cmd/ack 路由退回总段数判据（该主题实际只有 4 段）",
)

# B：状态映射漏掉 done
inject(
    BACKEND / "app" / "mqtt" / "handlers.py",
    '    "returning": "collecting",\n    "done": "done",',
    '    "returning": "collecting",',
    "缺陷B：ROBOT_STATUS_MAP 漏掉 done（文档 §5.6 要求有）",
)

# C：returning 被改成映射到合法但错误的值
inject(
    BACKEND / "app" / "mqtt" / "handlers.py",
    '    "returning": "collecting",',
    '    "returning": "navigating",',
    "缺陷C：returning→navigating（合法值但文档要求 collecting）",
)

# D：schema 退回裸 str
inject(
    BACKEND / "app" / "schemas" / "__init__.py",
    "    main_class: WasteClassLiteral\n",
    "    main_class: str\n",
    "缺陷D：EventAggregate.main_class 退回裸 str（枚举约束丢失）",
)

# E：派单白名单重新硬编码
inject(
    BACKEND / "app" / "mqtt" / "handlers.py",
    "        if ingest.aggregate.main_class in WasteClass.HIGH_PRIORITY:",
    '        if ingest.aggregate.main_class in ("foam", "fishing_gear"):',
    "缺陷E：派单白名单硬编码回字面量（与 HIGH_PRIORITY 脱钩）",
)

# F：前端 CLASS_ORDER 多一类
inject(
    ROOT / "frontend" / "src" / "utils" / "constants.js",
    "export const CLASS_ORDER = ['foam', 'plastic', 'fishing_gear', 'other']",
    "export const CLASS_ORDER = ['foam', 'plastic', 'fishing_gear', 'other', 'ghost']",
    "缺陷F：前端 CLASS_ORDER 多出一类（与后端枚举漂移）",
)

# G：数据库 enum 少一类
inject(
    BACKEND / "db" / "init" / "01_schema.sql",
    "CREATE TYPE waste_class_enum AS ENUM ('foam', 'plastic', 'fishing_gear', 'other');",
    "CREATE TYPE waste_class_enum AS ENUM ('foam', 'plastic', 'fishing_gear');",
    "缺陷G：DB waste_class_enum 少一类（'other' 丢字段）",
)

# H：订阅计划少一条
inject(
    BACKEND / "app" / "mqtt" / "topics.py",
    '    Topics.ROBOT_ACK_WILDCARD: "handle_robot_ack",\n',
    "",
    "缺陷H：SUBSCRIBE_PLAN 漏掉 robot/+/cmd/ack（文档 §7.2 要求订阅）",
)

# ======================================================================
# 第二类：测试型守卫（backend/tests/test_ws_contract.py）
# ======================================================================
log("\n--- 测试型守卫（WS 推送负载）---")

# 先确认 WS 测试基线为绿，否则后面的注入无意义
code_ws, out_ws = run_pytest(WS_TEST)
log(f"  [基线] {WS_TEST} 退出码 = {code_ws}（应为 0）")
if code_ws != 0:
    log("    基线不是全绿，WS 侧自证无法进行。输出尾部：")
    log("\n".join(out_ws.splitlines()[-12:]))
    missed.append("WS 测试基线不为绿")


def _ws_guard():
    return run_pytest(WS_TEST)


# I：task_update 负载漏掉 event_id（★ 真实修过的缺陷）
inject(
    BACKEND / "app" / "mqtt" / "handlers.py",
    '                "event_id": task.event_id,\n                "robot_id": task.robot_id,',
    '                "robot_id": task.robot_id,',
    "缺陷I：handlers 的 task_update 负载漏掉 event_id",
    guard=_ws_guard,
    evidence_marker="缺少",
)

# J：契约常量漏声明一种消息类型
inject(
    BACKEND / "app" / "ws" / "manager.py",
    '    # 客户端 ping 的应答（无 data）\n    "pong": (),',
    '    # 客户端 ping 的应答（无 data）',
    "缺陷J：WS_MESSAGE_CONTRACT 漏掉 pong",
    guard=_ws_guard,
    evidence_marker="缺少",
)

# K：contract 里给 task_update 少写一个字段
inject(
    BACKEND / "app" / "ws" / "manager.py",
    '    "task_update": ("task_id", "event_id", "robot_id", "status"),',
    '    "task_update": ("task_id", "robot_id", "status"),',
    "缺陷K：task_update 契约少声明 event_id（校验会漏掉真正的缺失）",
    guard=_ws_guard,
    evidence_marker="缺少",
)

# L：前端丢了 robot_status 分支
inject(
    ROOT / "frontend" / "src" / "stores" / "realtime.js",
    "      } else if (msg.type === 'robot_status' && msg.data) {",
    "      } else if (msg.type === '__gone__' && msg.data) {",
    "缺陷L：前端丢了 robot_status 处理分支（消息被无声丢弃）",
    guard=_ws_guard,
    evidence_marker="没有处理分支",
)

log("\n--- 还原复检 ---")
code, out = run_checker()
code_ws2, out_ws2 = run_pytest(WS_TEST)
log(f"  check_contract_drift.py 退出码 = {code}（应为 0）")
log(f"  {WS_TEST} 退出码 = {code_ws2}（应为 0）")
if code == 0 and code_ws2 == 0:
    log("  [PASS] 所有注入均已还原，两类守卫都回到全绿")
else:
    log("  [FAIL] 还原不完整！")
    if code != 0:
        log("\n".join(out.splitlines()[-15:]))
    if code_ws2 != 0:
        log("\n".join(out_ws2.splitlines()[-15:]))

code = 0 if (code == 0 and code_ws2 == 0) else 1

# ---- 字节级还原自证 ----
# Windows 文本模式写回会把 '\n' 翻成 '\r\n'，导致"还原后"文件字节与原始
# 不一致（git 假 M、mtime 搅动）。这里对同一文件做一次注入→还原，
# 断言前后 SHA256 完全一致 —— 不一致即自证失败。
log("\n--- 字节级还原自证 ---")
_byte_selfcheck_target = BACKEND / "app" / "mqtt" / "handlers.py"
_raw_before = _byte_selfcheck_target.read_bytes()
_text_before = _raw_before.decode("utf-8")
_anchor = '    "returning": "collecting",'
if _anchor not in _text_before:
    log("  [SKIP] 字节级还原自证 —— handlers.py 锚点已变动")
else:
    _injected = _text_before.replace(
        _anchor, '    "returning": "collecting",  # byte-selfcheck', 1
    )
    try:
        _byte_selfcheck_target.write_bytes(_injected.encode("utf-8"))
        _byte_selfcheck_target.write_bytes(_raw_before)
    finally:
        _byte_selfcheck_target.write_bytes(_raw_before)
    _hash_before = hashlib.sha256(_raw_before).hexdigest()
    _hash_after = hashlib.sha256(
        _byte_selfcheck_target.read_bytes()
    ).hexdigest()
    if _hash_before == _hash_after:
        log(f"  [PASS] 注入→还原后字节完全一致，SHA256 一致（{_hash_before[:16]}…）")
    else:
        missed.append("字节级还原自证")
        log("  [FAIL] 注入→还原后字节不一致（git 会出现假 M）！")
        log(f"         还原前 SHA256 = {_hash_before}")
        log(f"         还原后 SHA256 = {_hash_after}")

# ---- 汇总 ----
log("\n" + "=" * 70)
log("自证汇总")
log("=" * 70)
log(f"  注入项总数 : {total}")
log(f"  成功捕获   : {total - len(skipped) - len(missed)}")
log(f"  未被捕获   : {len(missed)}  {missed if missed else ''}")
log(f"  锚点失效   : {len(skipped)}  {skipped if skipped else ''}")

exit_code = 0
if missed:
    log("\n  ★ 存在「注入缺陷却没被抓到」的检查项 —— 该检查项形同虚设")
    exit_code = 1
if skipped:
    log("\n  ★ 存在锚点失效的注入项 —— 自证覆盖率已下降，请更新锚点")
    exit_code = 1
if code != 0:
    log("\n  ★ 注入未还原干净 —— 工作区已被污染")
    exit_code = 1

if exit_code == 0:
    log("\n  全部注入均被捕获，且工作区干净。检查脚本可信。")
else:
    log("\n  自证未通过。")

OUT.write_text("\n".join(lines), encoding="utf-8")
sys.exit(exit_code)
