"""Risk tiers, tool grants and agent registration.

The rule this module enforces: what an agent is allowed to do is decided by its
registration, never by its prompt. A tool the agent was not granted does not
exist from the model's point of view, and a write tool never executes inside
the agent loop; it is staged for a human (see gate.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Callable


class Capability(StrEnum):
    READ = "read"      # runs immediately, no side effects
    DRAFT = "draft"    # produces something a human reviews before anyone sees it
    WRITE = "write"    # changes a system of record; only ever applied after approval


class Tier(IntEnum):
    READ_ONLY = 0
    DRAFTS = 1
    WRITE_WITH_CONSENT = 2


TIER_ALLOWS: dict[Tier, set[Capability]] = {
    Tier.READ_ONLY: {Capability.READ},
    Tier.DRAFTS: {Capability.READ, Capability.DRAFT},
    Tier.WRITE_WITH_CONSENT: {Capability.READ, Capability.DRAFT, Capability.WRITE},
}

# Evals each tier must pass before an agent of that tier may be registered for production.
TIER_REQUIRES_SAFETY_SUITE = {Tier.READ_ONLY: False, Tier.DRAFTS: True, Tier.WRITE_WITH_CONSENT: True}


class PolicyError(Exception):
    """Raised when a registration or a tool call breaks policy. Always audited."""


@dataclass(frozen=True)
class Tool:
    name: str
    capability: Capability
    description: str
    params: dict[str, Any]                 # JSON schema; additionalProperties is forced to false
    run: Callable[..., Any]                # READ: returns data. DRAFT/WRITE: applied by the gate
    undo: Callable[..., Any] | None = None  # WRITE only: compensation used for rollback

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {**self.params, "additionalProperties": False}}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    owner: str
    tier: Tier
    tools: tuple[str, ...]
    instructions: str
    max_steps: int = 8
    output_visibility: str = "public"      # who will read the output; drives the leak check


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)
    agents: dict[str, AgentSpec] = field(default_factory=dict)

    def add_tool(self, tool: Tool) -> None:
        if tool.capability is Capability.WRITE and tool.undo is None:
            raise PolicyError(f"write tool {tool.name} has no undo, so it cannot be rolled back")
        self.tools[tool.name] = tool

    def register(self, spec: AgentSpec) -> AgentSpec:
        """Nothing runs without an owner, a tier and an explicit tool grant that fits the tier."""
        if not spec.owner:
            raise PolicyError(f"{spec.name}: no owner")
        if not spec.tools:
            raise PolicyError(f"{spec.name}: no tool grant")
        allowed = TIER_ALLOWS[spec.tier]
        for name in spec.tools:
            tool = self.tools.get(name)
            if tool is None:
                raise PolicyError(f"{spec.name}: unknown tool {name}")
            if tool.capability not in allowed:
                raise PolicyError(f"{spec.name}: tier {spec.tier.name} cannot be granted "
                                  f"{tool.capability} tool {name}")
        self.agents[spec.name] = spec
        return spec

    def granted(self, agent: str) -> list[Tool]:
        return [self.tools[t] for t in self.agents[agent].tools]


def validate_args(tool: Tool, args: dict[str, Any]) -> None:
    """Small JSON-schema subset: required keys, no unknown keys, primitive types."""
    props = tool.params.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise PolicyError(f"{tool.name}: unknown argument(s) {sorted(unknown)}")
    for key in tool.params.get("required", []):
        if key not in args:
            raise PolicyError(f"{tool.name}: missing argument {key}")
    types = {"string": str, "integer": int, "array": list, "boolean": bool}
    for key, value in args.items():
        expected = types.get(props[key].get("type", ""))
        if expected and not isinstance(value, expected):
            raise PolicyError(f"{tool.name}: {key} must be {props[key]['type']}")
        if "enum" in props[key] and value not in props[key]["enum"]:
            raise PolicyError(f"{tool.name}: {key} must be one of {props[key]['enum']}")
        allowed_items = props[key].get("items", {}).get("enum")
        if allowed_items and isinstance(value, list) and set(value) - set(allowed_items):
            raise PolicyError(f"{tool.name}: {key} items must be from {allowed_items}")
