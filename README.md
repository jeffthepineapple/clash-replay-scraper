# clash-replay-scraper

Interactive scraper for [RoyaleAPI](https://royaleapi.com) that mines a Clash
Royale deck archetype: finds every evolution/hero variation of a seed deck,
lists the rated players playing it, walks their full battle history, and pulls
a replay timeline (card-by-card, tick-accurate) for every matching battle.

Output: `battles.csv` and `plays.csv`, joinable on `replay_tag`.

## How it works

- An anonymous headful browser (installed Google Chrome on Windows, Playwright
  Chromium elsewhere) solves RoyaleAPI's Cloudflare challenge and holds the
  `cf_clearance` cookie. It does nothing else.
- Every actual fetch runs in parallel through `curl`, behind a self-tuning
  rate limiter that ramps up until RoyaleAPI answers 429 and then settles just
  under that ceiling (AIMD, same idea as TCP congestion control). The 429
  limit is per source IP, so this is the only lever that matters for speed --
  more browser tabs or sessions do not help.
- Public pages (deck stats, ratings, battle history) are fetched anonymously.
  Only `/data/replay` is login-gated; that request carries the RoyaleAPI
  session cookie read out of whichever local browser you're logged in with
  (Chrome, Edge, Firefox, Brave, Vivaldi, Opera, Arc, Safari -- any OS).
- Only battles played on a *variation* of the seed deck are kept: same base
  cards, evolution/hero swaps allowed, no substituted cards.

## Prerequisites

- Python 3.11+
- `curl` on `PATH`
- A Linux desktop needs `DISPLAY` or `WAYLAND_DISPLAY` set (the Cloudflare
  challenge only solves in a real, visible browser -- headless doesn't clear
  it).
- Logged into [royaleapi.com](https://royaleapi.com) in at least one local
  browser (Battle Replay is a login-gated feature).
- On Windows, Google Chrome must be installed. The scraper launches the stable
  Chrome channel for Cloudflare verification instead of Playwright's bundled
  Chromium.

## Install

```sh
git clone https://github.com/jeffthepineapple/clash-replay-scraper.git
cd clash-replay-scraper
pip install -r requirements.txt
playwright install chromium
```

## Usage

```sh
./scrape.py            # interactive TUI
./scrape.py selftest   # end-to-end check against one player
```

The TUI walks through:

1. **Session** -- which local browser's RoyaleAPI login to use (auto-picked if
   only one).
2. **Settings** -- parallel request count and rate ceiling. Defaults are
   deliberately conservative; raise them if you want to trade speed for a
   higher chance of hitting 429s (the limiter recovers on its own either way).
3. **Roster** -- every rated player on the seed deck and its variations, as a
   table you can page through.
4. **Player picker** -- TAB-completed names/tags, numbers, ranges (`1,4,7-9`),
   or `all`. Add as many lines as you like; an empty line starts the crawl.
5. **Depth** -- how many battle-history pages per player (10 battles/page).
   Blank or `0` walks each player's full history. Ctrl-C during the crawl
   stops it and still writes the CSVs with whatever was fetched.

To scrape a different archetype, edit `SEED` in `royale/ui.py` -- a
comma-separated list of card slugs, e.g. `cannon-ev1,fireball,hog-rider,...`
(evolution cards end in `-ev1`, champions/heroes in `-hero`).

## Troubleshooting

- **`no RoyaleAPI session found in any local browser`** -- log into
  royaleapi.com in a supported browser, then rerun.
- **`403 on ... reload royaleapi.com`** -- the anonymous browser's Cloudflare
  pass expired mid-run; rerun.
- **`no display`** -- SSH/headless box: run under Xvfb (`xvfb-run
  ./scrape.py`) or a real desktop session.
