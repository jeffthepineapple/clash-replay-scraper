#!/usr/bin/env python3
"""Drop win-traded and abandoned matches from a finished crawl.

Post-processing, not a collection filter: the crawl keeps everything and this
writes a cleaned copy alongside it, so thresholds can be retuned without
re-scraping. Two rules, both aimed at a player who was not really playing:

  leak      either side leaked >= 100 elixir. Leaking is continuous, so this
            only happens when someone stops spending for most of the match --
            a real game sits near 5 (p95 is 18).
  fast3     someone took three crowns within 90 seconds. A genuine 3-0 that
            fast does not happen against an opponent who is defending.
  empty     the replay carried no timeline at all. Old battles keep their row
            in the history long after RoyaleAPI drops the replay itself, so
            these arrive as a battle with zero card plays and no elixir table.

Usage:  ./filter_matches.py data/run1 [--leak 100] [--secs 90]
Writes battles.clean.csv / plays.clean.csv and battles.flagged.csv.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

TPS = 20


def num(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--leak", type=float, default=100.0, help="elixir leaked by either side")
    ap.add_argument("--secs", type=float, default=90.0, help="a 3-crown inside this is suspect")
    a = ap.parse_args()

    battles = list(csv.DictReader((a.outdir / "battles.csv").open()))
    plays = list(csv.DictReader((a.outdir / "plays.csv").open()))

    # Last card played is the best duration proxy the replay gives us.
    last: dict[str, int] = defaultdict(int)
    for p in plays:
        t = int(p["tick"] or 0)
        if t > last[p["replay_tag"]]:
            last[p["replay_tag"]] = t

    played: dict[str, int] = defaultdict(int)
    for p in plays:
        played[p["replay_tag"]] += 1

    flagged: dict[str, list[str]] = {}
    for b in battles:
        tag = b["replay_tag"]
        why = []
        if not played[tag]:
            why.append("empty")
        if max(num(b["team_elixir_leaked"]), num(b["oppo_elixir_leaked"])) >= a.leak:
            why.append("leak")
        crowns = max(num(b["team_crowns"]), num(b["opponent_crowns"]))
        secs = last[tag] / TPS
        if crowns >= 3 and secs <= a.secs:
            why.append("fast3")
        if why:
            flagged[tag] = why

    keep = [b for b in battles if b["replay_tag"] not in flagged]
    kept_tags = {b["replay_tag"] for b in keep}

    def dump(path: Path, rows: list[dict], fields: list[str]) -> None:
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    bf = list(battles[0].keys()) if battles else []
    pf = list(plays[0].keys()) if plays else []
    dump(a.outdir / "battles.clean.csv", keep, bf)
    dump(a.outdir / "plays.clean.csv", [p for p in plays if p["replay_tag"] in kept_tags], pf)
    dump(a.outdir / "battles.flagged.csv",
         [{**b, "flags": "+".join(flagged[b["replay_tag"]])}
          for b in battles if b["replay_tag"] in flagged], bf + ["flags"])

    reasons: dict[str, int] = defaultdict(int)
    for why in flagged.values():
        reasons["+".join(why)] += 1
    print(f"battles {len(battles)} -> kept {len(keep)}, dropped {len(flagged)} "
          f"({100 * len(flagged) / max(len(battles), 1):.1f}%)")
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {r:12} {n}")
    print(f"plays {len(plays)} -> {sum(1 for p in plays if p['replay_tag'] in kept_tags)}")


if __name__ == "__main__":
    main()
