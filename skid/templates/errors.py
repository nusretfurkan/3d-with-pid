"""Errors of the pure equipment-definition layer (re-exported by ``skid.equipment``)."""

from __future__ import annotations

__all__ = ["DefinitionError"]


class DefinitionError(ValueError):
    """A definition or recipe lookup, check or resolution failed; the message says why."""
