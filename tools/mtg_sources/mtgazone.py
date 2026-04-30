"""mtgazone.com tier-list page parser.

The tier-list pages are the only mtgazone surface where decklists are
fully server-rendered (deck-article posts hide them behind a JS
`cardtooltip` widget). We scrape:

    https://mtgazone.com/<format>-bo1-metagame-tier-list/

per Arena format and emit one `ParsedDeck` per `<div class="deck-block">`.

Parse contract (verified against
`https://mtgazone.com/historic-bo1-metagame-tier-list/` on 2026-04-30):

* Tier sections are marked by an outer `<h2 …>` with an inner
  `<span id="Tier_<N>_Decks">Tier <N> Decks</span>`. Each subsequent
  `deck-block` until the next tier marker belongs to that tier.
* Each deck-block has:
    - `<div class="name">…</div>` containing
      `<archetype> by <author>` or `<archetype> from <source>`.
    - `<button class="btn copyurl stripped" data-info="https://mtgazone.com/user-decks/<id>/">`
      giving the canonical deep-link.
    - One or more `<div class="decklist main">`, optional
      `<div class="decklist sideboard">`, optional
      `<div class="decklist short companion">`.
    - Cards inside any decklist as `<div class="card …"
      data-quantity="N" data-name="X">…</div>`. `data-name` is the
      authoritative card name (HTML-entity encoded).

mtgazone does not publish per-deck winrate or sample size on tier-list
pages — both fields stay `None`. Tier letters map from the numeric
tiers via `_TIER_NUM_TO_LETTER` so the sidecar shape is consistent
across sources.

Cards are not given with `(SET) NUM` — just the name. We resolve each
name through the local Scryfall index (`resolve_name`) to fill
`set_code`/`collector` so the output matches the rest of the CLI's
DeckEntry contract. Resolution failures are reported up to the caller
as schema drift (see `parse_mtgazone` raising on zero entries).
"""

from __future__ import annotations

import html
import re
from typing import Callable

from ._common import DeckEntry, ParsedDeck, slugify

# mtgazone uses numeric tiers (1/2/3, occasionally 4/5). Roadmap sidecar
# locks tier to S/A/B/C/D letters. Stable mapping below.
_TIER_NUM_TO_LETTER = {
    "1": "S",
    "2": "A",
    "3": "B",
    "4": "C",
    "5": "D",
}

# Format -> tier-list URL slug. mtgazone exposes only BO1 tier lists for
# the constructed-Arena formats. Pioneer is published as `explorer-bo1`
# (the Arena-native equivalent), so the alias is built into the table.
_URL_TEMPLATES = {
    "standard": "https://mtgazone.com/standard-bo1-metagame-tier-list/",
    "alchemy": "https://mtgazone.com/alchemy-bo1-metagame-tier-list/",
    "historic": "https://mtgazone.com/historic-bo1-metagame-tier-list/",
    "timeless": "https://mtgazone.com/timeless-bo1-metagame-tier-list/",
    "explorer": "https://mtgazone.com/explorer-bo1-metagame-tier-list/",
    # Pioneer on Arena = Explorer; same page.
    "pioneer": "https://mtgazone.com/explorer-bo1-metagame-tier-list/",
}


def url_for_format(fmt: str) -> str | None:
    """URL of mtgazone's tier-list page for `fmt`, or None if unsupported.

    Brawl / standardbrawl are not served by mtgazone tier lists; caller
    must hard-fail with an explicit message when those are requested.
    """
    return _URL_TEMPLATES.get(fmt)


# --- HTML region carving --------------------------------------------------

# Outer tier marker. Two flavours observed across mtgazone tier-list
# pages (verified 2026-04-30):
#   <span id="Tier_2_Decks">…</span>   (historic, alchemy, timeless)
#   <span id="Tier_2">…</span>         (standard, explorer)
# `Tier_List_Disclaimer` etc are filtered out by requiring a digit
# immediately after the underscore.
_TIER_MARKER_RE = re.compile(
    r'<span\s+id="Tier_(\d+)(?:_Decks)?"', re.IGNORECASE,
)

