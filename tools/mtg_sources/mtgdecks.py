"""mtgdecks.net per-format archetype index parser.

Per `docs/sources.md` mtgdecks was previously dropped because Cloudflare
403'd every WebFetch. A 2026-05-01 probe (P2 in this branch) shows the
site now serves a vanilla `mtg-toolkit/0.1` UA with `200 OK`, so we wire
it as a third Historic source. Only `historic` is implemented; the other
formats are a single URL change away but stay out of scope until we have
a curated reason to add them (the project's "no scope creep without a
re-curate" rule from `docs/sources.md`).

Page topology (verified 2026-05-01 against
`https://mtgdecks.net/Historic`, the `/Historic/<slug>` archetype pages,
and the `/Historic/h-<slug>-decklist-by-<user>-<id>` deck pages):

* **Index** — one `<tr class="… tier-(1|2)|rogue tier-all">` row per
  archetype, each containing
  `<a href="https://mtgdecks.net/Historic/<slug>" class="text-uppercase">`.
  The same row carries:
    - tier (`tier-1` -> S, `tier-2` -> A, `rogue` -> "");
    - meta-share `<b class="meta-share hidden-xs">6.33%</b>`;
    - winrate `<td class="sort number"><b>61%</b></td>` (or `&mdash;`
      for unraced archetypes);
    - decks count `<td class="sort number hidden-xs">53</td>`.
  We capture meta-share into nothing (no field on `ParsedDeck` for it),
  winrate into `winrate`, decks count into `sample`. Tier maps via
  `_TIER_CLASS_TO_LETTER`.

* **Archetype** — server-rendered list of individual user-submitted
  decks. The first `<a href="/Historic/h-<slug>-decklist-by-<user>-<id>">`
  in document order is the most-recent submission (mtgdecks lists by
  recency by default). v0 takes that single deck per archetype to mirror
  mtgazone's "one-deck-per-archetype" output shape; multi-deck-per-
  archetype is a later feature, flagged in the report but not shipped.

* **Deck** — embeds the full MTGA paste verbatim in
  `<textarea id="arena_deck">Deck\n4 …\nSideboard\n…</textarea>`. Plain
  `<count> <name>` lines, same shape mtggoldfish's hidden form input
  carries; reuses the same line-by-line resolve loop pattern.

mtgdecks **does** publish a tier letter (S/A) and a winrate per
archetype on the index. Sample is the per-archetype "Decks" count.
Cards are not given with `(SET) NUM` — just the name — so each card
is resolved through the local Scryfall index (`resolve_name`) to fill
`set_code`/`collector` and the deck file matches the rest of the CLI's
DeckEntry contract. Per-card resolution failures bump the deck's
`unresolved` counter (surfaced through `ParsedDeck.unresolved`).

Slug naming: mtgdecks's URL slug is already kebab-case ASCII, so we use
it verbatim as the filename stem (`boros-energy.txt`). When this clashes
with a slug another source already wrote into the same `--out` dir,
`_write_meta_corpus` overwrites — which is the existing behaviour for
mtgazone and mtggoldfish too. There is no "merge sources" semantic in
the corpus today; if that's wanted later, it's a `_write_meta_corpus`
change, not a parser concern.
"""

from __future__ import annotations

import html
import re
import urllib.error
from typing import Callable

from ._common import DeckEntry, ParsedDeck, http_get_text

# mtgdecks's tier badges. `tier-1` and `tier-2` are letter-mapped to
# match mtgazone's S/A/B/C/D normalisation in `_common.py`. `rogue`
# means "no tier" — leave the field empty rather than invent a letter.
_TIER_CLASS_TO_LETTER = {
    "tier-1": "S",
    "tier-2": "A",
    "rogue": "",
}

# Format -> index URL. Only Historic is curated for v0 per
# `docs/sources.md`'s scope rule. The site exposes `/Standard`,
# `/Pioneer`, `/Modern` etc., but those duplicate mtggoldfish's data
# and adding them without a doc update would silently expand scope.
_URL_TEMPLATES = {
    "historic": "https://mtgdecks.net/Historic",
}


def url_for_format(fmt: str) -> str | None:
    """URL of mtgdecks's archetype-index page for `fmt`, or None.

    Only `historic` is supported; caller hard-fails for any other format
    via the standard "source does not publish a tier list for format X"
    branch in `cmd_fetch_meta`.
    """
    return _URL_TEMPLATES.get(fmt)


# --- index-page region carving -------------------------------------------

