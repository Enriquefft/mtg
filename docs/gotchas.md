# MTGA + Scryfall gotchas

These are non-obvious behaviors you'll trip over if you don't know them.

## 1. `legalities.brawl` = Historic Brawl on Arena

Despite the name, Scryfall's `brawl` field tracks the 100-card singleton
**Historic Brawl** format on Arena. Scryfall's `standardbrawl` is the
Standard-pool variant.

There is *no* `historicbrawl` key on Scryfall today (it used to exist, was
removed after the formats consolidated).

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
