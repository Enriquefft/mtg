#!/usr/bin/env bash
# Run every available parser against one Arena format to build / refresh
# the corpus under data/corpus/<fmt>/. Sequential (sources share the
# Scryfall resolver and HTTP throttle is per-source anyway) and verbose
# so a failed parser is obvious.
#
# Usage:  scripts/expand-corpus.sh [format|all] [--fresh]   (default: brawl)
#         scripts/expand-corpus.sh historic
#         scripts/expand-corpus.sh all              # walk every Arena format
#         scripts/expand-corpus.sh historic --fresh # wipe meta-cache + corpus first
set -uo pipefail

# --- argv parse ---------------------------------------------------------
FRESH=0
FMT=""
for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    -*)
      echo "unknown flag: $arg" >&2
      echo "usage: $0 [format|all] [--fresh]" >&2
      exit 2
      ;;
    *)
      if [ -n "$FMT" ]; then
        echo "unexpected positional: $arg (already have FMT=$FMT)" >&2
        exit 2
      fi
      FMT="$arg"
      ;;
  esac
done
FMT="${FMT:-brawl}"

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# `all` mode: re-invoke this script once per Arena format. Sequential
# across formats AND sources because the Scryfall index pickle is
# ~500MB per process; parallelism would balloon RAM and any meta.json
# cross-write isn't a concern (each format has its own sidecar).
if [ "$FMT" = "all" ]; then
  ALL_FORMATS=(standard alchemy historic timeless pioneer brawl explorer standardbrawl)

  # --fresh in `all` mode: parent wipes everything ONCE then re-execs
  # children without --fresh so they don't redundantly re-wipe between
  # iterations (which would also delete sources written by earlier formats
  # if the meta-cache layout ever changed to share a directory).
  if [ "$FRESH" -eq 1 ]; then
    echo "==> --fresh: wiping data/meta-cache and data/corpus/<fmt> for all formats"
    rm -rf "$ROOT/data/meta-cache"
    echo "    rm -rf $ROOT/data/meta-cache"
    for f in "${ALL_FORMATS[@]}"; do
      if [ -d "$ROOT/data/corpus/$f" ]; then
        rm -rf "$ROOT/data/corpus/$f"
        echo "    rm -rf $ROOT/data/corpus/$f"
      fi
    done
  fi

  FAILED_FMTS=()
  for f in "${ALL_FORMATS[@]}"; do
    echo
    echo "######### $f #########"
    "${BASH_SOURCE[0]}" "$f" || FAILED_FMTS+=("$f")
  done
  if [ "${#FAILED_FMTS[@]}" -gt 0 ]; then
    echo "==> failed formats: ${FAILED_FMTS[*]}"
    exit 1
  fi
  exit 0
fi

MTG="$ROOT/tools/mtg"
LOG_DIR="$ROOT/data/corpus/.fetch-logs"
mkdir -p "$LOG_DIR"

