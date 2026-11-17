---
name: openspec-propose
description: Propose a new change with all planning artifacts (proposal, specs, design, tasks) under openspec/changes/. Use when the user wants to plan a feature or fix before coding. Also use when the user says "openspec propose" or "opsx propose".
license: MIT
metadata:
  author: openspec (adapted, no CLI)
  version: "1.0"
---

Create a change and write all its planning artifacts. Read `openspec/README.md` first for the layout and formats.

**Planning only.** Do not edit project code. Stop after the artifacts are ready. Start implementation only on a new user request (`openspec-apply-change`).

## Steps

1. **Understand the request.** If unclear, ask: "What change do you want to work on?" Ask before you start only when the answer changes scope, visible behavior, compatibility or acceptance. For small details, assume and write the assumption in the artifacts.
2. **Load context.** Read `openspec/config.yaml`. Use `context` and `rules` as constraints for you. Do not copy them into the artifacts. Read the relevant code, tests and docs, and the existing specs in `openspec/specs/` (`ls -R openspec/specs`). Read related specs in full.
3. **Create the change.** Choose a kebab-case name (`add-user-auth`). If `openspec/changes/<name>/` exists, ask: continue it or use a new name. Create the folder and `.openspec.yaml` (see README).
4. **Write the artifacts in order.** Start from `openspec/templates/`. Re-read earlier artifacts from disk before you write the next one.

   **proposal.md** — WHY.
   - Why: 1-2 sentences. Problem and why now.
   - What Changes: specific bullets. Mark breaking changes **BREAKING**.
   - Capabilities: *New* (each becomes `specs/<path>/spec.md`) and *Modified* (use the exact existing path under `openspec/specs/`). Reuse an existing capability instead of a near-duplicate name. No behavior change at all: set `skip_specs: true` and do not invent a requirement.
   - Impact: code, APIs, dependencies. Keep it short. Implementation detail goes in design.md.

   **specs/<capability>/spec.md** — WHAT. One file per capability in the proposal. Write them one at a time. Follow "Spec format" in the README. A spec is a behavior contract: inputs, outputs, errors, constraints, testable scenarios. No class names, libraries or steps. For MODIFIED, copy the full existing requirement first.

   **design.md** — HOW. Optional. Write it only for cross-cutting changes, new dependencies, data-model, security, performance or migration concerns, or real ambiguity. Sections: Context, Goals / Non-Goals, Decisions (with alternatives), Risks / Trade-offs (`[Risk] → Mitigation`), Migration Plan, Open Questions. Open Questions are only for things that cannot change the specs, approach or tasks. Resolve the others now, with the user.

   **tasks.md** — checklist. Follow the template and the Tasks format in the README.
   - Small tasks, ordered by dependency, grouped under numbered headings.
   - Each task says how to verify it (test, command, visible behavior).
   - Each group carries its own tests and docs. No final "testing" or "docs" group.
   - Archive and other post-merge steps go as plain bullets under `## Workflow follow-up`.
   - Check design.md Open Questions first. If one changes what is built, ask the user.
5. **Summarize.** Give the change name and path, the artifacts created (and any skipped, with the reason). End with: "The plan is ready for review. When you're ready, ask me to apply it."

## Guardrails

- Ground scope and tasks in what you found in the code. Separate observed behavior from assumptions. Show conflicts with existing specs. Do not hide them.
- No generic tasks like "explore the codebase". Do that discovery now.
- Each spec file and each artifact must exist on disk before you go on.
