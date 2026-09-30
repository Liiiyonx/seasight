"""Provision the quick-login accounts shown on the SeaSight login page.

This script is intentionally separate from the normal ``create_user.py`` path:
the legacy competition demo passwords are shorter than the production account
policy. It only upserts the known demo users (admin / operator / approver /
viewer) and never touches business tables or the broader seed dataset.

``approver`` exists so the human-approval demo can be played by a *second*
person: operator 发起 run，approver 批准。两个角色同一个账号会让
「高风险动作必须由第二个自然人放行」这句话在答辩时站不住。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from passlib.context import CryptContext
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_session_factory
from app.models.misc import User, UserRole


@dataclass(frozen=True)
class DemoAccount:
    username: str
    password: str
    full_name: str
    role: str
    township_scope: str | None = None


DEMO_ACCOUNTS = (
    DemoAccount("admin", "admin123456", "系统管理员", UserRole.ADMIN),
    DemoAccount(
        "operator",
        "operator123456",
        "乡镇操作员",
        UserRole.OPERATOR,
        "马鼻镇",
    ),
    # 审批员单独开一个账号，而不是让 admin 兼职审批：
    # 「高风险动作必须由第二个自然人放行」是演示里人机协同那一段的论据，
    # 用同一个账号登录批准会把这个论据讲破。approver 只有审批权，
    # 不能发起 run / 取消 run（见 core/deps.require_operator）。
    DemoAccount(
        "approver",
        "approver123456",
        "值班审批员",
        UserRole.APPROVER,
        "马鼻镇",
    ),
    DemoAccount("viewer", "viewer123456", "访客账号", UserRole.VIEWER),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or refresh the three accounts shown on the login page"
    )
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="Required when APP_ENV=production.",
    )
    return parser.parse_args()


async def _run() -> None:
    password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    async with get_session_factory()() as session:
        for account in DEMO_ACCOUNTS:
            user = (
                await session.execute(
                    select(User).where(User.username == account.username)
                )
            ).scalar_one_or_none()
            action = "updated" if user is not None else "created"
            if user is None:
                user = User(username=account.username)
                session.add(user)

            user.hashed_password = password_context.hash(account.password)
            user.full_name = account.full_name
            user.role = account.role
            user.township_scope = account.township_scope
            user.is_active = True
            print(
                f"Demo user {account.username!r} {action}; "
                f"role={account.role}, scope={account.township_scope or '-'}"
            )

        await session.commit()


def main() -> None:
    args = _parse_args()
    if settings.app_env == "production" and not args.confirm_production:
        raise SystemExit("Refusing to modify production without --confirm-production")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
