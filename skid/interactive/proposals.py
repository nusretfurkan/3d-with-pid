"""Bounded, deterministic natural-language change proposals. No language model is involved.

``propose`` recognises a small fixed set of single-change phrasings and turns
one into exactly one existing command envelope for review:

- rename: ``rename P-101 to P-201``, ``change the tag of the pump to P-201``
- one typed dimension: ``set the tank height to 1.5 m``,
  ``set body length of P-101 to 480 mm`` (template parameters only, converted
  to metres; recorded with a new assumption naming the request)
- physical movement: ``move P-101 0.5 m in +x``, ``move the pump up 100 mm``,
  ``move P-101 to (2, 0, 0) m`` (placement only; incident routes become
  ``needs_reroute``, and no route points are invented)
- P&ID movement (the text must mention the symbol, P&ID, diagram or drawing):
  ``move the pump symbol right by 40``, ``move P-101 on the P&ID to (400, 300)``

Anything else, including compound requests, ambiguous references, line-tag
renames, insertion, deletion and rotation, yields an explicit unsupported
proposal with no commands. Equipment references resolve by tag, or by a kind
word (``pump``, ``tank``, ``check valve``...) only when exactly one entity
matches. Applying a proposal still goes through ``apply_command`` validation.
"""

from __future__ import annotations

import re

from ..core.validation import normalize_tag
from ..templates import get_template

MAX_TEXT = 500
MAX_MOVE_M = 50.0
MAX_DIMENSION_M = 50.0
MAX_DIAGRAM_MOVE = 2000.0
EXAMPLES = (
    "rename P-101 to P-201",
    "set the tank height to 1.5 m",
    "set body length of P-101 to 480 mm",
    "move P-101 0.5 m in +x",
    "move P-101 to (2, 0, 0) m",
    "move the pump symbol right by 40",
    "move P-101 on the P&ID to (400, 300)",
)
LIMITATION = ("Deterministic parser for one rename, one template dimension, or one physical or P&ID move; "
              "it is not a language model and does not infer engineering intent.")

_NUM = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"
_UNIT = r"(?P<unit>mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?|m|met(?:er|re)s?)"
_PLEASE = r"(?:please\s+)?"
_UNIT_FACTORS = {"mm": 0.001, "cm": 0.01, "m": 1.0}
_VERBS = re.compile(r"\b(rename|retag|re-tag|set|change|make|adjust|update|move|place|put|insert|add|delete|remove|"
                    r"rotate|turn|connect|disconnect|duplicate|copy|undo|redo|resize|replace)\b", re.IGNORECASE)
_UNSUPPORTED_VERBS = {
    "insert": "Valve insertion is available as a structured insert_valve command in the command form; this parser "
              "does not compose new equipment, ports or connections.",
    "add": "Adding equipment or connections is not supported by this parser; insert_valve is available in the "
           "structured command form.",
    "delete": "Deletion has no supported command in this version.",
    "remove": "Removal has no supported command in this version.",
    "duplicate": "Duplication has no supported command in this version.",
    "copy": "Duplication has no supported command in this version.",
    "rotate": "Rotation is not parsed; submit move_equipment or move_symbol with a full rotation through the command "
              "form.",
    "turn": "Rotation is not parsed; submit move_equipment or move_symbol with a full rotation through the command "
            "form.",
    "connect": "Connections change only through insert_valve in this version.",
    "disconnect": "Connections change only through insert_valve in this version.",
    "undo": "Use the Undo control (an undo command) instead of a proposal.",
    "redo": "Use the Redo control (a redo command) instead of a proposal.",
    "replace": "Replacing equipment has no supported command in this version.",
    "resize": "Name the parameter and value, for example 'set the tank height to 1.5 m'.",
}
_KIND_WORDS = (
    ("centrifugal_pump", ("pump", "centrifugal pump")),
    ("tank", ("tank", "vessel")),
    ("check_valve", ("check valve", "non-return valve", "non return valve", "nrv")),
    ("isolation_valve", ("isolation valve", "hand valve")),
    (None, ("valve",)),
    ("indicator", ("indicator", "gauge", "instrument")),
    ("boundary", ("boundary", "outlet boundary", "scope boundary", "battery limit")),
)
_PID_CONTEXT = re.compile(r"\b(symbol|p&id|pid|diagram|drawing|sheet)\b", re.IGNORECASE)
_PID_QUALIFIERS = re.compile(r"\s*\b(?:(?:on|in)\s+the\s+(?:p&id|pid|diagram|drawing|sheet)|symbol)\b", re.IGNORECASE)
_PHYSICAL_QUALIFIERS = re.compile(r"\s*\b(?:in\s+3d|in\s+the\s+(?:3d|physical)\s+(?:model|layout|view)|in\s+the\s+"
                                  r"model|physically)\b", re.IGNORECASE)
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./#-]{0,39}$")


