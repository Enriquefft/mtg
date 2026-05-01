"""aetherhub.com metagame parser.

Aetherhub is Arena-native and publishes per-deck winrate + match
counts, which moxfield/archidekt do not. We trade volume for signal:
the Metagame index lists ~50 archetypes per format and each links to
one canonical deck page.

Flow:

  1. Fetch `https://aetherhub.com/Metagame/<format-slug>/`. The page
     embeds 50 archetype links of the form
     `/Metagame/<fmt>/Deck/<slug>-<id>`. We rewrite each to
     `/Deck/<slug>-<id>` (the actual single-deck URL — the Metagame
     route is a card-breakdown view, not the deck itself).

  2. For each deck URL, fetch the page. The deck list lives inside
     `<div class="hover-imglink">N <a class="cardLink"
     data-card-name="..." data-card-set="..." data-card-number="...">`
     blocks. Section is determined by the most-recent `<h5>Commander
     N cards (...)</h5>` / `<h5>Main N cards (...)</h5>` /
     `<h5>Sideboard N cards (...)</h5>` heading.

  3. Winrate + sample come from `<h5 class="mb-0">P% Win Rate: W
     Wins - L Losses</h5>` near the page header.

Cloudflare rate-limiting: the deck pages 200 with a vanilla browser
UA at 0.5s throttle; faster bursts can trigger the JS challenge page.
We sleep `_PER_DECK_THROTTLE_SECS` between fetches.

Aetherhub serves up to 50 archetypes per format on the Metagame
index — the parser caps at that cap. To get a richer corpus combine
with moxfield (volume) + untapped (brawl breadth).
"""

from __future__ import annotations

import html as html_mod
import re
import time
import urllib.error
from typing import Callable

from ._common import DeckEntry, ParsedDeck, http_get_text, slugify

_HOST = "https://aetherhub.com"
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_PER_DECK_THROTTLE_SECS = 0.5

# Format slug as it appears in the aetherhub URL path. Pioneer is
# served from `/Metagame/Pioneer/` (paper-tilted but Arena-applicable);
# Explorer-BO1 is the Arena-native equivalent and offers richer
# coverage, so when the user asks for `pioneer` we send them to
# Explorer-BO1 (mirrors mtgazone's pioneer→explorer aliasing).
_FORMAT_SLUG = {
    "standard": "Standard-BO1",
    "alchemy": "Alchemy-BO1",
    "historic": "Historic-BO1",
    "timeless": "Timeless-BO1",
    "pioneer": "Explorer-BO1",
    "brawl": "Historic-Brawl",
    # standardbrawl deliberately omitted — aetherhub publishes <10
    # decks under /Metagame/Brawl/, not enough to be worth the fan-out.
}

# Archetype links on /Metagame/<fmt>/. We rewrite to /Deck/<slug-id>
# at fetch time; both forms share the canonical {slug}-{id} suffix.
_ARCHETYPE_LINK_RE = re.compile(
    r'/Metagame/[A-Za-z0-9-]+/Deck/([a-z0-9-]+-\d+)',
)

# Per-card row inside the deck-list region. The `<div
# class="hover-imglink">` wrapper holds a literal count followed by an
# `<a class="cardLink"` carrying name/set/number data attrs. Rendered
# once per card; sidebar tooltips and modal previews use a different
# HTML shape (no leading count) so this regex naturally excludes them.
_CARD_ROW_RE = re.compile(
    r'<div\s+class="hover-imglink">\s*(\d+)\s*'
    r'<a[^>]*class="cardLink"[^>]*'
    r'data-card-name="([^"]+)"[^>]*'
    r'data-card-set="([^"]+)"[^>]*'
    r'data-card-number="([^"]+)"',
    re.DOTALL,
)

# Section headers. `<h5>Commander N cards (M distinct)</h5>`,
# `<h5>Main N cards (...)</h5>`, `<h5>Sideboard N cards (...)</h5>`.
# We anchor on the leading word to keep the regex stable across the
# parenthesised distinct-count drift.
_SECTION_HEADER_RE = re.compile(
    r'<h5(?:\s+[^>]*)?>(Commander|Main|Sideboard|Companion)\s+\d+\s+cards',
    re.IGNORECASE,
)

# Winrate + sample. Stable across all aetherhub deck pages probed
# 2026-05-01. Pages without enough matches drop the `<h5>` entirely;
# we treat that as winrate=None / sample=None rather than 0/0.
_WINRATE_RE = re.compile(
    r'<h5[^>]*>(\d+)% Win Rate:\s*(\d+)\s*Wins\s*-\s*(\d+)\s*Losses</h5>',
)


def url_for_format(fmt: str) -> str | None:
    """URL of the aetherhub Metagame index for `fmt`, or None if unsupported."""
    slug = _FORMAT_SLUG.get(fmt)
    if slug is None:
        return None
    return f"{_HOST}/Metagame/{slug}/"


def _section_for_header(token: str) -> str:
    """Map an aetherhub section header word to our DeckEntry.section.

    `Commander` -> 'commander', `Main` -> 'deck', `Sideboard` ->
    'sideboard', `Companion` -> 'companion'. Defaults to 'deck' for
    any future header we haven't seen yet (silent default beats
    raising on minor copy changes; cmd_fetch_meta still hard-fails on
    zero-card decks, catching genuine drift).
    """
    t = token.strip().lower()
    if t == "commander":
        return "commander"
    if t == "sideboard":
        return "sideboard"
    if t == "companion":
        return "companion"
    return "deck"


def _http_get_html(url: str) -> str:
    return http_get_text(url, user_agent=_BROWSER_UA)


