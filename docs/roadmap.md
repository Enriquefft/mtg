# CLI roadmap: collection-first deck discovery

## Philosophy

Claude is smart, code is not. The CLI does enumeration, scoring, parsing,
IO. Claude does taste, judgment, narrative. Every subcommand on this page
moves work **out** of Claude into deterministic code so a single session
turn answers the canonical prompt below without burning context on data
shuffling.

## The canonical prompt this roadmap exists to serve

> "Find every viable deck I can build right now from my collection at ≥90%
> (≥97% with replacements). Format `<X>`. Good decks only — meta or novel.
> If I'm short cards, suggest owned replacements that preserve the deck's
> plan."

Today this prompt requires Claude to: WebFetch ~5 meta pages, hand-parse
~30 decklists, save each to disk, loop `coverage`/`gaps`, and eyeball
substitutions card-by-card. Several hours, error-prone, novel-deck signal
absent.

After the additions below the same prompt is four CLI calls and a single
narrative turn.

## Required subcommands

### 1. `fetch-meta` — pull a meta corpus into the repo

```
tools/mtg fetch-meta <format> [--source mtgazone|mtggoldfish|untapped]
                              [--out decks/<format>/]
                              [--limit N]
```

Walks the per-format source list in `docs/sources.md`, scrapes each
tier-list / archetype page, writes one MTGA-export `.txt` per archetype
into `--out`. Body is standard `Deck` + `Sideboard` MTGA export, byte-
identical to what Arena's import dialog accepts — no inline metadata.

Tier / win-rate / sample / source / fetched-at metadata lives in a
sidecar `<out>/meta.json` indexed by relative deck-file path:

```json
{
  "izzet-phoenix.txt": {
    "source": "untapped",
    "tier": "S",
    "winrate": 0.547,
    "sample": 2143,
    "fetched": "2026-04-30",
    "archetype": "Izzet Phoenix"
  }
}
```

Sidecar over embedded headers because (a) the deck file stays a clean
single source of truth for the cards, (b) `parse_deck` (`tools/mtg.py`,
the `[warn] cannot parse line` path) does not silently skip `#`-prefixed
lines today and patching it to do so would let real typos slip past the
validator, (c) downstream subcommands look the metadata up by path
without re-parsing.

Per-source parsers live in `tools/mtg/sources/<host>.py`. Add them
incrementally. **Hard-fail on source errors**, including 403 (see
`docs/sources.md` bot-block table). Exit non-zero, print every failed
source + URL + status to stderr, write nothing to `--out`. Silent
fallback to the next source would degrade the corpus invisibly and
violates the production-ready floor — let the caller (Claude) retry,
escalate, or pick an explicit `--source` and re-run.

Single source of truth invariant: Scryfall stays canonical for card
data. Source pages are canonical only for *deck composition + tier
signal* — both fields the local Scryfall index cannot derive.

### 2. `coverage --batch` — rank every deck in a directory

```
tools/mtg coverage --glob 'decks/historic/*.txt'
                   [--min 0.90]
                   [--with-subs]
                   [--json]
```

Replaces today's one-deck-per-invocation flow. Columns:

| archetype | tier | owned% | missing-WC (M/R/U/C) | with-subs% | top-3 missing |

`--with-subs` invokes `suggest-subs` internally per deck and reports the
lifted percentage so the user sees both numbers side by side.

`--json` is the contract Claude reads. The text table is for humans.

### 3. `suggest-subs` — the replacement engine

The hardest and highest-leverage gap. For each missing card propose
owned candidates that preserve deck function.

```
tools/mtg suggest-subs <deck.txt> [--max-per-card N=5]
                                  [--apply <out.txt>]
```

Deterministic algorithm. No LLM. Reuses `_classify` (`tools/mtg.py`),
which is already a per-card function returning a `set[str]` of role
tags (removal / sweeper / counter / hand attack / peek / CA / loot /
tutor / ramp / recursion / threat). The work here is renaming
`_classify` → `classify_card` and exporting `_ROLE_FUNC`/`_ROLE_TYPE`
as module-level constants so `suggest-subs` can call them without
duplicating the rules.

For each missing card `C` with role tags `R`, color identity `I`, CMC
`K`, type-line `T`:

