"""Helpers for the Blender adapter in ``skid.adapters.blender``.

``geometry`` and ``plan`` are pure Python and importable without Blender, so
the geometry contract can be tested with any Python 3.11+ interpreter.
``scene`` and ``preview`` import ``bpy`` and run only inside Blender.
``run_build`` is a Blender command-line entry point that builds a persisted
project into a blend file, a manifest, and an optional preview image.
"""
