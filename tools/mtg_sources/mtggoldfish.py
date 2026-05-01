"""mtggoldfish.com metagame-page parser.

Per `docs/sources.md` mtggoldfish is the primary paper-meta source and
publishes per-format `/metagame/<fmt>` pages with server-rendered
archetype tiles. Each tile gives us:

    <div class='archetype-tile' id='<numeric-id>'>
      ...
      <a class='card-image-tile-link-overlay' href='/archetype/<slug>'>
      ...
      <div class='archetype-tile-title'>
        <span class='deck-price-online'>
          <a href="/archetype/<slug>#online">Archetype Name</a>
        ...
      <div class='archetype-tile-statistic metagame-percentage'>
        <div class='archetype-tile-statistic-value'>
          12.5%
          <span class='archetype-tile-statistic-value-extra-data'>(220)</span>

The decklist itself is **not** on the metagame page. The tile links
out to `/archetype/<slug>`, where the deck export lives in a hidden
form field — the static HTML still contains it (the `Arena` tab loads
via AJAX, but the form input is server-rendered):

    <input type="hidden" name="deck_input[deck]"
           id="deck_input_deck"
           value="2 Island
4 Vibrant Outburst
...
sideboard
1 Negate
2 Spell Pierce
" />

That value is plain `count name` lines (no `(SET) NUM`), so we resolve
each card name through the local Scryfall index — same path mtgazone's
parser uses. A literal `sideboard` line (lowercase) splits main from
sideboard.

Per-archetype HTTP is delegated to `_common.http_get_text` so the UA
and 403-retry-once policy match the index fetch in `_fetch_meta_page`.
We don't import back into `tools/mtg.py` (circular) and we don't grow
a parallel HTTP stack.

mtggoldfish does **not** publish per-deck winrate or letter tiers.
Tier stays `""` and winrate stays `None` per spec; synthesising a
letter from %-share would be fiction. Sample size comes from the
parenthesised count next to META% — the only sample number on the
tile and the natural deck-count signal for that archetype.

Probe verified 2026-04-30 against
`https://www.mtggoldfish.com/metagame/{historic, pioneer, standard}`
and per-archetype pages `/archetype/<slug>` with a normal Mozilla UA.
"""

from __future__ import annotations

import html
import re
import urllib.error
from typing import Callable

from ._common import DeckEntry, ParsedDeck, http_get_text, slugify

# Format -> tier-list URL slug. Only the formats `docs/sources.md` lists
# under "Arena format meta" / mtggoldfish are wired in. mtggoldfish has
# pages for alchemy/timeless/explorer/brawl too, but those are
# Arena-niche and the curated source list pins us to the three with
# meaningful sample sizes; adding more without a re-curate would silently
# expand scope past what the project's source-of-truth doc declares.
_URL_TEMPLATES = {
    "standard": "https://www.mtggoldfish.com/metagame/standard",
    "historic": "https://www.mtggoldfish.com/metagame/historic",
    "pioneer": "https://www.mtggoldfish.com/metagame/pioneer",
}


def url_for_format(fmt: str) -> str | None:
    """URL of mtggoldfish's metagame page for `fmt`, or None if unsupported."""
    return _URL_TEMPLATES.get(fmt)


# --- index-page region carving -------------------------------------------

# Each archetype tile opens with `<div class='archetype-tile' id='<id>'>`.
# `id` is either a numeric tile id or empty (budget-decks section). We
# only consume tiles with a non-empty id — the "Budget Decks" rail uses
# `id=''` and links to `/deck/<id>` (a single user submission), not to
# an aggregated archetype, which would skew sample sizes.
_TILE_RE = re.compile(
    r"<div\s+class='archetype-tile'\s+id='(\d+)'>",
    re.IGNORECASE,
)

# Tile title link. The first `<a href="/archetype/<slug>#online">Name</a>`
# inside `archetype-tile-title` is canonical. We anchor on the `#online`
# anchor specifically to avoid the duplicate `#paper` link that follows.
_TILE_TITLE_RE = re.compile(
    r'<a\s+href="(/archetype/[^"#]+)#online">([^<]+)</a>',
    re.IGNORECASE,
)

# META% statistic. The numeric value is the percent share; the
# parenthesised extra-data is the deck count contributing to that share.
# Both whitespace patterns observed are accommodated by the loose `\s*`.
_TILE_META_RE = re.compile(
    r"<div\s+class='archetype-tile-statistic\s+metagame-percentage'>.*?"
    r"<div\s+class='archetype-tile-statistic-value'>\s*"
    r"([0-9.]+)%\s*"
    r"(?:<span\s+class='archetype-tile-statistic-value-extra-data'>\s*"
    r"\(\s*(\d+)\s*\)\s*</span>\s*)?",
    re.IGNORECASE | re.DOTALL,
)


