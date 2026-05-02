# Corpus expansion follow-ups

Items left over from the corpus-expansion batch closed by `ebcea7e
feat(corpus): expand fetch yield + fix parser limit bugs` and the
five-bug fix-up `ab14028 fix(corpus): 5 format-coverage bugs surfaced
by full-fmt run`. Verified live (probes captured 2026-05-02).

Anything new added here must respect three later invariants from the
speed-upgrade batch:

- `ThreadPoolExecutor` + `--workers` (`99e3b94`) — parser code must
  be thread-safe (no shared mutable globals; HTTP throttle remains
  per-host).
- `--deep` opt-in via per-module `_DEEP_LIMIT` (`ecac7f2`) — any
  "fetch more" feature plumbs through that flag, not a new one.
- HTTP keep-alive + redirect following (`d800dfa`) — reuse `_common`
  helpers; do not open a parallel HTTP stack.

CLI invariants in `docs/sources.md` (no bot-block circumvention,
single source of truth: Scryfall) apply.

## Open

(none — both items previously listed here are now resolved; see
"Resolved" below.)

## Resolved

### 1. untapped explorer no-sitemap fallback (RESOLVED 2026-05-02)

Implemented. `tools/mtg_sources/untapped.py:725-744` routes empty-
sitemap formats with a `_FMT_TO_EVENT_NAME` entry into
`_decks_via_format_wide_api`, which hits
`decks_by_event_scope_and_rank_v2/free` with `MetaPeriodId=703`
(`Explorer_Ladder`) and groups results by the `ptg` field to
synthesise archetype tuples. `explorer` was re-added to
`_FMT_TO_SITEMAP_SLUG` (line 125). Smoke-tested at `--limit 50`:
56 raw decks → 7 kept after legality gate (12.5% pass rate is
expected — `Explorer_Ladder` is the shared Pioneer/Explorer telemetry
bucket so the corpus mixes formats and the gate filters down to
Explorer-legal lists).

### 2. mtgdecks pagination support (RESOLVED 2026-05-02)

Implemented. `tools/mtg_sources/mtgdecks.py` adds module-scope
`_DEEP_LIMIT = 5000` + `_MAX_PAGES = 50` (read by
`tools/mtg.py:7308` via `getattr(parser_module, "_DEEP_LIMIT", None)`
when `--deep` is set). The per-archetype loop now extracts the
page-1 row-walk into `_rows_from_archetype_table` and, when
`per_archetype_cap` exceeds page-1's ~15-row yield, walks
`/<archetype>/page:N` for `N=2..M` while terminating on HTTP error,
zero new (cross-page-deduped) rows, or budget reached. Default
(non-deep) invocations skip the walk entirely so historic behaviour
is unchanged. Probe confirmed `boros-energy` paginates to 11 pages
× 15 ≈ 165 unique decks/archetype.

## Documented absence (close by adding code comment, drop from tracker)

These were on the open list but probes confirmed they are either no
longer issues or genuinely not fixable on our side. Action: one-line
comment in the relevant module pointing to this section, then strike
the entry from this doc the next time it's edited.

### 3. mtggoldfish historic + explorer + timeless (NO-GO, CF-walled)

Probed 2026-05-02. Index pages work, pagination works, slug
discovery works — but the per-deck export pages are blocked.

Listing-page reachability (all HTTP 200 with `Mozilla/5.0`):

| URL                                          | decks/page | pages | total claimed |
|----------------------------------------------|-----------:|------:|--------------:|
| `/archetype/explorer-other/decks?page=N`     | 50         | 68    | 3366          |
| `/archetype/timeless-other/decks?page=N`     | 50         | 6     | small         |
| `/archetype/other-<GUID>/decks?page=N` (historic) | 50    | 77    | large         |

Slug pattern: explorer + timeless use stable `<format>-other`;
historic uses `other-<uuid>` (GUID is the only archetype tile on
`/metagame/historic`, so it's a one-fetch scrape — no lookup table
needed).

