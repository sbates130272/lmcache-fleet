#!/usr/bin/env python3
"""Fleet stimulator: store KV blocks, or fetch them back, as separate jobs.

`lmcache bench server` is an end-to-end sanity test, and its one case is fixed:
store a sequence, then immediately look the same sequence up and retrieve it to
compare checksums. That is the wrong shape for modelling a cache. Every block
is read back within a fraction of a second of being written, while it is still
in L1, so L2 never serves a read and the write and read rates cannot be varied
apart.

This drives the same client the bench does, through the same wire protocol, but
one operation at a time:

  store     look each sequence up and store whatever is missing. Nothing is
            read back.
  restore   look each sequence up and retrieve whatever is there, the way an
            engine pulls a prefix back. Nothing is stored. Blocks still in L1
            come from L1; blocks that have spilled are loaded from L2 first.
  verify    the bench's own store-then-reread case, unchanged, for checking
            that bytes round-trip.

It takes every `lmcache bench server` option, so the KV shape, transfer mode and
RPC endpoint are spelled exactly as they are there. Runs inside the lmcache
image, which is where the bench client lives.
"""
import argparse
import json
import math
import random
import sys
import time

from lmcache.cli.commands.bench.server_bench.arguments import add_server_arguments
from lmcache.cli.commands.bench.server_bench.cases.baseline import BaselineBenchCase
from lmcache.cli.commands.bench.server_bench.client import ServerBenchClient
from lmcache.cli.commands.bench.server_bench.config import parse_args_to_config
from lmcache.cli.commands.bench.server_bench.helpers import _build_token_ids


def sequences(args):
    """The sequence ids to visit, in order."""
    window = range(args.start, args.end)
    if args.order == "sequential":
        ids = list(window)
        if args.count is not None:
            # Wrap, so a count past the window revisits it from the top.
            ids = [window[i % len(window)] for i in range(args.count)]
        return ids
    # Uniform with replacement: the same block can be asked for twice, which is
    # what makes some restores L1 hits and others L2 loads.
    rng = random.Random(args.seed)
    return [rng.choice(window) for _ in range(args.count or len(window))]


def percentile(samples, q):
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def run(client, args, log):
    """Visit each sequence once with the chosen operation; return the tallies."""
    t = {"requests": 0, "chunks_requested": 0, "chunks_hit": 0, "chunks_stored": 0,
         "chunks_retrieved": 0, "full_hits": 0, "full_misses": 0, "errors": 0}
    lat = {"lookup": [], "store": [], "retrieve": []}

    for n, seq in enumerate(sequences(args)):
        if n:
            time.sleep(args.interval)
        request = client.create_request(
            seq, request_id=f"{args.op}-{seq}-{n}", request_kind=args.op,
            token_ids=_build_token_ids(seq, args.num_tokens))
        if request is None:
            continue
        try:
            t["requests"] += 1
            lookup = client.lookup(request)
            if not lookup.succeeded:
                t["errors"] += 1
                continue
            lat["lookup"].append(lookup.latency_ms)
            t["chunks_requested"] += request.total_chunks
            t["chunks_hit"] += lookup.hit_chunks
            t["full_hits"] += lookup.is_full_hit
            t["full_misses"] += lookup.is_full_miss
            hit = lookup.hit_chunks * request.chunk_size
            miss = request.num_full_tokens - hit

            if args.op == "store" and miss:
                client.fill_deterministic_kv(request)
                done = client.store(request, start_token=hit, token_count=miss)
                if done is None or not done.succeeded:
                    t["errors"] += 1
                    continue
                lat["store"].append(done.latency_ms)
                t["chunks_stored"] += miss // request.chunk_size
            elif args.op == "restore" and hit:
                done = client.retrieve(request, start_token=0, token_count=hit)
                if done is None or not done.succeeded:
                    t["errors"] += 1
                    continue
                lat["retrieve"].append(done.latency_ms)
                t["chunks_retrieved"] += lookup.hit_chunks
        finally:
            client.release_request(request)
    return t, lat


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--op", choices=("store", "restore", "verify"), default="store")
    ap.add_argument("--order", choices=("sequential", "random"), default="sequential",
                    help="visit [--start, --end) in order, or sample it uniformly")
    ap.add_argument("--count", type=int, default=None,
                    help="requests to send (default: one per sequence in the window)")
    ap.add_argument("--seed", type=int, default=0, help="seed for --order random")
    ap.add_argument("--min-hit-rate", type=float, default=0.0,
                    help="restore only: exit non-zero if fewer than this fraction "
                         "of the chunks asked for were found")
    ap.add_argument("--client-index", type=int, default=0,
                    help="distinct per client talking to one server at the same "
                         "time: each index registers its own block of worker ids")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="print only the summary line")
    add_server_arguments(ap)
    args = ap.parse_args()
    if args.end is None or args.end <= args.start:
        ap.error("--end is required and must be greater than --start")

    def log(msg):
        if not args.quiet:
            print(msg, flush=True)

    config = parse_args_to_config(args)
    client = ServerBenchClient(config, lambda _msg: None, client_index=args.client_index)
    client.start()
    started = time.monotonic()
    try:
        if args.op == "verify":
            result = BaselineBenchCase(
                sequence_count=args.end - args.start, sequence_id_offset=args.start,
                interval_seconds=args.interval, num_tokens=config.num_tokens,
            ).run(client, lambda _msg: None)
            # Gate on the operations, not the checksums: under the dedup
            # adapter L2 returns a shared block, so a mismatch there is the
            # adapter working as documented.
            ok = result.completed_runs > 0 and all(
                passed for name, checks in result.checks.items()
                if name.endswith("_succeeded") for passed in checks)
            log(f"verify: {result.completed_runs} sequences, "
                f"{result.passed_count('checksum_match')} checksum matches, "
                f"{result.failed_count('checksum_match')} mismatches")
            return 0 if ok else 1
        tallies, latencies = run(client, args, log)
    finally:
        client.close()

    elapsed = time.monotonic() - started
    hit_rate = (tallies["chunks_hit"] / tallies["chunks_requested"]
                if tallies["chunks_requested"] else 0.0)
    summary = {"op": args.op, "order": args.order, "window": [args.start, args.end],
               **tallies, "hit_rate": round(hit_rate, 4), "elapsed_s": round(elapsed, 2)}
    for name, samples in latencies.items():
        if samples:
            summary[f"{name}_ms_p50"] = round(percentile(samples, 0.5), 2)
            summary[f"{name}_ms_p99"] = round(percentile(samples, 0.99), 2)
    # One JSON line: readable in `docker logs`, and parseable by anything else.
    print(json.dumps(summary), flush=True)

    if tallies["errors"]:
        print(f"FAIL: {tallies['errors']} request(s) errored", file=sys.stderr)
        return 1
    if args.op == "restore" and hit_rate < args.min_hit_rate:
        print(f"FAIL: hit rate {hit_rate:.2%} is below --min-hit-rate "
              f"{args.min_hit_rate:.2%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
