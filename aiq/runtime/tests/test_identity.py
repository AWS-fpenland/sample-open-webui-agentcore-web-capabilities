# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aiq_agentcore import identity
from aiq_agentcore.contracts import ErrorCode

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"


@pytest.fixture
def keypair(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    class FakeSigningKey:
        def __init__(self):
            self.key = key.public_key()

    class FakeJwks:
        def get_signing_key_from_jwt(self, token):
            return FakeSigningKey()

    monkeypatch.setattr(identity, "_jwks_client", lambda issuer: FakeJwks())
    return pem


def mint(pem, **claims):
    now = int(time.time())
    payload = {"sub": "abc-123", "iss": ISSUER, "client_id": "client-a", "token_use": "access", "iat": now,
               "exp": now + 3600, "username": "user-a", "cognito:groups": ["admin"]}
    payload.update(claims)
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": "k1"})


def test_valid_access_token(keypair):
    p = identity.verify_access_token(mint(keypair))
    assert p.sub == "abc-123" and p.client_id == "client-a" and p.groups == ("admin",)


def test_wrong_client_rejected(keypair):
    with pytest.raises(identity.AuthError) as ei:
        identity.verify_access_token(mint(keypair, client_id="evil"))
    assert ei.value.error.code is ErrorCode.FORBIDDEN


def test_expired_rejected(keypair):
    with pytest.raises(identity.AuthError) as ei:
        identity.verify_access_token(mint(keypair, exp=int(time.time()) - 120))
    assert ei.value.error.code is ErrorCode.UNAUTHENTICATED


def test_wrong_issuer_rejected(keypair):
    with pytest.raises(identity.AuthError):
        identity.verify_access_token(mint(keypair, iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"))


def test_id_token_rejected(keypair):
    with pytest.raises(identity.AuthError):
        identity.verify_access_token(mint(keypair, token_use="id"))


def test_missing_bearer():
    with pytest.raises(identity.AuthError):
        identity.principal_from_request({"authorization": "Basic abc"})
    with pytest.raises(identity.AuthError):
        identity.principal_from_request({})
