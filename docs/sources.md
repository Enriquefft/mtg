# Curated meta sources

The internet is full of stale MTG info. These sources are known to be
maintained and represent the *current* Arena meta. Always check the
publication date on the page itself before trusting numbers — even good
sites occasionally surface an old article in a sidebar.

For each source: `WebFetch <url>` from inside Claude to pull a snapshot.

Last verified: 2026-04-30.

## Card data + legalities (canonical)

| source | what it gives | freshness |
|---|---|---|
| https://api.scryfall.com/ | every card, every printing, every format legality, Arena availability | <1h after bans, daily bulk |
| https://scryfall.com/search?q=... | interactive Scryfall query syntax (`legal:brawl game:arena t:legendary`) | live |

The `mtg` CLI here uses Scryfall's bulk download — that's the single source
of truth for the toolkit. Don't cross-reference any other card database;
they all lag.

## Arena format meta

### Historic Brawl (Scryfall key: `brawl`)
- https://aetherhub.com/Metagame/Historic-Brawl/  — % representation by commander, last 90 days
- https://aetherhub.com/Decks/Historic-Brawl/    — top-level deck list browser
- https://mtgdecks.net/Historic-Brawl            — top decks aggregated from results
- https://mtgaassistant.net/Meta/Historic-Brawl/  — meta breakdown
- https://mtga.untapped.gg/constructed/historic-brawl/tier-list  — tier list with win rates

### Standard Brawl (Scryfall key: `standardbrawl`)
- https://aetherhub.com/Metagame/Brawl/
- https://mtgdecks.net/Brawl
- https://mtgaassistant.net/Meta/Brawl

### Standard
- https://www.mtggoldfish.com/metagame/standard
- https://mtgazone.com/standard-decks/
- https://mtgdecks.net/Standard

### Alchemy
- https://mtgazone.com/alchemy-decks/
- https://mtga.untapped.gg/constructed/alchemy/tier-list
- https://mtgdecks.net/Alchemy

### Historic
- https://www.mtggoldfish.com/metagame/historic
- https://mtgazone.com/historic-decks/
- https://mtga.untapped.gg/constructed/historic/tier-list

### Timeless
- https://mtgazone.com/timeless-decks/
- https://mtgdecks.net/Timeless

### Pioneer (Arena Explorer pulls from this pool)
- https://www.mtggoldfish.com/metagame/pioneer
- https://mtgdecks.net/Pioneer

## Banlist + announcements

- https://magic.wizards.com/en/banned-restricted-list  — official, but slower than Scryfall
- https://magic.wizards.com/en/news               — announcement articles for ban changes

## Avoid for Arena work

- **edhrec.com** — paper Commander only; many "good" recommendations are not on Arena, are banned in Brawl, or use the wrong color-identity rules.
- **mtgtop8.com** — paper-only competitive results.
- **moxfield.com** — scraping prohibited; use only via web UI.
- **gatherer.wizards.com** — official but slow to update and missing newer fields.

## Workflow

When picking a commander or evaluating a meta call:

1. `mtg search 'legal:brawl game:arena t:legendary t:creature ...'` to enumerate candidates.
2. WebFetch the top 1–2 meta sources for the format above.
3. Cross-reference: if a commander shows up at >2% on aetherhub AND has positive win rates on untapped, it's a real meta deck.
4. For the deck list: prefer aetherhub deck pages (they expose MTGA-export blocks directly).
