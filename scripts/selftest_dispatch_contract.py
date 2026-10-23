"""自证：把派单引擎的缺陷注入回去，确认测试真的会红。

注入清单（每条都对应本项目真实出现过的写法）：
  1. 类别加权退回「只看 event」—— 收 task/robot 形参却不用
  2. 类别加权退回硬编码 FOAM 字面量
  3. priority 退回硬编码三元式 `1 if ... else 3`
  4. TaskPriority 与 WasteClass.HIGH_PRIORITY 脱钩（改分级依据）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PY = sys.executable

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DISPATCH = BACKEND / "app/services/dispatch.py"
TASK_MODEL = BACKEND / "app/models/task.py"

TEST_FILE = "tests/test_dispatch_engine.py"
STATIC_GUARD_FILE = "tests/test_static_guards.py"

ok: list[str] = []
missed: list[str] = []
skipped: list[str] = []


def run_tests(target: str = TEST_FILE) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", target, "-q", "--no-header"],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def inject(path: Path, old: str, new: str, label: str, target: str = TEST_FILE) -> None:
    original = path.read_text(encoding="utf-8")
    if old not in original:
        skipped.append(label)
        print(f"  [SKIP] {label}  —— 锚点未命中，注入未生效")
        return
    try:
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        code, out = run_tests(target)
        if code != 0:
            ok.append(label)
            failed = [
                ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAILED")
            ]
            print(f"  [PASS] {label}")
            for f in failed[:3]:
                print(f"         {f}")
        else:
            missed.append(label)
            print(f"  [FAIL] {label}  —— 注入后测试仍全绿，守卫无效！")
    finally:
        path.write_text(original, encoding="utf-8")


print("=" * 66)
print("自证：注入派单引擎缺陷，确认测试会红")
print("=" * 66)

print("\n[基线] 未注入任何缺陷")
code, out = run_tests()
if code == 0:
    print("  全绿 ✓（可以开始注入）")
else:
    print("  基线就是红的，先修好再自证：")
    print(out[-2000:])
    sys.exit(1)

# ---------- 注入 1：加权退回只看 event ----------
print("\n[注入 1] 类别加权退回「只看 event、不比对在途任务」")
inject(
    DISPATCH,
    """        active_tasks = await self.tasks.list_active_tasks_for_robot(robot_id)
        if not active_tasks:
            return 1   # 手上没任务 → 无复用可言，普通权重

        active_event_ids = [t.event_id for t in active_tasks if t.event_id]
        if not active_event_ids:
            return 1

        classes = await self.events.list_main_classes_for_events(active_event_ids)
        # 该机器人任一在途任务的类别与本次事件相同 → 顺路，加权
        return 0 if event.main_class in classes else 1""",
    """        return 0 if event.main_class == "foam" else 1""",
    "加权只看 event（本缺陷的原始写法）",
)

# ---------- 注入 2：形参完全弃用 ----------
print("\n[注入 2] 加权函数收了机器人参数却不用")
inject(
    DISPATCH,
    "    async def _class_match_weight(self, event: Event, robot_id: str) -> int:",
    "    async def _class_match_weight(self, event: Event, robot_id: str, _unused_marker=None) -> int:",
    "签名演进（占位，预期不触发失败）",
)

# ---------- 注入 3：priority 退回硬编码 ----------
print("\n[注入 3] priority 退回硬编码三元式")
inject(
    DISPATCH,
    "        priority = TaskPriority.for_waste_class(event.main_class)",
    """        priority = 1 if event.main_class == "foam" else 3""",
    "priority 硬编码（与真源脱钩）",
)

# ---------- 注入 4：分级依据与 HIGH_PRIORITY 脱钩 ----------
print("\n[注入 4] TaskPriority 改写死类别名，与 HIGH_PRIORITY 脱钩")
inject(
    TASK_MODEL,
    """        from app.models.event import WasteClass

        return cls.URGENT if main_class in WasteClass.HIGH_PRIORITY else cls.NORMAL""",
    """        return cls.URGENT if main_class == "foam" else cls.NORMAL""",
    "分级依据脱钩（漏掉 fishing_gear）",
)

# ---------- 注入 5：通用静态守卫必须能独立抓到「收而不用」 ----------
print("\n[注入 5] 通用静态守卫独立验证：新增一个收了不用形参的函数")
inject(
    DISPATCH,
    "    async def _class_match_weight(self, event: Event, robot_id: str) -> int:",
    "    async def _probe_unused(self, marker: str) -> int:\n"
    "        return 1\n\n"
    "    async def _class_match_weight(self, event: Event, robot_id: str) -> int:",
    "通用守卫 test_static_guards.py 抓到未使用形参",
    target=STATIC_GUARD_FILE,
)

total = len(ok) + len(missed) + len(skipped)
print("\n" + "=" * 66)
print(f"命中 {len(ok)}/{total}  锚点失效 {len(skipped)}  漏报 {len(missed)}")
print("=" * 66)
if skipped:
    print("★ 以下注入的锚点未命中 —— 必须修正锚点，否则自证是空的：")
    for s in skipped:
        print(f"  - {s}")
if missed:
    print("★ 以下缺陷注入后测试仍绿 —— 守卫存在盲区：")
    for m in missed:
        print(f"  - {m}")

sys.exit(0 if (not missed and not skipped) else 1)
