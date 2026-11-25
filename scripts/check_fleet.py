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
import os
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


def _l2_sample(coord, insts, http_base):
    """Per server: (name, capacity, used, L2 chunks stored, L2 placements listed)."""
    _, agg = directory(coord)
    rows = []
    for inst in sorted(insts, key=lambda i: i.get("ip", "")):
        name, iid = inst.get("ip", ""), inst["instance_id"]
        # Servers are published on the host as HTTP_BASE + N - 1, and N is only
        # recoverable from the name the server advertised to the coordinator.
        digits = name.rsplit("-", 1)[-1]
        if not digits.isdigit():
            raise ValueError(f"cannot map {name or iid!r} to a host port")
        base = f"http://127.0.0.1:{http_base + int(digits) - 1}"
        adapters = (get(base, "/status").get("storage_manager", {})
                    .get("l2_eviction_controller", {}).get("adapters", []))
        capped = [a for a in adapters if a.get("total_capacity_bytes", 0) > 0]
        cap, used = ((capped[0]["total_capacity_bytes"], capped[0]["total_bytes_used"])
                     if capped else (0, 0))
        with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
            stored = sum(float(line.rsplit(" ", 1)[1]) for line in r.read().decode().splitlines()
                         if line.startswith("lmcache_mp_l2_store_completed_objects_chunks_total"))
        rows.append((name, cap, used, int(stored), agg[(iid, "l2")]))
    return rows


def l2_eviction_failures(coord, insts, http_base, timeout):
    """What is wrong with each server's L2 eviction; empty when it worked.

    A capacity that is configured but never enforced looks healthy from the
    directory alone: both tiers are populated either way. So this asks each
    server directly. It must have an eviction controller with a capacity, sit
    at or under that capacity, and have stored more L2 chunks than the
    directory still lists for it -- the difference is what was evicted.

    That difference is only meaningful on a fleet that has gone quiet. While
    stores are landing the directory trails the servers' own counters, and the
    lag alone reads as "a few chunks evicted" on a server nowhere near its
    watermark. So nothing is compared until two samples in a row agree.
    """
    deadline = time.monotonic() + timeout
    rows = _l2_sample(coord, insts, http_base)
    while True:
        time.sleep(3)
        again = _l2_sample(coord, insts, http_base)
        if again == rows:
            break
        rows = again
        if time.monotonic() >= deadline:
            return ["fleet still changing (stores or evictions in flight); "
                    "the L2 eviction check needs it idle"]

    failures = []
    for name, cap, used, stored, listed in rows:
        if not cap:
            failures.append(f"{name}: no L2 eviction controller with a capacity")
        elif used > cap:
            failures.append(f"{name}: L2 holds {used} bytes, over its {cap} capacity")
        elif stored <= listed:
            failures.append(f"{name}: stored {stored} L2 chunks and the directory "
                            f"still lists {listed}, so nothing was evicted")
        else:
            print(f"  {name}: {used / 2**20:.0f} of {cap / 2**20:.0f} MiB, "
                  f"{stored - listed} of {stored} L2 chunks evicted")
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("servers", type=int, nargs="?", default=2,
                    help="number of servers that must be registered")
    ap.add_argument("--coord", default="http://127.0.0.1:9300")
    ap.add_argument("--min-keys", type=int, default=1)
    ap.add_argument("--l2-eviction", action="store_true",
                    help="also require every server to have evicted from L2 "
                         "and to sit within its L2 capacity")
    ap.add_argument("--http-base", type=int, default=int(os.environ.get("HTTP_BASE", "8181")),
                    help="host port of server 1's HTTP API")
    # The stimulators exit as soon as their stores are acknowledged, but the
    # coordinator directory is built from server-side reports that land a beat
    # later. Without this the check races a fleet that is merely slow.
    ap.add_argument("--timeout", type=float, default=90,
                    help="seconds to wait for the directory to satisfy the checks")
    args = ap.parse_args()

    deadline = time.monotonic() + args.timeout
    failures = ["no attempt completed"]
    total = 0
    insts = []
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

    evicted = ""
    if args.l2_eviction:
        print("\nL2 eviction:")
        try:
            failures = l2_eviction_failures(args.coord, insts, args.http_base, args.timeout)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            failures = [f"server not answering: {exc}"]
        if failures:
            print("\nFAIL", file=sys.stderr)
            for f in failures:
                print(f"  {f}", file=sys.stderr)
            return 1
        evicted = ", L2 evicted and within capacity"
    print(f"\nPASS: {args.servers} server(s), L1 and L2 placements on each{evicted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
