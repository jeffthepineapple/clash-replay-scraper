# Running the scraper on GCP VMs

Repo: https://github.com/jeffthepineapple/clash-replay-scraper
**Branch: `robust-crawl`** (not `main` — see §6).

## The goal

Pull as many distinct ranked battles as possible, with full card-by-card
timelines and placement coordinates. Never the same match twice.

Work is organised archetype by archetype — Golem, then PEKKA Bridge Spam, then
Miner, Goblin Drill, Royal Hogs, X-Bow — purely so progress is legible: one
archetype completes, then the next. There's no technical reason to interleave
them, and doing so would make "are we done with Golem?" unanswerable.

So the shape that fits: **all 8 VMs on one archetype at a time**, each taking a
different shard, finishing that archetype in hours, then all moving to the
next. Not one archetype per VM.

Numbers below are measured from a 45.5-hour single-IP crawl unless marked
otherwise. Two questions I could not answer from evidence; both are go/no-go
and are flagged as untested rather than guessed.

---

## 1. Is the 429 ceiling per source IP? Is 8 IPs a linear 8×?

**Per IP: almost certainly. Linear 8×: probably not, and the login cookie is why.**

Two kinds of request:

| Request | Auth | Share of traffic |
|---|---|---|
| Deck boards, player battle history | **anonymous** — no session cookie | ~1 per 10 battles |
| `/data/replay` | **carries the account cookie** | ~1 per kept battle |

Public pages carry no account identity, so they're bound to IP alone and should
scale cleanly across 8 machines.

`/data/replay` is the risk. It's the bulk of requests and every one carries the
same `__royaleapi_session_v2`. If RoyaleAPI limits or flags per account —
untested — then eight machines on one account is the real ceiling, and that
pattern is more likely to get a session invalidated than throttled.

**Evidence held:** every run so far was single-IP. The AIMD limiter converged to
1.2–1.9 req/s on all of them regardless of archetype or time of day, consistent
with a stable server-side ceiling. 429 counts: 1,637 over 45h (PEKKA), 233 over
~26h (Golem).

**Evidence not held:** any multi-IP run; any test of one session from several
IPs concurrently.

**Do this:** bring up **two** VMs, run one hour, compare combined throughput to
the 712 battles/hour single-IP baseline. ~1,400 means linear — go to eight.
~712, or 403s and login redirects, means the account is the ceiling and you need
separate RoyaleAPI logins per machine or per pair.

---

## 2. Headless viability — does Xvfb work?

**Unknown, untested, and the most likely thing to kill the plan.**

Known for certain:

- Headless Chromium **does not** clear the Cloudflare challenge. Hence
  `headless=False` in the code.
- `royale/transport.py` refuses to start on Linux without `DISPLAY` or
  `WAYLAND_DISPLAY`. `xvfb-run` sets `DISPLAY`, so it passes that check.
- The README recommends `xvfb-run ./scrape.py` for this case.

Not known: whether the challenge actually *solves* under Xvfb. Passing the
environment check is not passing the challenge — Cloudflare fingerprints far
more than the presence of a display. The README's claim is untested here.

Second risk: **datacenter IPs.** GCP ranges are well known to Cloudflare and
routinely draw harder challenges than residential ones. Working on a laptop
does not imply working from `us-west1`.

**Test this first, on one VM, before anything else:**

```bash
sudo apt-get update && sudo apt-get install -y xvfb python3-pip git
git clone https://github.com/jeffthepineapple/clash-replay-scraper.git
cd clash-replay-scraper && git checkout robust-crawl
pip install -r requirements.txt && playwright install --with-deps chromium
export ROYALEAPI_SESSION='<cookie value — see §3>'
xvfb-run -a ./scrape.py selftest
```

`selftest` runs the whole pipeline in about a minute. `ok chrome ... N card
plays` means clear. A hang, or `AuthError: Cloudflare challenge never cleared`,
means the idea is dead in this form. Fallbacks: residential/mobile proxies in
front of the VMs; machines with real desktop sessions; or keep the crawl on the
laptop and use the VMs for engine conversion only.

