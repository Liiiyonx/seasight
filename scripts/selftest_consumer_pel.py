"""自证：把「PEL 回收」的缺陷注入回去，确认测试真的会红。

注入清单：
  1. 主循环去掉 PEL 认领（回到原始缺陷：消息永久滞留）
  2. _reclaim_pending 直接返回空（不认领，形同虚设）
  3. 认领用错 API（xreadgroup 代替 xautoclaim —— 读不到 PEL）
  4. min_idle_ms 设为 0（会把「正在处理」的消息也抢回来重复处理）
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

CONSUMER = BACKEND / "app/services/consumer.py"
TEST_FILE = "tests/test_consumer_pel.py"

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
print("自证：注入「PEL 回收」缺陷，确认测试会红")
print("=" * 66)

print("\n[基线] 未注入任何缺陷")
code, out = run_tests()
if code == 0:
    print("  全绿 ✓（可以开始注入）")
else:
    print("  基线就是红的，先修好再自证：")
    print(out[-2000:])
    sys.exit(1)

# ---------- 注入 1：主循环去掉 PEL 认领 ----------
print("\n[注入 1] 主循环去掉 PEL 认领（原始缺陷）")
inject(
    CONSUMER,
    """            for entry_id, fields in await _reclaim_pending(
                redis, stream, group, consumer_name
            ):
                await _process_entry(
                    redis, entry_id, fields, stream, group,
                    DispatchEngine, EventRepository, EventStatus,
                )""",
    """            # （注入：PEL 认领被删掉了）""",
    "主循环没接 PEL 认领",
)

# ---------- 注入 2：认领函数直接返回空 ----------
print("\n[注入 2] _reclaim_pending 直接返回空（不认领）")
inject(
    CONSUMER,
    """    result = await redis.xautoclaim(
        stream, group, consumer_name, min_idle_ms, count=10
    )""",
    """    return []   # （注入：不认领了）
    result = await redis.xautoclaim(
        stream, group, consumer_name, min_idle_ms, count=10
    )""",
    "认领函数形同虚设",
)

# ---------- 注入 3：用错 API ----------
print("\n[注入 3] 认领用 xreadgroup 代替 xautoclaim")
inject(
    CONSUMER,
    """    result = await redis.xautoclaim(
        stream, group, consumer_name, min_idle_ms, count=10
    )""",
    """    result = await redis.xreadgroup(
        group, consumer_name, {stream: ">"}, count=10
    )""",
    "用错 API（读不到 PEL）",
)

# ---------- 注入 4：min_idle 设为 0 ----------
print("\n[注入 4] PEL_MIN_IDLE_MS 设为 0（抢回正在处理的消息）")
inject(
    CONSUMER,
    "PEL_MIN_IDLE_MS = 60_000",
    "PEL_MIN_IDLE_MS = 0",
    "空闲阈值为 0（重复处理）",
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
