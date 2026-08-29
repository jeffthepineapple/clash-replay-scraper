#!/bin/sh
# Overnight 2.6 collection.
#
# Pass 1 walks every archive to its end, which exhausts the 2.6 games RoyaleAPI
# currently holds (~1 day of history per player). After that the only way to get
# more is to wait for these players to play, so the loop re-walks the recent
# pages every couple of hours and appends whatever is new. --refresh re-visits
# finished players; battles already on disk are never fetched twice, so each
# sweep costs a few hundred cheap history requests and only pays for new games.
set -u
OUT=${1:-data/hog26}
FLOOR=${2:-2000}

echo "=== pass 1: full depth, floor $FLOOR -> $OUT ==="
./scrape.py run --min-rating "$FLOOR" --group 8 --out "$OUT"

while true; do
    echo "=== sleeping 2h, next sweep at $(date -v+2H '+%H:%M') ==="
    sleep 7200
    echo "=== sweep at $(date '+%H:%M') ==="
    ./scrape.py run --min-rating "$FLOOR" --group 8 --out "$OUT" --refresh --pages 3
done