# Each deck-block opens with `<div class="deck-block" id="uuid-<id>">` and
# closes when the *next* deck-block opens or the page section ends. We
# don't need to balance every nested div: the structure has stable inner
# anchors (`name`, `decklist`, `data-quantity`) we can pluck individually.
_DECK_BLOCK_RE = re.compile(
    r'<div\s+class="deck-block"\s+id="uuid-([A-Za-z0-9_-]+)"',
    re.IGNORECASE,
)

# Deep-link to the per-deck page on mtgazone.
_COPY_URL_RE = re.compile(
    r'<button[^>]*class="btn\s+copyurl\s+stripped"[^>]*'
    r'data-info="(https://mtgazone\.com/user-decks/[^"]+)"',
    re.IGNORECASE,
)

# Archetype display name. `(?P<n>...)` is the *first* `<div class="name">`
# inside the block — the only one that matches inside `name-container`.
_NAME_RE = re.compile(
    r'<div\s+class="name">\s*(?P<n>.+?)\s*</div>',
    re.IGNORECASE | re.DOTALL,
)

# Decklist section opener. `class="decklist main"`, `decklist sideboard`,
# or `decklist short companion`. The trailing word(s) decide the section.
_DECKLIST_OPEN_RE = re.compile(
    r'<div\s+class="decklist\s+([^"]+)"',
    re.IGNORECASE,
)

# Per-card record. `data-quantity` and `data-name` are stable across
# every section; we don't need the inner `<a>`.
_CARD_RE = re.compile(
    r'<div\s+class="card[^"]*"\s*\n?\s*data-quantity="(\d+)"\s*\n?\s*'
    r'data-name="([^"]+)"',
    re.IGNORECASE,
)


def _section_for_decklist_class(cls: str) -> str | None:
    """Map mtgazone's decklist-class flavour to our DeckEntry.section.

    Observed flavours (verified 2026-04-30 across all five Arena BO1
    tier-list pages): `main`, `sideboard`, `sideboard info`, `short
    companion`.

    `main` -> 'deck', `sideboard` -> 'sideboard',
    `short companion` -> 'companion', and `sideboard info` -> None
    because that container holds a stats blurb (`<h5>60 Cards<br>$464</h5>`)
    not cards. Returning None tells the entry-extractor to skip the
    block entirely; raising would treat a known-empty stat container
    as schema drift.

    Anything else is unknown and the caller must hard-fail (silent
    miscategorisation = wrong validate / coverage answers downstream).
    """
    flavour = cls.strip().lower()
    if flavour == "main":
        return "deck"
    if flavour == "sideboard":
        return "sideboard"
    if flavour == "sideboard info":
        return None
    if "companion" in flavour:
        return "companion"
    raise ValueError(f"unknown decklist class: {cls!r}")


def _strip_attribution(name: str) -> str:
    """Trim trailing `by <author>` / `from <source>` from archetype name.

    mtgazone displays `<archetype> by <author>` on user submissions and
    `<archetype> from Untapped` on imported decks. Strip those so the
    sidecar `archetype` field is the deck name only — the slug derived
    from it then groups versions of the same archetype together.
    """
    n = name.strip()
    for sep in (" by ", " from "):
        idx = n.lower().rfind(sep)
        if idx > 0:
            return n[:idx].strip()
    return n


# --- main entry point -----------------------------------------------------


