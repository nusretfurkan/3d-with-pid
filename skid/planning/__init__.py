"""Engineering planning: an exact-model, tool-free Claude Code CLI planner behind deterministic checks.

- ``bridge``: the local CLI subprocess bridge (subscription auth, exact model/effort, no tools, cleanup).
- ``context``: the authoritative JSON context of one revision, capabilities and composition vocabulary.
- ``prompt``: planner instructions; the request travels as a JSON string inside the context.
- ``schema``: the structured plan schema, its CLI subset and the local strict checker.
- ``translate``: checked plan -> existing core operations -> one ``batch`` envelope.
- ``engineering``: ``propose_engineering`` (never raises for planner/plan faults; returns a proposal record).

The service (``skid.interactive``) owns locking, staleness and apply. See ``docs/PLANNING.md``.
"""

from .bridge import EFFORT, MODEL, ClaudeCliPlanner, PlannerConfig, PlannerError
from .engineering import propose_engineering

__all__ = ["EFFORT", "MODEL", "ClaudeCliPlanner", "PlannerConfig", "PlannerError", "propose_engineering"]
