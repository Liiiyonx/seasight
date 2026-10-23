"""Create or update the dedicated account used by the Nexent MCP server.

Run this inside the backend container so the application database settings and
password hashing dependency are available. The password is read from
``SEASIGHT_API_PASSWORD`` and is never printed or stored in the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from passlib.context import CryptContext
from sqlalchemy import select

from app.db.session import dispose_engine, get_session_factory
from app.models.misc import User, UserRole


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--username",
        default=os.getenv("SEASIGHT_API_USERNAME", ""),
        help="service username; defaults to SEASIGHT_API_USERNAME",
    )
    parser.add_argument(
        "--role",
        choices=(UserRole.VIEWER, UserRole.OPERATOR),
        default=UserRole.VIEWER,
        help="least-privilege role for the service account (default: viewer)",
    )
    return parser.parse_args()


async def _upsert_account(username: str, password: str, role: str) -> str:
    password_hash = CryptContext(schemes=["bcrypt"], deprecated="auto").hash(
        password
    )

    async with get_session_factory()() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            session.add(
                User(
                    username=username,
                    hashed_password=password_hash,
                    full_name="Nexent MCP Service",
                    role=role,
                    is_active=True,
                )
            )
            action = "created"
        else:
            user.hashed_password = password_hash
            user.full_name = user.full_name or "Nexent MCP Service"
            user.role = role
            user.is_active = True
            action = "updated"
        await session.commit()

    return action


async def _main() -> int:
    args = _parse_args()
    username = args.username.strip()
    password = os.getenv("SEASIGHT_API_PASSWORD", "")
    if not username or not password:
        print(
            "SEASIGHT_API_USERNAME and SEASIGHT_API_PASSWORD are required",
            file=sys.stderr,
        )
        return 2

    try:
        action = await _upsert_account(username, password, args.role)
    finally:
        await dispose_engine()

    print(f"[nexent-account] {action}: username={username} role={args.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
