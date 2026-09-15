import time

import jwt
import pytest

from web_capabilities.auth import AuthenticationError, Authenticator


SERVICE_KEY = "synthetic-service-key-" + "a" * 32
IDENTITY_SECRET = "synthetic-identity-secret-" + "b" * 32


def token(**updates):
    now = int(time.time())
    claims = {"sub": "synthetic-user", "role": "user", "iss": "open-webui", "iat": now, "exp": now + 300}
    claims.update(updates)
    return jwt.encode(claims, IDENTITY_SECRET, algorithm="HS256")


def test_verified_owui_identity_not_a_cognito_token():
    principal = Authenticator(SERVICE_KEY, IDENTITY_SECRET).authenticate("Bearer " + SERVICE_KEY, token())
    assert principal.subject == "synthetic-user"
    assert principal.role == "user"


@pytest.mark.parametrize("updates", [
    {"iss": "attacker"}, {"sub": ""}, {"role": "pending"},
    {"exp": 1}, {"exp": int(time.time()) + 1000},
    {"iat": True}, {"sub": None}, {"iat": int(time.time()) + 100},
])
def test_rejects_invalid_claims(updates):
    with pytest.raises(AuthenticationError):
        Authenticator(SERVICE_KEY, IDENTITY_SECRET).authenticate("Bearer " + SERVICE_KEY, token(**updates))


@pytest.mark.parametrize("authorization,identity", [
    ("", ""), ("Bearer wrong", token()), ("Bearer " + SERVICE_KEY, ""),
    ("Bearer " + SERVICE_KEY, "plain-user-id"),
    ("Bearer " + SERVICE_KEY, jwt.encode({"sub": "synthetic-user"}, "wrong-secret-" * 4, algorithm="HS256")),
])
def test_both_service_key_and_signed_identity_required(authorization, identity):
    with pytest.raises(AuthenticationError):
        Authenticator(SERVICE_KEY, IDENTITY_SECRET).authenticate(authorization, identity)


def test_rejects_short_or_shared_secrets():
    with pytest.raises(ValueError):
        Authenticator("short", IDENTITY_SECRET)
    with pytest.raises(ValueError):
        Authenticator(SERVICE_KEY, SERVICE_KEY)
