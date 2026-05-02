"""archidekt.com user-deck search parser.

Archidekt exposes a public REST API (archidekt.com/api) used by its
React frontend. We fetch individual decks via:

    https://archidekt.com/api/decks/<deck_id>/

to enumerate decks per format. Archidekt's search API (`/api/decks/?...`)
appears to be restricted to internal use; the workaround is to scrape deck
IDs from the public search page HTML and fetch each individually.

Format mapping (archidekt deckFormat codes → Arena format names):

    standard      -> 1   (Standard)
    alchemy       -> 8   (Alchemy)
    historic      -> 19  (Historic)
    timeless      -> 18  (Timeless)
    pioneer       -> 6   (Pioneer)
    brawl         -> 20  (Historic Brawl)

Throttling: 0.3s between per-deck fetches. Archidekt's infrastructure
is more lenient than Cloudflare-protected moxfield; deck fetches are
lightweight and the platform doesn't publish usage limits.

Winrate / sample: not surfaced by Archidekt (these are user-built decks,
not metagame samples). Both fields stay None; the source's strength is
corpus depth and novelty (brewers vs. meta-optimized lists).

Card normalization: Archidekt stores canonicalized Scryfall UIDs in the
`oracleCard` blob; we extract `oracleCard.name` which is already in
Scryfall's canonical form (handling multi-face cards correctly).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
from typing import Callable

from ._common import DeckEntry, ParsedDeck, http_get_text, slugify

_API_HOST = "https://archidekt.com/api"
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_SEARCH_PAGE_SIZE = 50
_PER_DECK_THROTTLE_SECS = 0.3
_MAX_SEARCH_PAGES = 200

_FORMAT_MAP = {
    "standard": 1,
    "alchemy": 8,
    "historic": 19,
    "timeless": 18,
    "pioneer": 6,
    "brawl": 20,
}


def url_for_format(fmt: str) -> str | None:
    archidekt_fmt = _FORMAT_MAP.get(fmt)
    if archidekt_fmt is None:
        return None
    return (
        f"https://archidekt.com/search/decks/"
        f"?deckFormat={archidekt_fmt}&sort=-updated"
    )


def _http_get_json(url: str) -> dict:
    text = http_get_text(
        url,
        accept="application/json, text/plain, */*",
        user_agent=_BROWSER_UA,
    )
    return json.loads(text)


def _extract_deck_ids_from_html(html: str) -> list[str]:
    deck_id_pattern = re.compile(r'href="/decks/(\d+)/')
    matches = deck_id_pattern.findall(html)
    return list(dict.fromkeys(matches))


def _slug_from_deck(name_hint: str, deck_id: str) -> str:
    name_slug = slugify(name_hint) if name_hint else ""
    if len(name_slug) < 4:
        name_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", deck_id).strip("-").lower()
        return name_slug or "deck"
    id_safe = re.sub(r"[^a-zA-Z0-9]+", "", deck_id).lower()[:6]
    return f"{name_slug}-{id_safe}" if id_safe else name_slug


_CATEGORY_TO_SECTION = {
    "Commander": "commander",
    "Companion": "companion",
    "Sideboard": "sideboard",
}


def _entries_from_deck(
    deck_json: dict,
    resolve_name: Callable[[str], dict | None],
) -> tuple[list[DeckEntry], int]:
    out: list[DeckEntry] = []
    unresolved = 0
    cards = deck_json.get("cards") or []
    if not isinstance(cards, list):
        return out, unresolved

    for card_entry in cards:
        if not isinstance(card_entry, dict):
            continue
        quantity = card_entry.get("quantity")
        if not isinstance(quantity, int) or quantity <= 0:
            continue

        card = card_entry.get("card") or {}
        if not isinstance(card, dict):
            continue

        oracle_card = card.get("oracleCard") or {}
        if not isinstance(oracle_card, dict):
            continue

        front_name = oracle_card.get("name")
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

        categories = card_entry.get("categories") or []
        section = "deck"
        if isinstance(categories, list):
            for cat in categories:
                if cat in _CATEGORY_TO_SECTION:
                    section = _CATEGORY_TO_SECTION[cat]
                    break

        out.append(DeckEntry(
            count=quantity,
            name=canonical_name,
            set_code=set_code,
            collector=collector,
            section=section,
        ))

    return out, unresolved


def _public_url(deck_id: str) -> str:
    return f"https://archidekt.com/decks/{deck_id}/"


def parse_archidekt(
    raw_text: str,
    fmt: str,
    *,
    fetched: str,
    url: str,
    resolve_name: Callable[[str], dict | None],
    limit: int | None = None,
    **_: object,
) -> list[ParsedDeck]:
    archidekt_fmt = _FORMAT_MAP.get(fmt)
    if archidekt_fmt is None:
        return []

    target = limit if (limit is not None and limit > 0) else 50

    deck_ids: list[tuple[str, str]] = []
    seen: set[str] = set()

    deck_ids_from_html = _extract_deck_ids_from_html(raw_text)

    for deck_id in deck_ids_from_html:
        if deck_id in seen or len(deck_ids) >= target:
            continue
        seen.add(deck_id)
        deck_ids.append((deck_id, ""))

    if not deck_ids:
        raise ValueError(
            "archidekt: search page returned 0 deck IDs — page format drift?"
        )

    deck_ids = deck_ids[:target]

    decks: list[ParsedDeck] = []
    seen_slugs: dict[str, int] = {}
    total_ids = len(deck_ids)
    tick_every = max(1, total_ids // 25)

    for i, (deck_id, _name_hint) in enumerate(deck_ids, start=1):
        if i == 1 or i % tick_every == 0:
            print(
                f"[archidekt] {i}/{total_ids} probed, {len(decks)} decks",
                file=sys.stderr,
                flush=True,
            )
        try:
            deck_json = _http_get_json(f"{_API_HOST}/decks/{deck_id}/")
        except urllib.error.HTTPError:
            continue
        time.sleep(_PER_DECK_THROTTLE_SECS)

        if not isinstance(deck_json, dict):
            continue

        if deck_json.get("deckFormat") != archidekt_fmt:
            continue

        archetype = deck_json.get("name") or deck_id
        if not isinstance(archetype, str):
            archetype = deck_id

        entries, unresolved = _entries_from_deck(deck_json, resolve_name)
        if not entries:
            continue

        slug_base = _slug_from_deck(archetype, deck_id)
        n = seen_slugs.get(slug_base, 0) + 1
        seen_slugs[slug_base] = n
        slug = slug_base if n == 1 else f"{slug_base}-{n}"

        decks.append(ParsedDeck(
            slug=slug,
            archetype=archetype.strip(),
            source="archidekt",
            url=_public_url(deck_id),
            tier="",
            winrate=None,
            sample=None,
            fetched=fetched,
            entries=entries,
            unresolved=unresolved,
        ))

    return decks
