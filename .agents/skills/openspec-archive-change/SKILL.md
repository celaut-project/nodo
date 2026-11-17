---
name: openspec-archive-change
description: Archive a finished change. Syncs its delta specs to the main specs, then moves it to docs/openspec/changes/archive/. Use when implementation is complete. Also use when the user says "openspec archive" or "opsx archive".
license: MIT
metadata:
  author: openspec (adapted, no CLI)
  version: "1.0"
---

Archive a finished change. Read `docs/openspec/README.md` for the layout.

## Steps

1. **Select the change.** Use the given name, or the one in the conversation, or ask (show only folders in `docs/openspec/changes/` other than `archive/`). Say: "Using change: <name>".
2. **Check artifacts.** `proposal.md` and the delta specs must exist (`design.md` is optional; specs may be absent if `.openspec.yaml` has `skip_specs: true`). If something is missing, warn and ask if the user wants to go on.
3. **Check the PR.** Ask whether the implementing PR is merged, and for its number. If not merged, warn and ask to confirm. Add `pr: <number>` to `.openspec.yaml`.
4. **Check specs.** If delta specs exist, compare each with its main spec and summarize what would change. Ask:
   - Changes needed: "Sync now (recommended)" or "Archive without syncing".
   - Already synced: "Archive now", "Sync anyway" or "Cancel".
   - A capability is blocked (MODIFIED or RENAMED on a missing main spec): offer only "Archive without syncing" or "Cancel".

   For "Sync", run the `openspec-sync-specs` workflow now and wait for it. If it reports a problem, stop. Do not archive. After the sync, read each main spec again and confirm it matches the delta.
5. **Move the change.** Create `docs/openspec/changes/archive/` if needed. The target is `docs/openspec/changes/archive/YYYY-MM-DD-<name>` with today's date. If the name already starts with a date, do not add another. If the target exists, stop and report. Then use `mv` (or `git mv`).
6. **Summarize**: change name, archive path, whether specs were synced, and any warnings (incomplete artifacts or unmerged PR).

## Guardrails

- Warnings inform. They do not block. Confirm, then go on.
- Never archive while a sync is not finished or failed.
- Keep `.openspec.yaml`. It moves with the folder.
