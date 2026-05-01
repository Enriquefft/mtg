# MTG Arena deck-building toolkit

Groundwork for building MTG Arena decks. Lets a Claude session answer
"is this card legal in Historic Brawl", "what does this card do", and
"validate my deck list" without hitting rate limits or trusting
outdated internet info.

## Hard rule: `data/` is CLI-only

`data/` holds the ~500 MB Scryfall bulk dump, an 80 MB pickled index,
and the collection snapshot — reading any into context blows the
window. **Never `Read`/`Glob`/`Grep`/`cat`/`head`/`tail`/`jq` files in
`data/`.** `.claude/settings.json` enforces this with a deny rule; the
only sanctioned path is `tools/mtg`. If a query isn't covered by a
subcommand, add one. `python -c "open('data/...')"` and shell
redirects (`cmd < data/foo`) bypass the deny list — don't use them.

## Quick start

```bash
nix develop                                     # or: direnv allow
cd /home/hybridz/Projects/mtg
tools/mtg sync                                  # one-time, ~30s download
tools/mtg validate decks/nadu/v0.txt -f brawl   # offline validation
```

## How to use it

1. **Run `tools/mtg sync` once per session** — no-op if Scryfall hasn't
   republished. Warns if the cache is >36h old.

2. **For card lookups** (instant, offline, no rate budget):
   ```bash
   tools/mtg card "Hei Bai, Forest Guardian"
   tools/mtg legal "A-Nadu, Winged Wisdom" brawl
   tools/mtg printing MH3 193
   ```

3. **For deck validation** (always before "done"):
   ```bash
   tools/mtg validate decks/<name>/<version>.txt -f brawl
   ```
   Exit 0 = clean. Checks arena, legality, size, singleton, CI, commander.

4. **For composition analysis**, after each version:
   ```bash
   tools/mtg analyze decks/<name>/<version>.txt
   ```
   Type mix, function tags (removal/sweeper/counter/hand attack/peek/
   CA/loot/tutor/ramp/recursion/threat), curve, per-card table.
   Sideboard: `--include-sideboard` / `--sideboard-only`.

5. **For comparing two deck versions**:
   ```bash
   tools/mtg diff decks/<name>/v0.txt decks/<name>/v1.txt
   ```
   Per-card added / removed / delta between two MTGA-export files.

6. **For sister-card discovery** when the anchor uses a named mechanic:
   ```bash
   tools/mtg related "<card>" -f <format>
   ```
   Every Arena-legal card sharing each keyword (rarer first). Use
   BEFORE drafting around a unique card.

7. **For manabase / wildcard / companion checks**, every version:
   ```bash
   tools/mtg manabase  decks/<name>/<version>.txt   # pip demand + sources + etb-tapped
   tools/mtg wildcards decks/<name>/<version>.txt   # rarity counts (MTGA WC cost)
   tools/mtg companion decks/<name>/<version>.txt   # eligibility per companion
   ```
   - One-shot: `tools/mtg check decks/<name>/<version>.txt -f brawl` runs
     validate + analyze + manabase + wildcards + companion in one go (add
     `--collection` to also run `gaps`).

8. **For ad-hoc commander/staple discovery** (live Scryfall):
   ```bash
   tools/mtg search 'legal:brawl game:arena t:legendary t:creature c=5'
   ```
   Full syntax: https://scryfall.com/docs/syntax

9. **For meta info**, open `docs/sources.md` — per-format URLs with
   last-verified dates. WebFetch when you need numbers.

10. **For collection-aware decisions** (own / WC cost), populate
    `data/collection.json` once via `tools/mtg collection {dump,import,from-decks}`,
    then query: `collection`, `own <name>`, `owned '<scryfall query>'`,
    `gaps <deck>`, `coverage <deck>`,
    `wantlist [--latest-only] [--decks 'decks/foo/*.txt']`. `dump` injects
    a .NET payload (`tools/inject/`, one-time `dotnet build -c Release`)
    into MTGA's Mono runtime (Linux/Proton + Nix shell); `from-decks` is
    a lower bound; `wantlist` is max-shortfall across saved decks.

