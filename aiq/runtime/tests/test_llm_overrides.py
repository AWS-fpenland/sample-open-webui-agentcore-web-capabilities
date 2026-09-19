# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline checks of the per-request model clients: the Mantle API key is re-read before every call (ADR-23 follow-up)."""

import pytest

from aiq_agentcore import llm_overrides as lo


@pytest.fixture
def fake_tokens(monkeypatch):
    seq = iter(["tok-1", "tok-2", "tok-3"])
    monkeypatch.setattr(lo, "mantle_token", lambda region, max_age_s=120.0: next(seq))


def test_anthropic_lane_key_is_refreshed_before_each_call(fake_tokens):
    ChatAnthropic = pytest.importorskip("langchain_anthropic").ChatAnthropic
    client = ChatAnthropic(
        model="anthropic.claude-sonnet-5", base_url="https://127.0.0.1:9/anthropic", api_key="tok-0", max_tokens=32
    )
    h = lo._ModelErrorJournal("writer", "anthropic.claude-sonnet-5", "mantle_messages")
    h.client, h.region = client, "us-east-1"
    h.on_chat_model_start({}, [[]])
    assert client._client.api_key == "tok-1" and client._async_client.api_key == "tok-1"
    h.on_llm_start({}, [])
    assert client._client.api_key == "tok-2"


def test_openai_lane_key_is_refreshed(fake_tokens):
    ChatOpenAI = pytest.importorskip("langchain_openai").ChatOpenAI
    client = ChatOpenAI(model="deepseek.v3.2", base_url="https://127.0.0.1:9/v1", api_key="tok-0", max_retries=0)
    assert lo.refresh_mantle_key(client, "mantle_chat", "us-east-1") is True
    assert client.root_client.api_key == "tok-1" and client.root_async_client.api_key == "tok-1"


def test_converse_lane_is_untouched(fake_tokens):
    class Dummy:
        api_key = "keep"

    assert lo.refresh_mantle_key(Dummy(), "converse", "us-east-1") is False


def test_refresh_failure_never_raises(monkeypatch):
    def boom(region, max_age_s=120.0):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(lo, "mantle_token", boom)
    h = lo._ModelErrorJournal("writer", "m", "mantle_messages")
    h.client, h.region = object(), "us-east-1"
    h.on_chat_model_start({}, [[]])  # logs a warning, does not raise
