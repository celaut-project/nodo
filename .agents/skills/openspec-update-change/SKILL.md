---
name: openspec-update-change
description: Revise the planning artifacts of an existing change and keep them consistent. Use when the user wants to fold new decisions into a plan or reconcile its artifacts. Also use when the user says "openspec update change" or "opsx update". Never edits code.
license: MIT
metadata:
  author: openspec (adapted, no CLI)
  version: "1.0"
---

Revise a change's artifacts and keep them consistent. Never edit code. Read `openspec/README.md` for formats.

## Steps

1. **Select the change** (`openspec/changes/<name>/`). Use the given name, or the one in the conversation, or ask. Say: "Using change: <name>".
2. **Understand the request.** A specific edit ("the design now uses X") is the starting point. For "update" or "make it coherent", do a consistency review.
3. **Read and reconcile.** Read the artifacts the request touches and all the other existing ones. Draft the edit in the conversation, not in files. Then check every other artifact against it, in any direction (a change in tasks can need a change in the proposal). Look for contradictions, gaps and duplication:
   - Does the proposal's Capabilities list match the files in `specs/`?
   - Do the tasks cover every requirement? Does design.md still match the specs?
   - Do the delta specs still follow the README format?
4. **Confirm, then write.** Show each proposed revision and why. Write it only after the user agrees. Leave rejected revisions alone. If everything is already consistent, say so and change nothing.
5. **Point to the next step** (advice only). Missing artifact: say which one `openspec-propose` would write. Tasks already ticked: say which tasks need a second look because the plan changed. All done: suggest `openspec-archive-change`.

## Guardrails

- Planning files only. If the new plan needs code changes, name them. Do not make them.
- Do not add new artifacts here, except a missing spec file the user confirms.
- If the request changes the *intent* of the change, suggest a new change with a new name.