- Candidate set: owned cards where
  - **owned copies ≥ proposed copies in the deck** (without this
    predicate `--with-subs%` is fiction — the engine could "solve" a
    deck by proposing cards the user doesn't own),
  - legal in the deck's declared format,
  - **not an `A-` rebalanced card unless the format is Alchemy or
    Historic-Brawl-with-rebalances** (see `docs/gotchas.md` §2 — A-
    cards have distinct legalities; subbing one in silently changes
    deck legality),
  - color identity ⊆ `I` (or = `I` for Brawl),
  - role tags overlap `R`,
  - `|CMC − K| ≤ 2` (band ±1 rejects most real subs — a 2-CMC counter
    is a fine sub for a 3-CMC counter; let scoring sort closeness),
  - not already at max copies in the deck (singleton for Brawl, 4 for
    constructed),
  - does not violate companion clause if one is declared (see step 5
    on extracting `_jegantha_ok` / `kaheera_ok` / `_is_permanent` into
    pure predicates).

- Score components, summed:
  - `3 · |role(cand) ∩ R| / max(1, |role(cand)|)` — role overlap
    normalised by candidate breadth. Plain `3·|role∩R|` over-rewards
    multi-tag cards: a vanilla creature classified `creature, threat`
    would beat a clean 1-mana removal spell when subbing for removal.
  - `2 · max(0, 2 − |CMC − K|)` — CMC closeness within the ±2 band.
  - `1 · (type-line match)` — same primary type (creature / instant /
    sorcery / enchantment / planeswalker / artifact).
  - `1 · (supertype match)` — same supertype (legendary, etc).
  - **Rare-role boost**: if `R` contains a *rare* role for the deck
    (sweeper, counter, tutor — verify rarity by counting role-tag
    incidence in the deck's existing card pool), multiply final score
    by 1.5. Replacing a sweeper with a threat is much worse than
    replacing a threat with a different threat.

- For Brawl decks: if `C` is **not** Game-Changer-flagged but a
  candidate **is** (`game_changer: true` on Scryfall, surfaced by
  `analyze`), drop the candidate or warn explicitly — silently
  promoting the deck's bracket changes matchmaking pool.

- Output: per-missing-card block with top `N` candidates and their
  scores. Cards with zero owned copies never appear.

`--apply <out.txt>` writes a substituted deck picking the #1 candidate
per missing slot. **Multi-face cards must be emitted as `Front //
Back`** (`MULTIFACE_LAYOUTS`, `tools/mtg.py:427`) or Arena rejects the
import. The output is itself a valid MTGA-export deck — closing the
loop with `coverage` and `validate`. If a missing card has zero
qualifying candidates, the slot stays missing in `--apply` output and
`--with-subs%` carries the full deficit forward (denominator unchanged,
numerator does not gain).

Why this lives in the CLI and not in Claude: enumerate–rank–filter over
the 30K-row dataset. Pure pattern-match work. Reading 100 candidates per
missing slot into Claude's context would blow the window inside three
decks.

### 4. `shells` — novel decks the meta corpus misses

```
tools/mtg shells --format <fmt> [--min-cards N] [--by keyword|type|theme]
```

Groups owned, format-legal cards by keyword (Blitz, Survival,
Aftermath…), creature type (Demon, Sliver, Faerie…), or theme tag.
Emits clusters where `owned-card-count ≥ --min-cards`.

Defaults: **24 for 60-card constructed, 15 for Brawl.** A 60-card deck
needs ~24 themed nonland slots; lands and generic support fill the
rest. Setting the threshold at 50 (a full deck's worth) hides the real
shells where the theme is the engine and the rest is glue. Same logic
for Brawl: ~15 themed cards is enough to build around.

Same `A-` filter rule as `suggest-subs`: exclude rebalanced cards
unless the format actually accepts them.

Per cluster:

- card count,
- color spread (which color combinations are reachable),
- top 10 highest-rarity owned anchors (mythic/rare proxy for the
  cards a deck would build *around*).

Claude reads the cluster list, picks which deserve a build, drafts.
The CLI does not attempt to assemble decks — that requires taste.

### 5. Historic / constructed format support

The CLI is Brawl-shaped today. Audit results against `tools/mtg.py`:

- `validate` (`tools/mtg.py:469-486`): already honours `Sideboard`,
  4-of, 60-min, ≤15 SB. **No change needed.**
- `coverage` / `gaps`: `_deck_demand` (`tools/mtg.py:2434`) already
  includes sideboard. **No change needed.**
- `cmd_wildcards` (`tools/mtg.py:1083`): main + commander only.
  **Fix: include sideboard in the totals.**
- `cmd_analyze` (`tools/mtg.py:780`): main only. **Fix: take a
  `--include-sideboard` flag (default off — composition analysis is
  about the main 60), but also accept sideboard for SB-only views.**
- `cmd_companion` (`tools/mtg.py:1181`): runs the predicate over
  `{deck, commander}`. **Fix: for non-Brawl formats, run the
  predicate over `{deck, sideboard}` and require the companion card
  itself to be in the sideboard. The mechanical predicate
  (`_jegantha_ok` etc.) is the same; the slot it reads from is not.**
- **Companion-predicate extraction**: `_jegantha_ok`, `kaheera_ok`,
  `_is_permanent`, and friends live as inline lambdas inside
  `cmd_companion` today. Lift them to module-level pure functions —
  `suggest-subs` (step 2) needs to call them when filtering candidates
  against a declared companion clause. Extract here, before step 2
  lands, or step 2 will duplicate them.

Add `docs/historic.md` covering: 60-card / 4-of / 15-SB / companion in
sideboard / Alchemy `A-` cards excluded from Historic / Pioneer-Arena
pool subtleties.

Split the "Workflow for building a new deck" section in `CLAUDE.md`
into a Brawl path and a constructed path. Cross-link to
`docs/historic.md`.

### 6. `wantlist` retarget (no code change)

Already glob-driven. After `fetch-meta` populates `decks/historic/`:

```
tools/mtg wantlist --decks 'decks/historic/*.txt' --latest-only
```

answers "what should I craft to maximise meta coverage". Add this as a
worked example in `CLAUDE.md` once `fetch-meta` lands.

## Canonical workflow after the additions

```bash
tools/mtg sync
tools/mtg fetch-meta historic --out decks/historic/ --limit 30
tools/mtg coverage --glob 'decks/historic/*.txt' --with-subs --min 0.90 --json > /tmp/cov.json
tools/mtg shells --format historic --by keyword                    > /tmp/shells.txt
```

Claude reads `/tmp/cov.json` + `/tmp/shells.txt`, picks the top 5 by
`tier × with-subs%`, narrates the trade-offs and the recommended subs.
One CLI block, one Claude turn.

## Implementation order

Land in this sequence. Each step is independently shippable.

1. **Historic / constructed format support + helper extraction.**
   Sideboard fixes in `cmd_wildcards`/`cmd_analyze`/`cmd_companion`,
   plus three pure-function lifts that step 2 depends on:
   - rename `_classify` → `classify_card`, export `_ROLE_FUNC` /
     `_ROLE_TYPE` as module-level constants;
   - lift `_jegantha_ok`, `kaheera_ok`, `_is_permanent`, and the
     other companion predicates out of `cmd_companion`'s inline
     lambdas to module-level pure functions;
   - factor the snapshot-staleness warner so collection-touching
     subcommands can call it without re-implementing the check.
   Smallest blast radius; unblocks every step below.
2. **`suggest-subs`.** Highest leverage. `--with-subs` is empty
   without it. Calls the helpers extracted in step 1.
3. **`coverage --batch`.** Trivial once `suggest-subs` is callable —
   it's a glob loop with a JSON formatter.
4. **`fetch-meta`.** Largest surface (one parser per source) but each
   source can be added independently. Start with mtgazone — only
   auto-fetchable Arena tier source after the 2026-04-30 untapped
   block. untapped scrape deferred; aetherhub manual-only.
5. **`shells`.** Cherry on top; novel-deck signal.
6. **`docs/historic.md` + `CLAUDE.md` workflow split.** Documentation
   pass once the code is in place.

## Out of scope (intentional)

- **No deck-strength model.** Tier comes from the source page; Claude
  reasons about it. Building one would duplicate work the meta sites
  already do better.
- **No price / wallet model beyond wildcards.** Arena economy is WC.
- **No deckbuilding-from-scratch agent.** `shells` surfaces clusters;
  Claude assembles.
- **No paper-format support.** This toolkit is Arena-only by charter.
- **No bot-block circumvention.** untapped/aetherhub stay manual per `docs/sources.md`.

## Invariants any contributor must preserve

- `data/` stays CLI-only. New subcommands access it via the existing
  index helpers, never by re-opening the bulk JSON.
- Scryfall is the single source of truth for card data. No new card
  databases. No per-card API caching — bulk covers everything.
- No workarounds, no `# TODO fix later`, no half-wired flags. A
  subcommand ships complete or it doesn't ship.
- Every new subcommand emits both a human-readable text form and a
  `--json` form. Claude reads JSON; humans read text.
- Every collection-touching subcommand (`coverage`, `gaps`, `wantlist`,
  `suggest-subs`, `shells`, `own`, `owned`) warns to stderr if
  `data/collection.json` is older than 7 days, mirroring the
  `mtg sync` staleness warner for the bulk index. A stale snapshot
  produces silently-wrong rankings — the user must know.
- `A-` rebalanced cards are filtered by format wherever the CLI
  proposes a card the user did not type themselves (`suggest-subs`,
  `shells`). The base card and its `A-` printing have distinct
  legalities (see `docs/gotchas.md` §2); proposing the wrong one
  silently breaks the deck on import.
- Multi-face cards (`MULTIFACE_LAYOUTS`, `tools/mtg.py:427`) are
  emitted as `Front // Back` whenever the CLI writes a deck file.
  Front-only spellings fail Arena's importer (`docs/gotchas.md` §3a).
