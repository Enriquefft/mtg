#!/usr/bin/env bash
# Run every available parser against one Arena format to build / refresh
# the corpus under data/corpus/<fmt>/. Verbose so a failed parser is
# obvious; per-source logs land in data/corpus/.fetch-logs/<src>-<fmt>.log.
#
# Usage:  scripts/expand-corpus.sh [format|all] [--fresh]   (default: brawl)
#         scripts/expand-corpus.sh historic
#         scripts/expand-corpus.sh all                       # walk every Arena format
#         scripts/expand-corpus.sh historic --fresh          # wipe meta-cache + corpus first
#         PARALLEL_FORMATS=4 scripts/expand-corpus.sh all    # fan out 4 formats at a time
#         WORKERS=4 scripts/expand-corpus.sh historic        # concurrent sources within a format
#         WORKERS=4 PARALLEL_FORMATS=4 scripts/expand-corpus.sh all  # both axes
#
# Sources run inside `mtg fetch-meta-all`, which merges all source results
# into one dedup pass and writes once per format — a single in-process
# writer with no sidecar corruption surface.
#
# WORKERS=N enables Phase C cross-source HTTP parallelism: `fetch-meta-all`
# fans out up to N sources concurrently using Python threads.  Each source
# still runs sequentially internally (per-page throttle preserved); the
# concurrency is at the source level, not the page level.  N=1 (default)
# keeps the Phase B sequential path (identical semantics, cleaner traces).
# Tune N up to the number of sources for a format (≈7); higher gives no
# additional benefit.
#
# Cross-FORMAT parallelism IS independently safe (each format owns its own
# data/corpus/<fmt>/ directory with its own meta.json + _freq.json sidecar,
# so two formats fetched concurrently never touch the same sidecar).
#
# RAM ceiling: each child process loads the ~80MB Scryfall index pickle.
# PARALLEL_FORMATS=4 is a sane default for ≥8GB-RAM machines (~320MB
# for indexes + Python overhead, comfortably under). Do not auto-detect:
# the operator knows their box.
set -uo pipefail

# --- signal handling ----------------------------------------------------
# Ctrl+C / SIGTERM during a long PARALLEL_FORMATS run used to leave
# python ThreadPoolExecutor children stuck in shutdown for tens of
# seconds (Python can't kill threads, so workers run until I/O returns;
# the parent's `wait` blocks until python exits).  cmd_fetch_meta_all
# now installs a SIGINT handler that calls os._exit(130) for immediate
# python termination — but the bash parent still needs to:
#   1. forward the signal to backgrounded children + their descendants
#      (the `tee | python` pipeline inside each subshell);
#   2. NOT loop into the next format after wait returns 130;
#   3. clean up the rcdir tempfile from the PARALLEL_FORMATS branch.
# Single trap covers both code paths (PARALLEL_FORMATS=1 single-pipeline
# and PARALLEL_FORMATS>1 fan-out) and absorbs the rcdir cleanup that
# previously lived in a separate `trap ... EXIT` inside the if-branch.
RCDIR=""

