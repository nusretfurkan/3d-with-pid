"""Equipment research: an exact-model, search-only Claude Code CLI researcher behind stream evidence checks.

- ``brief``: the ``skid.equipment.research/1`` schema, exact bounds, URL/text rules and ``validate_brief``.
- ``stream``: line-by-line stream verification and the WebSearch evidence ledger (real CLI result format).
- ``prompt``: research instructions; the request text travels as a JSON string, nothing else is sent.
- ``process``: owned child processes (the planner bridge's pattern, kept local; ``skid.planning`` is not
  imported) and ``ResearchError``.
- ``researcher``: ``ClaudeCliResearcher``, ``ResearchConfig``, ``validate_research_result``.

The planner bridge stays tool-free and unchanged. See ``docs/EQUIPMENT_RESEARCH.md``.
"""

from .brief import (BOUNDS, BRIEF_JSON_SCHEMA, BRIEF_SCHEMA_ID, OUTPUT_SCHEMA, SOURCE_ID_PATTERN,
                    TEXT_EVIDENCE_WARNING, cli_output_schema, is_public_url, text_problem, url_problem, validate_brief)
from .process import ERROR_CODES, ResearchError
from .prompt import research_prompt
from .researcher import (DEFAULT_CLI, RETRIEVAL_ROLE, VERIFIED_METADATA_FIELDS, ClaudeCliResearcher, ResearchConfig,
                         compose_brief, research_argv, validate_research_result)
from .stream import (EFFORT, MAX_SEARCH_CALLS, MODEL, RESEARCH_TOOLS, RETRIEVAL_MODEL, SEARCH_BUDGET,
                     SEARCH_STATUSES, parse_search_result)

__all__ = ["BOUNDS", "BRIEF_JSON_SCHEMA", "BRIEF_SCHEMA_ID", "DEFAULT_CLI", "EFFORT", "ERROR_CODES",
           "MAX_SEARCH_CALLS", "MODEL", "OUTPUT_SCHEMA", "RESEARCH_TOOLS", "RETRIEVAL_MODEL", "RETRIEVAL_ROLE",
           "SEARCH_BUDGET", "SEARCH_STATUSES", "SOURCE_ID_PATTERN", "TEXT_EVIDENCE_WARNING",
           "VERIFIED_METADATA_FIELDS", "ClaudeCliResearcher", "ResearchConfig", "ResearchError", "cli_output_schema",
           "compose_brief", "is_public_url", "parse_search_result", "research_argv", "research_prompt",
           "text_problem", "url_problem", "validate_brief", "validate_research_result"]
