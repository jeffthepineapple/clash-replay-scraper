"""Rich TUI: the whole program is this interactive flow. No flags, no argv.

Deck is fixed (SEED) -- the tool exists to mine that archetype, so it is not
asked for. What you choose is which of the players found on the deck and its
evo/hero variations get crawled, and how deep to walk their history.

Speed is not a prompt: the limiter ramps up on real crawl traffic until
RoyaleAPI answers 429, then settles just under that, and the live figure sits on
the end of the progress bar.
"""

from __future__ import annotations

try:
    import readline
except ImportError:  # not part of the Python standard library on Windows
    readline = None
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import parse, pipeline
from .cookies import Session, find_sessions
from .limiter import Limiter
from .transport import LOGIN_WAIT, AuthError, Curl, Pages

SEED = "cannon-ev1,fireball,hog-rider,ice-golem,ice-spirit,musketeer,skeletons,the-log"
OUTDIR = Path(".")

console = Console()


def _progress(limiter: Limiter) -> Progress:
    """Progress bar with the live request rate glued on the end."""
    return Progress(
        TextColumn("[bold blue]{task.description}"), BarColumn(), MofNCompleteColumn(),
        TimeElapsedColumn(), TextColumn("[dim]{task.fields[rate]}"), console=console)


def _rate_field(limiter: Limiter) -> dict:
    return {"rate": str(limiter)}


# ---------------------------------------------------------------- session
def pick_session() -> Session | None:
    """Which local browser holds a RoyaleAPI login. None: sign in in ours instead."""
    with console.status("[cyan]reading browser cookie jars..."):
        found = find_sessions()
    if not found:
        console.print("[yellow]no RoyaleAPI session in any local cookie jar[/]\n"
                      "[dim]Chrome and Edge 127+ on Windows encrypt their jar app-bound: "
                      "nothing outside the browser can read it.[/]")
        return None
    if len(found) == 1:
        console.print(f"[green]session[/] {found[0]}")
        return found[0]

    t = Table("#", "browser", "session", title="RoyaleAPI logins found")
    for i, s in enumerate(found, 1):
        t.add_row(str(i), s.browser, s.value[:12] + "...")
    t.add_row("s", "this browser", "[dim]sign in now[/]")
    console.print(t)
    while True:
        raw = console.input("[bold]use which[/] [dim](1)[/] ").strip().lower() or "1"
        if raw == "s":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(found):
            return found[int(raw) - 1]
        console.print("[yellow]pick one of the listed numbers, or s to sign in[/]")


def browser_login(pages: Pages) -> Session:
    """Sign in inside our own browser and keep the cookie it earns."""
    console.print("[cyan]sign in to RoyaleAPI in the browser window[/] "
                  f"[dim](waiting up to {LOGIN_WAIT // 60} min)[/]")
    session = Session("this browser", pages.login())
    console.print("[green]signed in[/]")
    return session


# ---------------------------------------------------------------- roster
def roster(curl: Curl, seed: str, workers: int) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    """Every player listed on the seed deck and all its variations."""
    with console.status("[cyan]finding deck variations..."):
        decks = pipeline.similar_decks(curl, seed)
    console.print(f"[green]{len(decks)} decks[/] (seed + {len(decks) - 1} variations)")

    with _progress(curl.limiter) as prog:
        t = prog.add_task("rating boards", total=len(decks), **_rate_field(curl.limiter))
        players, found_on = pipeline.rated_players(
            curl, decks, workers=workers,
            tick=lambda: prog.update(t, advance=1, **_rate_field(curl.limiter)),
            on_error=lambda d, e: console.print(f"[yellow]skip[/] board {d}: {e}"))
    if not players:
        raise AuthError("no rated players on any of those decks")

    # Best players first: the roster is a leaderboard, so read it like one.
    order = sorted(players, key=lambda t_: -int(players[t_]["rating"] or 0))
    seed_cards = set(seed.split(","))
    table = Table("#", "player", "tag", "rating", "wins 7d", "clan", "variation",
                  title=f"{len(order)} players on this archetype")
    for i, tag in enumerate(order, 1):
        p = players[tag]
        variant = found_on.get(tag, "")
        table.add_row(str(i), p["player_name"], tag, p["rating"], p["wins_7d"], p["clan_tag"],
                      "seed" if variant == seed else ",".join(
                          c for c in variant.split(",") if c not in seed_cards))
    console.print(table)
    return players, found_on, order


