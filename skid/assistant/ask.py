"""Ask orchestration: one frozen snapshot -> a local answer or one bounded model job -> one response.

``prepare(project, digest, request, answer_id)`` never mutates anything and
never calls a model. It checks the selected IDs against the snapshot
(``AskRequestError`` otherwise), resolves mentions, classifies the question
and returns a ``Prepared`` record. For every local outcome its ``response`` is
already complete:

- identity, immediate or bounded expanded connectivity, relation (is A
  upstream of B / what lies between) and overview answers;
- clarifications for unknown or ambiguous tags, an ambiguous or missing
  selection, a missing item and too many items;
- change requests: read-only, the client may copy the text to Change; nothing
  is planned or applied;
- historical questions: only the current snapshot is available.

Anything else is a broader question: ``job`` then holds the prompt, the
Ask-specific schema and the IDs a reply may cite. ``finish(prepared, ...)``
turns the bridge's reply, error, cancellation or time-out into the response.
Output that fails ``schema.validate_answer`` is rejected whole; the response
is then ``unavailable`` and still carries the local facts of the targets.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .context import build_context, build_prompt
from .intent import classify
from .query import STANDING_UNKNOWNS, ProjectQuery, plain
from .response import make_response, references, safe_bridge, source_label
from .schema import AskRequestError, cli_schema, validate_answer

__all__ = ["MAX_PROMPT_BYTES", "MAX_TARGETS", "Job", "Prepared", "finish", "prepare"]

MAX_TARGETS = 4
MAX_PROMPT_BYTES = 256 << 10
LABELS = {"recorded_fact": "Recorded fact", "graph_conclusion": "From the connection graph",
          "general_explanation": "General explanation (not project data)", "unknown": "Unknown"}
VERIFY_NOTE = ("A consistent model is not an engineering verification: no duty, sizing, safety, control or standards "
               "check is made.")
NOT_RECORDED = ("Not recorded in the model: process duty, flow, fluid properties, pressures, temperatures, materials, "
                "ratings and control philosophy (see unknowns).")


@dataclass(frozen=True)
class Job:
    prompt: str
    schema: dict
    allowed_ids: tuple
    prompt_bytes: int
    prompt_sha256: str
    compact: bool


@dataclass(frozen=True)
class Prepared:
    answer_id: str
    request: object
    project: dict
    digest: str
    query: ProjectQuery
    intent: object
    targets: tuple
    evidence: tuple
    response: dict | None = None
    job: Job | None = None


def _local_responder(query_kind):
    return {"kind": "deterministic", "language_model": False, "query": query_kind, "model_called": False}


def _display_note(request, project, digest):
    shown = request.displayed_source
    if not shown or (shown["source_revision"] == project.get("revision") and shown["input_digest"] == digest):
        return []
    return [f"Answer: current model r{project.get('revision')}; visible {shown['mode']} content r"
            f"{shown['source_revision']}. The visible content may differ from the current model; this answer "
            "describes the current model only."]


class _Builder:
    def __init__(self, answer_id, request, project, digest, query, intent):
        self.answer_id, self.request, self.project, self.digest = answer_id, request, project, digest
        self.query, self.intent = query, intent
        self.revision = project.get("revision")

    def local_notes(self):
        return [f"Answered locally by deterministic queries of the {source_label(self.revision, self.digest)}; no "
                "question or project data was sent to a language model.", VERIFY_NOTE] + _display_note(
            self.request, self.project, self.digest)

    def respond(self, status, query_kind, lines, ids=(), unknowns=(), targets=(), evidence=()):
        response = make_response(self.answer_id, self.request, self.project, self.digest, status, lines,
                                 references(self.query, ids), unknowns, self.local_notes(),
                                 _local_responder(query_kind))
        return Prepared(self.answer_id, self.request, self.project, self.digest, self.query, self.intent,
                        tuple(targets), tuple(evidence), response=response)

    def example_tag(self):
        query = self.query
        tags = sorted((query.tag(e) for e in query.entities if query.ports_of.get(e)), key=str.casefold)
        return tags[0] if tags else "TAG"

    def unknowns(self, targets):
        found = []
        for record_id in targets:
            found += self.query.unknowns_for(record_id)
        return found or list(STANDING_UNKNOWNS)

    def failed_notes(self, job):
        return [f"The question and a {max(1, job.prompt_bytes // 1024)} KiB context of revision {self.revision} were "
                "handed to the local Claude Code CLI bridge, which produced no usable answer. Any facts shown come "
                f"from deterministic queries of the {source_label(self.revision, self.digest)}.",
                VERIFY_NOTE] + _display_note(self.request, self.project, self.digest)


def _identity(builder, targets):
    lines = []
    for record_id in targets:
        if lines:
            lines.append("")
        lines += builder.query.identity_lines(record_id)
    lines += ["", NOT_RECORDED]
    ids = [i for t in targets for i in [t] + builder.query.connectivity_ids(t)]
    return builder.respond("answered", "identity", lines, ids, builder.unknowns(targets), targets=targets,
                           evidence=targets)


def _connectivity(builder, targets):
    query, intent = builder.query, builder.intent
    lines, ids = [], []
    for record_id in targets:
        if lines:
            lines.append("")
        lines += query.connectivity_lines(record_id)
        ids += query.connectivity_ids(record_id)
        if intent.expanded:
            for direction in intent.directions:
                traced, result = query.trace_lines(record_id, direction)
                lines += [""] + traced
                ids += result["ids"]
        else:
            lines.append(f"For the full bounded path, ask “trace upstream and downstream of {query.tag(record_id)}”.")
    return builder.respond("answered", "expanded_connectivity" if intent.expanded else "connectivity", lines, ids,
                           builder.unknowns(targets), targets=targets, evidence=targets)


def _historical(builder, targets, unknown):
    query, intent = builder.query, builder.intent
    earlier = sorted({r for r in intent.revisions if r != builder.revision})
    when = f"in revision {', '.join(str(r) for r in earlier)}" if earlier else "earlier"
    lines = [f"Ask reads only the current model, revision {builder.revision}. It does not query earlier revisions, "
             f"the undo history or older publications, so it cannot say what was recorded {when}."]
    shown = builder.request.displayed_source
    if shown and shown["source_revision"] != builder.revision:
        lines.append(f"The visible {shown['mode']} content is revision {shown['source_revision']}; Ask cannot read "
                     "it, and its facts may differ from the current revision.")
    for token in unknown:
        lines.append(_unknown_line(query, token, builder.revision))
    if targets:
        lines += ["", f"Current facts (revision {builder.revision}; they may differ from earlier revisions):"]
        for record_id in targets:
            lines += query.identity_lines(record_id)
    return builder.respond("clarification", "historical", lines, targets,
                           [f"Facts recorded in earlier revisions are not available to Ask (it reads revision "
                            f"{builder.revision} only)."], targets=targets)


def _unknown_line(query, token, revision):
    similar = query.suggestions(token)
    hint = f" Current tags with a similar spelling: {', '.join(similar)}." if similar else ""
    return f"“{plain(token, 80)}” is not a current tag or ID in revision {revision}.{hint}"


def _clarify(builder, query_kind, lines, ids=(), unknowns=()):
    return builder.respond("clarification", query_kind, lines, ids, unknowns)


def _job(query, request, digest, evidence, targets):
    allowed = tuple(query.semantic_ids())
    for compact in (False, True):
        prompt = build_prompt(build_context(query, request, digest, evidence, targets, compact=compact))
        data = prompt.encode("utf-8")
        if len(data) <= MAX_PROMPT_BYTES:
            return Job(prompt, cli_schema(allowed), allowed, len(data), hashlib.sha256(data).hexdigest(), compact)
    return None


def prepare(project, digest, request, answer_id) -> Prepared:
    query = ProjectQuery(project)
    revision = project.get("revision")
    for selected in request.selected_ids:
        if not query.kind_of(selected):
            raise AskRequestError(f"selected ID {plain(selected, 80)!r} is not an entity, port or connection of "
                                  f"revision {revision}")
    mentions = query.mentions(request.question)
    intent = classify(mentions.masked)
    builder = _Builder(answer_id, request, project, digest, query, intent)
    selection = list(request.selected_ids)

    if intent.kind == "change_request":
        return builder.respond("clarification", "change_request", [
            f"Ask is read-only: nothing was planned, applied, saved or built, and the project is unchanged "
            f"(revision {revision}).",
            "This reads as a change request. To draft it, copy it to Change (Quick Edit or Engineering Plan), review "
            "the proposal there and apply it explicitly."], mentions.targets or selection)

    seen = {token.casefold() for token in mentions.unknown}
    unknown = list(mentions.unknown)
    for token in intent.unnamed:  # classification is case-folded; report the user's own spelling
        if token.casefold() not in seen:
            match = re.search(re.escape(token), request.question, re.IGNORECASE)
            unknown.append(match.group(0) if match else token)
            seen.add(token.casefold())
    explicit = list(mentions.targets)
    if intent.kind == "historical":
        targets = explicit or (selection if len(selection) == 1 else [])
        return _historical(builder, targets[:MAX_TARGETS], unknown)
    if mentions.ambiguous:
        lines = [f"“{token}” matches more than one item in revision {revision}: "
                 + ", ".join(f"{query.tag(i)} ({i})" for i in ids) + "." for token, ids in mentions.ambiguous]
        lines.append("Which one do you mean? Name its stable ID or select it, then ask again.")
        return _clarify(builder, "ambiguous", lines, [i for _, ids in mentions.ambiguous for i in ids] + explicit,
                        [f"“{token}” is ambiguous in revision {revision}." for token, _ in mentions.ambiguous])
    if unknown:
        lines = [_unknown_line(query, token, revision) for token in unknown]
        lines.append("Which item do you mean? Name a current tag or select the item, then ask again.")
        return _clarify(builder, "unknown", lines, explicit,
                        [f"No item named “{plain(t, 80)}” exists in revision {revision}." for t in unknown])

    if intent.kind == "overview":  # about the whole project; the selection is not a target
        return builder.respond("answered", "overview", query.overview_lines(), sorted(query.entities))
    local = intent.kind in ("identity", "connectivity", "relation")
    if explicit:
        targets = explicit
    elif selection and (local or intent.pronoun):
        if len(selection) > 1 and intent.pronoun != "plural":
            return _clarify(builder, "ambiguous_selection", [
                f"Your question refers to one item, but {len(selection)} items are selected: "
                + ", ".join(query.tag(i) for i in selection) + ".",
                "Which one do you mean? Select one item, name its tag, or ask about “these” items."], selection)
        targets = selection
    elif intent.pronoun:
        return _clarify(builder, "no_selection", [
            "Your question refers to a selected item, but nothing is selected.",
            f"Select an item or name its tag (for example {builder.example_tag()}), then ask again."])
    else:
        targets = []

    if local and len(targets) > MAX_TARGETS:
        return _clarify(builder, "too_many", [
            f"The question names {len(targets)} items; Ask answers about at most {MAX_TARGETS} at a time.",
            "Ask about fewer items."], targets)
    if intent.kind == "identity" and targets:
        return _identity(builder, targets)
    if intent.kind == "connectivity":
        if not targets:
            return _clarify(builder, "no_target", [
                "Which item do you mean? Select it or name its tag "
                f"(for example “What is upstream and downstream of {builder.example_tag()}?”)."])
        return _connectivity(builder, targets)
    if intent.kind == "relation":
        if len(targets) != 2:
            return _clarify(builder, "no_target", [
                f"This question compares two items, but {len(targets)} were found. Name exactly two current tags."],
                targets)
        lines, ids = query.relation_lines(targets[0], targets[1], intent.relation)
        return builder.respond("answered", "relation", lines, ids, builder.unknowns(targets), targets=targets,
                               evidence=targets)

    evidence = list(dict.fromkeys(targets + selection))
    job = _job(query, request, digest, evidence, targets)
    if job is None:
        lines = ["A broader explanation needs the language model, but this project's Ask context exceeds the "
                 f"{MAX_PROMPT_BYTES // 1024} KiB prompt limit, so nothing was sent."] + _fallback(builder, evidence)
        prepared = builder.respond("unavailable", "general", lines, evidence, builder.unknowns(evidence[:MAX_TARGETS]))
        prepared.response["responder"]["error"] = {"code": "ask_context_limit",
                                                   "message": "the Ask context exceeds the prompt limit"}
        return prepared
    return Prepared(answer_id, request, project, digest, query, intent, tuple(targets), tuple(evidence), job=job)


def _fallback(builder, evidence):
    query = builder.query
    if evidence:
        lines = ["", f"Local facts from the current model, revision {builder.revision} (deterministic; they do not "
                     "answer the broader question):"]
        for record_id in evidence[:MAX_TARGETS]:
            lines += query.identity_lines(record_id)
        return lines
    example = builder.example_tag()
    return ["", f"Local questions still work without the language model, for example “What is {example}?” or "
                f"“What is upstream and downstream of {example}?”."]


def _model_notes(builder, job, metadata):
    model = metadata.get("model") or "the configured model"
    effort = metadata.get("effort")
    requested = effort.get("requested") if isinstance(effort, dict) else effort
    return [f"Broader explanation from {plain(model, 60)} (requested effort {plain(requested or 'unknown', 20)}) "
            "through the local Claude Code CLI on the Claude Max subscription, with no tools. The question, "
            f"selection, bounded history and a {max(1, job.prompt_bytes // 1024)} KiB context of revision "
            f"{builder.revision} were sent to it.",
            f"Cited IDs were checked against revision {builder.revision}; that does not prove every sentence correct. "
            "General explanations are not project data.", VERIFY_NOTE] + _display_note(
        builder.request, builder.project, builder.digest)


def finish(prepared, reply=None, error=None, cancelled=None, timed_out=False) -> dict:
    """The response for a model job. ``cancelled`` is ``"cancelled"`` or ``"shutdown"``; ``error`` an exception."""
    job = prepared.job
    builder = _Builder(prepared.answer_id, prepared.request, prepared.project, prepared.digest, prepared.query,
                       prepared.intent)
    query = prepared.query
    evidence = list(prepared.evidence)
    metadata = (reply or {}).get("metadata") if isinstance(reply, dict) else getattr(error, "metadata", None)
    responder = {"kind": "deterministic", "language_model": False, "query": "general", "model_called": True,
                 "bridge": safe_bridge(metadata), "prompt_bytes": job.prompt_bytes,
                 "prompt_sha256": job.prompt_sha256}
    if cancelled:
        reason = "the service is shutting down" if cancelled == "shutdown" else "the question was cancelled"
        responder["error"] = {"code": "ask_cancelled", "message": f"{reason}; the model process was stopped"}
        return make_response(prepared.answer_id, prepared.request, prepared.project, prepared.digest, "cancelled", [
            f"No answer: {reason} before an answer arrived. Nothing was changed."], [], [],
            [f"Asked about {source_label(builder.revision, prepared.digest)}."], responder)
    if timed_out or error is not None:
        if timed_out:
            code, message = "ask_timeout", "the language model did not answer within the Ask time limit"
        else:
            code = getattr(error, "code", None) or "ask_model_fault"
            message = plain(getattr(error, "message", None) or f"{type(error).__name__}: {error}", 300)
        responder["error"] = {"code": plain(code, 60), "message": message}
        lines = [f"A broader explanation needs the language model, which is unavailable: {message}."]
        lines += _fallback(builder, evidence)
        return make_response(prepared.answer_id, prepared.request, prepared.project, prepared.digest, "unavailable",
                             lines, references(query, evidence), builder.unknowns(evidence[:MAX_TARGETS]),
                             builder.failed_notes(job), responder)
    answer = reply.get("plan") if isinstance(reply, dict) else None
    problems = validate_answer(answer, job.allowed_ids)
    if problems:
        responder["error"] = {"code": "ask_model_rejected",
                              "message": f"{len(problems)} problem(s), first: {problems[0][0]}: {problems[0][1]}"}
        lines = [f"The language-model answer was rejected by local checks and is not shown ({len(problems)} "
                 f"problem(s), first: {plain(problems[0][0], 80)}: {plain(problems[0][1], 160)})."]
        lines += _fallback(builder, evidence)
        return make_response(prepared.answer_id, prepared.request, prepared.project, prepared.digest, "unavailable",
                             lines, references(query, evidence), builder.unknowns(evidence[:MAX_TARGETS]),
                             builder.failed_notes(job), responder)
    lines = [answer["summary"].strip()]
    cited = []
    for statement in answer["statements"]:
        tags = [query.tag(i) for i in dict.fromkeys(statement["reference_ids"])]
        cited += statement["reference_ids"]
        lines.append(f"{LABELS[statement['kind']]}: {statement['text'].strip()}" + (f" [{', '.join(tags)}]"
                                                                                  if tags else ""))
    status, query_kind = "answered", "general"
    if answer["outcome"] == "clarification":
        status, query_kind = "clarification", "clarification"
        lines.append(f"Question: {answer['clarification_question'].strip()}")
    elif answer["outcome"] == "change_request":
        status, query_kind = "clarification", "change_request"
        lines.append("Ask is read-only and changed nothing. Copy the request to Change to draft a reviewed proposal.")
    responder.update(kind="language_model", language_model=True, query=query_kind)
    unknowns = list(answer["unknowns"]) + [s["text"] for s in answer["statements"] if s["kind"] == "unknown"]
    return make_response(prepared.answer_id, prepared.request, prepared.project, prepared.digest, status, lines,
                         references(query, cited + evidence), unknowns, _model_notes(builder, job, metadata or {}),
                         responder)
