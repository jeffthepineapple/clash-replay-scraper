# Running the scraper on GCP VMs — answers

Repo: https://github.com/jeffthepineapple/clash-replay-scraper
**Branch: `robust-crawl`** (not `main` — see "What broke" below).

Numbers here are measured from a 45.5-hour single-IP crawl on macOS unless
marked otherwise. Two of your six questions I cannot answer from evidence, and
I've said so rather than guessed — they're both deployment blockers, so test
them on one VM before provisioning eight.

---

## 1. Is the 429 ceiling per source IP? Is 8 IPs a linear 8×?

**Per IP: almost certainly yes. Linear 8×: probably not, and the reason is the
login cookie.**

The crawl makes two kinds of request:

| Request | Auth | Share of traffic |
|---|---|---|
| Deck boards, player battle history | **anonymous** — no session cookie | ~1 per 10 battles |
| `/data/replay` | **carries your account cookie** | ~1 per kept battle |

Public pages are fetched with no account identity at all, so those are bound to
IP and nothing else. Eight IPs should scale those cleanly.

`/data/replay` is the problem. It is the majority of requests by volume and
every one of them carries the same `__royaleapi_session_v2` cookie. If
RoyaleAPI rate-limits or flags per account — which I have **not** tested — then
eight machines hammering one account is the bottleneck, and worse, it is the
kind of pattern that gets a session invalidated rather than throttled.

**Evidence I have:** every run in this project was single-IP. The limiter is
AIMD and converged to 1.2–1.9 req/s on all of them regardless of archetype or
time of day, which is consistent with a stable server-side ceiling. Observed
429 counts: 1,637 over 45h (PEKKA), 233 over ~26h (Golem).

**Evidence I do not have:** any multi-IP run, or any test of whether one
session works from several IPs concurrently.

**Recommendation:** bring up **two** VMs first, run them for an hour, and
compare combined battles/hour against the 712/hour single-IP baseline. If it's
~1,400 you're linear and can go to eight. If it's ~712 or you start seeing
403s/login redirects, the account is the ceiling and you need separate
RoyaleAPI accounts per machine (or per pair).

---

## 2. Headless viability — does Xvfb work?

**Unknown. This is the single most likely thing to kill the plan, and I have
not tested it.**

What I know for certain:

- Headless Chromium **does not** clear the Cloudflare challenge. That's why the
  code launches `headless=False`.
- `royale/transport.py` refuses to start on Linux unless `DISPLAY` or
  `WAYLAND_DISPLAY` is set. `xvfb-run` sets `DISPLAY`, so it will get past that
  check.
- The README recommends `xvfb-run ./scrape.py` for exactly this case.

What I don't know: whether Cloudflare's challenge actually *solves* under Xvfb.
Passing the environment check is not the same as passing the challenge —
Cloudflare fingerprints far more than the presence of a display. The README's
claim is untested by me.

There is also a second-order risk: **datacenter IPs.** GCP ranges are widely
known to Cloudflare and frequently get harder challenges than residential IPs.
Even if Xvfb works on your laptop, it may not work from `us-west1`.

**Test this first, on one VM, before anything else:**

```bash
sudo apt-get update && sudo apt-get install -y xvfb python3-pip
git clone https://github.com/jeffthepineapple/clash-replay-scraper.git
cd clash-replay-scraper && git checkout robust-crawl
pip install -r requirements.txt && playwright install --with-deps chromium
export ROYALEAPI_SESSION='<cookie value — see §3>'
xvfb-run -a ./scrape.py selftest
```

`selftest` is the whole pipeline end-to-end in about a minute. If it prints
`ok chrome ... N card plays`, you're clear. If it hangs or raises
`AuthError: Cloudflare challenge never cleared`, the idea is dead in this form
and the fallbacks are:

- residential/mobile proxies in front of the VMs (adds cost and complexity)
- run the crawl on machines with real desktop sessions
- keep it on the laptop and use the VMs for engine conversion only

---

## 3. The login cookie on a VM

**Solved as of commit `0642042` — set `ROYALEAPI_SESSION`.**

Previously the cookie could only come from a local browser's jar, which a
server doesn't have. Now:

```bash
export ROYALEAPI_SESSION='<value of __royaleapi_session_v2>'
```

It is checked before the browser jars and wins outright. To get the value: log
into royaleapi.com in a desktop browser, DevTools → Application → Cookies →
`https://royaleapi.com` → copy `__royaleapi_session_v2`.

**Can it be reused across 8 machines?** Mechanically yes, it's just a cookie.
Whether RoyaleAPI *tolerates* it is the open question in §1 — one session
appearing from eight datacenter IPs at once is an unusual pattern. I have not
tested it and would not assume it's safe.

