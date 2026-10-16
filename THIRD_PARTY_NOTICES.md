# Third-party notices

Recorded 2026-10-16. This file lists third-party material that this repository contains or depends on. It is an audit record, not legal advice. Package-declared license metadata can be incomplete; uncertain entries are marked.

**Project license:** project-authored material is licensed under the MIT License ([`LICENSE`](LICENSE)), except seven Blender integration files licensed under GPL-3.0-or-later ([`LICENSES/GPL-3.0-or-later.txt`](LICENSES/GPL-3.0-or-later.txt)); see [section 4](#4-blender-python-api-gpl-exceptions) and [docs/LICENSING.md](docs/LICENSING.md). The images and the P&ID SVG in `docs/media/` are original project output generated from the synthetic example ([provenance](docs/media/PROVENANCE.md)); they are project material under MIT, not third-party material. This file covers third-party material only and grants no rights in it.

## 1. Material included in this repository

### Three.js r170 (MIT)

The repository vendors these files, unmodified, from the npm package `three@0.170.0`:

| File | SHA-256 | Bytes |
| --- | --- | --- |
| `web/vendor/three/build/three.module.min.js` | `08fd7545d13d2c7fb65ab691530a802dafefd638596501854f267d0fb13c39e7` | 691648 |
| `web/vendor/three/examples/jsm/controls/OrbitControls.js` | `80efaadea4f8a636a65fb0bd08bfef62f3d93a0bb94e2e7500f23176c5c07f4e` | 32134 |
| `web/vendor/three/examples/jsm/controls/TransformControls.js` | `9848ad589dfcf5f3769d0836472cb37cc46e6e0b36eb42ebcf7c898d0072e8ec` | 41663 |
| `web/vendor/three/LICENSE` | `4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454` | 1081 |

Source: `https://registry.npmjs.org/three/-/three-0.170.0.tgz`, npm integrity `sha512-FQK+LEpYc0fBD+J8g6oSEyyNzjp+Q7Ks1C568WWaoMRLW+TkNNWmenWeGgJjV105Gd+p/2ql1ZcjYvNiPZBhuQ==`. On 2026-10-16 the four hashes were recomputed from the actual files, and they matched both `web/vendor/README.md` and a locally installed copy of the package. The registry integrity also matched. `LICENSE` is byte-identical to the upstream [r170 LICENSE](https://github.com/mrdoob/three.js/blob/r170/LICENSE). `tests/browser/unit/static.test.mjs` checks the hashes against `web/vendor/README.md`.

The license text below must accompany these files:

```text
The MIT License

Copyright © 2010-2024 three.js authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

No other third-party code, fonts, images, CAD files or symbol artwork is included. P&ID symbols, sheet layout, equipment geometry and example data are authored for this project. They are not copied or traced from ISA-5.1, ISO 10628, vendor drawings or any symbol library, and no standards conformance is claimed (see `skid/drafting/symbols.py` and `skid/adapters/pid_symbols/__init__.py`).

## 2. Runtime dependencies installed by users (not redistributed)

The repository contains no Python wheels, shared libraries, Node packages, browsers or Blender files. Users install these separately; their own license terms apply. The complete set of package-declared metadata for the 39 installed CAD packages is in [docs/DEPENDENCY_INVENTORY.json](docs/DEPENDENCY_INVENTORY.json).

| Component | Version (observed) | Declared or upstream license | Notes |
| --- | --- | --- | --- |
| Python (CPython) | 3.12.15 (CAD venv); core is standard library only | [PSF License Agreement](https://docs.python.org/3/license.html) | Interpreter not bundled. |
| CadQuery | 2.6.1 (only direct pin in `requirements-cad.txt`) | Apache-2.0 (metadata and upstream `LICENSE`) | |
| cadquery-ocp (OCP binding) | 7.8.1.1.post1 | Binding: Apache-2.0 per the upstream [CadQuery/OCP LICENSE](https://github.com/CadQuery/OCP/blob/master/LICENSE). The wheel declares **no** license metadata and has no license file in `dist-info`. | **Uncertain:** the macOS arm64 wheel bundles 75 shared libraries (OCCT plus image, font and compression libraries). Their notices are not declared in the wheel. |
| Open CASCADE Technology (inside the OCP wheel) | 7.8.1 | [LGPL-2.1](https://github.com/Open-Cascade-SAS/OCCT/blob/V7_8_1/LICENSE_LGPL_21.txt) with [Open CASCADE exception 1.0](https://github.com/Open-Cascade-SAS/OCCT/blob/V7_8_1/OCCT_LGPL_EXCEPTION.txt) | See the notice below. |
| VTK | 9.3.1 | BSD (metadata); BSD-3-Clause style `LICENSE` in `dist-info` | Pulled in by cadquery-ocp. |
| CasADi | 3.8.1 | LGPL-3.0-or-later (metadata `License` field; classifier only "OSI Approved") | Pulled in by CadQuery and not called by project code. The installed package ships per-component licenses under `casadi/include/licenses/`. |
| NLopt | 2.11.0 | MIT (metadata) | **Uncertain:** upstream NLopt mixes MIT and LGPL sources; the wheel's compiled contents were not verified. |
| Other transitive packages | see inventory | MIT, BSD-2/3-Clause, Apache-2.0, PSF-2.0, MIT-CMU, 0BSD, Zlib, CC0-1.0 (as declared) | Exact per-package fields, classifiers and license-file hashes are in the inventory. |

### Open CASCADE Technology notice

This project's CAD path uses facilities provided by the Open CASCADE Technology software, through the CadQuery and OCP packages. The OCCT exception text, as published for V7_8_1:

```text
Open CASCADE exception (version 1.0) to GNU LGPL version 2.1.

The object code (i.e. not a source) form of a "work that uses the Library"
can incorporate material from a header file that is part of the Library.
As a special exception to the GNU Lesser General Public License version 2.1,
you may distribute such object code incorporating material from header files
provided with the Open CASCADE Technology libraries (including code of CDL
generic classes) under terms of your choice, provided that you give
prominent notice in supporting documentation to this code that it makes use
of or is based on facilities provided by the Open CASCADE Technology software.
```

The repository distributes source only, with no OCCT object code. This notice is included as good practice. It is not a conclusion about which obligations apply.

## 3. Optional tools (not redistributed)

| Tool | Observed version | Terms | Use |
| --- | --- | --- | --- |
| Blender | 5.1.1 | GPL-3.0-or-later | Optional offline 3D output, run as a separate program. See the Blender section below. |
| Playwright (`@playwright/test`, `playwright`, `playwright-core`) | 1.58.2 | Apache-2.0, with `NOTICE` (Microsoft; Puppeteer-derived code) and `ThirdPartyNotices.txt` in the packages | Optional browser tests only. Downloaded browser binaries carry their own terms. |
| fsevents | 2.3.2 | MIT | Optional macOS transitive dependency of Playwright. |
| Node.js | 22.x | Node.js license | Optional browser unit/e2e tests. No frontend build step. |
| Claude Code CLI | 2.1.301 observed | Proprietary: "© Anthropic PBC. All rights reserved. Use is subject to Anthropic's Commercial Terms of Service." | Optional external model service for Ask, engineering plan and research modes. Users install and authenticate it themselves. It is not bundled, and project context leaves the machine only when those modes are used. |

## 4. Blender Python API: GPL exceptions

Blender's [FAQ](https://www.blender.org/support/faq/) states that published scripts calling the Blender Python API must be made available under the GNU GPL. Its separate-program exception requires, among other conditions, that the product contain no Blender API calls. These project files call `bpy` or `mathutils` directly:

- `skid/adapters/blender_runner.py`
- `skid/adapters/blender_support/scene.py`
- `skid/adapters/blender_support/preview.py`
- `tests/blender_smoke.py`

These files load them inside Blender: `skid/adapters/blender.py` (a deferred import of `scene` and a `bpy` availability check) and `skid/adapters/blender_support/run_build.py`. `tests/test_geometry_contract.py` contains a test that imports `skid.adapters.blender`. `skid/adapters/blender_support/plan.py` and `geometry.py` do not import `bpy`.

**Licence decision (owner, 2026-10-16):** these seven files are licensed under GPL-3.0-or-later and carry `SPDX-License-Identifier: GPL-3.0-or-later`. Everything else authored for this project, including `skid/adapters/blender_support/{__init__,plan,geometry}.py`, `skid/build.py` and `scripts/check_blender.sh`, is MIT. The normal CLI and service read adapter constants from the MIT `plan.py` and start Blender as a separate program; they do not import a GPL file. Running Blender as a separate subprocess is not asserted to change the terms of the GPL files themselves. The details and their limits are in [docs/LICENSING.md](docs/LICENSING.md); this is a reasoned assessment, not a legal opinion.

**Credit.** Blender (<https://www.blender.org>) is free software from the Blender project, used here as a separately installed tool for the optional offline output. It is not included in this repository, did not contribute to it, and does not endorse it. No Blender logo or trademark artwork is included.

## 5. Research links and references

Links in research results, assumptions and examples (for example the general product overview cited in `examples/recipes/shell_and_tube_exchanger.definition.json`) are references only. They grant no rights to linked text, images, drawings, CAD or dimensions, and the project bundles no such material. Development references that were inspected privately are not part of this repository. These include a third-party MIT-licensed UI skill set and a third-party MIT-licensed procedural pump example.
