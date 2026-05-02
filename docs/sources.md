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
| `aetherhub.com` | 200 (re-verified 2026-05-01) | **auto-fetched, all formats with `/Metagame/<slug>/`.** Earlier `403` resolved; vanilla UA gets 200 on both index and `/Deck/<slug>` pages. Wired as `fetch-meta --source aetherhub <fmt>` for brawl/standard/alchemy/historic/timeless/pioneer. |
| `mtgdecks.net` | 200 (re-verified 2026-05-01) | **third Historic source.** Earlier `403` resolved; vanilla UA gets 200. Wired as `fetch-meta --source mtgdecks historic`. `--deep` walks `/<archetype>/page:N` pagination (~165 decks/archetype reachable on the heaviest archetype, observed 2026-05-02). |
| `archidekt.com` | 200 | **user-deckbuilder source, all formats.** High novelty bias (different selection than tier-list scrapers). Wired as `fetch-meta --source archidekt <format>`. |
| `api2.moxfield.com` | 200 (browser-UA + Origin/Referer headers) | **highest-volume user-deckbuilder source, all formats** (their `historicBrawl` filter maps to our `brawl`; their "Brawl" is Standard Brawl). Wired as `fetch-meta --source moxfield <format>`. No winrate/sample (user-built, not metagame). 0.6s throttle. |

We deliberately do **not** circumvent the blocks. Headless Chromium / TLS
impersonation (`curl-impersonate`, `curl_cffi`) would work, but: (a)
violates those sites' ToS, (b) adds heavy deps + ongoing fingerprint
maintenance, (c) the project's "one source of truth: Scryfall" rule. If
a host's vanilla-UA path 403s, the parser hard-fails and the source is
dropped from the per-format wiring rather than worked around.

Per-source implementation details live alongside the parser in
`tools/mtg_sources/<host>.py`. Per-format wiring lives in
`scripts/expand-corpus.sh`.

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
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=historic-brawl — `fetch-meta --source untapped brawl`
- https://www.moxfield.com/decks/public?fmt=historicBrawl — `fetch-meta --source moxfield brawl`
- https://aetherhub.com/Metagame/Historic-Brawl/ — `fetch-meta --source aetherhub brawl` (per-archetype winrates)
- https://www.archidekt.com/ — `fetch-meta --source archidekt brawl`
- https://mtgaassistant.net/Meta/Historic-Brawl/ — meta breakdown (manual cross-reference)

### Standard Brawl (Scryfall key: `standardbrawl`)
- https://www.moxfield.com/decks/public?fmt=brawl — `fetch-meta --source moxfield standardbrawl` (Moxfield's "Brawl" filter is Standard Brawl)
- https://www.archidekt.com/ — `fetch-meta --source archidekt standardbrawl`
- https://mtgazone.com/standard-brawl/ — deck articles only; mtgazone publishes no Brawl tier list, `fetch-meta` does not support this source for this format
- https://mtgaassistant.net/Meta/Brawl

### Standard
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=standard — `fetch-meta --source untapped standard`
- https://www.moxfield.com/decks/public?fmt=standard — `fetch-meta --source moxfield standard`
- https://aetherhub.com/Metagame/Standard-BO1/ — `fetch-meta --source aetherhub standard`
- https://www.archidekt.com/ — `fetch-meta --source archidekt standard`
- https://www.mtggoldfish.com/metagame/standard — `fetch-meta --source mtggoldfish standard`
- https://mtgazone.com/standard-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone standard`

### Alchemy
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=alchemy — `fetch-meta --source untapped alchemy`
- https://www.moxfield.com/decks/public?fmt=alchemy — `fetch-meta --source moxfield alchemy`
- https://aetherhub.com/Metagame/Alchemy-BO1/ — `fetch-meta --source aetherhub alchemy`
- https://www.archidekt.com/ — `fetch-meta --source archidekt alchemy`
- https://mtgazone.com/alchemy-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone alchemy`

