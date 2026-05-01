# MTG Arena format quick-reference

Format keys here match Scryfall's `legalities.<key>` field, which is what
`mtg legal` and `mtg validate -f <fmt>` consume.

| Arena format     | scryfall key      | size | singleton | sideboard | identity rule | notes |
|------------------|-------------------|------|-----------|-----------|---------------|-------|
| Standard         | `standard`        | 60+  | no (4-of) | ≤15       | none          | rotates yearly |
| Standard Brawl   | `standardbrawl`   | 60   | yes       | none      | commander's CI | 1 commander, current Standard pool |
| Historic         | `historic`        | 60+  | no (4-of) | ≤15       | none          | non-rotating Arena pool incl. Anthologies |
| **Historic Brawl** | **`brawl`**     | 100  | yes       | none      | commander's CI | 1 commander, full Historic pool. Scryfall calls this `brawl`. |
| Alchemy          | `alchemy`         | 60+  | no (4-of) | ≤15       | none          | uses A-rebalanced versions |
| Timeless         | `timeless`        | 60+  | no (4-of) | ≤15       | none          | broadest Arena pool, no rebalanced cards |
| Pioneer          | `pioneer`         | 60+  | no (4-of) | ≤15       | none          | paper Pioneer; smaller card pool than Explorer used to be |
| Explorer         | `explorer`        | 60+  | no (4-of) | ≤15       | none          | Arena's pre-Pioneer step; Wizards has been merging this into Pioneer |

## Naming gotcha

Format-name gotcha: see `docs/historic.md` §"Format-name gotcha".

## Banned-as-commander

Some cards are legal in Brawl but banned **as commander** (e.g. plain old
banned-list maintenance after a card proves problematic). Scryfall encodes
this as `legalities.brawl == "banned"` on the affected card. The Wizards
banned-and-restricted page is the canonical announcement source if you
need to know *why* something is banned:

- https://magic.wizards.com/en/banned-restricted-list

## Color identity

A card's `color_identity` is the union of all mana symbols in its mana
cost AND in its rules text (including activated/triggered abilities and
hybrid pips), plus its color indicator. Lands' identity is the colors of
mana they can produce. Scryfall's `color_identity` array is canonical.

Brawl rule: every card in your deck must have `color_identity ⊆`
commander's `color_identity`. The validator enforces this.
