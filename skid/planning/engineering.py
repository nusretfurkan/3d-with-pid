"""Engineering-mode proposals: context -> planner -> strict plan checks -> one checked batch -> core dry-run.

``propose_engineering(document, text, proposal_id, planner, cancel)`` never
mutates the document and never raises for planner or plan faults: every
outcome is a proposal record. Only ``status: "ready"`` carries an executable
command, exactly one ``batch`` envelope that ``apply_command`` accepted
against the same revision and digest. ``unavailable`` (planner could not run
or was not verified), ``rejected`` (output failed checks), ``questions``
(mandatory decisions missing), ``unsupported`` and ``invalid`` (core
rejected the batch) have no commands. Geometry is never claimed checked here.

With ``research`` (a record already verified by ``evidence.verify_reply``)
the brief is added to the planner context as untrusted evidence and to the
proposal's ``research`` section, and translation cites it in the recorded
assumptions' sources. A ready researched plan states where its references go
(``research.persistence``): ``saved_with_assumptions`` lists the assumption
records whose source the command gives the research identity on apply;
``proposal_only`` (with a note) means no recorded assumption cites it (an
edit such as a rename records none; a service-written guided-route
assumption never cites research), so nothing in the model will cite it.
``research_failed_proposal`` is the refusal for a
requested research stage that did not produce a verified brief: research was
explicitly requested, so planning without sources is never substituted.
"""

from __future__ import annotations

import datetime
import hashlib
import json

from ..core import CommandError, apply_command, canonical_digest
from . import evidence
from .bridge import EFFORT, MODEL, PlannerError
from .context import build_context, example_definitions
from .prompt import build_prompt
from .schema import cli_schema, validate_plan
from .translate import TranslationError, batch_envelope, translate_plan

__all__ = ["LIMITATION", "MAX_TEXT", "PROPOSAL_TTL_S", "RESEARCH_LIMITATION", "clean_request", "failed_proposal",
           "propose_engineering", "research_failed_proposal"]

MAX_TEXT = 2000
PROPOSAL_TTL_S = 3600
LIMITATION = (f"Engineering plans come from {MODEL} with requested effort {EFFORT} (the CLI does not report the "
              "effort it applied) through the local Claude Code CLI on the Claude Max subscription, with no tools. "
              "The plan is untrusted input: it is checked against a strict "
              "schema, translated into one batch of existing commands and dry-run by the core validator. Review is "
              "required before apply. No process-design, safety, standards or fabrication check is made, and CAD/"
              "display geometry is checked only by the build that follows apply.")
GEOMETRY_NOTE = ("Not checked: geometry, CAD export and STEP reimport run only in the build after apply; a failure "
                 "there is reported as a failed build and the previous complete outputs are kept.")
RESEARCH_LIMITATION = ("Equipment research is a separate, search-only step that receives only the request text (no "
                       "project data). Its brief summarizes public web-search results as untrusted text evidence; no "
                       "image or CAD file is inspected or imported. It can support general characteristics, named "
                       "connections and subtype distinctions, never dimensions, duty, ratings or vendor data: every "
                       "proposed dimension remains an illustrative assumption.")
RESEARCH_NOT_RECORDED = ("No research reference will be saved in the model: no assumption this plan records cites the "
                         "research (an edit such as a rename records none), so the brief and its sources stay only in "
                         "this proposal review until it expires and are not kept with the project after apply, save "
                         "or reload.")
RECORDED = {True: "recorded in the model on apply", False: "not referenced by any operation; not recorded"}
UNAVAILABLE_CODES = ("planner_unavailable", "planner_auth", "planner_timeout", "planner_cancelled",
                     "planner_exit_status", "planner_error_result")
_SECTIONS = (("model", "entities"), ("model", "ports"), ("model", "connections"), ("model", "assumptions"),
             ("pid", "symbols"), ("pid", "edges"), ("physical", "placements"), ("physical", "routes"))


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds")


def _diagnostic(code, message, ids=(), severity="error"):
    return {"code": code, "severity": severity, "entity_ids": [str(i) for i in ids if i], "message": message}


def clean_request(text):
    """``(cleaned, None)`` or ``(None, reason)``. Newlines and tabs are kept; other control characters refuse."""
    if not isinstance(text, str):
        return None, "The request text must be a string."
    if any((ord(ch) < 32 and ch not in "\n\t\r") or ch in "  " for ch in text):
        return None, "The request contains control characters."
    cleaned = "\n".join(" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n")).strip()
    if not cleaned:
        return None, "The request is empty."
    if len(cleaned) > MAX_TEXT:
        return None, f"The request is longer than {MAX_TEXT} characters."
    return cleaned, None


