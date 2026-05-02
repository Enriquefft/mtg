"""Shared deck-parsing primitives.

Lifted out of `tools/mtg.py` so per-source parsers (untapped, mtgazone,
mtggoldfish, ...) can produce `DeckEntry` lists the rest of the CLI
already knows how to validate, write, and analyse — without each parser
re-deriving regex / section / multi-face rules. Single source of truth.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.client
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin, urlsplit

# Single source of truth for the toolkit's outbound User-Agent. Used by
# `tools/mtg.py` (Scryfall JSON) and by per-source parsers fetching
# sub-resources (e.g. mtggoldfish per-archetype pages). One constant so
# rotating identity / version is one edit.
USER_AGENT = "mtg-toolkit/0.1 (github.com/Enriquefft/mtg)"


# Transient HTTP codes that justify a single retry-with-backoff. Codes
# chosen because every observation in `data/corpus/.fetch-logs/` for
# these has been a one-shot blip — the same URL re-fetched 2-10 seconds
# later returns 200.  Specifically:
#   429 — rate limit (archidekt's `/search/decks/` under PARALLEL_FORMATS
#         load).  Honors `Retry-After` if present, capped to avoid
#         pathological multi-minute sleeps blocking the worker pool.
#   502 — Cloudflare-fronted host's origin returned 5xx briefly.
#   503 — origin overloaded / maintenance window.
#   504 — Cloudflare timed out talking to origin.
#   526 — Cloudflare can't validate origin SSL (aetherhub flapped 526s
#         in a recent run).  Always transient on the origin side.
# 403 stays opt-in (`retry_403_once`) because some sources legitimately
# 403 the toolkit's UA and only succeed via per-source workarounds.
_TRANSIENT_RETRY_CODES: frozenset[int] = frozenset({429, 502, 503, 504, 526})

# Cap on `Retry-After` honoring so a hostile or buggy origin can't park
# our worker for minutes.  60s honors legitimate minute-scale waits
# (some origins legitimately need ~45-60s after a burst) while still
# bounding worker-pool-park risk; sustained blocks beyond this surface
# as a hard fail so the operator sees the source is genuinely down.
_RETRY_AFTER_CAP_SECS: float = 60.0

# Hosts that exhibit cross-process burst-rate-limit behavior under
# `PARALLEL_FORMATS=N`. Outbound requests to these hosts are serialized
# across all Python processes via fcntl.flock on a per-host lockfile in
# `_LOCK_DIR`. archidekt.com returns bare 429 (no Retry-After) on
# concurrent `/search/decks/` GETs from PARALLEL_FORMATS=8; verified
# 6/8 → 429 via concurrent curl probe. Other hosts (moxfield, untapped,
# aetherhub, mtgazone, mtggoldfish, mtgdecks) have not exhibited this
# pattern in `data/corpus/.fetch-logs/`. Add evidence-supported entries
# only — over-broad locking trades wall-clock for nothing on hosts that
# already cope with N=8 concurrency.
_CROSS_PROCESS_LOCK_HOSTS: frozenset[str] = frozenset({"archidekt.com"})

# Pause inside the lock window AFTER the HTTP response is read but BEFORE
# releasing the flock, so the next process to acquire doesn't immediately
# re-fire and re-trip the host's burst counter. archidekt's empirical
# block window is ≥30s; 1s spacing → 1 req/s sustained across 8 procs,
# well below archidekt's per-IP threshold while still keeping a
# `--fresh all` build under ~2 min of archidekt time total.
_CROSS_PROCESS_COOLDOWN_SECS: float = 1.0

# Per-host lockfile directory. Reuses the per-source log dir already
# created by `tools/mtg.py:_fetch_one_source` during corpus builds, so
# no new dir convention. Created lazily and idempotently on first lock
# acquisition (no tempdir fallback — cross-process semantics require
# every process to agree on the path).
_LOCK_DIR: Path = Path("data/corpus/.fetch-logs")

# Backoff schedule for HTTP 429 when `heavy_429_retry=True` AND the
# response carries no `Retry-After` header (archidekt's case). Three
# retries with ±20% jitter; total worst-case wait ~65s before hard
# fail. Calibrated against archidekt's observed ≥30s block window:
# attempt 0 fires after 4-6s (often within the block, may still 429),
# attempt 1 after another 12-18s (block likely cleared if no sibling
# burst), attempt 2 after 36-54s (clears all but sustained outages).
_HEAVY_429_SCHEDULE_SECS: tuple[float, ...] = (5.0, 15.0, 45.0)
_HEAVY_429_JITTER_FRAC: float = 0.20


@contextlib.contextmanager
def _host_cross_process_lock(
    hostname: str | None, *, enabled: bool,
) -> "Iterator[None]":
    """fcntl.flock-based cross-process serialization for fragile hosts.

    No-op when `enabled` is False OR the hostname isn't in
    `_CROSS_PROCESS_LOCK_HOSTS`. For registered hosts with the flag on:
    acquires `LOCK_EX` on `_LOCK_DIR/.host-<hostname>.lock`, yields,
    then sleeps `_CROSS_PROCESS_COOLDOWN_SECS` BEFORE releasing so the
    next process inherits a quiet host. Concurrent-curl probe against
    archidekt confirmed 200ms cooldown is too tight (next proc
    re-trips); 1s spaces 8-proc fan-out cleanly.

    The `enabled` flag is the call-site opt-in. Search-page callers
    (`tools/mtg.py:_fetch_meta_page`) set it True because PARALLEL_FORMATS
    fan-out hammers `/search/decks/`. Per-archetype callers leave it
    False because (a) those endpoints have not exhibited 429 burst
    behavior, and (b) flock-per-deck × 50 decks × 1s cooldown × 8 procs
    would compound into minutes of pure cooldown wait per `--fresh all`
    build. Belt-and-suspenders: the registry filter still applies, so
    flagging True for an unregistered host is also a no-op — keeps
    serialization narrowly scoped to evidence-supported cases.

    Linux-only (fcntl). The dev shell and CI are Linux per
    flake.nix / CLAUDE.md.
    """
    if (
        not enabled
        or hostname is None
        or hostname not in _CROSS_PROCESS_LOCK_HOSTS
    ):
        yield
        return
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _LOCK_DIR / f".host-{hostname}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            time.sleep(_CROSS_PROCESS_COOLDOWN_SECS)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _retry_sleep_for(exc: urllib.error.HTTPError, default: float) -> float:
    """Sleep duration for a transient retry. Honors `Retry-After` header.

    `Retry-After` per RFC 7231 may be either delta-seconds (an int) or
    an HTTP-date.  We support delta-seconds (the common form on 429s);
    HTTP-date variants fall through to `default` rather than parse a
    full RFC date format inline.  Cap at `_RETRY_AFTER_CAP_SECS`.
    """
    hdr = exc.headers.get("Retry-After") if exc.headers else None
    if hdr:
        try:
            return min(float(hdr), _RETRY_AFTER_CAP_SECS)
        except (ValueError, TypeError):
            pass
    return default


def http_get_text(
    url: str,
    *,
    accept: str = "text/html,application/xhtml+xml",
    retry_403_once: bool = False,
    retry_sleep_secs: float = 2.0,
    referer: str | None = None,
    user_agent: str | None = None,
    extra_headers: dict[str, str] | None = None,
    heavy_429_retry: bool = False,
    cross_process_lock: bool = False,
) -> str:
    """Fetch `url` as text using the shared User-Agent.

    Stdlib-only thin wrapper. Exists so per-source parsers that need
    sub-resource HTTP (mtggoldfish per-archetype pages) don't import
    back into `tools/mtg.py` (circular) and don't grow a parallel HTTP
    stack with a different UA / timeout policy.

    Transient-error retry (legacy path, default): any response whose
    status is in `_TRANSIENT_RETRY_CODES` (429/502/503/504/526) gets
    one retry after a backoff.  429 honors `Retry-After`; others use
    `retry_sleep_secs`.  A second failure on the same code re-raises —
    sustained 5xx / 429 across two attempts is a real outage, not a blip.

    `retry_403_once`: per `docs/sources.md` mtggoldfish "occasionally
    403s; retry once". When True, a single retry with `retry_sleep_secs`
    delay is attempted on the first 403; any second 403 re-raises.

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

    `heavy_429_retry`: opt-in stronger 429 backoff for hosts that send
    bare 429 (no `Retry-After`) under cross-process burst load. When
    True AND the response is 429 AND no `Retry-After` is present:
    `_HEAVY_429_SCHEDULE_SECS` (5/15/45s) with ±20% jitter, up to 3
    retries (4 attempts total). 429 with `Retry-After`, and all other
    transient codes, fall through to the legacy single-retry path
    above. Off by default — the heavy schedule is too costly for hosts
    that 429 per-deck (would compound 50× per format); enable only at
    search-page call sites where one slow retry is worth a successful
    fetch.

    `cross_process_lock`: opt-in `fcntl.flock`-based cross-process
    serialization for hosts in `_CROSS_PROCESS_LOCK_HOSTS`. Off by
    default. Enable only at search-page call sites — per-archetype
    fetches don't 429 in concurrent observation and N×50 lock cycles
    with cooldown would dominate wall-clock. Sleep-during-retry
    happens OUTSIDE the lock window (the lock scope is one
    `_do_http_get` call) so sibling processes can attempt during a
    retry sleep. Unregistered hosts no-op even with the flag set.
    """
    legacy_retried = False
    heavy_429_attempts = 0
    while True:
        try:
            return _do_http_get(
                url, accept=accept, referer=referer,
                user_agent=user_agent, extra_headers=extra_headers,
                cross_process_lock=cross_process_lock,
            )
        except urllib.error.HTTPError as e:
            has_retry_after = bool(
                e.headers and e.headers.get("Retry-After")
            )

            if (
                e.code == 429
                and heavy_429_retry
                and not has_retry_after
            ):
                if heavy_429_attempts >= len(_HEAVY_429_SCHEDULE_SECS):
                    raise
                base = _HEAVY_429_SCHEDULE_SECS[heavy_429_attempts]
                jitter = random.uniform(
                    1.0 - _HEAVY_429_JITTER_FRAC,
                    1.0 + _HEAVY_429_JITTER_FRAC,
                )
                time.sleep(base * jitter)
                heavy_429_attempts += 1
                continue

            retry_this = (
                e.code in _TRANSIENT_RETRY_CODES
                or (retry_403_once and e.code == 403)
            )
            if not retry_this or legacy_retried:
                raise
            sleep_for = (
                _retry_sleep_for(e, retry_sleep_secs)
                if e.code in _TRANSIENT_RETRY_CODES
                else retry_sleep_secs
            )
            time.sleep(sleep_for)
            legacy_retried = True


# Per-host HTTP connection pool. `urllib.request.urlopen` builds a
# fresh HTTPSConnection (TCP+TLS handshake, ~150-300ms RTT-dependent)
# per call; for a 2000-deck moxfield pull at ~500 calls (search + per-
# deck) that adds 1-10 minutes of pure handshake. We hold one open
# connection per `(scheme, host, port)` keyed entry and reuse it across
# calls — http.client speaks HTTP/1.1 keep-alive natively.
#
# Stdlib only. urllib3 / requests are not importable in the dev shell.
#
# Thread safety: PER-HOST locks. `http.client.HTTPSConnection` is not
# thread-safe (request/getresponse/read must run as one transaction),
# so a lock has to span the entire cycle. A SINGLE global lock would
# also serialise threads on DIFFERENT hosts, defeating Phase C
# parallelism — moxfield's slow page-walk would block aetherhub from
# making any progress. We key locks by `(scheme, host, port)` so each
# host serialises independently; threads targeting distinct hosts run
# concurrently. The dict that maps key → lock is itself protected by
# `_POOL_REGISTRY_LOCK`, held only briefly during dict access (lock
# lookup + create-if-missing); the slow HTTP I/O happens under the
# per-host lock alone.
#
# Pool growth is bounded by host count (~7 sources). No eviction.
_POOL: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
_POOL_LOCKS: dict[tuple[str, str, int], threading.Lock] = {}
_POOL_REGISTRY_LOCK = threading.Lock()


def _get_host_lock(
    scheme: str, host: str, port: int,
) -> threading.Lock:
    """Return the per-host `(scheme, host, port)` lock, creating if missing.

    Held briefly under `_POOL_REGISTRY_LOCK` for the dict access only;
    the returned lock is then used by the caller to serialise the actual
    HTTP cycle for THAT host. New hosts add ~1 lock to `_POOL_LOCKS` —
    no eviction; bounded by source count.
    """
    key = (scheme, host, port)
    with _POOL_REGISTRY_LOCK:
        lock = _POOL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _POOL_LOCKS[key] = lock
    return lock


def _get_conn(
    scheme: str, host: str, port: int,
) -> http.client.HTTPConnection:
    """Return a pooled HTTP(S)Connection for `(scheme, host, port)`.

    Caller must hold the per-host lock from `_get_host_lock`. `_POOL`
    is read/mutated only under that lock for THAT key; distinct hosts
    have distinct locks so cross-host concurrent access is safe (each
    operates on a different dict slot). Creates a fresh connection on
    first use; http.client lazily opens the socket on `request()`, so
    cache-miss cost here is just object allocation.
    """
    key = (scheme, host, port)
    conn = _POOL.get(key)
    if conn is None:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=120)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=120)
        _POOL[key] = conn
    return conn


# Cap on `_do_http_get` redirect chain length. urllib.request defaults to
# 10; we pick 5 because every legitimate redirect we've seen on these
# hosts terminates in <=2 (archidekt: trailing-slash strip 308; aetherhub
# / mtggoldfish: occasional Cloudflare 302 → /__cf_chl_jschl_tk__).  A
# tighter cap surfaces routing bugs faster.
_MAX_REDIRECTS = 5


def _do_one_hop(
    url: str,
    *,
    accept: str,
    referer: str | None,
    user_agent: str | None,
    extra_headers: dict[str, str] | None,
) -> tuple[int, str, list[tuple[str, str]], bytes]:
    """Execute one HTTP GET via the pool. Returns `(status, reason, headers, raw)`.

    Does not raise on non-200 — the caller (`_do_http_get`) inspects status
    and either follows a redirect or wraps the response in `HTTPError`.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    host = parts.hostname
    if host is None:
        raise ValueError(f"_do_http_get: url has no hostname: {url!r}")
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    headers = {"User-Agent": user_agent or USER_AGENT, "Accept": accept}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)

    key = (scheme, host, port)
    host_lock = _get_host_lock(scheme, host, port)

    # Connection-died fallback: HTTP keep-alive idle-times-out after
    # the server's keep-alive window (typically 5-30s), and a previously-
    # pooled connection may be closed when we next try to use it. The
    # request raises RemoteDisconnected / BadStatusLine / ConnectionError
    # depending on which point of the cycle the FIN arrives. Retry once
    # with a freshly-allocated connection — anything beyond that is a
    # real network problem. We then re-raise wrapped in `URLError` to
    # mirror `urllib.request.urlopen`'s contract: callers across the
    # toolkit catch `(HTTPError, URLError)` to treat transport failure
    # as "drop this archetype/deck, continue the run" — bare `OSError`
    # / `ConnectionRefusedError` would leak past those handlers and
    # crash the per-format process.
    with host_lock:
        conn = _get_conn(scheme, host, port)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            reason = resp.reason
            resp_headers = resp.getheaders()
            will_close = resp.will_close
        except (
            http.client.RemoteDisconnected,
            http.client.BadStatusLine,
            ConnectionError,
            OSError,
        ):
            try:
                conn.close()
            except Exception:
                pass
            _POOL.pop(key, None)
            try:
                conn = _get_conn(scheme, host, port)
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                reason = resp.reason
                resp_headers = resp.getheaders()
                will_close = resp.will_close
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                ConnectionError,
                OSError,
            ) as e:
                try:
                    conn.close()
                except Exception:
                    pass
                _POOL.pop(key, None)
                raise urllib.error.URLError(
                    f"transport failure for {url!r}: {e!r}"
                ) from e

        # `Connection: close` (HTTP/1.0 default, or sent by Cloudflare-
        # fronted hosts on some responses) means the server has already
        # closed the socket after this read. Returning it to the pool
        # would force the next call to discover the FIN via
        # RemoteDisconnected and pay the retry-with-fresh-conn cost.
        # Evict proactively instead. http.client sets `will_close` based
        # on the `Connection` header AND HTTP version, so this is the
        # canonical signal — no header parsing needed.
        if will_close:
            try:
                conn.close()
            except Exception:
                pass
            _POOL.pop(key, None)

    return status, reason, resp_headers, raw


