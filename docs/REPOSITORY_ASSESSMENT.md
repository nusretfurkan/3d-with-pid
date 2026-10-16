# Repository preparation assessment

Prepared 2026-10-16. This records demonstrated software behavior and unresolved decisions. It is not professional engineering approval. The repository remains a private review candidate: no remote, publication or project license has been authorized.

## Evidence collected

| Check | Observed result | Scope |
| --- | --- | --- |
| Original pump fixture | Valid, complete, no diagnostics; matches `examples.make_fixture` byte for byte | 8 entities, 10 ports, 5 lines, 12 assumption records; fixture SHA-256 `ff72b69539770b76ee86f2e6623fc7af9671b3e1ff1e9025f866caf42cb5a0fe` |
| Workspace Python baseline | 1,001 tests passed, zero skipped, 236.614 s | Python 3.12.15, real CadQuery 2.6.1/OCCT 7.8.1 and Blender 5.1.1; before final packaging assessment |
| Workspace Node baseline | 197 passed, zero failed/skipped | Node 22.23.1; unit tests, not browser acceptance |
| Preserved Blender smoke | 1,789 checks passed | Real Blender 5.1.1 outside the restrictive sandbox |
| New reference runner (executor) | 90 core checks; 267 core+CAD checks passed | Real CLI, drafting, CAD, service publication, independent STEP reimport; no external models |
| Browser portability (executor) | Workflow 9/9; zero page problems | Real service/generators and headless Chromium; isolated test project |

Fresh-clone reproduction, final candidate checks and independent software review are still pending. Their actual outcomes will replace this paragraph before delivery. GitHub Actions and other operating systems have not been executed. Raw development/model/authentication logs and user workspaces are excluded; the checked-in runner lets a recipient generate new evidence.

## Architecture and assumptions

The authoritative JSON document, permanent identities and separation of `model`, `pid`, and `physical` are preserved. All edits use validated revision-checked commands. The five-output service keeps the previous verified complete publication when a new revision is pending, failed, cancelled, superseded or corrupt. Standalone geometry exit 0 can still mean pending; the offline Blender bundle is a different publication path.

All reference dimensions, DN50 liquid-service data, placements and routes are identified prototype assumptions. Duty, flow, pressure, temperature, material, thickness, ratings, operating/safety design and instrument hookups are unknown. Generated STEP solids and consistency checks establish neither engineering adequacy nor vendor/fabrication accuracy. Read [capabilities](CAPABILITIES.md), [input contracts](INPUT_CONTRACTS.md) and [reference example](REFERENCE_EXAMPLE.md).

## Failures and corrections

The shell Claude launcher could not locate its already installed native binary outside the sandbox. Existing native executable access worked; exact `claude-opus-5-5`, requested `medium`, Claude Max authentication and API-key source `none` were verified. No reinstall, global setting change, substitute model or API-key billing was used. Portable source defaults now find the operator's own executable; the explicit CLI-path override remains.

An independent negative probe found that the initial new exporter accepted an explicit input under a symlinked directory outside its source root. Correction and regression evidence are pending. This failure has not been counted as a passed gate.

## Audit reconciliation and remaining decisions

The [initial audit](PUBLIC_CONTENTS_AUDIT.md) is a historical source-boundary assessment, not a claim that the final export was checked. Final reconciliation is pending. The [dependency inventory](DEPENDENCY_INVENTORY.json), [third-party notices](../THIRD_PARTY_NOTICES.md), and [fixture provenance](FIXTURE_PROVENANCE.md) distinguish installed dependencies, redistributed Three.js source, original artwork, research links and owner-edited regression data.

No project `LICENSE` is selected. Owner review must decide project terms and Blender API adapter distribution. The r47 fixture retains owner edits/timestamps, and a separate fixture retains captured model questions; publication of those contents remains an explicit owner decision. No credentials were found in the proposed source set, but the final candidate scan remains pending. Dependency wheels/binaries, local development skills, downloaded references, raw logs, environments and user projects are excluded.

Qualified independent process, mechanical/piping and drafting review is outstanding. No connected professional reviewer is available in this session; no external review request was sent. The [review acceptance criteria](ENGINEERING_REVIEW.md) specify the required evidence and signoff. Automated agents provide software review only.
