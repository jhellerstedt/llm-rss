"""Zulip command loop for managing the author whitelist.

Runs once per cron cycle: reads new messages in a dedicated topic, applies
add/remove/list commands, replies, and advances an idempotency cursor.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from author_resolve import AuthorResolveError, resolve
from author_whitelist import AuthorWhitelist
from api_usage import record_zulip_api
from zulip_context import _client_for_realm, fetch_messages_narrow

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 168
_DEFAULT_MAX_MESSAGES = 200


def parse_command(content: str) -> tuple[str, str] | None:
    """Return (action, arg) for add/remove/list, else None."""
    text = (content or "").strip()
    text = re.sub(r"^@(?:\*\*[^*]+\*\*|[\w.\-]+)\s+", "", text).strip()
    if not text:
        return None
    low = text.lower()
    if low == "list" or low.startswith("list "):
        return ("list", "")
    for action in ("add", "remove"):
        if low == action or low.startswith(action + " "):
            return (action, text[len(action):].strip())
    return None


def _message_text(msg: dict[str, Any]) -> str:
    raw = msg.get("content") or ""
    if "<" not in raw:
        return raw.strip()
    try:
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    except Exception:
        return raw.strip()


def format_added_reply(author, added: bool) -> str:
    aff = f" ({author.affiliation})" if author.affiliation else ""
    bits = []
    if author.orcid:
        bits.append(f"ORCID {author.orcid}")
    if author.openalex_id:
        bits.append(f"OpenAlex {author.openalex_id}")
    if author.works_count is not None:
        bits.append(f"{author.works_count} works")
    tail = ("\n   " + " · ".join(bits)) if bits else ""
    if added:
        return (
            f"Added author **{author.display_name}**{aff}{tail}\n"
            "   Their papers will now always be included regardless of score."
        )
    return f"Updated author **{author.display_name}**{aff}{tail}"


def format_removed_reply(author) -> str:
    return f"Removed **{author.display_name}** from the whitelist."


def format_list_reply(wl: AuthorWhitelist) -> str:
    if not wl.authors:
        return "Author whitelist is empty."
    lines = ["Author whitelist:"]
    for a in wl.authors:
        aff = f" — {a.affiliation}" if a.affiliation else ""
        ident = a.orcid or a.openalex_id or a.id
        lines.append(f"- {a.display_name}{aff} ({ident})")
    return "\n".join(lines)


def format_error_reply(msg: str) -> str:
    return (
        f"Could not process that: {msg}\n"
        "Usage: `add <ORCID id/URL or Google Scholar profile URL>`, "
        "`remove <ORCID/OpenAlex id/name>`, or `list`."
    )


def _send(client, stream: str, topic: str, content: str, dryrun: bool) -> None:
    if dryrun:
        logger.info("[author-whitelist dryrun] would reply: %s", content)
        return
    try:
        client.send_message(
            {"type": "stream", "to": stream, "topic": topic, "content": content}
        )
    except Exception:
        logger.exception("Failed to send author-whitelist reply")


def _react(client, message_id: int, *, success: bool, dryrun: bool) -> None:
    emoji = "+1" if success else "-1"
    if dryrun:
        logger.info(
            "[author-whitelist dryrun] would react :%s: on message %s",
            emoji,
            message_id,
        )
        return
    try:
        result = client.add_reaction(
            {"message_id": message_id, "emoji_name": emoji}
        )
        record_zulip_api(1)
        if result.get("result") != "success":
            logger.warning(
                "add_reaction :%s: failed for message %s: %s",
                emoji,
                message_id,
                result.get("msg", result),
            )
    except Exception:
        logger.exception("Failed to add reaction on message %s", message_id)


def run_author_whitelist_bot(
    whitelist: AuthorWhitelist,
    *,
    command_source: dict[str, Any],
    realms: dict[str, dict[str, str]],
    mailto: str | None,
    dryrun: bool,
) -> bool:
    """Process new commands in the configured topic. Returns True if changed."""
    realm = command_source.get("realm")
    stream = command_source.get("stream")
    topic = command_source.get("topic") or "author whitelist"
    if not realm or not stream:
        logger.warning(
            "[author-whitelist] command_source missing realm/stream; skipping"
        )
        return False
    try:
        client = _client_for_realm(realms, realm)
    except Exception:
        logger.exception(
            "[author-whitelist] could not create Zulip client for %s", realm
        )
        return False

    bot_email = (realms.get(realm) or {}).get("email", "").lower()
    key = f"{realm}:{stream}:{topic}"
    cursor = whitelist.get_cursor(key)

    msgs = fetch_messages_narrow(
        client,
        stream,
        topic,
        int(command_source.get("lookback_hours", _DEFAULT_LOOKBACK_HOURS)),
        int(command_source.get("max_messages", _DEFAULT_MAX_MESSAGES)),
    )
    msgs = sorted(msgs, key=lambda m: m.get("id", 0))

    changed = False
    for msg in msgs:
        mid = int(msg.get("id", 0))
        if mid <= cursor:
            continue
        if (msg.get("sender_email") or "").lower() == bot_email:
            whitelist.set_cursor(key, mid)
            continue
        cmd = parse_command(_message_text(msg))
        if cmd is None:
            whitelist.set_cursor(key, mid)
            continue
        action, arg = cmd
        success = False
        try:
            if action == "list":
                _send(client, stream, topic, format_list_reply(whitelist), dryrun)
                success = True
            elif action == "remove":
                removed = whitelist.remove(arg)
                if removed is not None:
                    changed = True
                    _send(
                        client, stream, topic, format_removed_reply(removed), dryrun
                    )
                    success = True
                else:
                    _send(
                        client,
                        stream,
                        topic,
                        format_error_reply(f"no whitelist entry matched '{arg}'"),
                        dryrun,
                    )
            elif action == "add":
                author = resolve(
                    arg, mailto=mailto, added_by=msg.get("sender_email")
                )
                added = whitelist.add(author)
                changed = True
                _send(
                    client, stream, topic, format_added_reply(author, added), dryrun
                )
                success = True
        except AuthorResolveError as e:
            _send(client, stream, topic, format_error_reply(str(e)), dryrun)
        except Exception as e:
            logger.exception("[author-whitelist] command failed: %s", arg)
            _send(
                client,
                stream,
                topic,
                format_error_reply(f"unexpected error ({e})"),
                dryrun,
            )
        _react(client, mid, success=success, dryrun=dryrun)
        whitelist.set_cursor(key, mid)
    return changed
