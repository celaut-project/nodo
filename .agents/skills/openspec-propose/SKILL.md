---
name: openspec-propose
description: Propose a new change with all planning artifacts (proposal, specs, design) under docs/openspec/changes/. Use when the user wants to plan a feature or fix before coding. Also use when the user says "openspec propose" or "opsx propose".
license: MIT
metadata:
  author: openspec (adapted, no CLI)
  version: "1.0"
---

Create a change and write all its planning artifacts. Read `docs/openspec/README.md` first for the layout and formats.

**Planning only.** Do not edit project code. Stop after the artifacts are ready. Implementation is tracked in the GitHub issue and the PR, not here.

## Steps

1. **Understand the request.** If unclear, ask: "What change do you want to work on?" Ask before you start only when the answer changes scope, visible behavior, compatibility or acceptance. For small details, assume and write the assumption in the artifacts.
2. **Load context.** Read `docs/openspec/config.yaml`. Use `context` and `rules` as constraints for you. Do not copy them into the artifacts. Read the relevant code, tests and docs, and the existing specs in `docs/openspec/specs/` (`ls -R docs/openspec/specs`). Read related specs in full.
3. **Create the change.** Choose a kebab-case name (`add-user-auth`). If `docs/openspec/changes/<name>/` exists, ask: continue it or use a new name. Ask for the GitHub issue number (or say there is none) and put it in `.openspec.yaml` as `issue:` and in the proposal. Create the folder and `.openspec.yaml` (see README).
4. **Write the artifacts in order.** Start from `docs/openspec/templates/`. Re-read earlier artifacts from disk before you write the next one.

   **proposal.md** — WHY.
   - Why: 1-2 sentences. Problem and why now.
   - What Changes: specific bullets. Mark breaking changes **BREAKING**.
   - Capabilities: *New* (each becomes `specs/<path>/spec.md`) and *Modified* (use the exact existing path under `docs/openspec/specs/`). Reuse an existing capability instead of a near-duplicate name. No behavior change at all: set `skip_specs: true` and do not invent a requirement.
   - Impact: code, APIs, dependencies. Keep it short. Implementation detail goes in design.md.

   **specs/<capability>/spec.md** — WHAT. One file per capability in the proposal. Write them one at a time. Follow "Spec format" in the README. A spec is a behavior contract: inputs, outputs, errors, constraints, testable scenarios. No class names, libraries or steps. For MODIFIED, copy the full existing requirement first.

   **design.md** — HOW. Optional. Write it only for cross-cutting changes, new dependencies, data-model, security, performance or migration concerns, or real ambiguity. Sections: Context, Goals / Non-Goals, Decisions (with alternatives), Risks / Trade-offs (`[Risk] → Mitigation`), Migration Plan, Open Questions. Open Questions are only for things that cannot change the specs or approach. Resolve the others now, with the user.

5. **Summarize.** Give the change name and path, the artifacts created (and any skipped, with the reason). End with: "The change is ready for review. Link it from the issue and the PR."

## Guardrails

- Ground scope in what you found in the code. Separate observed behavior from assumptions. Show conflicts with existing specs. Do not hide them.
- Do not write task lists. Work tracking belongs to the issue.
- Each spec file and each artifact must exist on disk before you go on.
