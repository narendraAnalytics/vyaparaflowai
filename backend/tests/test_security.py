import uuid

import jwt
import pytest

from app.core.security import (
    create_access_token,
    decode_token,
    generate_refresh_secret,
    hash_secret,
    verify_secret,
)


def test_hash_and_verify_secret_roundtrip():
    hashed = hash_secret("correct horse battery staple")
    assert verify_secret("correct horse battery staple", hashed)
    assert not verify_secret("wrong password", hashed)


def test_hash_is_not_the_plaintext():
    hashed = hash_secret("hello")
    assert hashed != "hello"


def test_generate_refresh_secret_is_unique_and_urlsafe():
    a, b = generate_refresh_secret(), generate_refresh_secret()
    assert a != b
    assert len(a) > 20


def test_access_token_roundtrip():
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    token = create_access_token(
        user_id=user_id, org_id=org_id, secret="test-secret", expires_minutes=15
    )
    payload = decode_token(token, "test-secret")
    assert payload["sub"] == str(user_id)
    assert payload["org_id"] == str(org_id)
    assert payload["type"] == "access"


def test_access_token_wrong_secret_rejected():
    token = create_access_token(
        user_id=uuid.uuid4(), org_id=uuid.uuid4(), secret="right", expires_minutes=15
    )
    with pytest.raises(jwt.PyJWTError):
        decode_token(token, "wrong")


def test_expired_access_token_rejected():
    token = create_access_token(
        user_id=uuid.uuid4(), org_id=uuid.uuid4(), secret="s", expires_minutes=-1
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token, "s")