# --- per-archetype-page region carving -----------------------------------

# Hidden form input that carries the full decklist as plain text.
# Markup observed (verified 2026-04-30) ends the input with another
# attribute after `value="..."`:
#   <input type="hidden" name="deck_input[deck]"
#          id="deck_input_deck" value="2 Island\n..." autocomplete="off" />
# Capture stops at the first unescaped `"`. mtggoldfish HTML-encodes
# apostrophes as `&#39;` and never emits a literal `"` inside the
# value, so the negated character class is exact.
_DECK_INPUT_RE = re.compile(
    r'<input\s+type="hidden"\s+name="deck_input\[deck\]"\s+'
    r'id="deck_input_deck"\s+value="([^"]+)"',
    re.IGNORECASE | re.DOTALL,
)

# Deck-line inside the form value: `<count> <name>`. Lines that match the
# literal `sideboard` separator (case-insensitive) flip the section flag
# in the loop and are not treated as cards.
_DECK_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


# --- main entry point ----------------------------------------------------


def parse_mtggoldfish(
    raw_html: str,
    fmt: str,
    *,
    fetched: str,
    url: str,
    resolve_name: Callable[[str], dict | None],
) -> list[ParsedDeck]:
    """Parse mtggoldfish's `/metagame/<fmt>` page into `ParsedDeck` list.

    Walks each `<div class='archetype-tile' id='<numeric>'>` tile in
    document order, fetches the linked `/archetype/<slug>` page, and
    extracts the decklist from the hidden `deck_input[deck]` form
    field. Per-archetype HTTP is via `_common.http_get_text` with the
    403-retry-once policy enabled (per `docs/sources.md`).

    Empty result list is *not* an error here — `cmd_fetch_meta` decides
    whether zero decks for this URL means schema drift and surfaces it
    one layer up. Letting empty pass through keeps the parser pure.

    Sub-resource HTTP failures (per-archetype 4xx/5xx after retry) are
    treated as drift for that single archetype and dropped silently
    here; the index fetch is the load-bearing one and is enforced by
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
            # Tile with no title link — schema drift for this tile.
            # Skip silently; cmd_fetch_meta hard-fails on zero decks.
            continue
        href = title_m.group(1)  # e.g. /archetype/pioneer-izzet-prowess
        archetype_raw = html.unescape(title_m.group(2)).strip()
        if not archetype_raw:
            continue

        deck_url = "https://www.mtggoldfish.com" + href

        # META% + sample. Both fields are best-effort — a tile that hides
        # them is still parseable; we just leave sample None.
        sample: int | None = None
        meta_m = _TILE_META_RE.search(body)
        if meta_m and meta_m.group(2):
            try:
                sample = int(meta_m.group(2))
            except ValueError:
                sample = None

        try:
            arch_html = http_get_text(deck_url, retry_403_once=True)
        except urllib.error.HTTPError:
            # Per-archetype hard-fail after retry: drop this tile, keep
            # parsing the rest. The page-wide fetch already established
            # mtggoldfish is reachable, so a single 403/404 here is
            # almost always a stale archetype URL on the index.
            continue
        except urllib.error.URLError:
            continue

        entries, unresolved = _entries_from_archetype_page(arch_html, resolve_name)
        if not entries:
            # Drift: archetype page rendered but no parseable cards.
            continue

        slug_base = slugify(archetype_raw)
        n = seen_slugs.get(slug_base, 0) + 1
        seen_slugs[slug_base] = n
        slug = slug_base if n == 1 else f"{slug_base}-{n}"

        decks.append(ParsedDeck(
            slug=slug,
            archetype=archetype_raw,
            source="mtggoldfish",
            url=deck_url,
            tier="",  # mtggoldfish does not publish letter tiers
            winrate=None,  # not published per-archetype
            sample=sample,
            fetched=fetched,
            entries=entries,
            unresolved=unresolved,
        ))

    return decks


def _entries_from_archetype_page(
    arch_html: str, resolve_name: Callable[[str], dict | None],
) -> tuple[list[DeckEntry], int]:
    """Extract (DeckEntry list, dropped-copies count) from `/archetype/<slug>`.

    Pulls the hidden `deck_input[deck]` form field, splits on the
    literal `sideboard` line (lowercase, mtggoldfish-specific), and
    resolves each `<count> <name>` row through `resolve_name`. Cards
    that don't resolve to a Scryfall printing are dropped — emitting a
    deck-line MTGA would reject on import is worse than emitting a
    short deck — and the dropped copy count is returned so cmd_fetch_meta
    can surface it via the sidecar.
    """
    out: list[DeckEntry] = []
    unresolved = 0
    m = _DECK_INPUT_RE.search(arch_html)
    if not m:
        return out, unresolved

    body = html.unescape(m.group(1))
    section = "deck"

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == "sideboard":
            section = "sideboard"
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
