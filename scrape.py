#!/usr/bin/env python3
"""RoyaleAPI replay scraper. Interactive rich TUI -- just run it.

    ./scrape.py            the TUI
    ./scrape.py selftest   end-to-end check against one player

seed deck -> deck variations -> rated players -> battle history -> replays

Only battles played on a *variation* of the seed deck are kept: same base cards,
evolution and hero swaps allowed, no substituted cards. cannon/cannon-ev1 and
musketeer/musketeer-hero all count as the same deck; swap in a Rocket and the
battle is dropped.

A headful Chromium solves the Cloudflare challenge and then issues every
request itself, batched, behind a self-tuning rate limiter that probes
RoyaleAPI's 429 threshold and stays under it (royale/limiter.py). Cloudflare
fingerprints the TLS handshake, so an outside HTTP client cannot borrow the
pass. The RoyaleAPI session cookie -- lifted from whichever local browser you
are logged in with, any browser, any OS -- rides only in the second browser
context, the one used for the login-gated /data/replay calls.

Writes battles.csv and plays.csv, joinable on replay_tag.
"""

from __future__ import annotations

import sys

from royale import parse, pipeline
from royale.cookies import find_sessions
from royale.limiter import Limiter
from royale.transport import AuthError, Client, Pages
from royale import ui
from royale.ui import SEED, app, console


def selftest() -> None:
    """Smallest end-to-end check: every stage must yield sane data."""
    sessions = find_sessions()
    assert sessions, "no RoyaleAPI session in any local browser"

    assert parse.base_cards("cannon-ev1,musketeer-hero") == {"cannon", "musketeer"}
    assert parse.is_variation("cannon,fireball,hog-rider,ice-golem,ice-spirit,"
                              "musketeer-hero,skeletons-ev1,the-log", SEED)
    assert not parse.is_variation("rocket," + ",".join(SEED.split(",")[1:]), SEED)

    pages = Pages(sessions[0])
    try:
        curl = Client(pages)
        assert curl.logged_in(), f"{sessions[0]} is not logged in"

        lim = curl.limiter
        start = lim.rate

        decks = pipeline.similar_decks(curl, SEED)
        assert decks[0] == SEED and len(decks) > 1, decks
        # /similar lists near neighbours too -- a Knight where the seed runs Ice
        # Golem, say -- so the roster is a superset of the archetype. Purity comes
        # from the battle filter, not from this list; most of it should still be
        # variations, and a board that is not one merely costs a request.
        variations = [d for d in decks if parse.is_variation(d, SEED)]
        assert len(variations) >= len(decks) * 0.6, [d for d in decks if d not in variations]

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

        rows, dropped, modes = pipeline.battles(curl, one, found_on, SEED, max_pages=3)
        assert rows, f"no variation battles for {tag} ({dropped} dropped)"
        assert modes and sum(modes.values()) == len(rows), (modes, len(rows))
        # The mode filter must actually bite: keeping a type nothing matches leaves nothing.
        none_kept, _, _ = pipeline.battles(curl, one, found_on, SEED, max_pages=1,
                                           keep_types=frozenset({"__nosuchmode__"}))
        assert not none_kept, "mode filter kept battles it should not have"
        assert all(parse.is_variation(r["team_deck"], SEED) for r in rows), rows[0]["team_deck"]
        assert rows[0]["result"] in {"win", "loss", "draw"}, rows[0]

        # replays() streams into a sink; anything with .write does, so the check
        # collects in memory instead of touching the caller's CSVs.
        collected: list[tuple] = []

        class _Collect:
            @staticmethod
            def write(b, stats, plays) -> bool:
                collected.append((b, stats, plays))
                return True  # a sink returns False only for an already-written tag

        got = pipeline.replays(curl, rows[:4], sink=_Collect())
        assert got == len(rows[:4]) == len(collected), f"parallel replays dropped some: {got}"
        _, stats, plays = collected[0]
        assert len(plays) > 10, plays
        assert all(p["side"] in {"blue", "red"} and p["card"] for p in plays), plays[:3]
        assert plays == sorted(plays, key=lambda p: p["tick"]), "timeline not in tick order"
        assert stats["team_elixir_total"] and stats["oppo_elixir_leaked"], stats

        # Placements must reconcile with RoyaleAPI's own count. Each elixir table
        # reads 'Total <cards> <elixir>', and that card count is exactly the number
        # of markers carrying coordinates -- hero-ability rows have none by design,
        # so a selector that silently drops decorated markers shows up right here.
        for side, key in (("blue", "team_cards"), ("red", "oppo_cards")):
            placed = [p for p in plays if p["side"] == side and p["x"]]
            assert str(len(placed)) == stats[key], \
                f"{side}: parsed {len(placed)} placements, page says {stats[key]}"
            assert all(0 <= float(p["x"]) <= 18 and 0 <= float(p["y"]) <= 32
                       for p in placed), "placement off the 18x32 arena"
        noxy = [p for p in plays if not p["x"]]
        assert all(p["ability"] == "1" or p["card"] == "_invalid" for p in noxy), noxy[:3]

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
                  f"{len(rows)} variation battles ({dropped['deck']} dropped), "
                  f"modes {', '.join(f'{k}={v}' for k, v in sorted(modes.items()))}, "
                  f"{got} replays in parallel, {len(plays)} card plays "
                  f"({sum(1 for p in plays if p['x'])} placed)\n"
                  f"[dim]rate: {lim}[/]")


def run_unattended(argv: list[str]) -> int:
    """./scrape.py run [--min-rating N] [--pages N] [--group N] [--out DIR]

    No prompts, so it survives a night in a terminal nobody is watching. Ctrl-C
    stops after the group in flight; rerunning the same --out resumes.
    """
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(prog="scrape.py run")
    ap.add_argument("--deck", default=SEED,
                    help="seed deck as comma-separated card slugs; evo cards end -ev1, "
                         "champions/heroes -hero. Defaults to the Hog 2.6 seed.")
    ap.add_argument("--card", default="",
                    help="crawl every deck built around this card (e.g. golem) instead of "
                         "one fixed list; overrides --deck")
    ap.add_argument("--min-rating", type=int, default=0,
                    help="drop players rated below this (the board is Ultimate Champion only)")
    ap.add_argument("--pages", type=int, default=0,
                    help="history pages per player, 0 walks the whole archive")
    ap.add_argument("--group", type=int, default=ui.GROUP,
                    help="players per checkpoint")
    ap.add_argument("--out", type=Path, default=ui.OUTDIR, help="output directory")
    ap.add_argument("--all-modes", action="store_true",
                    help="keep 2v2, challenges and friendlies too")
    ap.add_argument("--refresh", action="store_true",
                    help="re-walk players already finished, picking up new battles only")
    ap.add_argument("--any-deck", action="store_true",
                    help="keep every deck these players ran, not just seed variations")
    a = ap.parse_args(argv)
    return ui.auto(a.deck, a.min_rating, a.pages, a.group, a.out, not a.all_modes,
                   not a.any_deck, a.refresh, a.card)


def main() -> int:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "selftest":
            selftest()
            return 0
        if len(sys.argv) > 1 and sys.argv[1] == "run":
            run_unattended(sys.argv[2:])
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
