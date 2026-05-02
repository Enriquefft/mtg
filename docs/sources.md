# Curated meta sources

The internet is full of stale MTG info. These sources are known to be
maintained and represent the *current* Arena meta. Always check the
publication date on the page itself before trusting numbers — even good
sites occasionally surface an old article in a sidebar.

For each source: `WebFetch <url>` from inside Claude to pull a snapshot.

Last verified: 2026-05-01.

## Bot-block reality (read before WebFetching)

Cloudflare IUAM blocks scripted requests on several MTG sites. Verified
2026-04-30 by probing each URL with `curl -A Mozilla/5.0`:

| host | status | decision |
|---|---|---|
| `api.scryfall.com` | 200 | canonical, always use |
| `mtga.untapped.gg` | 200 | **primary `fetch-meta` parser for Brawl + 100x corpus boost for Historic/Pioneer/Standard/Alchemy/Timeless.** Re-enabled 2026-05-01. Each `/constructed/<slug>/archetypes/<id>/...` page server-renders decks via `__NEXT_DATA__.ssrProps.apiDeckData.data` (Historic / Brawl / Pioneer / Alchemy / Timeless), with two API fallbacks for Standard (fully client-rendered) and post-rotation Brawl. The analytics endpoint at `api.mtga.untapped.gg/api/v1/analytics/query/decks_by_event_scope_and_rank_v2/free` is **anonymous-accessible** (no 403); earlier finding was wrong. Decks come over the wire as V4 base64url+LEB128 deckstrings, decoded via the mtgajson `loc_en.json` titleId crosswalk and resolved through Scryfall. |
| `mtgazone.com` | 200 | **primary `fetch-meta` parser.** Tier-list pages (e.g. `/<format>-bo1-metagame-tier-list/`) carry server-rendered `<div class="deck-block">` decklists; deck-article URLs do not. |
| `mtgaassistant.net` | 200 | secondary, Brawl meta breakdown |
| `magic.wizards.com` | 200 | official ban announcements |
| `mtggoldfish.com` | 200 (occasional 403) | primary paper meta; retry once on 403, then fall back |
| **`aetherhub.com`** | **403** | **manual-research only** — see note below |
| `mtgdecks.net` | 200 (re-verified 2026-05-01) | **third Historic source.** Earlier `403` resolved; vanilla UA gets 200. Wired as `fetch-meta --source mtgdecks historic`. |
| `archidekt.com` | 200 | **user-deckbuilder source, all formats.** High novelty bias (different selection than tier-list scrapers). Wired as `fetch-meta --source archidekt <format>`. |

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

**`untapped.gg` (re-enabled 2026-05-01):** untapped is now the only
automated Brawl source — `fetch-meta --source untapped brawl` walks
`https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=historic-brawl`
and emits one 100-card singleton per archetype (commander first, then
companion if any, then 99 deck cards). Format-name mapping inside the
parser: `brawl` -> `historic-brawl` (matches the rest of the toolkit's
"`-f brawl` = Historic Brawl" convention). The same parser also covers
`historic`, `standard`, `pioneer`, `alchemy`, `timeless` with sample
sizes 100x larger than mtgazone's tier-list snapshot (untapped's corpus
is millions of MTGA matches/month vs. mtgazone's hand-curated handful).
Three resolution paths inside `_fetch_archetype_decks`: SSR-embedded
(Historic / Brawl / Pioneer / Alchemy / Timeless), SSR-suggested API
(post-rotation tail), and format-wide API (Standard, fully
client-rendered) with poll-on-202 for untapped's async-query backend.

**`aetherhub.com` (manual-only):** retained as a *secondary* H-Brawl
cross-check now that untapped is the primary automated source. If the
user asks for an even deeper signal than untapped publishes, ask them
to browse aetherhub manually and paste the relevant page text into
the session — do not WebFetch it.

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
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=historic-brawl — `fetch-meta --source untapped brawl` (verified 2026-05-01; 1470 archetypes, 100-card singleton with commander; format CLI key `brawl` maps to untapped slug `historic-brawl`)
- https://www.archidekt.com/ — `fetch-meta --source archidekt brawl` (user-built decklists, high novelty)
- https://mtgaassistant.net/Meta/Historic-Brawl/ — meta breakdown (manual cross-reference)

