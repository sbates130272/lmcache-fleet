#!/usr/bin/env python3
"""Assert that a running fleet actually stored something in both tiers.

report.sh prints the same directory for a human to read. This exits non-zero
instead, so CI can gate on it: every server registered, keys tracked, and at
least one L1 and one L2 placement on *each* server. The per-server split is the
point -- a fleet where one server took every store would still show healthy
totals.
"""
import argparse
import collections
import json
import sys
import time
import urllib.error
import urllib.request


def get(coord, path, timeout=5):
    with urllib.request.urlopen(coord + path, timeout=timeout) as r:
        return json.load(r)


def directory(coord):
    """(total keys, {(instance_id, tier): placement count})."""
    agg = collections.Counter()
    total = offset = 0
    while True:
        page = get(coord, f"/directory/keys?limit=1000&offset={offset}")
        total = page["total"]
        for key in page["keys"]:
            for p in key.get("placements", []):
                agg[(p.get("instance_id", "?"), p.get("tier", "?"))] += 1
        offset += len(page["keys"])
        if offset >= total or not page["keys"]:
            return total, agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("servers", type=int, nargs="?", default=2,
                    help="number of servers that must be registered")
    ap.add_argument("--coord", default="http://127.0.0.1:9300")
    ap.add_argument("--min-keys", type=int, default=1)
    # The stimulators exit as soon as their stores are acknowledged, but the
    # coordinator directory is built from server-side reports that land a beat
    # later. Without this the check races a fleet that is merely slow.
    ap.add_argument("--timeout", type=float, default=90,
                    help="seconds to wait for the directory to satisfy the checks")
    args = ap.parse_args()

    deadline = time.monotonic() + args.timeout
    failures = ["no attempt completed"]
    total = 0
    agg = collections.Counter()

    while True:
        try:
            insts = get(args.coord, "/instances").get("instances", [])
            total, agg = directory(args.coord)
            failures = []

            if len(insts) < args.servers:
                failures.append(
                    f"only {len(insts)} of {args.servers} instance(s) registered")
            if total < args.min_keys:
                failures.append(
                    f"{total} unique key(s) tracked, expected at least {args.min_keys}")
            for inst in sorted(i["instance_id"] for i in insts):
                for tier in ("l1", "l2"):
                    if not agg[(inst, tier)]:
                        failures.append(f"{inst}: no {tier} placements")
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            failures = [f"coordinator not answering: {exc}"]

        if not failures or time.monotonic() >= deadline:
            break
        time.sleep(3)

    print(f"unique keys tracked: {total}")
    for (inst, tier), n in sorted(agg.items()):
        print(f"  {inst:40s} {tier:3s} : {n}")

    if failures:
        print("\nFAIL", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"\nPASS: {args.servers} server(s), L1 and L2 placements on each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
