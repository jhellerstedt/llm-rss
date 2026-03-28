# Agent instructions (Cursor / AI)

This file is for **contributors** using AI coding assistants. It does not affect running LLM-RSS.

## Instruction priority

1. **Your explicit instructions** — this file, chat, and any other project rules you add.
2. **Superpowers skills** (if the [Superpowers](https://github.com/obra/superpowers) Cursor plugin is installed) — workflow habits like debugging and verification.
3. **Default assistant behavior** — everything else.

If something here conflicts with a Superpowers skill, follow **this file and the user**.

## Suggested workflows for this repo

- **New behavior or features** — Use the **brainstorming** skill first, then **writing-plans** if the change is multi-step.
- **Bugs or surprising behavior** — Use **systematic-debugging** before changing code.
- **Claiming “done,” opening a PR, or merging** — Use **verification-before-completion**: run whatever checks exist (see below) and show evidence in the thread.

This repository is a small Python codebase (`main.py`, adapters, Kagi/Zulip clients). There is **no automated test suite** in-tree yet; verification usually means running the app against a sample config and confirming RSS output or a dry run, unless you add tests.

## Skill and command reference

A **snapshot** of Superpowers skills, subagents, hooks, and deprecated slash commands lives in [docs/superpowers-plugin.md](docs/superpowers-plugin.md). The **source of truth** is whatever version of the plugin you have installed; that doc can lag behind plugin updates.
