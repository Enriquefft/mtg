# Historic & Historic Brawl on Arena

## What these formats are

**Historic** is Arena's non-rotating 60-card constructed format. Pool =
every Standard set since *Ixalan* + Anthologies + Remastered sets +
direct-to-Historic drops. Decks: 60+ main, 4-of, ≤15 sideboard, no
commander, no color-identity rule.

**Historic Brawl** is the 100-card singleton commander variant on the
same pool. One legendary creature/planeswalker commander, 99 unique
non-commander cards, color identity ⊆ commander CI, no sideboard.
Casual matchmaking is bracketed by the Game-Changer list (below).
Both share the Historic ban list; Brawl adds commander-only bans.

## Format-name gotcha

Scryfall's legality keys do not map 1:1 to the names players use:

| user says            | scryfall key      | `mtg` flag          |
|----------------------|-------------------|---------------------|
| "Historic Brawl"     | `brawl`           | `-f brawl`          |
| "Standard Brawl"     | `standardbrawl`   | `-f standardbrawl`  |
| "Historic" (60-card) | `historic`        | `-f historic`       |

There is **no** `historicbrawl` key on Scryfall. Passing `-f historicbrawl`
to `mtg validate` fails — `-f brawl` is correct for the 100-card format.

```bash
tools/mtg validate decks/nadu/v1.txt -f brawl       # Historic Brawl
tools/mtg validate decks/historic/<deck>.txt -f historic  # Historic constructed
tools/mtg legal "A-Nadu, Winged Wisdom" brawl       # check Historic Brawl legality
```

Full table of Arena formats and their Scryfall keys lives in
`docs/formats.md`.

## The card pool

Historic = Standard ∪ non-rotating sets ∪ Anthologies ∪ Remastered
sets ∪ Alchemy `A-` printings. Timeless ⊃ Historic (adds back banned
cards) but **excludes A- printings** — an `A-` card legal in Historic
/ Brawl is illegal in Timeless, and the unrebalanced original may be
the reverse. Always check via `tools/mtg legal "<name>" <fmt>`.

## Wildcard / currency model

### Rarity → wildcard cost

MTGA crafts are 1:1 — one wildcard of the matching rarity per copy.
Rarity totals for any deck:

```bash
tools/mtg wildcards decks/<name>/<version>.txt
```

Cross-deck planning (what to craft to maximise meta coverage):

```bash
tools/mtg wantlist --decks 'decks/historic/*.txt' --latest-only
```

`wantlist` aggregates max-shortfall across every matching deck — you
only ever craft each card once.

### Draft economy

WCs drop from packs (rarity-track progress per pack) and quest
rewards. Drafting is the best gold/gem efficiency for rare/mythic WCs
at scale; constructed event rewards are smaller but steady. For a
single deck: `tools/mtg gaps decks/<name>/<version>.txt` short-lists
the missing cards and WC cost.

## Singleton & 100-card rule (Brawl)

Exactly **100 cards** including the commander. No partner pairs (Arena
does not implement partner). Singleton applies to every card except
basic lands — any number of basic Mountains, only one of any nonbasic
land or any other card. `tools/mtg validate -f brawl` enforces all
three.

## Color identity in Brawl

Every card's `color_identity` (Scryfall-computed) ⊆ commander's.
Hybrid pips count as both colors. Reminder-text symbols don't count
(Scryfall already strips them). Lands count by produced mana — a
mono-W commander cannot run *Hallowed Fountain*. The validator
enforces this; full mechanics in `docs/gotchas.md` §4.

## Banned-as-commander vs banned outright

Scryfall encodes commander-only bans as `legalities.brawl == "banned"`.
On Arena, Historic Brawl uses the `brawl` field for both "legal in the
99" and "legal as commander", so `banned` here means the card cannot
be the commander (and usually cannot be in the 99 either — the
validator rejects both). Check with `tools/mtg legal "<card>" brawl`.
Canonical WotC announcement page:
https://magic.wizards.com/en/banned-restricted-list — the local index
picks up new bans within ~1h after `tools/mtg sync`.

## Game-Changer bracket

