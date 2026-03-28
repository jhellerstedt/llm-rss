# LLM-RSS

## Introduction

LLM-RSS reads titles and abstracts from science RSS feeds (Nature, arXiv, APS, and others), scores each item with **Kagi** (FastGPT API) against your research areas and optional **Zulip** discussion context, and writes filtered RSS XML you can host (for example with nginx) and subscribe to in Zotero.

## Installation

1. **Clone the repository** and enter the project directory.

2. **Create a virtual environment** (recommended) and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure Kagi**
   - Create a [Kagi](https://kagi.com) account and open **Settings → Advanced → API portal** to generate an API token.
   - Add **API credits** (FastGPT is billed per query; see [FastGPT API](https://help.kagi.com/kagi/api/fastgpt.html)).
   - Set `KAGI_API_KEY` in the environment or put `api_key` under `[kagi]` in your TOML config.
   - Requests use `Authorization: Bot <token>` against `https://kagi.com/api/v0/fastgpt` by default.

4. **Configure the app**
   - Copy [config.d/config.toml.example](config.d/config.toml.example) to `config.d/config.toml` and edit.
   - Use `[[groups]]` for separate topical feeds (each group has its own `urls`, thresholds, and `rss_path`).
   - With no `groups` key, a **legacy** single-group layout is still supported: top-level `urls`, `research_areas`, `excluded_areas`, and `rss_path`.

5. **Optional: Zulip context**
   - Install credentials the same way as in **zulip-kagi-bot**: either a JSON file mapping realm name to `{ "email", "api_key", "site" }`, or environment variables `ZULIP_REALM_<NAME>_EMAIL`, `ZULIP_REALM_<NAME>_API_KEY`, `ZULIP_REALM_<NAME>_SITE`.
   - Point `[zulip] realms_config_file` at that JSON, or place `zulip_realms.json` in the working directory, or set `ZULIP_REALMS_CONFIG_FILE`.
   - Per group, set `zulip_sources` to a list of tables: `realm`, `stream`, optional `topic`, `lookback_hours`, `max_messages`.
   - If raw context exceeds `context_max_chars`, the tool calls Kagi’s **Universal Summarizer** with engine `muriel` (Research) once to compress it before scoring.

## Running

Process every `*.toml` in `config.d/`:

```bash
python main.py
```

Single config file:

```bash
python main.py --config-path config.d/config.toml
```

Dry run (no XML written):

```bash
python main.py --dryrun
```

Cron example (daily at midnight):

```bash
0 0 * * * cd /path/to/llm-rss && /path/to/.venv/bin/python main.py
```

## Supported RSS providers

- Nature
- arXiv
- APS (American Physical Society)
- bioRxiv
- Cell
- AIP (American Institute of Physics)
- IOP (Institute of Physics)

## Hosting via nginx

`docker-compose.yaml` mounts `./data` at `/data/`. With the default nginx config, feeds are served under `/rss/` (for example `http://localhost:8080/rss/cm_physics.xml` if your group writes `data/cm_physics.xml`).

## Zotero

Add the hosted XML URL to Zotero as an RSS feed.

## Contributing

For AI-assisted editing in Cursor, see [AGENTS.md](AGENTS.md) (includes a pointer to the Superpowers skill reference under [docs/superpowers-plugin.md](docs/superpowers-plugin.md)).
