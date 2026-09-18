# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Session-adapter tests for the AgentCore Code Interpreter sandbox provider (fake boto3 client, no AWS)."""
import base64

import pytest

from aiq_agentcore import sandbox_agentcore as sb

pytest.importorskip("deepagents", reason="deepagents (upstream AI-Q dependency) not installed in adapter-only env")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.stopped = 0
        self.fail_write = False

    def invoke_code_interpreter(self, **kw):
        self.calls.append(kw)
        name, args = kw["name"], kw["arguments"]
        if name == "executeCommand":
            cmd = args["command"]
            if "AIQ_B64_EOF" in cmd:  # upload shim
                return {"stream": [{"result": {"content": [{"type": "text", "text": "ok"}],
                                               "structuredContent": {"stdout": "ok\n", "stderr": "", "exitCode": 0}}}]}
            if "__AIQ_NOT_FOUND__" in cmd:  # download shim
                import base64 as _b
                if cmd.rstrip().endswith("chart.png'") or "chart.png" in cmd:
                    return {"stream": [{"result": {"structuredContent": {"stdout": _b.b64encode(b"\x89PNG\r\n").decode(), "stderr": "", "exitCode": 0}}}]}  # noqa: E501
                if "missing.txt" in cmd:
                    return {"stream": [{"result": {"structuredContent": {"stdout": "__AIQ_NOT_FOUND__\n", "stderr": "", "exitCode": 0}}}]}  # noqa: E501
                return {"stream": [{"result": {"structuredContent": {"stdout": _b.b64encode(b"a,b\n1,2\n").decode(), "stderr": "", "exitCode": 0}}}]}  # noqa: E501
            if cmd.startswith("find "):
                return {"stream": [{"result": {"structuredContent": {"stdout": "/tmp/aiq/j/aiq-artifacts/chart.png\n/tmp/aiq/j/aiq-artifacts/notes.exe\n", "stderr": "", "exitCode": 0}}}]}  # noqa: E501
            return {"stream": [{"result": {"content": [{"type": "text", "text": "hello"}],
                                           "structuredContent": {"stdout": "hello\n", "stderr": "warn\n", "exitCode": 3}}}]}
        if name == "writeFiles":
            if self.fail_write:
                return {"stream": [{"result": {"isError": True, "content": [{"type": "text", "text": "absolute paths not allowed"}]}}]}  # noqa: E501
            return {"stream": [{"result": {"content": [{"type": "text", "text": "Successfully wrote"}]}}]}
        if name == "readFiles":
            path = args["paths"][0]
            if path.endswith("chart.png"):
                return {"stream": [{"result": {"content": [{"type": "resource", "resource": {"uri": f"file://{path}", "blob": b"\x89PNG\r\n"}}]}}]}  # noqa: E501
            if path.endswith("missing.txt"):
                return {"stream": [{"result": {"isError": True, "content": [{"type": "text", "text": "No such file"}]}}]}
            return {"stream": [{"result": {"content": [{"type": "resource", "resource": {"uri": f"file://{path}", "text": "a,b\n1,2\n"}}]}}]}  # noqa: E501
        raise AssertionError(name)

    def stop_code_interpreter_session(self, **kw):
        self.stopped += 1


def make():
    c = FakeClient()
    return c, sb.AgentCoreCodeInterpreterSandbox(client=c, identifier="aws.codeinterpreter.v1", session_id="sess-1", sync_cap=840)


def test_execute_parses_structured_content():
    c, s = make()
    r = s.execute("echo hello", timeout=30)
    assert r.output == "hello\nwarn\n" and r.exit_code == 3 and r.truncated is False
    assert c.calls[0]["name"] == "executeCommand" and c.calls[0]["sessionId"] == "sess-1"


def test_execute_wraps_long_timeouts_with_timeout_command():
    c, s = make()
    s.execute("sleep 1", timeout=5000)
    assert c.calls[0]["arguments"]["command"].startswith("timeout 840 bash -c ")


def test_upload_uses_python_shim_with_absolute_paths():
    c, s = make()
    out = s.upload_files([("/tmp/aiq/j/x.txt", b"data")])
    assert out[0].error is None and c.calls[0]["name"] == "executeCommand"
    assert base64.b64encode(b"data").decode() in c.calls[0]["arguments"]["command"] and "/tmp/aiq/j/x.txt" in c.calls[0]["arguments"]["command"]  # noqa: E501


def test_download_blob_text_and_missing():
    _, s = make()
    res = s.download_files(["/tmp/aiq/j/aiq-artifacts/chart.png", "/tmp/aiq/j/table.csv", "/tmp/aiq/j/missing.txt"])
    assert res[0].content == b"\x89PNG\r\n" and res[1].content == b"a,b\n1,2\n"
    assert res[2].content is None and res[2].error == "file_not_found"


def test_close_is_idempotent():
    c, s = make()
    s.close()
    s.close()
    assert c.stopped == 1


def test_kind_and_harvest_registry():
    assert sb._kind("chart.png") == "image" and sb._kind("data.csv") == "dataset" and sb._kind("x.bin") == "other"
    with sb._HARVEST_LOCK:
        sb._HARVEST["job_x"] = [sb.HarvestedArtifact("a.png", "/p/a.png", b"1", "image")]
    assert [a.name for a in sb.take_artifacts("job_x")] == ["a.png"] and sb.take_artifacts("job_x") == []


def test_collect_merges_stream_events():
    out = sb._collect([{"result": {"content": [{"type": "text", "text": "a"}], "structuredContent": {"exitCode": 0}}},
                       {"result": {"content": [{"type": "text", "text": "b"}], "isError": True}}])
    assert out["text"] == "a\nb" and out["structured"] == {"exitCode": 0} and out["is_error"] is True