_expand_corpus_cleanup() {
  # Block re-entry — second Ctrl+C should not race the first.
  trap '' INT TERM
  # `jobs -rp` lists running backgrounded job PIDs (direct children of
  # this shell only).  pkill -P <pid> cascades to grandchildren so the
  # `tee | python` pipeline inside each subshell dies along with its
  # parent subshell.
  local pids
  pids=$(jobs -rp 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "==> caught signal, terminating $(echo "$pids" | wc -w) child(ren)..." >&2
    for p in $pids; do
      pkill -TERM -P "$p" 2>/dev/null || true
      kill -TERM "$p" 2>/dev/null || true
    done
    sleep 0.3
    for p in $pids; do
      pkill -KILL -P "$p" 2>/dev/null || true
      kill -KILL "$p" 2>/dev/null || true
    done
  fi
  if [ -n "$RCDIR" ] && [ -d "$RCDIR" ]; then
    rm -rf "$RCDIR"
  fi
}

trap '_expand_corpus_cleanup; exit 130' INT TERM
trap '_expand_corpus_cleanup' EXIT

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

# `all` mode: re-invoke this script once per Arena format. Default is
# sequential across formats (PARALLEL_FORMATS=1); set PARALLEL_FORMATS=N
# to fan out N formats concurrently. Cross-format is race-free (each
# format owns its sidecars). RAM ceiling: ~80MB pickle per child.
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

  PARALLEL_FORMATS="${PARALLEL_FORMATS:-1}"
  FAILED_FMTS=()

  if [ "$PARALLEL_FORMATS" -gt 1 ]; then
    echo "==> PARALLEL_FORMATS=$PARALLEL_FORMATS — fanning out concurrently"
    echo "    NOTE: stdout from concurrent children interleaves; the per-format"
    echo "    '######### <fmt> #########' banner brackets segments, and the"
    echo "    canonical record stays in $ROOT/data/corpus/.fetch-logs/<src>-<fmt>.log."
    # FAILED_FMTS can't be appended from a subshell, so each child
    # writes its exit status to /tmp/expand-corpus-rc.<pid>/<fmt>.rc and
    # the parent collects them after `wait`.
    RCDIR=$(mktemp -d -t expand-corpus-rc.XXXXXX) || {
      echo "==> mktemp -d failed; cannot collect child exit codes" >&2
      exit 1
    }
    rcdir="$RCDIR"
    # Cleanup of $RCDIR is handled by the script-level EXIT trap
    # (see top of file) so signal-driven exits also purge the tempdir.
    for f in "${ALL_FORMATS[@]}"; do
      # Cap concurrent jobs at PARALLEL_FORMATS. `wait -n` blocks until
      # any child exits, freeing a slot.
      while [ "$(jobs -rp | wc -l)" -ge "$PARALLEL_FORMATS" ]; do
        wait -n
      done
      (
        echo
        echo "######### $f #########"
        "${BASH_SOURCE[0]}" "$f"
        echo $? > "$rcdir/$f.rc"
      ) &
    done
    wait
    for f in "${ALL_FORMATS[@]}"; do
      rc=$(cat "$rcdir/$f.rc" 2>/dev/null || echo 1)
      if [ "$rc" -ne 0 ]; then
        FAILED_FMTS+=("$f")
      fi
    done
  else
    for f in "${ALL_FORMATS[@]}"; do
      echo
      echo "######### $f #########"
      "${BASH_SOURCE[0]}" "$f" || FAILED_FMTS+=("$f")
    done
  fi

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

echo "==> expand-corpus fmt=$FMT"
echo "==> logs: $LOG_DIR/<source>-$FMT.log"

# Truncate the gate's validate log at the start of every run so it
# accumulates within ONE run (across all parsers) but doesn't grow
# unbounded across runs.  Per-source logs are managed by _fetch_one_source
# in Python (overwrite-mode open), so no bash tee needed.
: > "$LOG_DIR/validate-$FMT.log"

START_TS=$(date +%s)
FAILED=()

# `fetch-meta-all` runs every source whose url_for_format(fmt) returns
# non-None, then merges + deduplicates + writes once.
# `_FETCH_META_PARSERS` IS the source-of-truth for which sources support
# which format; the old case-block ENABLED matrix is gone.
#
# Per-source log files still land at $LOG_DIR/<src>-$FMT.log because
# _fetch_one_source opens each file in Python (overwrite mode, line-
# buffered) — same final path as the old tee pattern.
#
# WORKERS env var enables Phase C concurrent sources (--workers N flag).
# Unset or empty → default 1 = serial (Phase B semantics).
WORKERS_FLAG=()
if [ -n "${WORKERS:-}" ]; then
  WORKERS_FLAG=(--workers "$WORKERS")
fi
"$MTG" fetch-meta-all "$FMT" "${WORKERS_FLAG[@]}" 2>&1 | tee "$LOG_DIR/fetch-meta-all-$FMT.log"
rc=${PIPESTATUS[0]}
case "$rc" in
  0) ;;
  2) echo "==> no sources support $FMT (skipped)" ;;
  *) FAILED+=("fetch-meta-all"); echo "==> fetch-meta-all FAILED (exit $rc)" ;;
esac

echo
echo "==> corpus-clean $FMT"
clean_log="$LOG_DIR/corpus-clean-$FMT.log"
# Drops decks that fail _validate_for_corpus (catches both legacy entries
# pre-dating the fetch-time gate and anything that slipped past it).
# corpus-clean rebuilds _freq.json itself after deletions, so no separate
# freq --rebuild step is needed.
"$MTG" corpus-clean "$FMT" 2>&1 | tee "$clean_log"
clean_rc=${PIPESTATUS[0]}
if [ "$clean_rc" -ne 0 ]; then
  echo "==> corpus-clean FAILED (exit $clean_rc) — see $clean_log"
  FAILED+=("corpus-clean")
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
