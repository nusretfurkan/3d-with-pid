# Getting started

From a clean checkout to a working core, the five-output workspace, and the checks. Every step after the core is optional.

## 1. Requirements

| Need | For | Notes |
| --- | --- | --- |
| Linux or macOS (POSIX) | everything | Persistence and the service use `flock`, process groups and POSIX signals. Windows is not supported. |
| Python 3.12 | everything | The core uses only the standard library. The CAD requirements are pinned for Python 3.12. |
| Git | cloning | |
| Blender (optional) | the offline `.blend` bundle and its integration tests | Any executable you point to with `--blender` or `SKID_BLENDER`. Blender 5.1.1 is the verified version; other versions are untested. |
| Node.js 22 and npm (optional) | UI unit tests; Playwright browser checks | Node.js 22 is the verified target; older versions are untested (the quoted test glob below relies on `node --test` glob support). The web UI itself needs **no** npm install and no build step; Three.js 0.170.0 is vendored under `web/vendor/`. |
| Claude Code CLI with a Claude Max login (optional) | Engineering plan, broad Ask and equipment research | See [section 6](#6-optional-ai-planning). Not needed for anything else. |

## 2. Clone and create an environment

```sh
git clone https://github.com/nusretfurkan/3d-with-pid.git 3d-with-pid
cd 3d-with-pid
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

`requirements-cad.txt` pins `cadquery==2.6.1` and applies `constraints-cad.txt`. That file records the exact versions of one audited environment: CPython 3.12 on macOS arm64. A cold install of these pins into a new virtual environment (no system site packages) was verified on macOS arm64 with Python 3.12 at source commit `6026f62`; other platforms and Python versions are untested and may need different wheels or fail. See the [repository assessment](REPOSITORY_ASSESSMENT.md) for the recorded run. Record `python -m pip freeze` with any results you report. Dependency licences are listed in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

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

Without CadQuery installed, the CAD-dependent tests are skipped with a stated reason. Without `SKID_BLENDER`, the real-Blender integration test is skipped. Read the summary line: a run with skips does not cover those parts. To include Blender (5.1.1 verified; other versions untested):

```sh
SKID_BLENDER=/path/to/blender python -m unittest discover -s tests -p 'test_*.py'
/path/to/blender --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py
SKID_BLENDER=/path/to/blender sh scripts/check_blender.sh     # the smoke test plus the real-Blender integration tests
```

`tests/blender_smoke.py`, run directly or through `scripts/check_blender.sh`, writes its previews and bundles to `artifacts/blender-preview/`. That directory is ignored by Git.

**Expected skips in a clone of this repository.** Even with CadQuery and `SKID_BLENDER` set, some tests skip on purpose, and each prints its reason:

- 3 workspace-manifest tests in `tests/test_release_packaging.py` apply only to the private source workspace. In a clone, the shipped-manifest and re-export checks run in their place and must pass.
- 8 replay cases in `tests/test_research_bridge.py` and `tests/test_research_brief.py` need recorded real research streams. Those raw transcripts are private and are not shipped.

These 11 skips are expected. The real-Blender integration test also skips when `SKID_BLENDER` is unset. The legacy geometry-recipe and `creation-r3` regressions now read committed fixtures under `tests/fixtures/` and always run. Do not delete or skip tests, or add raw model or authentication streams, to reach zero skips. For reference, the full suite at commit `6026f62` reported 12 skips, the 11 above plus the `creation-r3` case before its fixture was committed. Final verification at `827d6b7` ran 1,032 Python tests: 1,021 passed and 11 explicitly skipped, with real Blender enabled; see the [assessment](REPOSITORY_ASSESSMENT.md#final-post-review-verification).

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

`npm ci` installs only `playwright`, `playwright-core` and, on macOS, the optional `fsevents` package. It does not install `@playwright/test` or Three.js; Three.js is vendored. npm may warn about the `fsevents` install script. That warning does not affect the checks and needs no permission or global setting change.

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

This is the original two-view path: the legacy SVG plus a `.blend` file. It does not produce STEP, the legend or the description, and it shows composed equipment only as an explicit placeholder. The Blender adapter runs inside Blender's Python API. Blender is installed separately. The adapter's Blender API files are licensed GPL-3.0-or-later, and the rest of the project is MIT ([licensing](LICENSING.md#gpl-exceptions-blender-integration)).

## 8. Exporting a source copy (optional)

`scripts/export_release.py` uses only the standard library. It copies exactly the files listed in `scripts/release_manifest.json`:

```sh
python scripts/export_release.py --check                                  # plan, read and scan; writes nothing
python scripts/export_release.py --output release/candidate [--git-init]  # a new or empty directory
```

`--output` must name a new or empty directory; nothing is overwritten. The copy is assembled in a fresh sibling directory and then renamed into place. A report, `<output>.export.json`, is written beside the copy. `--git-init` runs `git init` in the copy and stages its files. The exporter never commits, adds a remote or pushes.

Exit status: `0` exported (or the check passed); `1` refused, nothing exported; `2` usage error; `3` partial: the copy exists, but a later step such as the requested Git setup failed.

`release/` is a safe default. It is never exported and is ignored by Git. Do not put an output inside an exported tree such as `skid/` or `tests/`, because a later export could include an earlier copy. Do not rely on the exporter to detect this.

Local output bundles and copies are yours to make, and copies follow [the licence terms](LICENSING.md). Agents working in this repository publish externally only with the owner's explicit authorisation ([agent instructions](../AGENTS.md#repository-status)).

## Next

- [Capabilities and limits](CAPABILITIES.md)
- [Input contracts](INPUT_CONTRACTS.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Working with AI agents](AGENT_WORKFLOWS.md)
