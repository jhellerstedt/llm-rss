"""Zulip topic \"feedback ranking\": post recommendations and read reaction signals."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from api_usage import record_zulip_api
from openalex_enrich import PaperEnrichment, format_enrichment_for_feedback_zulip
from rss_merge import normalize_link
from zulip_context import fetch_messages_narrow, strip_zulip_html, _client_for_realm

logger = logging.getLogger(__name__)

FEEDBACK_RANKING_TOPIC = "feedback ranking"
# Per process_group run, at most this many new messages per group (best by relevance, then impact).
MAX_FEEDBACK_RANKING_POSTS_PER_GROUP = 2

TitleLinkScore = tuple[str, str, int, int, PaperEnrichment | None]


@dataclass
class GroupFeedbackCandidates:
    """Passing papers eligible for Zulip feedback ranking from one config group."""

    group_name: str
    zulip_sources: list[dict[str, Any]]
    messages_by_pair: dict[tuple[str, str], list[dict[str, Any]]]
    title_link_scores: list[TitleLinkScore]
    single_author_impact_penalty: int = 1
    max_posts: int = MAX_FEEDBACK_RANKING_POSTS_PER_GROUP
_LINK_LINE = re.compile(r"(?im)^\s*Link:\s*(.+?)\s*$")

# Zulip canonical reaction names are "+1" / "-1"; aliases include thumbs_up / thumbs_down.
_THUMBS_UP_NAMES = frozenset({"+1", "thumbs_up", "like", "thumbsup", "thumbs-up"})
_THUMBS_DOWN_NAMES = frozenset({"-1", "thumbs_down", "thumbsdown", "thumbs-down"})

# Zulip "quote & reply" wraps the quoted bot post in <blockquote>...</blockquote> and
# precedes it with a "@user said:" mention line. We split those apart so human replies
# are never mistaken for bot feedback posts (gating/dedup) but can still be read as context.
_BLOCKQUOTE_RE = re.compile(r"(?is)<blockquote>.*?</blockquote>")
_BLOCK_CLOSE_RE = re.compile(r"(?is)</(p|div|blockquote|li|h[1-6]|ul|ol|pre)>")
_BR_RE = re.compile(r"(?is)<br\s*/?>")
_SAID_LINE_RE = re.compile(r"(?i)\bsaid\b\s*:?\s*$")


def bot_identity_for_realm(
    zulip_realms: dict[str, dict[str, str]], realm: str
) -> tuple[str | None, str | None]:
    """(bot_email, bot_name) for a realm, lowercased; (None, None) if unknown."""
    creds = zulip_realms.get(str(realm).lower()) or zulip_realms.get(realm) or {}
    email = str(creds.get("email") or "").strip().lower() or None
    name = str(creds.get("bot_name") or "").strip().lower() or None
    return email, name


def message_is_from_bot(
    msg: dict[str, Any], bot_email: str | None, bot_name: str | None = None
) -> bool:
    """True if the message was authored by the configured bot account.

    When neither ``bot_email`` nor ``bot_name`` is known we cannot distinguish authors,
    so we fall back to treating every message as the bot's (legacy behavior).
    """
    if not bot_email and not bot_name:
        return True
    sender_email = str(msg.get("sender_email") or "").strip().lower()
    if bot_email and sender_email == bot_email:
        return True
    if bot_name:
        sender_name = str(msg.get("sender_full_name") or "").strip().lower()
        if sender_name == bot_name:
            return True
    return False


def _bot_messages(
    messages: list[dict[str, Any]], bot_email: str | None, bot_name: str | None = None
) -> list[dict[str, Any]]:
    if not bot_email and not bot_name:
        return list(messages)
    return [m for m in messages if message_is_from_bot(m, bot_email, bot_name)]


def unique_realm_stream_pairs(zulip_sources: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Stable-unique (realm.lower(), stream) from zulip_sources rows."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for src in zulip_sources:
        realm = src.get("realm")
        stream = src.get("stream")
        if not realm or not stream:
            continue
        key = (str(realm).lower(), str(stream))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def lookback_max_for_pair(
    zulip_sources: list[dict[str, Any]], realm: str, stream: str
) -> tuple[int, int]:
    """Max lookback_hours and max_messages among sources matching this realm+stream."""
    r = realm.lower()
    lookbacks: list[int] = []
    max_msgs: list[int] = []
    for src in zulip_sources:
        if str(src.get("realm", "")).lower() != r or str(src.get("stream", "")) != stream:
            continue
        lookbacks.append(int(src.get("lookback_hours", 168)))
        max_msgs.append(int(src.get("max_messages", 500)))
    if not lookbacks:
        return 168, 500
    return max(lookbacks), max(max_msgs)


def parse_feedback_link_from_body(content: str) -> str | None:
    """Extract URL from bot message body (line \"Link: ...\")."""
    raw = strip_zulip_html(content or "")
    m = _LINK_LINE.search(raw)
    if not m:
        return None
    url = m.group(1).strip().strip("<>")
    return url or None


def _reaction_is_thumbs_up(reaction: dict[str, Any]) -> bool:
    name = reaction.get("emoji_name")
    if isinstance(name, str) and name in _THUMBS_UP_NAMES:
        return True
    code = str(reaction.get("emoji_code") or "")
    return code == "1f44d" or code.startswith("1f44d-")


def _reaction_is_thumbs_down(reaction: dict[str, Any]) -> bool:
    name = reaction.get("emoji_name")
    if isinstance(name, str) and name in _THUMBS_DOWN_NAMES:
        return True
    code = str(reaction.get("emoji_code") or "")
    return code == "1f44e" or code.startswith("1f44e-")


def count_thumbs_reactions(message: dict[str, Any]) -> tuple[int, int]:
    """Count thumbs up / down on a Zulip message dict."""
    reactions = message.get("reactions") or []
    if not isinstance(reactions, list):
        return 0, 0
    up = down = 0
    for r in reactions:
        if not isinstance(r, dict):
            continue
        if _reaction_is_thumbs_up(r):
            up += 1
        elif _reaction_is_thumbs_down(r):
            down += 1
    return up, down


def aggregate_feedback_signals(
    messages: list[dict[str, Any]],
    bot_email: str | None = None,
    bot_name: str | None = None,
) -> dict[str, tuple[int, int]]:
    """Map normalize_link(url) -> (thumbs_up_count, thumbs_down_count) from bot posts only."""
    out: dict[str, tuple[int, int]] = {}
    for msg in _bot_messages(messages, bot_email, bot_name):
        link = parse_feedback_link_from_body(str(msg.get("content") or ""))
        if not link:
            continue
        key = normalize_link(link)
        u, d = count_thumbs_reactions(msg)
        ou, od = out.get(key, (0, 0))
        out[key] = (ou + u, od + d)
    return out


def links_announced_in_messages(
    messages: list[dict[str, Any]],
    bot_email: str | None = None,
    bot_name: str | None = None,
) -> set[str]:
    """Normalized links the bot already posted in the feedback topic.

    Only bot-authored messages count, so a teammate quoting a post (which embeds the
    bot's ``Link:`` line) is not treated as an announcement.
    """
    keys: set[str] = set()
    for msg in _bot_messages(messages, bot_email, bot_name):
        link = parse_feedback_link_from_body(str(msg.get("content") or ""))
        if link:
            keys.add(normalize_link(link))
    return keys


def latest_feedback_ranking_message(
    messages: list[dict[str, Any]],
    bot_email: str | None = None,
    bot_name: str | None = None,
) -> dict[str, Any] | None:
    """Most recent bot post in the topic whose body contains a feedback ``Link:`` line."""
    for msg in reversed(_bot_messages(messages, bot_email, bot_name)):
        if parse_feedback_link_from_body(str(msg.get("content") or "")):
            return msg
    return None


def feedback_ranking_ready_for_next_post(
    messages: list[dict[str, Any]],
    bot_email: str | None = None,
    bot_name: str | None = None,
) -> bool:
    """True when there is no prior bot post, or the latest bot post has a thumbs reaction.

    Reactions are read only from the bot's own messages; teammate replies in the topic
    never gate the queue (they feed scoring context instead).
    """
    latest = latest_feedback_ranking_message(messages, bot_email, bot_name)
    if latest is None:
        return True
    up, down = count_thumbs_reactions(latest)
    return (up + down) > 0


def merge_signal_maps(
    a: dict[str, tuple[int, int]], b: dict[str, tuple[int, int]]
) -> dict[str, tuple[int, int]]:
    out = dict(a)
    for k, (u, d) in b.items():
        ou, od = out.get(k, (0, 0))
        out[k] = (ou + u, od + d)
    return out


def format_feedback_prompt_snippet(
    article_link: str, signals: dict[str, tuple[int, int]]
) -> str:
    """Short section for the scoring prompt, or empty if no prior feedback row."""
    key = normalize_link(article_link)
    if key not in signals:
        return ""
    up, down = signals[key]
    return (
        "\n### Team feedback on prior recommendations (Zulip)\n"
        "This paper's URL was previously posted in your stream's \"feedback ranking\" topic. "
        f"Emoji reactions recorded there: thumbs_up={up}, thumbs_down={down}. "
        "Treat this as a weak signal from your team (not a hard rule): favor alignment with "
        "thumbs up, discount slightly if thumbs down dominated, but still judge title and "
        "abstract on their merits.\n"
    )


def _html_block_text(content: str) -> str:
    """Strip HTML but keep block boundaries as newlines for line-based parsing."""
    s = _BLOCK_CLOSE_RE.sub("\n", content or "")
    s = _BR_RE.sub("\n", s)
    return strip_zulip_html(s)


def _link_from_block_text(text: str) -> str | None:
    m = _LINK_LINE.search(text or "")
    if not m:
        return None
    url = m.group(1).strip().strip("<>")
    return url or None


def extract_human_comment_text(content: str) -> str:
    """Teammate's own words from a feedback reply (quoted bot post and 'said:' line removed)."""
    without_quote = _BLOCKQUOTE_RE.sub("\n", content or "")
    text = _html_block_text(without_quote)
    kept: list[str] = []
    for raw in text.splitlines():
        t = raw.strip()
        if not t or _SAID_LINE_RE.search(t):
            continue
        kept.append(t)
    return " ".join(kept).strip()


def quoted_post_from_message(content: str) -> tuple[str | None, str | None]:
    """(title, link) of the bot post quoted inside a reply, if any."""
    m = _BLOCKQUOTE_RE.search(content or "")
    if not m:
        return None, None
    inner_text = _html_block_text(m.group(0))
    link = _link_from_block_text(inner_text)
    title: str | None = None
    for raw in inner_text.splitlines():
        t = raw.strip()
        if not t or _SAID_LINE_RE.search(t) or t.lower().startswith("link:"):
            continue
        title = t
        break
    return title, link


def extract_team_comments(
    messages: list[dict[str, Any]],
    bot_email: str | None,
    bot_name: str | None = None,
    *,
    max_comments: int = 20,
) -> list[dict[str, str | None]]:
    """Teammate replies (non-bot) in the feedback topic, paired with the quoted paper."""
    out: list[dict[str, str | None]] = []
    for msg in messages:
        if message_is_from_bot(msg, bot_email, bot_name):
            continue
        content = str(msg.get("content") or "")
        comment = extract_human_comment_text(content)
        if not comment:
            continue
        title, link = quoted_post_from_message(content)
        sender = str(msg.get("sender_full_name") or "").strip() or "team member"
        out.append(
            {"sender": sender, "comment": comment, "title": title, "link": link}
        )
    return out[-max_comments:]


def build_team_comments_block(
    messages_by_pair: dict[tuple[str, str], list[dict[str, Any]]],
    zulip_realms: dict[str, dict[str, str]],
    *,
    max_chars: int | None = None,
    max_comments: int = 20,
) -> str:
    """Section text for teammate replies in the feedback topic (fed into Zulip context)."""
    comments: list[dict[str, str | None]] = []
    for (realm, _stream), msgs in messages_by_pair.items():
        bot_email, bot_name = bot_identity_for_realm(zulip_realms, realm)
        comments.extend(
            extract_team_comments(msgs, bot_email, bot_name, max_comments=max_comments)
        )
    if not comments:
        return ""
    comments = comments[-max_comments:]
    lines: list[str] = []
    for c in comments:
        ref = (c.get("title") or c.get("link") or "").strip()
        ref_part = f" (re: {ref})" if ref else ""
        lines.append(f"- {c['sender']}: {c['comment']}{ref_part}")
    block = (
        '### Team comments on prior recommendations (Zulip "feedback ranking")\n'
        "Human teammates left these notes on previously posted papers. Use them as "
        "guidance about the team's tastes and priorities when scoring new papers "
        "(a weak signal, not hard rules).\n" + "\n".join(lines)
    )
    if max_chars is not None:
        return block[:max_chars]
    return block


def format_feedback_post_body(
    title: str,
    link: str,
    enrichment: PaperEnrichment | None = None,
) -> str:
    base = f"{title.strip()}\n\nLink: {link.strip()}"
    extra = format_enrichment_for_feedback_zulip(enrichment)
    if extra:
        return f"{base}\n\n{extra}"
    return base


def _impact_for_feedback_ranking(
    impact: int,
    enrichment: PaperEnrichment | None,
    *,
    single_author_penalty: int,
) -> int:
    """Impact score used only for ordering Zulip feedback picks (raw scores unchanged elsewhere)."""
    if (
        single_author_penalty > 0
        and enrichment is not None
        and enrichment.author_count == 1
    ):
        return max(0, impact - single_author_penalty)
    return impact


def filter_to_group_winning_links(
    batch: GroupFeedbackCandidates,
    winners: dict[str, str],
) -> list[TitleLinkScore]:
    """Keep only rows whose link this group won in ``winning_group_by_link``."""
    gn = batch.group_name
    return [
        row
        for row in batch.title_link_scores
        if winners.get(normalize_link(row[1]), gn) == gn
    ]


def select_top_ranked_for_feedback_posts(
    title_link_scores: list[TitleLinkScore],
    *,
    max_posts: int = MAX_FEEDBACK_RANKING_POSTS_PER_GROUP,
    single_author_impact_penalty: int = 1,
) -> list[tuple[str, str, PaperEnrichment | None]]:
    """Pick up to ``max_posts`` items with highest (relevance, adjusted impact).

    When OpenAlex enrichment has ``author_count == 1``, ``impact`` is reduced by
    ``single_author_impact_penalty`` (clamped at 0) for this sort only. Set penalty
    to 0 to disable. Thresholds and feed text still use the model's raw scores.
    """
    if max_posts <= 0:
        return []
    pen = max(0, int(single_author_impact_penalty))
    ranked = sorted(
        title_link_scores,
        key=lambda t: (
            -t[2],
            -_impact_for_feedback_ranking(t[3], t[4], single_author_penalty=pen),
        ),
    )
    out: list[tuple[str, str, PaperEnrichment | None]] = []
    seen_keys: set[str] = set()
    for title, link, _rel, _imp, en in ranked:
        key = normalize_link(link)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append((title, link, en))
        if len(out) >= max_posts:
            break
    return out


def load_feedback_state_for_group(
    zulip_sources: list[dict[str, Any]],
    zulip_realms: dict[str, dict[str, str]],
) -> tuple[dict[str, tuple[int, int]], dict[tuple[str, str], list[dict[str, Any]]]]:
    """One fetch per unique (realm, stream): merged reaction signals and raw messages per pair."""
    merged: dict[str, tuple[int, int]] = {}
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        lookback, max_msg = lookback_max_for_pair(zulip_sources, realm, stream)
        try:
            client = _client_for_realm(zulip_realms, realm)
        except KeyError as e:
            logger.error("%s", e)
            continue
        except Exception:
            logger.exception("Zulip feedback client init failed realm=%s", realm)
            continue
        try:
            msgs = fetch_messages_narrow(
                client, stream, FEEDBACK_RANKING_TOPIC, lookback, max_msg
            )
        except Exception:
            logger.exception(
                "Zulip feedback fetch failed realm=%s stream=%s", realm, stream
            )
            continue
        # Keep all senders in by_pair so teammate replies can feed scoring context;
        # reaction signals are read only from the bot's own posts.
        bot_email, bot_name = bot_identity_for_realm(zulip_realms, realm)
        by_pair[(realm, stream)] = msgs
        merged = merge_signal_maps(
            merged, aggregate_feedback_signals(msgs, bot_email, bot_name)
        )
    return merged, by_pair


def post_feedback_ranking_for_new_items(
    zulip_sources: list[dict[str, Any]],
    zulip_realms: dict[str, dict[str, str]],
    *,
    messages_by_pair: dict[tuple[str, str], list[dict[str, Any]]],
    titles_and_links: list[tuple[str, str, PaperEnrichment | None]],
    dryrun: bool,
    max_sends_per_group: int = MAX_FEEDBACK_RANKING_POSTS_PER_GROUP,
) -> None:
    """Post messages for ``titles_and_links`` where the link is not already in that topic.

    At most ``max_sends_per_group`` successful sends **across all** realm/stream pairs
    (first matching destinations in stable pair order consume the budget).
    """
    if not titles_and_links or max_sends_per_group <= 0:
        return
    sends_left = max_sends_per_group
    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        if sends_left <= 0:
            break
        msgs = messages_by_pair.get((realm, stream), [])
        bot_email, bot_name = bot_identity_for_realm(zulip_realms, realm)
        posted = links_announced_in_messages(msgs, bot_email, bot_name)
        try:
            client = _client_for_realm(zulip_realms, realm)
        except KeyError as e:
            logger.error("%s", e)
            continue
        except Exception:
            logger.exception("Zulip feedback client init failed realm=%s", realm)
            continue
        for title, link, enrichment in titles_and_links:
            if sends_left <= 0:
                return
            key = normalize_link(link)
            if key in posted:
                continue
            body = format_feedback_post_body(title, link, enrichment)
            if dryrun:
                logger.info(
                    "[dry run] would post feedback ranking realm=%s stream=%s link=%s",
                    realm,
                    stream,
                    key[:80],
                )
                posted.add(key)
                sends_left -= 1
                continue
            try:
                result = client.send_message(
                    {
                        "type": "stream",
                        "to": stream,
                        "topic": FEEDBACK_RANKING_TOPIC,
                        "content": body,
                    }
                )
                if result.get("result") != "success":
                    logger.warning(
                        "Zulip send_message failed realm=%s stream=%s: %s",
                        realm,
                        stream,
                        result,
                    )
                    continue
                record_zulip_api(1)
                posted.add(key)
                sends_left -= 1
            except Exception:
                logger.exception(
                    "Zulip post feedback failed realm=%s stream=%s", realm, stream
                )
