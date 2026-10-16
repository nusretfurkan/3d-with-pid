# Contributing

Thank you for helping. This repository is a **private review candidate without a chosen licence**. Until the owner selects one, contributions are discussed through issues, and nothing is merged under licence terms that have not been decided. Please do not add a `LICENSE` file or licence headers.

## Before you start

- Read [AGENTS.md](AGENTS.md) (it applies to people as well as agents) and [docs/CAPABILITIES.md](docs/CAPABILITIES.md).
- For anything larger than a fix, open an issue first that describes the behaviour, the affected data concern (`model`, `pid` or `physical`) and the interface you expect to change.

## Ground rules

- **One authoritative model.** Generators read a frozen snapshot and never write engineering data. Every edit is a validated command with `expected_revision`.
- **Stable identity.** IDs never derive from tags, and regeneration never invents IDs.
- **Honest status.** Unresolved placement, pending routes, stale or failed outputs stay visible. Do not make a pending state look complete.
- **No weakened checks.** Do not relax a validator, integrity check or test assertion, or skip a test, to make a change pass. If a test is wrong, explain why, fix it with a negative control, and say so in the pull request.
- **Label fakes.** Test doubles for planners, CLIs, Blender or generators must be labelled as such in names and output. A fake pass is not real acceptance.
- **Provenance.** New assumed values cite an assumption with a truthful description and source. Do not add duty, ratings, materials, vendor data or standards claims that the code cannot justify.
- **Original assets only.** Symbols, artwork and prose must be your own original work, and you must have the right to contribute them. Do not add third-party artwork or text that is not approved for this repository and licensed for redistribution: no standards-derived symbol art, vendor drawings or CAD, screenshots of third-party products, or text copied from standards, vendor documents or other sources. Third-party licence and notice texts that a dependency or vendored component requires are kept unchanged with it. New dependencies need a recorded licence and a reason; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- **No secrets or personal data.** Never commit credentials, API keys, account e-mails, organisation IDs, machine-specific home paths, or real client project data.

## Development checks

```sh
python -m unittest discover -s tests -p 'test_*.py'          # with requirements-cad.txt installed
SKID_BLENDER=/path/to/blender python -m unittest discover -s tests -p 'test_*.py'   # if you touched Blender code
node --test "tests/browser/unit/*.test.mjs"                  # Node.js 22, after npm ci, if you touched web/
python scripts/reference.py --output work/ref --cad          # if you touched generation or publication
```

Report the exact commands and the actual results, including skips and failures. A run with skipped CAD or Blender tests does not cover those areas. Browser scenarios (`tests/browser/*.e2e.mjs`) need Playwright and a Chromium build ([getting started](docs/GETTING_STARTED.md#5-run-the-checks)).

## Pull requests

Use the pull-request template. Keep changes focused, update the docs that describe changed behaviour, and describe only behaviour you have actually observed. AI-assisted contributions are welcome under the same rules; state which parts were generated, and which model ran with what requested effort. An AI review is not engineering sign-off ([docs/ENGINEERING_REVIEW.md](docs/ENGINEERING_REVIEW.md)).

## Engineering feedback

Feedback from practising process, mechanical, piping and drafting engineers is especially valuable. Use the **Engineering review** issue template and the criteria in [docs/ENGINEERING_REVIEW.md](docs/ENGINEERING_REVIEW.md). Share only projects you are allowed to share.
