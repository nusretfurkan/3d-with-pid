"""Read-only Ask: grounded answers about one frozen project revision (pure standard library).

- ``query``: deterministic identity, directed connectivity, bounded traversal and reference facts.
- ``intent``: conservative classification of a question (local forms, change requests, history, general).
- ``schema``: the frozen request checks and the strict, answer-only model output schema.
- ``context``: the bounded tool-free model context and instructions for broader questions.
- ``response``: the frozen response DTO, verified references and source labels.
- ``ask``: ``prepare`` (local answer or one model job) and ``finish`` (model reply -> response).

Nothing here mutates a document, dispatches a command, saves, builds or calls
a model; the service (``skid.interactive``) owns locking, the bridge call,
cancellation and staleness. See ``docs/ASK.md``.
"""

from .ask import MAX_PROMPT_BYTES, MAX_TARGETS, Prepared, finish, prepare
from .response import RESPONSE_FIELDS, STATUSES, stamp
from .schema import AskRequest, AskRequestError, parse_request

__all__ = ["MAX_PROMPT_BYTES", "MAX_TARGETS", "RESPONSE_FIELDS", "STATUSES", "AskRequest", "AskRequestError",
           "Prepared", "finish", "parse_request", "prepare", "stamp"]
