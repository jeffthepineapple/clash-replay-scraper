#!/usr/bin/env python3
"""Combine crawls from several miners into one deduplicated dataset.

Sharding keeps miners off each other's players, but a battle belongs to two
players and they can land in different shards, so overlap is possible however
the work is split. replay_tag is the match's own identity, so deduplicating on
it is what actually guarantees one row per match -- across miners, across runs,
and across the two perspectives of a single game.

Usage:  ./merge_runs.py merged/ data/golem data/friend-a data/friend-b
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path, help="directory to write the merged set into")
    ap.add_argument("dirs", type=Path, nargs="+", help="crawl directories to combine")
    ap.add_argument("--clean", action="store_true",
                    help="merge the *.clean.csv files instead of the raw ones")
    a = ap.parse_args()

    suffix = ".clean.csv" if a.clean else ".csv"
    battles: dict[str, dict] = {}
    plays: dict[str, list[dict]] = {}
    bfields: list[str] = []
    pfields: list[str] = []

    for d in a.dirs:
        b = read(d / f"battles{suffix}")
        p = read(d / f"plays{suffix}")
        if b and not bfields:
            bfields = list(b[0].keys())
        if p and not pfields:
            pfields = list(p[0].keys())
        by_tag: dict[str, list[dict]] = {}
        for row in p:
            by_tag.setdefault(row["replay_tag"], []).append(row)
        new = 0
        for row in b:
            tag = row["replay_tag"]
            if tag in battles:
                continue  # same match, already have it from another miner
            battles[tag] = row
            plays[tag] = by_tag.get(tag, [])
            new += 1
        print(f"  {d}: {len(b)} battles, {new} new ({len(b) - new} already seen)")

    if not battles:
        ap.error("no battles found in any of those directories")

    a.out.mkdir(parents=True, exist_ok=True)
    with (a.out / "battles.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, bfields, extrasaction="ignore")
        w.writeheader()
        w.writerows(battles.values())
    n = 0
    with (a.out / "plays.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, pfields, extrasaction="ignore")
        w.writeheader()
        for tag in battles:
            w.writerows(plays[tag])
            n += len(plays[tag])
    print(f"\nmerged -> {a.out}: {len(battles)} battles, {n} plays")


if __name__ == "__main__":
    main()
