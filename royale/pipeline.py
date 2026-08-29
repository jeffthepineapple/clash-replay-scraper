"""Crawl stages and CSV output. UI-free: each stage takes an optional `tick`
callback so the TUI can drive a progress bar without this module knowing about
one. Stages are independent -- call only the ones a new feature needs.

Every stage is fan-out over a thread pool. Threads are not the throttle: the
shared Limiter inside `curl` is, so pool size only has to be big enough to keep
the allowed rate saturated.
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
from typing import Callable, Iterable

from . import parse
from .transport import BASE, ClearanceExpired, Curl

Tick = Callable[[], None]
DEFAULT_POOL = 4  # concurrent in-flight requests; kept low, the Limiter tunes the rate on top

BATTLE_FIELDS = [
    "replay_tag", "deck", "player_tag", "player_name", "clan_tag", "rating", "rank", "wins_7d",
    "battle_time", "battle_timestamp", "battle_type", "result", "team_tags", "opponent_tags",
    "team_crowns", "opponent_crowns", "team_deck", "opponent_deck",
    "team_elixir_total", "team_elixir_troop", "team_elixir_building", "team_elixir_spell",
    "team_elixir_leaked", "oppo_elixir_total", "oppo_elixir_troop", "oppo_elixir_building",
    "oppo_elixir_spell", "oppo_elixir_leaked", "plays",
]
PLAY_FIELDS = ["replay_tag", "play_index", "tick", "seconds", "side", "card", "ability"]


def _noop() -> None:
    pass


def _fan(items: list, fn, workers: int, tick: Tick, on_error) -> list[tuple]:
    """Run fn over items in parallel, keeping (item, result) for the ones that
    worked. Ctrl-C and an expired Cloudflare pass both stop the wave and keep
    whatever already landed.
    """
    def attempt(item):
        try:
            return fn(item)
        except Exception as e:  # one bad item must not kill the wave
            return e

    out, pool = [], ThreadPoolExecutor(max(1, workers))
    try:
        for item, res in zip(items, pool.map(attempt, items)):
            tick()
            if isinstance(res, Exception):
                if on_error:
                    on_error(item, res)
                if isinstance(res, ClearanceExpired):
                    break
            else:
                out.append((item, res))
    except KeyboardInterrupt:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


# ---------------------------------------------------------------- stages
def similar_decks(curl: Curl, seed: str, limit: int = 25) -> list[str]:
    return parse.similar_decks(curl.get(f"/decks/stats/{seed}/similar"), seed)[:limit]


def rated_players(curl: Curl, decks: Iterable[str], per_deck: int | None = None,
                  workers: int = DEFAULT_POOL, tick: Tick = _noop, on_error=None,
                  ) -> tuple[dict[str, dict], dict[str, str]]:
    """Ratings boards for every deck -> (tag -> player, tag -> deck found on).

    A player can top the boards of several variations; keep the first hit so the
    battle crawl visits each player exactly once.
    """
    decks = list(decks)
    fetched = _fan(decks, lambda d: parse.rated_players(
        curl.get(f"/decks/stats/{d}/players/ratings")), workers, tick, on_error)

    players: dict[str, dict] = {}
    found_on: dict[str, str] = {}
    for deck, rows in fetched:
        for p in rows[:per_deck]:
            players.setdefault(p["player_tag"], p)
            found_on.setdefault(p["player_tag"], deck)
    return players, found_on


def player_battles(curl: Curl, tag: str, max_pages: int = 0,
                   out: list[dict] | None = None) -> list[dict]:
    """A player's battle history, following the pager's 'older' link.

    /battles/history serves 10 battles a page and its next link carries
    ?before=<oldest battle, epoch ms>, so this walks back through the whole
    archive -- far deeper than the battles page's infinite scroll. Sequential by
    necessity: page N+1's cursor is only known once page N is parsed, which is
    why parallelism lives across players instead.

    max_pages == 0 walks to the end. `out` is appended to as pages arrive, so an
    interrupt leaves the caller holding everything fetched so far.
    """
    path = f"/player/{tag}/battles/history"
    rows = [] if out is None else out
    seen = {b["replay_tag"] for b in rows}
    for page in count(1):
        if max_pages and page > max_pages:
            break
        html = curl.get(path)
        fresh = [b for b in parse.battles(html) if b["replay_tag"] not in seen]
        if not fresh:
            break
        seen.update(b["replay_tag"] for b in fresh)
        rows += fresh
        nxt = parse.next_history_page(html)
        if not nxt:
            break
        path = nxt
    return rows


def battles(curl: Curl, players: dict[str, dict], found_on: dict[str, str], seed: str,
            max_pages: int = 0, workers: int = DEFAULT_POOL, tick: Tick = _noop, on_error=None,
            ) -> tuple[list[dict], int]:
    """Battle rows for every player in parallel, keeping only games played on a
    variation of `seed` -- same base cards, evo/hero swaps allowed, no
    substituted cards.

    Returns (rows, dropped) so the caller can report what the filter removed.
    """
    tags = list(players)
    fetched = _fan(tags, lambda t: player_battles(curl, t, max_pages), workers, tick, on_error)

    rows, dropped = [], 0
    for tag, got in fetched:
        for b in got:
            if not parse.is_variation(b["team_deck"], seed):
                dropped += 1
                continue
            rows.append({"deck": found_on.get(tag, seed), **players[tag], **b})
    return rows, dropped


def replay_params(b: dict) -> dict:
    return {
        "tag": b["replay_tag"], "team_tags": b["team_tags"], "opponent_tags": b["opponent_tags"],
        "team_crowns": b["team_crowns"], "opponent_crowns": b["opponent_crowns"],
        "referrer_path": f"{BASE}/player/{b['team_tags'].split(',')[0]}/battles",
    }


def fetch_replay(curl: Curl, b: dict) -> tuple[dict, list[dict]]:
    data = curl.json("/data/replay", replay_params(b))
    if not data.get("success"):
        raise RuntimeError(f"replay {b['replay_tag']} refused (login expired?)")
    return parse.replay(data["html"])


def replays(curl: Curl, rows: list[dict], workers: int = DEFAULT_POOL,
            tick: Tick = _noop, on_error=None,
            ) -> list[tuple[dict, tuple[dict, list[dict]]]]:
    return _fan(rows, lambda b: fetch_replay(curl, b), workers, tick, on_error)


# ---------------------------------------------------------------- output
def write_csv(outdir: Path, got: list[tuple[dict, tuple[dict, list[dict]]]]
              ) -> tuple[Path, Path, int]:
    """battles.csv + plays.csv, joinable on replay_tag."""
    outdir.mkdir(parents=True, exist_ok=True)
    battles_csv, plays_csv = outdir / "battles.csv", outdir / "plays.csv"
    n_plays = 0
    with battles_csv.open("w", newline="") as bf, plays_csv.open("w", newline="") as pf:
        bw, pw = csv.DictWriter(bf, BATTLE_FIELDS), csv.DictWriter(pf, PLAY_FIELDS)
        bw.writeheader()
        pw.writeheader()
        for b, (stats, plays) in got:
            bw.writerow({**b, **stats, "plays": len(plays)})
            pw.writerows(plays)
            n_plays += len(plays)
    return battles_csv, plays_csv, n_plays
