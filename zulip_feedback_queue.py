"""Persistent queue + hourly dispatch for Zulip \"feedback ranking\" posts."""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from api_usage import record_zulip_api
from openalex_enrich import PaperEnrichment
from rss_merge import normalize_link
from zulip_context import fetch_messages_narrow, _client_for_realm
from zulip_feedback import (
    FEEDBACK_RANKING_TOPIC,
    bot_identity_for_realm,
    count_thumbs_reactions,
    feedback_ranking_ready_for_next_post,
    format_feedback_post_body,
    latest_feedback_ranking_message,
    links_announced_in_messages,
    lookback_max_for_pair,
    unique_realm_stream_pairs,
)
from zulip_feedback_weekly_stats import record_enqueued, record_posted, resolve_bucket

logger = logging.getLogger(__name__)

QUEUE_VERSION = 1

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment, misc]


def feedback_ranking_queue_path(config_path: Path, zulip_cfg: dict[str, Any]) -> Path:
    rel = zulip_cfg.get("feedback_ranking_queue_file")
    if rel:
        p = Path(str(rel))
        if not p.is_absolute():
            return (config_path.parent / p).resolve()
        return p.resolve()
    return config_path.with_name(f"{config_path.stem}.feedback_ranking_queue.json")


def paper_enrichment_to_json(en: PaperEnrichment | None) -> dict[str, Any] | None:
    if en is None:
        return None
    return {
        "top_author_name": en.top_author_name,
        "top_h_index": en.top_h_index,
        "first_affiliation": en.first_affiliation,
        "last_affiliation": en.last_affiliation,
        "top_author_affiliation": en.top_author_affiliation,
        "author_count": en.author_count,
    }


def paper_enrichment_from_json(data: dict[str, Any] | None) -> PaperEnrichment | None:
    if not data:
        return None
    raw_ac = data.get("author_count")
    author_count: int | None
    if raw_ac is None:
        author_count = None
    else:
        try:
            author_count = int(raw_ac)
        except (TypeError, ValueError):
            author_count = None
    top_h: int | None
    if "top_h_index" not in data:
        top_h = None
    else:
        raw_h = data.get("top_h_index")
        if raw_h is None:
            top_h = None
        else:
            try:
                top_h = int(raw_h)
            except (TypeError, ValueError):
                top_h = None
    return PaperEnrichment(
        top_author_name=str(data.get("top_author_name", "")),
        first_affiliation=str(data.get("first_affiliation", "")),
        last_affiliation=str(data.get("last_affiliation", "")),
        top_h_index=top_h,
        top_author_affiliation=str(data.get("top_author_affiliation", "Unknown")),
        author_count=author_count,
    )


def _default_doc() -> dict[str, Any]:
    return {"version": QUEUE_VERSION, "queues": []}


