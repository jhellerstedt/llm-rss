#!/usr/bin/env python3
"""Export Zulip stream (and optional topic) message history as Markdown for drafting llm-rss config."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import toml
import typer
from dotenv import load_dotenv

from zulip_context import (
    _client_for_realm,
    fetch_messages_narrow,
    format_messages,
    load_zulip_realms,
)

load_dotenv()

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _realms_config_path(
    config: Optional[Path],
    realms_file: Optional[str],
) -> Optional[str]:
    if realms_file:
        return str(Path(realms_file).expanduser())
    if config is not None and config.is_file():
        cfg = toml.load(config)
        z = cfg.get("zulip") or {}
        p = z.get("realms_config_file")
        if p:
            return str(Path(p).expanduser())
    return None


def _client(realm: str, config: Optional[Path], realms_file: Optional[str]):
    try:
        import zulip  # noqa: F401
    except ImportError as e:
        typer.secho(
            "Install the 'zulip' package (pip install -r requirements.txt).",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1) from e

    rf = _realms_config_path(config, realms_file)
    realms = load_zulip_realms(config_file=rf)
    key = realm.lower()
    if key not in realms:
        typer.secho(
            f"Unknown realm {realm!r}. Configured: {sorted(realms)}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    return _client_for_realm(realms, key), key


@app.command("list-realms")
def list_realms_cmd(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="TOML config whose [zulip] realms_config_file is used (optional).",
    ),
    realms_file: Optional[str] = typer.Option(
        None,
        "--realms-file",
        "-r",
        help="Path to zulip realms JSON (overrides config and defaults).",
    ),
) -> None:
    """Print realm names from zulip_realms.json, env, or --realms-file / --config."""
    rf = _realms_config_path(config, realms_file)
    realms = load_zulip_realms(config_file=rf)
    if not realms:
        typer.secho(
            "No realms found. Add zulip_realms.json, set ZULIP_REALMS_CONFIG_FILE, "
            "or ZULIP_REALM_<NAME>_EMAIL / _API_KEY / _SITE.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    for name in sorted(realms):
        site = realms[name].get("site", "")
        typer.echo(f"- `{name}` — {site}")


@app.command("list-streams")
def list_streams_cmd(
    realm: str = typer.Argument(..., help="Realm key from zulip_realms.json (or env)."),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    realms_file: Optional[str] = typer.Option(None, "--realms-file", "-r"),
    include_public: bool = typer.Option(
        True,
        "--include-public/--no-include-public",
        help="Include public channels the bot/user can access (Zulip API default).",
    ),
) -> None:
    """List channels as Markdown (name, id, description) for picking `stream` values."""
    client, rkey = _client(realm, config, realms_file)
    result = client.get_streams(include_public=include_public)
    if result.get("result") != "success":
        typer.secho(f"get_streams failed: {result!r}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    streams = result.get("streams") or []
    typer.echo(f"# Zulip channels — realm `{rkey}`\n")
    typer.echo("| name | stream_id | description |")
    typer.echo("| --- | ---: | --- |")
    for s in sorted(streams, key=lambda x: (x.get("name") or "").lower()):
        name = (s.get("name") or "").replace("|", "\\|")
        sid = s.get("stream_id", "")
        desc = (s.get("description") or "").replace("\n", " ").replace("|", "\\|")[:200]
        typer.echo(f"| {name} | {sid} | {desc} |")
    typer.echo("\nUse the **name** column as `stream = \"...\"` in `zulip_sources`.")


@app.command("list-topics")
def list_topics_cmd(
    realm: str = typer.Argument(...),
    stream: str = typer.Argument(..., help="Exact channel name."),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    realms_file: Optional[str] = typer.Option(None, "--realms-file", "-r"),
) -> None:
    """List recent topics in a channel (for optional `topic` in zulip_sources)."""
    client, rkey = _client(realm, config, realms_file)
    sid_r = client.get_stream_id(stream)
    if sid_r.get("result") != "success":
        typer.secho(f"get_stream_id failed: {sid_r!r}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    stream_id = sid_r["stream_id"]
    result = client.get_stream_topics(stream_id)
    if result.get("result") != "success":
        typer.secho(f"get_stream_topics failed: {result!r}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    topics = result.get("topics") or []
    typer.echo(f"# Topics in `{stream}` — realm `{rkey}`\n")
    for t in topics:
        name = t.get("name", "")
        max_id = t.get("max_id", "")
        typer.echo(f"- `{name}` (latest message id: {max_id})")


@app.command("export")
def export_cmd(
    realm: str = typer.Argument(...),
    streams: list[str] = typer.Option(
        ...,
        "--stream",
        "-s",
        help="Channel name; pass multiple times for several sections.",
    ),
    topic: Optional[str] = typer.Option(
        None,
        "--topic",
        "-t",
        help="Only when a single --stream is given: narrow to this topic.",
    ),
    lookback_hours: int = typer.Option(168, "--lookback-hours", "-l", min=1),
    max_messages: int = typer.Option(400, "--max-messages", "-m", min=1),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    realms_file: Optional[str] = typer.Option(None, "--realms-file", "-r"),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write Markdown here; default is stdout.",
    ),
    no_preamble: bool = typer.Option(False, "--no-preamble", help="Omit intro and TOML hint."),
) -> None:
    """Fetch messages and write Markdown suitable for pasting into Kagi (or similar)."""
    if topic is not None and len(streams) != 1:
        typer.secho("--topic requires exactly one --stream", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)

    client, rkey = _client(realm, config, realms_file)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    if not no_preamble:
        lines.append("# Zulip context export (llm-rss)\n")
        lines.append(
            "Use this to draft `[[groups]]` entries: `research_areas`, `excluded_areas`, "
            "and `zulip_sources` in `config.d/config.toml`.\n"
        )
        lines.append(f"- **Realm:** `{rkey}`")
        lines.append(f"- **Generated:** {now}")
        lines.append(f"- **Lookback:** {lookback_hours} h, **max messages per stream:** {max_messages}\n")

        toml_lines = ["zulip_sources = ["]
        if topic:
            toml_lines.append(
                f'  {{ realm = "{rkey}", stream = "{streams[0]}", topic = "{topic}", '
                f"lookback_hours = {lookback_hours}, max_messages = {max_messages} }},"
            )
        else:
            for s in streams:
                toml_lines.append(
                    f'  {{ realm = "{rkey}", stream = "{s}", lookback_hours = {lookback_hours}, '
                    f"max_messages = {max_messages} }},"
                )
        toml_lines.append("]")
        lines.append("## Suggested `zulip_sources` snippet\n")
        lines.append("```toml")
        lines.extend(toml_lines)
        lines.append("```\n")
        lines.append("## Messages\n")

    for s in streams:
        label = f"{rkey}/{s}" + (f"/{topic}" if topic else "")
        try:
            msgs = fetch_messages_narrow(client, s, topic, lookback_hours, max_messages)
        except Exception as e:
            typer.secho(f"Fetch failed for {label}: {e}", err=True, fg=typer.colors.RED)
            raise typer.Exit(1) from e
        body = format_messages(msgs)
        lines.append(f"### `{label}`\n")
        if body:
            lines.append(body)
        else:
            lines.append("_No messages in this window._")
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        typer.secho(f"Wrote {output}", err=False)
    else:
        typer.echo(text, nl=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