# ---------------------------------------------------------------- picker
def _completer(options: list[str]):
    def complete(text: str, state: int):
        low = text.lower().lstrip()
        hits = [o for o in options if o.lower().startswith(low)]
        if not hits:  # fall back to substring so a bare tag or surname completes
            hits = [o for o in options if low in o.lower()]
        return hits[state] if state < len(hits) else None
    return complete


def pick_players(players: dict[str, dict], order: list[str]) -> list[str]:
    """Type names or tags with TAB completion, numbers, ranges, or 'all'."""
    labels = {f"{players[tag]['player_name']} #{tag}".strip(): tag for tag in order}
    options = sorted(labels) + sorted(f"#{t}" for t in order) + ["all"]

    if readline is not None:
        readline.set_completer_delims(",")
        readline.set_completer(_completer(options))
        readline.parse_and_bind("tab: complete")
        help_text = "[bold]TAB[/] completes names and tags"
    else:
        help_text = "type names or tags"

    console.print(Panel(
        f"{help_text}  ·  numbers and ranges work "
        "([cyan]1,4,7-9[/])  ·  [cyan]all[/] takes everyone\n"
        "add as many lines as you like, [bold]empty line[/] starts the crawl",
        title="pick players", border_style="cyan", expand=False))

    chosen: list[str] = []
    while True:
        raw = input("players> ").strip()
        if not raw:
            if chosen:
                return chosen
            console.print("[yellow]nothing picked yet[/]")
            continue
        for token in (t.strip() for t in raw.split(",") if t.strip()):
            hits = _resolve(token, players, order, labels)
            if not hits:
                console.print(f"[yellow]no match[/] {token!r}")
            for tag in hits:
                if tag not in chosen:
                    chosen.append(tag)
                    console.print(f"  [green]+[/] {players[tag]['player_name']} "
                                  f"[dim]#{tag}[/] [dim]({len(chosen)} picked)[/]")


def _resolve(token: str, players: dict[str, dict], order: list[str],
             labels: dict[str, str]) -> list[str]:
    """One token -> player tags. Accepts 'all', 3, 7-9, '#TAG', a label, a name."""
    low = token.lower()
    if low == "all":
        return list(order)
    if token in labels:
        return [labels[token]]

    lo, _, hi = token.partition("-")
    if lo.isdigit() and (hi.isdigit() or not hi):
        rng = range(int(lo), int(hi or lo) + 1)
        return [order[n - 1] for n in rng if 1 <= n <= len(order)]

    tag = token.lstrip("#").upper()
    if tag in players:
        return [tag]
    hits = [t for t in order if low in players[t]["player_name"].lower()]
    if len(hits) > 1:
        console.print(f"[yellow]{token!r} matches {len(hits)} players[/] -- TAB to disambiguate")
        return []
    return hits


# ---------------------------------------------------------------- settings
def pick_settings(limiter: Limiter) -> int:
    """Max parallel requests and the rate ceiling. Kept low by default --
    RoyaleAPI's 429 is per source IP, so more browser tabs would not help;
    this is the one knob that actually trades speed for risk of being throttled.
    """
    console.print(Panel(
        f"[bold]parallel requests[/] -- concurrent in-flight fetches, default "
        f"[cyan]{pipeline.DEFAULT_POOL}[/]\n"
        f"[bold]rate ceiling[/] -- req/s the limiter won't probe past, default "
        f"[cyan]{limiter.ceiling:.0f}[/]\n"
        "the limiter still self-tunes under this ceiling and still backs off on 429",
        title="settings", border_style="cyan", expand=False))
    workers = _ask_int("parallel requests", pipeline.DEFAULT_POOL, lo=1, hi=32)
    limiter.ceiling = _ask_int("rate ceiling (req/s)", int(limiter.ceiling), lo=1, hi=60)
    return workers


