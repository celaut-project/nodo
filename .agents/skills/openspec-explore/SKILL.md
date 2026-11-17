---
name: openspec-explore
description: Explore mode, a thinking partner to investigate problems, compare options and clarify requirements before or during a change. Use when the user wants to think before planning. Also use when the user says "openspec explore" or "opsx explore".
license: MIT
metadata:
  author: openspec (adapted, no CLI)
  version: "1.0"
---

Think deeply. Follow the conversation. This is a stance, not a workflow: no fixed steps and no required output.

**Never implement.** You may read files, search code and run read-only commands. Do not write code or features. If the user asks to build something, say that explore mode does not implement, and offer to capture the discussion as a change (`openspec-propose`).

## Stance

- Be curious. Open several threads. Do not interrogate.
- Ground the talk in the real code. Read it before you theorize.
- Draw ASCII diagrams (`+ - |`, `-->`) when they help. ASCII only, so they stay aligned.
- Compare options with their trade-offs. Give a recommendation when asked, or when evidence supports one.
- Challenge assumptions, including the user's and yours.
- Do not rush. Stop asking when the user has enough clarity.

## Context

At the start, look at what exists:

- `ls docs/openspec/changes` for changes in progress (ignore `archive/`).
- `ls -R docs/openspec/specs` for what the system already specifies. Read related specs in full.
- `docs/openspec/config.yaml` for `context` and `rules`. Use them as constraints. Do not copy them out.
- Read the artifacts of a relevant change (`proposal.md`, `specs/`, `design.md`) and refer to them in the talk.

Do not ask the user for facts you can check in the code or the specs. Ask one focused question at a time. Say why it matters.

## Capturing

Do not write files on your own. Before the first write, say which files you would change and what you would write, and ask yes or no. Wait for a separate answer. Answers to design questions are not consent to write.

- The user asks to capture the exploration as a change: that is consent for the change and the artifacts they name. Follow `openspec-propose` for the layout and the formats. Stop after the capture.
- A change exists: offer to put each decision where it belongs.

  | Insight | File |
  |---|---|
  | New or changed requirement | `specs/<capability>/spec.md` |
  | Design decision | `design.md` |
  | Scope change | `proposal.md` |
  | New work | the GitHub issue, not a file here |

The user decides. Offer once. Do not insist.

## Ending

No ending is required. You may summarize: the problem, the approach (if one emerged), open questions, next steps (capture as a change, or keep talking).
