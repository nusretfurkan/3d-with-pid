"""Bounded, deterministic classification of an Ask question (no language model).

``classify(masked)`` reads the question with every resolved mention replaced by
``X`` (``query.Mentions.masked``) and returns an ``Intent``:

- ``change_request``: an imperative edit (rename, move, set, insert, ...). Ask
  never plans or applies it; the client may copy it to Change as a draft.
- ``historical``: a question expressly about an earlier revision, an old tag
  or a prior state, which the current snapshot cannot answer.
- ``identity``, ``connectivity``, ``relation`` and ``overview``: simple,
  anchored forms answered locally from the model.
- ``general``: everything else, including any question asking why, whether it
  should, or how something works. It needs the language model.

Flags: ``expanded`` (trace / full path / all / which items are),
``directions`` (upstream and/or downstream), ``pronoun`` (``singular``,
``plural`` or ``None``), ``relation`` (``upstream``, ``downstream`` or
``between``), ``revisions`` (explicit ``rN`` numbers) and ``unnamed``
(tag-like subjects without a hyphen, such as ``tk101``, that resolved to
nothing). Matching is conservative: an unrecognized form is ``general``,
never a guessed local answer. Operating-behaviour words (closes, starts,
happens, ...) always make a question ``general``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["CHANGE_VERBS", "Intent", "classify"]

CHANGE_VERBS = frozenset((
    "add", "adjust", "apply", "build", "change", "connect", "copy", "create", "decrease", "delete", "disconnect",
    "duplicate", "edit", "enlarge", "extend", "increase", "insert", "lengthen", "lower", "make", "modify", "move",
    "place", "put", "raise", "rebuild", "redo", "reduce", "relabel", "remove", "rename", "reopen", "replace",
    "reroute", "resize", "retag", "rotate", "route", "save", "scale", "set", "shift", "shorten", "shrink", "swap",
    "undo", "update", "publish", "regenerate", "generate", "draw",
))
_PROJECT_NOUNS = (r"(?:skid|project|system|model|plant|process|drawing|diagram|sheet|p&id|pid|unit|package|design|"
                  r"document|revision|view|page|pane|workspace|app|tool|answer|question|assistant)")
_POLITE = re.compile(r"^(?:(?:please|kindly|hey|hi|hello|ok|okay|so|now)\b[,!:]?\s*)+")
_ASK_PREFIX = re.compile(r"^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|"
                         r"i\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+|i'd\s+like\s+(?:you\s+)?to\s+|"
                         r"let'?s\s+|go\s+ahead\s+and\s+|try\s+to\s+)")
_TELL = re.compile(r"^(?:(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?(?:tell|show|give)\s+me\s+"
                   r"(?:(?:more\s+)?about\s+)?|(?:i\s+(?:want|need|would\s+like)\s+to\s+know|do\s+you\s+know|"
                   r"i\s+wonder)\s+)")
_HISTORY = re.compile(
    r"\b(?:previous(?:ly)?|earlier|formerly|originally|used\s+to|history|historical|prior\s+(?:revision|version|"
    r"state|tag|name)|before\s+(?:the\s+|my\s+|that\s+|this\s+|it\s+was\s+)?(?:rename|renaming|renamed|change|"
    r"edit|move|update|undo|redo|revision)|old(?:er)?\s+(?:revision|version|tag|name|sheet|drawing|publication|"
    r"state|model|value)|last\s+(?:revision|version|publication|build)|(?:was|were)\s+(?:it|this|that|x|they)\s+"
    r"(?:called|named|tagged|labell?ed)|what\s+(?:was|were)\b|did\s+(?:it|this|that|x|they)\s+(?:use\s+to|have))")
_REVISION = re.compile(r"\b(?:r|rev|revision)\s*#?\s*(\d{1,9})\b")
_SINGULAR = re.compile(r"\b(?:it|its|it's|this|that|the\s+selected\s+(?:item|one|equipment|line|port|entity|object|"
                       r"thing)|selected\s+(?:item|one|equipment|line|port|entity))\b(?!\s+" + _PROJECT_NOUNS + r"\b)")
_PLURAL = re.compile(r"\b(?:they|them|their|these|those|both|each\s+of\s+them|the\s+selection|(?:the\s+)?selected\s+"
                     r"(?:items|ones|entities|objects|lines|ports))\b(?!\s+" + _PROJECT_NOUNS + r"\b)")
_SUBJECT = (r"(?:x(?:\s*(?:,|and|&)\s*x)*|it|this|that|these|those|they|them|both|the\s+selection|"
            r"(?:the\s+)?selected\s+\w+|this\s+\w+|that\s+\w+|these\s+\w+|those\s+\w+)")
_ASPECTS = (r"(?:ports?|nozzles?|inlets?|outlets?|dimensions?|parameters?|placement|position|location|coordinates?|"
            r"origin|assumptions?|tag|kind|type|identity|details|properties)")
_IDENTITY = [re.compile(pattern) for pattern in (
    r"^(?:what|who|which)(?:\s+is|\s+are|'s|s)\s+(?:the\s+)?" + _SUBJECT
    + r"(?:\s+(?:item|items|one|ones|equipment|line|lines|port|thing|tag|entity|exactly|again))?$",
    r"^(?:what|which)\s+(?:kind|type|sort)\s+of\s+[a-z&\s]{1,30}?\s*(?:is|are)\s+" + _SUBJECT + r"$",
    r"^(?:describe|identify|summari[sz]e|explain\s+what|details\s+(?:of|on|for|about)|info(?:rmation)?\s+"
    r"(?:on|about|for)|about|what\s+do\s+(?:you|we)\s+know\s+about)\s+(?:the\s+)?" + _SUBJECT
    + r"(?:\s+(?:is|are))?$",
    r"^" + _SUBJECT + r"$",
    r"^where\s+(?:is|are)\s+(?:the\s+)?" + _SUBJECT + r"(?:\s+(?:placed|located|positioned))?$",
    r"^what\s+(?:does|do)\s+" + _SUBJECT + r"\s+(?:measure|observe|monitor|indicate|watch|sense|read)$",
    r"^(?:what|which)\s+(?:is|are)\s+(?:the\s+)?" + _ASPECTS + r"\s+(?:of|for|on)\s+(?:the\s+)?" + _SUBJECT + r"$",
    r"^(?:what\s+(?:is|are)\s+)?(?:the\s+)?" + _SUBJECT + r"(?:'s|s)?\s+" + _ASPECTS + r"$",
)]
_RELATION = [(re.compile(pattern), kind) for pattern, kind in (
    (r"^(?:is|are)\s+x\s+(?:directly\s+|immediately\s+)?(upstream|downstream)\s+(?:of|from)\s+x$", None),
    (r"^(?:what|which\s+\w+)\s+(?:is|are|lies|lie|sits?)\s+between\s+x\s+and\s+x$", "between"),
    (r"^(?:what\s+is\s+the\s+)?(?:flow\s+)?(?:path|route)\s+(?:from\s+|between\s+)?x\s+(?:to|and)\s+x$", "between"),
    (r"^how\s+(?:is|are|does|do)\s+x\s+connect(?:ed)?\s+(?:to|with)\s+x$", "between"),
    (r"^(?:is|are)\s+x\s+(?:and\s+x\s+)?connected(?:\s+(?:to|with)\s+x)?$", "between"),
)]
_OVERVIEW = [re.compile(pattern) for pattern in (
    r"^(?:list|show|enumerate|name)\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?(?:items|equipment|entities|lines|connections|"
    r"instruments|tags|components)(?:\s+(?:in|on|of)\s+(?:the\s+|this\s+)?\w+)?$",
    r"^what\s+(?:items|equipment|entities|lines|connections|instruments|tags|components)\s+(?:are|is)\s+"
    r"(?:there|in|on)\b[\w\s&]*$",
    r"^what(?:'s|\s+is)\s+in\s+(?:the|this)\s+" + _PROJECT_NOUNS + r"$",
    r"^what\s+(?:is|does)\s+(?:the|this)\s+" + _PROJECT_NOUNS + r"(?:\s+(?:contain|have|include))?$",
)]
_UNKNOWN_SUBJECT = re.compile(r"(?:^(?:what|who|which)\s+(?:is|are)\s+(?:the\s+)?|\b(?:of|from|to|for|about|into)\s+"
                              r"(?:the\s+)?)([a-z][a-z0-9_./#-]*\d[a-z0-9_./#-]*)(?=$|[\s,;?!])")
_NOT_A_TAG = re.compile(r"^(?:dn|pn|nps|iso|isa|iec|en|din|api|asme|ansi|bs|jis|sch|cl|class|rev|r|utf|sha|step)"
                        r"[-.]?\d|^\d")
_GENERAL = re.compile(
    r"\b(?:why|should|shouldn't|could|would|might|recommend\w*|suggest\w*|advis\w*|improv\w*|optimi[sz]\w*|explain|"
    r"what\s+if|safe|safety|hazard\w*|risk\w*|adequate|suitable|appropriate|enough|siz(?:e|ed|es|ing)|capacity|"
    r"duty|rates?|npsh|head|power|efficien\w*|compar\w*|differen\w*|differ|versus|vs|purpose|role|function\w*|"
    r"works?|operat\w*|best|better|worse|problems?|issues?|fail\w*|cause\w*|effects?|impacts?|calculat\w*|"
    r"estimat\w*|typical\w*|normal(?:ly)?|usual\w*|standards?|codes?|complian\w*|comply|cost\w*|prices?|vendors?|"
    r"materials?|temperature|pressure\s+drop|how\s+(?:does|do|would|should|can|could|many|much)|happen\w*|"
    r"clos(?:e|es|ed|ing)|open(?:s|ed|ing)?|start\w*|stop\w*|trip\w*|runs|running|shut\w*|leak\w*|block\w*)\b")
_CONNECTIVITY = re.compile(
    r"\b(?:upstream|downstream|connected|connects?|connections?|connecting|feeds?|fed|feeding|supplies|supplied|"
    r"flows?|flowing|comes?\s+from|goes?|go|leads?|discharges?\s+(?:to|into)|drains?\s+(?:to|into)|neighbou?rs?|"
    r"attached|linked|before|after|preceding|following|trace|tracing|path|paths)\b")
_EXPANDED = re.compile(
    r"\b(?:trace|tracing|full|entire|whole|complete|all\s+the\s+way|end\s*-?\s*to\s*-?\s*end|everything|"
    r"ultimately|eventually|expand(?:ed)?|chain|path|paths|sequence|all|which\s+\w+s\s+(?:are|lie|sit))\b")


@dataclass(frozen=True)
class Intent:
    kind: str
    expanded: bool = False
    directions: tuple = ("upstream", "downstream")
    pronoun: str | None = None
    relation: str | None = None
    revisions: tuple = field(default_factory=tuple)
    historical: bool = False
    unnamed: tuple = field(default_factory=tuple)  # tag-like subjects without a hyphen (for example "tk101")


def _normalize(text):
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = " ".join(text.casefold().split())
    text = re.sub(r"\s+([,?!.;:])", r"\1", text)
    return text.strip(" ?!.;:\"'")


def _strip(text):
    previous = None
    while previous != text:
        previous = text
        text = _POLITE.sub("", text)
        text = _TELL.sub("", text).strip()
    return text


def _pronoun(text):
    if _PLURAL.search(text):
        return "plural"
    if _SINGULAR.search(text):
        return "singular"
    return None


def classify(masked) -> Intent:
    """Classify a masked question (resolved mentions replaced by ``X``)."""
    text = _normalize(masked)
    text = _POLITE.sub("", text).strip()
    rest = text[_ASK_PREFIX.match(text).end():] if _ASK_PREFIX.match(text) else text
    first = re.match(r"[a-z]+", rest)
    revisions = tuple(int(n) for n in _REVISION.findall(text))
    historical = bool(_HISTORY.search(text))
    pronoun = _pronoun(text)
    if first and first.group(0) in CHANGE_VERBS:
        return Intent("change_request", pronoun=pronoun, revisions=revisions, historical=historical)
    body = _strip(text)
    up, down = "upstream" in body, "downstream" in body
    directions = ("upstream",) if up and not down else ("downstream",) if down and not up else (
        "upstream", "downstream")
    expanded = bool(_EXPANDED.search(body))
    unnamed = tuple(dict.fromkeys(token for token in _UNKNOWN_SUBJECT.findall(body) if not _NOT_A_TAG.match(token)))
    common = {"pronoun": pronoun, "revisions": revisions, "historical": historical, "unnamed": unnamed}
    if historical:
        return Intent("historical", expanded=expanded, directions=directions, **common)
    for pattern, kind in _RELATION:
        match = pattern.match(body)
        if match:
            return Intent("relation", directions=directions, relation=kind or match.group(1), **common)
    if any(pattern.match(body) for pattern in _OVERVIEW):
        return Intent("overview", **common)
    if any(pattern.match(body) for pattern in _IDENTITY):
        return Intent("identity", **common)
    if _GENERAL.search(body):
        return Intent("general", expanded=expanded, directions=directions, **common)
    if _CONNECTIVITY.search(body):
        return Intent("connectivity", expanded=expanded, directions=directions, **common)
    return Intent("general", expanded=expanded, directions=directions, **common)
