#!/bin/sh
# Overnight 2.6 collection, restart-safe.
#
# The crawl can die on its own: Cloudflare rotates the clearance and a bad
# enough 403 storm ends the process. Everything it collected is already on
# disk and the ledger records finished players, so the fix is simply to start
# it again -- it resumes rather than repeating. Hence the retry loop.
#
# Pass 1 walks archives to their end. Once that completes there is nothing
# further to walk, and more 2.6 only appears as these players keep playing,
# so the sweeps re-check recent pages every couple of hours.
set -u
OUT=${1:-data/hog26}
FLOOR=${2:-2000}
TRIES=0

echo "=== pass 1: full depth, floor $FLOOR -> $OUT ==="
until ./scrape.py run --min-rating "$FLOOR" --group 8 --out "$OUT"; do
    TRIES=$((TRIES + 1))
    echo "=== pass 1 died (attempt $TRIES) at $(date '+%H:%M') -- resuming in 60s ==="
    sleep 60
done
echo "=== pass 1 complete at $(date '+%H:%M') ==="

while true; do
    echo "=== sleeping 2h ==="
    sleep 7200
    echo "=== sweep at $(date '+%H:%M') ==="
    ./scrape.py run --min-rating "$FLOOR" --group 8 --out "$OUT" --refresh --pages 3 || \
        echo "=== sweep died, will try again next cycle ==="
done