class _Unsupported(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _clean_text(text):
    if not isinstance(text, str):
        raise _Unsupported("The request text must be a string.")
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
        raise _Unsupported("The request contains control characters.")
    text = " ".join(text.split()).strip()
    if not text:
        raise _Unsupported("The request is empty.")
    if len(text) > MAX_TEXT:
        raise _Unsupported(f"The request is longer than {MAX_TEXT} characters.")
    return text.rstrip(".!")


def _number(text, what, limit, positive=False):
    try:
        value = float(text)
    except ValueError:
        raise _Unsupported(f"{what} {text!r} is not a number.") from None
    if positive and value <= 0:
        raise _Unsupported(f"{what} must be positive.")
    if abs(value) > limit:
        raise _Unsupported(f"{what} {value:g} exceeds the parser's limit of {limit:g}.")
    return value


def _metres(value, unit):
    unit = unit.lower()
    key = "mm" if unit.startswith("mm") or unit.startswith("milli") else "cm" if unit.startswith(("cm", "centi")) \
        else "m"
    return round(value * _UNIT_FACTORS[key], 9) + 0.0


def _reference_text(ref):
    ref = ref.strip().strip("\"'")
    ref = re.sub(r"^(?:the|a|an)\s+", "", ref, flags=re.IGNORECASE)
    ref = re.sub(r"(?:'s|’s)$", "", ref)
    ref = re.sub(r"\s+(?:equipment|item)$", "", ref, flags=re.IGNORECASE)
    return ref.strip()


def _describe_entity(entity):
    return f"{entity['tag']} ({entity['id']})"


def resolve_entity(project, ref):
    """``(entity_id, interpretation)`` for a tag or a unique kind word; raise ``_Unsupported`` otherwise."""
    model = project["model"]
    text = _reference_text(ref)
    if not text:
        raise _Unsupported("No equipment was named.")
    key = normalize_tag(text)
    tagged = [eid for eid, entity in sorted(model["entities"].items()) if normalize_tag(entity["tag"]) == key]
    if len(tagged) == 1:
        return tagged[0], f"'{ref.strip()}' is the tag of {_describe_entity(model['entities'][tagged[0]])}"
    lines = [c for c in model["connections"].values() if normalize_tag(c["tag"]) == key]
    if lines:
        raise _Unsupported(f"{lines[0]['tag']} is a line (connection) tag; line tags have no rename or move command "
                           "in this version.")
    lowered = text.lower()
    observed = re.match(r"^(level|pressure|temperature|flow)\s+(?:indicator|gauge)$", lowered)
    if observed:
        matches = sorted(eid for eid, entity in model["entities"].items() if entity["kind"] == "indicator"
                         and observed.group(1) in str((entity.get("observes") or {}).get("variable", "")))
        if len(matches) == 1:
            return matches[0], f"'{text}' was interpreted as {_describe_entity(model['entities'][matches[0]])}, " \
                               "the only indicator observing that variable"
        if matches:
            raise _Unsupported(f"'{text}' is ambiguous: it matches "
                               f"{', '.join(model['entities'][m]['tag'] for m in matches)}. Name one tag.")
    for kind, words in _KIND_WORDS:
        if lowered in words:
            matches = sorted(eid for eid, entity in model["entities"].items()
                             if (entity["kind"] == kind if kind else entity["kind"].endswith("_valve")))
            if len(matches) == 1:
                return matches[0], f"'{text}' was interpreted as {_describe_entity(model['entities'][matches[0]])}, " \
                                   "the only matching item"
            if matches:
                listed = ", ".join(model["entities"][m]["tag"] for m in matches)
                raise _Unsupported(f"'{text}' is ambiguous: it matches {listed}. Name one tag.")
            raise _Unsupported(f"No {text} exists in this project.")
    known = ", ".join(sorted(entity["tag"] for entity in model["entities"].values()))
    raise _Unsupported(f"No equipment matches '{text}'. Known tags: {known}.")


def _incident_lines(project, entity_id):
    model = project["model"]
    ports = {pid for pid, port in model["ports"].items() if port["entity_id"] == entity_id}
    return sorted(c["tag"] for c in model["connections"].values()
                  if c["from_port_id"] in ports or c["to_port_id"] in ports)


def _new_assumption_id(proposal_id, project):
    base = f"asm-{proposal_id}"
    existing = set(project["model"]["assumptions"])
    candidate, index = base, 1
    while candidate in existing:
        index += 1
        candidate = f"{base}-{index}"
    return candidate


def _envelope(proposal_id, expected_revision, kind, payload, assumptions=None):
    command = {"id": f"cmd-{proposal_id}", "expected_revision": expected_revision, "type": kind, "payload": payload}
    if assumptions:
        command["assumptions"] = assumptions
    return command


# --- Intents ---------------------------------------------------------------------


def _rename(project, match, ctx):
    entity_id, interpretation = resolve_entity(project, match["ref"])
    tag = match["tag"].strip().strip("\"'“”").strip()
    if not _TAG.match(tag):
        raise _Unsupported(f"New tag {tag!r} must start with a letter or digit and use at most 40 letters, digits, "
                           "spaces or . _ / # -.")
    entity = project["model"]["entities"][entity_id]
    return {
        "intent": "rename",
        "summary": f"Rename {_describe_entity(entity)} to {tag}. Its ID, ports, connections, P&ID layout and physical "
                   "placement are unchanged.",
        "commands": [_envelope(ctx["id"], ctx["revision"], "rename_entity", {"entity_id": entity_id, "tag": tag})],
        "assumptions": [interpretation],
        "unknowns": [],
    }


def _parameter_phrases():
    names = set()
    for kind in ("tank", "centrifugal_pump", "isolation_valve", "check_valve", "indicator", "boundary"):
        names.update(get_template(kind, 1)["parameters"])
    return sorted((name.replace("_", " ") for name in names), key=len, reverse=True)


def _set_parameter(project, match, ctx):
    entity_id, interpretation = resolve_entity(project, match["ref"])
    entity = project["model"]["entities"][entity_id]
    name = "_".join(match["param"].lower().split())
    try:
        declarations = get_template(entity["kind"], entity["template_version"])["parameters"]
    except (KeyError, ValueError):
        raise _Unsupported(f"{entity['tag']} has no known template.") from None
    if name not in declarations:
        listed = ", ".join(n.replace("_", " ") for n in sorted(declarations))
        raise _Unsupported(f"{entity['tag']} ({entity['kind']}) has no parameter '{match['param']}'. Its parameters: "
                           f"{listed}.")
    value = _metres(_number(match["value"], "The value", MAX_DIMENSION_M * 1000, positive=True), match["unit"])
    if value > MAX_DIMENSION_M:
        raise _Unsupported(f"{value:g} m exceeds the parser's limit of {MAX_DIMENSION_M:g} m.")
    old = entity["parameters"].get(name, {})
    assumption_id = _new_assumption_id(ctx["id"], project)
    assumption = {
        "description": f"{entity['tag']} {name} set to {value:g} m by interactive request \"{ctx['text']}\". "
                       "User-specified prototype value; not a vendor, design or verified value.",
        "source": f"interactive proposal {ctx['id']} (deterministic parser)",
    }
    payload = {"entity_id": entity_id, "name": name,
               "quantity": {"value": value, "unit": "m", "assumption_id": assumption_id}}
    lines = _incident_lines(project, entity_id)
    unknowns = ["Whether this value suits the process or the equipment is not checked; only template bounds and "
                "model consistency are validated."]
    if lines:
        unknowns.append(f"If the change moves a nozzle, the incident routes ({', '.join(lines)}) are marked "
                        "needs_reroute and keep their old points; this proposal supplies no new route points.")
    previous = f"{old.get('value'):g} {old.get('unit')}" if isinstance(old.get("value"), (int, float)) else "unset"
    return {
        "intent": "set_parameter",
        "summary": f"Set {entity['tag']} {name} from {previous} to {value:g} m, recorded with new assumption "
                   f"{assumption_id}.",
        "commands": [_envelope(ctx["id"], ctx["revision"], "set_parameter", payload, {assumption_id: assumption})],
        "assumptions": [interpretation, f"The value is recorded as user-specified prototype assumption "
                                        f"{assumption_id}, not as a vendor or design value."],
        "unknowns": unknowns,
    }


def _axis_vector(match):
    if match.groupdict().get("updown"):
        return (0.0, 0.0, 1.0 if match["updown"].lower() == "up" else -1.0), match["updown"].lower()
    axis = match["axis"].replace(" ", "").lower()
    sign = -1.0 if axis.startswith("-") else 1.0
    vector = [0.0, 0.0, 0.0]
    vector["xyz".index(axis[-1])] = sign
    return tuple(vector), f"{'-' if sign < 0 else '+'}{axis[-1]}"


def _move_equipment(project, match, ctx):
    entity_id, interpretation = resolve_entity(project, match["ref"])
    entity = project["model"]["entities"][entity_id]
    placement = project["physical"]["placements"].get(entity_id)
    placed = placement is not None and placement.get("status") == "placed"
    groups = match.groupdict()
    assumptions = [interpretation]
    if groups.get("x") is not None:
        unit = groups.get("unit") or "m"
        position = [_metres(_number(match[a], f"{a}", MAX_MOVE_M * 1000), unit) for a in ("x", "y", "z")]
        if any(abs(v) > MAX_MOVE_M for v in position):
            raise _Unsupported(f"Positions beyond {MAX_MOVE_M:g} m are outside the parser's limit.")
        if not groups.get("unit"):
            assumptions.append("Coordinates without a unit were read as metres.")
        what = f"to ({', '.join(f'{v:g}' for v in position)}) m"
    else:
        if not placed:
            raise _Unsupported(f"{entity['tag']} has no resolved placement to move from; give an absolute position, "
                               f"for example 'move {entity['tag']} to (1, 0, 0) m'.")
        amount = _metres(_number(match["amount"], "The distance", MAX_MOVE_M * 1000), match["unit"])
        if abs(amount) > MAX_MOVE_M:
            raise _Unsupported(f"Moves beyond {MAX_MOVE_M:g} m are outside the parser's limit.")
        vector, label = _axis_vector(match)
        position = [round(p + amount * v, 9) + 0.0 for p, v in zip(placement["position_m"], vector)]
        what = f"by {amount:g} m along {label} to ({', '.join(f'{v:g}' for v in position)}) m"
    if placed:
        rotation = list(placement["rotation_deg"])
        kind = "move_equipment"
    else:
        rotation = [0.0, 0.0, 0.0]
        kind = "place_equipment"
        assumptions.append(f"{entity['tag']} had no placement; rotation [0, 0, 0] is assumed.")
    assumption_id = _new_assumption_id(ctx["id"], project)
    assumption = {
        "description": f"{entity['tag']} placed at {position} m by interactive request \"{ctx['text']}\". "
                       "Manual prototype layout choice; not a verified installation position.",
        "source": f"interactive proposal {ctx['id']} (deterministic parser)",
    }
    payload = {"entity_id": entity_id, "placement": {"position_m": position, "rotation_deg": rotation,
                                                     "status": "placed", "manual": True,
                                                     "assumption_id": assumption_id}}
    lines = _incident_lines(project, entity_id)
    unknowns = ["Clearances, supports and collisions at the new position are not checked."]
    if lines:
        unknowns.insert(0, f"Incident routes {', '.join(lines)} become needs_reroute with their old points retained; "
                           "this proposal supplies no new route points.")
    return {
        "intent": kind,
        "summary": f"Move {_describe_entity(entity)} physically {what}. Process connectivity and the P&ID symbol are "
                   "unchanged.",
        "commands": [_envelope(ctx["id"], ctx["revision"], kind, payload, {assumption_id: assumption})],
        "assumptions": assumptions + [f"The new position is recorded as manual layout assumption {assumption_id}."],
        "unknowns": unknowns,
    }


def _move_symbol(project, match, ctx):
    entity_id, interpretation = resolve_entity(project, match["ref"])
    entity = project["model"]["entities"][entity_id]
    symbols = sorted(sid for sid, symbol in project["pid"]["symbols"].items() if symbol["entity_id"] == entity_id)
    if len(symbols) != 1:
        raise _Unsupported(f"{entity['tag']} has {len(symbols)} P&ID occurrences ({', '.join(symbols) or 'none'}); "
                           "the parser moves only a single occurrence. Use move_symbol in the command form.")
    symbol = project["pid"]["symbols"][symbols[0]]
    groups = match.groupdict()
    if groups.get("x") is not None:
        x = _number(match["x"], "x", 10000)
        y = _number(match["y"], "y", 10000)
        what = f"to ({x:g}, {y:g})"
    else:
        amount = _number(match["amount"], "The distance", MAX_DIAGRAM_MOVE)
        direction = match["dir"].lower()
        dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[direction]
        x, y = symbol["x"] + dx * amount, symbol["y"] + dy * amount
        what = f"{amount:g} drawing units {direction} to ({x:g}, {y:g})"
    payload = {"symbol_id": symbols[0], "x": float(x), "y": float(y), "rotation_deg": float(symbol["rotation_deg"])}
    return {
        "intent": "move_symbol",
        "summary": f"Move the P&ID symbol {symbols[0]} of {entity['tag']} {what} (stored diagram units; +y is down). "
                   "Physical placement and connectivity are unchanged.",
        "commands": [_envelope(ctx["id"], ctx["revision"], "move_symbol", payload)],
        "assumptions": [interpretation, "Rotation is kept at its stored value."],
        "unknowns": ["Guided diagram edges are regenerated; manual edge paths are kept and flagged for repair if their "
                     "endpoints moved. Sheet-zone references follow the new position."],
    }


def _patterns():
    params = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in _parameter_phrases())
    flags = re.IGNORECASE
    rename = [
        rf"^{_PLEASE}(?:rename|retag|re-tag)\s+(?P<ref>.+?)\s+(?:to|as|->|→)\s+(?P<tag>.+)$",
        rf"^{_PLEASE}(?:change|set|update)\s+(?:the\s+)?tag\s+(?:of|for)\s+(?P<ref>.+?)\s+to\s+(?P<tag>.+)$",
        rf"^{_PLEASE}(?:change|set|update)\s+(?P<ref>.+?)(?:'s)?\s+tag\s+to\s+(?P<tag>.+)$",
    ]
    parameter = [
        rf"^{_PLEASE}(?:set|change|make|adjust|update)\s+(?:the\s+)?(?P<param>{params})\s+(?:of|for)\s+(?P<ref>.+?)"
        rf"\s+(?:to|=|as)\s+(?P<value>{_NUM})\s*{_UNIT}$",
        rf"^{_PLEASE}(?:set|change|make|adjust|update)\s+(?P<ref>.+?)\s+(?P<param>{params})\s+(?:to|=|as)\s+"
        rf"(?P<value>{_NUM})\s*{_UNIT}$",
    ]
    physical = [
        rf"^{_PLEASE}(?:move|place|put)\s+(?P<ref>.+?)\s+(?:to|at)\s+\(?\s*(?:x\s*=\s*)?(?P<x>{_NUM})\s*,?\s+"
        rf"(?:y\s*=\s*)?(?P<y>{_NUM})\s*,?\s+(?:z\s*=\s*)?(?P<z>{_NUM})\s*\)?\s*{_UNIT}?$",
        rf"^{_PLEASE}(?:move|place|put)\s+(?P<ref>.+?)\s+(?:to|at)\s+\(\s*(?P<x>{_NUM})\s*,\s*(?P<y>{_NUM})\s*,\s*"
        rf"(?P<z>{_NUM})\s*\)\s*{_UNIT}?$",
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?:by\s+)?(?P<amount>{_NUM})\s*{_UNIT}\s+(?:in\s+|along\s+|towards?\s+)?"
        rf"(?:the\s+)?(?P<axis>[+-]?\s?[xyz])(?:\s*(?:axis|direction))?$",
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?:along\s+|in\s+)?(?P<axis>[+-]?[xyz])\s+(?:by\s+)?(?P<amount>{_NUM})\s*"
        rf"{_UNIT}$",
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?P<updown>up|down)(?:wards)?\s+(?:by\s+)?(?P<amount>{_NUM})\s*{_UNIT}$",
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?:by\s+)?(?P<amount>{_NUM})\s*{_UNIT}\s+(?P<updown>up|down)(?:wards)?$",
    ]
    diagram = [
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?:to|at)\s+\(?\s*(?:x\s*=\s*)?(?P<x>{_NUM})\s*,?\s+(?:y\s*=\s*)?"
        rf"(?P<y>{_NUM})\s*\)?$",
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?:to|at)\s+\(\s*(?P<x>{_NUM})\s*,\s*(?P<y>{_NUM})\s*\)$",
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?:by\s+)?(?P<amount>{_NUM})\s*(?:units?|du)?\s+(?P<dir>left|right|up|down)$",
        rf"^{_PLEASE}move\s+(?P<ref>.+?)\s+(?P<dir>left|right|up|down)\s+(?:by\s+)?(?P<amount>{_NUM})\s*(?:units?|du)?$",
    ]
    compile_all = lambda items: [re.compile(p, flags) for p in items]  # noqa: E731
    return {"rename": compile_all(rename), "parameter": compile_all(parameter), "physical": compile_all(physical),
            "diagram": compile_all(diagram)}


