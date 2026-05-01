"""Shared deck-parsing primitives.

Lifted out of `tools/mtg.py` so per-source parsers (untapped, mtgazone,
mtggoldfish, ...) can produce `DeckEntry` lists the rest of the CLI
already knows how to validate, write, and analyse — without each parser
re-deriving regex / section / multi-face rules. Single source of truth.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

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
    referer: str | None = None,
    user_agent: str | None = None,
    extra_headers: dict[str, str] | None = None,
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

    `referer`: optional `Referer` header. Required by mtgdecks.net deck
    pages per the source's spec (probe shows they 200 without it today,
    but sending the header keeps us inside the documented contract and
    avoids surprises if the server hardens). Threaded through both the
    initial fetch and the retry so the second attempt looks identical.

    `user_agent`: override the shared User-Agent for this call. Some
    sources (moxfield, aetherhub) refuse the toolkit UA and require a
    browser-like string. Default = `USER_AGENT`.

    `extra_headers`: optional dict merged into the request headers
    after Accept/User-Agent/Referer. Used by JSON APIs that demand
    `Origin` (moxfield) or other custom headers without making each
    one a named keyword.
    """
    try:
        return _do_http_get(
            url, accept=accept, referer=referer,
            user_agent=user_agent, extra_headers=extra_headers,
        )
    except urllib.error.HTTPError as e:
        if retry_403_once and e.code == 403:
            time.sleep(retry_sleep_secs)
            return _do_http_get(
                url, accept=accept, referer=referer,
                user_agent=user_agent, extra_headers=extra_headers,
            )
        raise