11. **For batch ownership ranking** across a deck directory:
    ```bash
    tools/mtg coverage --batch --glob 'decks/<fmt>/*.txt' --with-subs --min 0.90 --json
    ```
    Sorts by ownership %; `--with-subs` factors in `suggest-subs` rewrites.
    `--json` works in single-deck and batch mode.

12. **For pulling a meta corpus** into the repo:
    ```bash
    tools/mtg fetch-meta historic --out decks/historic/ --limit 30
    ```
    Sources: `mtgazone`, `mtggoldfish`, `mtgdecks`, `untapped` (the only
    automated Brawl source — `--source untapped brawl`; also covers
    `historic`/`standard`/`pioneer`/`alchemy`/`timeless` with much larger
    samples than mtgazone). See `docs/sources.md`.
    Add `--no-cache` to bypass `data/meta-cache/`, `--json` for stdout output.

13. **For owned-card replacements** preserving role/CMC/CI/companion:
    ```bash
    tools/mtg suggest-subs decks/<name>/v0.txt -f brawl --max-per-card 5
    tools/mtg suggest-subs decks/<name>/v0.txt -f brawl --apply decks/<name>/v0-subbed.txt
    ```
    Consults strictlybetter.eu for functional reprints + community-validated
    direct downgrades; owned matches outrank every heuristic candidate
    and are tagged `[strictlybetter]` in text output (`strictlybetter:
    true` in `--json`). First run with strictlybetter enabled performs
    a one-time ~10-minute bulk fetch of the obsoletes corpus into
    `data/strictlybetter-cache.json` (7d TTL); thereafter lookups are
    in-memory. Add `--no-strictlybetter` for offline / network-failure
    mode (skips the bulk fetch entirely).

14. **For novel-deck shell discovery** (cluster owned cards):
    ```bash
    tools/mtg shells --format historic --by keyword
    tools/mtg shells --format historic --by type --min-cards 20
    tools/mtg shells --format historic --by theme
    ```

## Training-cutoff rule (read this BEFORE drafting any cardlist)

**Card knowledge is frozen at your training cutoff; the dev shell's
date is real-time.** Sets printed after your cutoff exist on Scryfall
but you haven't memorised them. Recency check before drafting:

```bash
tools/mtg search 'legal:<fmt> game:arena date>=2025-10-01'
tools/mtg search 'legal:pioneer game:arena date>=2025-10-01 c<=B t:creature'
tools/mtg search 'legal:pioneer game:arena date>=2025-10-01 o:"destroy target"'
```

Use a date ≈ 3 months *before* your cutoff (sets near it are under-
represented in training). Skim, then `tools/mtg card "<name>"` for
oracle text. Not optional — "built from memory" is a quality bug.

## Format-name gotcha

See `docs/historic.md` §"Format-name gotcha". TL;DR: `-f brawl` = Historic Brawl, not `-f historicbrawl`.
Other format edge cases live in `docs/gotchas.md`.

## Layout

`tools/mtg{,.py}` (CLI), `data/` (gitignored bulk + index + collection),
`docs/` (formats / gotchas / sources / historic / roadmap), `decks/`
(MTGA-export files), `flake.nix` + `.envrc` (Nix dev shell: python3 + uv
+ jq + curl + dotnet-sdk_8 + util-linux).

## Workflow for building a new deck

Format-specific workflows live in `docs/historic.md` (Brawl + Historic constructed)
and `docs/formats.md` (other Arena formats). Recency check + shell enumeration +
mechanic sweep apply across all formats — see those docs for the full per-format procedure.

## What NOT to do

- Don't trust EDHREC, mtgtop8, or any paper-only source for Arena legality.
- Don't use magicthegathering.io — stale, no Arena awareness.
- Don't add MTGJSON / scryfall-local MCP / any other card DB. One source of truth: Scryfall.
- Don't add per-card API caching. Bulk covers all validation.
- Don't extend the CLI before it's needed. YAGNI.

## Further reading

- `docs/historic.md` — Historic & Historic Brawl: format gotchas, wildcards, shells, workflows
- `docs/formats.md`  — All Arena formats, Scryfall legality keys
- `docs/gotchas.md`  — A- prefix, multi-face ` // `, color identity, game_changer, bulk freshness
- `docs/sources.md`  — Curated meta URLs, bot-block reality table
- `docs/roadmap.md`  — CLI roadmap & invariants
