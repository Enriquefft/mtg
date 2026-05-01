#!/usr/bin/env bash
# Run every available parser against one Arena format to build / refresh
# the corpus under data/corpus/<fmt>/. Sequential (sources share the
# Scryfall resolver and HTTP throttle is per-source anyway) and verbose
# so a failed parser is obvious.
#
# Usage:  scripts/expand-corpus.sh [format|all]   (default: brawl)
#         scripts/expand-corpus.sh historic
#         scripts/expand-corpus.sh all     # walk every Arena format sequentially
set -uo pipefail

FMT="${1:-brawl}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# `all` mode: re-invoke this script once per Arena format. Sequential
# across formats AND sources because the Scryfall index pickle is
# ~500MB per process; parallelism would balloon RAM and any meta.json
# cross-write isn't a concern (each format has its own sidecar).
if [ "$FMT" = "all" ]; then
  ALL_FORMATS=(standard alchemy historic timeless pioneer brawl)
  rc=0
  for f in "${ALL_FORMATS[@]}"; do
    echo
    echo "######### $f #########"
    "${BASH_SOURCE[0]}" "$f" || rc=$?
  done
  exit "$rc"
fi

MTG="$ROOT/tools/mtg"
LOG_DIR="$ROOT/data/corpus/.fetch-logs"
mkdir -p "$LOG_DIR"

# Sources to try, in priority order. moxfield first (largest corpus, no
# throttle pain); aetherhub last (Cloudflare can JS-challenge bursts).
# untapped is the all-formats baseline.
SOURCES=(untapped moxfield aetherhub)

# Format -> sources that publish for it. Edits here keep the script
# from wasting wall-clock on (source, format) pairs that hard-fail at
# the parser layer.
case "$FMT" in
  # standardbrawl: aetherhub publishes <10 decks under /Metagame/Brawl/
  # (per aetherhub.py:64) — not worth the throttle.
  standardbrawl)              ENABLED="untapped moxfield" ;;
  brawl)                      ENABLED="untapped moxfield aetherhub" ;;
  standard|alchemy|historic)  ENABLED="untapped moxfield aetherhub" ;;
  timeless|pioneer|explorer)  ENABLED="untapped moxfield aetherhub" ;;
  *)                          ENABLED="${SOURCES[*]}" ;;
esac

echo "==> expand-corpus fmt=$FMT sources=$ENABLED"
echo "==> logs: $LOG_DIR/<source>-$FMT.log"

START_TS=$(date +%s)
FAILED=()

for src in $ENABLED; do
  log="$LOG_DIR/${src}-${FMT}.log"
  echo
  echo "--- [$src] $(date -Iseconds) ---"
  # stdbuf forces line-buffering so progress shows live; tee writes
  # the log AND streams to stdout so a hung fetch is visible.
  if stdbuf -oL -eL "$MTG" fetch-meta "$FMT" --source "$src" 2>&1 | tee "$log"; then
    echo "    [$src] ok"
  else
    rc=${PIPESTATUS[0]}
    echo "    [$src] FAILED (exit $rc) — see $log"
    FAILED+=("$src")
  fi
done

echo
echo "==> rebuilding freq index for $FMT"
"$MTG" freq "$FMT" --rebuild 2>&1 | tail -2

ELAPSED=$(( $(date +%s) - START_TS ))
echo
echo "==> done in ${ELAPSED}s"
"$MTG" recommend --format "$FMT" --json 2>/dev/null \
  | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(f'corpus_size={d[\"corpus_size\"]} buildable={d[\"buildable_count\"]}')" \
  || echo "==> recommend smoke-check failed (run manually)"

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "==> failed sources: ${FAILED[*]}"
  exit 1
fi
