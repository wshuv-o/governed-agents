"""Human review gate and all-or-nothing apply.

A run that staged actions waits in `awaiting_approval`. Approving applies its
actions in order. If any action fails, the ones already applied are undone in
reverse order (a saga), so a run never leaves a system of record half-changed.
Approval is a compare-and-set on the run status, so a double click or two
reviewers at once cannot apply anything twice.
"""
from __future__ import annotations

from .policy import Registry
from .store import Store


class GateError(Exception):
    pass


def _transition(store: Store, run_id: str, to: str) -> None:
    if not store.transition(run_id, "awaiting_approval", to):
        run = store.get(run_id)
        raise GateError(f"run {run_id} is {run['status'] if run else 'missing'}, not awaiting approval")


def approve(store: Store, registry: Registry, run_id: str, approver: str) -> str:
    if not approver:
        raise GateError("approver identity is required")
    _transition(store, run_id, "applying")
    store.log(run_id, "approved", {"by": approver})
    applied: list[dict] = []
    for action in store.actions(run_id):
        if action["status"] != "pending":
            continue
        tool = registry.tools[action["tool"]]
        try:
            result = tool.run(**action["args"])
        except Exception as e:
            store.set_action(action["id"], "failed", {"error": repr(e)})
            store.log(run_id, "apply_failed", {"action": action["id"], "error": repr(e)})
            stuck = []
            for done in reversed(applied):
                try:
                    registry.tools[done["tool"]].undo(result=done["result"], **done["args"])
                except Exception as undo_err:  # keep undoing the rest; a human must fix this one
                    stuck.append(done["id"])
                    store.log(run_id, "rollback_failed", {"action": done["id"], "error": repr(undo_err)})
                    continue
                store.set_action(done["id"], "rolled_back", done["result"])
                store.log(run_id, "rolled_back", {"action": done["id"]})
            status = "rollback_failed" if stuck else "rolled_back"
            store.finish(run_id, status, {"failed_action": action["id"], "approved_by": approver,
                                          "needs_manual_fix": stuck})
            return status
        action["result"] = result
        store.set_action(action["id"], "applied", result)
        store.log(run_id, "applied", {"action": action["id"], "tool": action["tool"], "result": result})
        applied.append(action)
    store.finish(run_id, "succeeded", {"applied": len(applied), "approved_by": approver})
    return "succeeded"


def reject(store: Store, run_id: str, reviewer: str, reason: str = "") -> None:
    if not reviewer:
        raise GateError("reviewer identity is required")
    _transition(store, run_id, "rejected")
    for action in store.actions(run_id):
        store.set_action(action["id"], "rejected")
    store.log(run_id, "rejected", {"by": reviewer, "reason": reason})
    store.finish(run_id, "rejected", {"rejected_by": reviewer, "reason": reason})