---

## 3. The login cookie on a VM

**Solved — set `ROYALEAPI_SESSION`** (commit `0642042`).

Previously the cookie could only be read from a local browser's jar, which a
server hasn't got. Now:

```bash
export ROYALEAPI_SESSION='<value of __royaleapi_session_v2>'
```

Checked before the browser jars and wins outright. To obtain it: log into
royaleapi.com in a desktop browser → DevTools → Application → Cookies →
`https://royaleapi.com` → copy `__royaleapi_session_v2`.

**Reusable across 8 machines?** Mechanically yes, it's just a cookie. Whether
RoyaleAPI tolerates it is the open question in §1. One session from eight
datacenter IPs at once is an unusual pattern; don't assume it's safe.

**If the session dies mid-run** the crawl detects it: three consecutive players
whose every replay fails stops the run with a clear message rather than burning
hours writing nothing. Public collection would keep working, so the failure
mode is battle rows with no timelines.

**Safer if available:** one RoyaleAPI login per VM, or per pair. Removes the
shared-account risk entirely.

---

## 4. Sharding

**Built in. Use `--shard i/n`.**

```bash
# VM 0 of 8
./scrape.py run --card golem --min-rating 2000 --pages 40 \
    --group 8 --shard 0/8 --out data/golem
# VM 1 → --shard 1/8 ... VM 7 → --shard 7/8
```

The split is `sha1(player_tag) % n`, not a slice of roster order — so every
machine computes the same assignment with no coordination, and it doesn't move
when RoyaleAPI reorders boards or a rating changes. Verified across 9,000
synthetic tags (`n=3` → 2973 / 3081 / 2946).

**Sharding is an optimisation, not the dedup guarantee.** A battle belongs to
two players who can land in different shards. The guarantee is `replay_tag`,
the match's own unique ID. Collect the eight directories and merge:

```bash
./merge_runs.py merged/ vm0/data/golem vm1/data/golem ... vm7/data/golem
```

Dedupes on `replay_tag`, emits exactly one row per match. Tested with
deliberate overlap: 120 + 120 sharing 40 → 200 out.

Within one machine duplicates are already impossible — the sink refuses any
`replay_tag` already on disk. Across 55,000+ matches collected to date, unique
count equals row count exactly.

---

## 5. Realistic throughput per IP

**Measured over 45.56 hours, single IP:**

| Metric | Rate |
|---|---|
| Battles (with replays) | **712 / hour** |
| Card plays | **45,003 / hour** |
| Limiter settles at | 1.2–1.9 req/s |
| 429s | ~36 / hour |

Cost is ~1.25 requests per kept battle: one replay fetch each, plus a history
page amortised over ~10 battles, plus what the deck filter discards.

Linear 8× would be **~5,700 battles/hour**, ~360k card plays/hour. Ceiling, not
forecast, until §1 is tested.

---

## 6. What broke before

All fixed on `robust-crawl`. Listed because a VM on `main` reintroduces every
one of them, silently.

| Failure | Symptom | Fix |
|---|---|---|
| Cloudflare TLS fingerprinting | 403 on *every* path incl. public; curl with a valid `cf_clearance` rejected | All fetches run as `fetch()` inside the browser that owns the pass |
| History pager truncation | Players capped at ~70 battles instead of 1,800+ | A page with no replays no longer ends the walk; only the pager does |
| Duplicate rows | Same match written twice when two roster players fought each other; plays double-counted | Writes idempotent on `replay_tag` |
| Empty replays | Old battles return a record with no timeline; saved as zero-play rows | `filter_matches.py` drops them (`empty` rule) |
| Clearance expiry mid-run | 403 storm → process exits → rest of the night lost | Renewals 2→6; `night.sh` retries and resumes |
| Challenge timeout under load | `TimeoutError: wait_for_function` at startup | Challenge retried 3× before failing |

