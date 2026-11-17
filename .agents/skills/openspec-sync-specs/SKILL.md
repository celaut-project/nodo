---
name: openspec-sync-specs
description: Merge the delta specs of a change into the main specs in docs/openspec/specs/, without archiving. Use when the user wants the main specs updated. Also use when the user says "openspec sync" or "opsx sync".
license: MIT
metadata:
  author: openspec (adapted, no CLI)
  version: "1.0"
---

Merge `docs/openspec/changes/<name>/specs/**/spec.md` into `docs/openspec/specs/**/spec.md`. Read `docs/openspec/README.md` for both formats.

## Steps

1. **Select the change.** Use the given name, or the one in the conversation, or ask. Say: "Using change: <name>". If it has no `specs/` files, say so and stop.
2. **For each delta spec**, read the delta and the main spec at the same path under `docs/openspec/specs/` (it may not exist). Then apply it. Merge with judgment. Do not copy the delta over the main spec.

   | Delta section | Action |
   |---|---|
   | ADDED | Add the requirement. If it already exists, update it to match. |
   | MODIFIED | Replace that requirement with the delta's version (text and all scenarios). Keep content the delta does not mention. |
   | REMOVED | Delete the whole requirement block. |
   | RENAMED | Rename FROM to TO. |
   | Purpose | Ignore it if the main spec exists. Copy it for a new main spec. |

   **New capability (no main spec).** Only ADDED can apply. Create `# <capability> Specification`, `## Purpose` (copied from the delta; `TBD` plus a warning if missing), and `## Requirements` with the ADDED blocks. If the delta has MODIFIED or RENAMED for a spec that does not exist, stop for that capability and report it. Do not invent the missing requirement. Ignore REMOVED and warn.

   **Removing the last requirement.** Delete the main spec file only if the change's `.openspec.yaml` has `retire_capabilities: true`. Otherwise stop for that capability and say what to do. Never leave an empty `## Requirements`.
3. **Check the result.** No delta headers remain in main specs. Requirements use `### Requirement:`, scenarios use `#### Scenario:`, each requirement has a scenario, and each Purpose is real.
4. **Summarize** per capability: requirements added, modified, removed, renamed; new files; any `TBD` Purpose; any retired capability.

## Guardrails

- Read the delta and the main spec before any write.
- Keep existing content and order that the delta does not mention.
- The operation must be idempotent. A second run changes nothing.
- If something is unclear, ask.
