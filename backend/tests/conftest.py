"""pytest 全局配置与共享 fixture。

设计要点：
- **不依赖真实数据库**。单元测试只覆盖纯逻辑（状态机、NMS、schema 校验），
  需要数据库的部分留给 scripts/smoke_test.py 做端到端验证。
- 这样 `make test` 在任何人机器上都能跑，不需要先起 Docker。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Unit tests must not inherit approval or persistence switches from a production
# .env file. Individual tests that exercise approvals inject their own runtime.
os.environ["AGENT_REQUIRE_APPROVAL_FOR_WRITE"] = "false"
os.environ["AGENT_PERSISTENT_REPOSITORY_ENABLED"] = "false"
os.environ["AGENT_MODEL_ADAPTER_ENABLED"] = "false"
os.environ["BACKGROUND_WORKERS_ENABLED"] = "false"

# 把 backend/ 加进 sys.path，使 `import app.*` 可用
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    """注册自定义标记。"""
    config.addinivalue_line("markers", "db: 需要数据库连接的测试（默认跳过）")


@pytest.fixture(scope="session")
def project_root() -> Path:
    """项目根目录。"""
    return BACKEND_ROOT.parent
