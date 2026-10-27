"""The systems of record the example agents work against, and the tools over them.

`Tracker` stands in for an issue tracker (Linear, Jira) and `Chat` for Slack.
The tracker can set owners, due dates and statuses, but the agent-facing tools
are built from `TrackerAgentView`, which has no method that writes any of them.
That is the structural guarantee: no prompt, injected text or model error can
produce a commitment write, because no tool has a code path to one. In
production the same idea is enforced again by the credential itself
(a token scoped to read + comment).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .policy import Capability, Tool

COMMITMENT_FIELDS = ("owner", "due", "status")


@dataclass
class Issue:
    id: str
    title: str
    status: str
    owner: str | None = None
    due: str | None = None
    labels: list[str] = field(default_factory=list)
    comments: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Tracker:
    issues: dict[str, Issue] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    # --- full API; humans and integrations use these, agents never see them ---
    def set_owner(self, issue_id: str, owner: str) -> None:
        self.issues[issue_id].owner = owner

    def set_due(self, issue_id: str, due: str) -> None:
        self.issues[issue_id].due = due

    def set_status(self, issue_id: str, status: str) -> None:
        self.issues[issue_id].status = status

    def add_comment(self, issue_id: str, body: str, author: str) -> str:
        cid = f"c{next(self._ids)}"
        self.issues[issue_id].comments.append({"id": cid, "body": body, "author": author})
        return cid

    def delete_comment(self, issue_id: str, comment_id: str) -> None:
        self.issues[issue_id].comments = [c for c in self.issues[issue_id].comments if c["id"] != comment_id]

    def add_labels(self, issue_id: str, labels: list[str]) -> list[str]:
        issue = self.issues[issue_id]
        added = [l for l in labels if l not in issue.labels]
        issue.labels += added
        return added

    def remove_labels(self, issue_id: str, labels: list[str]) -> None:
        issue = self.issues[issue_id]
        issue.labels = [l for l in issue.labels if l not in labels]


class TrackerAgentView:
    """Everything an agent may do to the tracker. Deliberately no owner/due/status writes."""

    def __init__(self, tracker: Tracker, author: str):
        self.__tracker = tracker
        self.__author = author

    def list_issues(self) -> list[dict[str, Any]]:
        return [{"id": i.id, "title": i.title, "status": i.status, "owner": i.owner,
                 "comments": [c["body"] for c in i.comments], "visibility": "public"}
                for i in self.__tracker.issues.values()]

    def comment(self, issue_id: str, body: str) -> dict[str, str]:
        return {"issue_id": issue_id, "comment_id": self.__tracker.add_comment(issue_id, body, self.__author)}

    def uncomment(self, issue_id: str, comment_id: str, **_: Any) -> None:
        self.__tracker.delete_comment(issue_id, comment_id)

    def label(self, issue_id: str, labels: list[str]) -> dict[str, Any]:
        return {"issue_id": issue_id, "added": self.__tracker.add_labels(issue_id, labels)}

    def unlabel(self, issue_id: str, added: list[str], **_: Any) -> None:
        self.__tracker.remove_labels(issue_id, added)


@dataclass
class Chat:
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)   # name -> {private, messages}
    posted: list[dict[str, str]] = field(default_factory=list)

    def read(self, channel: str) -> list[dict[str, str]]:
        ch = self.channels[channel]
        vis = "private" if ch["private"] else "public"
        return [{"channel": channel, "text": m, "visibility": vis} for m in ch["messages"]]

    def post(self, channel: str, text: str) -> dict[str, str]:
        self.posted.append({"channel": channel, "text": text})
        return {"channel": channel, "index": str(len(self.posted) - 1)}

    def unpost(self, index: str, **_: Any) -> None:
        self.posted[int(index)] = {"channel": "", "text": "[retracted]"}


def build_tools(tracker: Tracker, chat: Chat, agent_author: str = "agent") -> list[Tool]:
    view = TrackerAgentView(tracker, agent_author)
    return [
        Tool("list_issues", Capability.READ, "List tracker issues with status, owner and comments.",
             {"type": "object", "properties": {}}, run=lambda: view.list_issues()),
        Tool("read_channel", Capability.READ, "Read recent messages from a chat channel.",
             {"type": "object", "properties": {"channel": {"type": "string"}}, "required": ["channel"]},
             run=lambda channel: chat.read(channel)),
        Tool("draft_digest", Capability.DRAFT,
             "Draft a digest for a channel. A human reviews it before it is posted. "
             "Cite every issue you mention as [ISSUE-ID].",
             {"type": "object", "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
              "required": ["channel", "text"]},
             run=lambda channel, text: chat.post(channel, text),
             undo=lambda result, **_: chat.unpost(**result)),
        Tool("comment_on_issue", Capability.WRITE, "Add a comment to an issue (applied after human approval).",
             {"type": "object", "properties": {"issue_id": {"type": "string"}, "body": {"type": "string"}},
              "required": ["issue_id", "body"]},
             run=lambda issue_id, body: view.comment(issue_id, body),
             undo=lambda result, **_: view.uncomment(**result)),
        Tool("label_issue", Capability.WRITE, "Add labels to an issue (applied after human approval).",
             {"type": "object", "properties": {
                 "issue_id": {"type": "string"},
                 "labels": {"type": "array", "items": {"type": "string",
                            "enum": ["bug", "feature", "question", "needs-info", "duplicate"]}}},
              "required": ["issue_id", "labels"]},
             run=lambda issue_id, labels: view.label(issue_id, labels),
             undo=lambda result, **_: view.unlabel(**result)),
    ]
