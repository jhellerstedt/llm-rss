# Interactive Zulip author-whitelist bot — design

Status: approved (2026-09-08 brainstorm)
Date: 2026-09-08

## Problem

Author-whitelist commands (`add` / `remove` / `list`) only run inside the
llm-rss feed job, so Zulip commands are not answered until the next RSS run.
There is no `help` command. The original whitelist spec (2026-06-15)
explicitly chose “no always-on daemon.”

We want an **always-on Docker process** that listens on Zulip’s event queue
and replies immediately, independent of the feed cron. The feed job still
**reads** the whitelist when scoring; it no longer **polls** Zulip for
commands when the interactive bot is configured.

## Decisions (locked)

| Topic | Decision |
|-------|----------|
| Process | Long-running Docker container; not the feed cron |
| Zulip user | The same bot account already used for the configured realm (credentials in `zulip_realms.json` / env) |
| Where | One configured stream (any topic) **and** optional DMs |
| Stream gating | Command runs only if the message `flags` include `mentioned` (not `wildcard_mentioned` / `@all` alone) |
| DMs | No mention required (when `dms = true`) |
| Commands | `help`, `list`, `add <ORCID or Scholar URL>`, `remove <ORCID/OpenAlex id/name>` |
| Bare mention / unknown command | Reply with help text |
| Cron poll | Skip `run_author_whitelist_bot` when `[author_whitelist.interactive]` is set |
| Cron fallback | None. If the container is down, commands wait until it is back (plus startup catch-up) |
| Storage | Same `author_whitelist.json` (path from config); the interactive bot is the only writer while it is enabled |
| RSS effect | Force-include still applies on the **next** feed run, not at command time |
| Host | Same checkout as the feed job; share `config.d/` (or the configured whitelist path) via a bind mount |

## Architecture

| Unit | File | Responsibility |
|------|------|----------------|
| Event loop | `zulip_interactive_bot.py` (new) | Connect as the realm bot; filter events; catch-up on start; dispatch commands; reply |
| Command handlers | `author_whitelist_bot.py` | Extend `parse_command` with `help`; keep `format_*` replies; extract `handle_command(...)` used by the poll path and the listener |
| Store | `author_whitelist.py` | Unchanged schema; reload from disk before mutate; persist catch-up cursor in existing `cursor` map |
| Resolver | `author_resolve.py` | Unchanged (`add` still hits ORCID/OpenAlex) |
| Cron wire-up | `main.py` | Load whitelist for scoring; **do not** poll Zulip when `interactive` is present |
| Package | `Dockerfile`, `docker-compose.yaml`, `.dockerignore` | `zulip-bot` service, no ports |
| Docs / example | `README.md`, `config.d/config.toml.example` | How to talk to the bot and how to run the container |

### Data flow

```
Zulip message event
  └─ ignore if sender is the bot
  └─ if type=private and dms enabled → handle
  └─ if type=stream and stream name equals config `stream`
        and flags contain "mentioned" → handle
        (wildcard / @all without a direct mention is ignored)
  └─ else ignore

handle:
  strip @mention → parse_command
  if missing/unknown → help reply
  if list → format_list_reply (name + ORCID, else OpenAlex id)
  if add → reload JSON, resolve(), add(), save, reply, :+1:/:-1:
  if remove → reload JSON, remove(), save, reply, :+1:/:-1:
  advance cursor (message id)

feed job main.py (unchanged RSS path)
  └─ AuthorWhitelist.load(file) for force-include
  └─ skip run_author_whitelist_bot when interactive is set
```

### Catch-up after restart

On process start, before `call_on_each_event`:

1. Load whitelist JSON.
2. `get_messages` for recent DMs to the bot (if `dms`) and recent messages in
   the configured stream that mention the bot, after the stored cursor
   (lookback 24 hours, cap 200 messages, same helpers as
   `fetch_messages_narrow` where practical).
3. Handle those in id order; persist cursor.
4. Then subscribe to the event queue.

Cursor keys (in the existing `cursor` object):

- `interactive:{realm}:{stream}`
- `interactive:{realm}:dm`

Values are the last **processed Zulip message id**. Events with `id <= cursor`
are skipped.

