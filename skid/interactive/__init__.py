"""Local interactive command/build service (workflow contract 0.2, ``docs/WORKFLOW_CONTRACTS.md``).

- ``service.InteractiveService``: one document writer, build queue, worker supervision, five-artifact publication.
- ``worker``: the separate generation process (geometry and drafting generators, event wrapping, verification).
- ``integrity``: manifest, identity, file, digest and active-content checks shared by worker and service.
- ``proposals``: bounded deterministic natural-language change proposals (no language model).
- ``server``: loopback HTTP transport; ``python -m skid.interactive --project PATH``.

See ``docs/INTERACTIVE.md``. The package root imports nothing, so the worker
loads only what it needs.
"""
