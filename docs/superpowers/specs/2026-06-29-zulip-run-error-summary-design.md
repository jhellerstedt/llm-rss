# Zulip run error summary

**Date:** 2026-06-29  
**Status:** Approved

## Goal

After each `main()` run, post a single deduplicated summary of WARNING/ERROR log lines to configured Zulip stream(s) when issues occurred. Skip posting on clean runs.

## Config

```toml
[zulip.error_reporting]
enabled = true
topic = "llm-rss run"
realm = "myrealm"
stream = "llm errors"

[[zulip.error_reporting.destinations]]
realm = "otherrealm"
stream = "llm errors"
```

Single `realm`/`stream` and `destinations` entries merge (deduped by realm+stream).

## Behavior

- Root logger handler collects WARNING+ during `main()`.
- Messages dedupe after stripping `[group_name]` prefixes; identical lines show `×N`.
- Same body posted to every configured destination.
- Dry-run logs intent without sending.
- Posting failures never abort the run.

## Module

`zulip_run_error_report.py` — collector, formatter, poster. Wired from `main()` `finally`.
