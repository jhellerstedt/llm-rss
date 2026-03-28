# Superpowers plugin — contributor reference

This is a **snapshot** of the Cursor **Superpowers** plugin for people working on **llm-rss**. It is not authoritative: the installed plugin and its docs may add, rename, or remove skills over time.

## Why this file exists

- **AGENTS.md** (repo root) is the right place for *project-specific* agent rules; Cursor and many tools load it by default.
- This file holds the **generic** Superpowers catalog so we do not bloat **README.md** (which is for RSS users, not editor setup).

## How Superpowers expects you to work (summary)

- If a skill might apply — even slightly — **use it** rather than improvising (see **using-superpowers** in the plugin).
- **Process skills first** (e.g. brainstorming, systematic-debugging), then implementation-oriented habits.
- **Rigid** skills (e.g. TDD, debugging): follow the discipline; **flexible** skills: adapt to context.

## Skills

| Skill | When to use |
|--------|-------------|
| **using-superpowers** | Start of work — how skills fit together; check skills before acting. |
| **brainstorming** | Before creative work: features, components, behavior changes. |
| **writing-plans** | Multi-step work from a spec — plan before coding. |
| **executing-plans** | Run a written plan in another session with review steps. |
| **subagent-driven-development** | Same session: execute a plan with parallel/independent tasks. |
| **test-driven-development** | Features and bugfixes — tests before implementation (when you are adding or extending tests). |
| **systematic-debugging** | Bugs, failing tests, odd behavior — before guessing fixes. |
| **verification-before-completion** | Before “done,” merge, or PR — run checks and show evidence. |
| **dispatching-parallel-agents** | Several independent tasks with no shared ordering. |
| **using-git-worktrees** | Isolated branches/dirs for features or plan execution. |
| **requesting-code-review** | After bigger changes or before merge. |
| **receiving-code-review** | When applying review feedback — verify, don’t rubber-stamp. |
| **finishing-a-development-branch** | Work is done and tested — merge vs PR vs cleanup. |
| **writing-skills** | Authoring or editing skills and checking they work. |

## Subagents

- **code-reviewer** — After a solid chunk of implementation, compare it to the plan and standards.

## Hooks

- **sessionstart** — Runs at session start (label may appear as `sessionStart` in configuration).

## Slash commands (legacy)

Prefer the skills; these may still exist as `/` commands:

| Command | Use instead |
|---------|----------------|
| `/brainstorm` | **brainstorming** |
| `/write-plan` | **writing-plans** |
| `/execute-plan` | **executing-plans** |

## Invoking skills

- Name a skill in chat, use `/` commands if your setup still maps them, or rely on the agent to load skills when relevant.
- **Rules and hooks** from the plugin may run without extra setup.

## See also

- [AGENTS.md](../AGENTS.md) — project-level agent instructions and priority order.
