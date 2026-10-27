"""The example agent portfolio and the wiring that builds a registry for it."""
from __future__ import annotations

from .policy import AgentSpec, Capability, Registry, Tier, Tool
from .store import Store
from .world import Chat, Tracker, build_tools

AGENTS = [
    AgentSpec(
        name="standup-digest", owner="eng-manager@example.com", tier=Tier.DRAFTS,
        tools=("list_issues", "read_channel", "draft_digest"),
        instructions="Write a short daily digest of issue progress for #team. Read the issues and #team "
                     "first. Report only what the data shows; if nothing moved, say so.",
        output_visibility="public"),
    AgentSpec(
        name="issue-triage", owner="support-lead@example.com", tier=Tier.WRITE_WITH_CONSENT,
        tools=("list_issues", "label_issue", "comment_on_issue", "notify_person"),
        instructions="For the issue in the trigger, propose labels and, if information is missing, a polite "
                     "comment asking for it. You cannot assign owners, due dates or statuses; if one is "
                     "needed, notify the support lead instead."),
]


def notify_tool(store: Store) -> Tool:
    def queue(person: str, text: str, run_id: str = "") -> dict[str, str]:
        store.queue_message(person, text, run_id)
        return {"person": person, "queued": "yes"}

    return Tool("notify_person", Capability.DRAFT,
                "Ask a person something. Messages are bundled and capped per person per day.",
                {"type": "object", "properties": {"person": {"type": "string"}, "text": {"type": "string"}},
                 "required": ["person", "text"]},
                run=queue, undo=lambda **_: None)


def build(store: Store, tracker: Tracker | None = None, chat: Chat | None = None,
          agents: list[AgentSpec] | None = None) -> tuple[Registry, Tracker, Chat]:
    tracker = tracker or Tracker()
    chat = chat or Chat()
    reg = Registry()
    for tool in build_tools(tracker, chat) + [notify_tool(store)]:
        reg.add_tool(tool)
    for spec in agents or AGENTS:
        reg.register(spec)
    return reg, tracker, chat


def deliver(store: Store, send, max_per_day: int = 2, now: float | None = None) -> dict[str, int]:
    """Bundle pending messages into one per person, and hold them if the person's budget is spent."""
    import time
    now = now or time.time()
    by_person: dict[str, list[tuple[str, str]]] = {}
    for mid, person, text in store.pending_messages():
        by_person.setdefault(person, []).append((mid, text))
    sent: dict[str, int] = {}
    for person, msgs in by_person.items():
        if store.delivered_today(person) >= max_per_day:
            continue   # held until tomorrow; nothing is dropped
        send(person, "\n".join(f"- {t}" for _, t in msgs))
        store.mark_delivered([m for m, _ in msgs], now)
        sent[person] = len(msgs)
    return sent
