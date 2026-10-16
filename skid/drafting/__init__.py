"""Drafting outputs for one project revision: P&ID sheet, exact legend, item index and process description.

The public entry point is ``build_drafting(project, output_dir, emit=None,
cancelled=None, manual_notes=None) -> {pid, legend, description}`` as frozen in
``docs/WORKFLOW_CONTRACTS.md`` (version 0.2). See ``docs/DRAFTING.md``.
"""

from .builder import OUTPUT_FILES, PREVIEW_DIR, build_drafting
from .paper import LEGEND_SHEET_ID, SHEET_ID, to_paper, zone_of, zone_ref

__all__ = [
    "LEGEND_SHEET_ID",
    "OUTPUT_FILES",
    "PREVIEW_DIR",
    "SHEET_ID",
    "build_drafting",
    "to_paper",
    "zone_of",
    "zone_ref",
]
