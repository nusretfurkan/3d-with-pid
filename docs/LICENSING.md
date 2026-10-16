# Licensing

The owner chose these terms on 2026-10-16. This page describes their scope. It is not legal advice, and no lawyer has reviewed it. Where it explains how the parts interact, that is a reasoned reading of the cited sources, not a settled legal conclusion.

## Summary

| Material | Licence | Text |
| --- | --- | --- |
| Everything authored for this project, except the files listed under [exceptions](#gpl-exceptions-blender-integration) | MIT | [`LICENSE`](../LICENSE) |
| The seven Blender integration files listed under [exceptions](#gpl-exceptions-blender-integration) | GPL-3.0-or-later | [`LICENSES/GPL-3.0-or-later.txt`](../LICENSES/GPL-3.0-or-later.txt) |
| Three.js r170 in `web/vendor/three/` (third party, unmodified) | MIT, Copyright © 2010-2024 three.js authors | [`web/vendor/three/LICENSE`](../web/vendor/three/LICENSE) |

Copyright holder for project-authored material: `nusretfurkan`, 2026. [`NOTICE`](../NOTICE) summarises the attributions.

## MIT default

The [MIT License](../LICENSE) covers all project-authored material unless a file is listed as an exception. This includes:

- source code in `skid/` and `web/` (except `web/vendor/three/`), scripts and configuration;
- tests and test fixtures;
- documentation, examples, recipes and example projects;
- the screenshots and example output images under `docs/media/`, which this project's software generated from its own synthetic example.

The licence covers only rights the copyright holder has. Third-party pages cited by URL in fixtures, recipes or research results are references, not licensed material ([FIXTURE_PROVENANCE.md](FIXTURE_PROVENANCE.md)).

Individual MIT files carry no per-file licence header. `LICENSE` and this page cover them. The MIT condition is that the copyright and permission notice is included in all copies or substantial portions of the software.

**Patents.** The MIT License contains no explicit patent grant. This page makes no statement about whether any patent rights may be implied.

## GPL exceptions (Blender integration)

These files are licensed under the GNU General Public License, version 3 or (at your option) any later version. Each starts with `SPDX-License-Identifier: GPL-3.0-or-later`.

| File | Why |
| --- | --- |
| `skid/adapters/blender.py` | Checks for `bpy` and loads `blender_support.scene` inside Blender |
| `skid/adapters/blender_runner.py` | Blender `--python` entry script; imports `bpy` |
| `skid/adapters/blender_support/scene.py` | Builds the Blender scene with `bpy` |
| `skid/adapters/blender_support/preview.py` | Renders the preview with `bpy` and `mathutils` |
| `skid/adapters/blender_support/run_build.py` | Blender entry point; imports `skid.adapters.blender` and `preview` |
| `tests/blender_smoke.py` | In-Blender smoke test; imports `bpy`, `mathutils` and `scene` |
| `tests/test_geometry_contract.py` | Contains a test that imports `skid.adapters.blender` |

Reason: the [Blender FAQ](https://www.blender.org/support/faq/) says that published scripts using the Blender Python API have to be made available under the GNU GPL. These files call the API or load the modules that do. Running Blender as a separate process does not change that for them, so they are GPL-licensed directly.

The pure Python Blender helpers stay MIT: `skid/adapters/blender_support/__init__.py`, `plan.py` and `geometry.py` import no Blender module. So do `skid/build.py` and `scripts/check_blender.sh`, which only start Blender as a separate program.

### How the GPL files and MIT files fit together

- The GPL files import MIT modules from this project (for example `skid.core`, `skid.templates`, `skid.equipment`, `blender_support.plan` and `geometry`). The GNU project lists the Expat (MIT) licence as [compatible with the GPL](https://www.gnu.org/licenses/license-list.html#Expat), so MIT code may be used in a GPL program.
- **The MIT default does not override the GPL.** Anyone who conveys a GPL file, or a work combined with one (for example, the Blender adapter together with the MIT modules it imports), must meet the GPL's conditions for that work, including providing corresponding source under the GPL. The MIT files remain available under MIT on their own, as long as their notice is kept.
- **Import boundary.** The ordinary entry points (`skid.cli`, `skid.build`, `skid.interactive` and its build worker) do not load `bpy`, `mathutils` or any GPL file. `skid.build` reads the adapter constants from the MIT `blender_support.plan` module, and starts Blender as a separate program with a path to `skid/adapters/blender_runner.py` and plain command-line arguments. The production Blender API bridge files (`blender.py`, `blender_runner.py`, and `scene.py`, `preview.py` and `run_build.py` in `blender_support/`) are run by that offline Blender process. The two test files are GPL-labelled as listed above and may import the bridge in plain Python; for example, `tests/test_geometry_contract.py` imports `skid.adapters.blender`. `tests/test_blender_license_boundary.py` checks, in fresh interpreters, that importing the ordinary production entry points loads no Blender API module or GPL file. It does not prohibit importing a GPL file manually, and it does not guarantee how a court would treat a given combination.

## Generated deliverables

The software produces output files when it runs: for example the P&ID SVG, legend, item index, process description, STEP model, display scene, JSON lists and manifests, `.blend` files and preview images. These are called *generated deliverables* here.

**Additional permission.** As the copyright holder, `nusretfurkan` gives anyone who receives a generated deliverable permission to use, copy, modify, distribute and sublicense the project-authored material that the software emits into it, on terms of their choosing, without having to include this project's copyright or licence notice in the deliverable. Examples of such material: symbol shapes, drawing templates and title-block layout, boilerplate wording, and geometry produced by the built-in templates and generators. This is an additional permission for generated deliverables only.

It does **not**:

- change the licence of this repository's source code, tests, documentation or other files. Copying those files, including when they are placed inside a deliverable, still follows the MIT License (with its notice condition) or, for the exception files, the GPL;
- relicense or waive the GPL for the Blender integration files. The Blender FAQ separately describes `.blend` output as the user's own;
- grant any right in third-party material, such as Three.js, Blender, CadQuery or Open CASCADE, vendor or manufacturer data, standards content, or sources cited in research results;
- grant any right in the user's own inputs (project data, names, tags, values and text supplied by the user or by a model at the user's request), which stay with whoever holds them;
- grant more than the copyright holder owns.

Generated deliverables are illustrative. Their dimensions and data are prototype assumptions unless stated otherwise. They carry no engineering approval and no claim of sizing, fabrication readiness or standards conformance (see [ENGINEERING_REVIEW.md](ENGINEERING_REVIEW.md)). The MIT warranty disclaimer applies.

## Dependencies installed separately

This repository is published as source. Users install its dependencies themselves, each under its own terms; none is bundled here except the vendored Three.js files. [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) and [`DEPENDENCY_INVENTORY.json`](DEPENDENCY_INVENTORY.json) record them. In particular:

- **CadQuery and cadquery-ocp** (Apache-2.0), with **Open CASCADE Technology** inside the OCP wheel (LGPL-2.1 with the Open CASCADE exception), plus their transitive packages;
- **Blender** (GPL), optional, for the offline `.blend` path only. Blender is a separate program; it is not part of this repository, did not contribute to it, and does not endorse it;
- **Playwright** and **Node.js**, optional, for browser tests only;
- **Claude Code CLI** (proprietary, Anthropic terms), optional, for the planning, Ask and research modes.

This is one application in one repository. The Blender path is an optional offline output. This page makes no statement about the terms for distributing binaries or bundles that include these dependencies. Anyone preparing such a distribution must check each dependency's licence themselves.
