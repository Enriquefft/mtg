"""Shared deck-parsing primitives.

Lifted out of `tools/mtg.py` so per-source parsers (untapped, mtgazone,
mtggoldfish, ...) can produce `DeckEntry` lists the rest of the CLI
already knows how to validate, write, and analyse — without each parser
re-deriving regex / section / multi-face rules. Single source of truth.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Single source of truth for the toolkit's outbound User-Agent. Used by
# `tools/mtg.py` (Scryfall JSON) and by per-source parsers fetching
# sub-resources (e.g. mtggoldfish per-archetype pages). One constant so
# rotating identity / version is one edit.
USER_AGENT = "mtg-toolkit/0.1 (github.com/Enriquefft/mtg)"


def http_get_text(
    url: str,
    *,
    accept: str = "text/html,application/xhtml+xml",
    retry_403_once: bool = False,
    retry_sleep_secs: float = 2.0,
) -> str:
    """Fetch `url` as text using the shared User-Agent.

    Stdlib-only thin wrapper. Exists so per-source parsers that need
    sub-resource HTTP (mtggoldfish per-archetype pages) don't import
    back into `tools/mtg.py` (circular) and don't grow a parallel HTTP
    stack with a different UA / timeout policy.

    `retry_403_once`: per `docs/sources.md` mtggoldfish "occasionally
    403s; retry once". When True, a single retry with `retry_sleep_secs`
    delay is attempted on the first 403; any second 403 (or any other
    non-200) re-raises so the caller hard-fails per the production-
    ready floor.
    """
    try:
        return _do_http_get(url, accept=accept)
    except urllib.error.HTTPError as e:
        if retry_403_once and e.code == 403:
            time.sleep(retry_sleep_secs)
            return _do_http_get(url, accept=accept)
        raise


def _do_http_get(url: str, *, accept: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return raw.decode("utf-8", errors="replace")

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
    `unresolved` count of card lines the source listed but that did not
               resolve to a Scryfall printing — surfaced through the
               sidecar so a deck imported short (e.g. 56/60) is visible
               instead of silently corrupted. Per-card stderr would be
               noisy across a 30-deck fetch; one integer is enough.
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
    unresolved: int = 0


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, hyphenated, ASCII-only filename stem.

    Collapses every non-alnum run to a single hyphen, strips leading /
    trailing hyphens, returns at least `deck` for empty input. Stable
    across runs so sidecar `meta.json` keyed by filename merges cleanly.
    """
    s = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return s or "deck"
