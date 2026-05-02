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

* **Archetype** — server-rendered table of up to ~15 user-submitted
  decks (verified 2026-05-02 across 6 archetypes; consistently 15
  unique deck rows). The deck rows live inside a stable container —
  `<table cellpadding="0" cellspacing="0" class="clickable table table-
  striped hidden-xs">` — and each row carries a per-deck winrate cell of
  the form `W/L <br> (W&nbsp;-&nbsp;L) <br/> NN%`, plus a deck-page
  link of the form `/Historic/<slug>-decklist-by-<user>-<id>` (slug is
  free-form, not constrained to an `h-` prefix as an earlier probe
  suggested). We walk the rows of that container and take a per-archetype
  cap derived from `--limit / num_archetypes` (or all rows when no
  `--limit` is set), preserving document order = recency order.
  Duplicates with identical lists are collapsed downstream by
  `_write_meta_corpus`'s near-dup deduplication. When an archetype's URL
  appears multiple times in the index (very rare), we suffix deck names
  with variant markers. Per-deck winrate replaces the per-archetype
  winrate on `ParsedDeck.winrate` (more granular signal); `sample` is
  the per-row W+L count (per-deck match volume), falling back to the
  per-archetype count when the row's win/loss cells are missing.

  Pagination: the archetype page paginates as `/Historic/<slug>/page:N`,
  observed up to 11 pages × 15 decks ≈ 165 unique decks per archetype
  on `boros-energy` (probe 2026-05-02). The page:N walk only triggers
  under `--deep`, where `tools/mtg.py:7308` substitutes `_DEEP_LIMIT`
  for the caller's missing `--limit`, blowing the per-archetype budget
  past page-1's ~15-row yield. Default (non-deep) invocations skip the
  walk and preserve the original page-1-only behaviour bit-for-bit.

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

# Per-archetype deep-walk ceiling. Probe (2026-05-02) shows
# `boros-energy` paginates ~11 pages × 15 decks = ~165 unique decks
# reachable per archetype; multiplied by the ~30-archetype index gives a
# theoretical 5000-deck-per-format ceiling. `tools/mtg.py` reads this
# constant via `getattr` when `--deep` is set and passes it as `limit`.
_DEEP_LIMIT = 5000

# Hard cap on the page-walk. Observed max in the wild is 11; we triple
# it as a safety belt against a future archetype that paginates deeper.
# Beyond this we trust the "no new rows" terminator to stop us.
_MAX_PAGES = 50


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

