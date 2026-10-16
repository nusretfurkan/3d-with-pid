"""Shared CAD/display geometry: one recipe, OCCT solids, STEP and tessellated scenes.

``build_geometry(project, output_dir, emit=None, cancelled=None)`` is the
version 0.2 workflow interface (``docs/WORKFLOW_CONTRACTS.md``). It needs
CadQuery's OCP bindings (run with ``.venv-cad/bin/python``); the engineering
core stays independent of this package. See ``docs/CAD_SPIKE.md``.
"""

from .build import ADAPTER_VERSION, build_geometry, rebuild_from_recipe
from .snapshot import SnapshotError

__all__ = ["ADAPTER_VERSION", "SnapshotError", "build_geometry", "rebuild_from_recipe"]
