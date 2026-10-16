"""Shared exceptions for the engineering core.

Diagnostics are dictionaries ``{code, severity, entity_ids, message}`` as defined
in ``docs/CONTRACTS.md``. Storage and commands raise only these classes for
rejected input; the input objects are left untouched.
"""

from __future__ import annotations


def _describe(message, diagnostics):
    errors = [d for d in diagnostics if isinstance(d, dict) and d.get("severity") == "error"]
    if not errors:
        return str(message)
    first = errors[0]
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{message}: [{first.get('code')}] {first.get('message')}{more}"


class CommandError(Exception):
    """A command was rejected; ``diagnostics`` lists the reasons."""

    def __init__(self, diagnostics):
        self.diagnostics = list(diagnostics or [])
        super().__init__(self.diagnostics)

    def __str__(self):
        return _describe("command rejected", self.diagnostics)


class DocumentError(Exception):
    """A document could not be loaded or saved; ``diagnostics`` may explain why."""

    def __init__(self, message, diagnostics=None):
        self.message = message
        self.diagnostics = list(diagnostics or [])
        super().__init__(message, self.diagnostics)

    def __str__(self):
        return _describe(self.message, self.diagnostics)


class RevisionConflict(DocumentError):
    """The document on disk does not match the expected project identity or revision."""
