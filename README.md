# mtg

A deterministic deck-building toolkit for **Magic: The Gathering Arena**, built
to be driven by AI coding agents (Claude Code, Cursor, etc.).

The goal is simple: when an LLM is asked to brew, tune, or sanity-check an
MTGA deck, give it a single trustworthy command-line surface so it doesn't
have to guess at legalities, scrape stale fan sites, or hallucinate card text.

## Why this exists

LLMs are bad at MTG facts on their own:

- training data lags the Arena banlist by months,
- the same card name can have a paper version *and* an `A-` rebalanced version
  with different legalities,
- "Brawl" on Arena is what Scryfall calls `brawl` (Historic Brawl, 100-card
  singleton), not what Scryfall calls `standardbrawl`,
- a deck list that imports cleanly into Arena still might be illegal in the
  format the user intended.

This repo collapses all of that into one local CLI (`mtg`) backed by Scryfall's
daily bulk export, plus a small set of curated docs covering the gotchas and
the meta sources that are actually current.

When an agent works inside this repo, it can validate a 100-card list against
Historic Brawl in milliseconds, offline, against the same data Scryfall serves.

## Design rules

1. **Single source of truth.** Card data is Scryfall's `default_cards.json`
   bulk download. Nothing else. No secondary card DBs, no scraping.
2. **Offline by default.** `sync` and `search` are the only commands that
   touch the network. Everything else hits the local index.
3. **Agent-friendly output.** Plain text, stable formatting, exit codes that
   mean what they say (`0` = legal/valid, non-zero = problem).
4. **No workarounds.** A name is either resolvable or it isn't. A deck is
   either legal or the validator prints every reason it isn't.

## Layout

```
tools/mtg            # CLI shim → tools/mtg.py
tools/mtg.py         # the toolkit (sync, card, printing, legal, validate, search)
data/                # Scryfall bulk + pickled name/printing index (gitignored)
decks/<slug>/vN.txt  # MTGA-export deck files, versioned
docs/formats.md      # Arena format quick-reference (sizes, singleton, identity)
docs/gotchas.md      # the non-obvious stuff (A- prefix, brawl key, etc.)
docs/sources.md      # curated meta sources, last-verified dated
flake.nix            # dev shell (python3 + uv + jq + curl)
```

## Setup

With Nix + direnv:

```sh
direnv allow      # enters the flake dev shell automatically
mtg sync          # download Scryfall bulk + build local index (~150 MB)
```

Without Nix, anything with `python3 ≥ 3.10` works. No third-party Python
dependencies — the CLI uses only the standard library.

`mtg sync` is idempotent and short-circuits if Scryfall's `updated_at` matches
the local copy. Re-run it daily (or on demand after a banlist announcement).

## Commands

| command                                | what it does                                            |
|----------------------------------------|---------------------------------------------------------|
| `mtg sync [--force]`                   | refresh bulk + rebuild name/printing index              |
| `mtg card <name>`                      | full card info, including per-Arena-format legality     |
| `mtg printing <SET> <NUM>`             | look up by MTGA-style set code + collector number       |
| `mtg legal <name> <format>`            | yes/no legality with reason; exit 0 iff legal *and* on Arena |
| `mtg validate <deck.txt> -f <format>`  | parse + validate a full MTGA-export deck file           |
| `mtg search '<scryfall-query>'`        | live Scryfall search (one HTTP request)                 |

Supported format keys: `standard`, `standardbrawl`, `historic`, `brawl`
(= Historic Brawl on Arena), `alchemy`, `timeless`, `pioneer`, `explorer`.

### Validator coverage

- deck-size rules (60+ for constructed, exactly 100 for Brawl)
- singleton enforcement for Brawl (basic lands exempt)
- 4-of limit for constructed (basic lands exempt)
- sideboard size (≤ 15)
- per-card legality in the chosen format
- per-card Arena availability (`games` includes `arena`)
- Brawl color-identity subset rule
- commander type check (legendary creature / planeswalker)

## Deck file format

The MTGA export format, verbatim. Section headers (`Commander`, `Deck`,
`Sideboard`, `Companion`, `Maybeboard`) are recognised. Each card line is:

```
<count> <name> (<set>) <collector_number>
```

Example (`decks/nadu/v0.txt`):

```
Commander
1 A-Nadu, Winged Wisdom (MH3) 193

Deck
7 Forest (KTK) 258
1 Counterspell (FCA) 4
...
```

The `A-` prefix lives in the **name**, not the collector number — that's how
MTGA exports rebalanced cards, and it's how Scryfall stores them.

## Working with this repo as an AI agent

If you are an LLM agent reading this in a Claude Code session:

1. Run `mtg sync` once at the start of a session if `data/bulk-meta.json` is
   missing or > 24h old.
2. To answer "is X legal in Y", call `mtg legal "X" Y` — never guess from
   training data.
3. Before reporting a deck as ready, run `mtg validate <path> -f <format>`
   and surface every message it prints. Exit code 0 = clean.
4. For meta context (what's actually winning right now), use `docs/sources.md`
   — the listed sites are dated and known to be current.
5. For format-rule questions, read `docs/formats.md`. For the surprise-you
   stuff (rebalanced cards, brawl-key naming), read `docs/gotchas.md`.

The validator is intentionally strict and verbose; that's the contract — if
it stays quiet, the deck imports cleanly into Arena and is legal in the
declared format.

## License

No license declared yet. Treat as all-rights-reserved until that changes.