def _ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]> ").strip()
        if not raw:
            return default
        if raw.isdigit() and lo <= int(raw) <= hi:
            return int(raw)
        console.print(f"[yellow]a number from {lo} to {hi}, or blank for {default}[/]")


# ---------------------------------------------------------------- depth
def pick_depth() -> int:
    """History pages per player. 0 means walk the archive to its end."""
    console.print(Panel(
        "[bold]blank[/] or [cyan]0[/] walks each player's history all the way back\n"
        "otherwise a page count, [cyan]10[/] battles per page  ·  "
        "[bold]Ctrl-C[/] stops the crawl and still writes the CSVs",
        title="how deep", border_style="cyan", expand=False))
    while True:
        raw = input("pages> ").strip()
        if not raw:
            return 0
        if raw.isdigit():
            return int(raw)
        console.print("[yellow]a number, or blank for everything[/]")


# ---------------------------------------------------------------- crawl
def crawl(curl: Curl, seed: str, players: dict[str, dict],
          found_on: dict[str, str], max_pages: int, workers: int) -> int:
    lim = curl.limiter
    skipped = 0

    def skip(what: str, e: Exception) -> None:
        nonlocal skipped
        skipped += 1
        console.print(f"[yellow]skip[/] {what}: {e}")

    with _progress(lim) as prog:
        t = prog.add_task("battle history", total=len(players), **_rate_field(lim))
        rows, dropped = pipeline.battles(
            curl, players, found_on, seed, max_pages, workers,
            tick=lambda: prog.update(t, advance=1, **_rate_field(lim)),
            on_error=lambda tag, e: skip(f"battles {tag}", e))
    console.print(f"[green]{len(rows)} battles[/] on a seed-deck variation "
                  f"({dropped} dropped: other decks)")
    if not rows:
        return 0

    with _progress(lim) as prog:
        t = prog.add_task("replays", total=len(rows), **_rate_field(lim))
        got = pipeline.replays(
            curl, rows, workers,
            tick=lambda: prog.update(t, advance=1, **_rate_field(lim)),
            on_error=lambda b, e: skip(f"replay {b['replay_tag']}", e))

    battles_csv, plays_csv, n_plays = pipeline.write_csv(OUTDIR, got)
    table = Table("output", "rows", title=f"done ({skipped} skipped)")
    table.add_row(str(battles_csv), str(len(got)))
    table.add_row(str(plays_csv), str(n_plays))
    console.print(table)
    console.print(f"[dim]{lim.sent} requests · settled at {lim.rate:.1f}/s · "
                  f"peak {lim.peak:.1f}/s · {lim.hits} x 429[/]")
    return len(got)


# ---------------------------------------------------------------- app
def app(seed: str = SEED) -> int:
    console.print(Panel(
        f"[dim]deck[/] {seed}\n"
        f"[dim]base[/] {', '.join(sorted(parse.base_cards(seed)))}\n"
        "anonymous browser holds the Cloudflare pass · parallel curl does the work",
        title="RoyaleAPI replay scraper", border_style="cyan", expand=False))
    session = pick_session()

    with console.status("[cyan]starting anonymous browser, clearing Cloudflare..."):
        pages = Pages()
    try:
        curl = Curl(pages, session) if session else None
        if curl:
            with console.status("[cyan]checking login..."):
                ok = curl.logged_in()
            if not ok:
                # A jar can hold a cookie RoyaleAPI has since expired.
                console.print(f"[yellow]the {session.browser} session is not logged in[/]")
                curl = None
        if curl is None:
            curl = Curl(pages, browser_login(pages))
            with console.status("[cyan]checking login..."):
                if not curl.logged_in():
                    raise AuthError("signed in, but RoyaleAPI still refuses the session")
        console.print("[green]logged in[/]")

        workers = pick_settings(curl.limiter)
        players, found_on, order = roster(curl, seed, workers)
        chosen = pick_players(players, order)
        console.print(f"[green]{len(chosen)} player(s)[/] queued")
        max_pages = pick_depth()
        return crawl(curl, seed, {t: players[t] for t in chosen}, found_on, max_pages, workers)
    finally:
        pages.close()