# Single-format --fresh: wipe meta-cache + this format's corpus + per-
# source logs for THIS format before the source loop. .fetch-logs/ as
# a directory is preserved (cross-format diagnostic trail), but stale
# `<src>-$FMT.log` from earlier runs is pruned so the new run's logs
# stand alone.
if [ "$FRESH" -eq 1 ]; then
  echo "==> --fresh: wiping caches"
  if [ -d "$ROOT/data/meta-cache" ]; then
    rm -rf "$ROOT/data/meta-cache"
    echo "    rm -rf $ROOT/data/meta-cache"
  fi
  if [ -d "$ROOT/data/corpus/$FMT" ]; then
    rm -rf "$ROOT/data/corpus/$FMT"
    echo "    rm -rf $ROOT/data/corpus/$FMT"
  fi
  # Per-format log prune: leaves logs from other formats intact.
  shopt -s nullglob
  stale_logs=("$LOG_DIR"/*-"$FMT".log)
  shopt -u nullglob
  if [ "${#stale_logs[@]}" -gt 0 ]; then
    rm -f "${stale_logs[@]}"
    echo "    rm -f $LOG_DIR/*-$FMT.log (${#stale_logs[@]} files)"
  fi
fi

# Sources to try, in priority order. moxfield first (largest corpus, no
# throttle pain); archidekt next (user-deckbuilder, high novelty); aetherhub last
# (Cloudflare can JS-challenge bursts). untapped is the all-formats baseline.
SOURCES=(untapped moxfield archidekt aetherhub)

# Format -> sources that publish for it. Order = dedup priority (first
# source wins on near-dup match in `_write_meta_corpus`); tier/winrate-
# bearing sources (mtgazone, mtgdecks, mtggoldfish) FIRST so their signal-
# rich rows survive over signal-less duplicates from untapped/moxfield.
#
# - mtggoldfish enabled only for standard + pioneer (historic + explorer
#   pages are upstream-thin per live probe — 1 budget tile, parser
#   correctly filters; not a parser bug).
# - mtgdecks enabled only for historic (its sole supported format per
#   mtgdecks.py:81-83).
# - standardbrawl: aetherhub publishes <10 decks under /Metagame/Brawl/
#   (per aetherhub.py:64) — not worth the throttle.
case "$FMT" in
  standardbrawl)    ENABLED=(untapped moxfield archidekt) ;;
  brawl)            ENABLED=(untapped moxfield archidekt aetherhub) ;;
  standard)         ENABLED=(mtgazone mtggoldfish untapped moxfield archidekt aetherhub) ;;
  historic)         ENABLED=(mtgazone mtgdecks untapped moxfield archidekt aetherhub) ;;
  pioneer)          ENABLED=(mtgazone mtggoldfish untapped moxfield archidekt aetherhub) ;;
  explorer)         ENABLED=(mtgazone untapped moxfield archidekt aetherhub) ;;
  alchemy|timeless) ENABLED=(mtgazone untapped moxfield archidekt aetherhub) ;;
  *)                ENABLED=("${SOURCES[@]}") ;;
esac

# Drift detector: warn if the registry knows of sources for this format
# that the case-block above excludes. Prevents the script from silently
# missing a parser added in Python without a corresponding script edit.
# Failure in --list-sources is non-fatal — we don't want a transient mtg
# CLI bug to block the whole expand run.
if registry_sources=$("$MTG" fetch-meta "$FMT" --list-sources 2>/dev/null); then
  for src in $registry_sources; do
    found=0
    for enabled in "${ENABLED[@]}"; do
      if [ "$src" = "$enabled" ]; then found=1; break; fi
    done
    if [ "$found" -eq 0 ]; then
      echo "==> WARNING: registry parser '$src' supports $FMT but is not in ENABLED list" >&2
    fi
  done
fi

echo "==> expand-corpus fmt=$FMT sources=${ENABLED[*]}"
echo "==> logs: $LOG_DIR/<source>-$FMT.log"

START_TS=$(date +%s)
FAILED=()

for src in "${ENABLED[@]}"; do
  log="$LOG_DIR/${src}-${FMT}.log"
  echo
  echo "--- [$src] $(date -Iseconds) ---"

  # Per-source argv overrides. moxfield's CLI default is 300 (single-
  # shot ergonomics); a max-corpus build pass benefits from going
  # ~6x deeper, well under the 10000-deck API cap (50/page * 200 pages).
  extra_args=()
  case "$src" in
    moxfield) extra_args=(--limit 2000) ;;
  esac

  # stdbuf forces line-buffering so progress shows live; tee writes
  # the log AND streams to stdout so a hung fetch is visible.
  stdbuf -oL -eL "$MTG" fetch-meta "$FMT" --source "$src" "${extra_args[@]}" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  case "$rc" in
    0) echo "    [$src] ok" ;;
    # Exit 2 = source provably doesn't serve this format (per
    # tools/mtg.py:7062-7071). Treated as "skipped, expected" rather
    # than failure so the per-format ENABLED matrix stays robust
    # against url_for_format drift without operator action.
    2) echo "    [$src] skipped (unsupported for $FMT)" ;;
    *)
      echo "    [$src] FAILED (exit $rc) — see $log"
      FAILED+=("$src")
      ;;
  esac
done

echo
echo "==> corpus-clean $FMT"
clean_log="$LOG_DIR/corpus-clean-$FMT.log"
# Drops decks that fail _validate_for_corpus (catches both legacy
# entries pre-dating the fetch-time gate and anything that slipped
# past it). Runs BEFORE freq --rebuild so the index is built from
# the pruned corpus. corpus-clean rebuilds _freq.json itself after
# deletions; the explicit `freq --rebuild` below is kept as a belt-
# and-braces guarantee that the index reflects current on-disk state.
"$MTG" corpus-clean "$FMT" 2>&1 | tee "$clean_log"

echo
echo "==> rebuilding freq index for $FMT"
freq_log="$LOG_DIR/freq-$FMT.log"
# Stream full output to the log AND echo last 2 lines (the success
# summary) to stdout so an OK run stays terse. On failure, the log
# preserves the full traceback for diagnosis.
"$MTG" freq "$FMT" --rebuild 2>&1 | tee "$freq_log" | tail -2
freq_rc=${PIPESTATUS[0]}
if [ "$freq_rc" -ne 0 ]; then
  echo "==> freq --rebuild FAILED (exit $freq_rc) — see $freq_log"
  FAILED+=("freq")
fi

ELAPSED=$(( $(date +%s) - START_TS ))
echo
echo "==> done in ${ELAPSED}s"

# Pipeline split so a python parse failure on a corrupt meta.json shows
# up as a python traceback (not as an opaque "see recommend log" with
# only recommend's stderr inside).
recommend_log="$LOG_DIR/recommend-$FMT.log"
recommend_json="$LOG_DIR/recommend-$FMT.json"
if "$MTG" recommend --format "$FMT" --json >"$recommend_json" 2>"$recommend_log"; then
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(f'corpus_size={d[\"corpus_size\"]} buildable={d[\"buildable_count\"]}')" "$recommend_json" \
    || echo "==> recommend smoke-check: JSON parse failed (see $recommend_json)"
else
  echo "==> recommend smoke-check failed (see $recommend_log)"
fi

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "==> failed sources: ${FAILED[*]}"
  exit 1
fi