Cards with `game_changer: true` on Scryfall push your Brawl deck into
a higher casual-matchmaking bracket. Informational only — Wizards
enforces this on the matchmaking side, not via legality.
`tools/mtg card "<name>"` surfaces the flag; `tools/mtg suggest-subs`
refuses to silently promote a non-GC deck by substituting a GC card.

## Common deck-building shells

**Historic Brawl:** 5C goodstuff (every staple, land base does the
work), mono-color value (small CI, deeper lands, redundant card-draw),
tribal (Faerie / Sliver / Demon / Dragon), combo-tutor (commander
tutors a two-card kill).

**Historic constructed:** Izzet Phoenix, Affinity, Auras, Sacrifice /
Cat-Oven, Combo-Vannifar, Jund Food. Meta rotates; see `docs/sources.md`
for current tier-list URLs.

Discover novel shells from your collection rather than copying the meta:

```bash
tools/mtg shells --format brawl    --by keyword
tools/mtg shells --format historic --by type --min-cards 20
tools/mtg shells --format historic --by theme
```

## Common pitfalls

- **`A-` prefix legality split.** *Nadu, Winged Wisdom* is illegal in
  Historic Brawl; *A-Nadu, Winged Wisdom* is legal. The `A-` lives in
  the **name**, not the collector number. See `docs/gotchas.md` §2.
- **Multi-face cards need `Front // Back`.** Adventure / split / DFC /
  flip cards must spell both halves with the literal ` // ` separator
  (e.g. `Questing Druid // Seek the Beast`, `Brazen Borrower // Petty
  Theft`). Front-only is a hard validate error. See `docs/gotchas.md` §3a.
- **`(SET) NUM` is art-only.** MTGA resolves by name; the set/coll
  hint only picks art. Wrong set never breaks import — wrong name does.
- **Arena availability ≠ paper.** A card on Scryfall doesn't mean it's
  on Arena. The validator checks `arena ∈ games`; trust it over memory.
- **Stale collection.** Collection-aware subcommands warn to stderr if
  `data/collection.json` is >7 days old. Re-dump before trusting
  `coverage` / `gaps` / `suggest-subs` numbers.

## Workflow: building a Historic Brawl deck

0. **Recency check.** `tools/mtg search 'legal:brawl game:arena date>=<~6mo ago>'` — your training data does not cover the latest sets.
1. **Shell enumeration.** Name 2–3 candidate archetypes, pitch each in one sentence, pick one with explicit reasoning vs the others.
2. **Mechanic sweep.** If the prompt's anchor card has a named keyword, `tools/mtg related "<anchor>" -f brawl` to enumerate sister cards.
3. Pick a commander: `tools/mtg search 'legal:brawl game:arena t:legendary t:creature ...'`.
4. WebFetch a meta source from `docs/sources.md` and write a one-line plan vs each top archetype.
5. Draft `decks/<name>/v0.txt` in MTGA-export format.
6. `tools/mtg validate decks/<name>/v0.txt -f brawl` until clean.
7. `tools/mtg analyze decks/<name>/v0.txt` and `tools/mtg manabase decks/<name>/v0.txt` — read, don't just glance.
8. Iterate `v1.txt`, `v2.txt`. Re-run analyze + validate after each change.

## Workflow: building a Historic constructed deck

Same steps with constructed adjustments:

1. 60 main + ≤15 sideboard, 4-of non-basics, no color-identity rule.
2. Companion lives in the **sideboard**, not the command zone:
   ```
   Sideboard
   1 Jegantha, the Wellspring (IKO) 222
   ```
   `tools/mtg companion decks/<name>/<version>.txt` enforces both the
   mechanical predicate and the sideboard-slot rule for non-Brawl.
3. Composition analysis: main only (default), or include / isolate SB:
   ```bash
   tools/mtg analyze decks/<name>/<version>.txt --include-sideboard
   tools/mtg analyze decks/<name>/<version>.txt --sideboard-only
   ```
4. Pull a meta corpus to compare against:
   ```bash
   tools/mtg fetch-meta historic --out decks/historic/ --limit 30
   tools/mtg coverage --batch --glob 'decks/historic/*.txt' --with-subs --json
   ```
   `mtgazone` is the only auto-fetchable Arena tier source today; see
   `docs/sources.md` for the bot-block reality table.
