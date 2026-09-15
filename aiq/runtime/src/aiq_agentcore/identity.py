# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Inbound identity: verify the Cognito access token and derive the Principal.

AgentCore Runtime already validates the JWT against the configured
``customJWTAuthorizer`` before the request reaches the container, and forwards
the ``Authorization`` header because the runtime's ``requestHeaderAllowlist``
includes it. We verify again here (signature, issuer, expiry, client) so that
the tenant key is derived from cryptographically verified claims even if the
container is ever reached through a different path (local dev, misconfiguration).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from functools import lru_cache

from jwt import PyJWKClient, PyJWKClientError, decode as jwt_decode
from jwt.exceptions import InvalidTokenError

from .contracts import ApiError, ErrorCode, Principal

log = logging.getLogger(__name__)


class AuthError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.error = ApiError(code=code, message=message, retryable=False)


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


@lru_cache(maxsize=4)
def _jwks_client(issuer: str) -> PyJWKClient:
    # Cognito issuer: https://cognito-idp.<region>.amazonaws.com/<pool>
    return PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=3600)


def expected_issuer() -> str:
    iss = os.environ.get("AIQ_JWT_ISSUER", "").rstrip("/")
    if not iss:
        raise RuntimeError("AIQ_JWT_ISSUER is required (Cognito issuer URL)")
    return iss


def allowed_clients() -> list[str]:
    clients = _env_list("AIQ_JWT_ALLOWED_CLIENTS")
    if not clients:
        raise RuntimeError("AIQ_JWT_ALLOWED_CLIENTS is required")
    return clients


def bearer_from_headers(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == "authorization" and isinstance(v, str) and v.lower().startswith("bearer "):
            return v.split(" ", 1)[1].strip()
    return None


def verify_access_token(token: str, *, now: float | None = None) -> Principal:
    """Verify a Cognito *access* token and return the Principal.

    Cognito access tokens carry ``client_id`` (not ``aud``) and ``token_use=access``.
    """
    if not token or token.count(".") != 2:
        raise AuthError(ErrorCode.UNAUTHENTICATED, "missing or malformed bearer token")
    issuer = expected_issuer()
    try:
        signing_key = _jwks_client(issuer).get_signing_key_from_jwt(token)
        claims = jwt_decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "iss"], "verify_aud": False},
            leeway=30,
        )
    except PyJWKClientError as e:
        raise AuthError(ErrorCode.UNAUTHENTICATED, f"token key lookup failed: {e.__class__.__name__}") from e
    except InvalidTokenError as e:
        raise AuthError(ErrorCode.UNAUTHENTICATED, f"invalid token: {e.__class__.__name__}") from e

    if claims.get("token_use") not in (None, "access"):
        raise AuthError(ErrorCode.UNAUTHENTICATED, "not an access token")
    client_id = claims.get("client_id") or (claims.get("aud") if isinstance(claims.get("aud"), str) else None)
    if client_id not in allowed_clients():
        raise AuthError(ErrorCode.FORBIDDEN, "token was not issued to an allowed client")
    exp = int(claims["exp"])
    if (now or time.time()) >= exp:
        raise AuthError(ErrorCode.UNAUTHENTICATED, "token expired")
    groups = claims.get("cognito:groups") or []
    return Principal(
        sub=str(claims["sub"]),
        issuer=issuer,
        client_id=str(client_id),
        username=claims.get("username") or claims.get("cognito:username"),
        email=claims.get("email"),
        groups=tuple(str(g) for g in groups),
        token_expires_at=exp,
    )


def principal_from_request(headers: dict[str, str] | None) -> Principal:
    token = bearer_from_headers(headers)
    if not token:
        raise AuthError(ErrorCode.UNAUTHENTICATED, "Authorization: Bearer <access token> is required")
    return verify_access_token(token)


def fetch_discovery(issuer: str) -> dict:
    """Operator diagnostic: fetch the OIDC discovery document (no caching)."""
    with urllib.request.urlopen(f"{issuer}/.well-known/openid-configuration", timeout=10) as r:  # noqa: S310
        return json.load(r)
