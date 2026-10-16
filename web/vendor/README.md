# Vendored browser dependencies

Copied unmodified on 2026-10-14 from the locally installed npm package `three@0.170.0`
(`artifacts/ui-runtime/node_modules/three`, resolved from
`https://registry.npmjs.org/three/-/three-0.170.0.tgz`, lockfile integrity
`sha512-FQK+LEpYc0fBD+J8g6oSEyyNzjp+Q7Ks1C568WWaoMRLW+TkNNWmenWeGgJjV105Gd+p/2ql1ZcjYvNiPZBhuQ==`).
License: MIT, see `three/LICENSE` (Copyright © 2010-2024 three.js authors).

| File | Package path | SHA-256 | Bytes |
| --- | --- | --- | --- |
| `three/build/three.module.min.js` | `build/three.module.min.js` | `08fd7545d13d2c7fb65ab691530a802dafefd638596501854f267d0fb13c39e7` | 691648 |
| `three/examples/jsm/controls/OrbitControls.js` | `examples/jsm/controls/OrbitControls.js` | `80efaadea4f8a636a65fb0bd08bfef62f3d93a0bb94e2e7500f23176c5c07f4e` | 32134 |
| `three/examples/jsm/controls/TransformControls.js` | `examples/jsm/controls/TransformControls.js` | `9848ad589dfcf5f3769d0836472cb37cc46e6e0b36eb42ebcf7c898d0072e8ec` | 41663 |
| `three/LICENSE` | `LICENSE` | `4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454` | 1081 |

`TransformControls.js` (the 3D view's Move gizmo) was added unmodified on 2026-10-15 from the same
installed `three@0.170.0` package; it is covered by the same MIT `three/LICENSE`. It imports only
the bare `three` core (including the r170 `Controls` base class). The viewer configures its
instance at run time (world space, translate mode, axis colours, the free screen-plane handle
moved to an unused layer); the vendored file itself is not edited.

`web/index.html` maps the bare specifier `three` to `/vendor/three/build/three.module.min.js`
and `three/addons/` to `/vendor/three/examples/jsm/` with an inline import map. Nothing is
fetched from a CDN, and no web fonts are used. To update, copy the same paths from a newer
pinned package, record the new version, source and hashes here, and rerun `tests/browser`.
