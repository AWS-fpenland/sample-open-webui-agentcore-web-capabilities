"""Authenticate service and OWUI-signed identity; never trust plain user headers."""

import hmac
from dataclasses import dataclass

import jwt


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    subject: str
    role: str


class Authenticator:
    def __init__(self, service_key: str, identity_secret: str):
        if len(service_key.encode()) < 32 or len(identity_secret.encode()) < 32:
            raise ValueError("Separate service and identity secrets must each be at least 32 bytes")
        if hmac.compare_digest(service_key, identity_secret):
            raise ValueError("Service and identity secrets must be different")
        self.service_key = service_key
        self.identity_secret = identity_secret

    def authenticate(self, authorization: str, identity_token: str) -> Identity:
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + self.service_key).encode()):
            raise AuthenticationError("Authentication required")
        if len(identity_token) > 4096:
            raise AuthenticationError("Authentication required")
        try:
            claims = jwt.decode(
                identity_token,
                self.identity_secret,
                algorithms=["HS256"],
                issuer="open-webui",
                options={"require": ["sub", "iss", "iat", "exp", "role"]},
                leeway=5,
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not 1 <= len(subject) <= 256:
                raise ValueError()
            if claims["role"] not in ("user", "admin"):
                raise ValueError()
            if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
                raise ValueError()
            if not 0 < claims["exp"] - claims["iat"] <= 300:
                raise ValueError()
            return Identity(subject=subject, role=claims["role"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthenticationError("Authentication required") from None
