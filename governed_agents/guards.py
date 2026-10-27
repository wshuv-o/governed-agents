"""Checks a draft must pass before it is even staged for a human.

These target the ways a reporting agent goes wrong in practice:
- inventing progress: citing an issue it never read, or calling an issue done when it isn't;
- leaking: quoting a private channel into a public digest.
All checks are deterministic, so they run on every draft and in CI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ISSUE_ID = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
DONE_WORDS = re.compile(r"\b(done|completed?|shipped|fixed|merged|closed|resolved|finished)\b", re.I)
DONE_STATUSES = {"done", "closed", "resolved"}
SHINGLE = 6   # this many consecutive words from a private message counts as quoting it


@dataclass
class Seen:
    """What the agent has actually read during this run (persisted with the checkpoint)."""
    issues: dict[str, str] = field(default_factory=dict)      # id -> status
    private_texts: list[str] = field(default_factory=list)

    def record(self, tool: str, result: Any) -> None:
        items = result if isinstance(result, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            if tool == "list_issues" and "id" in item:
                self.issues[item["id"]] = item.get("status", "")
            if item.get("visibility") == "private" and item.get("text"):
                self.private_texts.append(item["text"])

    def to_json(self) -> dict[str, Any]:
        return {"issues": self.issues, "private_texts": self.private_texts}

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "Seen":
        return cls(**d) if d else cls()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _shingles(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def check_grounding(text: str, seen: Seen) -> list[str]:
    problems = []
    for iid in sorted(set(ISSUE_ID.findall(text)) - set(seen.issues)):
        problems.append(f"mentions {iid}, which was never read in this run")
    for line in re.split(r"(?<=[.!?])\s+|\n", text):
        if not DONE_WORDS.search(line):
            continue
        for iid in ISSUE_ID.findall(line):
            status = seen.issues.get(iid)
            if status is not None and status.lower() not in DONE_STATUSES:
                problems.append(f"says {iid} is finished, but its status is '{status}'")
    return problems


def check_leak(text: str, seen: Seen) -> list[str]:
    draft = _shingles(_words(text))
    return [f"quotes a private message: \"{p[:60]}\"" for p in seen.private_texts
            if draft & _shingles(_words(p))]


class Guards:
    def __init__(self, private_channels: set[str] | None = None):
        self.private_channels = private_channels or set()

    def check(self, tool: str, args: dict[str, Any], seen: Seen) -> list[str]:
        text = args.get("text") or args.get("body") or ""
        if not text:
            return []
        problems = check_grounding(text, seen)
        if args.get("channel") not in self.private_channels:
            problems += check_leak(text, seen)
        return problems
