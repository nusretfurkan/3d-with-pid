"""The generator suite the worker runs: geometry (display, CAD) and drafting (P&ID, legend, description).

``real_suite`` calls the contracted ``skid.geometry.build_geometry`` and
``skid.drafting.build_drafting``. Both are imported only when a build runs, so
the service starts, and reports an explicit failure, while either generator is
missing or broken. Tests pass a different suite to the worker; nothing reachable
over HTTP selects a suite.
"""

from __future__ import annotations

import importlib
import importlib.util

REAL_SUITE = "skid.interactive.generators:real_suite"
GENERATOR_MODULES = {"geometry": "skid.geometry", "drafting": "skid.drafting"}


class GeneratorUnavailable(RuntimeError):
    """A contracted generator module or function cannot be imported."""


def _function(module_name, function_name):
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # report import faults of the generator package itself truthfully
        raise GeneratorUnavailable(f"{module_name} cannot be imported ({type(exc).__name__}: {exc})") from exc
    function = getattr(module, function_name, None)
    if not callable(function):
        raise GeneratorUnavailable(f"{module_name} has no callable {function_name}")
    return function


class RealSuite:
    name = REAL_SUITE

    def geometry(self, project, output_dir, emit, cancelled):
        build_geometry = _function(GENERATOR_MODULES["geometry"], "build_geometry")
        return build_geometry(project, output_dir, emit=emit, cancelled=cancelled)

    def drafting(self, project, output_dir, emit, cancelled, manual_notes):
        build_drafting = _function(GENERATOR_MODULES["drafting"], "build_drafting")
        return build_drafting(project, output_dir, emit=emit, cancelled=cancelled, manual_notes=manual_notes)


def real_suite(options):
    if options:
        raise ValueError("the real generator suite takes no options")
    return RealSuite()


def availability() -> dict:
    """``{generator: None | reason}`` from module lookup only; importing (and CadQuery) happens in the worker."""
    report = {}
    for generator, module_name in GENERATOR_MODULES.items():
        try:
            found = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError) as exc:
            found, reason = False, f"{module_name} lookup failed ({exc})"
        else:
            reason = None if found else f"{module_name} is not installed in this checkout"
        report[generator] = None if found else reason
    return report
