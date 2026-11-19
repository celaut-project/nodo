---
name: github-issue
description: Write a GitHub issue for this repository (bug, feature or change) with the problem, the proposal, the expected behavior, the decisions and the open questions. Use when the user wants to plan, propose, report or document a change before coding, or says "write an issue", "propose" or "redacta un issue". The issue is the only planning record. There is no docs/openspec.
license: MIT
metadata:
  version: "1.0"
---

Write one GitHub issue that holds the whole plan of a change. The issue replaces the old `openspec/changes` folders (proposal, specs, design). Do not create planning files in the repository.

**Planning only.** Do not edit project code. Create or edit the issue, then stop.

## Where things live

| Where | What |
|---|---|
| GitHub issue | The problem, the proposal, the expected behavior, the decisions, the progress. |
| GitHub PR | The solution: code, tests, review. `Closes #N`. |
| `docs/*.md` | How the node works now, for users. Update it in the PR that changes the behavior. |

If a PR departs from the issue, edit the issue (or write a comment) in the same session. Do not let the two disagree.

## Steps

1. **Understand the request.** Ask only if the answer changes scope, visible behavior, compatibility or acceptance. For small details, assume and write the assumption in the issue.
2. **Read the code first.** Read the relevant code, tests and docs. Search the open issues (`gh issue list --search "<words>"`) for duplicates and for issues that this one depends on. Separate what you observed (with `path:line`) from what you assume.
3. **Choose the kind**: *bug* or *change* (see the two forms below). A small bugfix or chore needs only the bug form.
4. **Write the issue** in a file in the scratchpad. Show it to the user. Create it only after the user agrees:
   `gh issue create --title "<title>" --body-file <file> --label <bug|enhancement>`
   To change an existing issue: `gh issue edit <N> --body-file <file>`.
5. **Summarize**: give the issue number and link, and any open question that blocks the work.

## Title

One line. Say what is wrong or what changes, in plain words. Not "Improve X". Good: "Tor starts before the onion service is configured, so the node announces an onion it does not host".

## Form: bug

- **Context**: where and how you found it (version or commit, setup).
- **Bug**: what happens, with `path:line` for the cause. Say what the user sees.
- **Repro**: exact steps or a script, and the real output. A table of "now" against "expected" is good.
- **Fix**: the proposed fix. Name an alternative if one exists and say why you did not choose it.

## Form: change

- **Problem**: who is affected and what they cannot do. Why now. 1-3 paragraphs.
- **Proposal**: numbered, specific points. Mark a breaking change **BREAKING**. Name new config keys, commands or messages.
- **Expected behavior**: the contract, as testable scenarios. Observable behavior only: inputs, outputs, errors, limits. No class names, no libraries, no steps.
  - **Requirement**: one sentence with SHALL or MUST. One behavior for each requirement.
  - **Scenario**: `WHEN` or `GIVEN` a condition, `THEN` the result. Each requirement has at least one. Add the error case.
- **Out of scope**: what you reject and why.
- **Decisions**: each choice with the alternatives you rejected and the reason.
- **Risks and trade-offs**: `[Risk] → Mitigation`.
- **Impact**: code areas, config keys (`docs/CONFIG.md`, `config.example.yaml`), APIs, dependencies, migration. Keep it short.
- **Open questions**: only things that cannot change the proposal. Resolve the rest with the user now. If a question depends on another issue, write `Depends on #N` and say what stays true if that issue chooses differently.

Leave out a section that has nothing to say. Do not write "N/A".

## Progress

Track the work in the issue, not in a file. A short checklist in the body or in a comment is enough (`- [ ] step`). Use it only when the work has several PRs or several people.

## Rules

- Be specific. Use real names, paths, numbers and output. No filler.
- Use English technical writing (see the `asd-ste100` skill): short sentences, one idea each, active voice.
- Do not state the same behavior in two places. Link `docs/*.md` pages instead of copying them.
- Do not paste secrets, tokens or private data in an issue. Issues are public.
- Never create, edit or close an issue without the user's agreement.
