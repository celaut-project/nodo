# OpenSpec in this repository

This repository uses the OpenSpec method (spec-driven development) **without the OpenSpec CLI**.
The skills in `.agents/skills/` are guides. They tell the agent which files to write and how.
No Node.js and no `openspec` binary are needed.

## Layout

```
openspec/
  config.yaml          project context and per-artifact rules (read before writing)
  templates/           starting point for each artifact
  specs/<capability>/spec.md      main specs: what the system does now
  changes/<change>/               one folder per change in progress
    .openspec.yaml                metadata (see below)
    proposal.md  design.md  tasks.md
    specs/<capability>/spec.md    delta specs: what this change adds or alters
  changes/archive/YYYY-MM-DD-<change>/   finished changes
```

`<capability>` may have path segments (`identity/user-auth`). Use kebab-case.
Name a capability for a lasting behavior (`user-auth`), not for the work (`add-login-endpoint`).

`.openspec.yaml` has this form:

```yaml
schema: spec-driven
created: 2026-11-17
# skip_specs: true        # only for changes with no behavior change (refactor, tooling, docs)
# retire_capabilities: true  # only when a change removes a whole capability
```

## Workflow

1. `openspec-explore`: think and investigate. No files are written without approval.
2. `openspec-propose`: create the change and all planning artifacts. No code.
3. `openspec-update-change`: revise the artifacts and keep them consistent.
4. `openspec-apply-change`: implement `tasks.md` and tick the boxes.
5. `openspec-sync-specs`: merge delta specs into the main specs.
6. `openspec-archive-change`: sync, then move the change to `changes/archive/`.

Artifact order: `proposal` → `specs` and `design` → `tasks`.
An artifact is "done" when its file exists. `design.md` is optional.

## Spec format

A delta spec uses these sections: `## ADDED Requirements`, `## MODIFIED Requirements`,
`## REMOVED Requirements`, `## RENAMED Requirements`.

- Requirement: `### Requirement: <name>`. Use SHALL or MUST. One behavior per requirement.
  Keep the text before the first scenario under 500 characters.
- Scenario: `#### Scenario: <name>` (exactly four `#`). Each has `**WHEN**` or `**GIVEN**`,
  and `**THEN**`. Every requirement has at least one scenario.
- MODIFIED: copy the whole existing requirement (text and all scenarios), then edit it.
  A partial copy loses content at sync time.
- REMOVED: add `**Reason**` and `**Migration**`.
- RENAMED: `- FROM: \`### Requirement: Old\`` and `- TO: \`### Requirement: New\``.
- A delta for a **new** capability starts with `## Purpose` (one or two sentences).
  A delta for an existing capability has no Purpose.
- A spec states observable behavior. It does not name classes, libraries or steps.

A main spec has no delta headers. It looks like this:

```markdown
# <capability> Specification

## Purpose
<one or two sentences>

## Requirements

### Requirement: <name>
The system SHALL ...

#### Scenario: <name>
- **WHEN** ...
- **THEN** ...
```

## Tasks format

`tasks.md` uses numbered groups and checkboxes: `- [ ] 1.1 Description and how to verify`.
Only `x` or `X` in the box means done. Each group carries its own tests and docs.

To count progress (optional helper):

```bash
python3 -c "import re,sys;t=open(sys.argv[1]).read();a=re.findall(r'^\s*- \[(.)\]',t,re.M);print(sum(c in 'xX' for c in a),'/',len(a))" openspec/changes/<change>/tasks.md
```

## Checks before archive (manual)

- Every requirement has a scenario. Every scenario heading has four `#`.
- Each delta header in a main spec is gone after sync.
- Each new capability has a real Purpose, not `TBD`.
