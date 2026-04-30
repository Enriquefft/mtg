# MTG Arena deck-building toolkit

This repo is groundwork for building MTG Arena decks. It exists so any
future Claude session can answer "is this card legal in Historic Brawl on
Arena", "what does this card do", and "validate my deck list" without
hitting rate limits or trusting outdated internet info.

## Hard rule: `data/` is CLI-only

`data/` holds the 30K-row Scryfall bulk dump (~500 MB JSON), an 80 MB
pickled index, and the full collection snapshot. Reading any of these
into context will blow the window in a single tool call and corrupt the
session. **Never `Read`, `Glob`, `Grep`, `cat`, `head`, `tail`, or `jq`
the files in `data/`.** `.claude/settings.json` enforces this with a
deny rule; the only sanctioned access path is the `tools/mtg` CLI, which
materialises the answer to your question instead of the dataset behind
it. If a query you need isn't covered by an existing subcommand, add a
new subcommand — don't reach into the data files.

`python -c "open('data/...')"` and shell input redirects (`cmd < data/foo`) bypass the deny list — don't reach for them.

## Quick start

```bash
# inside the dev shell (direnv `use flake` handles this)
nix develop                                     # or: direnv allow

cd /home/hybridz/Projects/mtg
tools/mtg sync                                  # one-time, ~30s download
tools/mtg validate decks/nadu/v0.txt -f brawl   # offline validation
```

## How to use it

1. **Always run `tools/mtg sync` once per session** — it's a no-op if
   Scryfall hasn't published a new bulk since your last run, so it's cheap.
   The command warns you if the cache is >36h old.

2. **For card lookups**, use the local CLI, NOT the Scryfall API directly:
   ```bash
   tools/mtg card "Hei Bai, Forest Guardian"
   tools/mtg legal "A-Nadu, Winged Wisdom" brawl
   tools/mtg printing MH3 193
   ```
   These are instant, offline, and don't burn rate budget.

3. **For deck validation**, always run before declaring a deck "done":
   ```bash
   tools/mtg validate decks/<name>/<version>.txt -f brawl
   ```
   Exit code 0 = clean. The validator checks: arena availability, format
   legality, deck size, singleton rule, color identity, commander type.

4. **For composition analysis**, run after each version:
   ```bash
   tools/mtg analyze decks/<name>/<version>.txt
   ```
   Prints type composition, function tags (removal / sweeper / counter /
   hand attack / peek / card advantage / loot / tutor / ramp / recursion
   / threat), nonland mana curve, and a per-card classification table in
   deck-file order. Pure data — no thresholds, no warnings. You read
   the table and decide what's missing.

5. **For sister-card discovery** when an anchor card uses a named mechanic
   (Blitz, Survival, Squad, Repartee, etc.):
   ```bash
   tools/mtg related "<card>" -f <format>
   ```
   Prints every Arena-legal card sharing each keyword. Rarer keywords
   first — those are the synergy clusters. Use this BEFORE drafting
   around a unique card; the second-best card with the same keyword is
   often sitting in the same set and will be missed if you build from
   memory.

6. **For manabase / wildcard / companion checks**, run on every version:
   ```bash
   tools/mtg manabase  decks/<name>/<version>.txt   # pip demand + sources + etb-tapped
   tools/mtg wildcards decks/<name>/<version>.txt   # rarity counts (MTGA WC cost)
   tools/mtg companion decks/<name>/<version>.txt   # eligibility per companion
   ```
   All three are pure data dumps — no thresholds, no recommendations.
   Read the tables, decide what to change.

7. **For ad-hoc commander/staple discovery**, use the search subcommand —
   it hits Scryfall live (one HTTP request, paginated):
   ```bash
   tools/mtg search 'legal:brawl game:arena t:legendary t:creature c=5'
   ```
   Use Scryfall's full query syntax: https://scryfall.com/docs/syntax

