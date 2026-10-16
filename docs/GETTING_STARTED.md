# Getting started

From a clean checkout to a working core, the five-output workspace, and the checks. Every step after the core is optional.

## 1. Requirements

| Need | For | Notes |
| --- | --- | --- |
| Linux or macOS (POSIX) | everything | Persistence and the service use `flock`, process groups and POSIX signals. Windows is not supported. |
| Python 3.12 | everything | The core uses only the standard library. The CAD requirements are pinned for Python 3.12. |
| Git | cloning | |
| Blender (optional) | the offline `.blend` bundle and its integration tests | Any executable you point to with `--blender` or `SKID_BLENDER`. |
| Node.js 22 and npm (optional) | UI unit tests; Playwright browser checks | Node.js 22 is the verified target; older versions are untested (the quoted test glob below relies on `node --test` glob support). The web UI itself needs **no** npm install and no build step; Three.js 0.170.0 is vendored under `web/vendor/`. |
| Claude Code CLI with a Claude Max login (optional) | Engineering plan, broad Ask and equipment research | See [section 6](#6-optional-ai-planning). Not needed for anything else. |

## 2. Clone and create an environment

```sh
git clone <repository-url> equipment-3d-pid
cd equipment-3d-pid
python3.12 -m venv .venv-cad
. .venv-cad/bin/activate
python --version            # Python 3.12.x
```

The examples in these docs assume the environment is active, so `python` is your Python 3.12, and that you run commands from the repository root. The name `.venv-cad` matches the browser-test default; with another location, set `SKID_PYTHON=path/to/python` for those tests. The environment directory is ignored by Git.

## 3. Core: standard library only

No packages are needed yet.

```sh
mkdir -p work
python -m skid.cli sample work/skid.json        # copy of the example; refuses to overwrite
python -m skid.cli validate work/skid.json      # VALID, exit 0
python -m skid.cli lists work/skid.json         # equipment and line lists from the model
python -m skid.cli new work/blank.json --name "My study"   # a genuinely empty project
```

`sample` and `new` refuse to overwrite an existing file (exit 4), so running this block a second time stops at the first line; use new file names instead.

Continue with the [reference walkthrough](REFERENCE_EXAMPLE.md#hands-on-edits-standard-library-only) (rename, typed edit, moves, routes, undo/redo) and the [blank-project walkthrough](INPUT_CONTRACTS.md#worked-example-blank-project-to-complete-model). Each works in its own fresh subdirectory (`work/reference-manual/` and `work/contract-example/`), so they do not touch `work/skid.json` or `work/blank.json` from this section.

The standalone drafting generator also needs nothing extra:

```sh
python -m skid.drafting --project work/skid.json --out work/drafting    # pid.svg, legend.svg, item-index.json, description.*
```

It exits `0` complete, `2` pending, `1` failed. Its output is not a publication and is not compared with later edits.

## 4. Interactive five-output workspace

Install the CAD requirements into the active environment:

```sh
python -m pip install -r requirements-cad.txt
python -c "import cadquery; print(cadquery.__version__)"     # 2.6.1
```

`requirements-cad.txt` pins `cadquery==2.6.1` and applies `constraints-cad.txt`. That file records the exact versions of one audited environment: CPython 3.12 on macOS arm64. A clean install on another platform may need different wheels or fail. Record `python -m pip freeze` with any results you report. Dependency licences are listed in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

Start the service on a project file you own. It refuses anything under `examples/`, and it never creates a project:

```sh
python -m skid.cli sample work/demo.json
python -m skid.interactive --project work/demo.json --port 0 --keep-builds 12
# prints http://127.0.0.1:<port>/ ; Ctrl-C stops it (a running build is cancelled, never published)
```

The service binds only to `127.0.0.1`. It writes outputs to `work/demo.workflow/` and reads optional manual notes from `work/demo.notes.json`. The first build of the sample takes a while, because CAD export and STEP reimport run in a separate worker process. The views show real progressive previews; nothing is published until all five outputs verify. Without `--keep-builds`, every build directory is kept, and they add up to tens of megabytes each.

In the browser, the toolbar and each pane show source revisions and a status. Only **synchronized** means the five verified outputs match the current revision. *Pending*, *stale*, *failed*, *invalid* and *blocked* are explained in [troubleshooting](TROUBLESHOOTING.md#status-badges-and-what-to-do).

Other entry points that need the CAD environment:

```sh
python -m skid.geometry build work/skid.json work/geometry      # scene.json, model.step, verification; exit 0 also when PENDING
python -m skid.geometry recipe examples/recipes/horizontal_drum.recipe.json work/drum
```

## 5. Run the checks

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

Without CadQuery installed, the CAD-dependent tests are skipped with a stated reason. Without `SKID_BLENDER`, the real-Blender integration test is skipped. Read the summary line: a run with skips does not cover those parts. To include Blender:

```sh
SKID_BLENDER=/path/to/blender python -m unittest discover -s tests -p 'test_*.py'
/path/to/blender --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py
```

Some environments, such as sandboxes and headless machines without a GPU, can prevent Blender from starting; see [troubleshooting](TROUBLESHOOTING.md#blender).

The reproducible reference run records its evidence in a directory you choose ([details](REFERENCE_EXAMPLE.md#reference-script)):

```sh
python scripts/reference.py --output work/reference-core
python scripts/reference.py --output work/reference-cad --cad      # needs the CAD requirements
```

UI checks (optional):

```sh
node --version                           # v22.x is the verified target
npm ci                                   # installs the pinned Playwright dev dependency only
node --test "tests/browser/unit/*.test.mjs"
npx playwright install chromium          # downloads a browser build; only for the browser scenarios
node tests/browser/workflow.e2e.mjs      # starts its own service on a free port with a fresh project
```

Browser scenarios use `SKID_PYTHON` (default `.venv-cad/bin/python`) to start the service. They also use `SKID_BROWSER_ARGS`, a JSON array of Chromium arguments, to override the platform's WebGL flags, and `UI_REVIEW_DIR` to choose where captures go. Scenarios named `engineering`, `creation` and similar use **labelled fake** planners. They test the application path, not a real model.

## 6. Optional AI planning

Engineering plan, broad Ask and equipment research call the Claude Code CLI as a subprocess. Before using them:

1. Install Claude Code yourself and log in with a Claude Max subscription. This project does not install or configure it.
2. Check the login without exposing credentials. Run `env -u ANTHROPIC_API_KEY claude auth status --json` and confirm that `authMethod` is `claude.ai` and `subscriptionType` is `max`. The output can include your account e-mail; do not paste it into issues.
3. If `claude` is not on `PATH`, start the service with `--planner-cli /path/to/claude`.

The application always requests model `claude-opus-5-5` at effort `medium`. It removes `ANTHROPIC_API_KEY` from the child environment, so the subscription login is used rather than API billing. It refuses any reply whose reported model, authentication or API-key source does not match. Your Claude Code installation must therefore accept those exact settings. If it does not, plans and research fail with an explicit reason, and everything else keeps working. Use `--no-research` to hide the research option and `--planner-timeout` / `--research-timeout` to bound calls. What leaves your machine is described in [capabilities](CAPABILITIES.md#optional-ai-planning-and-research).

## 7. Offline Blender bundle (optional)

```sh
python -m skid.cli build work/skid.json --blender /path/to/blender      # exit 0 complete, 3 pending
python -m skid.cli status work/skid.json                                # synchronized / pending / stale ...
```

This is the original two-view path: the legacy SVG plus a `.blend` file. It does not produce STEP, the legend or the description, and it shows composed equipment only as an explicit placeholder. The Blender adapter runs inside Blender's Python API. Its distribution terms are an open owner decision ([THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)).

## Next

- [Capabilities and limits](CAPABILITIES.md)
- [Input contracts](INPUT_CONTRACTS.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Working with AI agents](AGENT_WORKFLOWS.md)