def _changes(before, after):
    """Validator-derived impacts: records added, removed or changed by the dry-run."""
    impacts = []
    for section, collection in _SECTIONS:
        old, new = before[section][collection], after[section][collection]
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(k for k in set(old) & set(new) if json.dumps(old[k], sort_keys=True)
                         != json.dumps(new[k], sort_keys=True))
        label = f"{section}.{collection}"
        for verb, ids in (("added", added), ("removed", removed)):
            if ids:
                impacts.append({"ids": ids, "effect": f"{label} {verb}", "source": "validator"})
        for record_id in changed:
            fields = sorted(k for k in set(old[record_id]) | set(new[record_id])
                            if json.dumps(old[record_id].get(k), sort_keys=True)
                            != json.dumps(new[record_id].get(k), sort_keys=True)) \
                if isinstance(old[record_id], dict) and isinstance(new[record_id], dict) else []
            detail = f" ({', '.join(fields)})" if fields else ""
            status = new[record_id].get("status") if isinstance(new[record_id], dict) else None
            note = f"; now {status}" if status and section in ("pid", "physical") else ""
            impacts.append({"ids": [record_id], "effect": f"{label} changed{detail}{note}", "source": "validator"})
    return impacts


def _base(document, text, proposal_id, planner):
    project = document["current"]
    created = _now()
    return {
        "id": proposal_id, "mode": "engineering", "intent": "engineering_plan", "text": text,
        "expected_revision": project["revision"], "base_digest": canonical_digest(project), "status": None,
        "supported": False, "summary": "", "reason": None, "commands": [], "operations": [], "command_count": 0,
        "operation_count": 0, "assumptions": [], "proposed_assumptions": [], "unknowns": [], "questions": [],
        "impacts": [], "reasoning": [],
        "validation": {"accepted": False, "diagnostics": [], "warnings": [], "no_op": False, "geometry_checked": False,
                       "geometry_note": GEOMETRY_NOTE,
                       "checked_against": {"revision": project["revision"],
                                           "input_digest": canonical_digest(project)}},
        "planner": dict(planner.describe()) if hasattr(planner, "describe") else {"kind": "unknown"},
        "applied": False, "stale": False, "created_at": _iso(created),
        "expires_at": _iso(created + datetime.timedelta(seconds=PROPOSAL_TTL_S)), "limitation": LIMITATION,
        "research": {"requested": False, "status": "not_requested"},
    }


def _refuse(proposal, status, reason, diagnostics=()):
    planned = proposal["summary"]
    proposal.update(status=status, supported=False, commands=[], command_count=0, reason=reason)
    proposal["summary"] = f"Cannot apply ({status}): {reason} No change will be made." + (
        f" Planner's summary: {planned}" if planned else "")
    proposal["validation"]["diagnostics"] = list(diagnostics)
    return proposal


def failed_proposal(document, text, proposal_id, planner, reason) -> dict:
    """An ``unavailable`` proposal for a fault outside the planner's own error reporting (never a command)."""
    cleaned, _ = clean_request(text)
    proposal = _base(document, cleaned if cleaned is not None else (text if isinstance(text, str) else ""),
                     proposal_id, planner)
    return _refuse(proposal, "unavailable", f"Engineering planning failed: {reason}.",
                   [_diagnostic("planner_fault", reason, [document["current"]["id"]])])


def research_failed_proposal(document, text, proposal_id, planner, research, status, reason, diagnostics=(),
                             stale=False) -> dict:
    """A refusal for a requested research stage without a usable brief; the planner is not run.

    ``research`` is the proposal's research section (``requested`` true, a non-``verified`` status and an
    ``error``), or a verified section when the project changed during research (``stale``).
    """
    cleaned, _ = clean_request(text)
    proposal = _base(document, cleaned if cleaned is not None else (text if isinstance(text, str) else ""),
                     proposal_id, planner)
    proposal["research"] = dict(research, limitation=RESEARCH_LIMITATION)
    proposal["stale"] = bool(stale)
    return _refuse(proposal, status, reason, diagnostics)


def _research_persistence(research, operations):
    """Where a ready researched plan's references go: the assumption records the command gives the research
    identity in their source (kept with the model on apply), else only this proposal."""
    marker = f"Research {research['id']} "
    ids = sorted(aid for operation in operations for aid, record in (operation.get("assumptions") or {}).items()
                 if marker in str(record.get("source", "")))
    if ids:
        return {"status": "saved_with_assumptions", "assumption_ids": ids}
    return {"status": "proposal_only", "assumption_ids": [], "note": RESEARCH_NOT_RECORDED}


def _known_ids(project, translation):
    model, pid = project["model"], project["pid"]
    ids = set(model["entities"]) | set(model["ports"]) | set(model["connections"]) | set(pid["symbols"]) \
        | set(pid["edges"]) | {project["id"]}
    for item in translation["described"] if translation else ():
        ids.update(item["ids"])
    return ids


