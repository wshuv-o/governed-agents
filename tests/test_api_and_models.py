import hashlib
import hmac
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from governed_agents.model import Anthropic, OpenAICompatible, Reply, Scripted, ToolCall
from governed_agents.runtime import Runtime
from governed_agents.world import Issue

SECRET = "whsec-test"
OPS = {"X-API-Key": "op-key", "X-Operator": "lead@example.com"}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("OPERATOR_KEYS", "op-key")
    from governed_agents import api as module
    module = importlib.reload(module)
    return module, TestClient(module.app)


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    return raw, {"X-Signature": "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
                 "Content-Type": "application/json"}


def test_webhook_requires_valid_signature(api):
    _, client = api
    raw, headers = signed({"issue_id": "ENG-1"})
    assert client.post("/v1/events/issue-triage", content=raw,
                       headers={**headers, "X-Signature": "sha256=forged"}).status_code == 401
    assert client.post("/v1/events/issue-triage", content=raw + b" ", headers=headers).status_code == 401
    assert client.post("/v1/events/issue-triage", content=raw, headers=headers).status_code == 202


def test_redelivered_event_is_deduplicated(api):
    _, client = api
    raw, headers = signed({"issue_id": "ENG-1"})
    first = client.post("/v1/events/issue-triage", content=raw, headers={**headers, "X-Event-Id": "e1"}).json()
    again = client.post("/v1/events/issue-triage", content=raw, headers={**headers, "X-Event-Id": "e1"}).json()
    assert first["run_id"] == again["run_id"] and again["duplicate"]


def test_operator_endpoints_need_key_and_identity(api):
    _, client = api
    assert client.get("/v1/runs").status_code == 401
    assert client.get("/v1/runs", headers={"X-API-Key": "op-key"}).status_code == 400
    assert client.get("/v1/runs", headers=OPS).status_code == 200


def test_full_flow_webhook_to_approval_with_audit_trail(api):
    module, client = api
    module.TRACKER.issues["ENG-1"] = module.TRACKER.issues.get("ENG-1") or \
        __import__("governed_agents.world", fromlist=["Issue"]).Issue("ENG-1", "Crash on upload", "triage")
    raw, headers = signed({"issue_id": "ENG-1"})
    run_id = client.post("/v1/events/issue-triage", content=raw, headers=headers).json()["run_id"]
    model = Scripted([Reply(tool_calls=[ToolCall("1", "list_issues", {})]),
                      Reply(tool_calls=[ToolCall("2", "label_issue", {"issue_id": "ENG-1", "labels": ["bug"]})]),
                      Reply(text="labelled")])
    Runtime(module.STORE, module.REGISTRY, model, worker="t").work_once()

    queue = client.get("/v1/runs", params={"status": "awaiting_approval"}, headers=OPS).json()
    assert [r["id"] for r in queue] == [run_id]
    assert client.post(f"/v1/runs/{run_id}/approve", headers=OPS).json() == {"status": "succeeded"}
    assert client.post(f"/v1/runs/{run_id}/approve", headers=OPS).status_code == 409
    assert module.TRACKER.issues["ENG-1"].labels == ["bug"]

    detail = client.get(f"/v1/runs/{run_id}", headers=OPS).json()
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds[:2] == ["enqueued", "claimed"] and "approved" in kinds and "applied" in kinds
    assert next(e for e in detail["events"] if e["kind"] == "approved")["data"]["by"] == "lead@example.com"


TOOLS = [{"name": "list_issues", "description": "d", "parameters": {"type": "object", "properties": {}}}]
CONVO = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
         {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "list_issues", "args": {}}]},
         {"role": "tool", "call_id": "t1", "content": "[]"}]


def test_openai_adapter_round_trip():
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "t2", "type": "function", "function": {"name": "list_issues", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    m = OpenAICompatible("m", "http://x/v1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    reply = m.complete(CONVO, TOOLS)
    assert reply.tool_calls[0].name == "list_issues" and reply.usage == {"input": 10, "output": 3}
    assert sent["messages"][2]["tool_calls"][0]["function"]["name"] == "list_issues"
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "[]"}


def test_anthropic_adapter_round_trip():
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}],
                                         "usage": {"input_tokens": 7, "output_tokens": 2}})

    m = Anthropic("m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    reply = m.complete(CONVO, TOOLS)
    assert reply.text == "done" and reply.tool_calls == []
    assert sent["system"] == "sys"
    assert sent["messages"][1]["content"][0]["type"] == "tool_use"
    assert sent["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "[]"}
    assert sent["tools"][0]["input_schema"]["type"] == "object"