# One archetype per `<tr>` whose `class` contains either `tier-1`,
# `tier-2`, or `rogue`, and the `tier-all` discriminator that filters
# out non-archetype rows (table headers, separator strips). The leading
# `\s+` on the class attribute is forgiving — mtgdecks emits two leading
# spaces inside `class="  tier-1 tier-all"`.
_TILE_RE = re.compile(
    r'<tr[^>]*class="\s*(tier-1|tier-2|rogue)\s+tier-all"',
    re.IGNORECASE,
)

# Archetype title link. Anchors on the absolute URL form mtgdecks uses
# inside the table (`https://mtgdecks.net/Historic/<slug>`); the relative
# `/Historic/<slug>` form appears elsewhere on the page (sidebar nav,
# breadcrumbs) and would steal the wrong slug if we matched it. The
# `text-uppercase` class is the unique discriminator for the title link.
_TILE_TITLE_RE = re.compile(
    r'<a\s+href="https://mtgdecks\.net/Historic/([a-z0-9-]+)"\s+'
    r'class="text-uppercase">([^<]+)</a>',
    re.IGNORECASE,
)

# Per-archetype "Decks" count (sample size). The hidden-xs cell carries
# the integer; the visible-xs sibling has it too but as a smaller-screen
# variant, so we anchor on the hidden-xs class.
_TILE_SAMPLE_RE = re.compile(
    r'<td\s+class="sort\s+number\s+hidden-xs">\s*(\d+)\s*</td>',
    re.IGNORECASE,
)

# Per-archetype winrate. The cell looks like `<td class="sort number">
# <b>61%</b></td>` for raced archetypes, or `<b>&mdash;</b>` for unraced.
# We accept either and parse the numeric form into a 0-1 fraction.
_TILE_WINRATE_RE = re.compile(
    r'<td\s+class="sort\s+number">\s*<b>\s*([0-9.]+)%\s*</b>',
    re.IGNORECASE,
)


# --- per-archetype-page region carving -----------------------------------

# Deck-page link inside an archetype page. The slug appears prefixed by
# `h-<archetype-slug>-decklist-by-<user>-<id>`. We capture the relative
# href; document order = recency order (newest first), so the first hit
# is the v0 singleton. Anchors on the relative `/Historic/h-` form that
# the archetype-page deck table uses (the sidebar / footer never link to
# `h-` URLs, so there's no false-positive risk).
_DECK_LINK_RE = re.compile(
    r'href="(/Historic/h-[a-z0-9-]+-decklist-by-[a-z0-9-]+-\d+)"',
    re.IGNORECASE,
)


# --- per-deck-page region carving ----------------------------------------

# The full MTGA paste lives inside a `<textarea id="arena_deck">`. The
# `rows="15"` attribute observed on the probe is informational; we only
# need the id to anchor and capture everything up to the first `</textarea>`.
_ARENA_TEXTAREA_RE = re.compile(
    r'<textarea\s+id="arena_deck"[^>]*>(.*?)</textarea>',
    re.IGNORECASE | re.DOTALL,
)

# `<count> <name>` line inside the arena_deck textarea. Same shape as
# mtggoldfish's hidden form value.
_DECK_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")

# Section markers mtgdecks emits inside the textarea. `Deck` opens the
# main; `Sideboard` opens the SB. Anything else inside the textarea is
# either a card line or empty.
_SECTION_HEADERS = {
    "deck": "deck",
    "sideboard": "sideboard",
}


# --- main entry point ----------------------------------------------------


