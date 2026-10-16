"""View adapters that render one read-only project snapshot.

``skid.adapters.pid`` renders the P&ID as SVG and runs anywhere the core runs.
``skid.adapters.blender`` runs inside Blender. The package root imports
nothing, so loading one adapter never requires the other's runtime.
"""
