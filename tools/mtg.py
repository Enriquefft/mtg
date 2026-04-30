#!/usr/bin/env python3
"""mtg — local query layer over Scryfall bulk data.

Single source of truth: Scryfall `default_cards.json` (refreshed daily).
All deck validation runs offline against the cached bulk; only `sync` and
`search` hit the network.

Subcommands:
    sync                       refresh bulk + rebuild name index
    card <name>                full card info
    printing <SET> <NUM>       lookup by MTGA-style set+collector
    legal <name> <format>      yes/no legality with reason
    validate <deck.txt> -f F   parse + validate MTGA deck file
    search <scryfall-query>    live Scryfall search (one HTTP request)

Run `mtg <subcommand> --help` for details.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(os.environ.get("MTG_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
BULK_JSON = DATA / "default_cards.json"
INDEX_PKL = DATA / "index.pkl"
META_JSON = DATA / "bulk-meta.json"

USER_AGENT = "mtg-toolkit/0.1 (github.com/Enriquefft/mtg)"
SCRYFALL_BULK = "https://api.scryfall.com/bulk-data/default-cards"
SCRYFALL_API = "https://api.scryfall.com"

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
            },
            indent=2,
        )
    )
    print(
        f"indexed {len(cards)} printings, {len(index['by_name'])} unique names "
        f"in {time.time()-t0:.1f}s"
    )
    return 0


def _build_index(cards: list[dict]) -> dict:
    """Build name + (set, collector) lookup tables.

    `by_name` maps lowercased card name -> list of printings (minimal fields).
    `by_printing` maps "(set, collector_number)" -> printing.
    """
    by_name: dict[str, list[dict]] = {}
    by_printing: dict[tuple[str, str], dict] = {}
    for c in cards:
        slim = {k: c.get(k) for k in KEEP_FIELDS if k in c}
        key = c["name"].lower()
        by_name.setdefault(key, []).append(slim)
        by_printing[(c["set"].lower(), c["collector_number"])] = slim
    return {"by_name": by_name, "by_printing": by_printing}


# ---------- index loading -------------------------------------------------


_INDEX: dict | None = None


def _load_index() -> dict:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    if not INDEX_PKL.exists():
        sys.exit("no local index; run `mtg sync` first")
    with INDEX_PKL.open("rb") as f:
        _INDEX = pickle.load(f)
    return _INDEX


def _warn_if_stale(max_age_h: float = 36.0) -> None:
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
    print(_format_card(c))
    return 0


def cmd_printing(args: argparse.Namespace) -> int:
    _warn_if_stale()
    idx = _load_index()
    c = idx["by_printing"].get((args.set.lower(), args.num))
    if not c:
        print(f"printing not found: {args.set} {args.num}", file=sys.stderr)
        return 1
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
    print(f"{c['name']}: {legal} in {fmt}; arena={'yes' if on_arena else 'no'}")
    return 0 if (legal == "legal" and on_arena) else 1


# ---------- deck parsing + validation ------------------------------------

DECK_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+\(([A-Za-z0-9]+)\)\s+(\S+)\s*$")
SECTION_HEADERS = {"deck", "commander", "companion", "sideboard", "maybeboard"}


@dataclass
class DeckEntry:
    count: int
    name: str
    set_code: str
    collector: str
    section: str  # 'commander' | 'deck' | 'sideboard' | ...


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
        if e.section not in {"deck", "commander", "sideboard"}:
            continue
        c = _resolve_card(e.name)
        if not c:
            msgs.append(f"unknown card: {e.name} ({e.set_code}) {e.collector}")
            continue
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


def cmd_validate(args: argparse.Namespace) -> int:
    _warn_if_stale()
    path = Path(args.deck)
    if not path.exists():
        print(f"deck file not found: {path}", file=sys.stderr)
        return 2
    entries = parse_deck(path)
    code, msgs = validate_deck(entries, args.format.lower())
    if msgs:
        for m in msgs:
            print(f"  ✗ {m}")
        print(f"{path}: {len(msgs)} issue(s)")
    else:
        cmdrs = [e.name for e in entries if e.section == "commander"]
        total = sum(e.count for e in entries if e.section in {"deck", "commander"})
        print(
            f"{path}: ok ({total} cards, format={args.format}"
            + (f", commander={cmdrs[0]}" if cmdrs else "")
            + ")"
        )
    return code


# ---------- search (live) ------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    q = args.query
    url = f"{SCRYFALL_API}/cards/search?q={urllib.parse.quote(q)}&unique=cards"
    try:
        data = _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("no cards matched")
            return 1
        raise
    cards = data.get("data") or []
    print(f"{data.get('total_cards', len(cards))} match(es); showing {len(cards)}:")
    for c in cards[: args.limit]:
        ci = "".join(c.get("color_identity") or []) or "C"
        on_arena = "✓" if "arena" in (c.get("games") or []) else " "
        print(
            f"  [{on_arena}] {c['name']:<40} {c['set'].upper():<5} "
            f"{c.get('type_line', ''):<30} {ci}"
        )
    if data.get("has_more"):
        print(f"  ... has_more=True (rerun on Scryfall for full list)")
    return 0


# ---------- entrypoint ---------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mtg", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="refresh Scryfall bulk + rebuild index")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("card", help="show full info for a card by name")
    s.add_argument("name")
    s.set_defaults(func=cmd_card)

    s = sub.add_parser("printing", help="lookup by MTGA-style set+collector")
    s.add_argument("set")
    s.add_argument("num")
    s.set_defaults(func=cmd_printing)

    s = sub.add_parser("legal", help="check legality in an Arena format")
    s.add_argument("name")
    s.add_argument("format")
    s.set_defaults(func=cmd_legal)

    s = sub.add_parser("validate", help="validate an MTGA-export deck file")
    s.add_argument("deck")
    s.add_argument("-f", "--format", required=True)
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("search", help="live Scryfall search (one HTTP request)")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
