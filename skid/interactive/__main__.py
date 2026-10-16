"""``python -m skid.interactive --project PATH [--port 8765]``: serve one project on 127.0.0.1.

The project file must already exist (``python -m skid.cli sample PATH`` makes a
copy of the fixture, ``python -m skid.cli new PATH`` a genuinely empty project);
the service never creates or replaces it implicitly and refuses the repository
examples. Engineering plans may opt into equipment research (``skid.research``,
same installed Claude Code executable and Max route; ``--no-research`` hides it). Generation outputs go to the workflow root
(default ``<project stem>.workflow`` beside the project).
"""

from __future__ import annotations

import argparse
import signal
import sys

from ..planning import ClaudeCliPlanner, PlannerConfig
from ..planning.bridge import DEFAULT_CLI
from ..planning.bridge import DEFAULT_TIMEOUT_S as DEFAULT_PLANNER_TIMEOUT_S
from .generators import REAL_SUITE
from .server import make_server
from .service import DEFAULT_MAX_PLANS, DEFAULT_TIMEOUT_S, InteractiveService, ServiceStartError, make_researcher


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m skid.interactive",
                                     description="Local interactive command/build service for one project.")
    parser.add_argument("--project", required=True, help="existing project document (JSON)")
    parser.add_argument("--port", type=int, default=8765, help="loopback port (default 8765; 0 picks a free port)")
    parser.add_argument("--workflow-dir", help="output root (default: <project stem>.workflow beside the project)")
    parser.add_argument("--notes", help="manual description notes sidecar (default: <project stem>.notes.json)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-build time limit in seconds")
    parser.add_argument("--keep-builds", type=int, default=0, metavar="N",
                        help="delete the oldest sealed, unpublished build directories beyond N (default 0 keeps all)")
    parser.add_argument("--no-initial-build", action="store_true",
                        help="do not build at start-up when the published outputs are not current")
    parser.add_argument("--quiet", action="store_true", help="do not log requests")
    parser.add_argument("--planner-cli", default=DEFAULT_CLI, metavar="PATH",
                        help="installed Claude Code executable for engineering plans (never downloaded or installed)")
    parser.add_argument("--planner-timeout", type=float, default=DEFAULT_PLANNER_TIMEOUT_S, metavar="S",
                        help=f"engineering plan time limit in seconds (default {DEFAULT_PLANNER_TIMEOUT_S:g})")
    parser.add_argument("--max-plans", type=int, default=DEFAULT_MAX_PLANS, metavar="N",
                        help=f"concurrent engineering plans (default {DEFAULT_MAX_PLANS})")
    parser.add_argument("--research-timeout", type=float, default=None, metavar="S",
                        help="equipment research time limit in seconds (default: the research package's own)")
    parser.add_argument("--no-research", action="store_true",
                        help="disable the optional equipment-research step (the Research equipment option is hidden)")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.keep_builds < 0:
        parser.error("--keep-builds must be nonnegative")
    if not args.planner_timeout > 0 or args.max_plans < 1:
        parser.error("--planner-timeout must be positive and --max-plans at least 1")
    if args.research_timeout is not None and not args.research_timeout > 0:
        parser.error("--research-timeout must be positive")
    planner = ClaudeCliPlanner(PlannerConfig(cli_path=args.planner_cli, timeout_s=args.planner_timeout))
    researcher, research_note = False, "disabled (--no-research)"
    if not args.no_research:
        try:  # the same installed executable and Claude Max route as the planner; nothing is installed
            researcher, research_note = make_researcher(args.planner_cli, args.research_timeout), None
        except Exception as exc:  # noqa: BLE001 - research is optional; the service still starts without it
            researcher, research_note = False, f"unavailable ({type(exc).__name__}: {str(exc)[:120]})"
    try:
        service = InteractiveService(args.project, args.workflow_dir, suite=REAL_SUITE, notes_path=args.notes,
                                     timeout=args.timeout, keep_builds=args.keep_builds, planner=planner,
                                     max_plans=args.max_plans, researcher=researcher)
    except ServiceStartError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for item in exc.diagnostics[:10]:
            print(f"  [{item.get('code')}] {item.get('message')}", file=sys.stderr)
        return 2
    try:
        server = make_server(service, args.port, quiet=args.quiet)
    except OSError as exc:
        service.close()
        print(f"error: cannot listen on 127.0.0.1:{args.port} ({exc})", file=sys.stderr)
        return 1
    state = service.state()
    project = state["project"]
    print(f"Serving project {project['id']} revision {project['revision']} at {server.url}", flush=True)
    print(f"  document  {service.path}\n  outputs   {service.root}\n  state     {state['sync']['message']}",
          flush=True)
    described = planner.describe()
    print(f"  planner   {described['model']} effort {described['effort']} via {described['cli']}"
          f"{'' if described['cli_present'] else ' (NOT FOUND: engineering plans will report unavailable)'}",
          flush=True)
    if researcher is False:
        print(f"  research  {research_note}", flush=True)
    else:
        info = researcher.describe() if hasattr(researcher, "describe") else {}
        print(f"  research  {info.get('model', '?')} effort {info.get('effort', '?')} via "
              f"{info.get('cli', args.planner_cli)} (opt-in per engineering plan)", flush=True)
    if not args.no_initial_build and state["sync"]["state"] != "synchronized":
        service.request_build("service started")
        print("  build     started for the current revision", flush=True)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("stopping: cancelling any running build (it is never published)", flush=True)
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
