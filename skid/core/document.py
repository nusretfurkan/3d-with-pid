"""Factory for new, genuinely empty engineering documents.

``new_document(project_id, name=None)`` returns the existing document schema
at revision 0 with empty entity, port, connection and assumption maps, empty
P&ID symbol/edge maps, empty physical placement/route maps and empty undo and
redo history. Nothing is seeded: equipment, connections and assumptions are
added later through validated commands (``add_equipment``, ``connect_ports``,
``batch``). The optional display ``name`` follows ``rename_project``: it is
trimmed and must satisfy ``validation.project_name_problem``; without one the
project has no ``name`` field and is shown by its ID.

The project ID is permanent and separate from the editable name. Factory IDs
are deliberately conservative (``PROJECT_ID_PATTERN``) because the ID is
written into file names, STEP headers and build identities; the persisted
schema itself accepts any nonempty string, so documents created elsewhere stay
loadable. Invalid arguments raise ``DocumentError`` with diagnostics and
nothing is created.
"""

from __future__ import annotations

import re

from .errors import DocumentError
from .validation import SCHEMA_VERSION, project_name_problem, validate_document

__all__ = ["PROJECT_ID_PATTERN", "new_document"]

PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _diagnostic(code, message):
    return {"code": code, "severity": "error", "entity_ids": [], "message": message}


def new_document(project_id, name=None) -> dict:
    """Return a new empty document for ``project_id`` (revision 0, no history); see the module docstring."""
    if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.match(project_id):
        message = (f"project id {project_id!r} must be 1 to 128 characters of letters, digits, '.', '_', ':' or '-', "
                   "starting with a letter or digit")
        raise DocumentError("cannot create the document", [_diagnostic("invalid_project_id", message)])
    project = {
        "id": project_id,
        "revision": 0,
        "model": {"entities": {}, "ports": {}, "connections": {}, "assumptions": {}},
        "pid": {"symbols": {}, "edges": {}},
        "physical": {"placements": {}, "routes": {}},
    }
    if name is not None:
        trimmed = name.strip() if isinstance(name, str) else name
        problem = project_name_problem(trimmed)
        if problem is not None:
            raise DocumentError("cannot create the document", [_diagnostic("invalid_project_name", problem)])
        project["name"] = trimmed
    document = {"schema_version": SCHEMA_VERSION, "current": project, "undo": [], "redo": []}
    errors = [d for d in validate_document(document) if d["severity"] == "error"]
    if errors:  # defensive: the empty schema must validate, never pass as created when it does not
        raise DocumentError("the new empty document does not validate", errors)
    return document
