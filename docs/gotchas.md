# MTGA + Scryfall gotchas

These are non-obvious behaviors you'll trip over if you don't know them.

## 1. `legalities.brawl` = Historic Brawl on Arena

Scryfall's legality keys do not match player-facing names:

| user says            | scryfall key      | `mtg` flag          |
|----------------------|-------------------|---------------------|
| "Historic Brawl"     | `brawl`           | `-f brawl`          |
| "Standard Brawl"     | `standardbrawl`   | `-f standardbrawl`  |
| "Historic" (60-card) | `historic`        | `-f historic`       |

There is **no** `historicbrawl` key on Scryfall — passing
`-f historicbrawl` to `mtg validate` fails. `-f brawl` is the
100-card Historic Brawl format. Full per-format table in `formats.md`.

## 2. A- prefix for Alchemy rebalanced cards

When Wizards rebalances a card for digital play, they create a new printing
prefixed with `A-` (e.g. `A-Nadu, Winged Wisdom`). On Scryfall:

- The rebalanced card has its own `oracle_id` and its own legalities.
- Its `name` already starts with `"A-"`.
- Its `collector_number` is `A-<N>` (e.g. `A-193`), even though the MTGA
  export shows just `193` after the parenthesized set code.

In the MTGA export format the line is:

    1 A-Nadu, Winged Wisdom (MH3) 193

The `A-` lives in the **name**, not the collector number. The validator
treats the name as authoritative — it looks up `A-Nadu, Winged Wisdom`
directly on Scryfall.

The base card and the rebalanced card often have **different legalities**.
Example:

| version                       | brawl     | timeless | alchemy   |
|-------------------------------|-----------|----------|-----------|
| Nadu, Winged Wisdom           | not_legal | legal    | not_legal |
| A-Nadu, Winged Wisdom         | legal     | not_legal| not_legal |

If you put unrebalanced Nadu in a Historic Brawl deck Arena will reject
it, even though both cards exist on Arena.

## 3. Arena resolves decks by name

When MTGA imports a deck list, it ignores the `(SET) NUM` portion and
resolves cards by name against the cards available on Arena. The set/coll
hint exists for art selection only.

Validator implication: a card is "Arena-legal" iff **any** printing of
that name has `arena` in its `games` array.

## 3a. Multi-face cards: MTGA wants the FULL name with ` // `

For any card whose Scryfall `layout` is one of:

    split | adventure | modal_dfc | transform | flip

MTGA's importer requires the full name including both halves and the
literal ` // ` separator. Examples:

    1 Unholy Annex // Ritual Chamber (DSK) 118        ✅ imports
    1 Unholy Annex (DSK) 118                          ❌ MTGA: "card not found"
    1 Brazen Borrower // Petty Theft (ELD) 39         ✅
    1 Fable of the Mirror-Breaker // Reflection of Kiki-Jiki (NEO) 141  ✅

`tools/mtg validate` enforces this as a hard error: any deck-line whose
resolved card has `layout ∈ {split, adventure, modal_dfc, transform,
flip}` must spell the full `name` (with ` // `). Front-only spellings
fail validation with `mtga-import: '<front>' must be written as
'<full>' for Arena to accept the deck import`.

## 4. Color identity for Brawl

`color_identity` is computed by Scryfall and includes:

- mana cost pips
- mana symbols in rules text (including hybrid)
- explicit color indicators
- for lands, the colors of mana they produce

Hybrid pips count as both colors. Reminder text inside reminder parens
does NOT count (Scryfall already handles this).

## 5. `game_changer` flag

Brawl introduced a "Game Changer" list — cards that push your deck into
a higher bracket (limits singleton vs casual matchmaking). Scryfall exposes
this as a top-level `game_changer: true` flag on affected cards. The
validator surfaces it in `mtg card` output but does not enforce a bracket
rule (Wizards' bracket implementation is matchmaking-side, not deck-legality).

## 6. Bulk freshness

`default_cards.json` is rebuilt by Scryfall daily, around 21:00 UTC. The
`mtg sync` command checks Scryfall's `updated_at` and skips the download
if our local copy is current. After a banlist update Scryfall typically
reflects new legalities within ~1 hour; the next `mtg sync` picks it up.

For *very* time-sensitive checks (banlist day-of), prefer:

    mtg search 'name:"<card>" legal:brawl'

which hits the live API and bypasses the local cache.