8. **For meta info** (what's playing well right now), open `docs/sources.md`.
   It lists per-format URLs known to be live + maintained, with a
   last-verified date. WebFetch them when you need real numbers.

9. **For collection-aware decisions** (do I own this? what's a deck cost
   me in wildcards?), populate `data/collection.json` once and query it:
   ```bash
   # one-time: build the .NET injector + payload from the nix dev shell.
   # Produces injector/bin/Release/net48/mtg-inject.exe and
   # payload/bin/Release/net48/MtgInventoryPayload.dll, which
   # `collection dump` shells out to.
   (cd tools/inject && dotnet build -c Release)

   # primary: dump straight from the running MTGA process (full pool,
   # cards you've never decked included). Launch MTGA via Steam, sign
   # in to the main menu, then:
   tools/mtg collection dump

   # alternative: import a tracker export
   tools/mtg collection import ~/Downloads/collection.csv

   # fallback: lower-bound from your own decks in Player.log
   tools/mtg collection from-decks

   # queries
   tools/mtg collection                # snapshot summary
   tools/mtg own "Sheoldred, the Apocalypse"
   tools/mtg gaps     decks/<name>/<version>.txt   # short-list + WC cost
   tools/mtg coverage decks/<name>/<version>.txt   # buildable %
   tools/mtg wantlist [--latest-only] [--decks 'decks/foo/*.txt']
   ```
   `wantlist` aggregates wildcard needs across every locally-saved deck
   so you can plan crafts globally rather than per-deck (max shortfall
   wins — you only craft each card once).
   `collection dump` builds a tiny .NET payload (`tools/inject/`) and
   injects it into MTGA's Mono runtime to read `WrapperController.
   InventoryManager.Cards` directly — the only path that captures cards
   you own but have never put in a deck. It needs the Nix dev shell
   (`dotnet-sdk_8` + `util-linux`'s `nsenter`) and works on Linux/Proton
   only; the injector joins MTGA's pressure-vessel mount namespace via
   `nsenter` so wineserver state is shared. Modern MTGA does **not**
   dump the card pool to Player.log — only the decks you have built —
   so `from-decks` is a strict lower bound. The CSV importer auto-
   detects either an `arena_id`/`cardId`/`grpId` column or a
   `set`+`collector_number` pair; the JSON importer accepts a flat
   `{arena_id: qty}` dict, the canonical wrapper, or a list of
   `{cardId, quantity}` records.

## Training-cutoff rule (read this BEFORE drafting any cardlist)

**Your card knowledge is frozen at your training cutoff. Today's date in
the dev shell is real-time.** Sets printed after your cutoff exist on
Scryfall; you have not memorised them. Building a deck from memory means
silently ignoring every card from those sets — which is exactly the kind
of staleness this repo was built to prevent.

Before suggesting a single card from memory, run a recency check against
the local index:

```bash
# enumerate Arena-legal cards in your format printed in the last ~6 months
tools/mtg search 'legal:<fmt> game:arena date>=2025-10-01'

# narrower: cards in your archetype's colors / role
tools/mtg search 'legal:pioneer game:arena date>=2025-10-01 c<=B t:creature'
tools/mtg search 'legal:pioneer game:arena date>=2025-10-01 o:"destroy target"'
```

Pick the date threshold ≈ 3 months *before* your training cutoff (sets
near the cutoff are usually under-represented in training data). Skim the
results before writing the deck. If a recent card looks relevant,
`tools/mtg card "<name>"` for full oracle text.

This is not optional. The user explicitly noticed when an earlier session
"built from memory" and missed recent cards — that's a deck-quality bug,
not a stylistic preference.

## Format-name gotcha (read this once)

Scryfall's `legalities.brawl` field = **Historic Brawl on Arena** (100-card
singleton with commander). Scryfall's `standardbrawl` = the smaller
Standard-pool variant. So when validating Historic Brawl decks, pass
`-f brawl`, not `-f historicbrawl`.

Other gotchas — A- prefixed cards, Arena name-resolution, color identity,
banned-as-commander — are documented in `docs/gotchas.md`.

## Layout

```
mtg/
├── CLAUDE.md           ← you are here
├── flake.nix .envrc    ← reproducible dev shell (python3 + uv + jq + curl + dotnet-sdk_8 + util-linux)
├── tools/mtg{,.py}     ← the CLI
├── data/               ← gitignored: bulk JSON + pickled index
├── docs/
│   ├── formats.md      ← Arena format quick-reference
│   ├── gotchas.md      ← edge cases that bite
│   └── sources.md      ← curated meta URLs per format
└── decks/              ← deck files in MTGA export format
    ├── nadu/v0.txt     ← legal Historic Brawl ref
    └── hei-bai/v1.txt  ← legal Historic Brawl ref (5C, current meta)
```

## Workflow for building a new deck

0. **Recency check (mandatory).** Run
   `tools/mtg search 'legal:<fmt> game:arena date>=<~6mo ago>'` and skim
   the results. You don't know cards printed after your training cutoff;
   this is how you find them. See "Training-cutoff rule" above.
1. **Shell enumeration (mandatory).** Before drafting, name 2–3 candidate
   archetypes that fit the prompt — e.g. for "mono-B Pioneer with
   Lecturing Scornmage", that's *Aggro / Annex-Demons / Sacrifice*. Pitch
   each in one sentence with its CA source and primary win condition.
   Pick one and say *why* it beats the others vs the current meta. This
   step is the difference between "I built one shell from memory" and a
   considered choice. Skipping it = the deck is the prompt's anchor card,
   not the best deck.
2. **Mechanic sweep.** If the prompt's anchor card has a named keyword
   (Blitz, Survival, Repartee, Squad, etc.), run
   `tools/mtg related "<anchor>" -f <fmt>` to enumerate sister cards.
   Many synergy plays cluster in the same set; missing the cluster is a
   common failure mode.
3. Pick a commander (`tools/mtg search 'legal:brawl game:arena ...'`).
4. WebFetch a meta source from `docs/sources.md` for inspiration. Write
   down the format's current top-5 archetypes — your deck needs a one-
   line plan vs each. If you can't articulate the plan, the deck isn't
   ready.
5. Write `decks/<name>/v0.txt` in MTGA export format:
   ```
   Commander
   1 <Commander Name> (SET) NUM

   Deck
   1 <Card Name> (SET) NUM
   ...
   ```
6. `tools/mtg validate decks/<name>/v0.txt -f brawl` until clean.
7. `tools/mtg analyze decks/<name>/v0.txt` — read the diagnostics. Address
   every flagged issue or note explicitly why it's intentional.
8. Iterate: `v1.txt`, `v2.txt`, etc. Re-run analyze after each iteration.

## What NOT to do

- ❌ Don't trust EDHREC, mtgtop8, or any paper-only source for Arena legality.
- ❌ Don't use the magicthegathering.io API — stale, no Arena awareness.
- ❌ Don't add MTGJSON, scryfall-local MCP, or any other card DB. One
  source of truth: Scryfall.
- ❌ Don't add per-card API caching. Bulk download covers all validation.
- ❌ Don't extend the CLI before it's needed. YAGNI.
