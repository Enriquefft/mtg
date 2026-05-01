# Curated meta sources

The internet is full of stale MTG info. These sources are known to be
maintained and represent the *current* Arena meta. Always check the
publication date on the page itself before trusting numbers — even good
sites occasionally surface an old article in a sidebar.

For each source: `WebFetch <url>` from inside Claude to pull a snapshot.

Last verified: 2026-04-30.

## Bot-block reality (read before WebFetching)

Cloudflare IUAM blocks scripted requests on several MTG sites. Verified
2026-04-30 by probing each URL with `curl -A Mozilla/5.0`:

| host | status | decision |
|---|---|---|
| `api.scryfall.com` | 200 | canonical, always use |
| `mtga.untapped.gg` | 200 (page) / 403 (api) | **API-only, scrape blocked as of 2026-04-30 — deferred.** Tier-list and deck pages are a Next.js SPA shell with no server-rendered decklists; the underlying `api.mtga.untapped.gg` JSON endpoints return 403 to anonymous calls. Manual research / WebFetch only. |
| `mtgazone.com` | 200 | **primary `fetch-meta` parser.** Tier-list pages (e.g. `/<format>-bo1-metagame-tier-list/`) carry server-rendered `<div class="deck-block">` decklists; deck-article URLs do not. |
| `mtgaassistant.net` | 200 | secondary, Brawl meta breakdown |
| `magic.wizards.com` | 200 | official ban announcements |
| `mtggoldfish.com` | 200 (occasional 403) | primary paper meta; retry once on 403, then fall back |
| **`aetherhub.com`** | **403** | **manual-research only** — see note below |
| `mtgdecks.net` | 200 (re-verified 2026-05-01) | **third Historic source.** Earlier `403` resolved; vanilla UA gets 200. Wired as `fetch-meta --source mtgdecks historic`. |

We deliberately do **not** circumvent the blocks. Headless Chromium / TLS
impersonation (`curl-impersonate`, `curl_cffi`) would work, but: (a)
violates those sites' ToS, (b) adds heavy deps + ongoing fingerprint
maintenance, (c) the project's "one source of truth: Scryfall" rule. The
unique data on these sites isn't load-bearing for the deck-build loop.

**`mtgdecks.net` (re-enabled 2026-05-01):** Cloudflare no longer 403s
the toolkit's vanilla UA. `fetch-meta --source mtgdecks historic` walks
the `/Historic` archetype-index table and emits one `ParsedDeck` per
archetype (the most-recent user-submitted decklist on each archetype
page; mtgdecks lists by recency). Tier letters come from the index
row's `tier-1`/`tier-2`/`rogue` class (`tier-1` -> S, `tier-2` -> A,
`rogue` -> ""); per-archetype winrate and "Decks" count populate the
sidecar's `winrate` and `sample` fields. Index URL is currently wired
for Historic only — adding Standard/Pioneer would duplicate mtggoldfish
without a curated reason and is deliberately out of scope (one source
per format unless we have a real signal differential).
Multi-deck-per-archetype output is a later feature; v0 ships singletons
to mirror mtgazone's shape.

**`aetherhub.com` (manual-only):** hosts the largest user-submitted
Historic Brawl decklist corpus + commander meta-share derived from it.
Nothing free replaces this for H-Brawl scope. If the user asks for deeper
H-Brawl meta than untapped + mtgaassistant give us, ask them to browse
aetherhub manually and paste the relevant page text into the session — do
not WebFetch it.

## Card data + legalities (canonical)

| source | what it gives | freshness |
|---|---|---|
| https://api.scryfall.com/ | every card, every printing, every format legality, Arena availability | <1h after bans, daily bulk |
| https://scryfall.com/search?q=... | interactive Scryfall query syntax (`legal:brawl game:arena t:legendary`) | live |

The `mtg` CLI here uses Scryfall's bulk download — that's the single source
of truth for the toolkit. Don't cross-reference any other card database;
they all lag.

## Arena format meta

All URLs below are WebFetch-safe (return 200 to scripted requests).
Listed primary → fallback per format.

### Historic Brawl (Scryfall key: `brawl`)
- https://mtga.untapped.gg/constructed/historic-brawl/tier-list — **API-only, scrape blocked (deferred)**; manual research only
- https://mtgaassistant.net/Meta/Historic-Brawl/ — meta breakdown

### Standard Brawl (Scryfall key: `standardbrawl`)
- https://mtgazone.com/standard-brawl/ — deck articles only; mtgazone publishes no Brawl tier list, so `fetch-meta` does not support this format
- https://mtgaassistant.net/Meta/Brawl

### Standard
- https://www.mtggoldfish.com/metagame/standard
- https://mtga.untapped.gg/constructed/standard/tier-list — **API-only, scrape blocked (deferred)**; manual research only
- https://mtgazone.com/standard-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone standard`

### Alchemy
- https://mtga.untapped.gg/constructed/alchemy/tier-list — **API-only, scrape blocked (deferred)**; manual research only
- https://mtgazone.com/alchemy-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone alchemy`

### Historic
- https://www.mtggoldfish.com/metagame/historic — `fetch-meta --source mtggoldfish historic`
- https://mtga.untapped.gg/constructed/historic/tier-list — **API-only, scrape blocked (deferred)**; manual research only
- https://mtgazone.com/historic-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone historic`
- https://mtgdecks.net/Historic — `fetch-meta --source mtgdecks historic` (verified 2026-05-01; one deck per archetype, most-recent submission; tier from row class, winrate + sample from index columns)

### Timeless
- https://mtga.untapped.gg/constructed/timeless/tier-list — **API-only, scrape blocked (deferred)**; manual research only
- https://mtgazone.com/timeless-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone timeless`

### Pioneer (Arena's Explorer format draws from this pool)
- https://mtga.untapped.gg/constructed/explorer/tier-list — **API-only, scrape blocked (deferred)**; manual research only
- https://mtgazone.com/explorer-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone explorer` (also reached via `--source mtgazone pioneer`)
- https://www.mtggoldfish.com/metagame/pioneer — paper Pioneer; retry once on 403

## Banlist + announcements

- https://magic.wizards.com/en/banned-restricted-list  — official, but slower than Scryfall
- https://magic.wizards.com/en/news               — announcement articles for ban changes

## Avoid for Arena work

- **edhrec.com** — paper Commander only; many "good" recommendations are not on Arena, are banned in Brawl, or use the wrong color-identity rules.
- **mtgtop8.com** — paper-only competitive results.
- **moxfield.com** — scraping prohibited; use only via web UI.
- **gatherer.wizards.com** — official but slow to update and missing newer fields.
- **aetherhub.com** (auto-fetch) — Cloudflare 403s every WebFetch. Manual-only for Historic Brawl, see Bot-block reality above.

## Workflow

When picking a commander or evaluating a meta call:

1. `mtg search 'legal:<fmt> game:arena t:legendary t:creature ...'` to enumerate candidates.
2. WebFetch the primary meta source for the format above; if it 403s, retry once, then use the listed fallback.
3. Cross-reference: a commander showing up on the untapped tier-list *and* in mtgaassistant/mtgazone deck articles is a real meta deck. For Historic Brawl specifically, ask the user to confirm aetherhub commander-share if a finer signal is needed.
4. For decklists: untapped tier-list pages and mtgazone deck articles expose MTGA-export blocks. mtggoldfish has a "Copy to MTGA" button on each deck page.