def _do_http_get(
    url: str,
    *,
    accept: str,
    referer: str | None = None,
    user_agent: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    headers = {"User-Agent": user_agent or USER_AGENT, "Accept": accept}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
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
    # Cross-source dedup back-pointers. Populated by `dedup_decks` when a
    # lower-priority duplicate is collapsed into this entry. Empty for
    # decks seen in only one source. Surfaces in the sidecar so a later
    # session can see "same list also lives at <urls>".
    also_seen_at: list[str] = field(default_factory=list)


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, hyphenated, ASCII-only filename stem.

    Collapses every non-alnum run to a single hyphen, strips leading /
    trailing hyphens, returns at least `deck` for empty input. Stable
    across runs so sidecar `meta.json` keyed by filename merges cleanly.
    """
    s = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return s or "deck"


# Source-priority ranking for `dedup_decks`. Lower index = higher priority
# = winner when two sources publish the same multiset. Order rationale:
#   * untapped — Arena-native, all formats, the only automated brawl source.
#   * moxfield — largest user-built corpus on the open web, brawl king.
#   * archidekt — second-largest user-built; format coverage parity.
#   * aetherhub — Arena-native w/ winrates, smaller volume.
#   * mtgazone / mtggoldfish / mtgdecks — legacy curated/paper-tilted.
# New parsers should be inserted at their evidence-supported position;
# this is one edit, not a per-call argument, because dedup must be
# deterministic across all `cmd_fetch_meta` invocations.
SOURCE_PRIORITY: tuple[str, ...] = (
    "untapped",
    "moxfield",
    "archidekt",
    "aetherhub",
    "mtgazone",
    "mtggoldfish",
    "mtgdecks",
)


def _source_rank(source: str) -> int:
    """Index into SOURCE_PRIORITY; unknown sources sort last (=most demoted)."""
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def cards_hash(deck: ParsedDeck) -> str:
    """Stable identity hash for cross-source dedup.

    Identity = sorted multiset of `(name, count)` over main-deck +
    commander + companion entries, EXCLUDING basic lands (so two
    archetypes that differ only in basic-land count collapse together —
    the deck plan is the same; the manabase is a tuning detail).
    Sideboard ignored: same deck across two formats can have different
    sideboards yet be the same archetype.

    Returns SHA-1 hex digest (12 chars sufficient for ~10⁶ corpus
    without practical collision risk — full 40 stored for safety).
    Returns "" if the deck has zero comparable entries (deck file
    likely corrupt; caller treats as no-collision).
    """
    pairs: dict[str, int] = {}
    for e in deck.entries:
        if e.section not in {"deck", "commander", "companion"}:
            continue
        # Names that include `// ` (multi-face) keep the full name —
        # collisions need the same printing, not the front-face only.
        pairs[e.name] = pairs.get(e.name, 0) + e.count
    # Basic-land filter is name-based (cheap, no resolve_name needed):
    # the five Arena basics + Wastes + Snow-Covered variants. Any other
    # land (Treasure Vault, City of Brass, ...) stays in the hash.
    for basic in (
        "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
        "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
        "Snow-Covered Mountain", "Snow-Covered Forest",
    ):
        pairs.pop(basic, None)
    if not pairs:
        return ""
    payload = "|".join(f"{n}\x1f{c}" for n, c in sorted(pairs.items()))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def is_stub_deck(
    deck: ParsedDeck,
    resolve_name: Callable[[str], dict | None],
) -> bool:
    """True if `deck` is a basic-land padded placeholder, not a real list.

    Two-signal conjunction (both must hold):
      * `unique_nonlands < 15` — real constructed has >=15 unique nonlands;
      * `max_basic_share >= 0.5` — and never a single basic >=50% of deck.

    Real mono-color brews have <15 unique nonlands too, but never a
    single basic land at half the deck. Real sealed/limited has a tall
    basic count but >=15 unique nonlands. Conjunction catches the stub
    pattern (commander + 5 nonlands + 94 Mountains) without false-
    positiving real decks.

    Originally inlined in untapped.py for the brawl `laelia-the-blade-
    reforged` pattern; generalised here so every parser benefits.
    """
    unique_nonlands = 0
    deck_total = 0
    max_basic = 0
    for e in deck.entries:
        if e.section != "deck":
            continue
        deck_total += e.count
        printing = resolve_name(e.name)
        if printing is None:
            continue
        type_line = printing.get("type_line") or ""
        if "Land" not in type_line:
            unique_nonlands += 1
            continue
        if "Basic" in type_line and e.count > max_basic:
            max_basic = e.count
    basic_share = (max_basic / deck_total) if deck_total else 0.0
    return unique_nonlands < 15 and basic_share >= 0.5


def dedup_decks(
    decks: list[ParsedDeck],
    *,
    existing_hashes: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[ParsedDeck], list[ParsedDeck], list[str]]:
    """Cross-source dedup by `cards_hash` identity.

    Within `decks`, when two entries share a hash, keep the one whose
    source has higher SOURCE_PRIORITY (lower index). The loser's `url`
    is appended to the winner's `also_seen_at`.

    `existing_hashes` (optional) maps `cards_hash → (source, slug)` for
    decks already on disk in the same corpus dir. A fresh deck colliding
    with an existing on-disk entry:
      * loses (gets dropped, existing stays) if existing has higher priority;
      * wins (kept, existing's slug returned for caller to unlink) otherwise.

    Returns `(kept, dropped_fresh, evicted_existing_slugs)`:
      * `kept` — fresh decks to write to disk;
      * `dropped_fresh` — fresh decks collapsed away (intra-batch losers
        and on-disk-existing wins);
      * `evicted_existing_slugs` — on-disk slugs the caller must unlink
        (and prune from sidecar) because a fresh higher-priority source
        beat them.
    """
    by_hash: dict[str, ParsedDeck] = {}
    dropped: list[ParsedDeck] = []
    evicted: list[str] = []
    existing_hashes = existing_hashes or {}

    for deck in decks:
        h = cards_hash(deck)
        if not h:
            # Hashless deck (no comparable entries): keep as-is, can't dedup.
            by_hash[f"_no_hash_{id(deck)}"] = deck
            continue

        existing = by_hash.get(h)
        if existing is not None:
            winner, loser = (
                (existing, deck)
                if _source_rank(existing.source) <= _source_rank(deck.source)
                else (deck, existing)
            )
            if loser.url and loser.url not in winner.also_seen_at:
                winner.also_seen_at.append(loser.url)
            by_hash[h] = winner
            dropped.append(loser)
            continue

        prior = existing_hashes.get(h)
        if prior is not None:
            prior_source, prior_slug = prior
            if _source_rank(prior_source) <= _source_rank(deck.source):
                # Existing on disk wins; drop this fresh entry.
                dropped.append(deck)
                continue
            # Fresh entry wins; existing slug needs eviction by caller.
            evicted.append(prior_slug)

        by_hash[h] = deck

    return list(by_hash.values()), dropped, evicted
