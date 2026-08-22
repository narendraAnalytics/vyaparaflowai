"""Password/refresh-secret hashing (argon2) and access-token JWT encode/
decode. Roles are deliberately NOT embedded in the JWT — see the Phase 2
foundation design doc: permissions are re-checked from the DB on every
request so a role change or deactivation takes effect immediately instead
of waiting out a token's lifetime.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()


def hash_secret(secret: str) -> str:
    return _ph.hash(secret)


def verify_secret(secret: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, secret)
    except VerifyMismatchError:
        return False


def create_access_token(
    *, user_id: uuid.UUID, org_id: uuid.UUID, secret: str, expires_minutes: int
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict[str, Any]:
    return jwt.decode(token, secret, algorithms=["HS256"])


def generate_refresh_secret() -> str:
    return secrets.token_urlsafe(32)
