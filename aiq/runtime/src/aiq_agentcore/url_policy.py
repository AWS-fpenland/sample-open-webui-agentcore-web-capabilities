# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""URL policy for page loading (pure functions, unit-tested).

A URL may be fetched only if it is https (or http when explicitly allowed), has a public
hostname that resolves exclusively to public unicast IPs, carries no credentials, uses a
standard port, and is not on the operator deny list. Redirect targets are re-checked with the
same policy after navigation (see nat_plugins/fetch_page.py).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain", ".home", ".lan", ".corp", ".intranet")
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata", "instance-data"}


@dataclass(frozen=True)
class UrlDecision:
    allowed: bool
    reason: str
    normalized: str | None = None
    host: str | None = None


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved
                or addr.is_unspecified or (addr.version == 6 and addr.is_site_local))


def resolve_public(host: str, resolver=None) -> tuple[bool, str]:
    """Resolve host; allowed only if every answer is a public IP (no DNS-rebinding into private space)."""
    try:
        if resolver is not None:
            answers = resolver(host)
        else:
            answers = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except (socket.gaierror, OSError) as e:
        return False, f"dns_failed:{e.__class__.__name__}"
    if not answers:
        return False, "dns_empty"
    bad = [a for a in answers if not _is_public_ip(a)]
    if bad:
        return False, f"non_public_ip:{bad[0]}"
    return True, "ok"


def evaluate_url(url: str, *, allow_http: bool = False, deny_hosts: set[str] | None = None,
                 allow_hosts: set[str] | None = None, resolver=None, check_dns: bool = True) -> UrlDecision:
    if not isinstance(url, str) or len(url) > 2048:
        return UrlDecision(False, "invalid_url")
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return UrlDecision(False, "invalid_url")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("https", "http") or (scheme == "http" and not allow_http):
        return UrlDecision(False, f"scheme_not_allowed:{scheme or 'none'}")
    if parts.username or parts.password:
        return UrlDecision(False, "credentials_in_url")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return UrlDecision(False, "no_host")
    if parts.port not in (None, 80, 443):
        return UrlDecision(False, f"port_not_allowed:{parts.port}")
    if host in _METADATA_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES) or host == "localhost":
        return UrlDecision(False, "blocked_host")
    try:
        ipaddress.ip_address(host)
        literal_ip = True
    except ValueError:
        literal_ip = False
    if literal_ip:
        return UrlDecision(False, "ip_literal_not_allowed")
    if "." not in host:
        return UrlDecision(False, "single_label_host")
    deny = {h.lower() for h in (deny_hosts or set())}
    if any(host == d or host.endswith("." + d) for d in deny):
        return UrlDecision(False, "denied_host")
    if allow_hosts:
        allow = {h.lower() for h in allow_hosts}
        if not any(host == a or host.endswith("." + a) for a in allow):
            return UrlDecision(False, "not_in_allowlist")
    if check_dns:
        ok, reason = resolve_public(host, resolver)
        if not ok:
            return UrlDecision(False, reason, host=host)
    normalized = parts._replace(fragment="").geturl()
    return UrlDecision(True, "ok", normalized=normalized, host=host)


def policy_from_env() -> dict:
    deny = {h.strip() for h in os.environ.get("AIQ_FETCH_DENY_HOSTS", "").split(",") if h.strip()}
    allow = {h.strip() for h in os.environ.get("AIQ_FETCH_ALLOW_HOSTS", "").split(",") if h.strip()}
    return {"allow_http": os.environ.get("AIQ_FETCH_ALLOW_HTTP", "false").lower() == "true",
            "deny_hosts": deny, "allow_hosts": allow or None}