**The blocker** is `/deck/<id>`: Cloudflare returns HTTP 403 with a
JS challenge ("Just a moment..."). Verified against:

- `_common.http_get_text(retry_403_once=True)` — still 403.
- Chrome-shaped headers + HTTP/2 + Referer + cookie jar — still 403.
- Alternate paths `/deck/arena_download/<id>`, `/deck/download/<id>`,
  `/deck/<id>/arena` — all 403.
- `/deck/visual/<id>` — 200, but card data renders via post-load JS;
  no inline export string.
- `curl_cffi` 0.15 with **all 12** browser fingerprint profiles
  (chrome99-146, safari153-2601, firefox133-147, edge99-101) —
  still 403 with `cf-mitigated: challenge`. TLS-fingerprint
  impersonation defeats CF bot-fingerprint mode but NOT the
  Turnstile JS-token managed-challenge flow goldfish runs.
  (Probed 2026-05-02 against `/deck/7606481`.)
- Playwright 1.58 with `chromium-1217` + stealth flags
  (`--disable-blink-features=AutomationControlled`, real DISPLAY,
  `navigator.webdriver=undefined` JS shim) — same 403. The Turnstile
  challenge intentionally treats automated browsers as bots
  regardless of fingerprint.

The existing parser sidesteps this on standard + pioneer because it
fetches `/archetype/<slug>` (the aggregated archetype page; serves
the `deck_input[deck]` hidden form). The "Other" archetypes 302
straight to `/decks` and have no aggregated page — there is no
"Other" decklist, only the bag-of-decks index.

The listings carry no per-deck winrate or sample-size, only Date,
Deck name, Author, Event, Place, Prices. So even if the wall
dropped, §5 (mtggoldfish per-deck winrate) would NOT close.

**Conclusion (REINFORCED 2026-05-02)**: not fixable from a Python
HTTP stack at all. Both TLS-impersonation and headless-browser paths
empirically fail against goldfish's Turnstile flow. The only
remaining options are (a) FlareSolverr/CapSolver-style third-party
CAPTCHA-solver service (out of scope: external paid dep, ToS, no
"single source of truth"), or (b) abandoning recovery. The
`docs/sources.md:30-35` no-bypass policy stays — it is now empirically
validated, not just precautionary.

Re-evaluate only if mtggoldfish exposes an unwalled per-deck export
endpoint upstream.

### 4. untapped timeless 0-deck → drift

Stale. Live probe: `tools/mtg fetch-meta timeless --source untapped
--limit 5` exits 0, writes 3 valid decks. Direct API:
`MetaPeriodId=709` returns 287 decks. Upstream republished between
the source-plan probe (2026-05-01) and now. No code change needed.

### 5. mtggoldfish per-deck winrate

Confirmed-impossible. `pioneer-izzet-prowess` archetype page (99388
bytes) contains zero winrate-shaped strings — only `window` (CSS) and
player-name tokens (`Winchester`, `MisterTwin`). META%-share is the
only stat published; synthesizing a winrate from share would be
fiction.

Action: add a comment at `mtggoldfish.py:45-49` ("no winrate field
upstream; META% share is the only deck-level stat") so future readers
don't re-open this.

### 6. untapped standardbrawl

Confirmed-impossible. `meta-periods/active` lists no
`Play_Brawl_Standard` entry — only historic-brawl. The format-wide
API has nothing to fall back to. Comment at `untapped.py`'s
`url_for_format` should note "standardbrawl: no active meta-period
upstream as of 2026-05-02".

## Out of scope (for this tracker)

- Per-source parallelization across sources (already partially
  addressed by `99e3b94`'s `ThreadPoolExecutor` for `fetch-meta-all`).
- Threading `--min-winrate` *into* parser calls (vs filtering at
  `cmd_fetch_meta` layer): cosmetic, current behavior is correct.
- Threading `--deep` into untapped's per-archetype slice: untapped's
  `[:8]` cap was already chosen for high-traffic head; deeper walk
  would need a separate design (the long tail is duplicate-heavy).
