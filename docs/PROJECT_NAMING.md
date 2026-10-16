# Project display names

An optional human-facing project name, separate from the permanent project ID. Command reference: [input contracts](INPUT_CONTRACTS.md#commands). Source is authoritative (`skid/core/validation.py`, `skid/core/commands.py`, `web/js/project-name.js`).

## Data

- `current.name` is an optional string. Absent means unnamed; human-facing titles then show `current.id`.
- Undo/redo snapshots carry `name` only if the project had one at that revision. Unnamed documents need no migration.
- Renaming never changes `id`, entity IDs, tags, coordinates, file names or artifact identities.
- `name` is part of the project's canonical digest, so a rename makes earlier publications stale and triggers normal regeneration.

## Name rule

`skid.core.validation.project_name_problem(value)` returns `None` or a reason. A valid stored name:

- is 1–120 code points, not whitespace only, with no leading or trailing whitespace;
- contains no control characters, surrogates, U+2028/U+2029, Unicode noncharacters or bidirectional formatting characters (U+061C, U+200E/F, U+202A–202E, U+2066–2069);
- contains at least one base character (general category outside C, Z and M), so text made only of format characters, combining marks or spaces is refused.

Letters of any script, combining marks on base letters, symbols, punctuation, emoji sequences, inner spaces and markup characters such as `<` and `&` are allowed; every output escapes them. The browser helper mirrors this rule so it can refuse locally; the command remains authoritative, and very recently assigned code points may be classified differently by Python and browser Unicode tables. Invalid stored names are `invalid_project_name` errors on the project or a snapshot. `project_display_name(project)` returns the valid name, else the ID.

## Command

```json
{"id": "cmd-1", "expected_revision": 7, "type": "rename_project", "payload": {"name": "North pump skid"}}
```

- Payload is exactly `{name}`; surrounding whitespace is stripped before the rule is applied. Invalid input is rejected with the document unchanged.
- A name equal to the current one after trimming is a `no_op`. Case changes are changes. A name equal to the ID is still stored.
- A change is one revision and one undo snapshot. Undo back to an unnamed revision removes `name`; redo reinstates it.
- Works inside `batch`. A name is not an assumption reference, so envelope assumptions on a rename are rejected as unreferenced.
- There is no command to clear a name; only undo restores absence. The engineering planner does not offer `rename_project`.

## Outputs

- P&ID and legend title blocks show the name, or the ID if unnamed. SVG `<title>` includes both when named.
- `description.json` adds `project_name` (or `null`); the HTML heading and meta line show the name with the ID.
- Stable identity remains `project.id` in SVG data attributes, metadata, the description footer, the inventory and every manifest.

## Workspace

The toolbar shows the name, else the ID. Click, Enter or Space opens an inline field; Enter sends one `rename_project` envelope, Escape cancels. Invalid or unchanged text sends nothing. A conflicting change elsewhere is shown and needs a deliberate second save against the new revision.

Tests: `tests/test_project_name.py`, `tests/browser/unit/project-name.test.mjs`. The 120 code-point bound does not guarantee the title block fits without its existing shrink and wrap.
