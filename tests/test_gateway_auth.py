from datetime import datetime, timedelta, timezone

import pytest
from conftest import load_source_module
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

gateway = load_source_module("gateway_main_test", "services/gateway/main.py")


@pytest.mark.asyncio
async def test_gateway_validates_signature_expiry_issuer_audience_and_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "a-very-long-test-secret-that-is-at-least-32-bytes"
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_ISSUER", "aml-reference")
    monkeypatch.setenv("JWT_AUDIENCE", "aml-api")
    token = jwt.encode(
        {
            "sub": "analyst-1",
            "roles": ["analyst"],
            "iss": "aml-reference",
            "aud": "aml-api",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        secret,
        algorithm="HS256",
    )
    principal = await gateway.verify_token(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )
    assert principal.subject == "analyst-1"
    assert principal.roles == {"analyst"}

    with pytest.raises(HTTPException) as error:
        await gateway.verify_token(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token + "tampered")
        )
    assert error.value.status_code == 401