Messages sent while the container is stopped are recovered on the next start
within that lookback. Older than 24 hours are dropped (no cron backup).

## Commands and replies

**Configured stream (mention required)** — `@` the realm bot:

```
@bot help
@bot list
@bot add https://orcid.org/0000-0002-1825-0097
@bot add https://scholar.google.com/citations?user=XXXX
@bot remove 0000-0002-1825-0097
```

**DM to the bot** — same text without a mention (when `dms = true`).

**Help text** (also used for a mention with no command, or an unrecognized
command): usage of the four commands; note that force-include applies on the
next RSS run.

**`list`:** one bullet per author: `display_name` + affiliation if present +
ORCID if present, else OpenAlex id, else store `id`. Empty: `Author whitelist
is empty.`

**`add` / `remove`:** keep `format_added_reply` / `format_removed_reply` /
`format_error_reply`. React `:+1:` on success, `:-1:` on failure, on the
**command** message (DMs included if the API allows reactions; if not, skip
reaction and still reply).

**Reply destination:** same stream topic as the command; DMs get a private
reply to the sender. Never post whitelist command output to other streams.

## Config

Example (placeholders only; copy into a gitignored local TOML):

```toml
[author_whitelist]
enabled = true
file = "author_whitelist.json"

[author_whitelist.interactive]
realm = "myrealm"
stream = "science"
dms = true
```

- `realm` / `stream` required when `interactive` is present.
- `dms` default `true`.
- Credentials: existing `zulip_realms.json` (or env) for that realm — the same
  bot user the feed job uses.
- OpenAlex `mailto` for `add`: `[openalex] mailto` or `OPENALEX_MAILTO`.

**Backward compatibility:** if `[author_whitelist.interactive]` is **absent**
and `command_source` is still set, `main.py` keeps the old topic poll. If
`interactive` is **present**, the poll is skipped even if `command_source`
remains in the file.

## Docker

- **`Dockerfile`:** `python:3.12-slim`, install `requirements.txt`, copy
  application `.py` modules (not `config.d/` secrets). `CMD` runs
  `python zulip_interactive_bot.py --config-path config.d/config.toml`.
- **Compose service `zulip-bot`:** `restart: unless-stopped`, no ports.
  Bind-mount `./config.d` and `.env` (optional). Working directory is the
  app root. The existing nginx service stays optional and is **not** required
  for the bot.
- **Run:** `docker compose up -d zulip-bot`
- **Logs:** `docker compose logs -f zulip-bot`
- **Deploy:** same git tree as the feed job. After pull, recreate the bot
  container; the next feed run picks up the poll skip. Run **one replica**
  (one event-queue consumer per bot account).

The feed cron continues to use host Python, not this image.

## Errors and operations

| Situation | Behavior |
|-----------|----------|
| Own messages, other streams, configured stream without `mentioned`, `@all` without a direct mention | Ignore |
| Bad `add` input / resolve failure | Error reply + usage; `:-1:`; loop continues |
| Zulip / OpenAlex / ORCID transient error | Log; error reply; do not crash |
| Event queue disconnect | Client reconnect; Compose restarts the process if it exits |
| Missing realm/stream or Zulip creds | Exit non-zero (unhealthy container) |
| Two writers | Must not happen: only the listener writes JSON while interactive is enabled. Reload from disk before each `add`/`remove`. Atomic `save()` already in `AuthorWhitelist.save`. |

## Tests

Unittest, no live Zulip or Docker required:

- `parse_command`: `help`; mention stripping; unknown → not a poll command
  (listener maps that to help)
- Event filter helper: configured stream + mentioned vs other stream vs no
  mention vs wildcard vs DM vs self-message
- `list` formatting includes ORCID when set
- `handle_command` add/remove with mocked `resolve` and temp JSON
- Poll skipped when `interactive` is set; poll still invoked when only
  `command_source` exists

## Out of scope

- Listening outside the configured stream (except DMs)
- A second Zulip bot user
- Instant RSS rewrite on `add`
- Webhook / public HTTP
- Cron command backup while the container is down (beyond 24h catch-up)
- Interactive commands other than help/list/add/remove