def parse_mtgdecks(
    raw_html: str,
    fmt: str,
    *,
    fetched: str,
    url: str,
    resolve_name: Callable[[str], dict | None],
) -> list[ParsedDeck]:
    """Parse the mtgdecks `/Historic` page into a list of `ParsedDeck`.

    For each archetype `<tr>`, fetch the linked `/Historic/<slug>` page,
    pick the first deck link (most recent), fetch that, and extract the
    `arena_deck` textarea. Per-archetype + per-deck HTTP is via
    `_common.http_get_text` with the 403-retry-once policy enabled and
    the documented Referer attached.

    Empty result list is *not* an error here — `cmd_fetch_meta` decides
    whether zero decks for this URL means schema drift and surfaces it
    one layer up. Letting empty pass through keeps the parser pure.

    Sub-resource HTTP failures (per-archetype or per-deck 4xx/5xx after
    retry) are treated as drift for that single archetype and dropped
    silently; the index fetch is the load-bearing one and is enforced by
    the caller's "zero decks = drift" rule.
    """
    decks: list[ParsedDeck] = []

    tile_matches = list(_TILE_RE.finditer(raw_html))
    if not tile_matches:
        return decks

    # Append a sentinel so each tile body slices to the next tile's start.
    bounds = [m.start() for m in tile_matches] + [len(raw_html)]
    seen_slugs: dict[str, int] = {}

    for i, m in enumerate(tile_matches):
        body = raw_html[m.start():bounds[i + 1]]

        title_m = _TILE_TITLE_RE.search(body)
        if not title_m:
            # Tile with no archetype link — schema drift for this row.
            # Skip silently; cmd_fetch_meta hard-fails on zero decks.
            continue
        slug = title_m.group(1)
        archetype_raw = html.unescape(title_m.group(2)).strip()
        if not slug or not archetype_raw:
            continue

        tier_class = m.group(1).lower()
        tier_letter = _TIER_CLASS_TO_LETTER.get(tier_class, "")

        sample: int | None = None
        sample_m = _TILE_SAMPLE_RE.search(body)
        if sample_m:
            try:
                sample = int(sample_m.group(1))
            except ValueError:
                sample = None

        winrate: float | None = None
        wr_m = _TILE_WINRATE_RE.search(body)
        if wr_m:
            try:
                winrate = float(wr_m.group(1)) / 100.0
            except ValueError:
                winrate = None

        archetype_url = f"https://mtgdecks.net/Historic/{slug}"

        # Fetch the archetype page to get the first deck's URL. We use
        # the index URL as Referer per the documented contract. A 4xx
        # after retry drops this archetype; the rest of the run continues.
        try:
            arch_html = http_get_text(
                archetype_url, retry_403_once=True, referer=url,
            )
        except urllib.error.HTTPError:
            continue
        except urllib.error.URLError:
            continue

        deck_link_m = _DECK_LINK_RE.search(arch_html)
        if not deck_link_m:
            # Drift: archetype page rendered but no deck link inside.
            continue
        deck_path = deck_link_m.group(1)
        deck_url = "https://mtgdecks.net" + deck_path

        try:
            deck_html = http_get_text(
                deck_url, retry_403_once=True, referer=archetype_url,
            )
        except urllib.error.HTTPError:
            continue
        except urllib.error.URLError:
            continue

        entries, unresolved = _entries_from_deck_page(deck_html, resolve_name)
        if not entries:
            # Drift: deck page rendered but textarea missing or empty.
            continue

        # The slug from the URL is already kebab-case ASCII and unique
        # per archetype on the index — no need to re-slugify the display
        # name. Disambiguate with `-2`, `-3`, ... if (somehow) two index
        # rows pointed to the same slug.
        n = seen_slugs.get(slug, 0) + 1
        seen_slugs[slug] = n
        final_slug = slug if n == 1 else f"{slug}-{n}"

        decks.append(ParsedDeck(
            slug=final_slug,
            archetype=archetype_raw,
            source="mtgdecks",
            url=deck_url,
            tier=tier_letter,
            winrate=winrate,
            sample=sample,
            fetched=fetched,
            entries=entries,
            unresolved=unresolved,
        ))

    return decks


def _entries_from_deck_page(
    deck_html: str, resolve_name: Callable[[str], dict | None],
) -> tuple[list[DeckEntry], int]:
    """Extract (DeckEntry list, dropped-copies count) from a deck page.

    Pulls the `<textarea id="arena_deck">…</textarea>` content, walks
    `<count> <name>` lines, and flips section state on the literal
    `Deck` / `Sideboard` headers (case-insensitive). Cards that don't
    resolve to a Scryfall printing are dropped — emitting a deck-line
    MTGA would reject on import is worse than emitting a short deck —
    and the dropped copy count is returned so cmd_fetch_meta can
    surface it via the sidecar.
    """
    out: list[DeckEntry] = []
    unresolved = 0

    m = _ARENA_TEXTAREA_RE.search(deck_html)
    if not m:
        return out, unresolved

    body = html.unescape(m.group(1))
    section = "deck"

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = _SECTION_HEADERS.get(line.lower())
        if header is not None:
            section = header
            continue
        line_m = _DECK_LINE_RE.match(line)
        if not line_m:
            continue
        try:
            count = int(line_m.group(1))
        except ValueError:
            continue
        if count <= 0:
            continue
        name = line_m.group(2).strip()
        if not name:
            continue
        printing = resolve_name(name)
        if printing is None:
            # Card name didn't resolve — skip rather than emit a
            # deck-line MTGA would reject on import.
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
