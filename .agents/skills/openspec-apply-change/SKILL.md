---
name: openspec-apply-change
description: Implement the tasks of a change from openspec/changes/<name>/tasks.md. Use when the user wants to start or continue implementation. Also use when the user says "openspec apply" or "opsx apply".
license: MIT
metadata:
  author: openspec (adapted, no CLI)
  version: "1.0"
---

Implement the tasks of a change. Read `openspec/README.md` for the layout.

## Steps

1. **Select the change.** Use the name given, or the one from the conversation, or the only folder in `openspec/changes/` (not `archive/`). If more than one, ask. Say: "Using change: <name>".
2. **Check readiness.** `proposal.md`, `tasks.md` and the delta specs must exist. If something is missing, stop and offer `openspec-propose`. Do not start implementing.
3. **Read context.** Read `proposal.md`, all `specs/**/spec.md`, `design.md` if present, and `tasks.md`. Read `openspec/config.yaml` for `context` and `rules`. Use them as constraints. Do not copy them into files.
4. **Show progress.** Count the tasks (`- [x]` done, `- [ ]` pending) and list what remains.
5. **Implement, one task at a time.**
   - Make the minimal change the task needs, including its tests and docs.
   - Run the verification the task names.
   - Change its box from `- [ ]` to `- [x]` right after. Mark it only if the specified behavior is fully done.
6. **Report.** Tasks done in this session and total progress. If all are done, suggest a review, then `openspec-archive-change`.

## Pause and ask when

- A task is unclear.
- The work reveals a design problem. Suggest `openspec-update-change`.
- A task needs more than the specs and tasks describe, or you want to drop, narrow or defer specified behavior. Show the extra scope. Do not absorb it silently.
- An error or blocker appears.

## Guardrails

- Keep going until done or blocked.
- Do not guess. Do not mark partial work as done.
- The specs are the contract. If code and spec disagree, stop and ask which one is right.