def propose_engineering(document, text, proposal_id, planner, cancel=None, examples=None, research=None) -> dict:
    cleaned, problem = clean_request(text)
    proposal = _base(document, cleaned if cleaned is not None else (text if isinstance(text, str) else ""),
                     proposal_id, planner)
    if research is not None:
        proposal["research"] = dict(evidence.public_record(research), limitation=RESEARCH_LIMITATION)
    if problem:
        return _refuse(proposal, "unsupported", problem)
    project = document["current"]
    examples = example_definitions() if examples is None else examples
    try:
        prompt = build_prompt(build_context(document, cleaned, examples, research=research))
    except (TypeError, ValueError) as exc:
        return _refuse(proposal, "unavailable", f"The planning context could not be built ({exc}).",
                       [_diagnostic("planner_context", str(exc)[:300], [project["id"]])])
    proposal["planner"]["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    proposal["planner"]["prompt_bytes"] = len(prompt.encode("utf-8"))
    try:
        reply = planner.run(prompt, cli_schema(), cancel)
    except PlannerError as exc:
        proposal["planner"].update(exc.metadata)
        status = "unavailable" if exc.code in UNAVAILABLE_CODES else "rejected"
        what = "is unavailable" if status == "unavailable" else "output was rejected"
        return _refuse(proposal, status, f"The engineering planner {what}: {exc.message}.",
                       [_diagnostic(exc.code, exc.message, [project["id"]])])
    proposal["planner"].update(reply.get("metadata") or {})
    plan = reply.get("plan")

    problems = validate_plan(plan)
    if problems:
        diagnostics = [_diagnostic("plan_schema", f"{path}: {message}") for path, message in problems[:20]]
        return _refuse(proposal, "rejected", f"The planner output does not match the plan schema ({len(problems)} "
                       f"problem(s), first: {problems[0][0]}: {problems[0][1]}).", diagnostics)
    proposal.update(summary=plan["summary"].strip(), reasoning=list(plan["reasoning"]),
                    unknowns=list(plan["unknowns"]),
                    questions=[dict(q) for q in plan["questions"]],
                    proposed_assumptions=[{"key": a["key"], "description": a["description"], "recorded": False}
                                          for a in plan["assumptions"]])
    planner_impacts = [dict(i, source="planner") for i in plan["impacts"]]
    withheld = len(plan["operations"])
    if plan["questions"]:
        proposal["impacts"] = planner_impacts
        return _refuse(proposal, "questions", f"The planner needs {len(plan['questions'])} decision(s) before "
                       f"anything can change{f'; {withheld} drafted operation(s) are withheld' if withheld else ''}. "
                       "Answer them in a new request.")
    if plan["outcome"] == "questions":
        return _refuse(proposal, "rejected", "The plan's outcome is 'questions' but it asks none.",
                       [_diagnostic("plan_inconsistent", "outcome questions without questions")])
    if plan["outcome"] == "unsupported" or not plan["operations"]:
        reason = "The planner reports that the request cannot be expressed with the supported operations." \
            if plan["outcome"] == "unsupported" else "The plan contains no operations."
        if plan["outcome"] == "unsupported" and withheld:
            reason += f" {withheld} operation(s) it listed are ignored."
        proposal["impacts"] = planner_impacts
        return _refuse(proposal, "unsupported", reason)

    try:
        translation = translate_plan(plan, project, proposal_id, examples, research=research, document=document)
    except TranslationError as exc:
        proposal["impacts"] = planner_impacts
        return _refuse(proposal, "rejected", f"The plan does not translate into valid operations "
                       f"({len(exc.diagnostics)} problem(s): {exc.diagnostics[0]['message']}).", exc.diagnostics)
    proposal["proposed_assumptions"] = translation["proposed_assumptions"]
    proposal["operations"] = translation["described"]
    proposal["operation_count"] = len(translation["operations"])
    envelope = batch_envelope(proposal_id, project["revision"], translation["operations"])
    known = _known_ids(project, translation)
    unknown_impacts = sorted({i for item in planner_impacts for i in item["ids"] if i not in known})
    proposal["impacts"] = [dict(item, ids=[i for i in item["ids"] if i in known]) for item in planner_impacts]
    proposal["assumptions"] = [f"{a['id']}: {a['description']} ({RECORDED[a['recorded']]})"
                               for a in translation["proposed_assumptions"]]
    proposal["unknowns"] = proposal["unknowns"] + [GEOMETRY_NOTE]
    try:
        result, warnings = apply_command(document, envelope)
    except CommandError as exc:
        proposal["impacts"] += [{"ids": [], "effect": "the core validator rejected the batch; nothing would change",
                                 "source": "validator"}]
        proposal["commands"] = []
        return _refuse(proposal, "invalid", "The core validator rejected the batch for this revision; it cannot be "
                       "applied.", exc.diagnostics)
    no_op = any(d.get("code") == "no_op" for d in warnings)
    proposal["impacts"] += [] if no_op else _changes(project, result["current"])
    if unknown_impacts:
        proposal["validation"]["warnings"].append(_diagnostic(
            "impact_unknown_id", f"the planner named unknown IDs in impacts: {', '.join(unknown_impacts[:10])}",
            severity="warning"))
    proposal.update(status="ready", supported=True, commands=[envelope], command_count=1)
    proposal["validation"].update(accepted=True, warnings=proposal["validation"]["warnings"] + list(warnings),
                                  no_op=no_op)
    if research is not None:
        proposal["research"]["persistence"] = _research_persistence(research, envelope["payload"]["operations"])
    return proposal
