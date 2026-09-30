#!/usr/bin/env python
"""WP-05/WP-12 Agent 评测系统 —— 一键执行入口。

用法（仓库根目录）：
    .\\.venv-analysis\\Scripts\\python.exe scripts\\run_agent_evals.py
    .\\.venv-analysis\\Scripts\\python.exe scripts\\run_agent_evals.py --output artifacts\\agent_evals\\latest_v2.json

做什么：
    1. 按固定场景集（backend/tests/agent_evals/scenarios.py）逐个执行：
       内存仓储 / SQLite 内存 / 假时钟 / 假模型客户端 / 内存 device transport，
       不连数据库 / Redis / MQTT / 外网 / 模型 / 设备。
    2. 统计 13 项聚合指标：六项 WP-05 指标 + 七项 WP-12 指标
       （重启恢复率 / 幂等冲突率 / 模型回退率 / Schema 拒绝率 /
        轨迹回放匹配率 / 审批交接成功率 / 设备故障恢复率）。
    3. 输出 JSON 到 artifacts/agent_evals/latest_v2.json（同时写带时间戳
       历史），v1 产物 latest.json 保持原样不覆盖；每次输出包含代码版本
       （git HEAD 短哈希 + 内容指纹）与配置哈希。
    4. 依赖 WP-10/WP-11/WP-14 接口缺失的场景明确跳过（skipped=true +
       skip_reason），不计入指标分母，不伪造通过。
    5. 产出前用 backend/tests/agent_evals/contract.py 自校验 schema 2.0。

退出码：
    0 = 评测完成且 latest_v2.json 已写出并通过 schema 校验
    1 = 未预期异常
    2 = 报告未通过 schema 自校验（脚本/契约配置缺陷，属开发期缺陷）

证据纪律（总控 E0-E4）：本评测全部为 E1（内部实现 / 确定性仿真证据），
报告 evidence_level = E1，禁止写成 E3/E4；声明 network/model/device 零访问。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台默认 GBK：统一 UTF-8 输出（只修编码，不改变语义）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
EVAL_DIR = BACKEND / "tests" / "agent_evals"
for _p in (str(BACKEND), str(EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 禁止 import 后修改：backend/app/** 只读（见消息约束）；此处只读使用内核
from app.services.agents import ERROR_CODES, TERMINAL_STATUSES  # noqa: E402
import contract  # noqa: E402
import metrics as metrics_mod  # noqa: E402
import scenarios as scenarios_mod  # noqa: E402

KERNEL_GLOB = "backend/app/services/agents/*.py"
DEFAULT_OUTPUT = "artifacts/agent_evals/latest_v2.json"


def git_head_short_hash(root: Path) -> str | None:
    """直接读 .git（不调用 git 子进程，Windows 下更稳），返回短哈希。"""
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref: "):
        try:
            full = (git_dir / head[5:].strip()).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    else:
        full = head
    full = full.strip()
    return full[:7] if full else None


def hash_files(root: Path, rel_files: list[str]) -> str:
    """对文件集合求确定性 sha256（按相对路径排序，路径与内容都参与）。"""
    hasher = hashlib.sha256()
    for rel in sorted(rel_files):
        path = root / rel
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        if path.is_file():
            hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def collect_kernel_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in (root / "backend" / "app" / "services" / "agents").glob("*.py"))


def collect_eval_files(root: Path) -> list[str]:
    files = [
        str(p.relative_to(root)).replace("\\", "/")
        for p in (root / "backend" / "tests" / "agent_evals").glob("*.py")
    ]
    files.append("scripts/run_agent_evals.py")
    return sorted(files)


def build_report(*, command: str) -> dict:
    """执行全部固定场景并组装评测报告（一次调用即完成全部工作）。"""
    specs = scenarios_mod.SCENARIOS
    results = [scenarios_mod.run_scenario(sc["id"]) for sc in specs]

    aggregated = metrics_mod.compute_metrics(results)

    kernel_files = collect_kernel_files(ROOT)
    eval_files = collect_eval_files(ROOT)
    head = git_head_short_hash(ROOT)
    fingerprint = hash_files(ROOT, kernel_files + eval_files)
    config_hash = hash_files(ROOT, eval_files)

    skipped_entries: list[str] = []
    scenario_entries = []
    for sc, res in zip(specs, results):
        is_skipped = bool(res.skipped)
        if is_skipped:
            skipped_entries.append(sc["id"])
        scenario_entries.append(
            {
                "id": sc["id"],
                "name": sc["name"],
                "description": sc["description"],
                "evidence_level": sc.get("evidence_level", "E1"),
                "expected": {
                    "status": sc["expected"]["status"],
                    "error_code": sc["expected"]["error_code"],
                    "shape": sc["expected"]["shape"],
                },
                "passed": res.passed,
                "skipped": is_skipped,
                "skip_reason": res.skip_reason,
                "actual": {
                    "status": res.actual_status,
                    "error_code": res.actual_error_code,
                    "termination_reason": res.actual_termination_reason,
                    "step_count": 0 if is_skipped else len(res.steps),
                    "step_types": [] if is_skipped else res.step_types,
                    "tool_executions": {} if is_skipped else res.tool_executions,
                    "decision_latency_ms": (
                        None if is_skipped else round(res.decision_latency_ms, 3)
                    ),
                },
                "notes": list(res.notes),
            }
        )

    executed = [res for res in results if not res.skipped]
    report = {
        "schema_version": contract.REPORT_SCHEMA_VERSION,
        "report_type": contract.REPORT_TYPE,
        "command": command,
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "code_version": {
            "source": "git" if head else "no-git",
            "value": head or "unknown",
            "fingerprint": fingerprint,
            "note": (
                "git HEAD 短哈希（直接读 .git）；fingerprint = sha256(内核文件 + 评测文件内容)。"
                "无 .git 时 value=unknown，指纹仍覆盖全部评测相关文件。"
            ),
        },
        "config": {
            "hash": config_hash,
            "scenario_count": len(specs),
            "scenario_ids": [sc["id"] for sc in specs],
            "files": eval_files,
            "kernel_files": kernel_files,
        },
        "evidence_level": "E1",
        "environment": {
            "network_access": False,
            "model_access": False,
            "device_access": False,
            "runtime_backend": "in-memory",
            "persistent_backend": "sqlite-memory (in-process, StaticPool)",
            "device_twin": "edge.device_sim (in-memory transport)",
            "model_client": "fake (injected, no public network)",
        },
        "sample_size": len(executed),
        "skipped_count": len(skipped_entries),
        "scenarios": scenario_entries,
        "metrics": aggregated,
        "wave3_skipped": skipped_entries,
    }
    return report


def write_report(report: dict, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    output.write_text(payload, encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    history = output.parent / f"eval_{stamp}.json"
    history.write_text(payload, encoding="utf-8")
    return history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WP-05/WP-12 Agent 评测系统（确定性离线评测）")
    parser.add_argument("--output", default=str(ROOT / DEFAULT_OUTPUT))
    args = parser.parse_args(argv)

    output = Path(args.output)
    command = f"{sys.executable} {' '.join(sys.argv)}"

    print("=" * 70)
    print("探海灵眸 SeaSight — WP-05/WP-12 Agent 评测系统（E1 确定性离线评测）")
    print("=" * 70)
    head = git_head_short_hash(ROOT)
    print(f"代码版本：git HEAD = {head or 'unknown'}")
    print(f"固定场景数：{len(scenarios_mod.SCENARIOS)}（WP-05 13 + WP-12 10 + 第四波 4）")
    print("评测模式：内存仓储 + SQLite 内存 + 假时钟 + 假模型客户端 + 内存 device transport")
    print("零访问声明：无模型 / 无网络 / 无真实设备\n")

    try:
        report = build_report(command=command)
    except Exception as exc:  # noqa: BLE001 —— 评测异常统一上报，不让脚本裸崩
        print(f"\n[评测失败] 未预期异常：{exc!r}")
        return 1

    errors = contract.validate_latest_json(report)
    if errors:
        print("\n[契约缺陷] 报告未通过 schema 自校验：")
        for e in errors:
            print(f"  - {e}")
        return 2

    try:
        history = write_report(report, output)
    except OSError as exc:
        print(f"\n[写出失败] 无法写入 {output}：{exc}")
        return 1

    # ---- 摘要输出 ----
    print("-" * 70)
    executed = [sc for sc in report["scenarios"] if not sc["skipped"]]
    passed = sum(1 for sc in executed if sc["passed"])
    print(f"通过 {passed}/{len(executed)} 个已执行场景（跳过 {report['skipped_count']} 个）")
    for sc in report["scenarios"]:
        if sc["skipped"]:
            print(f"  [SKIP] {sc['id']:<26} → {sc['skip_reason']}")
            continue
        mark = "PASS" if sc["passed"] else "FAIL"
        print(f"  [{mark}] {sc['id']:<26} → {sc['actual']['status']} "
              f"({sc['actual']['error_code'] or '-'})")
        for note in sc["notes"]:
            print(f"        ! {note}")

    print("-" * 70)
    m = report["metrics"]
    print("十三项聚合指标：")
    print(f"  WP-05  成功率             success_rate            = {m['success_rate']}")
    print(f"  WP-05  策略违规率         policy_violation_rate   = {m['policy_violation_rate']}")
    print(f"  WP-05  工具调用正确率     tool_correct_rate       = {m['tool_correct_rate']}")
    print(f"  WP-05  无效循环率         invalid_loop_rate       = {m['invalid_loop_rate']}")
    print(f"  WP-05  恢复成功率         recovery_success_rate   = {m['recovery_success_rate']}")
    print(f"  WP-05  P95 决策耗时       p95_decision_latency_ms = {m['p95_decision_latency_ms']}")
    print(f"  WP-12  重启恢复率         restart_recovery_rate   = {m['restart_recovery_rate']}")
    print(f"  WP-12  幂等冲突率         idempotency_conflict_rate = {m['idempotency_conflict_rate']}")
    print(f"  WP-12  模型回退率         model_fallback_rate     = {m['model_fallback_rate']}")
    print(f"  WP-12  Schema 拒绝率      model_schema_rejection_rate = {m['model_schema_rejection_rate']}")
    print(f"  WP-12  轨迹回放匹配率     trace_replay_match_rate = {m['trace_replay_match_rate']}")
    print(f"  WP-12  审批交接成功率     approval_handoff_success_rate = {m['approval_handoff_success_rate']}")
    print(f"  WP-12  设备故障恢复率     device_fault_recovery_rate = {m['device_fault_recovery_rate']}")
    print(f"  （说明）业务成功率        business_success_rate    = {m['business_success_rate']}")
    print("-" * 70)
    print(f"产物：{output}")
    print(f"历史：{history}")
    print(f"evidence_level=E1 · 退出码 0=完成（跳过场景需查看 skip_reason）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