def _entries_from_deck_page(
    raw_html: str,
    resolve_name: Callable[[str], dict | None],
) -> tuple[list[DeckEntry], int]:
    """Walk a deck-page HTML body -> (DeckEntry list, dropped-copy count).

    Section assignment uses the most-recent `<h5>Section ...</h5>`
    marker before each card row. We rely on `_resolve_card` to map
    aetherhub's printing back to an Arena printing (the page often
    cites paper sets like `STA` / `MH3`; the resolver picks an Arena
    reprint when one exists).

    The same card name appears inside multiple page regions (deck
    list, archetype-card breakdown sidebar, modal). The card-row
    regex anchors on the `<div class="hover-imglink">` wrapper that
    only the deck-list rendering uses, so duplicates are not a
    concern — but as defence-in-depth we de-duplicate `(name,
    section)` pairs in case a future template change widens the hit.
    """
    out: list[DeckEntry] = []
    unresolved = 0

    section_marks: list[tuple[int, str]] = [
        (m.start(), _section_for_header(m.group(1)))
        for m in _SECTION_HEADER_RE.finditer(raw_html)
    ]

    seen: set[tuple[str, str]] = set()
    for m in _CARD_ROW_RE.finditer(raw_html):
        count = int(m.group(1))
        if count <= 0:
            continue
        name = html_mod.unescape(m.group(2)).strip()
        if not name:
            continue

        section = "deck"
        for off, sec in section_marks:
            if off < m.start():
                section = sec
            else:
                break

        key = (name, section)
        if key in seen:
            continue
        seen.add(key)

        printing = resolve_name(name)
        if printing is None:
            unresolved += count
            continue
        set_code = (printing.get("set") or "").upper()
        collector = printing.get("collector_number") or ""
        if not set_code or not collector:
            unresolved += count
            continue
        out.append(DeckEntry(
            count=count,
            name=name,
            set_code=set_code,
            collector=collector,
            section=section,
        ))
    return out, unresolved


def parse_aetherhub(
    raw_html: str,
    fmt: str,
    *,
    fetched: str,
    url: str,
    resolve_name: Callable[[str], dict | None],
    limit: int | None = None,
    **_: object,
) -> list[ParsedDeck]:
    """Parse the Metagame index + walk per-archetype deck pages.

    `raw_html` is the Metagame index page (already fetched by
    cmd_fetch_meta). We extract `slug-id` tokens from
    `/Metagame/<fmt>/Deck/<slug-id>` links, rewrite to `/Deck/<slug-id>`,
    and fetch each page directly via `_http_get_html` with
    `_PER_DECK_THROTTLE_SECS` of sleep between requests.

    Hard-fail conditions (raise ValueError; cmd_fetch_meta exits 1):
      * the index page yields zero archetype links — schema drift.

    Per-deck failures (HTTP non-200, zero parseable cards) are
    skipped silently; the caller hard-fails one layer up if the
    aggregate is empty.
    """
    if fmt not in _FORMAT_SLUG:
        return []

    archetype_ids: list[str] = []
    seen_ids: set[str] = set()
    for m in _ARCHETYPE_LINK_RE.finditer(raw_html):
        token = m.group(1)
        if token in seen_ids:
            continue
        seen_ids.add(token)
        archetype_ids.append(token)

    if not archetype_ids:
        raise ValueError(
            "aetherhub: Metagame index yielded 0 archetype links — drift?"
        )

    target = limit if (limit is not None and limit > 0) else len(archetype_ids)
    archetype_ids = archetype_ids[:target]

    decks: list[ParsedDeck] = []
    seen_slugs: dict[str, int] = {}

    for token in archetype_ids:
        deck_url = f"{_HOST}/Deck/{token}"
        try:
            page_html = _http_get_html(deck_url)
        except urllib.error.HTTPError:
            continue
        time.sleep(_PER_DECK_THROTTLE_SECS)

        entries, unresolved = _entries_from_deck_page(page_html, resolve_name)
        if not entries:
            continue

        winrate: float | None = None
        sample: int | None = None
        wm = _WINRATE_RE.search(page_html)
        if wm:
            wins = int(wm.group(2))
            losses = int(wm.group(3))
            sample = wins + losses
            if sample > 0:
                # Use raw wins/(wins+losses) — aetherhub's rounded
                # display percentage drops sub-1% precision; we keep
                # the full ratio for downstream filtering.
                winrate = wins / sample

        # Slug = aetherhub's own (already lowercase, hyphenated). Drop
        # the trailing numeric id so two re-uploads of the same deck
        # don't collide on the corpus side; intra-batch dedup by
        # cards_hash catches near-duplicate uploads.
        slug_base = re.sub(r"-\d+$", "", token)
        if not slug_base:
            slug_base = slugify(token)
        n = seen_slugs.get(slug_base, 0) + 1
        seen_slugs[slug_base] = n
        slug = slug_base if n == 1 else f"{slug_base}-{n}"

        # Archetype display = slug with hyphens -> spaces, title-cased
        # commander-like form. The deck page's `<title>` is a more
        # human read but contains the format prefix ("Historic Brawl
        # - Atraxa, Grand Unifier") — easier to derive from the slug.
        archetype = " ".join(w.capitalize() for w in slug_base.split("-"))

        decks.append(ParsedDeck(
            slug=slug,
            archetype=archetype,
            source="aetherhub",
            url=deck_url,
            tier="",
            winrate=winrate,
            sample=sample,
            fetched=fetched,
            entries=entries,
            unresolved=unresolved,
        ))

    return decks
