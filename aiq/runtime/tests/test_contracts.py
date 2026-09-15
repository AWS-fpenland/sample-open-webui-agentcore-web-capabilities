# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import pytest
from pydantic import ValidationError

from aiq_agentcore.contracts import DocumentRef, InvokeRequest, Op, Principal, ResearchMode, Source, new_job_id


def test_tenant_key_is_opaque_and_stable(principal):
    k1 = principal.tenant_key
    k2 = Principal(**{**principal.model_dump(), "email": "x@example.invalid"}).tenant_key
    assert k1 == k2 and k1.startswith("u_") and principal.sub not in k1


def test_tenant_key_binds_issuer(principal):
    other = Principal(**{**principal.model_dump(), "issuer": "https://other.invalid"})
    assert other.tenant_key != principal.tenant_key


def test_invoke_request_rejects_owner_fields():
    with pytest.raises(ValidationError):
        InvokeRequest.model_validate({"op": "chat", "tenant_key": "u_evil"})
    with pytest.raises(ValidationError):
        InvokeRequest.model_validate({"op": "events", "job_id": "job_notahex"})


def test_invoke_request_defaults():
    r = InvokeRequest.model_validate({"op": "chat", "messages": [{"role": "user", "content": "hi"}]})
    assert r.mode is ResearchMode.AUTO and r.after == 0 and r.tail is True and r.op is Op.CHAT


def test_document_name_sanitised():
    d = DocumentRef(name="../../etc/passwd", content_type="text/plain")
    assert d.name == "passwd"
    with pytest.raises(ValidationError):
        DocumentRef(name="..", content_type="text/plain")


def test_job_id_deterministic_with_seed():
    assert new_job_id("t|req-1") == new_job_id("t|req-1")
    assert new_job_id("t|req-1") != new_job_id("t|req-2")
    assert new_job_id() != new_job_id()


def test_source_id_from_url():
    s = Source.make_id("https://example.com/a")
    assert s.startswith("src_") and len(s) == 20
