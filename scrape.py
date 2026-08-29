#!/usr/bin/env python3
"""RoyaleAPI replay scraper. Interactive rich TUI -- just run it.

    ./scrape.py            the TUI
    ./scrape.py selftest   end-to-end check against one player

seed deck -> deck variations -> rated players -> battle history -> replays

Only battles played on a *variation* of the seed deck are kept: same base cards,
evolution and hero swaps allowed, no substituted cards. cannon/cannon-ev1 and
musketeer/musketeer-hero all count as the same deck; swap in a Rocket and the
battle is dropped.

An anonymous headful Chromium solves the Cloudflare challenge and holds the
cf_clearance; every fetch then runs in parallel through curl behind a
self-tuning rate limiter that probes RoyaleAPI's 429 threshold and stays under
it (royale/limiter.py). The RoyaleAPI session cookie -- lifted from whichever
local browser you are logged in with, any browser, any OS -- is attached only to
the login-gated /data/replay calls.

Writes battles.csv and plays.csv, joinable on replay_tag.
"""

from __future__ import annotations

import sys

from royale import parse, pipeline
from royale.cookies import find_sessions
from royale.limiter import Limiter
from royale.transport import AuthError, Curl, Pages
from royale.ui import SEED, app, console


def selftest() -> None:
    """Smallest end-to-end check: every stage must yield sane data."""
    sessions = find_sessions()
    assert sessions, "no RoyaleAPI session in any local browser"

    assert parse.base_cards("cannon-ev1,musketeer-hero") == {"cannon", "musketeer"}
    assert parse.is_variation("cannon,fireball,hog-rider,ice-golem,ice-spirit,"
                              "musketeer-hero,skeletons-ev1,the-log", SEED)
    assert not parse.is_variation("rocket," + ",".join(SEED.split(",")[1:]), SEED)

    pages = Pages()
    try:
        curl = Curl(pages, sessions[0])
        assert curl.logged_in(), f"{sessions[0]} is not logged in"

        lim = curl.limiter
        start = lim.rate

        decks = pipeline.similar_decks(curl, SEED)
        assert decks[0] == SEED and len(decks) > 1, decks
        assert all(parse.is_variation(d, SEED) for d in decks), "similar decks are not variations"

        players, found_on = pipeline.rated_players(curl, [SEED])
        assert players, "no rated players"
        first = next(iter(players.values()))
        assert first["player_tag"].isalnum() and first["rating"].isdigit(), first
        one = {first["player_tag"]: first}

        tag = first["player_tag"]
        page1 = pipeline.player_battles(curl, tag, 1)
        assert page1, f"no battles on page 1 for {tag}"
        paged = pipeline.player_battles(curl, tag, 3)
        assert len(paged) > len(page1), "history pagination did not go deeper"
        assert len({b["replay_tag"] for b in paged}) == len(paged), "pagination duplicated battles"
        assert all(b["battle_timestamp"] for b in paged), "battles missing timestamps"
        assert min(b["battle_timestamp"] for b in paged) \
            < min(b["battle_timestamp"] for b in page1), "later pages are not older"

        rows, dropped = pipeline.battles(curl, one, found_on, SEED, max_pages=3)
        assert rows, f"no variation battles for {tag} ({dropped} dropped)"
        assert all(parse.is_variation(r["team_deck"], SEED) for r in rows), rows[0]["team_deck"]
        assert rows[0]["result"] in {"win", "loss", "draw"}, rows[0]

        got = pipeline.replays(curl, rows[:4])
        assert len(got) == len(rows[:4]), f"parallel replays dropped some: {len(got)}"
        stats, plays = got[0][1]
        assert len(plays) > 10, plays
        assert all(p["side"] in {"blue", "red"} and p["card"] for p in plays), plays[:3]
        assert plays == sorted(plays, key=lambda p: p["tick"]), "timeline not in tick order"
        assert stats["team_elixir_total"] and stats["oppo_elixir_leaked"], stats

        assert lim.sent > 10, f"limiter saw only {lim.sent} requests"
        assert lim.rate > start or lim.hits, "limiter never ramped up on clean traffic"
        assert lim.rate <= lim.ceiling and lim.rate >= lim.floor, lim
        # A 429 must actually pull the rate down, not just get counted.
        probe = Limiter(rate=10.0, ceiling=100.0)
        probe.blocked()
        assert probe.rate == 5.0, probe
        probe.hits, probe.rate = 0, 10.0
        probe.ok()
        assert probe.rate > 10.0, "slow start did not grow"
    finally:
        pages.close()
    console.print(f"[green]ok[/] {sessions[0]} | {len(decks)} decks, {len(players)} rated players, "
                  f"history {len(page1)} -> {len(paged)} over 3 pages, "
                  f"{len(rows)} variation battles ({dropped} dropped), "
                  f"{len(got)} replays in parallel, {len(plays)} card plays\n"
                  f"[dim]rate: {lim}[/]")


def main() -> int:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "selftest":
            selftest()
            return 0
        return 0 if app() else 1
    except AuthError as e:
        console.print(f"[red]{e}[/]")
        return 2
    except KeyboardInterrupt:
        console.print("[yellow]cancelled[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