def parse_mtgazone(
    raw_html: str,
    fmt: str,
    *,
    fetched: str,
    url: str,
    resolve_name: Callable[[str], dict | None],
) -> list[ParsedDeck]:
    """Parse a mtgazone tier-list page into a list of `ParsedDeck`.

    `resolve_name(name) -> printing dict | None` is injected so this
    module stays free of CLI-internal index plumbing — `tools/mtg.py`
    passes its own `_resolve_card`. Each card's `set_code`/`collector`
    come from the resolved printing; if a card cannot be resolved on
    Scryfall we drop it from the deck and the deck reports zero entries
    for that section, which the caller treats as schema drift and fails.

    Hard-fail conditions (caller catches `ValueError` and exits 1):
      * unknown decklist class encountered;
      * any tier-letter outside `_TIER_NUM_TO_LETTER`;
      * no tier markers AND no deck-blocks found (drift / blocked page).

    Empty result list is *not* an error here — caller decides whether
    zero decks for this URL means the page is genuinely empty (rare) or
    that the parser broke (frequent and serious). Hard-fail is enforced
    one layer up (`cmd_fetch_meta`) where total deck count is known.
    """
    decks: list[ParsedDeck] = []

    # Build (offset -> tier_letter) map. Walk left of each deck-block and
    # use the most recent tier marker.
    tier_marks: list[tuple[int, str]] = []
    for m in _TIER_MARKER_RE.finditer(raw_html):
        num = m.group(1)
        letter = _TIER_NUM_TO_LETTER.get(num)
        if letter is None:
            raise ValueError(
                f"mtgazone: unknown tier number {num!r} at offset {m.start()}"
            )
        tier_marks.append((m.start(), letter))

    block_starts = [m.start() for m in _DECK_BLOCK_RE.finditer(raw_html)]
    if not block_starts:
        return decks

    # Append a sentinel offset so each block's slice ends at the next
    # block (or end-of-document for the last one).
    block_bounds = block_starts + [len(raw_html)]

    seen_slugs: dict[str, int] = {}

    for i, start in enumerate(block_starts):
        end = block_bounds[i + 1]
        body = raw_html[start:end]

        # Tier: most recent marker before this block.
        tier_letter = ""
        for m_off, letter in tier_marks:
            if m_off < start:
                tier_letter = letter
            else:
                break

        name_m = _NAME_RE.search(body)
        if not name_m:
            # Source drift: every deck-block should have a name. Skip
            # silently here; cmd_fetch_meta hard-fails on zero decks.
            continue
        raw_name = html.unescape(name_m.group("n")).strip()
        archetype = _strip_attribution(raw_name)
        if not archetype:
            continue

        url_m = _COPY_URL_RE.search(body)
        deck_url = url_m.group(1) if url_m else url

        slug_base = slugify(archetype)
        # Two decks can share an archetype name (`Mardu Sacrifice` from
        # different authors). Disambiguate by appending `-2`, `-3`, ...
        n = seen_slugs.get(slug_base, 0) + 1
        seen_slugs[slug_base] = n
        slug = slug_base if n == 1 else f"{slug_base}-{n}"

        entries = _entries_from_block(body, resolve_name)
        if not entries:
            # Drift: deck-block with name but no parseable cards.
            continue

        decks.append(ParsedDeck(
            slug=slug,
            archetype=archetype,
            source="mtgazone",
            url=deck_url,
            tier=tier_letter,
            winrate=None,  # not published on tier-list pages
            sample=None,
            fetched=fetched,
            entries=entries,
        ))

    return decks


def _entries_from_block(
    body: str, resolve_name: Callable[[str], dict | None],
) -> list[DeckEntry]:
    """Extract DeckEntry list for one deck-block.

    Walks each `<div class="decklist …">` opener in order, slices to the
    next opener, extracts cards inside that slice. Section is derived
    from the class flavour. Cards whose names don't resolve to a
    Scryfall printing are dropped — we'd otherwise emit a deck file
    that fails `validate`. Drop count is implicit; cmd_fetch_meta
    surfaces zero-deck outcomes as schema drift.
    """
    out: list[DeckEntry] = []
    openers = list(_DECKLIST_OPEN_RE.finditer(body))
    if not openers:
        return out

    bounds = [m.start() for m in openers] + [len(body)]
    for i, m in enumerate(openers):
        section = _section_for_decklist_class(m.group(1))
        if section is None:
            # info / stats container — no cards in here.
            continue
        section_body = body[bounds[i]:bounds[i + 1]]
        for card_m in _CARD_RE.finditer(section_body):
            count = int(card_m.group(1))
            name = html.unescape(card_m.group(2)).strip()
            if not name or count <= 0:
                continue
            printing = resolve_name(name)
            if printing is None:
                # Cannot fill set/collector — skip rather than emit a
                # deck-line MTGA would reject on import.
                continue
            set_code = (printing.get("set") or "").upper()
            collector = printing.get("collector_number") or ""
            if not set_code or not collector:
                continue
            out.append(DeckEntry(
                count=count,
                name=name,
                set_code=set_code,
                collector=collector,
                section=section,
            ))
    return out
