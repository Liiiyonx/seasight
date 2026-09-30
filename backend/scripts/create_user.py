"""Create or update a SeaSight account without using demo seed data."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from passlib.context import CryptContext
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.misc import User, UserRole


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or update a SeaSight user")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=UserRole.ALL)
    parser.add_argument("--full-name", default=None)
    parser.add_argument("--township-scope", default=None)
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin instead of prompting.",
    )
    parser.add_argument(
        "--inactive",
        action="store_true",
        help="Create the account in disabled state.",
    )
    return parser.parse_args()


def _read_password(use_stdin: bool) -> str:
    if use_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters")
    return password


async def _run(args: argparse.Namespace, password: str) -> None:
    password_hash = CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password)
    async with get_session_factory()() as session:
        user = (
            await session.execute(select(User).where(User.username == args.username))
        ).scalar_one_or_none()
        action = "updated" if user is not None else "created"
        if user is None:
            user = User(username=args.username)
            session.add(user)
        user.hashed_password = password_hash
        user.full_name = args.full_name
        user.role = args.role
        user.township_scope = args.township_scope
        user.is_active = not args.inactive
        await session.commit()
    print(f"User {args.username!r} {action}; role={args.role}")


def main() -> None:
    args = _parse_args()
    password = _read_password(args.password_stdin)
    asyncio.run(_run(args, password))


if __name__ == "__main__":
    main()