def pooled_get(
    url: str,
    *,
    accept: str = "application/json",
    user_agent: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """GET via the keep-alive pool. Return `(status, body)` on 2xx; raise on non-2xx.

    Lower-level than `http_get_text`: no redirect-following, no retry, no
    redirect-spanning cross-process lock. Use when the caller must
    inspect non-200 success statuses (e.g. untapped's analytics API
    pattern of 202 + empty body while a query runs server-side) and owns
    the retry/poll loop itself. Non-2xx still raises `HTTPError` so
    existing per-source `except urllib.error.HTTPError` blocks behave
    identically. Inherits the per-host pool lock + connection-died
    fallback from `_do_one_hop`.
    """
    status, reason, headers, raw = _do_one_hop(
        url,
        accept=accept,
        referer=None,
        user_agent=user_agent,
        extra_headers=extra_headers,
    )
    if not (200 <= status < 300):
        hdrs = http.client.HTTPMessage()
        for k, v in headers:
            hdrs[k] = v
        raise urllib.error.HTTPError(url, status, reason, hdrs, None)
    return status, raw


def _do_http_get(
    url: str,
    *,
    accept: str,
    referer: str | None = None,
    user_agent: str | None = None,
    extra_headers: dict[str, str] | None = None,
    cross_process_lock: bool = False,
) -> str:
    """GET `url`, following HTTP redirects up to `_MAX_REDIRECTS` hops.

    `urllib.request.urlopen` followed 3xx automatically; raw `http.client`
    does not.  archidekt 308s `/search/decks/` -> `/search/decks` and
    Cloudflare-fronted hosts occasionally 302; without this loop the
    parser sees a spurious HTTPError on the redirect status code.

    `Referer` is dropped on cross-host hops to mirror urllib's behaviour
    (the original referer becomes meaningless once we've left the origin
    site, and some hosts treat a stale referer as anti-CSRF noise).

    Cross-process serialization: when `cross_process_lock=True` AND the
    initial hostname is in `_CROSS_PROCESS_LOCK_HOSTS`, the entire
    redirect-following request runs under a per-host `fcntl.flock`.
    Otherwise no-op. Locking the redirect loop (vs. each hop) keeps the
    308-then-200 sequence atomic so sibling processes can't interleave
    between the redirect and the actual fetch. Cross-host redirects
    (none currently observed in the registered hosts) proceed under the
    initial host's lock — the registry stays single-host until evidence
    demands otherwise to avoid lock-ordering deadlock risk if two
    registered hosts ever redirected to each other.
    """
    seen: set[str] = set()
    current_url = url
    current_referer = referer
    initial_hostname = urlsplit(url).hostname

    with _host_cross_process_lock(
        initial_hostname, enabled=cross_process_lock,
    ):
        for _hop in range(_MAX_REDIRECTS + 1):
            if current_url in seen:
                raise urllib.error.URLError(
                    f"redirect loop detected at {current_url!r}"
                )
            seen.add(current_url)

            status, reason, resp_headers, raw = _do_one_hop(
                current_url,
                accept=accept,
                referer=current_referer,
                user_agent=user_agent,
                extra_headers=extra_headers,
            )

            if status in (301, 302, 303, 307, 308):
                location = None
                for k, v in resp_headers:
                    if k.lower() == "location":
                        location = v
                        break
                if not location:
                    # 3xx without Location is a server bug; fall through
                    # to HTTPError so the caller sees the real status code.
                    break
                next_url = urljoin(current_url, location)
                # Drop Referer on cross-origin hops (urllib parity).
                prev_host = urlsplit(current_url).hostname
                next_host = urlsplit(next_url).hostname
                if prev_host != next_host:
                    current_referer = None
                current_url = next_url
                continue

            if status != 200:
                # Mimic urllib.error.HTTPError exactly: callers
                # (http_get_text retry-403-once, parsers' urllib.error.HTTPError
                # except-clauses) check `.code` and that contract is the public
                # surface of this function. Headers wrapped via http.client's
                # HTTPMessage so `.headers.get(...)` works downstream.
                hdrs = http.client.HTTPMessage()
                for k, v in resp_headers:
                    hdrs[k] = v
                raise urllib.error.HTTPError(
                    current_url, status, reason, hdrs, None,
                )

            return raw.decode("utf-8", errors="replace")

    # Loop exhausted without a terminal response.
    raise urllib.error.URLError(
        f"too many redirects (>{_MAX_REDIRECTS}) starting at {url!r}"
    )

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
    `variant_count` total near-duplicate copies of this archetype seen
               in the fetch (including self). 1 = unique. Set by
               `dedup_decks` near-dup clustering pass.
    `variants` lightweight back-pointers to collapsed near-duplicates.
               Each entry: `{slug, source, url}`. Empty for unclustered
               decks. Surfaced in the sidecar for traceability.
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
    # Near-duplicate clustering. Populated by the second pass in
    # `dedup_decks` (Jaccard ≥ 0.85). Default 1 / [] for unclustered.
    variant_count: int = 1
    variants: list[dict] = field(default_factory=list)


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


_BASIC_LANDS: frozenset[str] = frozenset({
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
})

# Near-dup threshold: decks sharing ≥ this fraction of their combined
# non-basic card-slot multiset are considered the same archetype.
_NEAR_DUP_JACCARD_THRESHOLD: float = 0.85


def _cards_multiset(deck: ParsedDeck) -> dict[str, int]:
    """Sorted (name → count) multiset used for near-dup Jaccard similarity.

    Same scope as `cards_hash`: main-deck + commander + companion, basics
    excluded. Returns an empty dict for skeletal / corrupt decks (zero
    comparable entries) — callers treat those as non-clusterable.
    """
    pairs: dict[str, int] = {}
    for e in deck.entries:
        if e.section not in {"deck", "commander", "companion"}:
            continue
        pairs[e.name] = pairs.get(e.name, 0) + e.count
    for basic in _BASIC_LANDS:
        pairs.pop(basic, None)
    return pairs


def _jaccard_multiset(a: dict[str, int], b: dict[str, int]) -> float:
    """Jaccard similarity over two card-count multisets.

    Treats each copy as a distinct element (4x Counterspell contributes
    4 to the intersection when both decks run 4; min(4,3)=3 when one
    runs 3). Returns 0.0 when both sets are empty.

    J(A,B) = |A ∩ B| / |A ∪ B|
    For multisets: intersection = Σ min(a_i, b_i),
                   union        = Σ max(a_i, b_i).
    """
    if not a and not b:
        return 0.0
    inter = 0
    union = 0
    keys = set(a) | set(b)
    for k in keys:
        av = a.get(k, 0)
        bv = b.get(k, 0)
        inter += min(av, bv)
        union += max(av, bv)
    return inter / union if union else 0.0


def _cluster_near_dups(
    decks: list[ParsedDeck],
) -> tuple[list[ParsedDeck], list[ParsedDeck]]:
    """Greedy single-linkage clustering over Jaccard multiset similarity.

    Two decks belong to the same cluster if their card-multiset Jaccard
    similarity is ≥ `_NEAR_DUP_JACCARD_THRESHOLD` (0.85). Within each
    cluster, the deck with the highest SOURCE_PRIORITY (lowest rank
    index) is the canonical winner; ties broken by winrate * sample
    descending (more evidence first), then by slug ascending for
    determinism.

    Algorithm:
      1. Sort all decks by priority key (source rank asc, then
         -winrate*sample desc, then slug asc) so the best candidate
         comes first.
      2. Walk sorted list; for each unclaimed deck, start a new cluster.
         Scan all later unclaimed decks — if Jaccard(cluster_rep, other)
         ≥ threshold, absorb other into the cluster.
      3. Cluster representative keeps its `ParsedDeck` unchanged except
         that `variant_count` and `variants` are populated.

    O(n²) over card-set comparisons. Acceptable for n < 2000 per format.

    Returns `(winners, near_dup_dropped)`.
    """
    if len(decks) <= 1:
        return list(decks), []

    def _sort_key(d: ParsedDeck) -> tuple:
        # Lower source rank = higher priority = sorts first.
        src = _source_rank(d.source)
        # Higher winrate×sample = more evidence = sorts first → negate.
        wr = d.winrate if d.winrate is not None else 0.0
        samp = d.sample if d.sample is not None else 0
        return (src, -(wr * samp), d.slug)

    ordered = sorted(decks, key=_sort_key)

    # Precompute multisets once — each deck pays the iteration cost once
    # rather than once per comparison pair.
    multisets: list[dict[str, int]] = [_cards_multiset(d) for d in ordered]

    claimed = [False] * len(ordered)
    winners: list[ParsedDeck] = []
    dropped: list[ParsedDeck] = []

    for i, rep in enumerate(ordered):
        if claimed[i]:
            continue
        claimed[i] = True
        rep_ms = multisets[i]

        # Skip decks that are effectively empty (corrupt / stub decks
        # that survived stub-filter); they can't form meaningful clusters.
        if not rep_ms:
            winners.append(rep)
            continue

        cluster_variants: list[dict] = []

        for j in range(i + 1, len(ordered)):
            if claimed[j]:
                continue
            other_ms = multisets[j]
            if not other_ms:
                continue
            if _jaccard_multiset(rep_ms, other_ms) >= _NEAR_DUP_JACCARD_THRESHOLD:
                claimed[j] = True
                other = ordered[j]
                cluster_variants.append({
                    "slug": other.slug,
                    "source": other.source,
                    "url": other.url,
                })
                dropped.append(other)

        if cluster_variants:
            rep.variant_count = 1 + len(cluster_variants)
            rep.variants = cluster_variants

        winners.append(rep)

    return winners, dropped


def dedup_decks(
    decks: list[ParsedDeck],
    *,
    existing_hashes: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[ParsedDeck], list[ParsedDeck], dict[str, str]]:
    """Cross-source dedup by exact `cards_hash` then near-dup clustering.

    Pass 1 — Exact dedup (unchanged):
      Within `decks`, when two entries share a hash, keep the one whose
      source has higher SOURCE_PRIORITY (lower index). The loser's `url`
      is appended to the winner's `also_seen_at`.

    `existing_hashes` (optional) maps `cards_hash → (source, slug)` for
    decks already on disk in the same corpus dir. A fresh deck colliding
    with an existing on-disk entry:
      * loses (gets dropped, existing stays) if existing has higher priority;
      * wins (kept, existing's slug returned for caller to unlink) otherwise.

    Pass 2 — Near-dup clustering (new):
      Greedy single-linkage Jaccard ≥ 0.85 over the non-basic card
      multiset. Decks that differ by 1-2 cards (same archetype uploaded
      multiple times with minor tweaks) collapse to one canonical
      representative. The winner's `variant_count` and `variants` fields
      are populated. Near-dup losers are appended to `dropped_fresh`.

    Returns `(kept, dropped_fresh, eviction_map)`:
      * `kept` — fresh decks to write to disk (after both passes);
      * `dropped_fresh` — fresh decks collapsed away (exact losers +
        near-dup losers);
      * `eviction_map` — `cards_hash -> on-disk slug` for every disk
        deck a fresh winner wants to replace. Caller filters by which
        winners actually survived a post-dedup cap, then unlinks the
        survivors' eviction targets. Returning the hash (not a flat
        list of slugs) lets the caller correlate evictions with winners
        without re-running the priority comparison.
    """
    by_hash: dict[str, ParsedDeck] = {}
    dropped: list[ParsedDeck] = []
    eviction_map: dict[str, str] = {}
    existing_hashes = existing_hashes or {}

    for deck in decks:
        h = cards_hash(deck)
        if not h:
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
                dropped.append(deck)
                continue
            eviction_map[h] = prior_slug

        by_hash[h] = deck

    # Pass 2: near-dup clustering (Jaccard ≥ 0.85) over exact-dedup
    # survivors. Runs unconditionally — near-dup pollution is wrong,
    # not a tuning knob (per CLAUDE.md §"Zero workarounds").
    exact_survivors = list(by_hash.values())
    clustered_winners, near_dup_dropped = _cluster_near_dups(exact_survivors)
    dropped.extend(near_dup_dropped)

    return clustered_winners, dropped, eviction_map
