# Codex "Not logged in" + stale usage values

Date: 2026-04-30
Severity: User-impacting (rate limit overrun)
Commit: `2f85770 fix(claudebar): use JWT exp for codex auth and per-engine cache freshness`

## Symptom

Tray showed "Not logged in" for the Codex engine while the user was actively logged in and using `codex` CLI. The tray simultaneously displayed stale usage values (5-Hour 74%, Weekly 22%) labelled "(cached from just now)" while the real values per `codex /status` were 5-Hour 3%. The user did not notice they were approaching the 5-hour cap and exceeded it.

## Root cause - Bug 1: false-negative auth check

`load_codex_token()` in `src/openai_usage.py` had a heuristic that returned `None` when `auth.json`'s `last_refresh` was older than 8 days. Codex CLI refreshes tokens lazily, so `last_refresh` age does NOT track real token validity - the access token's own JWT `exp` claim does. In this incident `last_refresh` was 8 days 2 hours old while the JWT `exp` was still 21 hours away, so the token was perfectly valid but claudebar refused to use it. With the auth check returning `None`, `fetch_openai_usage()` short-circuited before ever calling the `wham/usage` endpoint.

Fix: replaced the `last_refresh` heuristic with actual JWT `exp` decoding, with a 60-second safety margin. Falls through (token accepted) if the JWT cannot be decoded, on the principle that the API will return 401 itself if it really is bad - false negatives are worse than false positives because false negatives silently hide real consumption from the user.

## Root cause - Bug 2: shared cache freshness across engines

When `fetch_openai_usage()` short-circuits, `data_collector.collect_openai` falls back to `snapshot_cache.load_cache()` and surfaces the cached numbers with `is_stale=True`. The display label uses `get_staleness_text(stale_since)` and was reporting "just now". Why "just now" when the data was hours old?

`snapshot_cache.save_cache()` used a single shared `cached_at` field for both engines but only the engine being saved actually got refreshed - the other engine's blob was preserved verbatim. Every Claude refresh therefore bumped `cached_at` to `datetime.now()` while leaving the Codex blob frozen. `load_cache` then returned the stale Codex blob with `stale_since = now`, and the UI confidently labelled it "just now".

Fix: track `claude_cached_at` and `openai_cached_at` separately. Each engine's `stale_since` now reflects when THAT engine was last successfully fetched. Legacy `cached_at` is read as a fallback for caches written before this change.

## Why both bugs reinforced each other

Bug 1 was the proximate cause of the false "Not logged in" label. Bug 2 made the resulting fallback look fresh, which is precisely why the user didn't realise the displayed numbers were lying. Either bug alone would have been visible (Bug 1 → loud "Not logged in" + obviously old cache age; Bug 2 → harmless under normal operation). Combined, they produced a confidently wrong UI that hid the true 97% session usage until the rate limit hit.

## Verification

`is_codex_configured()` against the live `auth.json` returned `False` before the fix and `True` after. Synthetic round-trip on `save_cache`/`load_cache` confirmed that saving Claude no longer alters Codex's `stale_since`. After rebuild and relaunch the tray showed "Connected · Plus" with the correct 97% / 25% values.

## Lesson

When a JWT is available, decode and use its `exp` claim. Do not infer expiry from "when was this file last touched" - any caller that lazily refreshes will produce false negatives. And when caching state from multiple independent sources, give each source its own freshness timestamp; one shared timestamp is a lie waiting to be told.
