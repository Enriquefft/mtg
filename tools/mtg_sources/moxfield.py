"""moxfield.com user-deck search parser.

Moxfield exposes a public REST API (api2.moxfield.com) used by its own
React frontend. We hit:

    https://api2.moxfield.com/v2/decks/search?fmt=<f>&pageSize=50
        &pageNumber=<n>&sortType=updated&sortDirection=descending

to enumerate the most-recently-updated decks per format, then fetch
each individual deck via:

    https://api2.moxfield.com/v3/decks/all/<publicId>

Both endpoints sit behind Cloudflare and 403 the toolkit's default
User-Agent; we send a browser-like UA + the `Origin` / `Referer`
headers their CORS allow-list expects (parity with the website).

Format mapping (moxfield uses different filter values than our CLI):

    standard      -> standard
    alchemy       -> alchemy
    historic      -> historic
    timeless      -> timeless
    pioneer       -> pioneer
    brawl (ours)  -> historicBrawl   (Moxfield's "Brawl" is Standard Brawl)

Throttling: 0.6s between per-deck fetches. Probe verified 50/page +
2-page burst at this cadence stayed below Cloudflare's per-IP rate
floor; bursting harder triggers brief 403 windows.

Winrate / sample: not surfaced by Moxfield (these are user-built
decks, not metagame samples). Both fields stay None; the source's
strength is corpus depth, not signal quality. Recommend's own
ownership scoring + cross-source dedup do the discrimination.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
from typing import Callable

from ._common import DeckEntry, ParsedDeck, http_get_text, slugify

_API_HOST = "https://api2.moxfield.com"
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "Origin": "https://www.moxfield.com",
}
_BROWSER_REFERER = "https://www.moxfield.com/"

# Page size 50 = sweet spot. The API caps at ~100 but larger pages
# correlate with more 503s under load; 50 keeps us inside the polite
# envelope while halving search round-trips vs the default 24.
_SEARCH_PAGE_SIZE = 50

# Per-deck fetch throttle. Lower than 0.5s starts triggering 403
# bursts; higher slows large fetches (a 500-deck brawl pull is already
# ~5 minutes at this cadence, fine for a one-time corpus build).
_PER_DECK_THROTTLE_SECS = 0.6

# Maximum pages to walk during search, so a runaway --limit doesn't
# accidentally walk the whole 10k-deck index. 200 pages * 50 decks =
# 10000-deck cap, matching the API's totalResults ceiling.
_MAX_SEARCH_PAGES = 200

# Cap for `--deep` mode (cmd_fetch_meta consults this when the user
# passes `--deep` without an explicit `--limit`). Set to the API's
# totalResults ceiling so a deep build pass walks the whole search
# universe; the per-page walk in parse_moxfield already early-exits
# when `len(decks) >= limit`, so an over-large cap is safe.
_DEEP_LIMIT = _SEARCH_PAGE_SIZE * _MAX_SEARCH_PAGES  # 10000

_FORMAT_MAP = {
    "standard": "standard",
    "alchemy": "alchemy",
    "historic": "historic",
    "timeless": "timeless",
    "pioneer": "pioneer",
    "brawl": "historicBrawl",
    # standardbrawl deliberately omitted: probe shows fmt=standardBrawl
    # returns no results from this endpoint. If ever needed, re-probe.
}


def url_for_format(fmt: str) -> str | None:
    """Search-URL for the first page of `fmt`. None if unsupported.

    Caller treats None as a hard "this source doesn't cover this
    format" — surfaced as an exit-2 user error in cmd_fetch_meta.
    """
    moxfield_fmt = _FORMAT_MAP.get(fmt)
    if moxfield_fmt is None:
        return None
    return (
        f"{_API_HOST}/v2/decks/search?fmt={moxfield_fmt}"
        f"&pageSize={_SEARCH_PAGE_SIZE}&pageNumber=1"
        f"&sortType=updated&sortDirection=descending"
    )


def _http_get_json(url: str) -> dict:
    """Wrapper around http_get_text + json.loads with browser headers.

    Centralises the moxfield-specific UA / Origin / Referer combo so
    every API hit (search + per-deck) uses the same envelope.
    """
    text = http_get_text(
        url,
        accept="application/json, text/plain, */*",
        user_agent=_BROWSER_UA,
        referer=_BROWSER_REFERER,
        extra_headers=_BROWSER_HEADERS,
    )
    return json.loads(text)


def _slug_from_deck(name_hint: str, public_id: str) -> str:
    """Filename slug derived from the moxfield deck name + publicId.

    Prefer a slugified name (`gruul-ramp` is more useful than
    `s-r17g4ht0a904jbvvnbuw`), but fall back to the publicId if the
    name slugifies to fewer than 4 chars (emoji-only / blank names).
    Suffix the first 6 chars of the publicId so two decks with the
    same name don't collide before our intra-batch dedup runs.
    """
    name_slug = slugify(name_hint) if name_hint else ""
    if len(name_slug) < 4:
        name_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", public_id).strip("-").lower()
        return name_slug or "deck"
    pid_safe = re.sub(r"[^a-zA-Z0-9]+", "", public_id).lower()[:6]
    return f"{name_slug}-{pid_safe}" if pid_safe else name_slug


# Sections we care about from a moxfield deck's `boards` dict. Keys
# are moxfield board names; values are our DeckEntry.section labels.
# `signatureSpells`, `attractions`, `stickers`, `tokens` etc are
# Constructed-irrelevant for Arena and dropped.
_BOARD_TO_SECTION = {
    "commanders": "commander",
    "companions": "companion",
    "mainboard": "deck",
    "sideboard": "sideboard",
}


def _entries_from_deck(
    deck_json: dict,
    resolve_name: Callable[[str], dict | None],
) -> tuple[list[DeckEntry], int]:
    """Walk `deck_json.boards` -> DeckEntry list + dropped-copy count.

    Resolution-failure copies bump the unresolved counter (mirrors
    other parsers). We deliberately do NOT short-circuit on the
    moxfield-specific `card.isArenaLegal` flag — that field reflects
    the *printing* moxfield happens to have selected (often a paper
    Masterpiece / Commander reprint), not the card's overall Arena
    availability. Our `_resolve_card` resolver picks an Arena printing
    when one exists and returns None when the card is genuinely paper-
    only; trusting that path keeps us aligned with how every other
    parser treats card legality.

    Multi-face cards: moxfield serves either `Bonecrusher Giant` or
    `Bonecrusher Giant // Stomp`. We pass the moxfield name through
    `resolve_name` and then take the resolver's canonical `name`,
    which carries the `// Stomp` suffix MTGA's import requires.
    """
    out: list[DeckEntry] = []
    unresolved = 0
    boards = deck_json.get("boards") or {}
    if not isinstance(boards, dict):
        return out, unresolved

    for board_key, section in _BOARD_TO_SECTION.items():
        board = boards.get(board_key) or {}
        if not isinstance(board, dict):
            continue
        cards = board.get("cards") or {}
        if not isinstance(cards, dict):
            continue
        for entry in cards.values():
            if not isinstance(entry, dict):
                continue
            quantity = entry.get("quantity")
            card = entry.get("card") or {}
            if not isinstance(card, dict) or not isinstance(quantity, int):
                continue
            if quantity <= 0:
                continue
            front_name = card.get("name")
            if not isinstance(front_name, str) or not front_name:
                unresolved += quantity
                continue

            printing = resolve_name(front_name)
            if printing is None:
                unresolved += quantity
                continue
            canonical_name = printing.get("name") or front_name
            set_code = (printing.get("set") or "").upper()
            collector = printing.get("collector_number") or ""
            if not set_code or not collector:
                unresolved += quantity
                continue
            out.append(DeckEntry(
                count=quantity,
                name=canonical_name,
                set_code=set_code,
                collector=collector,
                section=section,
            ))
    return out, unresolved


def _public_url(public_id: str) -> str:
    return f"https://www.moxfield.com/decks/{public_id}"


def parse_moxfield(
    raw_text: str,
    fmt: str,
    *,
    fetched: str,
    url: str,
    resolve_name: Callable[[str], dict | None],
    limit: int | None = None,
    **_: object,
) -> list[ParsedDeck]:
    """Parse a moxfield search response + walk per-deck endpoints.

    `raw_text` is the JSON body of page 1 (already fetched by
    cmd_fetch_meta). We parse it for `data[].publicId` + `totalPages`,
    then walk additional pages directly via `_http_get_json` until we
    have `limit` decks (or exhaust pages). Each public ID gets a
    `/v3/decks/all/<id>` fetch with `_PER_DECK_THROTTLE_SECS` of sleep
    between them.

    Hard-fail conditions (raise ValueError; cmd_fetch_meta exits 1):
      * page 1 doesn't deserialise as `{"data": [...]}` (schema drift)
      * `data` exists but no entry yields a usable publicId — hints
        the API renamed the field.

    A handful of per-deck 404s / 403s are tolerated (non-public decks
    sometimes leak into search results; moxfield's CDN occasionally
    rate-limits): they bump a local `failed` counter but don't abort
    the overall fetch. If *every* per-deck fetch fails, we return an
    empty list and let cmd_fetch_meta hard-fail on zero decks.
    """
    moxfield_fmt = _FORMAT_MAP.get(fmt)
    if moxfield_fmt is None:
        # Caller already rejected via url_for_format; defensive guard.
        return []

    target = limit if (limit is not None and limit > 0) else 50

    try:
        page = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"moxfield: page 1 not JSON: {e}") from None
    if not isinstance(page, dict) or "data" not in page:
        raise ValueError("moxfield: page 1 missing 'data' field")

    public_ids: list[tuple[str, str]] = []  # (publicId, archetype-name-hint)
    seen: set[str] = set()

    def _ingest(p: dict) -> None:
        for row in p.get("data") or []:
            if not isinstance(row, dict):
                continue
            pid = row.get("publicId")
            if not isinstance(pid, str) or not pid or pid in seen:
                continue
            seen.add(pid)
            name_hint = row.get("name") if isinstance(row.get("name"), str) else ""
            public_ids.append((pid, name_hint))

    _ingest(page)
    total_pages = page.get("totalPages")
    if not isinstance(total_pages, int):
        total_pages = 1
    total_pages = min(total_pages, _MAX_SEARCH_PAGES)

    page_n = 1
    while len(public_ids) < target and page_n < total_pages:
        page_n += 1
        url_n = (
            f"{_API_HOST}/v2/decks/search?fmt={moxfield_fmt}"
            f"&pageSize={_SEARCH_PAGE_SIZE}&pageNumber={page_n}"
            f"&sortType=updated&sortDirection=descending"
        )
        try:
            page_data = _http_get_json(url_n)
        except urllib.error.HTTPError:
            # Search-page failure mid-walk: stop expanding, work with
            # what we have. Better than aborting the whole fetch.
            break
        _ingest(page_data)
        time.sleep(_PER_DECK_THROTTLE_SECS)

    if not public_ids:
        raise ValueError(
            "moxfield: search returned 0 publicIds — schema drift?"
        )

    public_ids = public_ids[:target]

    decks: list[ParsedDeck] = []
    seen_slugs: dict[str, int] = {}
    total_pids = len(public_ids)
    tick_every = max(1, total_pids // 25)

    for i, (pid, _name_hint) in enumerate(public_ids, start=1):
        if i == 1 or i % tick_every == 0:
            print(
                f"[moxfield] {i}/{total_pids} probed, {len(decks)} decks",
                file=sys.stderr,
                flush=True,
            )
        try:
            deck_json = _http_get_json(f"{_API_HOST}/v3/decks/all/{pid}")
        except urllib.error.HTTPError:
            continue
        time.sleep(_PER_DECK_THROTTLE_SECS)

        if not isinstance(deck_json, dict):
            continue

        # `format` on the per-deck blob confirms the search-filter
        # contract; if a row leaked under a different format ignore it.
        if deck_json.get("format") != moxfield_fmt:
            continue

        archetype = deck_json.get("name") or pid
        if not isinstance(archetype, str):
            archetype = pid

        entries, unresolved = _entries_from_deck(deck_json, resolve_name)
        if not entries:
            continue

        slug_base = _slug_from_deck(archetype, pid)
        n = seen_slugs.get(slug_base, 0) + 1
        seen_slugs[slug_base] = n
        slug = slug_base if n == 1 else f"{slug_base}-{n}"

        decks.append(ParsedDeck(
            slug=slug,
            archetype=archetype.strip(),
            source="moxfield",
            url=_public_url(pid),
            tier="",          # moxfield is user-built, no tier letters
            winrate=None,     # not published
            sample=None,      # not a metagame sample
            fetched=fetched,
            entries=entries,
            unresolved=unresolved,
        ))

    return decks
