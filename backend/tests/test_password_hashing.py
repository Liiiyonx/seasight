"""Password hashing must work with the pinned authentication dependencies."""

from passlib.context import CryptContext


def test_bcrypt_password_hash_round_trip() -> None:
    context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    password_hash = context.hash("seasight-test-password")

    assert context.verify("seasight-test-password", password_hash)
