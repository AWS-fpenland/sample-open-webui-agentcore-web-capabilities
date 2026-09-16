# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import pytest

from aiq_agentcore.url_policy import evaluate_url, resolve_public

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731
PRIVATE = lambda host: ["10.0.0.5"]  # noqa: E731
MIXED = lambda host: ["93.184.216.34", "169.254.169.254"]  # noqa: E731


@pytest.mark.parametrize("url,reason", [
    ("ftp://example.com/x", "scheme_not_allowed:ftp"),
    ("http://example.com/x", "scheme_not_allowed:http"),
    ("https://user:pw@example.com/x", "credentials_in_url"),
    ("https://example.com:8443/x", "port_not_allowed:8443"),
    ("https://169.254.169.254/latest/meta-data/", "blocked_host"),
    ("https://10.0.0.1/", "ip_literal_not_allowed"),
    ("https://[::1]/", "ip_literal_not_allowed"),
    ("https://localhost/", "blocked_host"),
    ("https://intranet/", "single_label_host"),
    ("https://svc.internal/", "blocked_host"),
    ("not a url", "scheme_not_allowed:none"),
])
def test_rejects(url, reason):
    d = evaluate_url(url, resolver=PUBLIC)
    assert not d.allowed and d.reason == reason


def test_dns_must_resolve_public_only():
    assert evaluate_url("https://example.com/a", resolver=PUBLIC).allowed
    assert evaluate_url("https://example.com/a", resolver=PRIVATE).reason.startswith("non_public_ip")
    assert evaluate_url("https://example.com/a", resolver=MIXED).reason.startswith("non_public_ip")
    ok, reason = resolve_public("example.com", resolver=lambda h: [])
    assert not ok and reason == "dns_empty"


def test_allow_deny_lists_and_normalisation():
    assert evaluate_url("https://docs.aws.amazon.com/x#frag", resolver=PUBLIC).normalized == "https://docs.aws.amazon.com/x"
    assert evaluate_url("https://bad.example/x", resolver=PUBLIC, deny_hosts={"bad.example"}).reason == "denied_host"
    assert evaluate_url("https://sub.bad.example/x", resolver=PUBLIC, deny_hosts={"bad.example"}).reason == "denied_host"
    assert evaluate_url("https://other.example/x", resolver=PUBLIC, allow_hosts={"aws.amazon.com"}).reason == "not_in_allowlist"
    assert evaluate_url("https://docs.aws.amazon.com/x", resolver=PUBLIC, allow_hosts={"aws.amazon.com"}).allowed
    assert evaluate_url("http://example.com/x", resolver=PUBLIC, allow_http=True).allowed