### Historic
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=historic — `fetch-meta --source untapped historic`
- https://www.moxfield.com/decks/public?fmt=historic — `fetch-meta --source moxfield historic`
- https://aetherhub.com/Metagame/Historic-BO1/ — `fetch-meta --source aetherhub historic`
- https://www.archidekt.com/ — `fetch-meta --source archidekt historic`
- https://www.mtggoldfish.com/metagame/historic — `fetch-meta --source mtggoldfish historic`
- https://mtgazone.com/historic-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone historic`
- https://mtgdecks.net/Historic — `fetch-meta --source mtgdecks historic` (one deck per archetype, most-recent submission; tier from row class, winrate + sample from index; `--deep` walks `/page:N` pagination for full archetype coverage, ~165 decks/archetype max observed 2026-05-02)

### Timeless
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=timeless — `fetch-meta --source untapped timeless`
- https://www.moxfield.com/decks/public?fmt=timeless — `fetch-meta --source moxfield timeless`
- https://aetherhub.com/Metagame/Timeless-BO1/ — `fetch-meta --source aetherhub timeless`
- https://www.archidekt.com/ — `fetch-meta --source archidekt timeless`
- https://mtgazone.com/timeless-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone timeless`

### Pioneer (Arena's Explorer format draws from this pool)
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=pioneer — `fetch-meta --source untapped pioneer` (untapped's analytics API uses `Explorer_Ladder` internally — same telemetry bucket — but sitemap + page URLs use `pioneer`)
- https://www.moxfield.com/decks/public?fmt=pioneer — `fetch-meta --source moxfield pioneer`
- https://aetherhub.com/Metagame/Explorer-BO1/ — `fetch-meta --source aetherhub pioneer` (aetherhub publishes under the Explorer-BO1 slug)
- https://www.archidekt.com/ — `fetch-meta --source archidekt pioneer`
- https://mtgazone.com/explorer-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone explorer` (also reached via `--source mtgazone pioneer`)
- https://www.mtggoldfish.com/metagame/pioneer — paper Pioneer; retry once on 403

### Explorer (Scryfall key: `explorer`; Arena-specific Pioneer subset)
- https://mtga.untapped.gg/sitemap/constructed-archetypes.xml?format=explorer — `fetch-meta --source untapped explorer` (sitemap is a 219-byte empty stub upstream; the parser auto-falls-back to `decks_by_event_scope_and_rank_v2/free` with `MetaPeriodId=703` (`Explorer_Ladder`) and synthesises archetypes by `ptg` field. ~270 decks across ~20 ptg buckets recovered, 2026-05-02.)
- https://www.moxfield.com/decks/public?fmt=pioneer — `fetch-meta --source moxfield pioneer` (Moxfield has no separate Explorer filter; Pioneer is the closest semantic match)
- https://aetherhub.com/Metagame/Explorer-BO1/ — `fetch-meta --source aetherhub pioneer` (already covered under Pioneer above)
- https://mtgazone.com/explorer-bo1-metagame-tier-list/ — `fetch-meta --source mtgazone explorer`

## Banlist + announcements

- https://magic.wizards.com/en/banned-restricted-list  — official, but slower than Scryfall
- https://magic.wizards.com/en/news               — announcement articles for ban changes

## Avoid for Arena work

- **edhrec.com** — paper Commander only; many "good" recommendations are not on Arena, are banned in Brawl, or use the wrong color-identity rules.
- **mtgtop8.com** — paper-only competitive results.
- **gatherer.wizards.com** — official but slow to update and missing newer fields.

## Workflow

When picking a commander or evaluating a meta call:

1. `mtg search 'legal:<fmt> game:arena t:legendary t:creature ...'` to enumerate candidates.
2. WebFetch the primary meta source for the format above; if it 403s, retry once, then use the listed fallback.
3. Cross-reference: a commander showing up on the untapped tier-list *and* aetherhub /Metagame *and* mtgaassistant/mtgazone deck articles is a real meta deck. aetherhub publishes per-archetype winrates so it's the cheapest finer-signal cross-check for Brawl.
4. For decklists: untapped tier-list pages and mtgazone deck articles expose MTGA-export blocks. mtggoldfish has a "Copy to MTGA" button on each deck page.