**Needs a human:** obtaining the cookie (one-time, §3); re-copying it if the
session expires (the crawl stops cleanly and says so).

**Does not need a human:** 429s (limiter absorbs them), clearance rotation
(re-solved automatically), process death (`night.sh` retries and resumes),
interruption (per-player checkpointing; rerunning the same command never
repeats work).

---

## 7. The archetype queue

All slugs below are **verified against RoyaleAPI**, with deck counts as returned:

| Order | Command flag | Decks | Status |
|---|---|---|---|
| 1 | `--card golem` | 25 | 238 / 943 players done on the laptop |
| 2 | `--card pekka,battle-ram` | 15 | in progress, 79 / 351 players |
| 3 | `--card miner` | 27 | not started |
| 4 | `--card goblin-drill` | 23 | not started |
| 5 | `--card royal-hogs` | 23 | not started |
| 6 | `--card x-bow` | 22 | not started |

A comma-separated `--card` means **all** those cards must be present, in any
evolution or hero form. `pekka,battle-ram` requires both — a Battle Ram deck
without PEKKA is rejected, as is PEKKA with Ram Rider.

Single cards cast a wide net: `--card golem` has produced 1,370 distinct deck
lists. That's intended here (volume is the goal), but whoever trains on it
should cluster on `team_deck` rather than treat one archetype as one strategy.

Narrower signatures are available if wanted: `miner,wall-breakers` → 3 decks,
`royal-hogs,flying-machine` → 13.

**Sizing.** Rosters are small: PEKKA is 351 players at rating ≥2000, Golem 943.
A single machine did 79 PEKKA players in 21 hours. Eight machines would exhaust
a 351-player roster in well under a day — **you run out of archetype before you
run out of compute.** Queue the next `--card` immediately; don't leave VMs idle.

**Resuming and re-sweeping.** Rerunning the same command with the same `--out`
resumes: finished players are skipped, known matches never re-fetched. Once an
archetype is exhausted, `--refresh --pages 3` re-walks everyone cheaply to pick
up newly played games — worth running periodically since retained history keeps
growing.

---

## 8. Suggested order

1. **One VM: `xvfb-run ./scrape.py selftest`.** Settles §2. If it fails, stop —
   nothing else matters.
2. **Two VMs, one hour, same archetype, different shards.** Compare combined
   throughput to 712/hour. Settles §1 and the shared-cookie question in §3.
3. **Scale to 8 only if step 2 gives ~1,400/hour** with no auth errors.
4. All 8 on archetype 1, `--shard 0/8` … `--shard 7/8`. When it exhausts,
   collect directories, `merge_runs.py`, move all 8 to archetype 2.

Steps 1–2 cost about two hours and remove both unknowns. Provisioning eight VMs
first risks eight machines that can't clear Cloudflare.

---

## Output format

Per archetype directory: `battles.csv` (one row per match), `plays.csv` (one row
per card played), `players.csv` (roster), `progress.json` (checkpoint). Join the
first two on `replay_tag`.

`plays.csv`: `replay_tag, play_index, tick, seconds, side, card, ability, x, y,
x_raw, y_raw`. `side=blue` is the tracked player. `x`/`y` are tiles on an 18×32
arena; `x_raw`/`y_raw` the source thousandths. Both blank only on champion
abilities (`ability=1`, `card=_invalid`), ~4% of rows.

`battles.csv` carries `team_cards`/`oppo_cards` — RoyaleAPI's own per-player
card counts. Coordinate-bearing placements reconcile against them exactly; a
mismatch means a parser bug.

Clean before use:

```bash
./filter_matches.py data/golem --secs 0
```

Writes `battles.clean.csv` / `plays.clean.csv` alongside the originals, plus
`battles.flagged.csv` with a `flags` column. `--secs 0` disables the
"3-crown inside 90s" rule, which was dropping genuine fast losses against
beatdown decks; the `empty` and `leak` rules are sound and stay on.
