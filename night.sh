#!/bin/sh
# Long unattended collection, restart-safe.
#
#   ./night.sh <outdir> <min-rating> [extra scrape.py run flags...]
#   ./night.sh data/pbs 2000 --card pekka,battle-ram
#
# The crawl dies on its own from time to time: Cloudflare rotates the clearance,
# or hands back a challenge that does not solve inside the timeout. Everything
# collected is already on disk and the ledger records finished players, so the
# fix is to start it again -- it resumes rather than repeats. Hence the retry
# loop, which is the whole reason to use this instead of calling scrape.py.
#
# Pass 1 walks archives to their end. After that more only arrives as these
# players keep playing, so the sweeps re-check recent pages every couple of hours.
set -u
OUT=${1:?usage: night.sh <outdir> <min-rating> [flags...]}
FLOOR=${2:?usage: night.sh <outdir> <min-rating> [flags...]}
shift 2

TRIES=0
echo "=== pass 1: floor $FLOOR -> $OUT $* ==="
until ./scrape.py run --min-rating "$FLOOR" --group 8 --out "$OUT" "$@"; do
    TRIES=$((TRIES + 1))
    echo "=== died (attempt $TRIES) at $(date '+%H:%M') -- resuming in 60s ==="
    sleep 60
done
echo "=== pass 1 complete at $(date '+%H:%M') ==="

while true; do
    echo "=== sleeping 2h ==="
    sleep 7200
    echo "=== sweep at $(date '+%H:%M') ==="
    ./scrape.py run --min-rating "$FLOOR" --group 8 --out "$OUT" --refresh --pages 3 "$@" \
        || echo "=== sweep died, retrying next cycle ==="
done