# The archetype page wraps its deck list in this distinctive table tag
# (verified 2026-05-02 across 6 archetypes — every page used this exact
# class string). Carving on this container is essential because mtgdecks
# also emits `/Historic/<other-slug>-decklist-by-…` links in sidebar
# nav and breadcrumbs that would steal the wrong slugs if we matched
# across the whole document.
_DECK_TABLE_RE = re.compile(
    r'<table\s+cellpadding="0"\s+cellspacing="0"\s+class="clickable\s+table\s+table-striped\s+hidden-xs">(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)

# Split the deck-table body into one chunk per `<tr>`. Each chunk holds
# at most one deck link + at most one W/L winrate cell; pairing them by
# row position keeps deck-link / winrate / sample correlated even when
# some rows have missing winrate cells (occasional `&mdash;` entries).
_DECK_TABLE_ROW_SPLIT_RE = re.compile(r'<tr\b[^>]*>', re.IGNORECASE)

# Deck-page links inside the deck-table body. The slug is free-form
# (`bo3-new-jeskai-chorus`, `gsz-gates`, `jeskai-truths-4`, `h-chorus`
# — no `h-` prefix invariant); the load-bearing structure is
# `<archetype-or-variant-slug>-decklist-by-<user>-<id>`. Document order
# is recency order (newest first); we slice to the per-archetype cap
# below.
_DECK_LINK_RE = re.compile(
    r'href="(/Historic/[a-z0-9-]+decklist-by[a-z0-9-]+-\d+)"',
    re.IGNORECASE,
)

# Per-deck winrate cell. Format observed: `W/L <br> (13&nbsp;-&nbsp;8)
# <br/> 61%`. We capture wins, losses, and the percent so we can derive
# both `winrate` (the percent) and a per-deck `sample` (W + L = total
# matches). Some rows have missing winrate cells (rare); those decks fall
# back to the per-archetype winrate / sample.
_DECK_ROW_WINRATE_RE = re.compile(
    r'W/L[^<]*<br[^>]*>\s*\((\d+)\s*&nbsp;-&nbsp;(\d+)\)\s*<br[^>]*>\s*(\d+)%',
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
    limit: int | None = None,
    **_: object,
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

    # Per-archetype deck cap: ~15 decks live in each archetype page (verified
    # 2026-05-02). Without a `--limit`, take all of them; with one, divide
    # evenly (round UP via ceil) so the per-archetype budget never under-
    # shoots the requested cap when limit is close to num_archetypes (e.g.
    # `--limit 50` against 30 archetypes => 2 decks per archetype = 60 raw,
    # truncated by cmd_fetch_meta to 50). Floor of 1 means even `--limit 1`
    # returns the most-recent deck from each archetype.
    if limit is not None and limit > 0:
        per_archetype_cap = max(1, -(-limit // len(tile_matches)))
    else:
        per_archetype_cap = None  # take all rows in each archetype

    # Append a sentinel so each tile body slices to the next tile's start.
    bounds = [m.start() for m in tile_matches] + [len(raw_html)]
    seen_archetypes: dict[str, int] = {}  # Track archetype slug collisions

    for i, m in enumerate(tile_matches):
        if limit is not None and limit > 0 and len(decks) >= limit:
            break
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

        # Per-archetype winrate / sample (fallback when a deck row's own
        # W/L cell is missing — rare, ~1 row per page).
        archetype_sample: int | None = None
        sample_m = _TILE_SAMPLE_RE.search(body)
        if sample_m:
            try:
                archetype_sample = int(sample_m.group(1))
            except ValueError:
                archetype_sample = None

        archetype_winrate: float | None = None
        wr_m = _TILE_WINRATE_RE.search(body)
        if wr_m:
            try:
                archetype_winrate = float(wr_m.group(1)) / 100.0
            except ValueError:
                archetype_winrate = None

        archetype_url = f"https://mtgdecks.net/Historic/{slug}"

        # Fetch the archetype page. We use the index URL as Referer per the
        # documented contract. A 4xx after retry drops this archetype; the
        # rest of the run continues.
        try:
            arch_html = http_get_text(
                archetype_url, retry_403_once=True, referer=url,
            )
        except urllib.error.HTTPError:
            continue
        except urllib.error.URLError:
            continue

        # Carve out the deck-list table (stable container — see _DECK_TABLE_RE
        # rationale above). Falling outside this container would pull
        # sidebar / breadcrumb deck links from the wrong archetype.
        table_m = _DECK_TABLE_RE.search(arch_html)
        if not table_m:
            # Drift: archetype page rendered but the deck table is missing.
            continue
        table_body = table_m.group(1)

        # Walk per-row chunks. Each `<tr>` chunk has at most one deck link
        # and at most one W/L winrate cell, so positional pairing within the
        # chunk keeps signal correlated to its deck. `seen_paths` is shared
        # across pages so the page:N walk below dedupes against page-1 too.
        seen_paths: set[str] = set()
        deck_rows = _rows_from_archetype_table(
            table_body, seen_paths, archetype_winrate, archetype_sample,
        )

        if not deck_rows:
            # Drift: archetype page rendered but no deck links in the table.
            continue

        # Pagination walk (`--deep`): only triggers when caller passed a
        # `limit` large enough that page-1 (~15 rows) doesn't satisfy the
        # per-archetype budget. `tools/mtg.py:7308` injects `_DEEP_LIMIT`
        # as `limit` when `--deep` is set, producing a `per_archetype_cap`
        # of ~167 against the ~30-archetype index — well past the 15/page
        # ceiling. Default invocations leave `per_archetype_cap` either
        # None (no `--limit`) or small (e.g. `--limit 50` -> cap=2), and
        # the loop is skipped entirely so historic behaviour is unchanged.
        if per_archetype_cap is not None and len(deck_rows) < per_archetype_cap:
            for page in range(2, _MAX_PAGES + 1):
                if len(deck_rows) >= per_archetype_cap:
                    break
                page_url = f"{archetype_url}/page:{page}"
                try:
                    page_html = http_get_text(
                        page_url, retry_403_once=True, referer=archetype_url,
                    )
                except urllib.error.HTTPError:
                    break
                except urllib.error.URLError:
                    break
                page_table_m = _DECK_TABLE_RE.search(page_html)
                if not page_table_m:
                    # End of pagination (mtgdecks 200s with empty body
                    # past the last page rather than 404'ing) or drift.
                    break
                new_rows = _rows_from_archetype_table(
                    page_table_m.group(1),
                    seen_paths,
                    archetype_winrate,
                    archetype_sample,
                )
                if not new_rows:
                    # No fresh deck paths on this page -> walked off the
                    # end of paginated content. Stop before _MAX_PAGES.
                    break
                deck_rows.extend(new_rows)

        # Cap per-archetype takes. None = unlimited (no --limit set);
        # otherwise slice to the budget computed above.
        if per_archetype_cap is not None:
            deck_rows = deck_rows[:per_archetype_cap]

        # Track archetype slug collisions separately from per-deck variants.
        arch_count = seen_archetypes.get(slug, 0) + 1
        seen_archetypes[slug] = arch_count
        arch_suffix = "" if arch_count == 1 else f"-{arch_count}"

        for deck_idx, (deck_path, row_winrate, row_sample) in enumerate(
            deck_rows, start=1
        ):
            if limit is not None and limit > 0 and len(decks) >= limit:
                break

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
            # per archetype on the index. When multiple decks from the same
            # archetype, suffix with variant marker only if deck_idx > 1.
            # If the same slug appears in multiple index rows (very rare),
            # append arch_suffix first.
            if deck_idx == 1:
                final_slug = f"{slug}{arch_suffix}"
            else:
                final_slug = f"{slug}{arch_suffix}-variant{deck_idx}"

            decks.append(ParsedDeck(
                slug=final_slug,
                archetype=archetype_raw,
                source="mtgdecks",
                url=deck_url,
                tier=tier_letter,
                winrate=row_winrate,
                sample=row_sample,
                fetched=fetched,
                entries=entries,
                unresolved=unresolved,
            ))

    return decks


def _rows_from_archetype_table(
    table_body: str,
    seen_paths: set[str],
    archetype_winrate: float | None,
    archetype_sample: int | None,
) -> list[tuple[str, float | None, int | None]]:
    """Extract `(deck_path, winrate, sample)` rows from one table body.

    Splits `table_body` on `<tr>` boundaries; each chunk holds at most one
    deck link and at most one W/L cell, so positional pairing within the
    chunk keeps signal correlated to its deck. Mutates `seen_paths` so the
    caller's `--deep` page:N walk dedupes across pages (the same deck can
    bubble up onto multiple pages when authors edit a deck and the site
    re-shuffles the order). Falls back to `archetype_winrate` / `_sample`
    when a row's W/L cell is missing or malformed (rare: ~1 row per page).
    """
    rows: list[tuple[str, float | None, int | None]] = []
    for chunk in _DECK_TABLE_ROW_SPLIT_RE.split(table_body):
        link_m = _DECK_LINK_RE.search(chunk)
        if not link_m:
            continue
        deck_path = link_m.group(1)
        if deck_path in seen_paths:
            continue
        seen_paths.add(deck_path)
        wr_row_m = _DECK_ROW_WINRATE_RE.search(chunk)
        if wr_row_m:
            try:
                wins = int(wr_row_m.group(1))
                losses = int(wr_row_m.group(2))
                pct = float(wr_row_m.group(3)) / 100.0
                row_winrate: float | None = pct
                row_sample: int | None = wins + losses
            except ValueError:
                row_winrate = archetype_winrate
                row_sample = archetype_sample
        else:
            row_winrate = archetype_winrate
            row_sample = archetype_sample
        rows.append((deck_path, row_winrate, row_sample))
    return rows


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
