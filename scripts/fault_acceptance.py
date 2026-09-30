#!/usr/bin/env python3
"""SeaSight WP-17 受控故障演练包（E1，一键复现既有确定性用例）。

不是新的「真实故障」证据，而是把已实现的确定性恢复/降级用例做成
可一键复现的 E1 证据包：用 subprocess 逐组调用 pytest，并用
--junitxml 结构化解析结果（不靠字符串猜测成功）。

分组：
  G1  health-degradation        健康检查真实降级语义
  G2  ack-startup-recovery      ACK 判重水位启动恢复（降级/幂等/超时/可观测/真实库）
  G3  ack-twin-loopback         设备孪生 ACK 闭环 + 重启重放幂等（真实 scratch 库）
  G4  ack-persistence-api        ACK 持久化/API 幂等（mqtt_ack + task_ack_api +
                                task_ack_persistence 稳定子集）

可选 live probe：设置 SEASIGHT_BACKEND_URL 后，对运行中的后端 GET /health
做只读观测（真实 status / dependencies / ack_recovery）。live probe 与内部
测试组分开记录，不混为同一证据来源。

证据上限（如实声明）：本包是受控测试（进程内注入 / 内存 transport /
scratch PostgreSQL），不代表真实 broker、公网、硬件或现场故障。

用法：
    $env:SEASIGHT_BACKEND_URL='http://127.0.0.1:8001'   # 可选
    .venv-analysis/Scripts/python.exe scripts/fault_acceptance.py

退出码：所有内部测试组通过 = 0；任一失败 = 1。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "artifacts" / "fault-acceptance"
TMP_DIR = OUT_DIR / "tmp"

DEFAULT_PYTHON = ROOT / ".venv-analysis" / "Scripts" / "python.exe"

GROUPS = [
    {
        "name": "health-degradation",
        "files": ["backend/tests/test_health_degradation.py"],
        "note": "健康检查真实降级语义：dependencies 明细、status 不盲目 ok、全挂 down",
    },
    {
        "name": "ack-startup-recovery",
        "files": ["backend/tests/test_ack_startup_recovery.py"],
        "note": "ACK 判重水位启动恢复：降级不失败 / 幂等 / 超时上界 / 可观测 / 真实 scratch 库",
    },
    {
        "name": "ack-twin-loopback",
        "files": ["backend/tests/test_ack_twin_loopback.py"],
        "note": "设备孪生 ACK 闭环：孪生 ACK -> 平台消费 -> 推进+落账 -> 重启重建 -> 重放不重复推进",
    },
    {
        "name": "ack-persistence-api-idempotency",
        "files": [
            "backend/tests/test_mqtt_ack.py",
            "backend/tests/test_task_ack_api.py",
            "backend/tests/test_task_ack_persistence.py",
        ],
        "note": "ACK 持久化 / 审计查询 / API 幂等（稳定子集）",
    },
]


def utf8_streams() -> None:
    """Windows 控制台统一 UTF-8（执行手册全局硬约束）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def run_pytest_group(group: dict, python: Path, idx: int) -> dict:
    """在仓库根目录调用 pytest；根目录方式无法收集时修正为 backend/ 内运行。"""
    xml_path = TMP_DIR / f"group{idx}.xml"
    files = group["files"]

    def build_cmd(cwd: Path, rel_files: list[str]) -> list[str]:
        return [
            str(python),
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "--tb=short",
            "--junitxml",
            str(xml_path),
            *rel_files,
        ]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    attempts: list[dict] = []
    # 第一次尝试：仓库根目录（文档规定的执行位置）
    cmd = build_cmd(ROOT, files)
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=600,
    )
    attempts.append({"cwd": str(ROOT), "command": " ".join(cmd)})

    # 修正规则：若根目录方式「没有收集到用例」或「未产出 junitxml」，
    # 视为该组在根目录不可用，改用 backend/ 目录内运行（不删除该组）。
    parsed = parse_junitxml(xml_path)
    need_retry = parsed is None or (proc.returncode != 0 and parsed["tests"] == 0)
    if need_retry:
        backend_files = [p.removeprefix("backend/") for p in files]
        cmd2 = build_cmd(ROOT / "backend", backend_files)
        proc = subprocess.run(
            cmd2,
            cwd=ROOT / "backend",
            env=env,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=600,
        )
        attempts.append({"cwd": str(ROOT / "backend"), "command": " ".join(cmd2)})
        parsed = parse_junitxml(xml_path)

    if parsed is None:
        parsed = {
            "parse_failed": True,
            "tests": 0,
            "passed": 0,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "failed_names": [],
        }

    passed = parsed["tests"] - parsed["failures"] - parsed["errors"]
    parsed["passed"] = passed
    group_ok = (
        proc.returncode == 0
        and parsed.get("parse_failed") is not True
        and parsed["tests"] > 0
        and parsed["failures"] == 0
        and parsed["errors"] == 0
    )

    return {
        "name": group["name"],
        "note": group["note"],
        "command": attempts[-1]["command"],
        "cwd": attempts[-1]["cwd"],
        "attempts": attempts,
        "returncode": proc.returncode,
        "parsed_summary": parsed,
        "passed": group_ok,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def parse_junitxml(xml_path: Path) -> dict | None:
    """解析 pytest 的 --junitxml 输出；无法解析返回 None（不靠字符串猜）。

    pytest 的 junitxml 根节点是 <testsuites>，统计属性挂在 <testsuite> 子节点上，
    所以对 <testsuite> 汇总，testcase 一律用 iter() 遍历。
    """
    if not xml_path.exists():
        return None
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return None

    def _int(value: str | None) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    tests = failures = errors = skipped = 0
    for suite in root.iter("testsuite"):
        tests += _int(suite.attrib.get("tests"))
        failures += _int(suite.attrib.get("failures"))
        errors += _int(suite.attrib.get("errors"))
        skipped += _int(suite.attrib.get("skipped"))

    failed_names: list[str] = []
    for tc in root.iter("testcase"):
        for child in tc:
            if child.tag in ("failure", "error"):
                failed_names.append(f"{tc.attrib.get('classname', '')}::{tc.attrib.get('name', '')}")

    return {
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "failed_names": failed_names,
    }


def live_probe() -> dict | None:
    """可选：对运行中的后端 GET /health 做只读观测。字段缺失如实记录。"""
    base = os.environ.get("SEASIGHT_BACKEND_URL")
    if not base:
        return None
    url = base.rstrip("/") + "/health"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        return {
            "url": url,
            "http_status": resp.status,
            "evidence_level": "E1",
            "status": data.get("status"),
            "dependencies": data.get("dependencies"),
            "ack_recovery": data.get("ack_recovery"),
            "ack_recovery_field_present": "ack_recovery" in data,
            "note": "live probe 是对当前运行实例 /health 的只读观测；"
            "若 ack_recovery 字段缺失（旧进程），如实记录为缺失，不伪造。",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "url": url,
            "evidence_level": "E1",
            "error": f"{type(exc).__name__}: {exc}",
            "note": "live probe 观测失败，仅记录，不参与内部测试组判定。",
        }


def main() -> int:
    utf8_streams()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    python = Path(os.environ.get("SEASIGHT_PYTHON", str(DEFAULT_PYTHON)))
    if not python.exists():
        print(f"[预检] 找不到 Python：{python}（可用 SEASIGHT_PYTHON 覆盖）", file=sys.stderr)
        return 1

    groups = []
    print("=" * 72)
    print("SeaSight WP-17 受控故障演练（E1）—— 逐组复跑确定性降级/恢复用例")
    print(f"runner python: {python}")
    print("=" * 72)

    for idx, group in enumerate(GROUPS, start=1):
        print(f"\n[G{idx}] {group['name']}")
        print(f"    {group['note']}")
        result = run_pytest_group(group, python, idx)
        groups.append(result)
        s = result["parsed_summary"]
        if result["passed"]:
            print(
                f"    [PASS] {s['tests']} tests, "
                f"{s['passed']} passed, {s['skipped']} skipped, "
                f"{s['failures']} failures, {s['errors']} errors  "
                f"(returncode={result['returncode']})"
            )
        else:
            print(
                f"    [FAIL] {s['tests']} tests, "
                f"{s['passed']} passed, {s['skipped']} skipped, "
                f"{s['failures']} failures, {s['errors']} errors  "
                f"(returncode={result['returncode']})"
            )
            for name in s.get("failed_names", []):
                print(f"        - {name}")
            print("    command:", result["command"])
            if result["stderr_tail"].strip():
                print("    stderr tail:")
                for line in result["stderr_tail"].splitlines()[-8:]:
                    print(f"        {line}")

    # 可选 live probe（与内部测试组分开，不混为同一证据）
    probe = live_probe()
    if probe is not None:
        print("\n[live-probe] GET /health（只读观测，独立记录）")
        print(f"    {json.dumps(probe, ensure_ascii=False, indent=4)}")
    else:
        print("\n[live-probe] 未设置 SEASIGHT_BACKEND_URL，跳过（live_probe=null）")

    passed_groups = sum(1 for g in groups if g["passed"])
    total_tests = sum(g["parsed_summary"]["tests"] for g in groups)
    total_passed = sum(g["parsed_summary"]["passed"] for g in groups)
    total_skipped = sum(g["parsed_summary"]["skipped"] for g in groups)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_level": "E1",
        "runner": "scripts/fault_acceptance.py",
        "python": str(python),
        "groups": [
            {
                "name": g["name"],
                "command": g["command"],
                "cwd": g["cwd"],
                "returncode": g["returncode"],
                "parsed_summary": g["parsed_summary"],
                "passed": g["passed"],
            }
            for g in groups
        ],
        "live_probe": probe,
        "scope_note": "受控故障注入/确定性恢复测试（E1）：逐组复跑既有确定性用例，"
        "全部为进程内注入 / 内存 transport / scratch PostgreSQL，不代表真实 broker、"
        "公网、硬件或现场故障；live_probe 仅为当前运行实例 /health 的只读观测。",
        "summary": {
            "groups_total": len(groups),
            "groups_passed": passed_groups,
            "tests_total": total_tests,
            "tests_passed": total_passed,
            "tests_skipped": total_skipped,
        },
    }

    report_file = OUT_DIR / "latest.json"
    report_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n" + "=" * 72)
    print(f"内部测试组：{passed_groups}/{len(groups)} 通过；"
          f"用例 {total_passed}/{total_tests} 通过（skipped {total_skipped}）")
    print(f"证据文件：{report_file}")
    print("=" * 72)

    return 0 if passed_groups == len(groups) else 1


if __name__ == "__main__":
    sys.exit(main())
