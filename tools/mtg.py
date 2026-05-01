#!/usr/bin/env python3
"""mtg — local query layer over Scryfall bulk data.

Single source of truth: Scryfall `default_cards.json` (refreshed daily).
All deck validation runs offline against the cached bulk; only `sync` and
`search` hit the network.

Subcommands:
    sync                          refresh bulk + rebuild name index
    card <name>                   full card info
    printing <SET> <NUM>          lookup by MTGA-style set+collector
    legal <name> <format>         yes/no legality with reason
    validate <deck.txt> -f F      parse + validate MTGA deck file
    analyze <deck.txt>            composition breakdown (curve, role mix, CA)
    related <name> [-f F]         cards sharing each keyword with the anchor
    manabase <deck.txt>           pip demand + color sources + etb-tapped lands
    wildcards <deck.txt>          rarity breakdown for MTGA wildcard cost
    companion <deck.txt>          per-companion eligibility check
    check <deck.txt> [-f F]       run validate+analyze+manabase+wildcards+companion
    search <scryfall-query>       live Scryfall search (one HTTP request)
    collection                    summary of current data/collection.json
    collection dump               full snapshot via DLL injection into MTGA
    collection import <FILE>      import a tracker export (CSV/JSON)
    collection from-decks         lower-bound snapshot from Player.log decks
    own <name>                    show owned count for a card
    owned <scryfall-query>        list owned cards matching a Scryfall query
    gaps <deck.txt>               cards short for a deck + wildcard cost
    coverage <deck.txt>           % of deck you can build right now
    diff <a.txt> <b.txt>          per-card delta between two deck files
    suggest-subs <deck.txt> -f F  propose owned replacements for missing cards
    fetch-meta <format>           scrape a meta source -> data/corpus/<fmt>/ + meta.json
    freq <format>                 card-frequency index over data/corpus/<fmt>/*.txt
    shells --format F             cluster owned cards by keyword/type/theme
    recommend --format F          rank corpus decks you can build + shell bridge

Run `mtg <subcommand> --help` for details.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob as glob_mod
import hashlib
import io
import json
import os
import pickle
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Per-source meta parsers live alongside this file in `tools/mtg_sources/`.
# The package isn't on sys.path by default (tools/ is a script dir, not a
# package root), so wire it in before the import. Spec named the package
# `tools/mtg/sources/`, but `tools/mtg` is a bash wrapper file — directory
# at that path would shadow it and break the documented `tools/mtg <cmd>`
# UX. The single-segment rename preserves SSOT and the published CLI.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mtg_sources import _common  # noqa: E402  (import-after-sys.path)
from mtg_sources._common import (  # noqa: E402
    DECK_LINE_RE,
    SECTION_HEADERS,
    MULTIFACE_LAYOUTS,
    USER_AGENT,
    DeckEntry,
    ParsedDeck,
    slugify,
)
from mtg_sources.aetherhub import (  # noqa: E402
    parse_aetherhub,
    url_for_format as aetherhub_url_for_format,
)
from mtg_sources.moxfield import (  # noqa: E402
    parse_moxfield,
    url_for_format as moxfield_url_for_format,
)
from mtg_sources.mtgazone import (  # noqa: E402
    parse_mtgazone,
    url_for_format as mtgazone_url_for_format,
)
from mtg_sources.mtggoldfish import (  # noqa: E402
    parse_mtggoldfish,
    url_for_format as mtggoldfish_url_for_format,
)
from mtg_sources.mtgdecks import (  # noqa: E402
    parse_mtgdecks,
    url_for_format as mtgdecks_url_for_format,
)
from mtg_sources.untapped import (  # noqa: E402
    parse_untapped,
    url_for_format as untapped_url_for_format,
)

ROOT = Path(os.environ.get("MTG_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
BULK_JSON = DATA / "default_cards.json"
INDEX_PKL = DATA / "index.pkl"
META_JSON = DATA / "bulk-meta.json"
STRICTLYBETTER_CACHE = DATA / "strictlybetter-cache.json"
# Meta-deck corpus root: machine-managed scrapes from `fetch-meta`. Each
# format gets `data/corpus/<fmt>/{*.txt, meta.json, _freq.json}`. Tracked
# human drafts live separately under `decks/<name>/v*.txt` (different
# namespace, different lifecycle). Gitignored via `data/.gitignore`.
CORPUS = DATA / "corpus"

SCRYFALL_BULK = "https://api.scryfall.com/bulk-data/default-cards"
SCRYFALL_API = "https://api.scryfall.com"

# strictlybetter.eu — community-curated functional-reprint + direct-upgrade
# database. Used by `suggest-subs` to prefer rules-text-equivalent owned
# alternatives over heuristic role/CMC matches. Both endpoints are bulk:
#   * /api/functional_reprints — single page (per_page=200, last_page=1).
#   * /api/obsoletes           — paginated (per_page server-capped at 50,
#     ~384 pages / ~19,200 rows total). The per-card variant
#     `/api/obsoletes/<name>` is partial-match on the *inferiors* side
#     only and CANNOT be used to find rows where `<name>` is the
#     superior, so we fetch the full corpus once and build a reverse
#     index `superior_lc -> {inferior_names}` in memory.
# Both are cached at STRICTLYBETTER_CACHE with a 7-day TTL.
# API guide: https://www.strictlybetter.eu/api-guide
STRICTLYBETTER_API = "https://www.strictlybetter.eu/api"
STRICTLYBETTER_TTL_S = 7 * 24 * 3600
STRICTLYBETTER_THROTTLE_S = 0.65  # under 100 req/min cap
STRICTLYBETTER_OBSOLETES_PER_PAGE = 50  # server-cap; values above are clamped
# Schema version for the on-disk cache. Bump when the meaning of a cache
# field changes so a stale cache from a prior schema is dropped instead
# of silently serving wrong-direction data.
#   v1 — initial: obsoletes returned UPGRADES (wrong for sub-suggest)
#   v2 — obsoletes per-card flipped to DOWNGRADES (still broken: the
#        per-card endpoint can't surface rows where the queried card is
#        the superior, so most downgrades are missed)
#   v3 — obsoletes is now bulk-fetched once (raw row list under
#        cache["obsoletes"]={"fetched_at","rows"}) and a reverse index
#        is built in memory; correct for sub-suggest in both directions
STRICTLYBETTER_CACHE_SCHEMA = 3

ARENA_FORMATS = {
    "standard",
    "standardbrawl",
    "historic",
    "brawl",  # Scryfall's `brawl` = Historic Brawl on Arena
    "alchemy",
    "timeless",
    "pioneer",
    "explorer",
}

# fields we keep in the local index (drop image URIs, prices, etc. to shrink)
KEEP_FIELDS = (
    "name",
    "oracle_id",
    "arena_id",  # MTGA-internal numeric id; required for collection export
    "set",
    "collector_number",
    "mana_cost",
    "cmc",
    "type_line",
    "oracle_text",
    "colors",
    "color_identity",
    "produced_mana",
    "power",
    "toughness",
    "loyalty",
    "keywords",
    "games",
    "legalities",
    "layout",
    "card_faces",
    "rarity",
    "released_at",
    "game_changer",
)

# Bump when the index dict schema changes (new field, dropped field,
# changed key shape). `_load_index` auto-rebuilds on mismatch so old
# pickles can't silently produce KeyErrors deep in some subcommand.
_INDEX_VERSION = 1


# ---------- HTTP ----------------------------------------------------------


def _req(url: str, *, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _get_json(url: str) -> Any:
    return json.loads(_req(url))


# ---------- sync ----------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    meta = _get_json(SCRYFALL_BULK)
    remote_updated = meta["updated_at"]
    download_uri = meta["download_uri"]
    size = meta["size"]

    cached: dict[str, Any] = {}
    if META_JSON.exists():
        cached = json.loads(META_JSON.read_text())

    if (
        not args.force
        and cached.get("updated_at") == remote_updated
        and BULK_JSON.exists()
        and INDEX_PKL.exists()
    ):
        print(f"already current ({remote_updated}); --force to rebuild")
        return 0

    print(f"downloading default-cards bulk ({size/1e6:.0f} MB) ...")
    t0 = time.time()
    data = _req(download_uri)
    BULK_JSON.write_bytes(data)
    print(f"downloaded in {time.time()-t0:.1f}s; building index ...")

    t0 = time.time()
    cards = json.loads(data)
    index = _build_index(cards)
    with INDEX_PKL.open("wb") as f:
        pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
    META_JSON.write_text(
        json.dumps(
            {
                "updated_at": remote_updated,
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "card_count": len(cards),
                "name_count": len(index["by_name"]),
                "arena_id_count": len(index["by_arena_id"]),
            },
            indent=2,
        )
    )
    print(
        f"indexed {len(cards)} printings, {len(index['by_name'])} unique names, "
        f"{len(index['by_arena_id'])} arena_ids in {time.time()-t0:.1f}s"
    )
    return 0


def _build_index(cards: list[dict]) -> dict:
    """Build name + (set, collector) lookup tables.

    `by_name` maps lowercased card name -> list of printings (minimal fields).
    Double-faced/split cards are also indexed under each face name and under
    the front-face-only name (so MTGA exports of `Sea Gate Restoration`
    resolve to `Sea Gate Restoration // Sea Gate, Reborn`).
    `by_printing` maps "(set, collector_number)" -> printing.
    """
    by_name: dict[str, list[dict]] = {}
    by_printing: dict[tuple[str, str], dict] = {}
    by_arena_id: dict[int, dict] = {}
    for c in cards:
        slim = {k: c.get(k) for k in KEEP_FIELDS if k in c}
        keys = {c["name"].lower()}
        # MTGA export uses just the front face; index that too.
        if " // " in c["name"]:
            keys.add(c["name"].split(" // ", 1)[0].lower())
        for face in c.get("card_faces") or []:
            if face.get("name"):
                keys.add(face["name"].lower())
        for k in keys:
            by_name.setdefault(k, []).append(slim)
        by_printing[(c["set"].lower(), c["collector_number"])] = slim
        aid = c.get("arena_id")
        if isinstance(aid, int):
            # Multiple printings can technically share an arena_id only if
            # Scryfall is mid-update; last one wins, but we prefer the entry
            # that has `arena` in games (some non-Arena reprints carry an
            # arena_id from a later digital re-release).
            existing = by_arena_id.get(aid)
            if existing is None or (
                "arena" in (slim.get("games") or [])
                and "arena" not in (existing.get("games") or [])
            ):
                by_arena_id[aid] = slim
    return {
        "_version": _INDEX_VERSION,
        "by_name": by_name,
        "by_printing": by_printing,
        "by_arena_id": by_arena_id,
    }


# ---------- index loading -------------------------------------------------


_INDEX: dict | None = None


def _rebuild_index_from_bulk() -> dict:
    """Rebuild `index.pkl` from the on-disk bulk JSON (no network).

    Used by `_load_index` when the cached pickle's `_version` doesn't match
    `_INDEX_VERSION` (schema drift). Keeps `bulk-meta.json`'s `updated_at`
    intact so `cmd_sync` won't re-download; only the derived index counts
    are refreshed.
    """
    if not BULK_JSON.exists():
        sys.exit("no bulk JSON to rebuild from; run `mtg sync` first")
    cards = json.loads(BULK_JSON.read_bytes())
    index = _build_index(cards)
    with INDEX_PKL.open("wb") as f:
        pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
    if META_JSON.exists():
        meta = json.loads(META_JSON.read_text())
        meta["card_count"] = len(cards)
        meta["name_count"] = len(index["by_name"])
        meta["arena_id_count"] = len(index["by_arena_id"])
        META_JSON.write_text(json.dumps(meta, indent=2))
    return index


def _load_index() -> dict:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    if not INDEX_PKL.exists():
        sys.exit("no local index; run `mtg sync` first")
    with INDEX_PKL.open("rb") as f:
        idx = pickle.load(f)
    cached_version = idx.get("_version")
    if cached_version != _INDEX_VERSION:
        print(
            f"[info] index format changed (v{cached_version} -> "
            f"v{_INDEX_VERSION}); rebuilding from bulk JSON...",
            file=sys.stderr,
        )
        idx = _rebuild_index_from_bulk()
    _INDEX = idx
    return _INDEX


_STALE_WARNED: bool = False


def _warn_if_stale(max_age_h: float = 36.0) -> None:
    """Warn once per process if the bulk cache is older than `max_age_h`.

    Idempotent within a single CLI invocation. `cmd_check` runs five
    sub-commands in sequence, each of which calls this at the top; without
    the latch the user sees five identical warnings. Latching here is
    simpler than threading a flag through every `cmd_*` helper.
    """
    global _STALE_WARNED
    if _STALE_WARNED:
        return
    if not META_JSON.exists():
        return
    meta = json.loads(META_JSON.read_text())
    fetched = meta.get("fetched_at")
    if not fetched:
        return
    t = time.mktime(time.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ"))
    age_h = (time.time() - t) / 3600
    if age_h > max_age_h:
        print(
            f"[warn] local bulk is {age_h:.1f}h old; consider `mtg sync`",
            file=sys.stderr,
        )
    # Latch even when not stale — the staleness verdict won't change
    # mid-run, so re-checking on every cmd_* call is wasted file IO.
    _STALE_WARNED = True


# ---------- name normalization -------------------------------------------


def _normalize_name(raw: str) -> str:
    """MTGA export uses the visible name (with leading "A-" for rebalanced
    cards) but Scryfall stores those as separate cards whose `name` already
    starts with "A-". So no stripping needed — just trim and case-fold for
    lookup."""
    return raw.strip().lower()


def _printings_for_name(name: str) -> list[dict]:
    idx = _load_index()
    return idx["by_name"].get(_normalize_name(name), [])


def _resolve_card(name: str) -> dict | None:
    """Pick a representative printing. Prefer one with `arena` in games."""
    prints = _printings_for_name(name)
    if not prints:
        return None
    for p in prints:
        if "arena" in (p.get("games") or []):
            return p
    return prints[0]


# ---------- card / printing -----------------------------------------------


def _emit_json(payload) -> None:
    """Single sink for `--json` output. Adds a trailing newline (so piping
    to `python -c 'json.load'` works cleanly) and routes non-JSON-native
    types through `str` (Path, set, etc).
    """
    print(json.dumps(payload, indent=2, default=str))


def _card_to_json(c: dict) -> dict:
    """Canonical JSON shape for a single card. Used by `card`, `printing`
    and embedded inside list-shaped payloads.
    """
    return {
        "name": c.get("name"),
        "set": (c.get("set") or "").upper(),
        "collector_number": c.get("collector_number"),
        "mana_cost": c.get("mana_cost"),
        "cmc": c.get("cmc"),
        "type_line": c.get("type_line"),
        "oracle_text": c.get("oracle_text"),
        "colors": c.get("colors") or [],
        "color_identity": c.get("color_identity") or [],
        "rarity": c.get("rarity"),
        "keywords": c.get("keywords") or [],
        "legalities": c.get("legalities") or {},
        "games": c.get("games") or [],
        "image_uris": c.get("image_uris") or None,
        "card_faces": [
            {
                "name": f.get("name"),
                "mana_cost": f.get("mana_cost"),
                "type_line": f.get("type_line"),
                "oracle_text": f.get("oracle_text"),
                "colors": f.get("colors") or [],
                "image_uris": f.get("image_uris") or None,
            }
            for f in (c.get("card_faces") or [])
        ] or None,
        "game_changer": bool(c.get("game_changer")),
    }


def _format_card(c: dict) -> str:
    lines = []
    lines.append(f"name        : {c['name']}")
    lines.append(f"set/coll    : {c['set'].upper()} {c['collector_number']}")
    lines.append(f"mana        : {c.get('mana_cost') or '-'}  (cmc {c.get('cmc')})")
    lines.append(f"type        : {c.get('type_line') or '-'}")
    ci = "".join(c.get("color_identity") or []) or "C"
    lines.append(f"identity    : {ci}")
    lines.append(f"games       : {','.join(c.get('games') or []) or '-'}")
    if c.get("oracle_text"):
        lines.append("oracle      :")
        for ln in c["oracle_text"].splitlines():
            lines.append(f"    {ln}")
    if c.get("card_faces"):
        for face in c["card_faces"]:
            lines.append(f"face        : {face.get('name')}")
            if face.get("oracle_text"):
                for ln in face["oracle_text"].splitlines():
                    lines.append(f"    {ln}")
    if c.get("game_changer"):
        lines.append("game_changer: yes")
    legal = c.get("legalities") or {}
    arena_legal = {f: legal.get(f, "?") for f in sorted(ARENA_FORMATS)}
    lines.append("arena formats:")
    for f, v in arena_legal.items():
        lines.append(f"    {f:<14} {v}")
    return "\n".join(lines)


def cmd_card(args: argparse.Namespace) -> int:
    _warn_if_stale()
    c = _resolve_card(args.name)
    if not c:
        print(f"card not found: {args.name}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        _emit_json(_card_to_json(c))
    else:
        print(_format_card(c))
    return 0


def cmd_printing(args: argparse.Namespace) -> int:
    _warn_if_stale()
    idx = _load_index()
    c = idx["by_printing"].get((args.set.lower(), args.num))
    if not c:
        print(f"printing not found: {args.set} {args.num}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        _emit_json(_card_to_json(c))
    else:
        print(_format_card(c))
    return 0


# ---------- legality -----------------------------------------------------


def cmd_legal(args: argparse.Namespace) -> int:
    _warn_if_stale()
    fmt = args.format.lower()
    if fmt not in ARENA_FORMATS:
        print(
            f"format must be one of: {', '.join(sorted(ARENA_FORMATS))}",
            file=sys.stderr,
        )
        return 2
    c = _resolve_card(args.name)
    if not c:
        print(f"card not found: {args.name}", file=sys.stderr)
        return 1
    legal = (c.get("legalities") or {}).get(fmt, "not_legal")
    on_arena = "arena" in (c.get("games") or [])
    is_legal = legal == "legal" and on_arena
    if getattr(args, "json", False):
        _emit_json({
            "name": c["name"],
            "format": fmt,
            "legal": is_legal,
            "status": legal,
            "on_arena": on_arena,
        })
    else:
        print(
            f"{c['name']}: {legal} in {fmt}; arena={'yes' if on_arena else 'no'}"
        )
    return 0 if is_legal else 1


# ---------- deck parsing + validation ------------------------------------

# DECK_LINE_RE / SECTION_HEADERS / DeckEntry / MULTIFACE_LAYOUTS are
# imported from `mtg_sources._common` (single source of truth so per-host
# scrapers in `tools/mtg_sources/` and the rest of this CLI can't drift).


def parse_deck(path: Path) -> list[DeckEntry]:
    section = "deck"
    out: list[DeckEntry] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in SECTION_HEADERS:
            section = line.lower()
            continue
        m = DECK_LINE_RE.match(line)
        if not m:
            print(f"[warn] cannot parse line: {raw!r}", file=sys.stderr)
            continue
        count, name, set_code, collector = m.groups()
        out.append(
            DeckEntry(int(count), name, set_code, collector, section)
        )
    return out


BRAWL_FORMATS = {"brawl", "standardbrawl"}


def _write_mtga_export(path: Path, entries: list[DeckEntry]) -> None:
    """Write a deck file in MTGA-export format.

    Sections emitted in order: Commander, Deck, Sideboard. A section
    header is emitted only when the section has at least one entry, with
    one blank line between sections. For multi-face cards (layouts in
    MULTIFACE_LAYOUTS) the resolved Scryfall `name` already contains the
    `Front // Back` slash — emit it as-is so MTGA accepts the import.
    """
    sections = (("commander", "Commander"), ("deck", "Deck"),
                ("sideboard", "Sideboard"))
    chunks: list[str] = []
    for key, header in sections:
        rows = [e for e in entries if e.section == key]
        if not rows:
            continue
        block: list[str] = [header]
        for e in rows:
            resolved = _resolve_card(e.name)
            layout = (resolved.get("layout") or "") if resolved else ""
            full = (resolved.get("name") or "") if resolved else ""
            if layout in MULTIFACE_LAYOUTS and " // " in full:
                name = full
            else:
                name = e.name
            block.append(f"{e.count} {name} ({e.set_code}) {e.collector}")
        chunks.append("\n".join(block))
    path.write_text("\n\n".join(chunks) + "\n")


def validate_deck(entries: list[DeckEntry], fmt: str) -> tuple[int, list[str]]:
    """Return (exit_code, messages). 0 = clean."""
    msgs: list[str] = []
    if fmt not in ARENA_FORMATS:
        return 2, [f"unknown format: {fmt}"]

    commanders = [e for e in entries if e.section == "commander"]
    deck = [e for e in entries if e.section == "deck"]
    sideboard = [e for e in entries if e.section == "sideboard"]

    is_brawl = fmt in BRAWL_FORMATS

    if is_brawl:
        if len(commanders) != 1 or commanders[0].count != 1:
            msgs.append(
                f"brawl: expected exactly 1 commander, got "
                f"{sum(e.count for e in commanders)}"
            )
        total = sum(e.count for e in commanders) + sum(e.count for e in deck)
        if total != 100:
            msgs.append(f"brawl: deck size must be 100, got {total}")
        # singleton (basic lands exempt)
        seen: dict[str, int] = {}
        for e in deck + commanders:
            seen[e.name] = seen.get(e.name, 0) + e.count
        for name, n in seen.items():
            c = _resolve_card(name)
            if not c:
                continue
            type_line = (c.get("type_line") or "").lower()
            is_basic = "basic" in type_line and "land" in type_line
            if n > 1 and not is_basic:
                msgs.append(f"singleton: {name} appears {n} times")
    else:
        total = sum(e.count for e in deck)
        if total < 60:
            msgs.append(f"{fmt}: main deck must be ≥60, got {total}")
        if sum(e.count for e in sideboard) > 15:
            msgs.append(f"{fmt}: sideboard must be ≤15")
        # 4-of limit (basic lands exempt)
        seen: dict[str, int] = {}
        for e in deck + sideboard:
            seen[e.name] = seen.get(e.name, 0) + e.count
        for name, n in seen.items():
            c = _resolve_card(name)
            if not c:
                continue
            type_line = (c.get("type_line") or "").lower()
            is_basic = "basic" in type_line and "land" in type_line
            if n > 4 and not is_basic:
                msgs.append(f"4-of: {name} appears {n} times")

    # color identity (Brawl only)
    cmdr_identity: set[str] | None = None
    if is_brawl and commanders:
        cmdr = _resolve_card(commanders[0].name)
        if cmdr:
            cmdr_identity = set(cmdr.get("color_identity") or [])
            type_line = (cmdr.get("type_line") or "").lower()
            ok_type = (
                "legendary" in type_line
                and ("creature" in type_line or "planeswalker" in type_line)
            )
            if not ok_type:
                msgs.append(
                    f"commander {cmdr['name']} not a legendary creature/planeswalker"
                )

    # per-card legality + arena availability
    for e in entries:
        if e.section not in {"deck", "commander", "sideboard", "companion"}:
            continue
        c = _resolve_card(e.name)
        if not c:
            msgs.append(f"unknown card: {e.name} ({e.set_code}) {e.collector}")
            continue
        # MTGA-import syntax: multi-face cards must be written as
        # "Front // Back" or Arena rejects the import outright.
        layout = c.get("layout") or ""
        full_name = c.get("name") or ""
        if (
            layout in MULTIFACE_LAYOUTS
            and " // " in full_name
            and e.name != full_name
        ):
            msgs.append(
                f"mtga-import: '{e.name}' (layout={layout}) must be written "
                f"as '{full_name}' for Arena to accept the deck import"
            )
        if "arena" not in (c.get("games") or []):
            msgs.append(f"not on arena: {e.name}")
        legal = (c.get("legalities") or {}).get(fmt, "not_legal")
        if legal != "legal":
            msgs.append(f"{legal} in {fmt}: {e.name}")
        if cmdr_identity is not None:
            ci = set(c.get("color_identity") or [])
            if not ci.issubset(cmdr_identity):
                extras = "".join(sorted(ci - cmdr_identity))
                msgs.append(
                    f"identity violation: {e.name} adds {{{extras}}} "
                    f"outside commander identity {{{''.join(sorted(cmdr_identity))}}}"
                )

    return (0 if not msgs else 1), msgs


def _card_legal_in(c: dict, fmt: str) -> bool:
    if "arena" not in (c.get("games") or []):
        return False
    return ((c.get("legalities") or {}).get(fmt, "not_legal") == "legal")


def cmd_validate(args: argparse.Namespace) -> int:
    _warn_if_stale()
    path = Path(args.deck)
    if not path.exists():
        print(f"deck file not found: {path}", file=sys.stderr)
        return 2
    entries = parse_deck(path)
    fmt = args.format.lower()

    if getattr(args, "json", False):
        code, msgs = validate_deck(entries, fmt)
        cmdrs = [e.name for e in entries if e.section == "commander"]
        total = sum(
            e.count for e in entries if e.section in {"deck", "commander"}
        )
        _emit_json({
            "deck": str(path),
            "format": fmt,
            "ok": code == 0,
            "errors": msgs,
            "warnings": [],
            "card_total": total,
            "commander": cmdrs[0] if cmdrs else None,
        })
        return code

    if args.verbose:
        cmdr_entry = next((e for e in entries if e.section == "commander"), None)
        cmdr_id: set[str] = set()
        if cmdr_entry:
            cmdr_card = _resolve_card(cmdr_entry.name)
            if cmdr_card:
                cmdr_id = set(cmdr_card.get("color_identity") or [])
        print(f"per-card check ({fmt}):")
        ok_n = bad_n = 0
        for e in entries:
            if e.section not in {"deck", "commander", "sideboard", "companion"}:
                continue
            c = _resolve_card(e.name)
            if not c:
                print(f"  ? {e.count}x {e.name:<40} (UNKNOWN)")
                bad_n += 1
                continue
            on_arena = "arena" in (c.get("games") or [])
            legal = (c.get("legalities") or {}).get(fmt, "not_legal")
            ci = set(c.get("color_identity") or [])
            id_ok = (not cmdr_id) or ci.issubset(cmdr_id)
            mark = "✓" if (on_arena and legal == "legal" and id_ok) else "✗"
            if mark == "✓":
                ok_n += 1
            else:
                bad_n += 1
            ci_str = "".join(sorted(ci)) or "C"
            print(
                f"  {mark} {e.count}x {e.name:<40} "
                f"[{c['set'].upper():<4} {c['collector_number']:<6}] "
                f"arena={'y' if on_arena else 'n'} {fmt}={legal} ci={ci_str}"
            )
        print(f"  -- {ok_n} ok, {bad_n} flagged")

    code, msgs = validate_deck(entries, fmt)
    if msgs:
        for m in msgs:
            print(f"  ✗ {m}")
        print(f"{path}: {len(msgs)} issue(s)")
    else:
        cmdrs = [e.name for e in entries if e.section == "commander"]
        total = sum(e.count for e in entries if e.section in {"deck", "commander"})
        print(
            f"{path}: ok ({total} cards, format={fmt}"
            + (f", commander={cmdrs[0]}" if cmdrs else "")
            + ")"
        )
    return code


# ---------- analyze (composition heuristics) -----------------------------

# Role-tagging is heuristic: regex over oracle text. Imperfect, but a
# forcing function — a deck with `card_advantage: 0` is almost certainly
# missing a draw plan, regardless of edge cases the regex misses.

_RX_REMOVAL = re.compile(
    r"\bdestroy target\b"
    r"|\bexile target [^.]*?\b(?:creature|permanent|nonland|attacking|blocking|planeswalker|artifact|enchantment)\b"
    r"|deals (?:\d+|x) damage to (?:target|any)"
    r"|target creature (?:gets|gains) -(?:\d+|x)"
    r"|fights target"
    r"|\breturn target [^.]*?\b(?:creature|permanent|nonland|artifact|enchantment|planeswalker)\b[^.]*?\bto (?:its|their) owner's? hand\b",
)
_RX_SWEEPER = re.compile(
    r"\bdestroy all\b"
    r"|\bexile all\b"
    r"|deals (?:\d+|x) damage to (?:each|every) (?:creature|opponent|player)"
    r"|(?:all|each) creatures? (?:get|gain) -(?:\d+|x)",
)
# Mana rocks: artifacts whose oracle effectively adds mana when tapped.
_RX_MANA_ROCK = re.compile(
    r"\{t\}: add (?:\{[wubrgcsxp1-9]|one mana|two mana|three mana|that much mana)",
)
# Tutor = library search that fetches a non-land card. Land searches —
# both "basic land" and by-type ("Plains, Island, Swamp, or Mountain
# card", Farseek-style) — are ramp, not tutors. The ramp regex below
# matches all those cases; classify_card suppresses `tutor` whenever `ramp`
# also matches, which is cheaper and more correct than encoding the
# basic-type list in a negative lookahead twice.
_RX_TUTOR = re.compile(
    r"search your library for [^.]*?\bcard\b",
)
# Ramp: classic ramp spells say "search your library for a basic <type>
# card" or "search your library for a land card". Catch both forms.
# Also require the search to be on YOUR library, not the opponent's
# (Path to Exile says "search their library").
_RX_RAMP = re.compile(
    r"search your library for [^.]*?\b(?:land|plains|island|swamp|mountain|forest|wastes)\b",
)
# Card draw: handle "draw a card", "draw N cards", "draw an additional
# card", "draw two additional cards", "draw cards equal to X". Phyrexian
# Arena and Sylvan Library require the loose [^.]*? form.
_RX_DRAW = re.compile(
    r"\bdraws? (?:a card|an? [^.]*?card|\w+ cards?|[^.]*?cards?|that many cards?|cards? equal)\b",
)
_RX_LOOT = re.compile(r"\bdiscards? (?:a card|\w+ cards?)\b")
_RX_HAND_ATTACK = re.compile(
    r"target (?:player|opponent) (?:reveals|discards)",
)
_RX_PEEK = re.compile(r"look at target (?:player|opponent)'s hand")
_RX_COUNTER = re.compile(r"\bcounter target\b")
_RX_RECUR = re.compile(r"return target [^.]*?\bfrom (?:your|a) graveyard\b")
# Alt wincons: cards that explicitly say "you win the game" (Approach,
# Maze's End, Jace WoM, Test of Endurance, Sanctum of All) or force a
# loss on the other side. Damage/life-total wins are NOT alt wincons —
# those route through normal combat math and don't need a tag.
_RX_WINCON = re.compile(
    r"\byou win the game\b"
    r"|\b(?:target (?:opponent|player)|each opponent) loses the game\b",
)


def _all_text(c: dict) -> str:
    parts = [c.get("oracle_text") or ""]
    for f in c.get("card_faces") or []:
        parts.append(f.get("oracle_text") or "")
    return "\n".join(parts).lower()


def classify_card(c: dict) -> set[str]:
    tags: set[str] = set()
    type_line = (c.get("type_line") or "").lower()
    text = _all_text(c)
    cmc = c.get("cmc") or 0

    if "land" in type_line:
        tags.add("land")
        # Lands are not "ramp": fetchlands and dual-fetches sacrifice
        # themselves to find a land, netting zero mana sources. True ramp
        # is a non-land that adds a land. Keep the ramp tag for nonlands.
        if _RX_DRAW.search(text):
            tags.add("card_advantage")
        return tags

    if "creature" in type_line:
        tags.add("creature")
    if "planeswalker" in type_line:
        tags.add("planeswalker")
    if "artifact" in type_line:
        tags.add("artifact")
    if "enchantment" in type_line:
        tags.add("enchantment")
    if "instant" in type_line:
        tags.add("instant")
    if "sorcery" in type_line:
        tags.add("sorcery")
    if "battle" in type_line:
        tags.add("battle")

    if _RX_SWEEPER.search(text):
        tags.add("sweeper")
    if _RX_REMOVAL.search(text):
        tags.add("removal")
    if _RX_COUNTER.search(text):
        tags.add("counter")
    is_ramp = _RX_RAMP.search(text) or (
        "artifact" in type_line and _RX_MANA_ROCK.search(text)
    )
    if is_ramp:
        tags.add("ramp")
    # `tutor` only when the search isn't a land-fetch — Farseek/Cultivate
    # are ramp, not tutors, even though they use "search your library".
    if _RX_TUTOR.search(text) and not is_ramp:
        tags.add("tutor")
    if _RX_DRAW.search(text):
        # Pure loot (draw + equal-count discard) doesn't net CA.
        draws = len(_RX_DRAW.findall(text))
        loots = len(_RX_LOOT.findall(text))
        if draws > loots:
            tags.add("card_advantage")
        else:
            tags.add("loot")
    if _RX_HAND_ATTACK.search(text):
        tags.add("hand_attack")
    if _RX_PEEK.search(text):
        tags.add("peek")
    if _RX_RECUR.search(text):
        tags.add("recursion")
    if _RX_WINCON.search(text):
        tags.add("wincon")
    if ("creature" in type_line or "planeswalker" in type_line) and cmc >= 4:
        tags.add("threat")

    return tags


# Roles used in the analyze output. Order is the print order; labels are
# what the deckbuilder reads. Purely declarative — adding a role means
# adding a regex above and an entry here.
_ROLE_TYPE = (
    ("land", "lands"),
    ("creature", "creatures"),
    ("planeswalker", "planeswalkers"),
    ("artifact", "artifacts"),
    ("enchantment", "enchantments"),
    ("instant", "instants"),
    ("sorcery", "sorceries"),
    ("battle", "battles"),
)
_ROLE_FUNC = (
    ("removal", "spot_removal"),
    ("sweeper", "sweeper"),
    ("counter", "counter"),
    ("hand_attack", "hand_attack"),
    ("peek", "peek"),
    ("card_advantage", "card_advantage"),
    ("loot", "loot"),
    ("tutor", "tutor"),
    ("ramp", "ramp"),
    ("recursion", "recursion"),
    ("wincon", "alt_wincon"),
    ("threat", "threat_cmc_4plus"),
)


def cmd_analyze(args: argparse.Namespace) -> int:
    """Composition data dump. Prints structured facts only — no thresholds,
    no warnings, no advice. The reader (you) judges the deck.

    Sections: header / composition by type / function-role counts / mana
    curve / per-card classification table.
    """
    _warn_if_stale()
    if args.include_sideboard and args.sideboard_only:
        sys.exit("--include-sideboard and --sideboard-only are mutually exclusive")
    path = Path(args.deck)
    if not path.exists():
        print(f"deck file not found: {path}", file=sys.stderr)
        return 2
    entries = parse_deck(path)
    if args.sideboard_only:
        sections = {"sideboard"}
    elif args.include_sideboard:
        sections = {"deck", "commander", "sideboard"}
    else:
        sections = {"deck", "commander"}
    main_entries = [e for e in entries if e.section in sections]
    total = sum(e.count for e in main_entries)

    role_counts: dict[str, int] = {}
    curve: dict[int, int] = {}
    nonland_total = 0
    gc_total = 0
    rows: list[tuple[DeckEntry, dict | None, set[str], int]] = []

    for e in main_entries:
        c = _resolve_card(e.name)
        if c is None:
            rows.append((e, None, set(), 0))
            continue
        tags = classify_card(c)
        cmc = int(c.get("cmc") or 0)
        for t in tags:
            role_counts[t] = role_counts.get(t, 0) + e.count
        if c.get("game_changer"):
            gc_total += e.count
        type_line = (c.get("type_line") or "").lower()
        if "land" not in type_line:
            nonland_total += e.count
            curve[cmc] = curve.get(cmc, 0) + e.count
        rows.append((e, c, tags, cmc))

    if getattr(args, "json", False):
        type_keys = {k for k, _ in _ROLE_TYPE}
        func_keys = {k for k, _ in _ROLE_FUNC}
        type_mix = {label: role_counts.get(key, 0) for key, label in _ROLE_TYPE}
        function_tags = {label: role_counts.get(key, 0) for key, label in _ROLE_FUNC}
        function_tags["game_changers"] = gc_total
        avg_cmc = (
            sum(c * n for c, n in curve.items()) / max(nonland_total, 1)
            if curve else 0.0
        )
        cards_payload = []
        for e, c, tags, cmc in rows:
            if c is None:
                cards_payload.append({
                    "name": e.name,
                    "count": e.count,
                    "section": e.section,
                    "cmc": None,
                    "types": [],
                    "tags": [],
                    "resolved": False,
                })
                continue
            cards_payload.append({
                "name": c.get("name"),
                "count": e.count,
                "section": e.section,
                "cmc": cmc,
                "types": sorted(tags & type_keys),
                "tags": sorted(tags & func_keys),
                "resolved": True,
            })
        _emit_json({
            "deck": str(path),
            "scope": sorted(sections),
            "card_total": total,
            "type_mix": type_mix,
            "function_tags": function_tags,
            "curve": {str(k): v for k, v in sorted(curve.items())},
            "nonland_total": nonland_total,
            "avg_cmc": round(avg_cmc, 4),
            "cards": cards_payload,
        })
        return 0

    print(f"deck: {path} ({total} cards, scope={'+'.join(sorted(sections))})")
    print()
    print("composition (by type, lands+nonlands):")
    for key, label in _ROLE_TYPE:
        print(f"  {label:<18} {role_counts.get(key, 0):>3}")
    print()
    print("function tags (per oracle-text regex; one card may carry several):")
    for key, label in _ROLE_FUNC:
        print(f"  {label:<18} {role_counts.get(key, 0):>3}")
    # Scryfall-curated Game Changer list — relevant for Brawl bracket.
    # Not a function tag; surfaced here for at-a-glance bracket awareness.
    print(f"  {'game_changers':<18} {gc_total:>3}")
    print()
    if curve:
        print("nonland mana curve:")
        max_n = max(curve.values())
        max_cmc = max(curve.keys())
        for cmc in range(0, max_cmc + 1):
            n = curve.get(cmc, 0)
            bar = "█" * int(round(20 * n / max_n)) if n else ""
            label = f"{cmc}+" if cmc == max_cmc and cmc >= 7 else str(cmc)
            print(f"  {label:>3}  {bar} {n}")
        avg = sum(c * n for c, n in curve.items()) / max(nonland_total, 1)
        print(f"  avg cmc: {avg:.2f} (nonland={nonland_total})")
        print()
    # Per-card classification table — single source of truth for what the
    # regexes tagged. If the totals look off, this is where you check.
    print("per-card classification (in deck-file order):")
    current_section = ""
    type_keys = {k for k, _ in _ROLE_TYPE}
    func_keys = {k for k, _ in _ROLE_FUNC}
    for e, c, tags, cmc in rows:
        if e.section != current_section:
            print(f"  [{e.section}]")
            current_section = e.section
        if c is None:
            print(f"    {e.count:>2}  {e.name:<40} cmc -    tags: UNKNOWN")
            continue
        type_tags = sorted(tags & type_keys)
        func_tags = sorted(tags & func_keys)
        all_tags = type_tags + func_tags
        tag_str = ", ".join(all_tags) if all_tags else "-"
        print(f"    {e.count:>2}  {e.name:<40} cmc {cmc:<2}  tags: {tag_str}")
    return 0


# ---------- related (sister-card discovery) ------------------------------


def cmd_related(args: argparse.Namespace) -> int:
    """Print cards that share each unique keyword with the input card.

    Many synergy plays (Repartee, Survival, Squad, etc.) live on a small
    cluster of cards in the same set. The deckbuilder must enumerate that
    cluster, not just remember the anchor. This is the forcing function:
    take a card, list every card with the same keyword in the target
    format, all from the local index (no HTTP).
    """
    _warn_if_stale()
    c = _resolve_card(args.name)
    if not c:
        print(f"card not found: {args.name}", file=sys.stderr)
        return 1
    fmt = (args.format or "").lower()
    if fmt and fmt not in ARENA_FORMATS:
        print(
            f"format must be one of: {', '.join(sorted(ARENA_FORMATS))}",
            file=sys.stderr,
        )
        return 2
    keywords = list(c.get("keywords") or [])
    if not keywords:
        if getattr(args, "json", False):
            _emit_json({
                "anchor": c["name"],
                "format": fmt or None,
                "keywords": [],
                "by_keyword": {},
            })
        else:
            print(
                f"{c['name']} has no Scryfall-tagged keywords; nothing to expand."
            )
        return 0

    idx = _load_index()
    # Iterate every distinct card name once. For each, resolve to its
    # Arena printing (so legality/games checks reflect the Arena copy,
    # not whichever paper printing happened to be first in the index).
    by_kw: dict[str, list[dict]] = {kw: [] for kw in keywords}
    seen: set[str] = set()
    for lname in idx["by_name"]:
        # Skip face-name aliases — we want one entry per oracle.
        prints = idx["by_name"][lname]
        if not prints:
            continue
        # Pick the Arena-preferring printing.
        rep = None
        for p in prints:
            if "arena" in (p.get("games") or []):
                rep = p
                break
        rep = rep or prints[0]
        if rep["name"] == c["name"] or rep["name"] in seen:
            continue
        pkw = set(rep.get("keywords") or [])
        if not pkw:
            continue
        if fmt:
            if (rep.get("legalities") or {}).get(fmt) != "legal":
                continue
            if "arena" not in (rep.get("games") or []):
                continue
        for kw in keywords:
            if kw in pkw:
                by_kw[kw].append(rep)
                seen.add(rep["name"])
                break

    if getattr(args, "json", False):
        out_by_kw: dict[str, list[dict]] = {}
        for kw in sorted(keywords, key=lambda k: len(by_kw[k])):
            cards = sorted(
                by_kw[kw], key=lambda x: (x.get("cmc") or 0, x["name"])
            )[: args.limit]
            out_by_kw[kw] = [
                {
                    "name": p["name"],
                    "set": (p.get("set") or "").upper(),
                    "cmc": p.get("cmc") or 0,
                    "color_identity":
                        "".join(p.get("color_identity") or []) or "C",
                    "type_line": p.get("type_line") or "",
                    "rarity": p.get("rarity") or "",
                }
                for p in cards
            ]
        _emit_json({
            "anchor": c["name"],
            "format": fmt or None,
            "keywords": keywords,
            "by_keyword": out_by_kw,
        })
        return 0

    print(f"sister cards by keyword (anchor: {c['name']}{', fmt=' + fmt if fmt else ''}):")
    # Rarer keywords first — named/mechanic clusters (Blitz, Survival,
    # Squad) are the high-signal cases; evergreens (Flying, Deathtouch)
    # match hundreds of cards and are noise unless you're explicitly
    # looking for tribal-style overlap.
    for kw in sorted(keywords, key=lambda k: len(by_kw[k])):
        cards = by_kw[kw]
        print(f"\n  [{kw}] — {len(cards)} other card(s)")
        for p in sorted(cards, key=lambda x: (x.get("cmc") or 0, x["name"]))[: args.limit]:
            ci = "".join(p.get("color_identity") or []) or "C"
            tline = (p.get("type_line") or "").split(" — ")[0]
            print(
                f"    {p['name']:<38} {p['set'].upper():<5} cmc {p.get('cmc') or 0:<2} "
                f"{ci:<4} {tline}"
            )
        if len(cards) > args.limit:
            print(f"    ... +{len(cards) - args.limit} more")
    return 0


# ---------- manabase / wildcards / companion (data dumps) ----------------

# A "pip" is one `{...}` token in a mana cost. Hybrid (`{W/U}`) and
# Phyrexian (`{W/P}`) both contribute to whichever colors appear in the
# token; generic and X pips contribute to none. We dump per-color demand,
# not a Karsten threshold — the deckbuilder reads the table and decides.

_PIP_RE = re.compile(r"\{([^}]+)\}")
_COLORS = ("W", "U", "B", "R", "G")
# "Enters tapped" / "Enters the battlefield tapped" — used as a flag, not
# a verdict (checklands and pathways read "enters tapped unless ...").
_RX_ETB_TAPPED = re.compile(
    r"\benters(?: the battlefield)? tapped\b",
)


def _pip_colors(symbol: str) -> set[str]:
    """Return the colors a single pip token contributes to.

    `{W}` -> {W}; `{W/U}` -> {W, U} (hybrid, either color satisfies it);
    `{W/P}` -> {W} (Phyrexian, the P is life-payment); `{2/W}` -> {W}
    (twobrid, the W half satisfies it). Generic/X/snow/colorless contribute
    nothing.
    """
    out: set[str] = set()
    for ch in symbol.upper():
        if ch in _COLORS:
            out.add(ch)
    return out


def cmd_manabase(args: argparse.Namespace) -> int:
    """Dump pip demand, color sources, and ETB-tapped land list.

    Three tables, no thresholds:
      1. pip demand by CMC: how many {W}/{U}/... your nonland costs need.
      2. color sources: count of deck cards that produce each color via
         Scryfall's `produced_mana` field (lands + Birds-of-Paradise-style
         nonlands both count).
      3. ETB-tapped lands: any land whose oracle contains "enters tapped"
         (verbatim text shown so conditional ones — checklands, pathways,
         shocklands — can be judged in context).
    """
    _warn_if_stale()
    path = Path(args.deck)
    if not path.exists():
        print(f"deck file not found: {path}", file=sys.stderr)
        return 2
    entries = parse_deck(path)
    main = [e for e in entries if e.section in {"deck", "commander"}]

    # pip demand: rows = cmc, cols = WUBRG. Hybrid pip is double-counted
    # (it adds to BOTH colors' demand because either source satisfies it).
    pip_by_cmc: dict[int, dict[str, int]] = {}
    nonland_count_by_cmc: dict[int, int] = {}
    # source counts: per color, how many copies in the deck produce it.
    src_by_color: dict[str, int] = {c: 0 for c in _COLORS}
    src_by_color["C"] = 0  # colorless production
    land_total = 0
    nonland_producer_total = 0
    tapped_lands: list[tuple[DeckEntry, dict]] = []

    for e in main:
        c = _resolve_card(e.name)
        if c is None:
            continue
        type_line = (c.get("type_line") or "").lower()
        is_land = "land" in type_line
        cmc = int(c.get("cmc") or 0)

        if not is_land:
            nonland_count_by_cmc[cmc] = nonland_count_by_cmc.get(cmc, 0) + e.count
            row = pip_by_cmc.setdefault(cmc, {col: 0 for col in _COLORS})
            for tok in _PIP_RE.findall(c.get("mana_cost") or ""):
                for col in _pip_colors(tok):
                    row[col] += e.count

        produced = set(c.get("produced_mana") or [])
        if produced:
            for col in _COLORS:
                if col in produced:
                    src_by_color[col] += e.count
            if "C" in produced:
                src_by_color["C"] += e.count
            if not is_land:
                nonland_producer_total += e.count

        if is_land:
            land_total += e.count
            text = _all_text(c)
            if _RX_ETB_TAPPED.search(text):
                tapped_lands.append((e, c))

    if getattr(args, "json", False):
        pip_demand_payload = {
            str(cmc): pip_by_cmc[cmc] for cmc in sorted(pip_by_cmc)
        }
        sources_payload = {col: src_by_color[col] for col in _COLORS + ("C",)}
        sources_payload["lands"] = land_total
        sources_payload["nonland_producers"] = nonland_producer_total
        tapped_payload = []
        for e, c in tapped_lands:
            text = c.get("oracle_text") or ""
            hit_lines = [
                ln.strip()
                for ln in text.splitlines()
                if _RX_ETB_TAPPED.search(ln.lower())
            ]
            tapped_payload.append({
                "name": c.get("name"),
                "count": e.count,
                "first_etb_line":
                    hit_lines[0] if hit_lines else None,
            })
        _emit_json({
            "deck": str(path),
            "pip_demand": pip_demand_payload,
            "nonland_count_by_cmc": {
                str(k): v for k, v in sorted(nonland_count_by_cmc.items())
            },
            "sources": sources_payload,
            "etb_tapped": len(tapped_lands),
            "etb_tapped_lands": tapped_payload,
        })
        return 0

    print(f"manabase: {path}")
    print()
    if pip_by_cmc:
        print("pip demand by cmc (hybrid pip counts toward each color):")
        header = "  cmc  " + "".join(f"{col:>4}" for col in _COLORS) + "   nonland"
        print(header)
        totals = {col: 0 for col in _COLORS}
        for cmc in sorted(pip_by_cmc):
            row = pip_by_cmc[cmc]
            for col in _COLORS:
                totals[col] += row[col]
            cells = "".join(f"{row[col]:>4}" for col in _COLORS)
            print(f"  {cmc:>3}  {cells}   {nonland_count_by_cmc.get(cmc, 0):>4}")
        cells = "".join(f"{totals[col]:>4}" for col in _COLORS)
        print(f"  tot  {cells}   {sum(nonland_count_by_cmc.values()):>4}")
        print()

    print("color sources (cards in deck that produce each color):")
    print("  color  sources")
    for col in _COLORS + ("C",):
        print(f"   {col}     {src_by_color[col]:>3}")
    print(f"  lands     {land_total}")
    print(f"  nonland producers  {nonland_producer_total}")
    print()

    print(
        f"etb-tapped lands ({len(tapped_lands)} entries; "
        "verbatim oracle line shown — read conditionals carefully):"
    )
    if not tapped_lands:
        print("  (none)")
    for e, c in tapped_lands:
        text = c.get("oracle_text") or ""
        # Show only the line(s) containing the etb-tapped phrase.
        hit_lines = [
            ln.strip()
            for ln in text.splitlines()
            if _RX_ETB_TAPPED.search(ln.lower())
        ]
        first = hit_lines[0] if hit_lines else "(see oracle text)"
        print(f"  {e.count:>2}  {c['name']:<35}  {first}")
    return 0


def cmd_wildcards(args: argparse.Namespace) -> int:
    """Count deck entries by Scryfall `rarity`. MTGA wildcard cost = the
    rare/mythic counts; commons and uncommons are usually free for an
    established account but the totals still belong in the dump.
    """
    _warn_if_stale()
    path = Path(args.deck)
    if not path.exists():
        print(f"deck file not found: {path}", file=sys.stderr)
        return 2
    entries = parse_deck(path)
    entries_in = [e for e in entries if e.section in {"deck", "commander", "sideboard"}]

    by_rarity: dict[str, int] = {}
    by_rarity_cards: dict[str, list[tuple[DeckEntry, dict]]] = {}
    unknown: list[DeckEntry] = []
    for e in entries_in:
        c = _resolve_card(e.name)
        if c is None:
            unknown.append(e)
            continue
        r = c.get("rarity") or "unknown"
        by_rarity[r] = by_rarity.get(r, 0) + e.count
        by_rarity_cards.setdefault(r, []).append((e, c))

    seen_rarities = sorted(
        by_rarity,
        key=lambda r: (_RARITY_ORDER.get(r, 99), r),
    )

    if getattr(args, "json", False):
        rarity_counts = {
            r: by_rarity.get(r, 0)
            for r in ("mythic", "rare", "uncommon", "common")
        }
        # surface any non-standard rarity buckets seen (token, special, ...).
        for r in seen_rarities:
            if r not in rarity_counts:
                rarity_counts[r] = by_rarity[r]
        cards_by_rarity = {}
        if args.list:
            for r in seen_rarities:
                cards_by_rarity[r] = [
                    {"name": c.get("name"), "count": e.count}
                    for e, c in sorted(
                        by_rarity_cards[r], key=lambda x: x[1]["name"]
                    )
                ]
        _emit_json({
            "deck": str(path),
            "rarity_counts": rarity_counts,
            "unresolved": len(unknown),
            "cards_by_rarity": cards_by_rarity,
        })
        return 0

    print(f"wildcards: {path} (deck + sideboard)")
    print()
    print("rarity breakdown:")
    for r in seen_rarities:
        print(f"  {r:<10} {by_rarity[r]:>3}")
    if unknown:
        print(f"  unresolved: {len(unknown)} entries (run validate)")
    print()
    if args.list:
        for r in seen_rarities:
            print(f"[{r}]")
            for e, c in sorted(by_rarity_cards[r], key=lambda x: x[1]["name"]):
                print(f"  {e.count:>2}  {c['name']}")
            print()
    return 0


# Companion eligibility: each rule is a pure mechanical predicate over the
# 99 + commander (or main 60 in non-singleton). We dump PASS/FAIL plus the
# specific cards that violate the rule, so the deckbuilder can either pivot
# or amend. Sources: comprehensive rules 702.139 + companion oracle text.
# https://magic.wizards.com/en/rules

_COMPANION_KAHEERA_TYPES = {
    "cat", "elemental", "nightmare", "dinosaur", "beast",
}
_COMPANION_KERUGA_MIN_CMC = 3
_COMPANION_LURRUS_MAX_CMC = 2
_COMPANION_YORION_MIN_DECK = 80


def _is_permanent(c: dict) -> bool:
    t = (c.get("type_line") or "").lower()
    return any(k in t for k in (
        "creature", "artifact", "enchantment", "land",
        "planeswalker", "battle",
    ))


_RX_PAREN = re.compile(r"\([^()]*\)")
_RX_QUOTED = re.compile(r"\"[^\"]*\"")
_RX_TAP_MANA = re.compile(r"\{t\}\s*[,:]", re.IGNORECASE)
# Keyword-shorthand activated abilities (rule 702 — costs printed as
# `<keyword> <cost>` rather than `<cost>: <effect>`). Word boundary via
# negative lookahead to avoid matching `equip`→`equipped`/`equipment`.
_RX_KW_ACT = re.compile(
    r"(?:^|[\s.,;])"
    r"(?:cycling|equip|crew|morph|megamorph|unearth|flashback|"
    r"channel|forecast|fortify|level\s+up|outlast|reinforce|scavenge|"
    r"transmute|transfigure|ninjutsu|commander\s+ninjutsu|"
    r"embalm|eternalize|jump-?start|aftermath|dash|prowl|recover|"
    r"spectacle|surge|emerge|escape|adapt|monstrosity|bestow|crank!|"
    r"crime|saddle|harmonize|craft)"
    r"(?![a-z])",
    re.IGNORECASE,
)


def _has_activated_ability(c: dict) -> bool:
    """Detect activated abilities — oracle lines with `<cost>: <effect>`.

    Mana abilities count (rule 605.1). The naive "any colon on a line"
    test over-fires on (a) reminder text inside parens that grants a
    *token* (not the parent card) an activated ability, (b) quoted
    strings on token-creating spells / Auras that grant abilities to
    something else, and (c) modal lines like `Choose one —`. So:

    1. Iteratively strip paren-reminder text and quoted token-grant
       strings; if any `:` survives in the residue it's an ability of
       the card itself.
    2. Fall back to `{T}:`/`{T},` mana-tap detection on text with
       quotes stripped (basics print their mana ability only as
       reminder text inside parens, but this also catches the explicit
       printed form).
    3. Fall back to keyword-shorthand activated abilities (`Equip 2`,
       `Crew 3`, `Cycling {1}`, morph, etc.) which are real activated
       abilities (rule 702) that don't use the colon syntax.
    """
    text = _all_text(c)
    if not text:
        return False
    stripped = text
    while True:
        nxt = _RX_PAREN.sub(" ", stripped)
        if nxt == stripped:
            break
        stripped = nxt
    stripped_no_quotes = _RX_QUOTED.sub(" ", stripped)
    if ":" in stripped_no_quotes:
        return True
    # Basic lands & some intrinsic-mana cards: tap-for-mana only printed
    # in reminder text, which the paren strip above removed. Re-check
    # against the version with parens kept (quotes still stripped).
    quotes_only_stripped = _RX_QUOTED.sub(" ", text)
    if _RX_TAP_MANA.search(quotes_only_stripped):
        return True
    if _RX_KW_ACT.search(stripped_no_quotes):
        return True
    return False


def _colored_pips(c: dict) -> list[str]:
    out: list[str] = []
    for tok in _PIP_RE.findall(c.get("mana_cost") or ""):
        cols = _pip_colors(tok)
        # Hybrid `{W/U}` is "either" — Jegantha treats that as one pip
        # of either side, satisfied as long as you don't repeat it. We
        # represent it as the sorted-pair string ("W/U") so two `{W/U}`
        # in the same cost still count as a repeat.
        if len(cols) == 1:
            out.append(next(iter(cols)))
        elif len(cols) > 1:
            out.append("/".join(sorted(cols)))
    return out


def _jegantha_ok(c: dict) -> bool:
    pips = _colored_pips(c)
    return len(pips) == len(set(pips))


def _lurrus_ok(c: dict) -> bool:
    return (
        "land" in (c.get("type_line") or "").lower()
        or not _is_permanent(c)
        or (c.get("cmc") or 0) <= _COMPANION_LURRUS_MAX_CMC
    )


def _kaheera_ok(c: dict) -> bool:
    tline = (c.get("type_line") or "").lower()
    if "creature" not in tline:
        return True
    tokens = set(tline.replace("—", " ").split())
    return bool(tokens & _COMPANION_KAHEERA_TYPES)


def _gyruda_ok(c: dict) -> bool:
    return (
        "land" in (c.get("type_line") or "").lower()
        or (c.get("cmc") or 0) % 2 == 0
    )


def _keruga_ok(c: dict) -> bool:
    return (
        "land" in (c.get("type_line") or "").lower()
        or (c.get("cmc") or 0) >= _COMPANION_KERUGA_MIN_CMC
    )


def _obosh_ok(c: dict) -> bool:
    return (
        "land" in (c.get("type_line") or "").lower()
        or (c.get("cmc") or 0) % 2 == 1
    )


def _zirda_ok(c: dict) -> bool:
    return not _is_permanent(c) or _has_activated_ability(c)


# Per-card predicates only. Yorion (deck-size), Umori (single nonland
# type), and Lutri (singleton) are aggregate constraints and don't fit a
# per-card map.
_COMPANION_PREDICATES = {
    "Lurrus of the Dream-Den": _lurrus_ok,
    "Kaheera, the Orphanguard": _kaheera_ok,
    "Jegantha, the Wellspring": _jegantha_ok,
    "Gyruda, Doom of Depths": _gyruda_ok,
    "Keruga, the Macrosage": _keruga_ok,
    "Obosh, the Preypiercer": _obosh_ok,
    "Zirda, the Dawnwaker": _zirda_ok,
}


def cmd_companion(args: argparse.Namespace) -> int:
    """Check each MTGA companion's eligibility predicate against the deck.

    For each companion: PASS or FAIL with the offending cards listed. No
    recommendation — the deckbuilder picks based on what they're building.
    """
    _warn_if_stale()
    path = Path(args.deck)
    if not path.exists():
        print(f"deck file not found: {path}", file=sys.stderr)
        return 2
    entries = parse_deck(path)
    is_brawl = args.format in BRAWL_FORMATS
    if is_brawl:
        sections = {"deck", "commander"}
    else:
        sections = {"deck", "commander", "sideboard"}
    scope = [e for e in entries if e.section in sections]
    # Yorion's threshold is starting-deck size — main + commander only,
    # NOT including sideboard, regardless of format.
    main_total = sum(
        e.count for e in entries if e.section in {"deck", "commander"}
    )
    sb_names = {e.name for e in entries if e.section == "sideboard"}

    cards: list[tuple[DeckEntry, dict]] = []
    for e in scope:
        c = _resolve_card(e.name)
        if c is not None:
            cards.append((e, c))

    def _violations(predicate, label: str) -> list[str]:
        return [
            f"{e.count}x {c['name']}"
            for e, c in cards
            if not predicate(c)
        ]

    def _sb_check(companion: str, viols: list[str]) -> list[str]:
        # Non-Brawl: companion must be in the sideboard at game start. Brawl
        # has no sideboard so this rule does not apply.
        if not is_brawl and companion not in sb_names:
            return viols + [f"{companion} not in sideboard"]
        return viols

    checks: list[tuple[str, list[str]]] = []

    # Lurrus: every nonland permanent card has cmc <= 2.
    bad = _violations(_lurrus_ok, "lurrus")
    checks.append((
        "Lurrus of the Dream-Den (cmc<=2 nonland permanents)",
        _sb_check("Lurrus of the Dream-Den", bad),
    ))

    # Kaheera: every creature card shares a type from the whitelist.
    bad = _violations(_kaheera_ok, "kaheera")
    types_label = "/".join(sorted(_COMPANION_KAHEERA_TYPES))
    checks.append((
        f"Kaheera, the Orphanguard (creatures must be: {types_label})",
        _sb_check("Kaheera, the Orphanguard", bad),
    ))

    # Jegantha: no card has two pips of the same color in its cost.
    bad = _violations(_jegantha_ok, "jegantha")
    checks.append((
        "Jegantha, the Wellspring (no card has repeated colored pip)",
        _sb_check("Jegantha, the Wellspring", bad),
    ))

    # Yorion: starting deck has at least 80 cards.
    yorion_msg: list[str] = (
        []
        if main_total >= _COMPANION_YORION_MIN_DECK
        else [f"deck has {main_total} cards, needs >= {_COMPANION_YORION_MIN_DECK}"]
    )
    checks.append((
        "Yorion, Sky Nomad (>=80-card starting deck)",
        _sb_check("Yorion, Sky Nomad", yorion_msg),
    ))

    # Gyruda: every nonland card has even cmc (0, 2, 4, ...).
    bad = _violations(_gyruda_ok, "gyruda")
    checks.append((
        "Gyruda, Doom of Depths (nonland cards have even cmc)",
        _sb_check("Gyruda, Doom of Depths", bad),
    ))

    # Keruga: every nonland card has cmc >= 3.
    bad = _violations(_keruga_ok, "keruga")
    checks.append((
        "Keruga, the Macrosage (nonland cmc>=3)",
        _sb_check("Keruga, the Macrosage", bad),
    ))

    # Obosh: every nonland card has odd cmc (1, 3, 5, ...).
    bad = _violations(_obosh_ok, "obosh")
    checks.append((
        "Obosh, the Preypiercer (nonland cards have odd cmc)",
        _sb_check("Obosh, the Preypiercer", bad),
    ))

    # Umori: only one card type among nonland cards (excluding land/instant
    # /sorcery toggles? rules text says "card types other than land", which
    # includes instants/sorceries). Spec: nonland cards share a single
    # card type.
    nonland_types: set[str] = set()
    for _, c in cards:
        tline = (c.get("type_line") or "").lower()
        if "land" in tline:
            continue
        for t in (
            "creature", "artifact", "enchantment", "instant",
            "sorcery", "planeswalker", "battle",
        ):
            if t in tline:
                nonland_types.add(t)
    umori_msg = (
        []
        if len(nonland_types) <= 1
        else [f"nonland cards span {len(nonland_types)} types: {sorted(nonland_types)}"]
    )
    checks.append((
        "Umori, the Collector (one nonland card type)",
        _sb_check("Umori, the Collector", umori_msg),
    ))

    # Zirda: every permanent card has an activated ability.
    bad = _violations(_zirda_ok, "zirda")
    checks.append((
        "Zirda, the Dawnwaker (every permanent has activated ability)",
        _sb_check("Zirda, the Dawnwaker", bad),
    ))

    # Lutri: no card appears more than once (singleton, ignoring basic
    # lands). Brawl is already singleton; this check is meaningful for
    # 60-card formats only.
    seen: dict[str, int] = {}
    for e, c in cards:
        seen[c["name"]] = seen.get(c["name"], 0) + e.count
    lutri_violations: list[str] = []
    for name, n in seen.items():
        if n > 1:
            cc = _resolve_card(name)
            if cc is None:
                continue
            t = (cc.get("type_line") or "").lower()
            is_basic = "basic" in t and "land" in t
            if not is_basic:
                lutri_violations.append(f"{n}x {name}")
    checks.append((
        "Lutri, the Spellchaser (singleton, basic lands exempt)",
        _sb_check("Lutri, the Spellchaser", lutri_violations),
    ))

    if getattr(args, "json", False):
        eligible: list[str] = []
        ineligible: dict[str, dict] = {}
        for label, viols in checks:
            # Companion display name is everything before the first " (".
            name = label.split(" (", 1)[0]
            if not viols:
                eligible.append(name)
            else:
                ineligible[name] = {
                    "rule": label,
                    "violations": viols,
                    "violation_count": len(viols),
                }
        _emit_json({
            "deck": str(path),
            "format": args.format,
            "eligible": eligible,
            "ineligible": ineligible,
        })
        return 0

    print(f"companion: {path}")
    print()
    for label, viols in checks:
        if not viols:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            for v in viols[:8]:
                print(f"        - {v}")
            if len(viols) > 8:
                print(f"        ... +{len(viols) - 8} more")
    return 0


# ---------- check (full battery) ----------------------------------------


def _check_divider(label: str) -> None:
    bar = "═" * 3
    print(f"\n{bar} {label} {bar}")


def _capture_json(fn, ns: argparse.Namespace) -> tuple[int, object]:
    """Invoke a sub-command in `--json` mode and capture its payload.

    Used by composite commands (e.g. `check --json`) so they can stitch
    together each stage's structured output without duplicating compute
    paths. Stdout is redirected through a `StringIO` buffer; the buffer
    is parsed as JSON and returned alongside the sub-command's exit
    code. Stderr is left attached to the real fd so warnings still
    surface during the composite run.
    """
    ns.json = True
    buf = io.StringIO()
    saved = sys.stdout
    sys.stdout = buf
    try:
        rc = fn(ns)
    finally:
        sys.stdout = saved
    raw = buf.getvalue().strip()
    payload = json.loads(raw) if raw else None
    return rc, payload


def cmd_check(args: argparse.Namespace) -> int:
    """Run the full validate/analyze/manabase/wildcards/companion battery
    against a deck file, with section dividers between each stage.

    Exit code propagates from `validate` only — the other stages are
    informational dumps, not pass/fail gates. With `--collection`, also
    runs `gaps` after `companion` if a snapshot exists; warns and skips
    otherwise.
    """
    deck = args.deck
    fmt = args.format

    if getattr(args, "json", False):
        rc, validate_payload = _capture_json(
            cmd_validate,
            argparse.Namespace(deck=deck, format=fmt, verbose=False),
        )
        _, analyze_payload = _capture_json(
            cmd_analyze,
            argparse.Namespace(
                deck=deck, include_sideboard=False, sideboard_only=False,
            ),
        )
        _, manabase_payload = _capture_json(
            cmd_manabase, argparse.Namespace(deck=deck),
        )
        _, wildcards_payload = _capture_json(
            cmd_wildcards, argparse.Namespace(deck=deck, list=False),
        )
        _, companion_payload = _capture_json(
            cmd_companion, argparse.Namespace(deck=deck, format=fmt),
        )
        gaps_payload: object = None
        if args.collection:
            if _load_collection() is None:
                print(
                    "[check] --collection requested but no snapshot at "
                    f"{COLLECTION_PATH}; skipping gaps.",
                    file=sys.stderr,
                )
            else:
                _, gaps_payload = _capture_json(
                    cmd_gaps, argparse.Namespace(deck=deck),
                )
        payload = {
            "deck": deck,
            "format": fmt,
            "validate": validate_payload,
            "analyze": analyze_payload,
            "manabase": manabase_payload,
            "wildcards": wildcards_payload,
            "companion": companion_payload,
        }
        if args.collection:
            payload["gaps"] = gaps_payload
        _emit_json(payload)
        return rc

    _check_divider("validate")
    validate_args = argparse.Namespace(deck=deck, format=fmt, verbose=False)
    rc = cmd_validate(validate_args)

    _check_divider("analyze")
    cmd_analyze(argparse.Namespace(
        deck=deck, include_sideboard=False, sideboard_only=False,
    ))

    _check_divider("manabase")
    cmd_manabase(argparse.Namespace(deck=deck))

    _check_divider("wildcards")
    cmd_wildcards(argparse.Namespace(deck=deck, list=False))

    _check_divider("companion")
    cmd_companion(argparse.Namespace(deck=deck, format=fmt))

    if args.collection:
        if _load_collection() is None:
            print(
                "[check] --collection requested but no snapshot at "
                f"{COLLECTION_PATH}; skipping gaps.",
                file=sys.stderr,
            )
        else:
            _check_divider("gaps")
            cmd_gaps(argparse.Namespace(deck=deck))

    return rc


# ---------- search (live) ------------------------------------------------


def _scryfall_search_url(query: str) -> str:
    return f"{SCRYFALL_API}/cards/search?q={urllib.parse.quote(query)}&unique=cards"


def _scryfall_search_all(query: str) -> list[dict]:
    """Fetch every page of a Scryfall search. Returns [] on 404 (no match)."""
    url: str | None = _scryfall_search_url(query)
    out: list[dict] = []
    first = True
    while url:
        if not first:
            # Scryfall asks for 50–100ms between requests; pause between pages.
            time.sleep(0.1)
        first = False
        try:
            data = _get_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        out.extend(data.get("data") or [])
        url = data.get("next_page") if data.get("has_more") else None
    return out


def cmd_search(args: argparse.Namespace) -> int:
    q = args.query
    url = _scryfall_search_url(q)
    try:
        data = _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            if getattr(args, "json", False):
                _emit_json({
                    "query": q,
                    "limit": args.limit,
                    "total_cards": 0,
                    "shown": 0,
                    "has_more": False,
                    "cards": [],
                })
                return 1
            print("no cards matched")
            return 1
        raise
    cards = data.get("data") or []
    shown = cards[: args.limit]
    if getattr(args, "json", False):
        _emit_json({
            "query": q,
            "limit": args.limit,
            "total_cards": data.get("total_cards", len(cards)),
            "shown": len(shown),
            "has_more": bool(data.get("has_more")),
            "cards": [_card_to_json(c) for c in shown],
        })
        return 0
    print(f"{data.get('total_cards', len(cards))} match(es); showing {len(cards)}:")
    for c in shown:
        ci = "".join(c.get("color_identity") or []) or "C"
        on_arena = "✓" if "arena" in (c.get("games") or []) else " "
        print(
            f"  [{on_arena}] {c['name']:<40} {c['set'].upper():<5} "
            f"{c.get('type_line', ''):<30} {ci}"
        )
    if data.get("has_more"):
        print(f"  ... has_more=True (rerun on Scryfall for full list)")
    return 0


# ---------- collection (canonical store + importers + queries) ----------

# Modern MTGA (2025+) no longer dumps the card collection to Player.log.
# The legacy marker `<== PlayerInventory.GetPlayerCardsV3` is gone, and
# `StartHook.InventoryInfo` carries currencies + boosters but no cards.
# The trackers that DO extract a snapshot (mtgap, Untapped Companion)
# inject a C# DLL into the MTGA process and read
# `WrapperController.Instance.InventoryManager.Cards` from live memory —
# out of scope for a deckbuilding toolkit.
#
# Strategy: be the analysis layer, not the tracker layer. Consume snapshot
# exports from whichever tracker the user already runs. Canonical on-disk
# shape is one JSON file under `data/collection.json` with `{arena_id:
# count}` plus metadata. All queries read this; all importers write this.

_MTGA_STEAM_APPID = "2141910"
COLLECTION_PATH = DATA / "collection.json"


def _warn_if_collection_stale(max_age_d: float = 7.0) -> None:
    if not COLLECTION_PATH.exists():
        return
    age_d = (time.time() - COLLECTION_PATH.stat().st_mtime) / 86400
    if age_d > max_age_d:
        print(
            f"[warn] collection snapshot is {age_d:.1f}d old; "
            f"consider `mtg collection dump`",
            file=sys.stderr,
        )


def _candidate_log_paths() -> list[Path]:
    home = Path.home()
    proton = (
        home
        / ".steam/steam/steamapps/compatdata"
        / _MTGA_STEAM_APPID
        / "pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA"
    )
    proton_alt = (
        home
        / ".local/share/Steam/steamapps/compatdata"
        / _MTGA_STEAM_APPID
        / "pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA"
    )
    mac = home / "Library/Logs/Wizards Of The Coast/MTGA"
    win_appdata = os.environ.get("APPDATA")
    win_dir = (
        Path(win_appdata).parent / "LocalLow/Wizards Of The Coast/MTGA"
        if win_appdata
        else None
    )
    out: list[Path] = []
    for d in (proton, proton_alt, mac, win_dir):
        if d is None:
            continue
        for name in ("Player.log", "Player-prev.log"):
            out.append(d / name)
    return out


def _resolve_log_path(arg: str | None) -> Path:
    if arg:
        p = Path(arg).expanduser()
        if not p.exists():
            sys.exit(f"log not found: {p}")
        return p
    for p in _candidate_log_paths():
        if p.exists() and p.stat().st_size > 0:
            return p
    sys.exit(
        "no MTGA Player.log found in standard locations. Pass --log <path>.\n"
        "Linux/Proton path: ~/.steam/steam/steamapps/compatdata/2141910/pfx/"
        "drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
    )


def _detailed_logs_enabled(text: str) -> bool | None:
    last: bool | None = None
    for m in re.finditer(r"DETAILED LOGS: (ENABLED|DISABLED)", text):
        last = m.group(1) == "ENABLED"
    return last


def _scene_trace(text: str) -> list[str]:
    """Ordered list of `toSceneName` values from Client.SceneChange events."""
    return re.findall(r'"toSceneName"\s*:\s*"([^"]+)"', text)


def _marker_counts(text: str) -> dict[str, int]:
    """Count of `<== <Marker>` response markers in the log."""
    counts: dict[str, int] = {}
    for m in re.finditer(r"<==\s*([A-Za-z_][\w.]*)", text):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def _scan_balanced(text: str, start: int) -> int:
    """Index just past the matching close-brace for `text[start]`.

    Tracks string literals so braces inside JSON strings don't count.
    Returns -1 on unbalanced input.
    """
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return -1


def _scan_json_blobs(text: str) -> list[tuple[int, str, Any]]:
    """List `(offset, marker, parsed)` for each `<== <marker>{...}` blob.

    Some builds emit `<== marker(arg)\\n{...}`; we skip non-`{`/`[` chars
    between the marker and the brace. Blobs span multiple lines —
    bracket-balanced.
    """
    out: list[tuple[int, str, Any]] = []
    for m in re.finditer(r"<==\s*([A-Za-z_][\w.]*)", text):
        marker = m.group(1)
        i = m.end()
        n = len(text)
        while i < n and text[i] not in "{[":
            if text[i : i + 3] in ("<==", "==>"):
                i = -1
                break
            i += 1
        if i < 0 or i >= n:
            continue
        end = _scan_balanced(text, i)
        if end < 0:
            continue
        try:
            parsed = json.loads(text[i:end])
        except json.JSONDecodeError:
            continue
        out.append((m.start(), marker, parsed))
    return out


def _load_collection() -> dict | None:
    if not COLLECTION_PATH.exists():
        return None
    try:
        return json.loads(COLLECTION_PATH.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"corrupt {COLLECTION_PATH}: {e}")


def _save_collection(
    cards: dict[int, int], *, source: str, completeness: str
) -> Path:
    import datetime as _dt

    payload = {
        "snapshot_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "completeness": completeness,
        "cards": {str(k): v for k, v in sorted(cards.items())},
    }
    COLLECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    COLLECTION_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    return COLLECTION_PATH


def _cards_owned(snap: dict) -> dict[int, int]:
    return {int(k): int(v) for k, v in (snap.get("cards") or {}).items()}


# ---- importers (each returns {arena_id: count}) ------------------------


def _import_csv(path: Path, idx: dict) -> dict[int, int]:
    """Parse CSV. Detects arena_id column or (set+collector_number) pair."""
    import csv

    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            sys.exit(f"empty CSV: {path}")
        headers = {h.strip().lower(): h for h in reader.fieldnames}
        arena_col = next(
            (
                headers[k]
                for k in ("arena_id", "arenaid", "cardid", "card_id", "grpid", "grp_id")
                if k in headers
            ),
            None,
        )
        qty_col = next(
            (headers[k] for k in ("quantity", "qty", "count", "amount") if k in headers),
            None,
        )
        if qty_col is None:
            sys.exit(
                f"CSV missing quantity column. Headers: {reader.fieldnames}\n"
                "Expected one of: quantity, qty, count, amount"
            )

        out: dict[int, int] = {}
        unresolved: list[str] = []

        if arena_col:
            for row in reader:
                aid = (row.get(arena_col) or "").strip()
                qty = (row.get(qty_col) or "").strip()
                if not aid or not qty:
                    continue
                try:
                    out[int(aid)] = out.get(int(aid), 0) + int(qty)
                except ValueError:
                    unresolved.append(f"{aid!r} qty={qty!r}")
            if unresolved:
                print(
                    f"[warn] skipped {len(unresolved)} non-numeric rows",
                    file=sys.stderr,
                )
            return out

        # Fall back to set+collector_number resolution.
        set_col = next(
            (headers[k] for k in ("set", "set_code", "edition") if k in headers),
            None,
        )
        cn_col = next(
            (
                headers[k]
                for k in ("collector_number", "number", "card_number", "collector")
                if k in headers
            ),
            None,
        )
        if not (set_col and cn_col):
            sys.exit(
                "CSV must contain either an arena_id column "
                "(arena_id/cardId/grpId) or both a set and a collector-number "
                f"column. Found: {reader.fieldnames}"
            )

        for row in reader:
            qty_raw = (row.get(qty_col) or "").strip()
            if not qty_raw:
                continue
            try:
                qty = int(qty_raw)
            except ValueError:
                continue
            if qty <= 0:
                continue
            set_code = (row.get(set_col) or "").strip().lower()
            cn = (row.get(cn_col) or "").strip()
            card = idx["by_printing"].get((set_code, cn))
            if card is None:
                unresolved.append(f"{set_code.upper()} {cn}")
                continue
            aid = card.get("arena_id")
            if not isinstance(aid, int):
                unresolved.append(f"{set_code.upper()} {cn} (no arena_id)")
                continue
            out[aid] = out.get(aid, 0) + qty

        if unresolved:
            print(
                f"[warn] {len(unresolved)} rows could not resolve to an arena_id "
                f"(first: {unresolved[0]}); rerun `mtg sync` if these are recent",
                file=sys.stderr,
            )
        return out


def _import_json(path: Path, idx: dict) -> dict[int, int]:
    """Parse JSON. Accepts:
    - flat dict `{"<arena_id>": count}`
    - our canonical `{"cards": {"<arena_id>": count}, ...}`
    - list `[{"arena_id": int, "quantity": int}, ...]`
    - list `[{"set": "...", "collector_number": "...", "quantity": ...}]`
    """
    raw = json.loads(path.read_text())
    out: dict[int, int] = {}

    def _bump(aid: int, qty: int) -> None:
        if qty > 0:
            out[aid] = out.get(aid, 0) + qty

    if isinstance(raw, dict):
        if "cards" in raw and isinstance(raw["cards"], dict):
            raw = raw["cards"]
        if all(isinstance(k, str) and k.lstrip("-").isdigit() for k in raw.keys()):
            for k, v in raw.items():
                if isinstance(v, int):
                    _bump(int(k), v)
            return out

    if isinstance(raw, list):
        unresolved = 0
        for item in raw:
            if not isinstance(item, dict):
                continue
            qty = item.get("quantity") or item.get("count") or item.get("qty")
            if not isinstance(qty, int) or qty <= 0:
                continue
            aid = item.get("arena_id") or item.get("cardId") or item.get("grpId")
            if isinstance(aid, int):
                _bump(aid, qty)
                continue
            set_code = (item.get("set") or item.get("set_code") or "").lower()
            cn = str(item.get("collector_number") or item.get("number") or "").strip()
            if set_code and cn:
                card = idx["by_printing"].get((set_code, cn))
                if card and isinstance(card.get("arena_id"), int):
                    _bump(card["arena_id"], qty)
                    continue
            unresolved += 1
        if unresolved:
            print(f"[warn] skipped {unresolved} list items with no arena_id route",
                  file=sys.stderr)
        return out

    sys.exit(
        f"unrecognized JSON shape in {path}. Expected one of:\n"
        "  - flat {arena_id: count} dict\n"
        "  - {\"cards\": {...}} canonical wrapper\n"
        "  - list of {arena_id, quantity}"
    )


def _import_auto(path: Path, idx: dict) -> dict[int, int]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _import_json(path, idx)
    if suffix == ".csv":
        return _import_csv(path, idx)
    head = path.read_text()[:1].strip()
    if head in ("{", "["):
        return _import_json(path, idx)
    return _import_csv(path, idx)


# ---- from-decks fallback ----------------------------------------------


def _decks_from_log(text: str) -> tuple[dict[int, int], int]:
    """Sum cardId quantities across every deck in EventGetCoursesV2 blobs.

    Returns (cardId→count, deck_count). Cards owned but never decked
    are missed entirely — this is a strict lower bound. Every deck the
    user has built contributes to the union; we cap at 4× per non-basic
    name later in the query layer.
    """
    blobs = _scan_json_blobs(text)
    cards: dict[int, int] = {}
    deck_count = 0
    sections = ("MainDeck", "Sideboard", "CommandZone", "Companions")
    for _off, _marker, parsed in blobs:
        courses = None
        if isinstance(parsed, dict):
            courses = parsed.get("Courses")
        if not isinstance(courses, list):
            continue
        for course in courses:
            if not isinstance(course, dict):
                continue
            deck = course.get("CourseDeck")
            if not isinstance(deck, dict):
                continue
            deck_count += 1
            for sec in sections:
                items = deck.get(sec)
                if not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    aid = it.get("cardId")
                    qty = it.get("quantity")
                    if isinstance(aid, int) and isinstance(qty, int) and qty > 0:
                        cards[aid] = max(cards.get(aid, 0), qty)
    return cards, deck_count


# ---- query helpers ----------------------------------------------------


def _aggregate_by_name(
    idx: dict, cards: dict[int, int]
) -> dict[str, dict]:
    """Roll printing-level counts up to the card name.

    Returns name_lc -> {"name": display_name, "owned": int, "rarity": str}.
    Rarity is the *highest* rarity seen across resolved printings (cards
    can be rare in one set and uncommon in another — wildcard cost is
    the higher value, but reprinting at lower rarity means MTGA usually
    accepts the lower wildcard, which is *not* something we predict
    here; we take the most-recent printing's rarity instead).
    """
    by_name: dict[str, dict] = {}
    unresolved: dict[int, int] = {}
    for aid, count in cards.items():
        card = idx["by_arena_id"].get(aid)
        if card is None:
            unresolved[aid] = count
            continue
        nm = (card.get("name") or "").strip()
        key = nm.lower()
        slot = by_name.setdefault(
            key,
            {
                "name": nm,
                "owned": 0,
                "rarity": card.get("rarity", "common"),
                "type_line": card.get("type_line", ""),
            },
        )
        slot["owned"] += count
        # Prefer the highest-rarity printing's rarity for wildcard math.
        if _RARITY_ORDER.get(card.get("rarity"), 0) > _RARITY_ORDER.get(
            slot["rarity"], 0
        ):
            slot["rarity"] = card.get("rarity")
    return by_name


_RARITY_ORDER = {"common": 1, "uncommon": 2, "rare": 3, "mythic": 4}

# Composite-rank tier weights for `coverage --batch --rank composite`.
# Multiplied against (with_subs_pct or owned_pct) to favour high-tier
# decks the user can mostly build. Anything outside S/A/B/C — including
# tierless decks (no meta.json or null tier) — gets the default 0.40 so
# they sort below ranked decks but aren't zeroed out.
_TIER_WEIGHTS = {"S": 1.0, "A": 0.85, "B": 0.70, "C": 0.55}
_TIER_WEIGHT_DEFAULT = 0.40


def _tier_weight(tier: str | None) -> float:
    """Composite-score weight for a deck's meta.json tier letter.

    Falls back to `_TIER_WEIGHT_DEFAULT` for missing / unknown tiers.
    """
    return _TIER_WEIGHTS.get((tier or "").upper(), _TIER_WEIGHT_DEFAULT)

# Keywords on cards everywhere — Flying / Trample / Vigilance / etc. They
# match thousands of cards apiece and tell you nothing about a synergy
# cluster. `cmd_shells` filters them out so the keyword bucketer surfaces
# named mechanics (Blitz, Survival, Squad, Repartee, Aftermath, …) only.
_EVERGREEN_KEYWORDS = frozenset({
    "Flying",
    "First strike",
    "Deathtouch",
    "Haste",
    "Vigilance",
    "Lifelink",
    "Trample",
    "Reach",
    "Menace",
    "Hexproof",
    "Defender",
    "Flash",
    "Ward",
    "Indestructible",
})
_BASIC_NAMES = frozenset({"plains", "island", "swamp", "mountain", "forest", "wastes"})


def _is_basic(card_or_name: dict | str) -> bool:
    if isinstance(card_or_name, str):
        nm = card_or_name
    else:
        nm = card_or_name.get("name") or ""
        tl = (card_or_name.get("type_line") or "").lower()
        if "basic" in tl and "land" in tl:
            return True
    return nm.split(" // ", 1)[0].strip().lower() in _BASIC_NAMES


# ---- inject (live MTGA dump via Mono DLL injection) -------------------
#
# `collection dump` is the only path that captures cards owned but never
# decked. We compile two assemblies under tools/inject/ (payload + injector)
# from a Nix-pinned dotnet SDK, then run the injector exe inside the same
# Wine prefix as the running MTGA. The injector locates MTGA's mono.dll,
# loads the payload, and invokes `MtgInventoryPayload.Loader.Load()`. The
# payload installs a MonoBehaviour that polls
# `WrapperController.Instance.InventoryManager.Cards` until it's populated,
# then serializes the Dictionary<int,int> with MTGA's bundled
# Newtonsoft.Json and writes JSON to a path supplied through a sidecar
# config (Path.GetTempPath()/mtg-toolkit-inject/config.json — both processes
# share a Wine prefix so the path resolves identically).
#
# Wine path translation: passing `Z:\home\hybridz\...\out.json` to the
# .NET-side File.WriteAllText lands the bytes at `/home/hybridz/.../out.json`
# on the Linux side, so Python reads the dump directly with no marshalling.
#
# Pressure-vessel namespace: Steam Linux Runtime launches MTGA inside its
# own mount namespace (and a user namespace for path remapping). Wine's
# IPC socket lives at /tmp/.wine-$uid/server-<dev>-<inode>/socket *inside*
# that namespace; from the host the socket is invisible, so a fresh
# `wine` invocation spawns its own wineserver and sees a disjoint process
# table — `Process.GetProcesses()` then can't find MTGA.exe. We join the
# sandbox via `nsenter -m -U --preserve-credentials -t <wineserver-pid>`
# before launching the injector, which puts the injector in the same
# mount + user namespace as MTGA so it shares the wineserver socket and
# sees MTGA.exe in its process list. The pressure-vessel rootfs already
# carries an FHS-compatible dynamic linker, so the Nix-built injector exe
# loads cleanly inside the namespace without further runtime shimming.

_INJECT_BUILD = ROOT / "tools" / "inject" / "build"
_INJECT_PAYLOAD_DLL = _INJECT_BUILD / "payload" / "MtgInventoryPayload.dll"
_INJECT_INJECTOR_EXE = _INJECT_BUILD / "injector" / "mtg-inject.exe"
_INJECT_TIMEOUT = 150  # payload polls 120s; allow slack for injector startup


def _linux_to_wine_z(p: Path) -> str:
    """Translate an absolute Linux path to its Wine `Z:` drive form."""
    s = str(p.resolve())
    if not s.startswith("/"):
        sys.exit(f"injector requires absolute path: {p}")
    return "Z:" + s.replace("/", "\\")


def _find_mtga_compatdata() -> Path:
    home = Path.home()
    candidates = [
        home / f".steam/steam/steamapps/compatdata/{_MTGA_STEAM_APPID}",
        home / f".local/share/Steam/steamapps/compatdata/{_MTGA_STEAM_APPID}",
    ]
    for c in candidates:
        if (c / "pfx").is_dir():
            return c
    sys.exit(
        f"MTGA Proton compatdata not found at:\n"
        + "\n".join(f"  {c}" for c in candidates)
        + "\nLaunch MTGA via Steam at least once before running `collection dump`."
    )


def _find_proton_wine() -> Path:
    """Locate the wine binary inside a Proton install.

    `proton run` is a Python wrapper that bootstraps the Steam Linux
    Runtime container and blocks on Steam IPC pipes that aren't fed
    when Proton is invoked outside the launcher (Xalia stalls waiting
    on a display). We bypass that and call the underlying wine
    directly — the same prefix is reused via $WINEPREFIX, so wineserver
    state is shared with the running game.

    Honors $MTG_PROTON_WINE for explicit override (path to a wine binary).
    """
    override = os.environ.get("MTG_PROTON_WINE")
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            sys.exit(f"$MTG_PROTON_WINE points at missing path: {p}")
        return p
    home = Path.home()
    roots = [
        home / ".steam/steam/steamapps/common",
        home / ".local/share/Steam/steamapps/common",
    ]
    candidates: list[Path] = []
    for r in roots:
        if not r.is_dir():
            continue
        for entry in r.iterdir():
            if entry.is_dir() and entry.name.startswith("Proton"):
                wine_bin = entry / "files" / "bin" / "wine"
                if wine_bin.is_file():
                    candidates.append(wine_bin)
    if not candidates:
        sys.exit(
            "no Proton wine binary found under steamapps/common/Proton*/files/bin/wine. "
            "Install Proton from Steam or set $MTG_PROTON_WINE."
        )
    def rank(p: Path) -> tuple[int, str]:
        n = p.parents[2].name
        if "Hotfix" in n:
            return (3, n)
        if "Experimental" in n:
            return (2, n)
        return (1, n)
    candidates.sort(key=rank, reverse=True)
    return candidates[0]


def _find_mtga_wineserver_pid(compatdata: Path) -> int | None:
    """PID of the wineserver attached to MTGA's prefix, or None if absent.

    We scan /proc for a process named `wineserver` whose environ contains
    `WINEPREFIX=<compatdata>/pfx`. Used as the nsenter target so the
    injector enters the pressure-vessel sandbox and shares MTGA's
    wineserver session. Returns None when no such wineserver exists —
    that's also the canonical signal that MTGA isn't running, since
    Steam's Proton wineserver lives only as long as MTGA does.
    """
    # `.steam/steam` is usually a symlink to `~/.local/share/Steam`; the
    # wineserver records WINEPREFIX with whichever spelling Steam exec'd
    # it under. Compare resolved real paths to dodge that mismatch.
    pfx_real = (compatdata / "pfx").resolve()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if not comm.startswith("wineserver"):
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        for var in environ.split(b"\0"):
            if not var.startswith(b"WINEPREFIX="):
                continue
            value = var[len(b"WINEPREFIX="):].decode("utf-8", "replace")
            try:
                if Path(value).resolve() == pfx_real:
                    return int(entry.name)
            except OSError:
                continue
    return None


def _nsenter_bin() -> Path:
    for cand in ("/usr/bin/nsenter", "/run/current-system/sw/bin/nsenter"):
        p = Path(cand)
        if p.is_file():
            return p
    found = shutil.which("nsenter")
    if found:
        return Path(found)
    sys.exit(
        "`nsenter` not found. Install util-linux (it ships with most distros) "
        "so the injector can join MTGA's pressure-vessel sandbox."
    )


def _verify_inject_artifacts() -> None:
    missing = [p for p in (_INJECT_PAYLOAD_DLL, _INJECT_INJECTOR_EXE) if not p.exists()]
    if not missing:
        return
    sys.exit(
        "missing build artifacts:\n"
        + "\n".join(f"  {p}" for p in missing)
        + "\n\nBuild them inside the Nix dev shell:\n"
        "  cd tools/inject/payload  && dotnet build -c Release\n"
        "  cd tools/inject/injector && dotnet build -c Release"
    )


def _detect_mtga_build(compatdata: Path) -> str:
    """Best-effort MTGA build version for the snapshot's `source` field."""
    info = compatdata / "pfx/drive_c/Program Files/Wizards of the Coast/MTGA"
    for name in ("MTGAVersion.txt", "version.txt"):
        f = info / name
        if f.exists():
            try:
                return f.read_text(errors="replace").strip().splitlines()[0]
            except OSError:
                pass
    # Fallback: stat MTGA.exe under the install dir we can locate.
    home = Path.home()
    for root in (
        home / ".steam/steam/steamapps/common/MTGA",
        home / ".local/share/Steam/steamapps/common/MTGA",
    ):
        exe = root / "MTGA.exe"
        if exe.exists():
            return f"mtime={int(exe.stat().st_mtime)}"
    return "unknown"


def _inject_dump(out_json: Path) -> dict[int, int]:
    _verify_inject_artifacts()

    compatdata = _find_mtga_compatdata()
    wine = _find_proton_wine()
    # Absence of a wineserver bound to MTGA's prefix is our canonical
    # "MTGA isn't running" signal — Proton's wineserver lifecycle is
    # tied to the game process, so we don't need a separate pgrep.
    wineserver_pid = _find_mtga_wineserver_pid(compatdata)
    if wineserver_pid is None:
        sys.exit(
            "MTGA is not running (no wineserver bound to its Proton prefix).\n"
            "Launch the game via Steam, sign in to the main menu (so the\n"
            "inventory hydrates), then re-run `tools/mtg collection dump`."
        )
    nsenter = _nsenter_bin()
    err_path = out_json.with_suffix(out_json.suffix + ".err")

    # Clean prior outputs so we know any new file is from this run.
    for p in (out_json, err_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    out_json.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["WINEPREFIX"] = str(compatdata / "pfx")
    # Suppress the menubuilder spam Wine emits on every startup.
    existing_overrides = env.get("WINEDLLOVERRIDES", "")
    sep = ";" if existing_overrides else ""
    env["WINEDLLOVERRIDES"] = f"{existing_overrides}{sep}winemenubuilder.exe=d"

    cmd = [
        str(nsenter),
        "--target", str(wineserver_pid),
        "--mount",
        "--user",
        "--preserve-credentials",
        "--",
        str(wine),
        str(_INJECT_INJECTOR_EXE),
        "--payload",
        _linux_to_wine_z(_INJECT_PAYLOAD_DLL),
        "--out",
        _linux_to_wine_z(out_json),
    ]
    proton_label = wine.parents[2].name
    print(
        f"injecting via {proton_label} wine (nsenter pid={wineserver_pid}) → MTGA…",
        file=sys.stderr,
    )
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(
            f"injector exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    if proc.stdout.strip():
        for line in proc.stdout.strip().splitlines():
            print(f"  {line}", file=sys.stderr)

    # Payload is now polling inside MTGA. Wait for the dump to land.
    deadline = time.time() + _INJECT_TIMEOUT
    while time.time() < deadline:
        if err_path.exists():
            sys.exit(f"payload error:\n{err_path.read_text()}")
        if out_json.exists():
            break
        time.sleep(1)
    else:
        sys.exit(
            f"timeout: payload did not produce {out_json} within {_INJECT_TIMEOUT}s.\n"
            "Confirm MTGA is on the main menu (post sign-in) and retry."
        )

    raw = json.loads(out_json.read_text())
    if not isinstance(raw, dict):
        sys.exit(f"unexpected payload output shape: {type(raw).__name__}")
    try:
        return {int(k): int(v) for k, v in raw.items()}
    except (TypeError, ValueError) as e:
        sys.exit(f"non-integer keys/values in dump: {e}")


# ---- commands ---------------------------------------------------------


def _empty_state_message() -> str:
    return (
        f"No collection imported yet ({COLLECTION_PATH} missing).\n"
        "\n"
        "Modern MTGA does not dump the card pool to Player.log. Three ways\n"
        "to populate the canonical snapshot:\n"
        "\n"
        "  1. Dump live from the running MTGA process (full pool, no tracker):\n"
        "     tools/mtg collection dump\n"
        "     Builds a payload DLL from tools/inject/ and injects it via\n"
        "     SharpMonoInjector. Requires MTGA running on the main menu.\n"
        "\n"
        "  2. Import a tracker export:\n"
        "       MTGA Pro Tracker  →  exports CSV with cardId,quantity\n"
        "       Untapped Companion →  exports CSV with set+collector+quantity\n"
        "     tools/mtg collection import ~/Downloads/collection.csv\n"
        "\n"
        "  3. Lower-bound from your own decks (fast, no extra tools — but\n"
        "     misses every card you haven't decked, which is most of them):\n"
        "     tools/mtg collection from-decks\n"
    )


def cmd_collection(args: argparse.Namespace) -> int:
    _warn_if_collection_stale()
    snap = _load_collection()
    if snap is None:
        if getattr(args, "json", False):
            _emit_json({
                "snapshot_at": None,
                "source": None,
                "completeness": None,
                "unique_arena_ids": 0,
                "unique_names": 0,
                "total_copies": 0,
                "rarity_unique": {},
                "rarity_owned": {},
            })
            return 1
        sys.stdout.write(_empty_state_message())
        return 1
    cards = _cards_owned(snap)
    idx = _load_index()
    by_name = _aggregate_by_name(idx, cards)
    rarity_owned: dict[str, int] = {}
    rarity_unique: dict[str, int] = {}
    for slot in by_name.values():
        r = slot["rarity"] or "common"
        rarity_owned[r] = rarity_owned.get(r, 0) + slot["owned"]
        rarity_unique[r] = rarity_unique.get(r, 0) + 1

    if getattr(args, "json", False):
        _emit_json({
            "snapshot_at": snap.get("snapshot_at"),
            "source": snap.get("source"),
            "completeness": snap.get("completeness"),
            "unique_arena_ids": len(cards),
            "unique_names": len(by_name),
            "total_copies": sum(cards.values()),
            "rarity_unique": rarity_unique,
            "rarity_owned": rarity_owned,
        })
        return 0

    print(f"snapshot:    {snap.get('snapshot_at')}")
    print(f"source:      {snap.get('source')}")
    print(f"completeness: {snap.get('completeness')}")
    print()
    print(f"unique arena_ids: {len(cards)}")
    print(f"unique names:     {len(by_name)}")
    print(f"total copies:     {sum(cards.values())}")
    print()
    print("by rarity (unique names / total copies):")
    for r in ("mythic", "rare", "uncommon", "common"):
        if r in rarity_unique:
            print(f"  {r:<10} {rarity_unique[r]:>5}  /  {rarity_owned[r]:>6}")
    if snap.get("completeness") == "lower-bound":
        print()
        print(
            "[lower-bound] this snapshot only covers cards you have decked;\n"
            "cards owned but never used are NOT included. To capture your\n"
            "full pool, import from a tracker — see `tools/mtg collection`\n"
            "with no snapshot."
        )
    return 0


def cmd_collection_import(args: argparse.Namespace) -> int:
    _warn_if_stale()
    _warn_if_collection_stale()
    src = Path(args.file).expanduser()
    if not src.exists():
        sys.exit(f"file not found: {src}")
    idx = _load_index()
    cards = _import_auto(src, idx)
    if not cards:
        sys.exit(f"no cards imported from {src}")
    out = _save_collection(cards, source=f"import:{src.name}", completeness="full")
    print(
        f"imported {len(cards)} unique arena_ids "
        f"({sum(cards.values())} total copies) → {out}",
        file=sys.stderr,
    )
    return 0


def cmd_collection_from_decks(args: argparse.Namespace) -> int:
    _warn_if_stale()
    _warn_if_collection_stale()
    log_path = _resolve_log_path(args.log)
    print(f"reading log: {log_path}", file=sys.stderr)
    text = log_path.read_text(errors="replace")
    enabled = _detailed_logs_enabled(text)
    if enabled is False:
        sys.exit(
            "Detailed Logs are DISABLED in this Player.log.\n"
            "Enable them in MTGA: Settings → Account → "
            "'Detailed Logs (Plugin Support)' → ON, restart MTGA, sign in, "
            "open the Decks tab once, then re-run."
        )
    cards, deck_count = _decks_from_log(text)
    if not cards:
        scenes = _scene_trace(text)
        markers = sorted(_marker_counts(text).items(), key=lambda kv: -kv[1])[:5]
        sys.exit(
            f"no decks found in {log_path}.\n"
            f"Scenes: {' → '.join(scenes) or '(none)'}\n"
            f"Top markers: {', '.join(f'{k}×{v}' for k, v in markers) or '(none)'}\n"
            "Open the Decks tab in MTGA so it emits EventGetCoursesV2, then "
            "re-run."
        )
    out = _save_collection(
        cards,
        source=f"from-decks:{deck_count}-decks",
        completeness="lower-bound",
    )
    print(
        f"reconstructed {len(cards)} unique arena_ids from {deck_count} "
        f"decks → {out}",
        file=sys.stderr,
    )
    print(
        "[lower-bound] only cards you have decked are present. Cards owned "
        "but never used are missing. Import from a tracker for a full pool.",
        file=sys.stderr,
    )
    return 0


def cmd_collection_dump(args: argparse.Namespace) -> int:
    _warn_if_stale()
    _warn_if_collection_stale()
    out_path = Path(args.out).expanduser() if args.out else (DATA / "collection.dump.json")
    cards = _inject_dump(out_path)
    if not cards:
        sys.exit(
            "dump returned 0 cards — MTGA's InventoryManager.Cards was empty.\n"
            "Sign in to the main menu (not the splash screen) and retry."
        )
    compatdata = _find_mtga_compatdata()
    build = _detect_mtga_build(compatdata)
    out = _save_collection(cards, source=f"inject:mtga@{build}", completeness="full")
    print(
        f"dumped {len(cards)} unique arena_ids "
        f"({sum(cards.values())} total copies) → {out}",
        file=sys.stderr,
    )
    return 0


def cmd_own(args: argparse.Namespace) -> int:
    _warn_if_collection_stale()
    snap = _load_collection()
    if snap is None:
        if getattr(args, "json", False):
            _emit_json({
                "name": args.name,
                "found": False,
                "error": "no collection snapshot",
            })
            return 1
        sys.exit(_empty_state_message().rstrip())
    idx = _load_index()
    name_lc = _normalize_name(args.name)
    printings = idx["by_name"].get(name_lc) or []
    if not printings:
        if getattr(args, "json", False):
            _emit_json({
                "name": args.name,
                "found": False,
                "error": "unknown card",
            })
            return 1
        sys.exit(f"unknown card: {args.name}")
    cards = _cards_owned(snap)
    by_name = _aggregate_by_name(idx, cards)
    slot = by_name.get(name_lc)
    owned = slot["owned"] if slot else 0
    canonical = printings[0]["name"]
    rarity = (slot or {}).get("rarity") or printings[0].get("rarity", "?")
    is_basic = _is_basic(canonical)
    target = 1 if is_basic else 4
    short = max(0, target - owned)
    if getattr(args, "json", False):
        _emit_json({
            "name": canonical,
            "found": True,
            "rarity": rarity,
            "owned": owned,
            "target": target,
            "short": short,
            "is_basic": is_basic,
        })
        return 0
    print(f"{canonical}  [{rarity}]")
    print(f"  owned: {owned}")
    if is_basic:
        print("  basic land — MTGA gives unlimited copies, ignore counts")
    else:
        print(f"  4-of target: {owned}/{target}  (short {short})")
    return 0


def cmd_owned(args: argparse.Namespace) -> int:
    _warn_if_collection_stale()
    snap = _load_collection()
    if snap is None:
        print(
            "no collection snapshot — run 'tools/mtg collection dump' first",
            file=sys.stderr,
        )
        return 2
    cards_owned = _cards_owned(snap)
    try:
        results = _scryfall_search_all(args.query)
    except urllib.error.HTTPError as e:
        print(f"Scryfall HTTP {e.code}: {e.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Scryfall request failed: {e.reason}", file=sys.stderr)
        return 1
    if not results:
        print(f"no Scryfall matches for: {args.query}", file=sys.stderr)
        return 0

    arena_eligible = sum(1 for c in results if isinstance(c.get("arena_id"), int))

    rows: list[tuple[int, str, str, str, str, float | None, str]] = []
    for c in results:
        aid = c.get("arena_id")
        if not isinstance(aid, int):
            continue
        qty = cards_owned.get(aid, 0)
        if qty < args.min:
            continue
        rows.append(
            (
                qty,
                c.get("name") or "",
                (c.get("set") or "").upper(),
                c.get("collector_number") or "",
                c.get("rarity") or "?",
                c.get("cmc") if isinstance(c.get("cmc"), (int, float)) else None,
                c.get("type_line") or "",
            )
        )

    if args.unique:
        collapsed: dict[str, tuple[int, str, str, str, str, float | None, str]] = {}
        for row in rows:
            qty, name, sset, cn, rarity, cmc, type_line = row
            cur = collapsed.get(name)
            if cur is None:
                collapsed[name] = row
                continue
            best_qty = max(cur[0], qty)
            if _RARITY_ORDER.get(rarity, 0) > _RARITY_ORDER.get(cur[4], 0):
                collapsed[name] = (best_qty, name, sset, cn, rarity, cmc, type_line)
            else:
                collapsed[name] = (best_qty,) + cur[1:]
        rows = list(collapsed.values())

    rows.sort(key=lambda r: (r[1].lower(), r[2], r[3]))

    json_rows = [
        {
            "name": name,
            "set": sset,
            "collector_number": cn,
            "rarity": rarity,
            "cmc": cmc,
            "type_line": type_line,
            "owned": qty,
        }
        for qty, name, sset, cn, rarity, cmc, type_line in rows
    ]
    total_copies = sum(r["owned"] for r in json_rows)

    if getattr(args, "json", False):
        _emit_json({
            "query": args.query,
            "min": args.min,
            "unique": bool(args.unique),
            "scryfall_total": len(results),
            "arena_eligible": arena_eligible,
            "rows": json_rows,
            "unique_owned": len(json_rows),
            "total_copies_owned": total_copies,
        })
        return 0

    if not rows:
        if arena_eligible == 0:
            print(
                f"matched {len(results)} on Scryfall but no Arena-legal matches",
                file=sys.stderr,
            )
        else:
            print(
                f"matched {arena_eligible} Arena-legal cards "
                f"(of {len(results)} on Scryfall) but you own none of them",
                file=sys.stderr,
            )
        return 0

    for qty, name, sset, cn, rarity, cmc, type_line in rows:
        cmc_str = f"{cmc:g}" if cmc is not None else "-"
        print(
            f"  {qty}x {name} ({sset} {cn}) [{rarity}] mv={cmc_str} {type_line}"
        )

    print(
        f"{len(rows)} unique / {total_copies} total owned / "
        f"{arena_eligible} arena-legal matches (of {len(results)} on Scryfall)"
    )
    return 0


def _resolve_deck_card(idx: dict, e: DeckEntry) -> dict | None:
    card = idx["by_printing"].get((e.set_code.lower(), e.collector))
    if card:
        return card
    candidates = idx["by_name"].get(_normalize_name(e.name)) or []
    if candidates:
        return candidates[0]
    return None


def _deck_demand(
    idx: dict, deck_path: Path
) -> tuple[dict[str, dict], list[DeckEntry]]:
    """Aggregate per-name demand from a deck file (mainboard + commander)."""
    entries = parse_deck(deck_path)
    demand: dict[str, dict] = {}
    unresolved: list[DeckEntry] = []
    for e in entries:
        if e.section in ("commander", "deck", "sideboard"):
            card = _resolve_deck_card(idx, e)
            if card is None:
                unresolved.append(e)
                continue
            key = (card.get("name") or "").lower()
            slot = demand.setdefault(
                key,
                {
                    "name": card.get("name"),
                    "needed": 0,
                    "rarity": card.get("rarity", "common"),
                    "card": card,
                },
            )
            slot["needed"] += e.count
            if _RARITY_ORDER.get(card.get("rarity"), 0) > _RARITY_ORDER.get(
                slot["rarity"], 0
            ):
                slot["rarity"] = card.get("rarity")
    return demand, unresolved


def _deck_gap_rows(
    demand: dict[str, dict], owned: dict[str, dict]
) -> list[tuple[str, int, int, int, str]]:
    """Per-card shortfall rows for one deck.

    Returns sorted list of (name, needed, have, short, rarity) for every
    non-basic card the deck wants more of than the collection holds.
    """
    rows: list[tuple[str, int, int, int, str]] = []
    for key, d in demand.items():
        if _is_basic(d["card"]):
            continue
        have = (owned.get(key) or {}).get("owned", 0)
        short = max(0, d["needed"] - have)
        if short > 0:
            rows.append((d["name"], d["needed"], have, short, d["rarity"]))
    rows.sort(key=lambda r: (-_RARITY_ORDER.get(r[4], 0), -r[3], r[0]))
    return rows


def _compute_missing(
    idx: dict, deck_path: Path, owned_by_name: dict[str, dict]
) -> list[dict]:
    """Per-slot shortfall list for the suggest-subs engine.

    Returns one dict per non-basic card the deck demands more copies of
    than the collection holds. Reuses `_deck_demand` for resolution, so
    unresolved entries are dropped silently here (the caller surfaces
    them separately when it needs to).
    """
    demand, _unresolved = _deck_demand(idx, deck_path)
    out: list[dict] = []
    for key, d in demand.items():
        card = d["card"]
        if _is_basic(card):
            continue
        owned = (owned_by_name.get(key) or {}).get("owned", 0)
        deficit = d["needed"] - owned
        if deficit <= 0:
            continue
        out.append({
            "name": d["name"],
            "card": card,
            "needed": d["needed"],
            "owned": owned,
            "deficit": deficit,
            "roles": classify_card(card),
            "cmc": float(card.get("cmc") or 0),
            "type_line": card.get("type_line") or "",
            "rarity": card.get("rarity") or "common",
        })
    return out


def cmd_gaps(args: argparse.Namespace) -> int:
    _warn_if_collection_stale()
    snap = _load_collection()
    if snap is None:
        sys.exit(_empty_state_message().rstrip())
    idx = _load_index()
    demand, unresolved = _deck_demand(idx, Path(args.deck))
    owned = _aggregate_by_name(idx, _cards_owned(snap))

    rows = _deck_gap_rows(demand, owned)

    wc_cost: dict[str, int] = {}
    for _name, _need, _have, short, rarity in rows:
        wc_cost[rarity] = wc_cost.get(rarity, 0) + short

    if getattr(args, "json", False):
        _emit_json({
            "deck": args.deck,
            "completeness": snap.get("completeness"),
            "gating": [
                {
                    "name": name,
                    "needed": need,
                    "have": have,
                    "short": short,
                    "rarity": rarity,
                }
                for name, need, have, short, rarity in rows
            ],
            "wildcards": wc_cost,
            "unresolved": [
                {
                    "name": e.name,
                    "set": e.set_code,
                    "collector_number": e.collector,
                    "count": e.count,
                }
                for e in unresolved
            ],
        })
        return 0

    print(f"deck: {args.deck}  (snapshot: {snap.get('completeness')})")
    if not rows:
        print("you own every card in this deck.")
    else:
        print()
        print(f"  {'rarity':<9} {'name':<46} {'need':>4} {'have':>4} {'craft':>5}")
        for name, need, have, short, rarity in rows:
            print(f"  {rarity:<9} {name[:46]:<46} {need:>4} {have:>4} {short:>5}")
        print()
        print("wildcards needed:")
        for r in ("mythic", "rare", "uncommon", "common"):
            if r in wc_cost:
                print(f"  {r:<10} {wc_cost[r]}")
    if unresolved:
        print()
        print(f"[warn] {len(unresolved)} deck line(s) did not resolve:")
        for e in unresolved[:5]:
            print(f"  {e.count} {e.name} ({e.set_code}) {e.collector}")
    if snap.get("completeness") == "lower-bound":
        print()
        print(
            "[lower-bound] some 'short' counts may be inflated — your\n"
            "from-decks snapshot only sees cards you've decked. If a card\n"
            "in this deck has appeared in a previous deck, the count is\n"
            "accurate; otherwise we may be undercounting your ownership."
        )
    return 0


# Primary card types — used by the suggest-subs scorer to decide whether
# a candidate's role overlap includes a "real" card-type tag.
_PRIMARY_CARD_TYPES = frozenset({
    "creature", "instant", "sorcery", "enchantment",
    "planeswalker", "artifact", "land", "battle",
})

# Supertypes — replacing a Legendary slot with another Legendary etc.
# rewards type continuity.
_SUPERTYPES = ("legendary", "basic", "snow", "world")


# ---------- strictlybetter.eu — functional reprints + obsoletes ----------
#
# Single source of truth for the rules-text-equivalent / direct-upgrade
# mapping is the strictlybetter.eu API. The repo cache at
# `data/strictlybetter-cache.json` is a transcription, not a parallel
# dataset — TTL'd at 7d, refetched on miss. Two shapes:
#
#   "functional_reprints": {
#       "fetched_at": "2026-05-01T12:34:56+00:00",
#       "groups": [["Llanowar Elves", "Elvish Mystic", ...], ...],
#   }
#   "obsoletes": {
#       "fetched_at": "2026-05-01T12:34:56+00:00",
#       "rows": [
#           {"id": ..., "upvotes": ..., "downvotes": ...,
#            "inferiors": [{"name": "Shock", ...}, ...],
#            "superiors": [{"name": "Lightning Bolt", ...}, ...],
#            "labels": {"strictly_better": true, ...}}, ...
#       ],
#   }
#
# Both come from BULK fetches:
#   * functional_reprints: single page (per_page=200, last_page=1).
#   * obsoletes: ~384 pages, server-capped at per_page=50, total ~19,200
#     rows. The per-card variant `/api/obsoletes/<name>` only matches on
#     the inferiors side, so it can't surface rows where the queried
#     card is the superior — we have to fetch the full corpus once and
#     build a reverse index `superior_lc -> {inferior_names}` locally.
#
# A "good" obsoletes row is filtered to `labels.strictly_better=True`
# AND `upvotes > downvotes` so we don't promote disputed community
# claims. The bulk fetch costs ~4 minutes wall-clock at the 0.65s
# throttle but happens at most once per 7d.
#
# `_strictlybetter_subs(card_name)` returns the union of functional
# reprints and validated downgrades for a card. Empty list on miss /
# network failure — never raises. The candidate-loop in
# `_run_suggest_subs` then intersects this with the user's collection
# and applies a fixed +1000 score boost so any owned reprint outranks
# every heuristic match.


def _strictlybetter_load_cache() -> dict:
    """Return the on-disk cache dict, or a fresh skeleton if missing/corrupt.

    Single source of truth for both subkeys; callers mutate and call
    `_strictlybetter_save_cache`. Corrupt JSON is logged and replaced —
    we never crash the CLI over a malformed sub-cache.
    """
    skeleton = {
        "schema": STRICTLYBETTER_CACHE_SCHEMA,
        "functional_reprints": None,
        "obsoletes": None,
    }
    if not STRICTLYBETTER_CACHE.exists():
        return dict(skeleton)
    try:
        data = json.loads(STRICTLYBETTER_CACHE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"[warn] strictlybetter cache unreadable ({e}); "
            "starting fresh in-memory",
            file=sys.stderr,
        )
        return dict(skeleton)
    if not isinstance(data, dict):
        return dict(skeleton)
    if data.get("schema") != STRICTLYBETTER_CACHE_SCHEMA:
        # Stale schema — drop everything and refetch on demand. The
        # functional_reprints groups are direction-agnostic and could in
        # principle be preserved, but the cache is small and refetching
        # once is cheaper than version-aware partial migrations.
        return dict(skeleton)
    data.setdefault("functional_reprints", None)
    data.setdefault("obsoletes", None)
    return data


def _strictlybetter_save_cache(cache: dict) -> None:
    """Persist `cache` to disk. Best-effort: log and continue on OSError."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STRICTLYBETTER_CACHE.write_text(json.dumps(cache, indent=2))
    except OSError as e:
        print(
            f"[warn] could not write strictlybetter cache: {e}",
            file=sys.stderr,
        )


def _strictlybetter_is_fresh(fetched_at: str | None) -> bool:
    """7-day TTL check on an ISO-8601 `fetched_at` string."""
    if not fetched_at:
        return False
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(fetched_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return False
    return age < STRICTLYBETTER_TTL_S


def _strictlybetter_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _strictlybetter_fetch_functional_reprints() -> list[list[str]] | None:
    """Bulk-fetch every functional-reprint group. Returns None on failure.

    The endpoint (`/api/functional_reprints`) returns the whole DB in
    one page (per_page=200, last_page=1) — no name search supported per
    the api-guide. Called at most once per 7d.
    """
    url = f"{STRICTLYBETTER_API}/functional_reprints"
    try:
        payload = _get_json(url)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as e:
        print(
            f"[warn] strictlybetter functional_reprints fetch failed "
            f"({e}); functional-reprint preference disabled this run",
            file=sys.stderr,
        )
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data") or []
    groups: list[list[str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cards = row.get("cards") or []
        names = [
            (c.get("name") or "").strip()
            for c in cards
            if isinstance(c, dict) and (c.get("name") or "").strip()
        ]
        if len(names) >= 2:
            groups.append(names)
    return groups


def _strictlybetter_fetch_obsoletes_bulk() -> list[dict] | None:
    """Paginate the entire `/api/obsoletes` corpus.

    Returns the flat list of raw rows (preserving `inferiors`,
    `superiors`, `labels`, `upvotes`, `downvotes`) so the caller can
    build whatever index it wants. The per-card variant
    (`/api/obsoletes/<name>`) only partial-matches on the inferiors
    side, so it cannot surface rows where the queried card is itself
    the superior — bulk is the only correct path for sub-suggest.

    Returns None on transport failure of any page; caller MUST NOT
    cache a partial corpus (next CLI invocation can retry cleanly).
    `per_page` is server-capped at 50 regardless of request value, so
    we walk every `last_page` reported by page 1.

    Honors `STRICTLYBETTER_THROTTLE_S` between requests to stay under
    the 100 req/min limit. Prints a progress line every 50 pages to
    stderr — this is a multi-minute fetch and silent would be wrong.
    """
    base = (
        f"{STRICTLYBETTER_API}/obsoletes"
        f"?per_page={STRICTLYBETTER_OBSOLETES_PER_PAGE}"
    )
    rows: list[dict] = []
    last_fetch = 0.0
    page = 1
    last_page: int | None = None
    while True:
        gap = time.monotonic() - last_fetch
        if last_fetch and gap < STRICTLYBETTER_THROTTLE_S:
            time.sleep(STRICTLYBETTER_THROTTLE_S - gap)
        url = f"{base}&page={page}"
        try:
            payload = _get_json(url)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
        ) as e:
            print(
                f"[warn] strictlybetter obsoletes bulk fetch failed at "
                f"page {page}: {e}; downgrade preference disabled this run",
                file=sys.stderr,
            )
            return None
        last_fetch = time.monotonic()
        if not isinstance(payload, dict):
            print(
                f"[warn] strictlybetter obsoletes bulk: page {page} "
                "returned non-dict payload; aborting",
                file=sys.stderr,
            )
            return None
        page_rows = payload.get("data") or []
        if not isinstance(page_rows, list):
            print(
                f"[warn] strictlybetter obsoletes bulk: page {page} "
                "missing `data` list; aborting",
                file=sys.stderr,
            )
            return None
        for row in page_rows:
            if isinstance(row, dict):
                rows.append(row)
        if last_page is None:
            try:
                last_page = int(payload.get("last_page") or 0) or None
            except (TypeError, ValueError):
                last_page = None
            if last_page:
                print(
                    f"[strictlybetter] obsoletes bulk fetch: "
                    f"{last_page} pages total (~{last_page * 50} rows)",
                    file=sys.stderr,
                )
        if last_page and (page % 50 == 0 or page == last_page):
            print(
                f"[strictlybetter] obsoletes bulk fetch: "
                f"page {page}/{last_page}",
                file=sys.stderr,
            )
        if last_page is not None and page >= last_page:
            break
        if last_page is None and not page_rows:
            # Defensive: no last_page reported and an empty page —
            # treat as end-of-stream rather than looping forever.
            break
        page += 1
    return rows


# Per-process memo: cache the inverted functional-reprints index after
# the first call so re-entrant `_run_suggest_subs` invocations (e.g.
# coverage --batch --with-subs walking N decks) don't re-read the JSON
# file N times. Keyed by None (single global) — the underlying cache
# file is the SoT, this is just an O(1) accessor.
_STRICTLYBETTER_REPRINT_INDEX: dict[str, set[str]] | None = None
_STRICTLYBETTER_OBSOLETE_INDEX: dict[str, set[str]] | None = None


def _strictlybetter_reprint_index(
    cache: dict, *, refresh: bool = False
) -> dict[str, set[str]]:
    """name_lc -> set of other names in the same functional-reprint group.

    Built from the `functional_reprints.groups` cache subkey. Triggers a
    bulk fetch when missing or stale; on fetch failure, returns an
    empty index (no preference contribution this run, no crash).
    """
    global _STRICTLYBETTER_REPRINT_INDEX
    if _STRICTLYBETTER_REPRINT_INDEX is not None and not refresh:
        return _STRICTLYBETTER_REPRINT_INDEX
    sub = cache.get("functional_reprints") or {}
    fetched_at = sub.get("fetched_at") if isinstance(sub, dict) else None
    groups = sub.get("groups") if isinstance(sub, dict) else None
    if not groups or not _strictlybetter_is_fresh(fetched_at) or refresh:
        fresh_groups = _strictlybetter_fetch_functional_reprints()
        if fresh_groups is not None:
            cache["functional_reprints"] = {
                "fetched_at": _strictlybetter_now(),
                "groups": fresh_groups,
            }
            _strictlybetter_save_cache(cache)
            groups = fresh_groups
        elif not groups:
            # First-ever call failed — return empty so caller degrades
            # gracefully. Don't memoize (next run might succeed).
            return {}
    inverted: dict[str, set[str]] = {}
    for grp in groups:
        names = [n for n in grp if isinstance(n, str) and n]
        for nm in names:
            others = {o for o in names if o.lower() != nm.lower()}
            if others:
                inverted.setdefault(nm.lower(), set()).update(others)
    _STRICTLYBETTER_REPRINT_INDEX = inverted
    return inverted


def _strictlybetter_obsoletes_index(
    cache: dict, *, refresh: bool = False
) -> dict[str, set[str]]:
    """superior_name_lc -> set of inferior names (community-validated).

    Built from the bulk row-list under `cache["obsoletes"]["rows"]`.
    Triggers a bulk fetch when the cached rows are missing or stale
    (7d TTL on the bulk timestamp); on fetch failure, returns an empty
    index (no preference contribution this run, no crash).

    Filter rules — only "good" downgrades survive:
      * `labels.strictly_better == True`
      * `upvotes > downvotes` (community-validated)
      * Skip self-references (defensive: an `id`-paired self-row would
        promote a card as a sub for itself)
      * Inferiors keep their original casing for output; index key is
        lowercase so callers don't have to canonicalize.
    """
    global _STRICTLYBETTER_OBSOLETE_INDEX
    if _STRICTLYBETTER_OBSOLETE_INDEX is not None and not refresh:
        return _STRICTLYBETTER_OBSOLETE_INDEX
    sub = cache.get("obsoletes") or {}
    fetched_at = sub.get("fetched_at") if isinstance(sub, dict) else None
    rows = sub.get("rows") if isinstance(sub, dict) else None
    if (
        not isinstance(rows, list)
        or not _strictlybetter_is_fresh(fetched_at)
        or refresh
    ):
        fresh_rows = _strictlybetter_fetch_obsoletes_bulk()
        if fresh_rows is not None:
            cache["obsoletes"] = {
                "fetched_at": _strictlybetter_now(),
                "rows": fresh_rows,
            }
            _strictlybetter_save_cache(cache)
            rows = fresh_rows
        elif not isinstance(rows, list):
            # First-ever call failed — return empty so caller degrades
            # gracefully. Don't memoize (next run might succeed).
            return {}
    inverted: dict[str, set[str]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        labels = row.get("labels") or {}
        if not labels.get("strictly_better"):
            continue
        try:
            up = int(row.get("upvotes") or 0)
            down = int(row.get("downvotes") or 0)
        except (TypeError, ValueError):
            continue
        if up <= down:
            continue
        superiors = [
            (c.get("name") or "").strip()
            for c in (row.get("superiors") or [])
            if isinstance(c, dict)
        ]
        inferiors = [
            (c.get("name") or "").strip()
            for c in (row.get("inferiors") or [])
            if isinstance(c, dict)
        ]
        for sup in superiors:
            if not sup:
                continue
            sup_lc = sup.lower()
            for inf in inferiors:
                if not inf or inf.lower() == sup_lc:
                    continue
                inverted.setdefault(sup_lc, set()).add(inf)
    _STRICTLYBETTER_OBSOLETE_INDEX = inverted
    return inverted


def _strictlybetter_subs(card_name: str) -> list[str]:
    """Names that are functional reprints OR community-validated downgrades.

    Single-call entry point used by `_run_suggest_subs`. Reads/refreshes
    the on-disk cache as needed (7d TTL). Empty list on miss, malformed
    response, or network failure — never raises.
    """
    name = (card_name or "").strip()
    if not name:
        return []
    cache = _strictlybetter_load_cache()
    out: list[str] = []
    seen: set[str] = set()
    key = name.lower()

    reprint_idx = _strictlybetter_reprint_index(cache)
    for n in reprint_idx.get(key, ()):
        if n.lower() not in seen and n.lower() != key:
            seen.add(n.lower())
            out.append(n)

    obsolete_idx = _strictlybetter_obsoletes_index(cache)
    for n in obsolete_idx.get(key, ()):
        if n.lower() not in seen and n.lower() != key:
            seen.add(n.lower())
            out.append(n)
    return out


def _freq_score_adjustment(deck_pct: float | None) -> float:
    """Frequency-aware score adjustment for sub candidates.

    Bias substitutions toward cards that actually appear in real decks of
    the format, not toward random Scryfall hits:
      * deck_pct >= 0.10  -> +1.0 (corpus-popular staple)
      * deck_pct <  0.05  -> -2.0 (rare/random card masquerading as a sub)
      * 0.05 <= deck_pct < 0.10 -> 0.0 (no adjustment)
      * None (card absent from freq index) -> 0.0 (don't penalize cards
        from sets newer than the corpus)
    """
    if deck_pct is None:
        return 0.0
    if deck_pct >= 0.10:
        return 1.0
    if deck_pct < 0.05:
        return -2.0
    return 0.0


def _suggest_subs_score(
    c: dict,
    cand_roles: set[str],
    missing_roles: set[str],
    missing_cmc: float,
    is_rare_role,
    *,
    cand_deck_pct: float | None = None,
) -> float:
    """Score one candidate card against one missing slot.

    Components are summed, then multiplied by 1.5 if the missing slot
    fills a role that's rare in the deck's pool (T=3, see caller). The
    frequency adjustment (see `_freq_score_adjustment`) is added AFTER
    the rare-role multiplier so that a "popular" candidate is rewarded
    by the same +1.0 regardless of role-rarity, and a "random Scryfall
    hit" is penalized by the same -2.0 — the freq term is about whether
    the card belongs in real decks, not about how it fits this slot.
    """
    inter = len(cand_roles & missing_roles)
    role_term = 3.0 * inter / max(1, len(cand_roles))
    cmc_term = 2.0 * max(0, 2 - abs((c.get("cmc") or 0) - missing_cmc))
    type_term = 1.0 if (cand_roles & _PRIMARY_CARD_TYPES) & missing_roles else 0.0
    tl = (c.get("type_line") or "").lower()
    super_term = 1.0 if any(s in tl for s in _SUPERTYPES) else 0.0
    score = role_term + cmc_term + type_term + super_term
    # T=3: a role is "rare for this deck" iff fewer than 3 cards in the
    # deck's pool tag it. Replacing a rare-role slot with a non-rare-role
    # candidate hurts disproportionately, so we boost rare-role matches.
    if any(is_rare_role(r) for r in (cand_roles & missing_roles)):
        score *= 1.5
    score += _freq_score_adjustment(cand_deck_pct)
    return score


def _anchor_names(
    entries: list[DeckEntry],
    fmt: str,
    freq_index: dict | None,
    *,
    deck_pct_threshold: float = 0.50,
) -> set[str]:
    """Canonical names of "anchor" cards in the deck.

    A card is an anchor (must NEVER be substituted) iff EITHER:
      * It is the commander (Brawl identity envelope — replacing it
        invalidates every other card; cmd_suggest_subs already preserves
        the commander row, this just propagates the same status into
        coverage / JSON / text).
      * Its `deck_pct` in the freq index for this format is >=
        `deck_pct_threshold` (default 0.50, i.e. shows up in 50%+ of
        corpus decks for the format) — these are the format's real
        staples; missing them means "you can't build this format right
        now," not "swap them out."

    Anchors are sourced from main + commander + companion only;
    sideboard cards never count as anchors (they're already optional in
    a way main/commander cards aren't).
    """
    anchors: set[str] = set()
    is_brawl = fmt in BRAWL_FORMATS
    if is_brawl:
        for e in entries:
            if e.section != "commander":
                continue
            c = _resolve_card(e.name)
            if c is not None:
                anchors.add((c.get("name") or e.name))
    if freq_index is None:
        return anchors
    cards = freq_index.get("cards") or {}
    for e in entries:
        if e.section not in {"deck", "commander", "companion"}:
            continue
        c = _resolve_card(e.name)
        if c is None:
            continue
        # Basic lands are never anchors regardless of deck_pct (every
        # deck plays Forests).
        if _is_basic(c):
            continue
        name = c.get("name") or e.name
        row = cards.get(name)
        if row is None:
            continue
        if float(row.get("deck_pct") or 0.0) >= deck_pct_threshold:
            anchors.add(name)
    return anchors


def _run_suggest_subs(
    deck_path: Path,
    fmt: str,
    idx: dict,
    snap: dict,
    max_per_card: int = 5,
    *,
    quiet: bool = False,
    anchor_check: bool = True,
    max_sub_pct: float = 0.30,
    freq_index: dict | None = None,
    strictlybetter: bool = True,
) -> dict:
    """Pure-compute core of `suggest-subs`.

    Returns the JSON-shaped dict the `--json` path emits. cmd_suggest_subs
    derives text output and `--apply` rewrites from this same dict, so the
    CLI surface and any internal caller (e.g. `coverage --with-subs`) see
    identical scoring.

    `quiet=True` suppresses the per-candidate `[warn] dropping
    game-changer ...` stderr line. JSON-emitting callers
    (`coverage --batch --with-subs --json`, `suggest-subs --json`) pass
    `quiet=True` so machine-readable output isn't polluted by per-deck
    candidate noise; the human text path leaves the warning visible.

    Sub-fidelity floor (F-batch):
      * `anchor_check=True` (default) protects format staples and the
        commander from substitution — anchor slots emit a
        `replacement: None`, `reason: "anchor"` row instead of a
        candidate list.
      * Candidate scoring is biased toward corpus-popular cards via
        `_freq_score_adjustment` (uses `freq_index` if supplied; loaded
        on demand otherwise).
      * `subs_pct = cards_substituted / non_basic_main_size`. When that
        exceeds `max_sub_pct`, the rewrite is "unacceptable" — the
        result dict carries `subs_acceptable: False`, the `--apply`
        path refuses the write, and `_coverage_with_subs_pct` clamps
        with-subs coverage to native ownership.

    Strictlybetter preference (O3):
      * When `strictlybetter=True` (default), `_strictlybetter_subs` is
        consulted per missing card. Owned candidates whose name is a
        functional reprint or community-validated direct upgrade get a
        fixed +1000 score boost so they outrank every heuristic match.
        Each boosted candidate carries `strictlybetter: true` in the
        JSON output. Falls back to pure heuristic scoring when the
        cache is empty / API unreachable / no owned reprint exists.
      * `strictlybetter=False` skips the API lookup entirely (offline
        mode; surfaced as `--no-strictlybetter` on `cmd_suggest_subs`).

    Caller is responsible for:
      - calling `_warn_if_stale()` / `_warn_if_collection_stale()`
      - validating `fmt` against ARENA_FORMATS
      - checking deck_path existence
      - loading idx and snap
    """
    from collections import Counter

    entries = parse_deck(deck_path)
    owned_by_name = _aggregate_by_name(idx, _cards_owned(snap))
    missing = _compute_missing(idx, deck_path, owned_by_name)

    # Freq index for the deck's format — used both for anchor detection
    # and for the per-candidate frequency-aware score adjustment. Cached
    # once per run so we don't re-read the JSON per missing slot.
    if freq_index is None:
        try:
            freq_index = _load_freq_index(fmt, rebuild_if_stale=False)
        except Exception:
            freq_index = None
    freq_cards = (freq_index or {}).get("cards") or {}

    def _deck_pct_for(name: str) -> float | None:
        row = freq_cards.get(name)
        if row is None:
            return None
        return float(row.get("deck_pct") or 0.0)

    anchors = (
        _anchor_names(entries, fmt, freq_index) if anchor_check else set()
    )

    # Build the deck's role pool for the rare-role frequency check —
    # every resolved commander/deck/sideboard card contributes its tag
    # set, weighted by copy count. T=3: tags carried by fewer than 3
    # copies are rare for this deck.
    role_freq: Counter[str] = Counter()
    for e in entries:
        if e.section not in {"commander", "deck", "sideboard"}:
            continue
        c = _resolve_deck_card(idx, e)
        if c is None:
            continue
        for r in classify_card(c):
            role_freq[r] += e.count

    def _is_rare_role(r: str) -> bool:
        return role_freq.get(r, 0) < 3  # T=3

    is_brawl = fmt in BRAWL_FORMATS
    cmdr_identity: set[str] | None = None
    if is_brawl:
        cmdr_entry = next(
            (e for e in entries if e.section == "commander"), None
        )
        if cmdr_entry is not None:
            cmdr = _resolve_deck_card(idx, cmdr_entry)
            if cmdr is not None:
                cmdr_identity = set(cmdr.get("color_identity") or [])

    # Companion guard: only honored outside Brawl (Brawl decks don't run
    # companions in the standard sideboard slot — and `_COMPANION_PREDICATES`
    # is per-card, so aggregate companions like Yorion legitimately have
    # no entry here.)
    declared_companion_pred = None
    if not is_brawl:
        for e in entries:
            if e.section != "sideboard":
                continue
            c = _resolve_deck_card(idx, e)
            if c is None:
                continue
            if "Companion —" in (c.get("oracle_text") or ""):
                declared_companion_pred = _COMPANION_PREDICATES.get(
                    c.get("name") or ""
                )
                break

    # Pre-compute per-name copy counts in the deck so the per-candidate
    # copy-cap check is O(1). Resolve each entry through `_resolve_card`
    # so multi-face cards written by their short face name (e.g.
    # `"Brazen Borrower"`) collapse to the canonical full name
    # (`"Brazen Borrower // Petty Theft"`) the candidate display side
    # uses — otherwise an already-included multi-face card sneaks past
    # the copy cap and is offered as a substitute for some other slot.
    deck_copies: Counter[str] = Counter()
    for e in entries:
        if e.section not in {"commander", "deck", "sideboard"}:
            continue
        rc = _resolve_card(e.name)
        canonical = (rc.get("name") if rc else None) or e.name
        deck_copies[canonical] += e.count
    max_copies = 1 if is_brawl else 4

    fillable = 0
    unfilled = 0
    anchor_unfilled = 0
    json_missing: list[dict] = []

    # Strictlybetter boost magnitude. The heuristic score peaks around
    # ~15 (3 + 4 + 1 + 1 = 9, * 1.5 rare-role = 13.5, + 1.0 freq); +1000
    # guarantees any rules-text-equivalent owned candidate sorts ahead
    # of every heuristic match even when stacked with rare-role/freq.
    SB_BOOST = 1000.0

    for slot in missing:
        miss_name = slot["name"]
        miss_card = slot["card"]
        miss_roles = slot["roles"]
        miss_cmc = slot["cmc"]
        miss_game_changer = bool(miss_card.get("game_changer"))

        # strictlybetter.eu functional-reprint + good-obsolete set for
        # this missing card. Lookup short-circuits when disabled or when
        # there's no canonical name to query.
        sb_names_lc: set[str] = set()
        if strictlybetter and miss_name:
            for n in _strictlybetter_subs(miss_name):
                sb_names_lc.add(n.lower())

        # Anchor preservation: a card is an anchor if it's the commander
        # OR appears in >= 50% of corpus decks for this format. Anchors
        # are NEVER substituted — when missing, the deck is fundamentally
        # not buildable and the slot is emitted with `replacement: None`,
        # `reason: "anchor"`. cmd_suggest_subs renders this as
        # `ANCHOR (missing)` and refuses to fill it during --apply.
        if miss_name in anchors:
            unfilled += 1
            anchor_unfilled += 1
            json_missing.append({
                "card": miss_name,
                "needed": slot["needed"],
                "owned": slot["owned"],
                "deficit": slot["deficit"],
                "roles": sorted(miss_roles),
                "cmc": miss_cmc,
                "type_line": slot["type_line"],
                "anchor": True,
                "replacement": None,
                "reason": "anchor",
                "candidates": [],
            })
            continue

        candidates: list[
            tuple[float, dict, dict, bool, bool, float | None, bool]
        ] = []
        for cand_name_lc, info in owned_by_name.items():
            cand_display = info.get("name") or ""
            if cand_display == miss_name:
                continue
            c = _resolve_card(cand_display)
            if c is None:
                continue
            if not _card_legal_in(c, fmt):
                continue
            # A- rebalanced cards are only legal in Alchemy / Brawl pools.
            if (c.get("name") or "").startswith("A-") and not (
                fmt == "alchemy" or fmt in BRAWL_FORMATS
            ):
                continue
            if cmdr_identity is not None:
                # spec deviation: ⊆ matches validator
                ci = set(c.get("color_identity") or [])
                if not ci.issubset(cmdr_identity):
                    continue
            cand_roles = classify_card(c)
            if not (cand_roles & miss_roles):
                continue
            if abs((c.get("cmc") or 0) - miss_cmc) > 2:
                continue
            in_deck = deck_copies.get(cand_display, 0)
            if not _is_basic(c) and in_deck >= max_copies:
                continue
            if info.get("owned", 0) < slot["deficit"]:
                continue
            if (
                declared_companion_pred is not None
                and not declared_companion_pred(c)
            ):
                continue
            cand_game_changer = bool(c.get("game_changer"))
            if is_brawl and cand_game_changer and not miss_game_changer:
                if not quiet:
                    print(
                        f"[warn] dropping game-changer candidate "
                        f"{cand_display!r} for non-game-changer slot "
                        f"{miss_name!r}",
                        file=sys.stderr,
                    )
                continue
            cand_deck_pct = _deck_pct_for(cand_display)
            score = _suggest_subs_score(
                c, cand_roles, miss_roles, miss_cmc, _is_rare_role,
                cand_deck_pct=cand_deck_pct,
            )
            rare_boost = any(
                _is_rare_role(r) for r in (cand_roles & miss_roles)
            )
            is_strictlybetter = cand_display.lower() in sb_names_lc
            if is_strictlybetter:
                # Rules-text-equivalent / community-validated upgrade —
                # outranks every heuristic match by construction.
                score += SB_BOOST
            candidates.append((
                score, c, info, rare_boost, cand_game_changer, cand_deck_pct,
                is_strictlybetter,
            ))

        candidates.sort(key=lambda t: (-t[0], (t[1].get("name") or "")))
        candidates = candidates[:max_per_card]

        if candidates:
            fillable += 1
        else:
            unfilled += 1

        json_missing.append({
            "card": miss_name,
            "needed": slot["needed"],
            "owned": slot["owned"],
            "deficit": slot["deficit"],
            "roles": sorted(miss_roles),
            "cmc": miss_cmc,
            "type_line": slot["type_line"],
            "anchor": False,
            "candidates": [
                {
                    "name": c.get("name"),
                    "score": round(score, 3),
                    "owned": info.get("owned", 0),
                    "roles": sorted(classify_card(c)),
                    "cmc": float(c.get("cmc") or 0),
                    "type_line": c.get("type_line") or "",
                    "rare_role_boost": rare_boost,
                    "game_changer": gc,
                    "deck_pct": (
                        round(cand_deck_pct, 4)
                        if cand_deck_pct is not None else None
                    ),
                    "strictlybetter": sb,
                }
                for score, c, info, rare_boost, gc, cand_deck_pct, sb
                in candidates
            ],
        })

    # Sub-pct cap denominator: every non-basic copy in main + commander
    # + companion. Sideboard is excluded per spec; basic lands too
    # (every deck plays Forests, swapping them isn't a "fidelity"
    # event). Numerator: every copy we'd actually substitute (anchors
    # NEVER count — if we're refusing to swap them, they're not part
    # of the rewrite churn). A slot with zero candidates also doesn't
    # count toward subs_pct (we couldn't substitute it even if we
    # wanted to); the result is still surfaced via `unfilled`.
    non_basic_main = 0
    for e in entries:
        if e.section not in {"deck", "commander", "companion"}:
            continue
        c = _resolve_card(e.name)
        if c is not None and _is_basic(c):
            continue
        non_basic_main += e.count

    cards_substituted = 0
    for slot in json_missing:
        if slot.get("anchor"):
            continue
        if not slot.get("candidates"):
            continue
        cards_substituted += slot["deficit"]

    if non_basic_main > 0:
        subs_pct = cards_substituted / non_basic_main
    else:
        subs_pct = 0.0
    subs_acceptable = subs_pct <= max_sub_pct

    return {
        "deck": str(deck_path),
        "format": fmt,
        "anchors": sorted(anchors),
        "missing": json_missing,
        "subs_pct": round(subs_pct, 4),
        "subs_acceptable": subs_acceptable,
        "max_sub_pct": max_sub_pct,
        "non_basic_main": non_basic_main,
        "cards_substituted": cards_substituted,
        "summary": {
            "missing_cards": len(missing),
            "fillable": fillable,
            "unfilled": unfilled,
            "anchor_unfilled": anchor_unfilled,
        },
    }


def cmd_suggest_subs(args: argparse.Namespace) -> int:
    """Propose owned replacements for missing cards in a deck.

    Deterministic engine: enumerate–filter–score over the user's collection.
    No LLM, no API call. Computation lives in `_run_suggest_subs`; this
    function handles arg validation, --apply rewriting, and presentation.

    JSON schema (--json):
    {
      "deck": "decks/foo/v1.txt",
      "format": "brawl",
      "anchors": ["Card Name", ...],
      "subs_acceptable": true,         # false when subs_pct > --max-sub-pct
      "subs_pct": 0.18,                # cards_substituted / non_basic_main
      "max_sub_pct": 0.30,
      "non_basic_main": 60,
      "cards_substituted": 11,
      "missing": [
        {
          "card": "Sheoldred, the Apocalypse",
          "needed": 1, "owned": 0, "deficit": 1,
          "roles": ["threat"], "cmc": 4,
          "type_line": "Legendary Creature — Phyrexian Praetor",
          "anchor": false,
          # When anchor: candidates=[], replacement=None, reason="anchor"
          "candidates": [
            {"name": "...", "score": 7.5, "owned": 2, "roles": [...],
             "cmc": 4, "type_line": "...",
             "rare_role_boost": false, "game_changer": false,
             "deck_pct": 0.12}
          ]
        }
      ],
      "summary": {
        "missing_cards": 7, "fillable": 5, "unfilled": 2,
        "anchor_unfilled": 1,
      }
    }
    """
    _warn_if_stale()
    _warn_if_collection_stale()

    fmt = args.format.lower()
    if fmt not in ARENA_FORMATS:
        print(f"unknown format: {fmt}", file=sys.stderr)
        return 2
    if args.max_per_card < 1:
        print("--max-per-card must be >= 1", file=sys.stderr)
        return 2
    if not (0.0 <= args.max_sub_pct <= 1.0):
        print(
            "--max-sub-pct must be between 0.0 and 1.0", file=sys.stderr,
        )
        return 2
    deck_path = Path(args.deck)
    if not deck_path.exists():
        print(f"deck file not found: {deck_path}", file=sys.stderr)
        return 2
    if args.apply is not None:
        out_parent = Path(args.apply).parent
        if not out_parent.exists():
            print(
                f"--apply parent directory does not exist: {out_parent}",
                file=sys.stderr,
            )
            return 2

    snap = _load_collection()
    if snap is None:
        sys.exit(_empty_state_message().rstrip())
    idx = _load_index()

    result = _run_suggest_subs(
        deck_path, fmt, idx, snap, args.max_per_card,
        quiet=args.json,
        anchor_check=not args.no_anchor_check,
        max_sub_pct=args.max_sub_pct,
        strictlybetter=not args.no_strictlybetter,
    )
    json_missing = result["missing"]
    summary = result["summary"]
    missing_count = summary["missing_cards"]
    fillable = summary["fillable"]
    unfilled = summary["unfilled"]
    subs_acceptable = result["subs_acceptable"]
    subs_pct = result["subs_pct"]
    entries = parse_deck(deck_path)
    is_brawl = fmt in BRAWL_FORMATS
    max_copies = 1 if is_brawl else 4

    deck_copies: dict[str, int] = {}
    for e in entries:
        if e.section in {"commander", "deck", "sideboard"}:
            deck_copies[e.name] = deck_copies.get(e.name, 0) + e.count

    text_chunks: list[str] = []
    for slot in json_missing:
        miss_name = slot["card"]
        miss_cmc = slot["cmc"]
        miss_roles = slot["roles"]
        slot_candidates = slot["candidates"]
        is_anchor = bool(slot.get("anchor"))
        text_block: list[str] = []
        if is_anchor:
            text_block.append(
                f"ANCHOR (missing): {slot['deficit']}x {miss_name} "
                f"(cmc={int(miss_cmc) if float(miss_cmc).is_integer() else miss_cmc}, "
                f"roles=[{','.join(miss_roles)}])"
            )
            text_block.append(
                "  this card is a format staple or the commander; "
                "no substitution offered — craft it or pick another deck."
            )
            text_chunks.append("\n".join(text_block))
            continue
        text_block.append(
            f"MISSING: {slot['deficit']}x {miss_name} "
            f"(cmc={int(miss_cmc) if float(miss_cmc).is_integer() else miss_cmc}, "
            f"roles=[{','.join(miss_roles)}])"
        )
        if not slot_candidates:
            text_block.append("  candidates (top 0): (none)")
        else:
            text_block.append(f"  candidates (top {len(slot_candidates)}):")
            for cand in slot_candidates:
                cand_cmc = cand["cmc"]
                if isinstance(cand_cmc, float) and cand_cmc.is_integer():
                    cand_cmc = int(cand_cmc)
                tag = " [strictlybetter]" if cand.get("strictlybetter") else ""
                text_block.append(
                    f"    {cand['score']:6.3f}  {cand['owned']}x "
                    f"{cand['name']:<32} cmc={cand_cmc}  "
                    f"roles=[{','.join(cand['roles'])}]{tag}"
                )
        text_chunks.append("\n".join(text_block))

    # --apply: rewrite the deck with the top-scored candidate per slot.
    # Sub fidelity floor: if the proposed rewrite swaps more than
    # --max-sub-pct of the deck (default 30%), refuse the write — the
    # output would be a "different deck wearing the same name," not a
    # substitution. Anchor slots are unfilled by construction (they're
    # the format staples / commander we promised never to swap).
    if args.apply is not None and not subs_acceptable:
        print(
            f"[error] sub fraction {subs_pct * 100:.1f}% exceeds "
            f"{args.max_sub_pct * 100:.0f}% cap; rewrite refused",
            file=sys.stderr,
        )
        return 2
    if args.apply is not None:
        # Each candidate name can appear at most max_copies times across
        # the substituted deck (1 in Brawl, 4 elsewhere). The same
        # candidate could rank top in several slots, so we walk the
        # candidate list per slot and pick the first one whose remaining
        # capacity (after pre-existing copies in the deck and earlier
        # picks in this loop) is ≥ this slot's deficit.
        used: dict[str, int] = {}
        for n, qty in deck_copies.items():
            used[n] = qty
        # name_lc -> (chosen_candidate_name, deficit)
        top_by_name: dict[str, tuple[str, int]] = {}
        for json_slot in json_missing:
            slot_deficit = json_slot["deficit"]
            chosen_name: str | None = None
            for entry in json_slot["candidates"]:
                cname = entry["name"]
                resolved = _resolve_card(cname)
                # Basic lands ignore the copy cap entirely.
                if resolved is not None and _is_basic(resolved):
                    chosen_name = cname
                    break
                if max_copies - used.get(cname, 0) >= slot_deficit:
                    chosen_name = cname
                    break
            if chosen_name is None:
                continue
            resolved = _resolve_card(chosen_name)
            if resolved is None or not _is_basic(resolved):
                used[chosen_name] = used.get(chosen_name, 0) + slot_deficit
            top_by_name[json_slot["card"].lower()] = (
                chosen_name, slot_deficit,
            )

        new_entries: list[DeckEntry] = []
        for e in entries:
            key = e.name.lower()
            # Skip commander substitution: replacing the commander
            # changes the deck's identity envelope and invalidates every
            # other card. The commander stays as-is; the user crafts it
            # or picks a different deck.
            if e.section == "commander":
                new_entries.append(e)
                continue
            if key in top_by_name and e.section in {"deck", "sideboard"}:
                cand_name, deficit = top_by_name[key]
                # Split the original line into (count - take) original
                # copies + take new candidate copies. `take` never
                # exceeds e.count because deficit <= needed and a single
                # entry's count <= needed.
                take = min(deficit, e.count)
                remaining = e.count - take
                if remaining > 0:
                    new_entries.append(DeckEntry(
                        remaining, e.name, e.set_code, e.collector, e.section,
                    ))
                # Find any printing for the candidate to source (SET) NUM.
                printings = idx["by_name"].get(cand_name.lower()) or []
                if not printings:
                    # Resolution above already accepted the candidate, so
                    # this branch is unreachable; fall back to passing
                    # the original line through to keep the deck valid.
                    new_entries.append(e)
                    continue
                p = printings[0]
                new_entries.append(DeckEntry(
                    take,
                    cand_name,
                    (p.get("set") or "").upper(),
                    str(p.get("collector_number") or ""),
                    e.section,
                ))
                # Decrement the remaining deficit so multi-line splits
                # don't double-substitute the same slot.
                top_by_name[key] = (cand_name, deficit - take)
                if top_by_name[key][1] <= 0:
                    del top_by_name[key]
            else:
                new_entries.append(e)
        _write_mtga_export(Path(args.apply), new_entries)
        print(f"wrote substituted deck → {args.apply}", file=sys.stderr)

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"deck: {deck_path}  (snapshot: {snap.get('completeness')})")
        print(f"format: {fmt}")
        if not missing_count:
            print("you own every non-basic card in this deck.")
        else:
            print()
            for chunk in text_chunks:
                print(chunk)
                print()
            anchor_unfilled = summary.get("anchor_unfilled", 0)
            extras = []
            if anchor_unfilled:
                extras.append(f"anchor_unfilled={anchor_unfilled}")
            extras.append(f"subs_pct={subs_pct * 100:.1f}%")
            extras.append(
                "acceptable" if subs_acceptable else "UNACCEPTABLE"
            )
            print(
                f"summary: missing={missing_count}, "
                f"fillable={fillable}, unfilled={unfilled}, "
                + ", ".join(extras)
            )
            if not subs_acceptable:
                print(
                    f"[warn] sub fraction {subs_pct * 100:.1f}% exceeds "
                    f"{args.max_sub_pct * 100:.0f}% cap — this rewrite "
                    f"would be a different deck. --apply will refuse.",
                    file=sys.stderr,
                )
    return 0


def _coverage_single(
    deck_path: Path, idx: dict, owned: dict[str, dict]
) -> tuple[int, int, list[tuple[str, int, str]], int]:
    """Per-deck coverage compute. Returns
    (total_have, total_need, gating, unresolved_count)."""
    demand, unresolved = _deck_demand(idx, deck_path)
    total_need = 0
    total_have = 0
    gating: list[tuple[str, int, str]] = []
    for key, d in demand.items():
        if _is_basic(d["card"]):
            continue
        need = d["needed"]
        have = min(need, (owned.get(key) or {}).get("owned", 0))
        total_need += need
        total_have += have
        short = need - have
        if short > 0:
            gating.append((d["name"], short, d["rarity"]))
    return total_have, total_need, gating, len(unresolved)


def _coverage_with_subs_pct(
    deck_path: Path,
    fmt: str,
    idx: dict,
    snap: dict,
    total_have: int,
    total_need: int,
    *,
    quiet: bool = False,
    max_sub_pct: float = 0.30,
    out: dict | None = None,
) -> float:
    """Re-run suggest-subs and fold filled deficits into coverage.

    `with_subs_pct = (owned_count + filled_deficit) / total_count`, where
    `filled_deficit` only counts slots that have at least one candidate
    AND aren't anchors (anchors are the deck's commander or format
    staples; we promised never to substitute them, so a missing anchor
    is unfilled by construction).

    Sub-fidelity floor: when the rewrite is unacceptable
    (`subs_pct > max_sub_pct`, i.e. > 30% of the deck would be swapped)
    we clamp `with_subs_pct` to the native `owned_pct`. Otherwise
    coverage --with-subs would still report 100% for decks that are
    only "buildable" by gutting them — which is exactly the bug the
    fidelity floor exists to fix.

    `quiet` is forwarded to `_run_suggest_subs` so the per-candidate
    `[warn] dropping game-changer ...` stderr noise stays out of
    `coverage --batch --with-subs --json` output.

    `out` (when provided) is populated with the F2 sidecar fields the
    `recommend` command needs: `subs_acceptable`, `subs_pct`,
    `cards_substituted`, `anchor_unfilled`, `anchor_total`,
    `non_basic_main`. Lets `_coverage_row` and `_recommend_compute`
    share one `_run_suggest_subs` call instead of paying for two.

    Math short-circuit: when `(1 - owned_pct) > max_sub_pct`, the F2
    fidelity floor is guaranteed to trip (more than `max_sub_pct` of
    the deck must be substituted just to fill the deficit), so the
    clamp returns `owned_pct` regardless of which candidates exist.
    Skip the heavy `_run_suggest_subs` scoring pass entirely. When this
    fires the sidecar adds `"short_circuited": True` so consumers can
    distinguish "we didn't compute the rewrite detail" from "we
    computed it and the counts are zero" — `cards_substituted`,
    `anchor_unfilled`, `anchor_total`, and `non_basic_main` are all
    set to 0 in that case but those zeros are placeholders, not facts.
    """
    if total_need == 0:
        if out is not None:
            out.update({
                "subs_acceptable": True,
                "subs_pct": 0.0,
                "cards_substituted": 0,
                "anchor_unfilled": 0,
                "anchor_total": 0,
                "non_basic_main": 0,
            })
        return 1.0
    owned_pct = total_have / total_need
    needed_sub_pct = 1.0 - owned_pct
    if needed_sub_pct > max_sub_pct:
        # Mathematically guaranteed unacceptable — F2's clamp would
        # return owned_pct anyway. Skip the suggest-subs call entirely.
        if out is not None:
            out.update({
                "subs_acceptable": False,
                "subs_pct": needed_sub_pct,  # lower bound; actual could be higher if anchors are missing
                "cards_substituted": 0,  # we didn't compute the rewrite, can't count slots
                "anchor_unfilled": 0,    # ditto
                "anchor_total": 0,       # ditto
                "non_basic_main": 0,     # ditto
                "short_circuited": True,  # signal to consumers that detail is not available
            })
        return owned_pct
    result = _run_suggest_subs(
        deck_path, fmt, idx, snap, quiet=quiet, max_sub_pct=max_sub_pct,
    )
    if out is not None:
        summary = result.get("summary") or {}
        out.update({
            "subs_acceptable": bool(result.get("subs_acceptable", True)),
            "subs_pct": float(result.get("subs_pct") or 0.0),
            "cards_substituted": int(result.get("cards_substituted") or 0),
            "anchor_unfilled": int(summary.get("anchor_unfilled") or 0),
            "anchor_total": len(result.get("anchors") or []),
            "non_basic_main": int(result.get("non_basic_main") or 0),
        })
    if not result.get("subs_acceptable", True):
        return owned_pct
    filled = 0
    for slot in result["missing"]:
        if slot.get("anchor"):
            continue
        if slot["candidates"]:
            filled += slot["deficit"]
    return (total_have + filled) / total_need


def _format_for_deck_path(p: Path) -> str:
    """Infer Arena format from the deck file's parent directory name.

    Falls back to `brawl` if the parent is not an Arena format. Caller is
    responsible for emitting the once-per-run fallback warning, since this
    helper has no run context.
    """
    parent = p.parent.name
    if parent in ARENA_FORMATS:
        return parent
    return "brawl"


def _load_deck_meta(deck_path: Path) -> dict:
    """Return the meta.json entry for `deck_path` (or {} if absent)."""
    meta_path = deck_path.parent / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(meta, dict):
        return {}
    entry = meta.get(deck_path.name)
    if isinstance(entry, dict):
        return entry
    return {}


def _coverage_row(
    deck_path: Path,
    idx: dict,
    snap: dict,
    owned: dict[str, dict],
    with_subs: bool,
    fallback_warn: list[bool],
    *,
    quiet: bool = False,
    max_sub_pct: float = 0.30,
    include_subs_meta: bool = False,
) -> dict:
    """Compute one batch-mode row. Mutates fallback_warn[0] -> True
    when this deck's parent dir is not in ARENA_FORMATS.

    `quiet` is forwarded to the suggest-subs sub-call so JSON-output
    callers don't emit the per-candidate game-changer stderr line for
    each deck in the batch.

    `max_sub_pct` overrides the F2 sub-fidelity floor (default 0.30 =
    30% of the deck). `include_subs_meta=True` adds the F2 sidecar
    fields (`subs_acceptable`, `anchor_unfilled`, `anchor_total`,
    `cards_substituted`, `subs_pct`, plus `short_circuited` when the
    math short-circuit fired) to the returned row — needed by
    `recommend` for build-status classification, opt-in elsewhere so
    the existing `coverage --batch` JSON schema stays unchanged.
    """
    parent = deck_path.parent.name
    fmt = parent if parent in ARENA_FORMATS else "brawl"
    if parent and parent not in ARENA_FORMATS:
        fallback_warn[0] = True

    total_have, total_need, gating, _unres = _coverage_single(
        deck_path, idx, owned,
    )
    owned_pct = (total_have / total_need) if total_need else 1.0

    wc: dict[str, int] = {"mythic": 0, "rare": 0, "uncommon": 0, "common": 0}
    for _name, short, rarity in gating:
        if rarity in wc:
            wc[rarity] += short

    gating_sorted = sorted(
        gating, key=lambda r: (-_RARITY_ORDER.get(r[2], 0), -r[1], r[0]),
    )
    top3 = [name for name, _short, _rarity in gating_sorted[:3]]

    with_subs_pct: float | None = None
    subs_meta: dict = {}
    if with_subs:
        with_subs_pct = _coverage_with_subs_pct(
            deck_path, fmt, idx, snap, total_have, total_need,
            quiet=quiet, max_sub_pct=max_sub_pct,
            out=subs_meta if include_subs_meta else None,
        )

    meta = _load_deck_meta(deck_path)
    tier_raw = meta.get("tier")
    tier = tier_raw if isinstance(tier_raw, str) and tier_raw else None

    tier_w = _tier_weight(tier)
    # Composite score: tier_weight * (with_subs_pct if --with-subs and
    # available else owned_pct). F2 clamps with_subs_pct to owned_pct
    # when subs are unacceptable, so this picks up that clamp for free
    # without any special-case here.
    base_pct = (
        with_subs_pct if (with_subs and with_subs_pct is not None)
        else owned_pct
    )
    composite = round(tier_w * base_pct, 4)

    row: dict = {
        "deck": str(deck_path),
        "archetype": deck_path.stem,
        "tier": tier,
        "tier_weight": tier_w,
        "owned_pct": round(owned_pct, 4),
        "missing_wc": wc,
        "with_subs_pct": (
            round(with_subs_pct, 4) if with_subs_pct is not None else None
        ),
        "composite": composite,
        "top3_missing": top3,
    }
    if include_subs_meta and subs_meta:
        row["subs_acceptable"] = subs_meta["subs_acceptable"]
        row["subs_pct"] = round(subs_meta["subs_pct"], 4)
        row["cards_substituted"] = subs_meta["cards_substituted"]
        row["anchor_unfilled"] = subs_meta["anchor_unfilled"]
        row["anchor_total"] = subs_meta["anchor_total"]
        row["non_basic_main"] = subs_meta["non_basic_main"]
        if subs_meta.get("short_circuited"):
            row["short_circuited"] = True
    return row


def _print_coverage_batch_text(rows: list[dict], with_subs: bool) -> None:
    """Render the batch-mode text table. Columns:
    archetype(30) tier(4) score(7) owned%(6) missing-WC(12) [with-subs(7)] top3.

    `score` (composite = tier_weight * base_pct) is always shown so the
    default --rank composite ordering is legible without re-running.
    """
    if with_subs:
        header = (
            f"{'archetype':<30} {'tier':<4} {'score':<7} {'owned':<6} "
            f"{'missing-WC':<12} {'subs':<6} top-3 missing"
        )
    else:
        header = (
            f"{'archetype':<30} {'tier':<4} {'score':<7} {'owned':<6} "
            f"{'missing-WC':<12} top-3 missing"
        )
    print(header)
    print("-" * len(header))
    for r in rows:
        wc = r["missing_wc"]
        wc_str = (
            f"{wc['mythic']}/{wc['rare']}/{wc['uncommon']}/{wc['common']}"
        )
        owned_str = f"{r['owned_pct']:.2f}"
        tier_str = r["tier"] or "-"
        score_str = f"{r['composite']:.4f}"
        top3_str = ", ".join(r["top3_missing"]) if r["top3_missing"] else "-"
        if with_subs:
            sub_pct = r["with_subs_pct"]
            sub_str = f"{sub_pct:.2f}" if sub_pct is not None else "-"
            print(
                f"{r['archetype'][:30]:<30} {tier_str:<4} {score_str:<7} "
                f"{owned_str:<6} {wc_str:<12} {sub_str:<6} {top3_str}"
            )
        else:
            print(
                f"{r['archetype'][:30]:<30} {tier_str:<4} {score_str:<7} "
                f"{owned_str:<6} {wc_str:<12} {top3_str}"
            )


def cmd_coverage(args: argparse.Namespace) -> int:
    _warn_if_collection_stale()
    snap = _load_collection()
    if snap is None:
        sys.exit(_empty_state_message().rstrip())
    idx = _load_index()
    owned = _aggregate_by_name(idx, _cards_owned(snap))

    if not args.batch:
        if args.deck is None:
            print(
                "coverage: provide a deck file or use --batch --glob '<pat>'",
                file=sys.stderr,
            )
            return 2
        deck_path = Path(args.deck)
        total_have, total_need, gating, unresolved_count = _coverage_single(
            deck_path, idx, owned,
        )
        pct = (100.0 * total_have / total_need) if total_need else 100.0
        gating.sort(
            key=lambda r: (-_RARITY_ORDER.get(r[2], 0), -r[1], r[0]),
        )
        if getattr(args, "json", False):
            _emit_json({
                "deck": args.deck,
                "completeness": snap.get("completeness"),
                "have": total_have,
                "need": total_need,
                "owned_pct": (total_have / total_need) if total_need else 1.0,
                "gating": [
                    {"name": name, "short": short, "rarity": rarity}
                    for name, short, rarity in gating
                ],
                "unresolved": unresolved_count,
            })
            return 0
        print(f"deck: {args.deck}  (snapshot: {snap.get('completeness')})")
        print(
            f"coverage: {total_have}/{total_need} non-basic copies  "
            f"({pct:.1f}%)"
        )
        if gating:
            print()
            print("gating cards (need wildcards):")
            for name, short, rarity in gating:
                print(f"  -{short} {rarity:<8} {name}")
        if unresolved_count:
            print()
            print(f"[warn] {unresolved_count} unresolved deck line(s)")
        return 0

    # Batch mode.
    if not args.glob:
        print(
            "coverage --batch requires --glob '<pattern>'", file=sys.stderr,
        )
        return 2
    if args.min is not None and not (0.0 <= args.min <= 1.0):
        print("--min must be in [0, 1]", file=sys.stderr)
        return 2

    deck_paths = sorted(
        Path(p) for p in glob_mod.glob(args.glob, recursive=True)
        if Path(p).is_file()
    )
    if not deck_paths:
        if args.json:
            json.dump([], sys.stdout)
            sys.stdout.write("\n")
        else:
            print("no deck files matched")
        return 0

    fallback_warn = [False]
    rows: list[dict] = []
    for path in deck_paths:
        rows.append(
            _coverage_row(
                path, idx, snap, owned, args.with_subs, fallback_warn,
                quiet=args.json,
            )
        )

    if fallback_warn[0]:
        print(
            "[warn] some deck paths have a parent dir not in ARENA_FORMATS; "
            "fell back to format=brawl",
            file=sys.stderr,
        )

    # Rank dispatch. `composite` (default) favours tier × ownership;
    # `quality` is tier-first with ownership as a tiebreaker; `ownership`
    # preserves the legacy sort. The --min filter still applies to the
    # ownership-style metric (with_subs_pct when --with-subs, else
    # owned_pct) regardless of rank — "show me decks I can actually
    # build" is independent of how we order them.
    rank = getattr(args, "rank", "composite")
    if rank == "ownership":
        if args.with_subs:
            rows.sort(
                key=lambda r: (-(r["with_subs_pct"] or 0.0), r["archetype"]),
            )
        else:
            rows.sort(key=lambda r: (-r["owned_pct"], r["archetype"]))
    elif rank == "quality":
        rows.sort(
            key=lambda r: (-r["tier_weight"], -r["owned_pct"], r["archetype"]),
        )
    else:  # composite
        rows.sort(key=lambda r: (-r["composite"], r["archetype"]))

    if args.min is not None:
        key = "with_subs_pct" if args.with_subs else "owned_pct"
        rows = [r for r in rows if (r[key] or 0.0) >= args.min]

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_coverage_batch_text(rows, args.with_subs)
    return 0


# ---------- shells (cluster owned cards for novel-deck discovery) --------

# Role tags eligible as shell themes. `_ROLE_TYPE` entries (creature /
# instant / land / …) are intentionally excluded — "all my creatures" is
# not a shell, it's a card pool. Only function tags survive: removal,
# sweeper, counter, hand_attack, peek, card_advantage, loot, tutor, ramp,
# recursion, threat, wincon. Mirrors the `_ROLE_FUNC` keys exactly so
# adding a new function role surfaces here automatically.
_SHELL_THEME_KEYS = frozenset(k for k, _ in _ROLE_FUNC)


def _shell_cluster_rows(
    idx: dict,
    cards_owned: dict[int, int],
    fmt: str,
    by: str,
    min_cards: int,
    top_anchors: int,
) -> list[dict]:
    """Bucket the user's owned, format-legal cards by `by` and return the
    structured cluster list both text and JSON output consume.

    Same return shape across modes — text and JSON render identical rows.
    """
    # Dedup by canonical name (the index already canonicalises multi-face
    # cards to `Front // Back`, so the cluster output spells them that way
    # without any reconstruction here).
    seen_names: set[str] = set()
    owned_cards: list[dict] = []
    for aid in cards_owned:
        c = idx["by_arena_id"].get(aid)
        if c is None:
            continue
        if not _card_legal_in(c, fmt):
            continue
        if _is_basic(c):
            continue
        name = c["name"]
        if name in seen_names:
            continue
        seen_names.add(name)
        owned_cards.append(c)

    clusters: dict[str, list[dict]] = {}
    for c in owned_cards:
        type_line = (c.get("type_line") or "").lower()
        if by == "keyword":
            keys = {
                kw for kw in (c.get("keywords") or [])
                if kw not in _EVERGREEN_KEYWORDS
            }
        elif by == "type":
            if "creature" not in type_line:
                continue
            full_type = c.get("type_line") or ""
            if " — " not in full_type:
                continue
            subtype_str = full_type.split(" — ", 1)[1]
            keys = set(subtype_str.split())
        elif by == "theme":
            keys = classify_card(c) & _SHELL_THEME_KEYS
        else:
            keys = set()
        for k in keys:
            clusters.setdefault(k, []).append(c)

    rows: list[dict] = []
    for key, cards in clusters.items():
        if len(cards) < min_cards:
            continue
        # Color-identity union, in WUBRG order; "C" if every card is
        # colorless. `color_pairs` is the sorted distinct identity each
        # card contributes (a deck-builder skim signal — does this cluster
        # actually overlap on colors, or is it a wishful union of mono
        # cards in different colors?).
        union: set[str] = set()
        pair_set: set[str] = set()
        for c in cards:
            ci = c.get("color_identity") or []
            union.update(ci)
            pair_set.add("".join(sorted(ci, key=_COLORS.index)))
        colors_str = "".join(col for col in _COLORS if col in union) or "C"
        color_pairs = sorted(pair_set)

        # Anchors: highest-rarity first, tie-break by CMC desc then name
        # asc. Mythics + rares are the "build around" cards a shell hangs
        # off; commons are usually glue.
        sorted_cards = sorted(
            cards,
            key=lambda c: (
                -_RARITY_ORDER.get(c.get("rarity") or "common", 0),
                -(c.get("cmc") or 0),
                c["name"],
            ),
        )
        anchors: list[dict] = []
        for c in sorted_cards[:top_anchors]:
            ci_list = c.get("color_identity") or []
            anchors.append({
                "name": c["name"],
                "set": (c.get("set") or "").upper(),
                "collector_number": str(c.get("collector_number") or ""),
                "rarity": c.get("rarity") or "",
                "cmc": float(c.get("cmc") or 0),
                "color_identity":
                    "".join(sorted(ci_list, key=_COLORS.index)) or "C",
            })

        rows.append({
            "key": key,
            "count": len(cards),
            "colors": colors_str,
            "color_pairs": color_pairs,
            "anchors": anchors,
            # Full owned-card name set for the cluster — `cmd_shells`
            # uses this for `--match-corpus` overlap and pops it before
            # emitting JSON so the public schema stays anchor-only.
            "_card_names": {c["name"] for c in cards},
        })

    rows.sort(key=lambda r: (-r["count"], r["key"]))
    return rows


# ---------- meta-corpus card frequency index ------------------------------


def _corpus_deck_files(fmt: str, *, include_derived: bool = False) -> list[Path]:
    """Sorted MTGA-export deck files in `data/corpus/<fmt>/`.

    Excludes JSON sidecars (`meta.json`, `_freq.json`, `_*.json`) so the
    corpus enumerator never tries to `parse_deck` a JSON blob. Returns []
    if the directory doesn't exist.

    `include_derived=True` additionally appends `data/corpus/<fmt>/derived/
    *.txt` (machine-rewritten ownership-maximized variants from
    `cmd_derive`). Off by default so freq index + archetype-anchor
    computation see only the source-of-truth corpus — derived decks
    sourced from those priors must not feed back into them. `recommend`
    and other consumer-side walks pass True so user-derived variants
    surface alongside the originals.
    """
    corpus_dir = CORPUS / fmt
    if not corpus_dir.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(corpus_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() != ".txt":
            continue
        if p.name.startswith("_"):
            continue
        out.append(p)
    if include_derived:
        derived_dir = corpus_dir / "derived"
        if derived_dir.is_dir():
            for p in sorted(derived_dir.iterdir()):
                if not p.is_file():
                    continue
                if p.suffix.lower() != ".txt":
                    continue
                if p.name.startswith("_"):
                    continue
                out.append(p)
    return out


def _archetype_for_deck(deck_path: Path) -> str:
    """Pretty archetype name from sidecar, falling back to filename stem."""
    meta = _load_deck_meta(deck_path)
    arch = meta.get("archetype")
    if isinstance(arch, str) and arch.strip():
        return arch
    return deck_path.stem


def _compute_freq_index(fmt: str) -> dict:
    """Walk `data/corpus/<fmt>/*.txt` and tally per-card stats.

    Schema is consumer-stable (F2/F3/F4 read this directly):
      - `deck_count` / `deck_pct` — main+sideboard combined membership
      - `total_main` / `total_sideboard` — copy split (commander &
        companion fold into main: 1 commander + 1 companion = 1 main copy
        each, matching how the deck legally plays)
      - `archetypes` — sorted unique archetype slugs (filename stems by
        default; sidecar `archetype` overrides for human-readable runs)
      - `basic` flag — every deck has Forests; consumers ranking by
        `deck_pct` should drop them rather than have us drop them here
        (single source of truth for "is X a basic" lives in `_is_basic`)
    """
    files = _corpus_deck_files(fmt)
    if not files:
        return {
            "format": fmt,
            "computed_at": _dt.date.today().isoformat(),
            "corpus_size": 0,
            "total_card_copies": 0,
            "unresolved_cards": 0,
            "cards": {},
        }

    # Per-card running stats keyed by canonical Scryfall name.
    stats: dict[str, dict[str, Any]] = {}
    archetypes_per_card: dict[str, set[str]] = {}
    total_copies = 0
    unresolved = 0

    for path in files:
        slug = path.stem
        archetype = _archetype_for_deck(path)
        # Per-deck membership: a card present in main + sideboard counts
        # as one deck for `deck_count` (spec: "main+sideboard combined").
        seen_in_deck: set[str] = set()
        for entry in parse_deck(path):
            if entry.section == "maybeboard":
                continue
            if entry.section not in {"deck", "commander", "companion", "sideboard"}:
                continue
            card = _resolve_card(entry.name)
            if card is None:
                unresolved += entry.count
                continue
            name = card.get("name") or entry.name
            row = stats.get(name)
            if row is None:
                row = {
                    "deck_count": 0,
                    "deck_pct": 0.0,
                    "total_copies": 0,
                    "total_main": 0,
                    "total_sideboard": 0,
                    "avg_copies_per_appearing_deck": 0.0,
                    "basic": _is_basic(card),
                    "archetypes": [],
                }
                stats[name] = row
                archetypes_per_card[name] = set()
            if entry.section == "sideboard":
                row["total_sideboard"] += entry.count
            else:
                # `deck`, `commander`, `companion` all play from main.
                row["total_main"] += entry.count
            row["total_copies"] += entry.count
            total_copies += entry.count
            if name not in seen_in_deck:
                seen_in_deck.add(name)
                row["deck_count"] += 1
                archetypes_per_card[name].add(archetype or slug)

    corpus_size = len(files)
    for name, row in stats.items():
        row["deck_pct"] = round(row["deck_count"] / corpus_size, 4)
        row["avg_copies_per_appearing_deck"] = round(
            row["total_copies"] / row["deck_count"], 2
        ) if row["deck_count"] else 0.0
        row["archetypes"] = sorted(archetypes_per_card[name])

    return {
        "format": fmt,
        "computed_at": _dt.date.today().isoformat(),
        "corpus_size": corpus_size,
        "total_card_copies": total_copies,
        "unresolved_cards": unresolved,
        "cards": stats,
    }


def _freq_index_path(fmt: str) -> Path:
    return CORPUS / fmt / "_freq.json"


def _freq_index_is_stale(fmt: str) -> bool:
    """True if `_freq.json` is missing or older than any deck file."""
    idx_path = _freq_index_path(fmt)
    if not idx_path.exists():
        return True
    idx_mtime = idx_path.stat().st_mtime
    for p in _corpus_deck_files(fmt):
        if p.stat().st_mtime > idx_mtime:
            return True
    # Sidecar bumps too: archetype names sourced from there.
    sidecar = CORPUS / fmt / "meta.json"
    if sidecar.exists() and sidecar.stat().st_mtime > idx_mtime:
        return True
    return False


def _write_freq_index(fmt: str, index: dict) -> Path:
    """Atomic-ish write of the freq index. Returns the path."""
    out_path = _freq_index_path(fmt)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, out_path)
    return out_path


def _load_freq_index(
    fmt: str, *, rebuild_if_stale: bool = True
) -> dict | None:
    """Read (and optionally rebuild) the freq index for `fmt`.

    Returns None when no corpus exists for the format (caller decides
    whether that's an error). Otherwise returns the schema dict; when
    `rebuild_if_stale` is True and the on-disk index is missing or older
    than the corpus, regenerates and writes it (with a stderr notice).
    """
    files = _corpus_deck_files(fmt)
    if not files:
        return None
    idx_path = _freq_index_path(fmt)
    if rebuild_if_stale and _freq_index_is_stale(fmt):
        print(f"[info] rebuilding freq index for {fmt}", file=sys.stderr)
        index = _compute_freq_index(fmt)
        _write_freq_index(fmt, index)
        return index
    if not idx_path.exists():
        return None
    try:
        return json.loads(idx_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"[warn] freq index unreadable ({e}); recomputing in-memory",
            file=sys.stderr,
        )
        return _compute_freq_index(fmt)


def _freq_rows_sorted(index: dict) -> list[tuple[str, dict]]:
    """Sort cards by deck_pct desc, deck_count desc, then name."""
    cards = index.get("cards") or {}
    return sorted(
        cards.items(),
        key=lambda kv: (
            -float(kv[1].get("deck_pct") or 0.0),
            -int(kv[1].get("deck_count") or 0),
            kv[0],
        ),
    )


def cmd_freq(args: argparse.Namespace) -> int:
    """Card-frequency index over `data/corpus/<fmt>/*.txt`.

    Three viewing modes:
      * default — top 30 cards by `deck_pct` as a text table
      * --card NAME — single-row lookup (resolved through Scryfall so
        casing / A-prefix / multi-face front-only inputs all hit)
      * --json [--all] — machine-readable; top 30 unless `--all`
      * --rebuild — force recompute, write `_freq.json`, print summary
      * --no-rebuild — read-only; never touch disk on stale index
    """
    fmt = args.format.lower()
    if fmt not in ARENA_FORMATS:
        print(
            f"format must be one of: {', '.join(sorted(ARENA_FORMATS))}",
            file=sys.stderr,
        )
        return 2

    files = _corpus_deck_files(fmt)
    if not files:
        print(f"no corpus for {fmt}", file=sys.stderr)
        return 1

    if args.rebuild:
        index = _compute_freq_index(fmt)
        out_path = _write_freq_index(fmt, index)
        size_b = out_path.stat().st_size
        print(
            f"wrote {out_path} "
            f"(corpus={index['corpus_size']}, "
            f"unique_cards={len(index['cards'])}, "
            f"copies={index['total_card_copies']}, "
            f"unresolved={index['unresolved_cards']}, "
            f"size={size_b}B)"
        )
        return 0

    index = _load_freq_index(fmt, rebuild_if_stale=not args.no_rebuild)
    if index is None:
        print(
            f"no _freq.json for {fmt} (run with --rebuild)",
            file=sys.stderr,
        )
        return 1

    cards = index.get("cards") or {}

    if args.card:
        # Resolve through Scryfall so user input casing / A-prefix /
        # front-face-only forms all map to the canonical key.
        card = _resolve_card(args.card)
        canonical = (card.get("name") if card else args.card) or args.card
        row = cards.get(canonical)
        if row is None:
            print(
                f"{canonical}: not in {fmt} freq index",
                file=sys.stderr,
            )
            return 1
        if args.json:
            _emit_json({"name": canonical, **row})
            return 0
        print(f"{canonical}")
        print(
            f"  deck_count : {row['deck_count']} / "
            f"{index['corpus_size']}  ({row['deck_pct'] * 100:.1f}%)"
        )
        print(
            f"  copies     : {row['total_copies']} "
            f"(main={row['total_main']}, sb={row['total_sideboard']}, "
            f"avg/deck={row['avg_copies_per_appearing_deck']})"
        )
        if row.get("basic"):
            print("  basic      : yes")
        if row.get("archetypes"):
            print("  archetypes :")
            for a in row["archetypes"]:
                print(f"    - {a}")
        return 0

    rows = _freq_rows_sorted(index)
    limit = None if args.all else 30
    shown = rows if limit is None else rows[:limit]

    if args.json:
        _emit_json({
            "format": index["format"],
            "computed_at": index["computed_at"],
            "corpus_size": index["corpus_size"],
            "total_card_copies": index["total_card_copies"],
            "unresolved_cards": index.get("unresolved_cards", 0),
            "shown": len(shown),
            "total_unique_cards": len(rows),
            "cards": [{"name": n, **r} for n, r in shown],
        })
        return 0

    print(
        f"freq (fmt={fmt}, corpus={index['corpus_size']}, "
        f"unique={len(rows)}, copies={index['total_card_copies']})"
    )
    print()
    print(
        f"  {'name':<40} {'pct':>6} {'decks':>5} "
        f"{'copies':>6} {'main':>5} {'sb':>4}  basic"
    )
    print(f"  {'-' * 40} {'-' * 6} {'-' * 5} {'-' * 6} {'-' * 5} {'-' * 4}  -----")
    for name, row in shown:
        basic_marker = "yes" if row.get("basic") else ""
        print(
            f"  {name[:40]:<40} "
            f"{row['deck_pct'] * 100:>5.1f}% "
            f"{row['deck_count']:>5} "
            f"{row['total_copies']:>6} "
            f"{row['total_main']:>5} "
            f"{row['total_sideboard']:>4}  {basic_marker}"
        )
    if limit is not None and len(rows) > limit:
        print(f"\n  ... {len(rows) - limit} more (use --all)")
    return 0


# ---------- shells <-> corpus archetype matching --------------------------


def _load_archetype_anchors(fmt: str) -> dict[str, dict]:
    """Per-archetype canonical card sets for `shells --match-corpus`.

    Walks `data/corpus/<fmt>/*.txt` (excluding sidecars + `_freq.json`) and
    returns ``{archetype_slug: {"cards": set[str], "size": int,
    "tier": str | None}}``. Anchor cards are non-basic-land main +
    commander + companion + sideboard entries resolved through
    `_resolve_card` (so casing / A-prefix / multi-face inputs all hit a
    canonical name). `size` is the count of distinct non-basic anchors
    — the denominator for `overlap_pct`. Returns ``{}`` when the corpus
    is empty so callers can short-circuit cleanly.
    """
    files = _corpus_deck_files(fmt)
    if not files:
        return {}
    out: dict[str, dict] = {}
    for path in files:
        slug = path.stem
        names: set[str] = set()
        for entry in parse_deck(path):
            if entry.section == "maybeboard":
                continue
            card = _resolve_card(entry.name)
            if card is None:
                continue
            if _is_basic(card):
                continue
            name = card.get("name") or entry.name
            names.add(name)
        meta = _load_deck_meta(path)
        tier_raw = meta.get("tier")
        tier = (
            tier_raw if isinstance(tier_raw, str) and tier_raw.strip()
            else None
        )
        out[slug] = {
            "cards": names,
            "size": len(names),
            "tier": tier,
        }
    return out


def _shell_corpus_matches(
    shell_cards: set[str],
    anchors: dict[str, dict],
    freq: dict | None,
    min_pct: float,
    min_count: int,
) -> list[dict]:
    """Rank archetypes by overlap with `shell_cards`, filter, cap at 3.

    `shell_cards` — set of canonical names in the cluster (already
    basics-excluded by `_shell_cluster_rows`). For each archetype we
    compute the raw overlap, the per-card weight from the freq index
    (`weight = 1 - min(deck_pct, 0.5)`, so corpus-wide staples count
    half and singletons count ~0.9), and the two ratios callers care
    about. Sort key: `weighted_overlap` desc when freq is available,
    else `anchor_overlap` desc; tie-break by archetype slug for stable
    output.
    """
    shell_size = len(shell_cards)
    if not anchors or shell_size == 0:
        return []
    freq_cards: dict[str, dict] = (freq or {}).get("cards") or {}

    def _weight(name: str) -> float:
        row = freq_cards.get(name)
        if not row:
            # Card unknown to the freq index = effectively unique.
            return 1.0
        pct_raw = row.get("deck_pct")
        try:
            pct = float(pct_raw) if pct_raw is not None else 0.0
        except (TypeError, ValueError):
            pct = 0.0
        return 1.0 - (pct if pct < 0.5 else 0.5)

    rows: list[dict] = []
    for slug, info in anchors.items():
        arch_cards: set[str] = info["cards"]
        arch_size = info["size"]
        if arch_size == 0:
            continue
        intersect = shell_cards & arch_cards
        anchor_overlap = len(intersect)
        if anchor_overlap < min_count:
            continue
        overlap_pct = anchor_overlap / arch_size
        if overlap_pct < min_pct:
            continue
        if freq is not None:
            weighted = sum(_weight(n) for n in intersect)
        else:
            weighted = float(anchor_overlap)
        rows.append({
            "archetype": slug,
            "tier": info.get("tier"),
            "anchor_overlap": anchor_overlap,
            "archetype_size_nonbasic": arch_size,
            "overlap_pct": round(overlap_pct, 4),
            "shell_size": shell_size,
            "shell_coverage_pct": round(anchor_overlap / shell_size, 4),
            "weighted_overlap": round(weighted, 2),
        })

    if freq is not None:
        rows.sort(
            key=lambda r: (-r["weighted_overlap"], r["archetype"]),
        )
    else:
        rows.sort(
            key=lambda r: (-r["anchor_overlap"], r["archetype"]),
        )
    return rows[:3]


def cmd_shells(args: argparse.Namespace) -> int:
    """Group owned, format-legal cards into synergy clusters.

    The CLI does enumeration; you do taste. Three bucketers — `keyword`
    (Blitz / Survival / Squad / …), `type` (creature subtypes for tribal
    decks), `theme` (function-role tags from `classify_card`) — surface
    the shells the meta corpus misses. `--min-cards` defaults to 24 for
    constructed and 15 for Brawl: enough themed slots to build around,
    not so many that the threshold filters out real shells.

    JSON schema (--json):
    {
      "format": "historic", "by": "keyword", "min_cards": 24,
      "clusters": [
        {"key": "Survival", "count": 31, "colors": "WUBRG",
         "color_pairs": ["", "G", "GW", "BG"],
         "anchors": [{"name": "Up the Beanstalk", "set": "WOE",
                      "collector_number": "246", "rarity": "rare",
                      "cmc": 2.0, "color_identity": "G"}]}
      ]
    }
    """
    _warn_if_stale()
    _warn_if_collection_stale()

    fmt = args.format.lower()
    if fmt not in ARENA_FORMATS:
        print(
            f"format must be one of: {', '.join(sorted(ARENA_FORMATS))}",
            file=sys.stderr,
        )
        return 2

    snap = _load_collection()
    if snap is None:
        print(
            "no collection snapshot — run 'tools/mtg collection dump' first",
            file=sys.stderr,
        )
        return 2

    by = args.by
    if args.min_cards is not None:
        min_cards = args.min_cards
    else:
        min_cards = 15 if fmt in BRAWL_FORMATS else 24

    idx = _load_index()
    cards_owned = _cards_owned(snap)

    clusters = _shell_cluster_rows(
        idx, cards_owned, fmt, by, min_cards, args.top_anchors,
    )
    if args.limit is not None:
        clusters = clusters[: args.limit]

    # `--match-corpus`: bridge each owned-card cluster to known
    # archetypes by overlapping shell cards against deck files in
    # `decks/<fmt>/`. Computed here (post-cluster) so the existing
    # bucketing logic stays untouched. Empty corpus -> warn once and
    # set every cluster's `matches` to []; missing freq index ->
    # fall back to unweighted ranking with a one-shot stderr [info].
    match_corpus = bool(getattr(args, "match_corpus", False))
    archetype_anchors: dict[str, dict] = {}
    freq_index: dict | None = None
    if match_corpus:
        archetype_anchors = _load_archetype_anchors(fmt)
        if not archetype_anchors:
            print(
                f"[warn] no corpus for {fmt} — --match-corpus reports "
                f"empty matches",
                file=sys.stderr,
            )
        else:
            freq_index = _load_freq_index(fmt, rebuild_if_stale=True)
            if freq_index is None or not (freq_index.get("cards") or {}):
                print(
                    f"[info] no freq index for {fmt}; "
                    f"--match-corpus falling back to unweighted overlap",
                    file=sys.stderr,
                )
                freq_index = None
        for cl in clusters:
            shell_names = cl.get("_card_names") or set()
            if archetype_anchors:
                cl["matches"] = _shell_corpus_matches(
                    shell_names,
                    archetype_anchors,
                    freq_index,
                    args.match_min,
                    args.match_anchors,
                )
            else:
                cl["matches"] = []

    # Pop the internal-only full card-name set before any output —
    # `_card_names` is a sentinel for `--match-corpus` and never
    # belongs in the JSON schema or text rendering.
    for cl in clusters:
        cl.pop("_card_names", None)

    if args.json:
        json.dump(
            {
                "format": fmt,
                "by": by,
                "min_cards": min_cards,
                "clusters": clusters,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if not clusters:
        print(
            f"no shells with ≥{min_cards} owned cards "
            f"(fmt={fmt}, by={by})"
        )
        return 0

    print(f"shells (fmt={fmt}, by={by}, min={min_cards}):")
    print()
    for cl in clusters:
        pairs_str = ",".join(p or "C" for p in cl["color_pairs"])
        print(
            f"  [{cl['key']}] — {cl['count']} owned cards · "
            f"colors: {cl['colors']} ({pairs_str})"
        )
        if cl["anchors"]:
            print("    anchors:")
            for a in cl["anchors"]:
                cmc_val = a["cmc"]
                cmc_str = (
                    str(int(cmc_val)) if float(cmc_val).is_integer()
                    else str(cmc_val)
                )
                print(
                    f"      {a['name'][:38]:<38} "
                    f"{a['set']:<5} {a['collector_number']:<5} "
                    f"{a['rarity']:<7} cmc {cmc_str:<3} "
                    f"{a['color_identity']:<4}"
                )
        if match_corpus:
            matches = cl.get("matches") or []
            if matches:
                print("    matches:")
                for m in matches:
                    pct = m["overlap_pct"] * 100
                    cov = m["shell_coverage_pct"] * 100
                    weighted = m["weighted_overlap"]
                    print(
                        f"      {m['archetype'][:24]:<24} "
                        f"overlap={pct:>4.0f}% "
                        f"({m['anchor_overlap']}/"
                        f"{m['archetype_size_nonbasic']} anchors), "
                        f"shell-cov={cov:>4.0f}%, "
                        f"weighted={weighted}"
                    )
            else:
                print("    matches: (none above threshold)")
        print()
    return 0


_DECK_VERSION_RE = re.compile(r"v(\d+)\.txt$", re.IGNORECASE)


def _resolve_wantlist_decks(
    pattern: str | None, latest_only: bool
) -> list[Path]:
    if pattern:
        pat_path = Path(pattern)
        if (
            pat_path.is_absolute()
            or pattern.startswith("..")
            or ".." in pat_path.parts
        ):
            print(
                f"--decks pattern must be relative to repo root: {pattern}",
                file=sys.stderr,
            )
            return []
        paths = sorted(ROOT.glob(pattern))
    else:
        paths = sorted(ROOT.glob("decks/*/v*.txt"))
    paths = [p for p in paths if p.is_file()]
    if latest_only:
        latest: dict[Path, tuple[int, Path]] = {}
        for p in paths:
            m = _DECK_VERSION_RE.search(p.name)
            if not m:
                continue
            v = int(m.group(1))
            cur = latest.get(p.parent)
            if cur is None or v > cur[0]:
                latest[p.parent] = (v, p)
        paths = sorted(slot[1] for slot in latest.values())
    return paths


def cmd_wantlist(args: argparse.Namespace) -> int:
    _warn_if_collection_stale()
    snap = _load_collection()
    if snap is None:
        print(
            "no collection snapshot — run 'tools/mtg collection dump' first",
            file=sys.stderr,
        )
        return 2
    idx = _load_index()

    deck_paths = _resolve_wantlist_decks(args.decks, args.latest_only)
    if not deck_paths:
        if getattr(args, "json", False):
            _emit_json({
                "decks_glob": args.decks,
                "latest_only": bool(args.latest_only),
                "deck_count": 0,
                "rows": [],
                "totals": {},
                "unresolved_total": 0,
            })
            return 0
        print("no deck files matched")
        return 0

    owned = _aggregate_by_name(idx, _cards_owned(snap))

    # name_lc -> {name, rarity, decks: [str], shortfalls: [int]}
    agg: dict[str, dict] = {}
    # (deck_label, entry_name) for each unresolved deck line, across all decks.
    unresolved_examples: list[tuple[str, str]] = []
    unresolved_total = 0
    decks_with_unresolved: set[str] = set()
    for path in deck_paths:
        try:
            demand, unresolved = _deck_demand(idx, path)
        except Exception as exc:  # pragma: no cover — surface parse errors
            print(f"error reading {path}: {exc}", file=sys.stderr)
            return 1
        rows = _deck_gap_rows(demand, owned)
        deck_label = (
            f"{path.parent.name}/{path.stem}"
            if path.parent.name and path.parent != ROOT
            else path.stem
        )
        if unresolved:
            unresolved_total += len(unresolved)
            decks_with_unresolved.add(deck_label)
            for e in unresolved:
                unresolved_examples.append((deck_label, e.name))
        for name, _need, _have, short, rarity in rows:
            key = name.lower()
            slot = agg.setdefault(
                key,
                {
                    "name": name,
                    "rarity": rarity,
                    "decks": [],
                    "shortfalls": [],
                },
            )
            slot["decks"].append(deck_label)
            slot["shortfalls"].append(short)
            if _RARITY_ORDER.get(rarity, 0) > _RARITY_ORDER.get(slot["rarity"], 0):
                slot["rarity"] = rarity

    rarity_buckets = ("mythic", "rare", "uncommon", "common")
    rarity_rank = {r: i for i, r in enumerate(rarity_buckets)}

    rows: list[tuple[str, str, int, int, list[str]]] = []
    totals: dict[str, int] = {}
    for slot in agg.values():
        wc = max(slot["shortfalls"])
        rarity = slot["rarity"]
        rows.append((rarity, slot["name"], len(slot["decks"]), wc, slot["decks"]))
        totals[rarity] = totals.get(rarity, 0) + wc

    rows.sort(
        key=lambda r: (
            rarity_rank.get(r[0], len(rarity_buckets)),
            -r[2],
            -r[3],
            r[1].lower(),
        )
    )

    if getattr(args, "json", False):
        _emit_json({
            "decks_glob": args.decks,
            "latest_only": bool(args.latest_only),
            "deck_count": len(deck_paths),
            "rows": [
                {
                    "rarity": rarity,
                    "name": name,
                    "deck_count": deck_count,
                    "wildcards_needed": wc,
                    "decks": decks,
                }
                for rarity, name, deck_count, wc, decks in rows
            ],
            "totals": {r: totals.get(r, 0) for r in rarity_buckets},
            "unresolved_total": unresolved_total,
            "unresolved_examples": [
                {"deck": label, "name": name}
                for label, name in unresolved_examples
            ],
        })
        return 0

    if rows:
        for rarity, name, deck_count, wc, decks in rows:
            shown = decks[:4]
            tail = ""
            if len(decks) > 4:
                tail = f", …+{len(decks) - 4} more"
            decks_str = ", ".join(shown) + tail
            print(
                f"{rarity:<9} {wc:>3}x  {name}  "
                f"({deck_count} deck{'s' if deck_count != 1 else ''}: {decks_str})"
            )
        print()

    summary = ", ".join(
        f"{totals.get(r, 0)} {r}" for r in rarity_buckets
    )
    print(
        f"total wildcards needed: {summary} across {len(deck_paths)} deck"
        f"{'s' if len(deck_paths) != 1 else ''}"
    )

    if unresolved_total:
        max_examples = 6
        shown = unresolved_examples[:max_examples]
        examples = ", ".join(f"{label} ({name})" for label, name in shown)
        extra = unresolved_total - len(shown)
        tail = f", …+{extra} more" if extra > 0 else ""
        deck_count = len(decks_with_unresolved)
        print(
            f"[warn] {unresolved_total} unresolved entries in {deck_count} "
            f"deck file{'s' if deck_count != 1 else ''}: {examples}{tail}",
            file=sys.stderr,
        )
    return 0


# ---------- diff ---------------------------------------------------------


def _aggregate_deck_for_diff(
    idx: dict, path: Path
) -> tuple[dict[str, int], str | None, list[DeckEntry]]:
    """Group deck entries by canonical (resolved) name.

    Returns (mainboard_counts, commander_name, unresolved). Unresolved
    entries fall back to the raw deck-line name so they still appear in
    the diff (cannot silently drop cards we can't find in the index).
    Sideboard / companion / maybeboard sections are ignored — the diff
    is for the playable deck.
    """
    counts: dict[str, int] = {}
    commander: str | None = None
    unresolved: list[DeckEntry] = []
    for e in parse_deck(path):
        if e.section not in ("commander", "deck"):
            continue
        card = _resolve_deck_card(idx, e)
        if card is None:
            unresolved.append(e)
            name = e.name
        else:
            name = card.get("name") or e.name
        if e.section == "commander":
            commander = name
            continue
        counts[name] = counts.get(name, 0) + e.count
    return counts, commander, unresolved


def cmd_diff(args: argparse.Namespace) -> int:
    _warn_if_stale()
    a_path = Path(args.a)
    b_path = Path(args.b)
    for p in (a_path, b_path):
        if not p.is_file():
            print(f"error: deck file not found: {p}", file=sys.stderr)
            return 2

    idx = _load_index()
    a_counts, a_cmd, a_unres = _aggregate_deck_for_diff(idx, a_path)
    b_counts, b_cmd, b_unres = _aggregate_deck_for_diff(idx, b_path)

    for label, path, unres in (
        ("a", a_path, a_unres),
        ("b", b_path, b_unres),
    ):
        if unres:
            sample = ", ".join(
                f"{e.name} ({e.set_code} {e.collector})" for e in unres[:5]
            )
            extra = len(unres) - 5
            tail = f", …+{extra} more" if extra > 0 else ""
            print(
                f"[warn] {len(unres)} unresolved entries in {label} "
                f"({path.name}): {sample}{tail}",
                file=sys.stderr,
            )

    a_total = sum(a_counts.values()) + (1 if a_cmd else 0)
    b_total = sum(b_counts.values()) + (1 if b_cmd else 0)

    commander_changed = a_cmd != b_cmd
    all_names = set(a_counts) | set(b_counts)
    removed: list[tuple[str, int]] = []
    added: list[tuple[str, int]] = []
    changed: list[tuple[str, int, int]] = []
    for name in all_names:
        ac = a_counts.get(name, 0)
        bc = b_counts.get(name, 0)
        if ac == bc:
            continue
        if ac == 0:
            added.append((name, bc))
        elif bc == 0:
            removed.append((name, ac))
        else:
            changed.append((name, ac, bc))

    if getattr(args, "json", False):
        plus = sum(n for _, n in added) + sum(
            max(0, bc - ac) for _, ac, bc in changed
        )
        minus = sum(n for _, n in removed) + sum(
            max(0, ac - bc) for _, ac, bc in changed
        )
        _emit_json({
            "a": str(a_path),
            "b": str(b_path),
            "a_total": a_total,
            "b_total": b_total,
            "commander_a": a_cmd,
            "commander_b": b_cmd,
            "commander_changed": commander_changed,
            "added": [
                {"name": n, "count": c}
                for n, c in sorted(added, key=lambda r: r[0].lower())
            ],
            "removed": [
                {"name": n, "count": c}
                for n, c in sorted(removed, key=lambda r: r[0].lower())
            ],
            "changed": [
                {"name": n, "from": ac, "to": bc, "delta": bc - ac}
                for n, ac, bc in sorted(changed, key=lambda r: r[0].lower())
            ],
            "net_added": plus,
            "net_removed": minus,
            "net_delta": b_total - a_total,
        })
        return 0

    print(f"diff: {a_path} -> {b_path}")

    if not commander_changed and not removed and not added and not changed:
        print(f"decks are identical ({a_total} cards each)")
        return 0

    if commander_changed:
        print(f"commander: {a_cmd or '(none)'} -> {b_cmd or '(none)'}")

    if removed:
        print()
        print(f"removed ({len(removed)}):")
        for name, n in sorted(removed, key=lambda r: r[0].lower()):
            print(f"  - {n} {name}")

    if added:
        print()
        print(f"added ({len(added)}):")
        for name, n in sorted(added, key=lambda r: r[0].lower()):
            print(f"  + {n} {name}")

    if changed:
        print()
        print(f"changed ({len(changed)}):")
        for name, ac, bc in sorted(changed, key=lambda r: r[0].lower()):
            delta = bc - ac
            sign = "+" if delta > 0 else "-"
            print(f"  ± {ac}->{bc} ({sign}{abs(delta)}) {name}")

    plus = sum(n for _, n in added) + sum(
        max(0, bc - ac) for _, ac, bc in changed
    )
    minus = sum(n for _, n in removed) + sum(
        max(0, ac - bc) for _, ac, bc in changed
    )
    delta = b_total - a_total
    dsign = "+" if delta > 0 else ("-" if delta < 0 else "")
    print()
    print(f"net: +{plus} cards / -{minus} cards / Δ {dsign}{abs(delta)}")
    return 0


# ---------- fetch-meta ---------------------------------------------------

# Per-source registry. Each entry maps `--source <name>` to the parser
# callable that turns raw HTML into `list[ParsedDeck]`. Adding a new
# source = adding a `tools/mtg_sources/<host>.py` and registering its
# `parse_<host>` here. **Hard rule**: every entry must also expose a
# `url_for_format(fmt) -> str | None` so `cmd_fetch_meta` can resolve
# the URL without hardcoding it. Keep these two-tuple to avoid a third
# config layer.
_FETCH_META_PARSERS = {
    "aetherhub": (parse_aetherhub, aetherhub_url_for_format),
    "moxfield": (parse_moxfield, moxfield_url_for_format),
    "mtgazone": (parse_mtgazone, mtgazone_url_for_format),
    "mtggoldfish": (parse_mtggoldfish, mtggoldfish_url_for_format),
    "mtgdecks": (parse_mtgdecks, mtgdecks_url_for_format),
    "untapped": (parse_untapped, untapped_url_for_format),
}

# Sources the spec lists in the `--source` choices but that we have not
# wired a parser for. Listed explicitly so `argparse` accepts the choice
# and `cmd_fetch_meta` can emit a deferred-source error message rather
# than argparse's generic "invalid choice" — gives Claude an actionable
# pointer to docs/sources.md. Empty as of 2026-05-01: untapped used to
# live here (Next.js SPA + 403'd API), but the `__NEXT_DATA__` SSR
# block carries the deck list (or a `decksQueryUrl` to a /free API
# endpoint that 200s anonymously) and the V4 deckstring decoder lifts
# the binary -> Scryfall-printing chain into mtg_sources.untapped.
_FETCH_META_DEFERRED_SOURCES: tuple[str, ...] = ()

_META_CACHE_TTL_SECS = 24 * 3600

# Sources where `docs/sources.md` records "occasional 403; retry once".
# `_fetch_meta_page` honours this for the index fetch; per-archetype
# sub-resource fetches inside `parse_mtggoldfish` / `parse_mtgdecks`
# use the same retry helper in `_common.py`. mtgdecks sits behind
# Cloudflare and historically 403s scripted requests (see
# `docs/sources.md`); the probe on 2026-05-01 returned 200 with a
# vanilla UA, but the retry-once policy is cheap insurance.
_FETCH_META_RETRY_403 = frozenset({"mtggoldfish", "mtgdecks"})

# Sources that need a `Referer` header on the index fetch. mtgdecks
# documents Referer as required for archetype/deck sub-resources; the
# index fetch sends the source root so the whole flow is consistently
# attributed and stays inside the documented contract.
_FETCH_META_INDEX_REFERER = {
    "mtgdecks": "https://mtgdecks.net/",
    "moxfield": "https://www.moxfield.com/",
}

# Sources whose index endpoint refuses the toolkit User-Agent and needs
# a browser string. Moxfield's api2 sits behind Cloudflare and 403s
# scripted UAs; we send the same Chrome string the parser uses for its
# per-deck calls so the burst looks consistent.
_FETCH_META_INDEX_USER_AGENT = {
    "aetherhub": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "moxfield": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Sources that need extra headers (Origin etc.) on the index fetch.
# Moxfield's API CORS allow-list expects an Origin matching the
# website's; without it Cloudflare's WAF returns a generic 403 page.
_FETCH_META_INDEX_EXTRA_HEADERS = {
    "moxfield": {"Origin": "https://www.moxfield.com"},
}

# Accept header overrides — sources whose index endpoint serves JSON
# (moxfield) need application/json or the API serves an HTML wrapper.
_FETCH_META_INDEX_ACCEPT = {
    "moxfield": "application/json, text/plain, */*",
}

# Per-source default --limit. The headline use case ("find every viable
# deck I can build") needs a deep corpus; sources that scale up cleanly
# (moxfield's user-built universe, untapped's Arena pings) get
# format-appropriate defaults so a fresh `fetch-meta` produces a usable
# pool without the user having to know per-source ceilings. Sources
# whose natural ceiling is small (mtgazone/mtggoldfish/mtgdecks tier
# lists run ~30 archetypes total) stay `None` so we take everything.
# aetherhub's index hard-caps at 50 archetypes; matching the ceiling
# keeps the help text honest. Wall-clock cost (per format, fresh):
# moxfield 300 * 0.6s ~= 3min; aetherhub 50 * 0.5s ~= 25s; untapped
# walk dominated by SPA-page latency, not deck count. User can always
# override with --limit explicitly (incl. --limit 0 to disable the cap).
_FETCH_META_DEFAULT_LIMIT: dict[str, int | None] = {
    "aetherhub": 50,
    "moxfield": 300,
    "untapped": 250,
    "mtgazone": None,
    "mtggoldfish": None,
    "mtgdecks": None,
}


def _meta_cache_path(source: str, url: str) -> Path:
    """Where to stash the raw HTML for `(source, url)`.

    sha256(url)[:16] keeps filenames bounded and stable across runs;
    `data/meta-cache/<source>/<hash>.html` namespaces by source so two
    parsers can request the same URL without colliding.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return DATA / "meta-cache" / source / f"{digest}.html"


def _fetch_meta_page(url: str, *, source: str, no_cache: bool) -> str:
    """Fetch HTML for `url`, honouring a 24h on-disk cache.

    Hard-fails on HTTP non-200 (raises HTTPError; caller catches and
    surfaces). For sources in `_FETCH_META_RETRY_403` (mtggoldfish per
    `docs/sources.md`), a single retry with a 2s delay is attempted on
    the first 403; any second 403 (or any other error) re-raises.
    Cache writes are atomic-ish (write + rename) so a killed process
    can't leave a half-written file that a later run trusts.
    """
    cache_path = _meta_cache_path(source, url)
    if not no_cache and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age <= _META_CACHE_TTL_SECS:
            return cache_path.read_text(encoding="utf-8", errors="replace")

    text = _common.http_get_text(
        url,
        accept=_FETCH_META_INDEX_ACCEPT.get(
            source, "text/html,application/xhtml+xml",
        ),
        retry_403_once=source in _FETCH_META_RETRY_403,
        referer=_FETCH_META_INDEX_REFERER.get(source),
        user_agent=_FETCH_META_INDEX_USER_AGENT.get(source),
        extra_headers=_FETCH_META_INDEX_EXTRA_HEADERS.get(source),
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, cache_path)
    return text


def _write_meta_corpus(decks: list[ParsedDeck], out_dir: Path) -> dict:
    """Materialise `decks` to `<out_dir>/<slug>.txt` + meta.json sidecar.

    Sidecar is **merge-by-filename**: existing entries keyed by other
    filenames are preserved, entries this run produced overwrite their
    own keys. Lets the caller refresh one source without losing
    sidecar entries written by a different source / earlier run.

    Returns the merged sidecar dict so callers (incl. --json mode) can
    print it without re-reading from disk.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / "meta.json"
    sidecar: dict[str, dict] = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text())
            if isinstance(existing, dict):
                sidecar = existing
        except (OSError, json.JSONDecodeError):
            sidecar = {}

    for d in decks:
        deck_file = out_dir / f"{d.slug}.txt"
        _write_mtga_export(deck_file, d.entries)
        entry: dict = {
            "source": d.source,
            "tier": d.tier,
            "winrate": d.winrate,
            "sample": d.sample,
            "fetched": d.fetched,
            "archetype": d.archetype,
            "url": d.url,
            # Number of source-listed copies the parser couldn't resolve to
            # a Scryfall printing — the deck file is short by this much.
            # Surfaced here (not stderr-only) so a later `validate` /
            # `coverage` run can see why the deck has < 60/100 cards
            # without re-running the fetch.
            "unresolved": d.unresolved,
            # Cross-source identity hash (sorted multiset of name×count
            # over main+commander+companion, basics excluded). Lets a
            # later `fetch-meta` run from a different source detect a
            # collision without re-parsing every existing deck file.
            "cards_hash": _common.cards_hash(d),
        }
        if d.also_seen_at:
            entry["also_seen_at"] = list(d.also_seen_at)
        sidecar[deck_file.name] = entry

    meta_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    return sidecar


def _evict_corpus_slugs(out_dir: Path, slugs: list[str]) -> None:
    """Remove `<slug>.txt` files + matching sidecar entries.

    Used by `cmd_fetch_meta` after dedup tells us a fresh higher-
    priority source beat an existing on-disk deck. Idempotent: missing
    files / missing sidecar entries are no-ops.
    """
    if not slugs:
        return
    for slug in slugs:
        path = out_dir / f"{slug}.txt"
        if path.exists():
            path.unlink()
    meta_path = out_dir / "meta.json"
    if not meta_path.exists():
        return
    try:
        sidecar = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(sidecar, dict):
        return
    for slug in slugs:
        sidecar.pop(f"{slug}.txt", None)
    meta_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")


def _existing_corpus_hashes(out_dir: Path) -> dict[str, tuple[str, str]]:
    """Read `<out_dir>/meta.json` → `{cards_hash: (source, slug)}`.

    Backs cross-source dedup in `cmd_fetch_meta`: a fresh deck colliding
    with an entry already on disk loses to the higher-priority source.
    Entries lacking `cards_hash` (pre-dedup-feature corpus) are skipped
    silently — they get rehashed on their next fetch.
    """
    meta_path = out_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        sidecar = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(sidecar, dict):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for filename, entry in sidecar.items():
        if not isinstance(entry, dict):
            continue
        h = entry.get("cards_hash")
        src = entry.get("source")
        if not isinstance(h, str) or not h or not isinstance(src, str):
            continue
        slug = filename.removesuffix(".txt")
        out[h] = (src, slug)
    return out


def cmd_fetch_meta(args: argparse.Namespace) -> int:
    """Scrape a meta source into a directory of MTGA-export deck files.

    Hard-fail policy (production-ready floor — nothing written to --out):

      Exit 2 (user error — caller asked for an impossible run):
        * unknown / deferred source;
        * unsupported format for any source;
        * source does not publish a tier list for the chosen format
          (e.g. mtggoldfish + brawl).

      Exit 1 (runtime / source-side failure):
        * HTTP non-200;
        * parser raises ValueError (drift);
        * parser returns zero decks from a 200 page (= schema drift);
        * resolution failures that would leave `--out` empty.
    """
    source = args.source
    if source in _FETCH_META_DEFERRED_SOURCES:
        supported = ", ".join(sorted(_FETCH_META_PARSERS))
        print(
            f"unknown source: {source} (deferred — see docs/sources.md "
            f"bot-block table; supported parsers: {supported})",
            file=sys.stderr,
        )
        return 2
    if source not in _FETCH_META_PARSERS:
        print(f"unknown source: {source}", file=sys.stderr)
        return 2

    parse_fn, url_fn = _FETCH_META_PARSERS[source]
    fmt = args.format.lower()
    if fmt not in ARENA_FORMATS:
        print(
            f"format must be one of: {', '.join(sorted(ARENA_FORMATS))}",
            file=sys.stderr,
        )
        return 2
    url = url_fn(fmt)
    if url is None:
        print(
            f"{source} does not publish a tier list for format {fmt!r}",
            file=sys.stderr,
        )
        # Exit 2 (user error) — caller picked a (source, format) pair the
        # source provably can't satisfy. Mirrors the unknown-source /
        # unsupported-format branches above. Exit 1 stays reserved for
        # runtime / source-side failures (HTTP, drift, zero-deck-parse).
        return 2

    _warn_if_stale()
    fetched = time.strftime("%Y-%m-%d", time.gmtime())

    try:
        html_text = _fetch_meta_page(url, source=source, no_cache=args.no_cache)
    except urllib.error.HTTPError as e:
        print(f"fetch failed: {url} -> HTTP {e.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"fetch failed: {url} -> {e.reason}", file=sys.stderr)
        return 1

    # `limit` is a soft hint to the parser: sources with large per-
    # archetype HTTP fan-out (untapped's historic-brawl walk is 1470
    # archetype pages × 2 sub-fetches each) can short-circuit instead
    # of fetching everything just to have `decks[:limit]` slice it
    # down post-parse. Every parser's signature accepts (and ignores
    # if not useful) `limit` via `**_` — see mtg_sources/*.py.
    #
    # Default-limit resolution: explicit user input wins (incl. 0 =
    # no cap). Otherwise fall back to per-source defaults so a fresh
    # `fetch-meta` produces a deep-enough corpus without the user
    # knowing each source's natural ceiling.
    if args.limit is not None:
        effective_limit = args.limit
    else:
        effective_limit = _FETCH_META_DEFAULT_LIMIT.get(source)
    try:
        decks = parse_fn(
            html_text, fmt,
            fetched=fetched, url=url, resolve_name=_resolve_card,
            limit=effective_limit,
        )
    except ValueError as e:
        print(f"parser drift on {url}: {e}", file=sys.stderr)
        return 1

    if not decks:
        print(
            f"parser drift on {url}: 0 decks extracted from a 200 response. "
            f"Source page likely changed structure; do not write to --out.",
            file=sys.stderr,
        )
        return 1

    # Stub filter applied centrally so every parser benefits — used to
    # be inlined in untapped.py for the brawl `laelia-the-blade-reforged`
    # commander+5-nonland+94-Mountains pattern.
    pre_stub = len(decks)
    decks = [d for d in decks if not _common.is_stub_deck(d, _resolve_card)]
    stub_dropped = pre_stub - len(decks)

    out_dir = Path(args.out) if args.out else (
        CORPUS / fmt
    )

    # Cross-source dedup: collapse same-multiset decks (basics excluded)
    # within this fetch and against the existing on-disk corpus. Higher-
    # priority source (per `_common.SOURCE_PRIORITY`) wins; the loser's
    # URL is appended to `also_seen_at` for traceability. Disk-resident
    # losers (`evicted`) are removed so the corpus stays canonical.
    existing_hashes = _existing_corpus_hashes(out_dir)
    decks, dedup_dropped_decks, evicted = _common.dedup_decks(
        decks, existing_hashes=existing_hashes,
    )
    dedup_dropped = len(dedup_dropped_decks)
    _evict_corpus_slugs(out_dir, evicted)

    if effective_limit is not None and effective_limit > 0:
        decks = decks[:effective_limit]

    sidecar = _write_meta_corpus(decks, out_dir)

    total_dropped = sum(d.unresolved for d in decks)

    if args.json:
        print(json.dumps({
            "source": source,
            "format": fmt,
            "url": url,
            "fetched": fetched,
            "out": str(out_dir),
            "deck_count": len(decks),
            "unresolved_total": total_dropped,
            "stub_dropped": stub_dropped,
            "dedup_dropped": dedup_dropped,
            "evicted_existing": len(evicted),
            "decks": [
                {
                    "slug": d.slug,
                    "archetype": d.archetype,
                    "tier": d.tier,
                    "url": d.url,
                    "main": sum(e.count for e in d.entries if e.section == "deck"),
                    "sideboard": sum(
                        e.count for e in d.entries if e.section == "sideboard"
                    ),
                    # Source-listed copies the parser couldn't resolve
                    # to a Scryfall printing — the deck file is short
                    # by this much. >0 signals a partial import.
                    "unresolved": d.unresolved,
                    "also_seen_at": list(d.also_seen_at),
                }
                for d in decks
            ],
        }, indent=2))
        return 0

    print(f"source : {source}")
    print(f"url    : {url}")
    print(f"format : {fmt}")
    print(f"out    : {out_dir}")
    print(f"decks  : {len(decks)}")
    if stub_dropped:
        print(f"stub-d : {stub_dropped} dropped (basic-land padding)")
    if dedup_dropped:
        print(f"dedup  : {dedup_dropped} fresh decks collapsed (cross-source)")
    if evicted:
        print(f"evict  : {len(evicted)} on-disk decks beaten by higher-priority source")
    print(f"sidecar: {len(sidecar)} total entries")
    print()
    # `drop` column shows per-deck dropped-copy count from `ParsedDeck.unresolved`
    # — copies the source listed but the parser couldn't resolve. > 0 means the
    # written deck file is short by that many cards (e.g. 56/60); a footer warning
    # surfaces the run-wide total so the operator sees corpus-level drift.
    print(f"{'tier':<4}  {'main':>4}  {'sb':>3}  {'drop':>4}  {'slug':<32}  archetype")
    print("-" * 84)
    for d in decks:
        main_n = sum(e.count for e in d.entries if e.section == "deck")
        sb_n = sum(e.count for e in d.entries if e.section == "sideboard")
        # Show a literal 0 (not "-") for the drop column so the eye
        # picks out the rows where it isn't 0.
        print(
            f"{d.tier or '-':<4}  {main_n:>4}  {sb_n:>3}  {d.unresolved:>4}  "
            f"{d.slug[:32]:<32}  {d.archetype}"
        )
    if total_dropped:
        # Partial-import warning: deck files were written but are short
        # by `total_dropped` copies. Surfaces as stderr so a downstream
        # `validate` sees the same signal even if stdout is piped away.
        print(
            f"[warn] {total_dropped} card cop"
            f"{'y' if total_dropped == 1 else 'ies'} unresolved across "
            f"{sum(1 for d in decks if d.unresolved)} deck"
            f"{'' if sum(1 for d in decks if d.unresolved) == 1 else 's'}; "
            f"deck files are short by that much (see `drop` column).",
            file=sys.stderr,
        )
    return 0


# ---------- recommend (collection-aware archetype + shell roll-up) -------


def _recommend_build_status(row: dict, min_threshold: float) -> str:
    """Classify a deck row into BUILDABLE / NEEDS_STAPLES / BLOCKED.

    BLOCKED beats NEEDS_STAPLES beats BUILDABLE — we report the worst
    blocker first so the user always sees the actual gating issue:

      * BLOCKED        — F2 anchor missing (commander or ≥50% corpus
                         staple). Substitution refuses to fill these,
                         so the deck simply isn't this deck without
                         them.
      * NEEDS_STAPLES  — F2 sub-fidelity floor tripped (`subs_acceptable`
                         False, i.e. > max-sub-pct of the deck would be
                         swapped). The user owns enough cards in role
                         shape to limp, but not enough to be playing
                         the archetype.
      * BUILDABLE      — owned_pct >= --min AND subs_acceptable AND no
                         missing anchors. Buildable today.

    Falls through to NEEDS_STAPLES when none of the explicit blockers
    fire but the deck is below `min_threshold` — "you don't own enough
    of the right cards" reads to the user as "needs staples", which is
    accurate.
    """
    if row.get("anchor_unfilled", 0) > 0:
        return "BLOCKED"
    if row.get("subs_acceptable") is False:
        return "NEEDS_STAPLES"
    base = max(
        row.get("owned_pct") or 0.0,
        row.get("with_subs_pct") or 0.0,
    )
    if base >= min_threshold:
        return "BUILDABLE"
    return "NEEDS_STAPLES"


def _recommend_compute(
    args: argparse.Namespace,
) -> tuple[dict, list[dict], list[dict]]:
    """Pure-compute core for `cmd_recommend`.

    Returns ``(meta, deck_rows, shell_rows)`` where:
      * `meta` is the run header (format, min, top, max_sub_pct, plus
        `corpus_size` / `decks_considered` / `buildable_count` so the
        text and JSON renderers stay in sync on the same numbers).
      * `deck_rows` are `_coverage_row` outputs (with F2 sidecar fields
        + `build_status` enum) sorted by composite desc and capped at
        `--top`. Already filtered against `--min` (via
        `owned_pct + with_subs_pct < 2 * min` — the F2 clamp means
        with_subs_pct == owned_pct when subs are unacceptable, so this
        also catches `NEEDS_STAPLES` decks that are nowhere close).
      * `shell_rows` are non-empty cluster->archetype matches when
        `--include-shells` is set OR fewer than 3 BUILDABLE decks
        survived the filter (the spec's auto-fallback so the user
        always gets *something* actionable).
    """
    fmt = args.format.lower()
    min_threshold = float(args.min)
    top_n = int(args.top)
    max_sub_pct = float(args.max_sub_pct)
    quiet = bool(getattr(args, "json", False))

    deck_paths = _corpus_deck_files(fmt, include_derived=True)
    snap = _load_collection()  # checked by caller; never None here.
    assert snap is not None

    idx = _load_index()
    owned = _aggregate_by_name(idx, _cards_owned(snap))

    deck_rows: list[dict] = []
    if deck_paths:
        fallback_warn = [False]
        for path in deck_paths:
            row = _coverage_row(
                path, idx, snap, owned,
                with_subs=True,
                fallback_warn=fallback_warn,
                quiet=quiet,
                max_sub_pct=max_sub_pct,
                include_subs_meta=True,
            )
            row["build_status"] = _recommend_build_status(row, min_threshold)
            deck_rows.append(row)

    # Filter: drop decks where neither owned nor sub-pct are anywhere
    # close to the threshold. F2 clamps with_subs_pct == owned_pct when
    # subs are unacceptable, so persist-combo (8.5% owned, sub-pct cap
    # tripped) ends up at 0.085 + 0.085 = 0.17 < 0.60 and gets dropped
    # cleanly. Decks that fail anchor checks but are otherwise in range
    # survive (so the user sees the BLOCKED status with the missing
    # anchor in the build advice).
    cutoff = 2.0 * min_threshold
    filtered = [
        r for r in deck_rows
        if (r["owned_pct"] + (r["with_subs_pct"] or 0.0)) >= cutoff
    ]
    filtered.sort(key=lambda r: (-r["composite"], r["archetype"]))
    capped = filtered[:top_n]

    buildable_count = sum(1 for r in capped if r["build_status"] == "BUILDABLE")
    want_shells = bool(getattr(args, "include_shells", False)) or (
        buildable_count < 3
    )

    shell_rows: list[dict] = []
    if want_shells:
        cards_owned = _cards_owned(snap)
        clusters = _shell_cluster_rows(
            idx, cards_owned, fmt,
            by="keyword",
            min_cards=15 if fmt in BRAWL_FORMATS else 24,
            top_anchors=10,
        )
        archetype_anchors = _load_archetype_anchors(fmt)
        freq_index = _load_freq_index(fmt, rebuild_if_stale=False)
        if freq_index is None or not (freq_index.get("cards") or {}):
            freq_index = None
        for cl in clusters:
            shell_names = cl.get("_card_names") or set()
            matches = (
                _shell_corpus_matches(
                    shell_names, archetype_anchors, freq_index,
                    min_pct=0.30, min_count=5,
                )
                if archetype_anchors else []
            )
            if not matches:
                continue
            shell_rows.append({
                "cluster_key": cl["key"],
                "owned_count": cl["count"],
                "anchors": cl["anchors"],
                "matches": matches,
            })

    meta = {
        "format": fmt,
        "min": round(min_threshold, 4),
        "top": top_n,
        "max_sub_pct": round(max_sub_pct, 4),
        "corpus_size": len(deck_paths),
        "decks_considered": len(deck_rows),
        "buildable_count": buildable_count,
        "shells_emitted": bool(shell_rows),
        "shells_reason": (
            "include_shells" if getattr(args, "include_shells", False)
            else ("fallback" if buildable_count < 3 else "none")
        ),
    }
    return meta, capped, shell_rows


def _print_recommend_text(
    meta: dict, decks: list[dict], shells: list[dict],
) -> None:
    """Human render. Sections mirror the JSON shape exactly:
    a per-deck ranked list with build advice + (when applicable) a
    cluster->archetype shell bridge.
    """
    fmt = meta["format"]
    print(
        f"recommend (fmt={fmt}, corpus={meta['corpus_size']}, "
        f"min={meta['min']}, top={meta['top']}, "
        f"max-sub-pct={meta['max_sub_pct']})"
    )
    print()

    if not decks:
        print(
            f"no decks above min={meta['min']} — try lowering --min, "
            f"expanding the corpus, or running tools/mtg fetch-meta"
        )
    else:
        print("=== ranked decks ===")
        for r in decks:
            wc = r["missing_wc"]
            wc_str = (
                f"{wc['mythic']}/{wc['rare']}/{wc['uncommon']}/{wc['common']}"
            )
            owned_pct = (r["owned_pct"] or 0.0) * 100
            sub_pct = (r["with_subs_pct"] or 0.0) * 100
            tier_str = r["tier"] or "-"
            line = (
                f"  {r['archetype']:<32} tier={tier_str:<2} "
                f"composite={r['composite']:.4f}  "
                f"owned={owned_pct:.0f}% (subs={sub_pct:.0f}%)  "
                f"missing {wc_str} WC"
            )
            print(line)
            top3 = r.get("top3_missing") or []
            if top3:
                print(f"    top missing: {', '.join(top3)}")
            anchor_unfilled = r.get("anchor_unfilled", 0)
            anchor_total = r.get("anchor_total", 0)
            if anchor_unfilled > 0:
                print(
                    f"    anchors missing: {anchor_unfilled} of {anchor_total}"
                )
            status = r["build_status"]
            if status == "BUILDABLE":
                advice = "    -> BUILDABLE"
            elif status == "BLOCKED":
                advice = "    -> BLOCKED — anchor missing"
            else:  # NEEDS_STAPLES
                advice = (
                    f"    -> NEEDS STAPLES "
                    f"({wc['mythic']}x mythic, {wc['rare']}x rare)"
                )
            print(advice)
            print()

    why = meta.get("shells_reason")
    if shells or why in {"include_shells", "fallback"}:
        suffix = (
            "" if why == "include_shells"
            else "  (auto-shown: < 3 BUILDABLE decks)"
        )
        print(f"=== shell -> archetype bridge ==={suffix}")
        if not shells:
            print("  (no clusters above match threshold)")
        for s in shells:
            match_strs = []
            for m in s["matches"]:
                pct = m["overlap_pct"] * 100
                tier = m.get("tier") or "-"
                match_strs.append(
                    f"{m['archetype']} [{tier}] {pct:.0f}% overlap"
                )
            print(
                f"  [{s['cluster_key']}] ({s['owned_count']} owned cards)"
                f" -> matches: {', '.join(match_strs)}"
            )


def cmd_recommend(args: argparse.Namespace) -> int:
    """End-to-end: rank decks the user can build and surface novel-deck
    shells. Always uses the live `data/collection.json` snapshot — the
    headline goal is "what should I actually play tonight", not
    "what could I play if I owned more". Errors out cleanly if the
    snapshot is missing.

    Output sections:
      1. Ranked deck recommendations (composite-sorted, capped at
         --top; per-deck build_status: BUILDABLE / NEEDS_STAPLES /
         BLOCKED).
      2. Shell -> archetype bridge (always with --include-shells; auto
         when < 3 BUILDABLE decks survive the --min filter).

    Stale freq index emits a one-line stderr warn — `recommend` reads
    `_freq.json` for sub-scoring + shell match weighting, so the user
    knows the prior may be off without us forcing a rebuild on a
    read-only command.
    """
    fmt = args.format.lower()
    if fmt not in ARENA_FORMATS:
        print(
            f"format must be one of: {', '.join(sorted(ARENA_FORMATS))}",
            file=sys.stderr,
        )
        return 2
    args.format = fmt

    if not (0.0 <= args.min <= 1.0):
        print("--min must be in [0, 1]", file=sys.stderr)
        return 2
    if args.top <= 0:
        print("--top must be > 0", file=sys.stderr)
        return 2
    if not (0.0 < args.max_sub_pct <= 1.0):
        print("--max-sub-pct must be in (0, 1]", file=sys.stderr)
        return 2

    if _load_collection() is None:
        print(
            "no collection snapshot — run 'tools/mtg collection dump' first",
            file=sys.stderr,
        )
        return 2

    if not _corpus_deck_files(fmt, include_derived=True):
        msg = (
            f"[warn] no corpus for {fmt} — "
            f"run tools/mtg fetch-meta {fmt} first"
        )
        if args.json:
            print(msg, file=sys.stderr)
            _emit_json({
                "format": fmt,
                "min": round(float(args.min), 4),
                "top": int(args.top),
                "max_sub_pct": round(float(args.max_sub_pct), 4),
                "corpus_size": 0,
                "decks": [],
                "shells": [],
            })
        else:
            print(msg, file=sys.stderr)
        return 0

    if _freq_index_is_stale(fmt):
        idx_path = _freq_index_path(fmt)
        if idx_path.exists():
            age_days = max(
                0,
                int((time.time() - idx_path.stat().st_mtime) // 86400),
            )
            print(
                f"[warn] freq index for {fmt} is {age_days} days old "
                f"(corpus newer); consider tools/mtg freq {fmt} --rebuild",
                file=sys.stderr,
            )
        else:
            print(
                f"[warn] freq index for {fmt} missing; "
                f"consider tools/mtg freq {fmt} --rebuild",
                file=sys.stderr,
            )

    meta, decks, shells = _recommend_compute(args)

    if args.json:
        _emit_json({
            "format": meta["format"],
            "min": meta["min"],
            "top": meta["top"],
            "max_sub_pct": meta["max_sub_pct"],
            "corpus_size": meta["corpus_size"],
            "decks_considered": meta["decks_considered"],
            "buildable_count": meta["buildable_count"],
            "shells_reason": meta["shells_reason"],
            "decks": decks,
            "shells": shells,
        })
        return 0

    _print_recommend_text(meta, decks, shells)
    return 0


# ---------- derive / invent (composition primitives) --------------------


def _format_from_corpus_path(deck_path: Path) -> str | None:
    """If `deck_path` resolves under data/corpus/<fmt>/, return fmt.

    Used by `derive` so the user doesn't have to repeat the format
    every time — the corpus layout already encodes it. Returns None
    when the path isn't under the corpus root or the parent dir is
    not an Arena format slug.
    """
    try:
        rel = deck_path.resolve().relative_to(CORPUS.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    fmt = parts[0]
    return fmt if fmt in ARENA_FORMATS else None


def _apply_subs_to_entries(
    entries: list[DeckEntry],
    json_missing: list[dict],
    idx: dict,
    is_brawl: bool,
) -> list[DeckEntry]:
    """Lift the `cmd_suggest_subs --apply` rewrite into a reusable helper.

    Walks `json_missing` (output of `_run_suggest_subs`), picks the
    top-scored candidate per non-anchor slot whose remaining copy
    capacity covers the deficit, and rewrites `entries` accordingly.
    Anchors and slots without a viable candidate pass through
    unchanged. Commander entries are never substituted.

    The `--apply` body in `cmd_suggest_subs` was the source — extracted
    verbatim to keep `derive` and `--apply` on identical mechanics.
    """
    max_copies = 1 if is_brawl else 4
    deck_copies: dict[str, int] = {}
    for e in entries:
        if e.section in {"commander", "deck", "sideboard"}:
            deck_copies[e.name] = deck_copies.get(e.name, 0) + e.count

    used: dict[str, int] = dict(deck_copies)
    top_by_name: dict[str, tuple[str, int]] = {}
    for json_slot in json_missing:
        if json_slot.get("anchor"):
            continue
        slot_deficit = json_slot["deficit"]
        chosen_name: str | None = None
        for entry in json_slot["candidates"]:
            cname = entry["name"]
            resolved = _resolve_card(cname)
            if resolved is not None and _is_basic(resolved):
                chosen_name = cname
                break
            if max_copies - used.get(cname, 0) >= slot_deficit:
                chosen_name = cname
                break
        if chosen_name is None:
            continue
        resolved = _resolve_card(chosen_name)
        if resolved is None or not _is_basic(resolved):
            used[chosen_name] = used.get(chosen_name, 0) + slot_deficit
        top_by_name[json_slot["card"].lower()] = (chosen_name, slot_deficit)

    new_entries: list[DeckEntry] = []
    for e in entries:
        key = e.name.lower()
        if e.section == "commander":
            new_entries.append(e)
            continue
        if key in top_by_name and e.section in {"deck", "sideboard"}:
            cand_name, deficit = top_by_name[key]
            take = min(deficit, e.count)
            remaining = e.count - take
            if remaining > 0:
                new_entries.append(DeckEntry(
                    remaining, e.name, e.set_code, e.collector, e.section,
                ))
            printings = idx["by_name"].get(cand_name.lower()) or []
            if not printings:
                new_entries.append(e)
                continue
            p = printings[0]
            new_entries.append(DeckEntry(
                take, cand_name,
                (p.get("set") or "").upper(),
                str(p.get("collector_number") or ""),
                e.section,
            ))
            top_by_name[key] = (cand_name, deficit - take)
            if top_by_name[key][1] <= 0:
                del top_by_name[key]
        else:
            new_entries.append(e)
    return new_entries


def cmd_derive(args: argparse.Namespace) -> int:
    """Ownership-maximized variant of a corpus deck.

    Substrate: `_run_suggest_subs` over the source deck, then apply the
    top-scored candidate to every non-anchor slot. Anchors (commander +
    format staples per `_anchor_names`) stay verbatim — when missing,
    the derived deck inherits the gap and `anchor_unfilled` flags it
    in the provenance sidecar so `recommend` can still surface the
    "needs anchor" status.

    Default output: `data/corpus/<fmt>/derived/<source-slug>.txt`.
    Format auto-detected from the source path's parent slug; --format
    overrides for off-corpus inputs.

    Provenance sidecar `data/corpus/<fmt>/derived/_meta.json` carries
    `{output: {source_slug, source_path, source_origin, source_url,
    derived_at, subs_pct, subs_acceptable, cards_substituted,
    anchor_unfilled, missing_total, fillable, unfilled}}`. Re-runnable;
    each invocation overwrites both the .txt and the matching sidecar
    entry. Re-runs surface drift (e.g. new printings, freshly-collected
    cards changing the candidate ranking).

    derive raises subs_pct's ceiling: the goal is substitution, so a
    20% rewrite cap (suggest-subs default) defeats the point. Default
    here is 1.0 (no cap); override with --max-sub-pct if you want the
    F2 fidelity floor enforced.
    """
    _warn_if_stale()
    _warn_if_collection_stale()

    deck_path = Path(args.deck)
    if not deck_path.exists():
        print(f"deck file not found: {deck_path}", file=sys.stderr)
        return 2

    inferred_fmt = _format_from_corpus_path(deck_path)
    fmt = (args.format or inferred_fmt or "").lower()
    if not fmt:
        print(
            f"could not infer format from {deck_path} (not under "
            f"data/corpus/<fmt>/); pass --format explicitly",
            file=sys.stderr,
        )
        return 2
    if fmt not in ARENA_FORMATS:
        print(
            f"format must be one of: {', '.join(sorted(ARENA_FORMATS))}",
            file=sys.stderr,
        )
        return 2

    if args.max_per_card < 1:
        print("--max-per-card must be >= 1", file=sys.stderr)
        return 2
    if not (0.0 <= args.max_sub_pct <= 1.0):
        print("--max-sub-pct must be between 0.0 and 1.0", file=sys.stderr)
        return 2

    snap = _load_collection()
    if snap is None:
        sys.exit(_empty_state_message().rstrip())
    idx = _load_index()

    result = _run_suggest_subs(
        deck_path, fmt, idx, snap, args.max_per_card,
        quiet=True,
        anchor_check=True,
        max_sub_pct=args.max_sub_pct,
        strictlybetter=not args.no_strictlybetter,
    )

    entries = parse_deck(deck_path)
    is_brawl = fmt in BRAWL_FORMATS
    new_entries = _apply_subs_to_entries(
        entries, result["missing"], idx, is_brawl,
    )

    out_path = (
        Path(args.out) if args.out
        else CORPUS / fmt / "derived" / f"{deck_path.stem}.txt"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_mtga_export(out_path, new_entries)

    sidecar_path = out_path.parent / "_meta.json"
    sidecar: dict[str, dict] = {}
    if sidecar_path.exists():
        try:
            existing = json.loads(sidecar_path.read_text())
            if isinstance(existing, dict):
                sidecar = existing
        except (OSError, json.JSONDecodeError):
            pass
    src_meta = _load_deck_meta(deck_path)
    sidecar[out_path.name] = {
        "source_slug": deck_path.stem,
        "source_path": str(deck_path),
        "source_origin": src_meta.get("source"),
        "source_url": src_meta.get("url"),
        "derived_at": time.strftime("%Y-%m-%d", time.gmtime()),
        "subs_pct": result["subs_pct"],
        "subs_acceptable": result["subs_acceptable"],
        "cards_substituted": result["cards_substituted"],
        "anchor_unfilled": result["summary"]["anchor_unfilled"],
        "missing_total": result["summary"]["missing_cards"],
        "fillable": result["summary"]["fillable"],
        "unfilled": result["summary"]["unfilled"],
    }
    sidecar_path.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n"
    )

    payload = {
        "source": str(deck_path),
        "derived": str(out_path),
        "format": fmt,
        "subs_pct": result["subs_pct"],
        "subs_acceptable": result["subs_acceptable"],
        "cards_substituted": result["cards_substituted"],
        "anchor_unfilled": result["summary"]["anchor_unfilled"],
        "missing_total": result["summary"]["missing_cards"],
        "fillable": result["summary"]["fillable"],
        "unfilled": result["summary"]["unfilled"],
    }
    if args.json:
        _emit_json(payload)
        return 0

    print(f"derived {deck_path.name} -> {out_path}")
    print(
        f"  subs_pct={result['subs_pct'] * 100:.1f}%  "
        f"acceptable={result['subs_acceptable']}  "
        f"substituted={result['cards_substituted']}  "
        f"anchors_missing={result['summary']['anchor_unfilled']}/"
        f"{result['summary']['missing_cards']}"
    )
    return 0


# ---------- entrypoint ---------------------------------------------------


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Register the standard `--json` flag on a read-only subcommand.

    Centralised so help text stays uniform across the CLI surface and a
    future change to naming/behaviour is one edit.
    """
    parser.add_argument(
        "--json", action="store_true",
        help="emit a JSON payload instead of human-readable text",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mtg", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="refresh Scryfall bulk + rebuild index")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("card", help="show full info for a card by name")
    s.add_argument("name")
    _add_json_flag(s)
    s.set_defaults(func=cmd_card)

    s = sub.add_parser("printing", help="lookup by MTGA-style set+collector")
    s.add_argument("set")
    s.add_argument("num")
    _add_json_flag(s)
    s.set_defaults(func=cmd_printing)

    s = sub.add_parser("legal", help="check legality in an Arena format")
    s.add_argument("name")
    s.add_argument("format")
    _add_json_flag(s)
    s.set_defaults(func=cmd_legal)

    s = sub.add_parser("validate", help="validate an MTGA-export deck file")
    s.add_argument("deck")
    s.add_argument("-f", "--format", required=True)
    s.add_argument("-v", "--verbose", action="store_true", help="print per-card status")
    _add_json_flag(s)
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("analyze", help="composition breakdown (curve, role mix, CA)")
    s.add_argument("deck")
    s.add_argument("--include-sideboard", action="store_true", default=False)
    s.add_argument("--sideboard-only", action="store_true", default=False)
    _add_json_flag(s)
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("related", help="cards sharing each keyword with the anchor card")
    s.add_argument("name")
    s.add_argument("-f", "--format", default=None, help="filter by Arena format")
    s.add_argument("--limit", type=int, default=15)
    _add_json_flag(s)
    s.set_defaults(func=cmd_related)

    s = sub.add_parser("manabase", help="pip demand, color sources, etb-tapped lands")
    s.add_argument("deck")
    _add_json_flag(s)
    s.set_defaults(func=cmd_manabase)

    s = sub.add_parser("wildcards", help="rarity breakdown for MTGA wildcard estimates")
    s.add_argument("deck")
    s.add_argument(
        "--list",
        action="store_true",
        help="also list every card grouped by rarity",
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_wildcards)

    s = sub.add_parser(
        "companion",
        help="check each MTGA companion's mechanical predicate against the deck",
    )
    s.add_argument("deck")
    s.add_argument("-f", "--format", default="brawl", help="format (default: brawl)")
    _add_json_flag(s)
    s.set_defaults(func=cmd_companion)

    s = sub.add_parser(
        "check",
        help="full battery: validate + analyze + manabase + wildcards + companion",
    )
    s.add_argument("deck")
    s.add_argument(
        "-f",
        "--format",
        default="brawl",
        help="format for the validate stage (default: brawl)",
    )
    s.add_argument(
        "--collection",
        action="store_true",
        help="also run `gaps` if a collection snapshot exists",
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("search", help="live Scryfall search (one HTTP request)")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    _add_json_flag(s)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser(
        "collection",
        help="show summary of current collection snapshot or manage it",
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_collection)
    csub = s.add_subparsers(dest="collection_cmd")

    sd = csub.add_parser(
        "dump",
        help="full snapshot via DLL injection into the running MTGA process",
    )
    sd.add_argument(
        "--out",
        default=None,
        help="raw dump path (default: data/collection.dump.json). The "
        "canonical snapshot is always written to data/collection.json.",
    )
    sd.set_defaults(func=cmd_collection_dump)

    si = csub.add_parser(
        "import",
        help="import a tracker export (CSV/JSON) into data/collection.json",
    )
    si.add_argument("file", help="path to CSV or JSON exported by a tracker")
    si.set_defaults(func=cmd_collection_import)

    sf = csub.add_parser(
        "from-decks",
        help="lower-bound snapshot reconstructed from MTGA decks in Player.log",
    )
    sf.add_argument(
        "--log",
        default=None,
        help="explicit Player.log path (default: auto-detect Linux/Proton, macOS, WSL)",
    )
    sf.set_defaults(func=cmd_collection_from_decks)

    s = sub.add_parser("own", help="show owned count for a card")
    s.add_argument("name", help="card name (Arena-style)")
    _add_json_flag(s)
    s.set_defaults(func=cmd_own)

    s = sub.add_parser(
        "owned",
        help="list owned cards matching a Scryfall query (live, paginated)",
    )
    s.add_argument("query", help="Scryfall query (https://scryfall.com/docs/syntax)")
    s.add_argument(
        "--min", type=int, default=1, help="filter to cards owned ≥N copies (default 1)"
    )
    s.add_argument(
        "--unique",
        action="store_true",
        help="collapse printings: one row per name, qty = max owned across printings",
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_owned)

    s = sub.add_parser(
        "suggest-subs",
        help="propose owned replacements for missing cards in a deck",
    )
    s.add_argument("deck", help="path to MTGA-export deck file")
    s.add_argument(
        "-f", "--format", default="brawl",
        help="format predicate (default: brawl)",
    )
    s.add_argument(
        "--max-per-card", type=int, default=5,
        help="max candidates per missing card (default: 5)",
    )
    s.add_argument(
        "--apply", default=None, metavar="OUT",
        help="write a substituted deck to OUT (validates clean for -f)",
    )
    s.add_argument(
        "--max-sub-pct", type=float, default=0.30, metavar="N",
        help=(
            "refuse --apply when more than N (0.0-1.0) of the deck "
            "would be substituted (default: 0.30 = 30%%)"
        ),
    )
    s.add_argument(
        "--no-anchor-check", action="store_true",
        help=(
            "disable anchor preservation (commander + format staples "
            "with deck_pct >= 0.50) — power-user override"
        ),
    )
    s.add_argument(
        "--no-strictlybetter", action="store_true",
        help=(
            "skip strictlybetter.eu lookup; rely on heuristic scoring "
            "only (offline mode — disables functional-reprint preference)"
        ),
    )
    s.add_argument(
        "--json", action="store_true",
        help="emit JSON instead of text table",
    )
    s.set_defaults(func=cmd_suggest_subs)

    s = sub.add_parser(
        "gaps",
        help="cards you are short for a deck + wildcard cost",
    )
    s.add_argument("deck", help="path to MTGA-export deck file")
    _add_json_flag(s)
    s.set_defaults(func=cmd_gaps)

    s = sub.add_parser(
        "coverage",
        help="%% of a deck buildable from your current collection",
    )
    s.add_argument(
        "deck", nargs="?", default=None,
        help="path to MTGA-export deck file (omit when using --batch)",
    )
    s.add_argument(
        "--batch", action="store_true",
        help="process every deck matching --glob",
    )
    s.add_argument(
        "--glob", default=None, metavar="PAT",
        help="glob pattern (e.g. 'decks/*/v1.txt'); supports ** with recursive",
    )
    s.add_argument(
        "--with-subs", action="store_true",
        help="also compute substitution-aware coverage via suggest-subs",
    )
    s.add_argument(
        "--json", action="store_true",
        help="emit a JSON payload instead of human-readable text "
        "(single-deck or batch)",
    )
    s.add_argument(
        "--min", type=float, default=None, metavar="N",
        help="filter rows below this fraction in [0,1] (ranking metric)",
    )
    s.add_argument(
        "--rank", choices=["ownership", "quality", "composite"],
        default="composite",
        help="sort key: ownership (legacy), quality (tier-first), "
        "composite (default: tier × ownership)",
    )
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser(
        "freq",
        help="card-frequency index over data/corpus/<fmt>/*.txt (popularity prior)",
    )
    s.add_argument(
        "format",
        help="Arena format (must have a corpus under data/corpus/<format>/)",
    )
    s.add_argument(
        "--rebuild", action="store_true",
        help="recompute the index and write data/corpus/<fmt>/_freq.json",
    )
    s.add_argument(
        "--no-rebuild", action="store_true",
        help="read-only: never auto-rebuild a stale index",
    )
    s.add_argument(
        "--card", default=None, metavar="NAME",
        help="show this card's row (deck_count, deck_pct, archetypes)",
    )
    s.add_argument(
        "--all", action="store_true",
        help="show every card (default: top 30 by deck_pct)",
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_freq)

    s = sub.add_parser(
        "shells",
        help="cluster owned cards by keyword/type/theme for novel-deck discovery",
    )
    s.add_argument(
        "--format", required=True,
        help="Arena format predicate (validated against ARENA_FORMATS)",
    )
    s.add_argument(
        "--by", choices=("keyword", "type", "theme"), default="keyword",
        help="bucketer (default: keyword)",
    )
    s.add_argument(
        "--min-cards", type=int, default=None, metavar="N",
        help="min cluster size (default: 15 for brawl, 24 otherwise)",
    )
    s.add_argument(
        "--top-anchors", type=int, default=10, metavar="N",
        help="anchor cards listed per cluster (default: 10)",
    )
    s.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="cap clusters listed (default: all)",
    )
    s.add_argument(
        "--match-corpus", action="store_true",
        help="for each shell, list the top 3 corpus archetypes whose "
             "card lists overlap most",
    )
    s.add_argument(
        "--match-min", type=float, default=0.30,
        help="minimum overlap_pct to report a match (default: 0.30)",
    )
    s.add_argument(
        "--match-anchors", type=int, default=5,
        help="minimum anchor_overlap count to report a match (default: 5)",
    )
    s.add_argument(
        "--json", action="store_true",
        help="emit JSON instead of text table",
    )
    s.set_defaults(func=cmd_shells)

    s = sub.add_parser(
        "wantlist",
        help="aggregate wildcard needs across every locally-saved deck",
    )
    s.add_argument(
        "--decks",
        default=None,
        help="glob pattern relative to repo root (default: decks/*/v*.txt)",
    )
    s.add_argument(
        "--latest-only",
        action="store_true",
        help="only consider the highest-numbered v<N>.txt per deck dir",
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_wantlist)

    s = sub.add_parser(
        "diff",
        help="per-card delta between two MTGA-export deck files",
    )
    s.add_argument("a", help="path to the older / left-hand deck file")
    s.add_argument("b", help="path to the newer / right-hand deck file")
    _add_json_flag(s)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser(
        "fetch-meta",
        help="scrape a meta source into <out>/<archetype>.txt + meta.json",
    )
    s.add_argument(
        "format",
        help=(
            "Arena format (standard/alchemy/historic/timeless/explorer/pioneer). "
            "Brawl variants are not on mtgazone tier lists."
        ),
    )
    s.add_argument(
        "--source",
        choices=("aetherhub", "moxfield", "untapped", "mtggoldfish", "mtgazone", "mtgdecks"),
        default="mtgazone",
        help=(
            "meta source (default: mtgazone). 'moxfield' = api2.moxfield.com "
            "user-built decks (largest corpus, all formats). 'aetherhub' = "
            "Arena-native /Metagame index w/ winrates (~50 archetypes per "
            "format). 'untapped' = Arena-native scrape (only automated "
            "brawl source). 'mtgdecks' covers Historic only. See docs/sources.md."
        ),
    )
    s.add_argument(
        "--out", default=None, metavar="DIR",
        help="output dir (default: data/corpus/<format>/)",
    )
    s.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help=(
            "cap deck count after parsing. If omitted, uses the "
            "per-source default (aetherhub=50, moxfield=300, "
            "untapped=250; mtgazone/mtggoldfish/mtgdecks=all). "
            "Pass --limit 0 to disable the cap entirely."
        ),
    )
    s.add_argument(
        "--json", action="store_true",
        help="emit JSON manifest instead of human-readable table",
    )
    s.add_argument(
        "--no-cache", action="store_true",
        help="bypass the 24h on-disk HTML cache and re-fetch",
    )
    s.set_defaults(func=cmd_fetch_meta)

    s = sub.add_parser(
        "recommend",
        help="rank corpus decks you can build (composite-sorted) + "
             "shell->archetype bridge for novel-deck discovery",
    )
    s.add_argument(
        "--format", required=True,
        help="Arena format (must have a corpus under data/corpus/<format>/)",
    )
    s.add_argument(
        "--min", type=float, default=0.30, metavar="N",
        help=(
            "drop decks where owned_pct + with_subs_pct < 2*N "
            "(default: 0.30; F2 clamp means decks with sub-pct cap "
            "tripped are filtered when ownership is also low)"
        ),
    )
    s.add_argument(
        "--top", type=int, default=10, metavar="N",
        help="cap ranked deck list at N (default: 10)",
    )
    s.add_argument(
        "--include-shells", action="store_true",
        help=(
            "always emit the shell->archetype bridge section (otherwise "
            "auto-shown only when < 3 BUILDABLE decks survive --min)"
        ),
    )
    s.add_argument(
        "--max-sub-pct", type=float, default=0.30, metavar="N",
        help=(
            "F2 sub-fidelity floor: clamp with_subs_pct to owned_pct "
            "when more than N (0.0-1.0) of the deck would be substituted "
            "(default: 0.30 = 30%%)"
        ),
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_recommend)

    s = sub.add_parser(
        "derive",
        help="rewrite a corpus deck with owned substitutions "
             "(per-slot top suggest-subs candidate); writes to "
             "data/corpus/<fmt>/derived/<slug>.txt by default",
    )
    s.add_argument("deck", help="source MTGA-export .txt (typically under data/corpus/<fmt>/)")
    s.add_argument(
        "-f", "--format", default=None,
        help=(
            "Arena format (auto-inferred when deck lives under "
            "data/corpus/<fmt>/; required otherwise)"
        ),
    )
    s.add_argument(
        "--out", default=None, metavar="PATH",
        help="output path (default: data/corpus/<fmt>/derived/<source-slug>.txt)",
    )
    s.add_argument(
        "--max-per-card", type=int, default=5, metavar="N",
        help="suggest-subs candidate pool size per missing slot (default: 5)",
    )
    s.add_argument(
        "--max-sub-pct", type=float, default=1.0, metavar="N",
        help=(
            "F2 sub-fidelity floor (0.0-1.0). Default 1.0 (no cap) since "
            "derive's job *is* substitution; pass a lower value to enforce "
            "the suggest-subs fidelity ceiling."
        ),
    )
    s.add_argument(
        "--no-strictlybetter", action="store_true",
        help="skip strictlybetter.eu lookup (offline; pure heuristic ranking)",
    )
    _add_json_flag(s)
    s.set_defaults(func=cmd_derive)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
