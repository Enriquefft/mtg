"""mtga.untapped.gg metagame parser.

untapped is the only meta source that publishes Brawl decklists at
scale (~1.4K Historic-Brawl archetypes vs. zero on mtgazone /
mtggoldfish / mtgdecks) and also gives the largest sample-counted
ladder corpora for Standard / Historic / Pioneer / Alchemy / Timeless.
That makes it the highest-leverage source in the registry — but it
also requires more plumbing than the static-HTML scrapers because
decklists are encoded as binary V4 deckstrings (base64url + LEB128
varint) referencing untapped's internal `titleId` namespace, not
Scryfall card names.

Resolution chain (single-source-of-truth-preserving):

    deckstring -> [titleId, ...]
                  via mtgajson `loc_en.json`  (titleId -> English name)
                  via mtgajson `cards.json`   (titleId -> {set, cn, grpid})
              -> name -> resolve_name(name)   -> Scryfall printing

We use untapped's own crosswalk for the titleId->name step (the binary
deckstring is their format, so they own that mapping), but every card
that lands in a `DeckEntry` is a Scryfall printing — the project's
"one source of truth: Scryfall" rule still holds because the actual
card identity (set / collector / oracle / legality) comes from Scryfall.

Two HTTP layers:

  1. Index page = the sitemap XML for the format
     (`https://mtga.untapped.gg/sitemap-<fmt>.xml`). One archetype per
     `<url>`; we filter to English entries (lang-prefix-free path).
     Per-format archetype counts (probe, 2026-05-01):
       historic=36, standard=90, pioneer=20, alchemy=8, timeless=19,
       historic-brawl=1470. explorer/brawl/standard-brawl sitemaps
       are stub-empty — untapped doesn't carry those formats — so
       `url_for_format` returns None for them.
     Fetched by `cmd_fetch_meta` via `_fetch_meta_page` (24h cache).

  2. Per-archetype HTTP done inside this module via `http_get_text`:
       a. The archetype page itself (Next.js SSR) — extracts decks
          embedded in `__NEXT_DATA__.props.pageProps.ssrProps.apiDeckData.data`.
          When that array is empty (common for Brawl, where the SSR
          renders the page chrome without preloading decks), we fall
          back to:
       b. The same `decksQueryUrl` that the SSR was about to call:
          `https://api.mtga.untapped.gg/api/v1/analytics/query/
           decks_by_event_scope_and_rank_v2/free?MetaPeriodId=<id>
           &RankingClassScopeFilter=<scope>`. Returns a flat list of
          deck objects across the whole format; we filter by
          `ptg == archetypeId` (the deck's "primary tag group" is the
          archetype id from the page URL).
       c. mtgajson global dumps: `cards.json` (~12 MB) + `loc_en.json`
          (~4.6 MB), fetched once per parse run, cached on disk under
          `data/meta-cache/untapped/global/` so a follow-up parse for
          a different format reuses the same map.

Per-deck shape: untapped does not publish letter tiers; `tier=""`.
Per-archetype-page SSR carries `archetypeTrendsMetaPeriodCurrent.
NORMAL.matches_count_valid.total` as the format-wide ladder sample
size. We don't pull a per-deck winrate (the underlying API endpoint
returns binary deckstrings + tag arrays only, no per-deck win
counts on the /free tier). `winrate=None`, `sample` populated from
the archetype's deck-list response when available.

We take the FIRST deck per archetype (the API returns them in
descending-frequency order). One archetype → one `ParsedDeck` matches
the existing parsers' shape; if the user wants per-deck breadth they
can re-parse with future per-archetype-deck-N selection.

Probe verified 2026-05-01 against:
  * sitemap.xml endpoints for all 9 formats
  * 6 archetype pages (Azorius Control / Galazeth Brawl)
  * `decks_by_event_scope_and_rank_v2/free` endpoint (200, 133 brawl
    decks for MetaPeriodId=711)
  * `mtgajson.untapped.gg/v1/latest/{cards.json, loc_en.json}`

Stdlib only — V4 deckstring decoder is ~25 LOC inline.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
from pathlib import Path
from typing import Callable

from ._common import DeckEntry, ParsedDeck, http_get_text, slugify

# --- format -> sitemap URL ----------------------------------------------

# Our `brawl` is Historic Brawl per the Scryfall convention (see
# CLAUDE.md "Format-name gotcha" + docs/historic.md). untapped routes
# Historic Brawl under `historic-brawl`, so we map `brawl` -> that slug.
# Pioneer's archetype pages live at `/constructed/pioneer/...` even
# though their deck-API event_name is `Explorer_Ladder` (untapped
# treats Pioneer/Explorer as one ladder); the sitemap URL still has
# `pioneer` in it so the per-format mapping stays simple.
#
# explorer / brawl (= Standard Brawl) / standard-brawl: untapped's
# sitemaps for these are stubs (zero archetype URLs in the probe),
# meaning untapped doesn't publish a tier list for them. Returning
# None here lets `cmd_fetch_meta` exit-2 with the "source does not
# publish a tier list for format X" error path rather than fetching
# an empty sitemap and exit-1'ing as drift.
_FMT_TO_SITEMAP_SLUG: dict[str, str] = {
    "standard":  "standard",
    "historic":  "historic",
    "pioneer":   "pioneer",
    "alchemy":   "alchemy",
    "timeless":  "timeless",
    "brawl":     "historic-brawl",  # our `brawl` = Historic Brawl
}


def url_for_format(fmt: str) -> str | None:
    """URL of untapped's sitemap for `fmt`, or None if unsupported.

    untapped's sitemap index lives at `/sitemap.xml`; per-format
    archetype sitemaps are sub-resources at
    `/sitemap/constructed-archetypes.xml?format=<slug>`. Verified
    2026-05-01: all 9 documented format slugs return HTTP 200, but
    `explorer` / `brawl` (= Standard Brawl) / `standard-brawl` return
    a 219-byte empty `<urlset>` stub (untapped doesn't carry those
    formats). We still URL them — the parser surfaces "0 archetypes"
    via `cmd_fetch_meta`'s "0 decks extracted" drift error, which
    accurately reflects the source.
    """
    slug = _FMT_TO_SITEMAP_SLUG.get(fmt)
    if slug is None:
        return None
    return (
        f"https://mtga.untapped.gg/sitemap/"
        f"constructed-archetypes.xml?format={slug}"
    )


# --- on-disk cache for global mtgajson dumps ----------------------------

# Mirrors `tools/mtg.py:_meta_cache_path` shape (`data/meta-cache/<source>/`)
# but namespaced under `global/` so per-format archetype-page caches
# (which use sha256(url)[:16] like `_fetch_meta_page`) can never collide
# with these named files. The two mtgajson dumps are stable across all
# format runs — no point re-fetching 17 MB once per format.
_GLOBAL_CACHE_TTL_SECS = 24 * 3600
_PAGE_CACHE_TTL_SECS = 24 * 3600

_MTGAJSON_CARDS_URL  = "https://mtgajson.untapped.gg/v1/latest/cards.json"
_MTGAJSON_LOC_EN_URL = "https://mtgajson.untapped.gg/v1/latest/loc_en.json"


def _data_dir() -> Path:
    """Resolve `data/` relative to this file (mirrors tools/mtg.py:DATA)."""
    return Path(os.environ.get("MTG_ROOT") or
                Path(__file__).resolve().parent.parent.parent) / "data"


def _cached_get_text(url: str, *, name: str | None = None,
                     ttl_secs: int = _PAGE_CACHE_TTL_SECS) -> str:
    """Fetch `url`, honouring an on-disk cache under `data/meta-cache/untapped/`.

    `name` overrides the auto-derived filename — used for the two
    global mtgajson dumps (`cards.json`, `loc_en.json`) so the same
    file is reused across every format/archetype walk. When `name`
    is None the cache key is sha256(url)[:16] under `pages/`,
    matching `_meta_cache_path`'s convention.
    """
    cache_root = _data_dir() / "meta-cache" / "untapped"
    if name is not None:
        cache_path = cache_root / "global" / name
    else:
        import hashlib
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        cache_path = cache_root / "pages" / f"{digest}.html"

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age <= ttl_secs:
            return cache_path.read_text(encoding="utf-8", errors="replace")

    text = http_get_text(url, accept="application/json,text/html;q=0.9")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, cache_path)
    return text


# --- titleId resolution -------------------------------------------------

# Cached across multiple parse_untapped() invocations within one process
# (the same Python invocation may legitimately fetch multiple formats
# in a row via a future batch-mode caller).
_GLOBAL_TITLEID_MAP: dict[int, str] | None = None


def _load_titleid_to_name() -> dict[int, str]:
    """Build {titleId: english_name} from cached mtgajson `loc_en.json`.

    `loc_en.json` is `[{"id": <titleId>, "text": <name>}]` — same
    schema verified across multiple snapshots. Empty `text` (e.g.
    placeholder strings) is dropped so we never try to resolve `""`
    against Scryfall.
    """
    global _GLOBAL_TITLEID_MAP
    if _GLOBAL_TITLEID_MAP is not None:
        return _GLOBAL_TITLEID_MAP
    raw = _cached_get_text(_MTGAJSON_LOC_EN_URL, name="loc_en.json",
                           ttl_secs=_GLOBAL_CACHE_TTL_SECS)
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(
            f"mtgajson loc_en.json: expected list, got {type(data).__name__}"
        )
    out: dict[int, str] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        tid = row.get("id")
        text = row.get("text")
        if isinstance(tid, int) and isinstance(text, str) and text:
            out[tid] = text
    if not out:
        raise ValueError("mtgajson loc_en.json: zero usable {id, text} rows")
    _GLOBAL_TITLEID_MAP = out
    return out


# --- V4 deckstring decoder ---------------------------------------------

class _Reader:
    """Minimal byte-stream reader with LEB128 varint support."""

    __slots__ = ("buf", "i")

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.i = 0

    def byte(self) -> int:
        b = self.buf[self.i]
        self.i += 1
        return b

    def varint(self, *, soft: bool = False) -> int:
        """LEB128 unsigned varint. `soft=True` returns 0 at EOF (used as
        terminator probe for the section loop)."""
        if soft and self.i >= len(self.buf):
            return 0
        result = 0
        shift = 0
        while True:
            b = self.buf[self.i]
            self.i += 1
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                return result
            shift += 7


def _b64url_decode(s: str) -> bytes:
    """Padding-tolerant base64url decode (untapped omits trailing `=`)."""
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _read_bucket(r: _Reader, qty_implicit: int | None) -> list[int]:
    """One quantity-bucket: cumulative-delta titleIds, all at given quantity.

    Used by `_read_board` to walk the five quantity-buckets (1, 2, 3, 4,
    explicit) that V4 uses to compress mainboard counts. For commanders
    + companions a single bucket with `qty_implicit=None` is used
    (each entry has its own quantity, and a mechanism tag follows).
    """
    out: list[int] = []
    n = r.varint()
    cum = 0
    for _ in range(n):
        qty = qty_implicit if qty_implicit is not None else r.varint()
        cum += r.varint()
        out.extend([cum] * qty)
    return out


def _read_board(r: _Reader) -> list[int]:
    """Mainboard / sideboard / wishboard: five quantity-buckets in order."""
    out: list[int] = []
    for q in (1, 2, 3, 4, None):
        out.extend(_read_bucket(r, q))
    return out


def _read_commanders_companions(r: _Reader) -> tuple[list[int], list[int]]:
    """Special bucket: each entry is (delta, mechanism_tag) pair.

    Tag 1 = commander, tag 2 = companion. Quantity is implicit-1
    (you can't have two of the same commander; companions ride sidecar).
    """
    n = r.varint()
    cum = 0
    by_mech: dict[int, list[int]] = {}
    for _ in range(n):
        cum += r.varint()
        mech = r.varint()
        by_mech.setdefault(mech, []).append(cum)
    return by_mech.get(1, []), by_mech.get(2, [])


def _decode_v4(deckstring: str) -> dict:
    """Decode an untapped V4 deckstring into titleId lists.

    Returns `{"commanders": [tid], "companions": [tid],
              "main": [tid * qty], "side": [tid * qty]}`.
    Raises ValueError if magic byte / version are wrong.
    """
    raw = _b64url_decode(deckstring)
    r = _Reader(raw)
    if r.byte() != 0x00:
        raise ValueError("V4 deckstring: missing 0x00 magic byte")
    ver = r.varint()
    if ver != 4:
        raise ValueError(f"V4 deckstring: unsupported version {ver}")
    cmds, comps = _read_commanders_companions(r)
    sections: dict[int, list[int]] = {1: [], 2: [], 3: []}
    while True:
        sec = r.varint(soft=True)
        if sec == 0:
            break
        if sec not in sections:
            # Future section ID — read+discard one board so the stream
            # advances to the next sentinel rather than mis-aligning.
            _read_board(r)
            continue
        sections[sec] = _read_board(r)
    return {
        "commanders": cmds,
        "companions": comps,
        "main": sections[1],
        "side": sections[2],
        # sections[3] = wishboard, intentionally dropped (Brawl-only,
        # no MTGA export concept; sideboard captures what we need).
    }


# --- sitemap walking ---------------------------------------------------

# Each `<url>` block contains a single `<loc>` plus several
# `<xhtml:link rel="alternate" hreflang="<lang>" href="..."/>`
# alternates. The English archetype URL is the one whose path has no
# language prefix (`/constructed/...` vs. `/de/constructed/...`).
# We extract just the canonical `<loc>` value and parse the path to
# pull out the numeric archetype id and the slug — both load-bearing.
_LOC_RE = re.compile(r"<loc>(https://mtga\.untapped\.gg/[^<]+)</loc>",
                     re.IGNORECASE)
_ARCH_PATH_RE = re.compile(
    r"^https://mtga\.untapped\.gg/constructed/[a-z0-9-]+/archetypes/"
    r"(\d+)/([a-z0-9-]+)/?$",
    re.IGNORECASE,
)


def _enumerate_archetypes(sitemap_xml: str) -> list[tuple[int, str, str]]:
    """Return ordered, de-duplicated `(archetypeId, slug, url)` tuples.

    Filters to English-only canonical URLs (no `/de/`, `/es/`, etc.
    language prefix) by matching the archetype path regex strictly.
    Preserves sitemap order so the corpus is reproducible run-to-run.
    """
    seen: set[int] = set()
    out: list[tuple[int, str, str]] = []
    for m in _LOC_RE.finditer(sitemap_xml):
        url = m.group(1)
        path_m = _ARCH_PATH_RE.match(url)
        if not path_m:
            continue
        aid = int(path_m.group(1))
        slug = path_m.group(2)
        if aid in seen:
            continue
        seen.add(aid)
        out.append((aid, slug, url))
    return out


# --- archetype page parsing --------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

_API_BASE = "https://api.mtga.untapped.gg/api/v1"

# untapped's analytics API uses an async-query pattern: the first call
# returns HTTP 202 + an empty body while their backend runs the query;
# subsequent calls hit the cached result and return 200 + the deck list.
# Poll up to N times with linear backoff. 5 attempts × 2s = 10s max
# wait per archetype, which is enough for warm queries (most return on
# attempt 2) and keeps a `--limit 5` Standard fetch under a minute even
# in the cold case where every archetype is being queried for the first
# time. `http_get_text` only raises on 4xx/5xx, so 202 surfaces here as
# a successful empty-body return; we have to detect it via
# `urlopen.status` directly.
_API_POLL_ATTEMPTS = 5
_API_POLL_DELAY_SECS = 2.0


def _api_get_decks(api_url: str) -> list[dict] | None:
    """GET an analytics API URL, polling on 202 until 200 or timeout.

    Returns the parsed deck list on success, None if every poll
    returned 202 (untapped's backend never finalised the query).
    Network/HTTPError on the underlying GET propagates up to the
    caller so a single 5xx drops just the archetype, not the run.
    """
    import urllib.request
    headers = {
        "User-Agent": "mtg-toolkit/0.1 (github.com/Enriquefft/mtg)",
        "Accept": "application/json",
    }
    for attempt in range(_API_POLL_ATTEMPTS):
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            status = r.status
            body = r.read().decode("utf-8", errors="replace")
        if status == 200 and body:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, list) else None
        # 202 or 200 with empty body -> poll again.
        if attempt < _API_POLL_ATTEMPTS - 1:
            time.sleep(_API_POLL_DELAY_SECS)
    return None


def _fetch_archetype_decks(
    archetype_url: str, archetype_id: int, fmt: str,
) -> tuple[list[dict], str | None, int | None]:
    """Return `(decks, archetype_name, sample)` for one archetype URL.

    Three possible paths, tried in order of cheapness:

      1. SSR-embedded decks. Historic / Brawl / Alchemy / Timeless /
         Pioneer all server-render the first 6-8 decks per archetype
         into `__NEXT_DATA__.props.pageProps.ssrProps.apiDeckData.data`.
         Zero HTTP cost beyond the page itself, complete data.

      2. SSR-suggested API URL. When `apiDeckData.data` is empty (Brawl
         post-set-rotation) we follow the page's own `decksQueryUrl`
         and filter the global response by `ptg == archetype_id`.

      3. Format-wide API. When SSR is `None` (Standard, as of 2026-05-01
         — the page is fully client-rendered with `clientProps.format`
         + `clientProps.primaryTagGroup`), we resolve the format's
         active meta-period via `meta-periods/active` and hit the same
         `decks_by_event_scope_and_rank_v2/free` endpoint directly,
         again filtering by `ptg`. Cached per-format so all 90
         Standard archetypes share one API call (+ one poll on 202).
    """
    page_html = _cached_get_text(archetype_url)
    m = _NEXT_DATA_RE.search(page_html)
    if not m:
        return [], None, None

    try:
        next_data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return [], None, None

    page_props = (next_data.get("props", {}).get("pageProps", {}))
    ssr = page_props.get("ssrProps")  # may be None — see path 3

    if isinstance(ssr, dict):
        archetype_name = _ssr_archetype_name(ssr)
        sample = _ssr_sample_count(ssr)

        # Path 1: SSR has decks already.
        decks = ((ssr.get("apiDeckData") or {}).get("data") or [])
        if decks:
            return list(decks), archetype_name, sample

        # Path 2: SSR points us at the API URL.
        decks_query = ssr.get("decksQueryUrl")
        if decks_query:
            api_decks = _api_get_decks(_API_BASE + decks_query)
            if api_decks is not None:
                matching = [d for d in api_decks
                            if isinstance(d, dict) and d.get("ptg") == archetype_id]
                return matching, archetype_name, sample
        return [], archetype_name, sample

    # Path 3: SSR is None — fully client-rendered page (Standard).
    api_decks = _format_wide_decks(fmt)
    if api_decks is None:
        return [], None, None
    matching = [d for d in api_decks
                if isinstance(d, dict) and d.get("ptg") == archetype_id]
    return matching, None, None


# Per-format API cache: untapped's `decks_by_event_scope_and_rank_v2`
# is global per format/meta-period — one call returns every archetype's
# decks for that combination. Caching at the format level means walking
# 90 Standard archetypes is one API call + one poll, not 90.
_FORMAT_API_CACHE: dict[str, list[dict] | None] = {}


def _format_wide_decks(fmt: str) -> list[dict] | None:
    """Return the cached format-wide deck list (path 3 in `_fetch_archetype_decks`).

    Resolves the active `MetaPeriodId` via `meta-periods/active`,
    picks the highest-id active period for the format's `event_name`,
    then GETs the analytics endpoint with poll-on-202. Result cached
    in process so subsequent archetype lookups within the same format
    walk are O(1).
    """
    if fmt in _FORMAT_API_CACHE:
        return _FORMAT_API_CACHE[fmt]
    event_name = _FMT_TO_EVENT_NAME.get(fmt)
    if not event_name:
        _FORMAT_API_CACHE[fmt] = None
        return None
    try:
        periods_text = _cached_get_text(
            f"{_API_BASE}/meta-periods/active",
            name="meta-periods.json",
            ttl_secs=3600,  # period rotates on set release; 1h cache OK
        )
        periods = json.loads(periods_text)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        _FORMAT_API_CACHE[fmt] = None
        return None
    if not isinstance(periods, list):
        _FORMAT_API_CACHE[fmt] = None
        return None
    matching = [p for p in periods
                if isinstance(p, dict)
                and p.get("event_name") == event_name
                and p.get("end_ts") is None]
    if not matching:
        _FORMAT_API_CACHE[fmt] = None
        return None
    # Highest id = current period (untapped issues monotonic ids).
    matching.sort(key=lambda p: -int(p.get("id", 0)))
    period_id = matching[0]["id"]
    api_url = (
        f"{_API_BASE}/analytics/query/decks_by_event_scope_and_rank_v2/free"
        f"?MetaPeriodId={period_id}&RankingClassScopeFilter=ALL"
    )
    try:
        decks = _api_get_decks(api_url)
    except (urllib.error.HTTPError, urllib.error.URLError):
        decks = None
    _FORMAT_API_CACHE[fmt] = decks
    return decks


# Format -> untapped `event_name` for the analytics endpoint. Used by
# path 3 (SSR-None archetype pages, currently Standard). Pioneer maps
# to `Explorer_Ladder` because untapped tracks the two formats under
# one ladder bucket — confirmed via probe: a Pioneer archetype page's
# `decksQueryUrl` references `MetaPeriodId=703` whose `event_name` is
# `Explorer_Ladder`. Brawl maps to `Play_Brawl_Historic` (the actual
# event Brawl matches log to in MTGA telemetry).
_FMT_TO_EVENT_NAME: dict[str, str] = {
    "standard": "Ladder",
    "historic": "Historic_Ladder",
    "pioneer":  "Explorer_Ladder",
    "alchemy":  "Alchemy_Ladder",
    "timeless": "Timeless_Ladder",
    "brawl":    "Play_Brawl_Historic",
}


def _ssr_archetype_name(ssr: dict) -> str | None:
    """Extract a human-readable archetype name from SSR.

    `archetypeTags.data` is a list of tag objects with a `name`. The
    untapped UI shows the first tag (the archetype itself) as the
    page title; subsequent tags are colour / shell labels.
    """
    tags = ((ssr.get("archetypeTags") or {}).get("data") or [])
    if not tags or not isinstance(tags, list):
        return None
    first = tags[0]
    if isinstance(first, dict):
        nm = first.get("name")
        if isinstance(nm, str) and nm:
            return nm
    return None


def _ssr_sample_count(ssr: dict) -> int | None:
    """Pull the format-wide valid match count from SSR if present.

    Per-archetype deck count isn't surfaced on SSR (the API endpoint
    just returns deck objects), so we use the meta-period's valid
    match total as the sample. It's a conservative ceiling — the
    actual archetype slice is smaller — but it's the only sample
    number untapped publishes consistently and matches the CLI's
    "sample = source-published number, no synthesis" convention.
    """
    mp = ssr.get("archetypeTrendsMetaPeriodCurrent")
    if not isinstance(mp, dict):
        return None
    normal = mp.get("NORMAL")
    if not isinstance(normal, dict):
        return None
    valid = normal.get("matches_count_valid")
    if not isinstance(valid, dict):
        return None
    n = valid.get("total")
    if isinstance(n, int) and n > 0:
        return n
    return None


# --- deckstring -> DeckEntry list --------------------------------------

def _entries_from_deckstring(
    deckstring: str,
    titleid_to_name: dict[int, str],
    resolve_name: Callable[[str], dict | None],
) -> tuple[list[DeckEntry], int]:
    """Decode and resolve one deckstring. Returns `(entries, unresolved)`.

    Resolution failures (titleId missing from loc_en, or name unknown
    to Scryfall) are counted as `unresolved` so the deck file is short
    rather than wrong. Per-card stderr would be noisy across a 30-deck
    fetch — one integer per deck is the project convention.

    Aggregates duplicate titleIds into a single DeckEntry per (name,
    section): the V4 decoder produces one entry per copy, but MTGA
    deck files use `<count> <name>` lines.
    """
    decoded = _decode_v4(deckstring)
    entries: list[DeckEntry] = []
    unresolved = 0

    def _emit(tids: list[int], section: str) -> None:
        nonlocal unresolved
        # Aggregate counts per titleId to collapse copies; preserve order
        # of first appearance for stable diffs.
        order: list[int] = []
        counts: dict[int, int] = {}
        for tid in tids:
            if tid not in counts:
                order.append(tid)
                counts[tid] = 0
            counts[tid] += 1
        for tid in order:
            qty = counts[tid]
            name = titleid_to_name.get(tid)
            if not name:
                unresolved += qty
                continue
            printing = resolve_name(name)
            if printing is None:
                unresolved += qty
                continue
            set_code = (printing.get("set") or "").upper()
            collector = printing.get("collector_number") or ""
            if not set_code or not collector:
                unresolved += qty
                continue
            entries.append(DeckEntry(
                count=qty, name=name, set_code=set_code,
                collector=collector, section=section,
            ))

    _emit(decoded["commanders"], "commander")
    _emit(decoded["companions"], "companion")
    _emit(decoded["main"], "deck")
    _emit(decoded["side"], "sideboard")
    return entries, unresolved


# --- main entry point --------------------------------------------------

def parse_untapped(
    raw_xml: str,
    fmt: str,
    *,
    fetched: str,
    url: str,
    resolve_name: Callable[[str], dict | None],
    limit: int | None = None,
    **_: object,
) -> list[ParsedDeck]:
    """Parse untapped's per-format sitemap into `ParsedDeck` list.

    `raw_xml` is the sitemap.xml body (fetched + cached by
    `cmd_fetch_meta._fetch_meta_page`). For each English archetype
    URL we fetch the archetype page, extract its first deck (SSR or
    API-fallback), decode the V4 deckstring, and emit one `ParsedDeck`.

    `limit` is the soft hint from `--limit N` — when set, we stop
    fetching archetype pages once we've accumulated N successful
    decks. Critical for `historic-brawl` (1470 archetypes × 2 HTTP
    requests each = ~12 minutes wall-time at full walk); without
    the short-circuit `--limit 5` would still walk all 1470 only
    to be sliced to 5 by `cmd_fetch_meta`.

    Empty result list is the caller's drift signal — `cmd_fetch_meta`
    surfaces the "0 decks extracted from a 200 response" error. We
    raise ValueError only when the data layout we depend on is itself
    missing (e.g. mtgajson loc_en.json schema break) — sub-resource
    failures (per-archetype 4xx/5xx) drop just that archetype.
    """
    titleid_to_name = _load_titleid_to_name()

    archetypes = _enumerate_archetypes(raw_xml)
    if not archetypes:
        # Sitemap parsed but no archetype URLs — bubble up as drift.
        return []

    decks: list[ParsedDeck] = []
    seen_slugs: dict[str, int] = {}

    for aid, slug, archetype_url in archetypes:
        if limit is not None and limit > 0 and len(decks) >= limit:
            break
        try:
            api_decks, archetype_name, sample = _fetch_archetype_decks(
                archetype_url, aid, fmt,
            )
        except (urllib.error.HTTPError, urllib.error.URLError):
            # Single archetype fetch failed; keep walking the rest. The
            # corpus-wide drift check at cmd_fetch_meta layer catches
            # the case where every archetype fails.
            continue

        if not api_decks:
            continue

        # First deck = highest-frequency for this archetype (untapped
        # API returns descending order). Matches mtgdecks.net's
        # one-deck-per-archetype shape so the corpus is comparable.
        first_deck = api_decks[0]
        deckstring = first_deck.get("ds")
        if not isinstance(deckstring, str) or not deckstring:
            continue

        try:
            entries, unresolved = _entries_from_deckstring(
                deckstring, titleid_to_name, resolve_name,
            )
        except ValueError:
            # V4 decode failed for this one deck (magic / version).
            # Skip — almost certainly corrupt input, not a parser bug.
            continue
        if not entries:
            continue

        # Slug from the URL (already lowercase, hyphenated, ASCII).
        # De-dup by appending -2, -3 if untapped reused a slug across
        # archetype IDs (very rare; mostly happens for "boros-burn"
        # variants that diverged in the meta period).
        n = seen_slugs.get(slug, 0) + 1
        seen_slugs[slug] = n
        out_slug = slug if n == 1 else f"{slug}-{n}"

        archetype_display = archetype_name or _slug_to_archetype(slug)

        decks.append(ParsedDeck(
            slug=out_slug,
            archetype=archetype_display,
            source="untapped",
            url=archetype_url,
            tier="",                # untapped publishes no letter tiers
            winrate=None,           # not on /free tier endpoints
            sample=sample,
            fetched=fetched,
            entries=entries,
            unresolved=unresolved,
        ))

    return decks


def _slug_to_archetype(slug: str) -> str:
    """Fall-back human name when SSR archetype tag is missing.

    `azorius-control` -> `Azorius Control`. Used only when both SSR
    paths fail to surface a tag list — the shape stays consistent
    with sources that always carry an archetype string.
    """
    return slugify(slug).replace("-", " ").title()