def _doc_to_by_pair(doc: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in doc.get("queues") or []:
        realm = str(row.get("realm", "")).lower()
        stream = str(row.get("stream", ""))
        if not realm or not stream:
            continue
        pending = row.get("pending") or []
        if not isinstance(pending, list):
            continue
        norm_pending: list[dict[str, Any]] = []
        for item in pending:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", ""))
            link = str(item.get("link", ""))
            if not link:
                continue
            en_raw = item.get("enrichment")
            bucket_id = str(item.get("bucket_id") or "") or None
            b_title = str(item.get("bucket_title") or "") or None
            b_kind = str(item.get("kind") or "") or None
            group_name = str(item.get("group_name") or "") or None
            row: dict[str, Any] = {
                "title": title,
                "link": link,
                "enrichment": en_raw if isinstance(en_raw, dict) else None,
            }
            if bucket_id:
                row["bucket_id"] = bucket_id
            if b_title:
                row["bucket_title"] = b_title
            if b_kind:
                row["kind"] = b_kind
            if group_name:
                row["group_name"] = group_name
            norm_pending.append(row)
        out[(realm, stream)] = norm_pending
    return out


def _by_pair_to_doc(by_pair: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[str, Any]:
    queues: list[dict[str, Any]] = []
    for (realm, stream) in sorted(by_pair.keys(), key=lambda t: (t[0], t[1])):
        items = by_pair[(realm, stream)]
        if not items:
            continue
        pending_out: list[dict[str, Any]] = []
        for it in items:
            row: dict[str, Any] = {
                "title": it["title"],
                "link": it["link"],
                "enrichment": it.get("enrichment"),
            }
            if it.get("bucket_id"):
                row["bucket_id"] = it["bucket_id"]
            if it.get("bucket_title"):
                row["bucket_title"] = it["bucket_title"]
            if it.get("kind"):
                row["kind"] = it["kind"]
            if it.get("group_name"):
                row["group_name"] = it["group_name"]
            pending_out.append(row)
        queues.append(
            {
                "realm": realm,
                "stream": stream,
                "pending": pending_out,
            }
        )
    return {"version": QUEUE_VERSION, "queues": queues}


@contextmanager
def _locked_queue_file(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        doc = _default_doc()
        with open(path, "w", encoding="utf-8") as wf:
            json.dump(doc, wf)
    f = open(path, "r+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        raw = f.read()
        if not raw.strip():
            doc = _default_doc()
        else:
            try:
                doc = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Feedback queue JSON corrupt at %s; resetting", path)
                doc = _default_doc()
        if not isinstance(doc, dict):
            doc = _default_doc()
        yield doc
        f.seek(0)
        f.truncate()
        json.dump(doc, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def zulip_sources_union(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if cfg.get("groups"):
        acc: list[dict[str, Any]] = []
        for g in cfg["groups"]:
            acc.extend(g.get("zulip_sources") or [])
        return acc
    return list(cfg.get("zulip_sources") or [])


def enqueue_feedback_ranking_for_group(
    config_path: Path,
    zulip_cfg: dict[str, Any],
    zulip_sources: list[dict[str, Any]],
    messages_by_pair: dict[tuple[str, str], list[dict[str, Any]]],
    titles_and_links: list[tuple[str, str, PaperEnrichment | None]],
    *,
    group_name: str,
    dryrun: bool,
    zulip_realms: dict[str, dict[str, str]] | None = None,
    feed_category: str | None = None,
    bucket_id: str | None = None,
    bucket_title: str | None = None,
    bucket_kind: str | None = None,
) -> int:
    """Append ranked items per (realm, stream) when not already posted or pending. Returns new row count."""
    if not titles_and_links or not zulip_sources:
        return 0
    realms = zulip_realms or {}
    if bucket_id and bucket_title and bucket_kind:
        bid, btitle, bkind = bucket_id, bucket_title, bucket_kind
    else:
        bid, btitle, bkind = resolve_bucket(group_name, feed_category)
    path = feedback_ranking_queue_path(config_path, zulip_cfg)
    existed_before = path.exists()
    added = 0
    with _locked_queue_file(path) as doc:
        by_pair = _doc_to_by_pair(doc)
        for realm, stream in unique_realm_stream_pairs(zulip_sources):
            msgs = messages_by_pair.get((realm, stream), [])
            bot_email, bot_name = bot_identity_for_realm(realms, realm)
            posted = links_announced_in_messages(msgs, bot_email, bot_name)
            key_list = by_pair.setdefault((realm, stream), [])
            pending_keys = {normalize_link(str(x["link"])) for x in key_list}
            for title, link, enrichment in titles_and_links:
                k = normalize_link(link)
                if k in posted or k in pending_keys:
                    continue
                key_list.append(
                    {
                        "title": title,
                        "link": link,
                        "enrichment": paper_enrichment_to_json(enrichment),
                        "bucket_id": bid,
                        "bucket_title": btitle,
                        "kind": bkind,
                        "group_name": group_name,
                    }
                )
                pending_keys.add(k)
                added += 1
                if not dryrun:
                    record_enqueued(
                        config_path,
                        zulip_cfg,
                        realm=realm,
                        stream=stream,
                        bucket_id=bid,
                        title=btitle,
                        kind=bkind,
                        dryrun=False,
                    )
                logger.info(
                    "Zulip feedback ranking queue: +1 realm=%s stream=%s link=%s group=%s dryrun=%s",
                    realm,
                    stream,
                    k[:80],
                    group_name,
                    dryrun,
                )
        doc.clear()
        doc.update(_by_pair_to_doc(by_pair))
    if added == 0 and not existed_before:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return added


def _feedback_reaction_timeout_hours(zulip_cfg: dict[str, Any]) -> float | None:
    raw = zulip_cfg.get("feedback_ranking_reaction_timeout_hours")
    if raw is None:
        return 48.0
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return 48.0
    return hours if hours > 0 else None


def dispatch_feedback_ranking_queue_once(
    config_path: Path,
    cfg: dict[str, Any],
    zulip_realms: dict[str, dict[str, str]],
    *,
    dryrun: bool,
) -> None:
    """For each (realm, stream) with backlog, process at most one head item (pop if posted or sent)."""
    zulip_cfg = dict(cfg.get("zulip") or {})
    reaction_timeout_hours = _feedback_reaction_timeout_hours(zulip_cfg)
    path = feedback_ranking_queue_path(config_path, zulip_cfg)
    if not path.exists():
        logger.debug("No feedback ranking queue file at %s", path)
        return
    zulip_sources_all = zulip_sources_union(cfg)
    if not zulip_sources_all:
        logger.debug("No zulip_sources in config; skipping feedback queue dispatch")
        return

    with _locked_queue_file(path) as doc:
        by_pair = _doc_to_by_pair(doc)
        if not any(by_pair.values()):
            return
        for (realm, stream) in sorted(by_pair.keys(), key=lambda t: (t[0], t[1])):
            pending = by_pair.get((realm, stream)) or []
            if not pending:
                continue
            cand = pending[0]
            title = str(cand.get("title", ""))
            link = str(cand.get("link", ""))
            en = paper_enrichment_from_json(
                cand["enrichment"] if isinstance(cand.get("enrichment"), dict) else None
            )
            cand_group = str(cand.get("group_name") or "uncategorized")
            if cand.get("bucket_id") and cand.get("bucket_title") and cand.get("kind"):
                cand_bid = str(cand["bucket_id"])
                cand_btitle = str(cand["bucket_title"])
                cand_bkind = str(cand["kind"])
            else:
                cand_bid, cand_btitle, cand_bkind = resolve_bucket(cand_group, None)
            lookback, max_msg = lookback_max_for_pair(zulip_sources_all, realm, stream)
            try:
                client = _client_for_realm(zulip_realms, realm)
            except KeyError as e:
                logger.error("%s", e)
                continue
            except Exception:
                # e.g. transient AssertionError when the server returns no version on
                # client init; skip this realm rather than crashing the whole dispatch.
                logger.exception(
                    "Feedback queue dispatch: client init failed realm=%s", realm
                )
                continue
            try:
                msgs = fetch_messages_narrow(
                    client, stream, FEEDBACK_RANKING_TOPIC, lookback, max_msg
                )
            except Exception:
                logger.exception(
                    "Feedback queue dispatch: fetch failed realm=%s stream=%s",
                    realm,
                    stream,
                )
                continue
            bot_email, bot_name = bot_identity_for_realm(zulip_realms, realm)
            posted = links_announced_in_messages(msgs, bot_email, bot_name)
            k = normalize_link(link)
            if k in posted:
                by_pair[(realm, stream)] = pending[1:]
                logger.info(
                    "Feedback queue: dropped stale head (already in topic) realm=%s stream=%s",
                    realm,
                    stream,
                )
                continue
            if not feedback_ranking_ready_for_next_post(
                msgs,
                bot_email,
                bot_name,
                reaction_timeout_hours=reaction_timeout_hours,
            ):
                logger.info(
                    "Feedback queue: waiting for reaction on previous post "
                    "realm=%s stream=%s",
                    realm,
                    stream,
                )
                continue
            latest = latest_feedback_ranking_message(msgs, bot_email, bot_name)
            if latest is not None and reaction_timeout_hours:
                up, down = count_thumbs_reactions(latest)
                if (up + down) == 0:
                    logger.info(
                        "Feedback queue: posting without reaction (timeout %.0fh) "
                        "realm=%s stream=%s",
                        reaction_timeout_hours,
                        realm,
                        stream,
                    )
            body = format_feedback_post_body(title, link, en)
            if dryrun:
                logger.info(
                    "[dry run] feedback queue would post realm=%s stream=%s link=%s",
                    realm,
                    stream,
                    k[:80],
                )
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
                        "Feedback queue send_message failed realm=%s stream=%s: %s",
                        realm,
                        stream,
                        result,
                    )
                    continue
                record_zulip_api(1)
                by_pair[(realm, stream)] = pending[1:]
                record_posted(
                    config_path,
                    zulip_cfg,
                    realm=realm,
                    stream=stream,
                    bucket_id=cand_bid,
                    title=cand_btitle,
                    kind=cand_bkind,
                    link=link,
                    dryrun=False,
                )
                logger.info(
                    "Feedback queue: posted realm=%s stream=%s link=%s",
                    realm,
                    stream,
                    k[:80],
                )
            except Exception:
                logger.exception(
                    "Feedback queue post failed realm=%s stream=%s", realm, stream
                )
        doc.clear()
        doc.update(_by_pair_to_doc(by_pair))
