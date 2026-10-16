"""Engineering core: validation, shared errors, persistence, and commands.

The core is importable without Blender. Submodules import each other through
``.errors``, ``.validation``, and ``.storage`` rather than this package root.
"""

from .errors import CommandError, DocumentError, RevisionConflict
from .validation import canonical_digest, validate_document, validate_project
from .storage import load_document, save_document
from .document import new_document

__all__ = [
    "CommandError",
    "DocumentError",
    "RevisionConflict",
    "canonical_digest",
    "load_document",
    "new_document",
    "save_document",
    "validate_document",
    "validate_project",
]

try:
    from .commands import apply_command
except ModuleNotFoundError as exc:
    # skid/core/commands.py is delivered by a separate task; tolerate only its absence.
    if exc.name != f"{__name__}.commands":
        raise
else:
    __all__.append("apply_command")
