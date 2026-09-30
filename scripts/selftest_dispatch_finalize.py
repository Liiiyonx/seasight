"""自证：把「派单收尾」的缺陷注入回去，确认测试真的会红。

注入清单（每条都对应本工程真实出现过的写法）：
  1. 定时补派退回「只建任务、不收尾」（本缺陷的原始形态）
  2. 补派 API 退回「只建任务、不收尾」
  3. finalize_dispatch 里去掉下发 MQTT
  4. finalize_dispatch 里去掉推送看板
  5. reassign_task 不重置 assigned_at（陷入空转重派）
  6. 定时循环里去掉 ACK 超时回退（回到 handle_ack_timeout 零调用）
  7. 定时循环里去掉换车重派（回退后没人再派）
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
CONSUMER = BACKEND / "app/services/consumer.py"

TEST_FILE = "tests/test_dispatch_finalize.py"

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


def inject(path: Path, old: str, new: str, label: str) -> None:
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
print("自证：注入「派单收尾」缺陷，确认测试会红")
print("=" * 66)

print("\n[基线] 未注入任何缺陷")
code, out = run_tests()
if code == 0:
    print("  全绿 ✓（可以开始注入）")
else:
    print("  基线就是红的，先修好再自证：")
    print(out[-2000:])
    sys.exit(1)

# ---------- 注入 1：定时补派退回只建任务 ----------
print("\n[注入 1] 定时补派退回「只建任务、不下发、不推送」（原始缺陷）")
inject(
    CONSUMER,
    """                        await session.commit()
                        # ★ 补派也必须下发 + 推送。这里曾经只建任务不通知，
                        #   于是「日志说补派成功」而机器人一动不动。
                        await finalize_dispatch(task)
                        dispatched += 1""",
    """                        await session.commit()
                        dispatched += 1""",
    "定时补派漏接收尾",
)

# ---------- 注入 2：补派 API 退回只建任务 ----------
print("\n[注入 2] 补派 API 退回「只建任务、不下发、不推送」（原始缺陷）")
inject(
    DISPATCH,
    """        for task in created:
            await finalize_dispatch(task)
        return created""",
    """        return created""",
    "补派 API 漏接收尾",
)

# ---------- 注入 3：收尾函数去掉下发 ----------
print("\n[注入 3] finalize_dispatch 去掉 MQTT 下发")
inject(
    DISPATCH,
    """            await mqtt_client.publish_task(
                robot_id=task.robot_id,
                task_id=task.task_id,
                lng=lng,
                lat=lat,
                priority=task.priority,
            )""",
    """            pass""",
    "收尾缺下发（机器人收不到指令）",
)

# ---------- 注入 4：收尾函数去掉推送 ----------
print("\n[注入 4] finalize_dispatch 去掉看板推送")
inject(
    DISPATCH,
    """    await ws_manager.push_task_update(
        {
            "task_id": task.task_id,
            "event_id": task.event_id,
            "robot_id": task.robot_id,
            "status": task.status,
        }
    )""",
    """    pass""",
    "收尾缺推送（看板不刷新）",
)

# ---------- 注入 5：重派不重置计时起点 ----------
print("\n[注入 5] reassign_task 不重置 assigned_at")
inject(
    DISPATCH,
    """        task.assigned_at = _utcnow()

        await finalize_dispatch(task)
        return True""",
    """        await finalize_dispatch(task)
        return True""",
    "重派沿用旧时间戳（空转重派）",
)

# ---------- 注入 6：定时循环去掉 ACK 超时回退 ----------
print("\n[注入 6] 定时循环去掉 ACK 超时回退（回到 handle_ack_timeout 零调用）")
inject(
    CONSUMER,
    """            for task_id in assigned_ids:
                try:
                    async with session_factory() as session:
                        task = await TaskRepository(session).get_by_task_id(task_id)
                        if task is None:
                            continue
                        engine = DispatchEngine(session)
                        if await engine.handle_ack_timeout(task):
                            await session.commit()
                except Exception as exc:   # noqa: BLE001
                    logger.warning(f"[补派] 任务 {task_id} 超时回退失败：{exc}")""",
    """                # （注入：这一段被删掉了）""",
    "ACK 超时回退没接上",
)

# ---------- 注入 7：定时循环去掉换车重派 ----------
print("\n[注入 7] 定时循环去掉换车重派（回退后没人再派）")
inject(
    CONSUMER,
    """            for task_id in pending_ids:
                try:
                    async with session_factory() as session:
                        task = await TaskRepository(session).get_by_task_id(task_id)
                        if task is None:
                            continue
                        engine = DispatchEngine(session)
                        if await engine.reassign_task(task):
                            await session.commit()
                except Exception as exc:   # noqa: BLE001
                    logger.warning(f"[补派] 任务 {task_id} 换车重派失败：{exc}")""",
    """                # （注入：这一段被删掉了）""",
    "换车重派没接上",
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
