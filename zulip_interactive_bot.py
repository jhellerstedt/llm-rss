"""Always-on Zulip event listener for author-whitelist commands.

Independent of the feed cron. See docs/superpowers/specs/2026-09-08-interactive-zulip-whitelist-bot-design.md
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import toml
from dotenv import load_dotenv

from author_whitelist import AuthorWhitelist
from author_whitelist_bot import (
    _message_text,
    _react,
    format_error_reply,
    handle_command,
    parse_command,
)
from api_usage import record_zulip_api
from zulip_context import _client_for_realm, _normalize_ts, load_zulip_realms

logger = logging.getLogger(__name__)

_CATCHUP_HOURS = 24
_CATCHUP_MAX = 200


def interactive_cursor_key(
    realm: str, kind: str, stream: str | None = None
) -> str:
    if kind == "dm":
        return f"interactive:{realm}:dm"
    return f"interactive:{realm}:{stream}"


def should_handle_message(
    message: dict[str, Any],
    *,
    bot_email: str,
    stream: str,
    dms: bool,
    flags: list[str] | None = None,
) -> bool:
    sender = (message.get("sender_email") or "").lower()
    if sender == (bot_email or "").lower():
        return False
    flag_list = list(flags if flags is not None else message.get("flags") or [])
    mtype = message.get("type")
    if mtype == "private":
        return bool(dms)
    if mtype != "stream":
        return False
    rec = message.get("display_recipient")
    name = rec if isinstance(rec, str) else str(message.get("stream") or "")
    if name != stream:
        return False
    return "mentioned" in flag_list


def _reply_payload(message: dict[str, Any], content: str) -> dict[str, Any]:
    if message.get("type") == "private":
        return {
            "type": "private",
            "to": [message.get("sender_email")],
            "content": content,
        }
    rec = message.get("display_recipient")
    stream = rec if isinstance(rec, str) else str(message.get("stream") or "")
    topic = message.get("subject") or message.get("topic") or "(no topic)"
    return {
        "type": "stream",
        "to": stream,
        "topic": topic,
        "content": content,
    }


def _send_reply(client, message: dict[str, Any], content: str, dryrun: bool) -> None:
    payload = _reply_payload(message, content)
    if dryrun:
        logger.info("[interactive dryrun] would reply: %s", content)
        return
    try:
        client.send_message(payload)
        record_zulip_api(1)
    except Exception:
        logger.exception("Failed to send interactive-bot reply")


def _fetch_recent(
    client,
    narrow: list[dict[str, str]],
    lookback_hours: int,
    max_messages: int,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_ts = cutoff.timestamp()
    collected: list[dict[str, Any]] = []
    anchor: str | int = "newest"
    num_before = min(max(1, max_messages), 5000)
    while len(collected) < max_messages:
        result = client.get_messages(
            {
                "anchor": anchor,
                "num_before": num_before,
                "num_after": 0,
                "narrow": narrow,
            }
        )
        if result.get("result") != "success":
            logger.warning("Zulip get_messages failed: %s", result.get("msg", result))
            break
        record_zulip_api(1)
        messages = result.get("messages") or []
        if not messages:
            break
        for msg in messages:
            ts = _normalize_ts(msg.get("timestamp") or 0)
            if ts >= cutoff_ts:
                collected.append(msg)
        if len(collected) >= max_messages:
            break
        oldest = min(messages, key=lambda m: m.get("id", 0))
        anchor = oldest.get("id")
        if len(messages) < num_before:
            break
        newest_ts = max(_normalize_ts(m.get("timestamp") or 0) for m in messages)
        if newest_ts < cutoff_ts:
            break
    collected.sort(key=lambda m: m.get("id", 0))
    if len(collected) > max_messages:
        collected = collected[-max_messages:]
    return collected


def catch_up_messages(
    client,
    *,
    stream: str,
    dms: bool,
) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    msgs.extend(
        _fetch_recent(
            client,
            [
                {"operator": "stream", "operand": stream},
                {"operator": "is", "operand": "mentioned"},
            ],
            _CATCHUP_HOURS,
            _CATCHUP_MAX,
        )
    )
    if dms:
        msgs.extend(
            _fetch_recent(
                client,
                [{"operator": "is", "operand": "private"}],
                _CATCHUP_HOURS,
                _CATCHUP_MAX,
            )
        )
    by_id: dict[int, dict[str, Any]] = {}
    for m in msgs:
        by_id[int(m.get("id", 0))] = m
    return [by_id[k] for k in sorted(by_id)]


def process_interactive_message(
    client,
    message: dict[str, Any],
    *,
    flags: list[str] | None,
    bot_email: str,
    realm: str,
    stream: str,
    dms: bool,
    whitelist: AuthorWhitelist,
    wl_path: Path,
    mailto: str | None,
    dryrun: bool = False,
) -> bool:
    """Handle one Zulip message. Returns True if the whitelist JSON changed."""
    if not should_handle_message(
        message,
        bot_email=bot_email,
        stream=stream,
        dms=dms,
        flags=flags,
    ):
        return False
    kind = "dm" if message.get("type") == "private" else "stream"
    key = interactive_cursor_key(realm, kind, stream)
    mid = int(message.get("id") or 0)
    if mid <= whitelist.get_cursor(key):
        return False
    cmd = parse_command(_message_text(message))
    action, arg = cmd if cmd is not None else ("help", "")
    changed = False
    success = False
    try:
        reply, success, cmd_changed = handle_command(
            whitelist,
            action,
            arg,
            mailto=mailto,
            added_by=message.get("sender_email"),
            wl_path=wl_path if action in ("add", "remove") else None,
        )
        changed = cmd_changed
        _send_reply(client, message, reply, dryrun)
    except Exception as e:
        logger.exception("[interactive] command failed")
        _send_reply(
            client,
            message,
            format_error_reply(f"unexpected error ({e})"),
            dryrun,
        )
    _react(client, mid, success=success, dryrun=dryrun)
    whitelist.set_cursor(key, mid)
    if not dryrun:
        whitelist.save(wl_path)
    return changed


def _interactive_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    aw_cfg = dict(cfg.get("author_whitelist") or {})
    inter = dict(aw_cfg.get("interactive") or {})
    return aw_cfg, inter


def run_listener(
    *,
    config_path: Path,
    dryrun: bool = False,
) -> None:
    cfg = toml.load(config_path)
    aw_cfg, inter = _interactive_config(cfg)
    if not aw_cfg.get("enabled", True):
        logger.error("[author_whitelist] is disabled")
        sys.exit(1)
    realm = (inter.get("realm") or "").strip()
    stream = (inter.get("stream") or "").strip()
    if not realm or not stream:
        logger.error(
            "[author_whitelist.interactive] requires realm and stream"
        )
        sys.exit(1)
    dms = bool(inter.get("dms", True))

    zulip_cfg = dict(cfg.get("zulip") or {})
    realms_path_cfg = zulip_cfg.get("realms_config_file")
    if realms_path_cfg:
        rp = Path(realms_path_cfg)
        if not rp.is_absolute():
            rp = (config_path.parent / rp).resolve()
        realms_path = str(rp)
    else:
        realms_path = os.environ.get("ZULIP_REALMS_CONFIG_FILE")
    realms = load_zulip_realms(
        config_file=realms_path, config_dir=config_path.parent
    )
    if realm not in realms:
        logger.error("Unknown Zulip realm %r; known: %s", realm, sorted(realms))
        sys.exit(1)

    wl_file = aw_cfg.get("file", "author_whitelist.json")
    wl_path = Path(wl_file)
    if not wl_path.is_absolute():
        wl_path = (config_path.parent / wl_path).resolve()
    whitelist = AuthorWhitelist.load(wl_path)

    openalex_cfg = dict(cfg.get("openalex") or {})
    mailto = openalex_cfg.get("mailto") or os.environ.get("OPENALEX_MAILTO")
    client = _client_for_realm(realms, realm)
    bot_email = (realms.get(realm) or {}).get("email", "")

    logger.info(
        "Interactive author-whitelist bot: realm=%s stream=%s dms=%s file=%s",
        realm,
        stream,
        dms,
        wl_path,
    )

    try:
        pending = catch_up_messages(client, stream=stream, dms=dms)
    except Exception:
        logger.exception("Catch-up fetch failed; continuing to event queue")
        pending = []
    for msg in pending:
        flags = list(msg.get("flags") or [])
        if msg.get("type") == "stream" and "mentioned" not in flags:
            flags.append("mentioned")
        process_interactive_message(
            client,
            msg,
            flags=flags,
            bot_email=bot_email,
            realm=realm,
            stream=stream,
            dms=dms,
            whitelist=whitelist,
            wl_path=wl_path,
            mailto=mailto,
            dryrun=dryrun,
        )

    def _on_event(event: dict[str, Any]) -> None:
        if event.get("type") != "message":
            return
        msg = event.get("message") or {}
        flags = list(event.get("flags") or msg.get("flags") or [])
        process_interactive_message(
            client,
            msg,
            flags=flags,
            bot_email=bot_email,
            realm=realm,
            stream=stream,
            dms=dms,
            whitelist=whitelist,
            wl_path=wl_path,
            mailto=mailto,
            dryrun=dryrun,
        )

    logger.info("Listening for Zulip message events")
    client.call_on_each_event(_on_event, event_types=["message"])


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Interactive Zulip author-whitelist bot"
    )
    parser.add_argument(
        "--config-path",
        default="config.d/config.toml",
        type=Path,
    )
    parser.add_argument("--dryrun", action="store_true")
    args = parser.parse_args(argv)
    run_listener(config_path=args.config_path, dryrun=args.dryrun)


if __name__ == "__main__":
    main()
