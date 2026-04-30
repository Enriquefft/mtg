"""Shared deck-parsing primitives.

Lifted out of `tools/mtg.py` so per-source parsers (untapped, mtgazone,
mtggoldfish, ...) can produce `DeckEntry` lists the rest of the CLI
already knows how to validate, write, and analyse — without each parser
re-deriving regex / section / multi-face rules. Single source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# MTGA export deck-line: `<count> <Name> (<SET>) <NUM>`. The set code is
# alphanumeric (Scryfall codes like `MH3`, `Y25`, `LTC`); collector
# numbers can contain letters / `-` (`MH3-193*`, `316★`), so accept any
# non-space run for that field.
DECK_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(.+?)\s+\(([A-Za-z0-9]+)\)\s+(\S+)\s*$"
)

# Section headers MTGA's own export emits, plus `maybeboard` which some
# external tools (Moxfield, mtgazone) emit and which we tolerate without
# treating as part of the deck for validation purposes.
SECTION_HEADERS = {"deck", "commander", "companion", "sideboard", "maybeboard"}

# Layouts whose Scryfall `name` is `Front // Back`. MTGA's deck importer
# rejects deck-lines that use only the front face for these — even though
# Scryfall happily resolves either spelling. Source for layout list:
# https://scryfall.com/docs/api/layouts
MULTIFACE_LAYOUTS = frozenset({
    "split",
    "adventure",
    "modal_dfc",
    "transform",
    "flip",
})


@dataclass
class DeckEntry:
    """One MTGA deck-line: `<count> <name> (<set>) <collector>` in <section>."""

    count: int
    name: str
    set_code: str
    collector: str
    section: str  # 'commander' | 'deck' | 'sideboard' | 'companion' | 'maybeboard'


@dataclass
class ParsedDeck:
    """One archetype scraped from a meta source.

    `slug`     filename-safe stem (no extension); becomes `<slug>.txt`.
    `archetype` human-readable name as displayed on the source page.
    `source`   short host token (`mtgazone`, `untapped`, ...).
    `url`      canonical deep-link to this deck on the source.
    `tier`     normalised letter (S/A/B/C/D) or `""` if absent.
    `winrate`  fraction in [0,1] or None if the source doesn't publish it.
    `sample`   match-sample size or None.
    `fetched`  ISO date (YYYY-MM-DD) the page was scraped.
    `entries`  list of `DeckEntry` in source order; commander/sideboard
               sections set via `DeckEntry.section`.
    """

    slug: str
    archetype: str
    source: str
    url: str
    tier: str
    winrate: float | None
    sample: int | None
    fetched: str
    entries: list[DeckEntry] = field(default_factory=list)


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, hyphenated, ASCII-only filename stem.

    Collapses every non-alnum run to a single hyphen, strips leading /
    trailing hyphens, returns at least `deck` for empty input. Stable
    across runs so sidecar `meta.json` keyed by filename merges cleanly.
    """
    s = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return s or "deck"
