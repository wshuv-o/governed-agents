"""HTTP surface.

POST /v1/events/{agent}       signed webhook (HMAC-SHA256) -> queued run; redeliveries are de-duplicated
GET  /v1/runs?status=...      list runs (e.g. the approval queue)
GET  /v1/runs/{id}            the run, its staged actions and its full audit log (enough to reconstruct it)
POST /v1/runs/{id}/approve    apply staged actions (all or nothing)
POST /v1/runs/{id}/reject
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from . import gate
from .agents import build
from .store import Store

app = FastAPI(title="governed-agents")
STORE = Store(os.environ.get("DATABASE_URL", "sqlite:///agents.db"))
REGISTRY, TRACKER, CHAT = build(STORE)


def _keys() -> set[str]:
    return {k for k in os.environ.get("OPERATOR_KEYS", "").split(",") if k}


def operator(x_api_key: str = Header(default=""), x_operator: str = Header(default="")) -> str:
    keys = _keys()
    if not keys or not any(hmac.compare_digest(x_api_key, k) for k in keys):
        raise HTTPException(401, "invalid API key")
    if not x_operator:
        raise HTTPException(400, "X-Operator (who is acting) is required for the audit log")
    return x_operator


@app.post("/v1/events/{agent}", status_code=202)
async def event(agent: str, request: Request, x_signature: str = Header(default=""),
                x_event_id: str = Header(default="")):
    secret = os.environ.get("WEBHOOK_SECRET", "")
    body = await request.body()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(x_signature, expected):
        raise HTTPException(401, "bad signature")
    if agent not in REGISTRY.agents:
        raise HTTPException(404, "unknown agent")
    try:
        trigger = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "body must be JSON")
    run_id, created = STORE.enqueue(agent, trigger, dedup_key=f"{agent}:{x_event_id}" if x_event_id else None)
    return {"run_id": run_id, "duplicate": not created}


@app.get("/v1/runs")
def list_runs(status: str | None = None, who: str = Depends(operator)):
    return [{k: r[k] for k in ("id", "agent", "status", "attempts", "result")} for r in STORE.runs(status)]


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, who: str = Depends(operator)):
    run = STORE.get(run_id)
    if not run:
        raise HTTPException(404)
    run.pop("state", None)
    return {**run, "actions": STORE.actions(run_id), "events": STORE.events(run_id)}


@app.post("/v1/runs/{run_id}/approve")
def approve(run_id: str, who: str = Depends(operator)):
    try:
        return {"status": gate.approve(STORE, REGISTRY, run_id, who)}
    except gate.GateError as e:
        raise HTTPException(409, str(e))


@app.post("/v1/runs/{run_id}/reject")
def reject(run_id: str, reason: str = "", who: str = Depends(operator)):
    try:
        gate.reject(STORE, run_id, who, reason)
    except gate.GateError as e:
        raise HTTPException(409, str(e))
    return {"status": "rejected"}


@app.get("/healthz")
def healthz():
    return {"ok": True}
