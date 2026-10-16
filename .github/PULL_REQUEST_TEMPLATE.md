## Summary

<!-- What changes and why. Link the issue. -->

## Data concerns touched

- [ ] `model` (engineering semantics)
- [ ] `pid` (diagram presentation)
- [ ] `physical` (placement, routes, geometry)
- [ ] generators / publication
- [ ] web UI
- [ ] docs only

## Checks run

```text
exact commands and their actual results (counts, skips, failures)
```

- [ ] Python suite run, with CAD requirements installed (state any skips)
- [ ] Blender integration run with `SKID_BLENDER`, if Blender code changed
- [ ] Node unit tests run, if `web/` changed
- [ ] `scripts/reference.py` run (`--cad` if generation or publication changed)

## Integrity

- [ ] No validator, integrity check or test assertion was weakened or skipped to make this pass. Any test change is explained above, with a negative control.
- [ ] Fakes and test doubles are labelled. No fake pass is reported as real acceptance.
- [ ] Pending, stale, unresolved and failed states remain explicit.
- [ ] IDs stay separate from tags. Manual layout and routing survive regeneration.
- [ ] New assumed values carry provenance. No sizing, rating, vendor or standards claims were added.
- [ ] No credentials, account details, personal paths, client data or third-party artwork are included.
- [ ] Docs describe only behaviour that was observed.

## AI assistance

<!-- None, or which parts were generated/reviewed, with model and requested effort. An AI review is not engineering sign-off. -->

## Licence

Contributions are accepted under the licence of the files they change ([docs/LICENSING.md](../docs/LICENSING.md)): MIT by default, GPL-3.0-or-later for the seven listed Blender integration files. Keep the current MIT/GPL file mapping, SPDX headers and third-party licence and notice texts as they are. Do not change `LICENSE`, `NOTICE`, `LICENSES/`, SPDX headers, the licence mapping or third-party notices without the owner's approval.