_PATTERNS = None


def _interpret(project, text, ctx):
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _patterns()
    verbs = [v.lower() for v in _VERBS.findall(text)]
    if len(verbs) > 1:
        raise _Unsupported(f"This looks like a compound request ({', '.join(verbs)}). Proposals carry one change at a "
                           "time so that nothing is partially applied; submit each change separately.")
    if verbs and verbs[0] in _UNSUPPORTED_VERBS:
        raise _Unsupported(_UNSUPPORTED_VERBS[verbs[0]])
    if _PID_CONTEXT.search(text) and verbs and verbs[0] == "move":
        stripped = " ".join(_PID_QUALIFIERS.sub(" ", text).split())
        for pattern in _PATTERNS["diagram"]:
            match = pattern.match(stripped)
            if match:
                return _move_symbol(project, match, ctx)
        raise _Unsupported("P&ID moves are understood as 'move <tag> symbol <left|right|up|down> by <n>' or "
                           "'move <tag> on the P&ID to (<x>, <y>)'.")
    for pattern in _PATTERNS["rename"]:
        match = pattern.match(text)
        if match:
            return _rename(project, match, ctx)
    for pattern in _PATTERNS["parameter"]:
        match = pattern.match(text)
        if match:
            return _set_parameter(project, match, ctx)
    stripped = " ".join(_PHYSICAL_QUALIFIERS.sub(" ", text).split())
    for pattern in _PATTERNS["physical"]:
        match = pattern.match(stripped)
        if match:
            return _move_equipment(project, match, ctx)
    raise _Unsupported("The request matches none of the supported single-change forms.")


def propose(project: dict, text, expected_revision: int, proposal_id: str) -> dict:
    """Return ``{id, expected_revision, text, summary, commands, assumptions, unknowns, supported, ...}``."""
    base = {"id": proposal_id, "expected_revision": expected_revision, "text": text if isinstance(text, str) else "",
            "parser": "deterministic-v1", "limitation": LIMITATION}
    try:
        cleaned = _clean_text(text)
        base["text"] = cleaned
        result = _interpret(project, cleaned, {"id": proposal_id, "revision": expected_revision, "text": cleaned})
    except _Unsupported as exc:
        return dict(base, supported=False, intent=None, summary=f"Unsupported request: {exc.reason} No change will "
                    "be made.", commands=[], assumptions=[], unknowns=[], reason=exc.reason, examples=list(EXAMPLES))
    return dict(base, supported=True, **result)
