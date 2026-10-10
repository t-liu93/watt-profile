# Agent Guide

> **English — source of truth** · [中文](AGENTS_zh.md)

This file contains stable working rules for agents in this repository. Keep project scope, architecture, milestone decisions, commands, and progress in dedicated project documents once they exist. Do not assume that a planned file, service, or command already exists.

## Language and documentation

- Reply in the user's language. Tracked project documentation is English-first and has a semantically equivalent Chinese mirror: `name.md` and `name_zh.md`. Update both in the same change. If they disagree, the English version is authoritative.
- Link each language pair immediately below its H1, and link to the matching language when referencing another mirrored document.
- Keep this guide concise. Add stable repository rules here when they are agreed; keep changing plans and execution evidence elsewhere.
- Keep each milestone in one English Markdown file and its Chinese mirror under `docs/milestone/`, such as `m1.md` and `m1_zh.md`. Put its design, contracts, atomic task sections, and acceptance record in that document; do not split them into additional milestone files.

## Everyday work

- Single-agent work is the default. Start delegated orchestration only when the user explicitly requests orchestration or subagents. An ordinary request to implement a task does not activate the delegated review loop.
- Work within the requested scope in small, reviewable steps. Preserve unrelated user changes. Resolve routine implementation choices from the available context; ask when a missing decision materially changes the result.
- Read the relevant current task card and source before editing. Use applicable project checks once they exist. Verify behavior in proportion to the change and report passed checks, unrun checks, and material limitations.
- Use synthetic or sanitized test data. Never commit secrets or real private data. Do not connect to or alter production services, devices, databases, or files during implementation or verification.

## Delegated orchestration

Each task card should state its boundaries, acceptance criteria, and relevant checks. Complete one task before advancing to a dependent task. The orchestrator owns task selection, handoffs, integration, and Git operations when agents share a worktree; agents must not perform concurrent commits or history rewrites in the same worktree.

| Role | Default model | Reasoning | When to change it |
| --- | --- | --- | --- |
| Orchestrator | `gpt-6.1-sol` | `medium` | Use Sol `high` only when cross-step coordination needs deeper reasoning. |
| Implementer / fixer | `gpt-6.1-luna` | `medium` | Use Sol `medium` for concurrency, authentication, money, data migration, or substantial cross-module contracts. |
| Reviewer | `gpt-6.1-sol` | `medium` | Raise effort only when concrete task risk justifies it; `high` is not the default. |

- User-selected model and reasoning settings override these defaults. Set models through the harness's actual controls; a prompt or this file cannot switch the current parent session's model. If a requested setting is unavailable, state the limitation and use an available suitable setting without claiming a switch occurred.
- Do not select `gpt-6.1-astra` automatically. Explain the specific unresolved risk to the user and let them decide whether to use it.
- Start each reviewer in a fresh agent context. Its task-specific input is **only the original task card and the current task diff**. Do not pass implementation reports, fixer narratives, prior review opinions, or the orchestrator's preferred verdict. The reviewer may inspect relevant repository code and rules and independently run appropriate checks.
- A review finding blocks acceptance only when it demonstrates a violated acceptance criterion, a reproducible functional defect, or a realistic security, data, or compatibility risk. State the evidence and required correction. Style preferences, speculative extreme cases, and improvements outside the task may be noted, but do not trigger repair or prevent a pass.
- If review passes, continue without a per-round report. For blocking findings, save a concise temporary review note and pass it, the task card, and the current diff to a fresh fixer. A fresh reviewer then examines the original task card and revised cumulative diff without the fixer's account. Allow at most **three** automatic repair and re-review rounds per task; report unresolved blockers to the user instead of continuing the loop.

## Temporary work and reports

- Implementation, review, and repair handoffs are temporary. An implementer need not write a report. Keep blocking review notes only while they are needed for repair, then remove them after the task passes or the orchestration ends.
- Save only two owner-facing outputs for each completed orchestration run under `development-notes/<run-id>/`: a Chinese `summary.md` and a Chinese `walkthrough.md`. This directory stays local and is never committed. The summary records completed scope, checks and outcomes, significant resolved or unresolved issues, and any pending work. The walkthrough gives exact startup, user actions, expected results, and cleanup steps for a temporary environment.
- Run verification with isolated temporary configuration, databases, containers, directories, and test data as applicable. Track resources created for the run and remove them when finished. Never delete or reset pre-existing resources as part of cleanup. Manual walkthroughs must also use a temporary environment and must not affect production.

## Git and delivery

- Keep commits scoped and use English Conventional Commit messages without AI attribution or `Co-Authored-By` trailers. Do not include unrelated changes.
- Outside orchestration, create a local commit only when the user explicitly asks. An explicit request for orchestrated implementation authorizes local implementation commits, repair fixups, and per-task squashing of **unpublished** commits within the requested scope.
- In orchestration, create one implementation commit per task after its checks pass. Commit review-driven repairs as fixups of that task's implementation commit; after acceptance, fold only that task's fixups into its implementation commit. Verify exact commit boundaries and a clean worktree before rewriting unpublished history.
- Pushing, publishing, tagging, force-pushing, or rewriting published history requires separate explicit authorization. Never treat an orchestration request as authorization for those actions.
- These Git rules apply once the repository has usable Git history. Do not claim a diff, commit, or review boundary that does not exist.