**If the session dies mid-run** the crawl detects it: three consecutive players
whose every replay fails stops the run with a clear message, rather than
burning hours writing nothing. Public-page collection would continue working;
only replays break, so you'd get battle rows with no timelines.

**Safer option if you have the accounts:** one RoyaleAPI login per VM, or per
pair of VMs. Removes the shared-account risk entirely.

---

## 4. Sharding

**Already built. Use `--shard i/n`.**

```bash
# VM 0 of 8
./scrape.py run --card pekka,battle-ram --min-rating 2000 --pages 40 \
    --group 8 --shard 0/8 --out data/pbs
# VM 1 → --shard 1/8, ... VM 7 → --shard 7/8
```

The split is `sha1(player_tag) % n`, not a slice of roster order. That means
every machine computes the same assignment independently, with no coordination,
and the answer doesn't move when RoyaleAPI reorders its boards or a rating
changes. Verified even across 9,000 synthetic tags (`n=3` → 2973/3081/2946).

**Sharding is an optimisation, not the deduplication guarantee.** A battle
belongs to two players, and they can land in different shards. The guarantee is
`replay_tag`, the match's own unique ID. Collect the eight output directories
and merge:

```bash
./merge_runs.py merged/ vm0/data/pbs vm1/data/pbs ... vm7/data/pbs
```

That dedupes on `replay_tag` and emits exactly one row per match. Tested with
deliberate overlap: 120 + 120 rows sharing 40 → 200 out.

Within a single machine, duplicates are already impossible: the sink refuses
any `replay_tag` already on disk. Across 55,464 collected matches to date the
unique count equals the row count exactly.

---

## 5. Realistic throughput per IP

**Measured over 45.56 hours, single IP, PEKKA archetype:**

| Metric | Rate |
|---|---|
| Battles (with replays) | **712 / hour** |
| Card plays | **45,003 / hour** |
| Request rate the limiter settles at | 1.2–1.9 req/s |
| 429s | 1,637 over 45.5h (~36/hour) |

Cost is roughly **1.25 requests per kept battle** — one replay fetch each, plus
one history page amortised over ~10 battles, plus whatever the deck filter
discards.

If 8 IPs scale linearly: **~5,700 battles/hour**, ~360k card plays/hour. Treat
that as a ceiling, not a forecast, until §1 is tested.

Sizing note: rosters are small. PEKKA is 351 players at rating ≥2000, Golem
943. At 712/hour a single machine finished 79 PEKKA players in 21 hours. Eight
machines would exhaust a 351-player roster in well under a day — **you will run
out of archetype before you run out of compute.** Line up several `--card`
signatures rather than provisioning eight VMs for one deck.

---

## 6. What broke last time

Everything below is **already fixed on `robust-crawl`**. Listed because a VM on
`main` reintroduces all of it, silently.

| Failure | Symptom | Fix |
|---|---|---|
| Cloudflare TLS fingerprinting | 403 on *every* path incl. public ones; curl carrying a valid `cf_clearance` rejected | All fetches now run as `fetch()` inside the browser that owns the pass |
| History pager truncation | Players capped at ~70 battles instead of 1,800+ | A page with no replays no longer ends the walk; only the pager does |
| Duplicate rows | Same match written twice when two roster players fought each other; plays double-counted | Writes are idempotent on `replay_tag` |
| Empty replays | Old battles return a record with no timeline; saved as zero-play rows | `filter_matches.py` drops them (`empty` rule) |
| Clearance expiry mid-run | 403 storm → process exits → rest of the night lost | Renewals 2→6; `night.sh` retries and resumes |
| Challenge timeout under load | `TimeoutError: wait_for_function` on startup | Challenge retried 3× before failing |

**Needs a human at the keyboard:**

- **Getting the cookie** — one-time, from a desktop browser (§3).
- **macOS Keychain prompt** — not applicable on Linux; `ROYALEAPI_SESSION`
  bypasses it entirely.
- **Session expiry** — if the cookie dies, someone must re-copy it. The crawl
  stops cleanly and says so.

**Does not need a human:** 429s (the limiter absorbs them), clearance rotation
(re-solved automatically), process death (`night.sh` retries and resumes),
interruption (per-player checkpointing; `progress.json` + `replay_tag` dedupe
mean rerunning the same command never repeats work).

---

## Suggested deployment order

1. **One VM, `xvfb-run ./scrape.py selftest`.** Settles §2. If it fails, stop —
   nothing else matters.
2. **Two VMs, one hour, same archetype, different shards.** Compare combined
   throughput to 712/hour. Settles §1 and the shared-cookie question in §3.
3. **Scale to 8 only if step 2 is ~1,400/hour** with no auth errors.
4. Give each VM a distinct `--shard i/8`, collect the directories, `merge_runs.py`.

Steps 1 and 2 cost about two hours and remove both unknowns. Provisioning eight
VMs before them risks eight machines that can't clear Cloudflare.
