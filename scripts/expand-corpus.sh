#!/usr/bin/env bash
# Run every available parser against one Arena format to build / refresh
# the corpus under data/corpus/<fmt>/. Verbose so a failed parser is
# obvious; per-source logs land in data/corpus/.fetch-logs/<src>-<fmt>.log.
#
# Usage:  scripts/expand-corpus.sh [format|all] [--fresh]   (default: brawl)
#         scripts/expand-corpus.sh historic
#         scripts/expand-corpus.sh all              # walk every Arena format
#         scripts/expand-corpus.sh historic --fresh # wipe meta-cache + corpus first
#
# Concurrency:
#   PARALLEL=N (env, default 1) — fan out the per-source loop across N
#     workers via xargs -P. Default 1 = current sequential behaviour.
#   CRITICAL: each worker invocation of `tools/mtg` loads the ~500MB
#     Scryfall index pickle into memory. PARALLEL=N peaks at roughly
#     N × 500MB resident; the default of 1 is intentional for low-RAM
#     operators. Bump PARALLEL only on machines with the headroom (a
#     PARALLEL=4 run on 8 enabled sources for a single format peaks
#     around 2GB before tapering as workers finish).
#   The `all` mode keeps cross-format serialization (each format
#   re-execs this script once); only intra-format gets the parallel
#   fan-out so meta.json writes stay isolated per format.
set -uo pipefail
PARALLEL="${PARALLEL:-1}"
if ! [[ "$PARALLEL" =~ ^[0-9]+$ ]] || [ "$PARALLEL" -lt 1 ]; then
  echo "PARALLEL must be a positive integer (got: $PARALLEL)" >&2
  exit 2
fi

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

echo "==> expand-corpus fmt=$FMT sources=${ENABLED[*]} (PARALLEL=$PARALLEL)"
echo "==> logs: $LOG_DIR/<source>-$FMT.log"

START_TS=$(date +%s)
FAILED=()

# Per-source argv overrides table. Kept here (vs. inside the worker so
# both PARALLEL=1 and PARALLEL>1 paths read the same source-of-truth).
# moxfield's CLI default is 300 (single-shot ergonomics); a max-corpus
# build pass benefits from going ~6x deeper, well under the 10000-deck
# API cap (50/page * 200 pages). Add per-source overrides here, NOT
# inside the worker function — keeps the extra-args layer trivially
# greppable.
extra_args_for() {
  case "$1" in
    moxfield) printf -- '--limit 2000' ;;
    *)        printf '' ;;
  esac
}

# Worker that fetches one source for the current format. Used by both
# the serial and the parallel paths (so the two can't drift apart).
# Returns the fetch-meta exit code so the caller can aggregate failures.
# stdbuf forces line-buffering so progress shows live in the log; tee
# also writes to stdout in the serial case for live visibility.
fetch_one_source() {
  local src="$1"
  local log="$LOG_DIR/${src}-${FMT}.log"
  # shellcheck disable=SC2046  # we want word splitting on the override
  local extras=($(extra_args_for "$src"))
  if [ "$PARALLEL" -eq 1 ]; then
    # Serial path: tee to stdout for live progress visibility.
    stdbuf -oL -eL "$MTG" fetch-meta "$FMT" --source "$src" "${extras[@]}" 2>&1 | tee "$log"
    return "${PIPESTATUS[0]}"
  fi
  # Parallel path: redirect entirely to the per-source log so concurrent
  # workers don't interleave on stdout. The aggregator below reads the
  # exit code from a sentinel line written to the log.
  stdbuf -oL -eL "$MTG" fetch-meta "$FMT" --source "$src" "${extras[@]}" >"$log" 2>&1
  return $?
}

if [ "$PARALLEL" -eq 1 ]; then
  # Original serial loop. Preserved exactly so PARALLEL=1 = old behaviour.
  for src in "${ENABLED[@]}"; do
    echo
    echo "--- [$src] $(date -Iseconds) ---"
    fetch_one_source "$src"
    rc=$?
    case "$rc" in
      0) echo "    [$src] ok" ;;
      # Exit 2 = source provably doesn't serve this format (per
      # tools/mtg.py:7062-7071). Treated as "skipped, expected" rather
      # than failure so the per-format ENABLED matrix stays robust
      # against url_for_format drift without operator action.
      2) echo "    [$src] skipped (unsupported for $FMT)" ;;
      *)
        echo "    [$src] FAILED (exit $rc) — see $LOG_DIR/${src}-${FMT}.log"
        FAILED+=("$src")
        ;;
    esac
  done
else
  # Parallel fan-out via xargs -P. Each worker writes its own log and
  # returns its exit code through a sentinel file in /tmp; we drain
  # the sentinels after xargs joins. Subshells inherit the script's
  # functions only when xargs invokes bash with `-c '...'` and we
  # re-export the worker; export-fn keeps the worker's body in one
  # place (single source of truth shared with the serial path).
  export -f fetch_one_source extra_args_for
  export FMT MTG LOG_DIR PARALLEL
  rc_dir="$(mktemp -d -t expand-corpus-rc-XXXXXX)"
  trap 'rm -rf "$rc_dir"' EXIT
  echo
  echo "--- parallel fan-out (workers=$PARALLEL) $(date -Iseconds) ---"
  # Note: -I implies -n 1 already, so omitting -n 1 silences the
  # "mutually exclusive" warning xargs emits when both are set.
  printf '%s\n' "${ENABLED[@]}" | xargs -P "$PARALLEL" -I {} bash -c '
    src="$1"
    rc_dir="$2"
    fetch_one_source "$src"
    echo $? > "$rc_dir/${src}.rc"
  ' _ {} "$rc_dir"
  # Drain results in the order ENABLED was declared so log output
  # matches the priority ordering the user expects.
  for src in "${ENABLED[@]}"; do
    rc=$(cat "$rc_dir/${src}.rc" 2>/dev/null || echo "127")
    case "$rc" in
      0) echo "    [$src] ok" ;;
      2) echo "    [$src] skipped (unsupported for $FMT)" ;;
      *)
        echo "    [$src] FAILED (exit $rc) — see $LOG_DIR/${src}-${FMT}.log"
        FAILED+=("$src")
        ;;
    esac
  done
fi

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
