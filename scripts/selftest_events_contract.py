"""自证：把刚修好的缺陷重新注入，确认测试真的会红。

只验「当前全绿」是不够的 —— 一个永远绿的测试没有任何守卫能力。
本脚本对每种缺陷形状各注入一次，跑对应测试，确认退出码非 0。

注入清单：
  1. events.py 退回「查全库最新任务」旧写法
  2. _try_dispatch 退回 -> None（丢掉本次产物）
  3. _try_dispatch 成功路径改成裸 return
  4. 某个仓库方法的元组解包被去掉（模拟新引入同类缺陷）
  5. 同步派单入口重新把事件投进 Redis Stream（去掉 enqueue_dispatch=False）
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

EVENTS_API = BACKEND / "app/api/v1/events.py"
HANDLERS = BACKEND / "app/mqtt/handlers.py"
REPOS = BACKEND / "app/repositories/__init__.py"

TEST_FILE = "tests/test_events_ingest_contract.py"

ok: list[str] = []
missed: list[str] = []
skipped: list[str] = []


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST_FILE, "-q", "--no-header"],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def inject(path: Path, old: str, new: str, label: str) -> None:
    """注入一处缺陷，跑测试，确认变红，然后还原。"""
    original = path.read_text(encoding="utf-8")
    if old not in original:
        skipped.append(label)
        print(f"  [SKIP] {label}  —— 锚点未命中，注入未生效")
        return
    try:
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        code, out = run_tests()
        if code != 0:
            ok.append(label)
            # 抽取失败测试名作为证据
            failed = [
                ln.strip()
                for ln in out.splitlines()
                if ln.strip().startswith("FAILED")
            ]
            print(f"  [PASS] {label}")
            for f in failed[:3]:
                print(f"         {f}")
        else:
            missed.append(label)
            print(f"  [FAIL] {label}  —— 注入后测试仍然全绿，守卫无效！")
    finally:
        path.write_text(original, encoding="utf-8")


print("=" * 66)
print("自证：注入已知缺陷，确认测试会红")
print("=" * 66)

# ---------- 基线 ----------
print("\n[基线] 未注入任何缺陷")
code, out = run_tests()
if code == 0:
    print("  全绿 ✓（可以开始注入）")
else:
    print("  基线就是红的，先修好再自证：")
    print(out[-2000:])
    sys.exit(1)

# ---------- 注入 1：退回旧写法 ----------
print("\n[注入 1] events.py 退回「查全库最新任务」")
inject(
    EVENTS_API,
    """        task = await _try_dispatch(payload.event_id)
        if task is not None:
            result.task_created = True
            result.task_id = task.task_id""",
    """        await _try_dispatch(payload.event_id)
        from app.repositories import TaskRepository

        tasks = await TaskRepository(session).list_tasks(limit=1)
        if tasks[0]:
            result.task_created = True
            result.task_id = tasks[0][0].task_id""",
    "旧写法反查（list_tasks 未解包）",
)

# ---------- 注入 2：丢掉返回值 ----------
print("\n[注入 2] _try_dispatch 返回类型退化")
inject(
    HANDLERS,
    "async def _try_dispatch(event_id: str) -> Task | None:",
    "async def _try_dispatch(event_id: str) -> None:",
    "_try_dispatch 返回注解退化为 None",
)

# ---------- 注入 3：成功路径裸 return ----------
print("\n[注入 3] _try_dispatch 成功路径改成裸 return")
inject(
    HANDLERS,
    """        await finalize_dispatch(task)

        return task""",
    """        await finalize_dispatch(task)

        return""",
    "成功路径裸 return（丢产物）",
)

# ---------- 注入 4：新引入同类缺陷 ----------
print("\n[注入 4] 仓库层元组解包被去掉（模拟新引入同类缺陷）")
inject(
    EVENTS_API,
    "    result = await service.ingest(payload, enqueue_dispatch=False)",
    """    result = await service.ingest(payload)
    _probe_items = await EventRepository(session).list_events(limit=1)
    _probe_first = _probe_items[0]""",
    "新调用点未解包元组",
)

# ---------- 注入 5：同步派单入口重新入队 ----------
print("\n[注入 5] 同步派单入口去掉 enqueue_dispatch=False")
inject(
    HANDLERS,
    "        result = await service.ingest(ingest, enqueue_dispatch=False)",
    "        result = await service.ingest(ingest)",
    "同步派单入口重新投 Redis Stream",
)

# ---------- 汇总 ----------
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