### Standard Brawl (Scryfall key: `standardbrawl`)
- https://mtgazone.com/standard-brawl/ — deck articles only; mtgazone publishes no Brawl tier list, so `fetch-meta` does not support this format
- https://mtgaassistant.net/Meta/Brawl

### Standard
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=standard — `fetch-meta --source untapped standard` (verified 2026-05-01; uses path 3 / format-wide API + poll-on-202 — Standard archetype pages are fully client-rendered)
- https://www.archidekt.com/ — `fetch-meta --source archidekt standard` (user-built decklists, high novelty)
- https://www.mtggoldfish.com/metagame/standard — `fetch-meta --source mtggoldfish standard`
- https://mtgazone.com/standard-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone standard`

### Alchemy
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=alchemy — `fetch-meta --source untapped alchemy` (verified 2026-05-01)
- https://www.archidekt.com/ — `fetch-meta --source archidekt alchemy` (user-built decklists, high novelty)
- https://mtgazone.com/alchemy-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone alchemy`

### Historic
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=historic — `fetch-meta --source untapped historic` (verified 2026-05-01; SSR-embedded decks)
- https://www.archidekt.com/ — `fetch-meta --source archidekt historic` (user-built decklists, high novelty)
- https://www.mtggoldfish.com/metagame/historic — `fetch-meta --source mtggoldfish historic`
- https://mtgazone.com/historic-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone historic`
- https://mtgdecks.net/Historic — `fetch-meta --source mtgdecks historic` (verified 2026-05-01; one deck per archetype, most-recent submission; tier from row class, winrate + sample from index columns)

### Timeless
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=timeless — `fetch-meta --source untapped timeless` (verified 2026-05-01)
- https://www.archidekt.com/ — `fetch-meta --source archidekt timeless` (user-built decklists, high novelty)
- https://mtgazone.com/timeless-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone timeless`

### Pioneer (Arena's Explorer format draws from this pool)
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=pioneer — `fetch-meta --source untapped pioneer` (verified 2026-05-01; untapped serves Pioneer archetype pages under `/constructed/pioneer/...` directly. The analytics API uses `Explorer_Ladder` as the event_name internally — same telemetry bucket — but the sitemap and page URLs use `pioneer`)
- https://www.archidekt.com/ — `fetch-meta --source archidekt pioneer` (user-built decklists, high novelty)
- https://mtgazone.com/explorer-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone explorer` (also reached via `--source mtgazone pioneer`)
- https://www.mtggoldfish.com/metagame/pioneer — paper Pioneer; retry once on 403

## Banlist + announcements

- https://magic.wizards.com/en/banned-restricted-list  — official, but slower than Scryfall
- https://magic.wizards.com/en/news               — announcement articles for ban changes

## Avoid for Arena work

- **edhrec.com** — paper Commander only; many "good" recommendations are not on Arena, are banned in Brawl, or use the wrong color-identity rules.
- **mtgtop8.com** — paper-only competitive results.
- **gatherer.wizards.com** — official but slow to update and missing newer fields.
- **aetherhub.com** (auto-fetch) — Cloudflare 403s every WebFetch. Manual-only for Historic Brawl, see Bot-block reality above.

## Workflow

When picking a commander or evaluating a meta call:

1. `mtg search 'legal:<fmt> game:arena t:legendary t:creature ...'` to enumerate candidates.
2. WebFetch the primary meta source for the format above; if it 403s, retry once, then use the listed fallback.
3. Cross-reference: a commander showing up on the untapped tier-list *and* in mtgaassistant/mtgazone deck articles is a real meta deck. For Historic Brawl specifically, ask the user to confirm aetherhub commander-share if a finer signal is needed.
4. For decklists: untapped tier-list pages and mtgazone deck articles expose MTGA-export blocks. mtggoldfish has a "Copy to MTGA" button on each deck page.
