"""The suite passes, and (more importantly) it fails when a guardrail is removed.
A grader that can't fail proves nothing, so these tests grade the grader."""
import copy

import yaml

from governed_agents import evals
from governed_agents.agents import AGENTS
from governed_agents.policy import Capability, Tool

SUITE = yaml.safe_load(evals.SUITE.read_text(encoding="utf-8"))


def run_all():
    return {c["id"]: evals.run_case(c, copy.deepcopy(SUITE["world"]), None) for c in SUITE["cases"]}


def test_suite_passes():
    assert evals.main([]) == 0


def test_suite_fails_without_guards(monkeypatch):
    monkeypatch.setattr(evals.Guards, "check", lambda self, tool, args, seen: [])
    results = run_all()
    for case in ("invented-progress-is-caught", "never-read-issue-is-caught", "private-channel-not-quoted-publicly"):
        assert not results[case].passed, case


def test_suite_fails_if_a_commitment_write_tool_is_ever_granted(monkeypatch):
    real_build = evals.build

    def leaky_build(store, tracker, chat):
        reg, t, c = real_build(store, tracker, chat, agents=[])
        reg.add_tool(Tool("set_owner", Capability.WRITE, "", {"type": "object", "properties": {
            "issue_id": {"type": "string"}, "owner": {"type": "string"}}},
            run=tracker.set_owner, undo=lambda **_: None))
        for spec in AGENTS:
            if spec.name == "issue-triage":
                spec = type(spec)(**{**spec.__dict__, "tools": spec.tools + ("set_owner",)})
            reg.register(spec)
        return reg, t, c

    monkeypatch.setattr(evals, "build", leaky_build)
    result = run_all()["injection-tries-commitment-writes"]
    assert not result.passed
    assert any("commitment fields changed" in f for f in result.failures)
