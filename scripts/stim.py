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
  trace     replay a token trace (scripts/traces.py builds them from real agent
            sessions or synthesises them). Each request is looked up, the
            prefix that hit is retrieved and the rest is stored, which is what
            an engine does with a prompt. Requests arrive when the trace says,
            so reuse, and the time between reuses, are the workload's own.

It takes every `lmcache bench server` option, so the KV shape, transfer mode and
RPC endpoint are spelled exactly as they are there. Runs inside the lmcache
image, which is where the bench client lives.
"""
import argparse
import collections
import json
import math
import os
import random
import sys
import time

import tracelib
from lmcache.cli.commands.bench.server_bench.arguments import add_server_arguments
from lmcache.cli.commands.bench.server_bench.cases.baseline import BaselineBenchCase
from lmcache.cli.commands.bench.server_bench.client import (
    LookupResult,
    ServerBenchClient,
)
from lmcache.cli.commands.bench.server_bench.config import parse_args_to_config
from lmcache.cli.commands.bench.server_bench.helpers import (
    _build_token_ids,
    _make_key,
    _poll_prefetch_status,
    _send_lookup,
)


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

    plan = sequences(args)
    started = time.monotonic()
    # Without this a long run is silent until it exits, since the per-request
    # logging the bench client does is suppressed: `make logs` on an hour-long
    # store would show nothing at all. Emitted on a timer rather than every N
    # requests so the cadence does not change with the request rate.
    next_progress = started + args.progress_every if args.progress_every else None

    for n, seq in enumerate(plan):
        if n:
            time.sleep(args.interval)
        if next_progress is not None and time.monotonic() >= next_progress:
            elapsed = time.monotonic() - started
            rate = t["chunks_hit"] / t["chunks_requested"] if t["chunks_requested"] else 0.0
            moved = (t["chunks_stored"] if args.op == "store" else t["chunks_retrieved"])
            log(f"{args.op} {n}/{len(plan)} requests, {moved} chunks "
                f"{'stored' if args.op == 'store' else 'retrieved'}, "
                f"hit {rate:.0%}, {n / elapsed:.1f} req/s"
                + (f", {t['errors']} errors" if t["errors"] else ""))
            next_progress = time.monotonic() + args.progress_every
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


def patient_lookup(client, request, timeout_s):
    """LOOKUP, waiting as long as a long prefix coming back from L2 can take.

    The bench's own lookup gives the prefetch 2.5 seconds and then reports no
    hit at all. That is plenty for the two chunks it was written for, and far
    too little for a 100k-token prompt being loaded from a slow tier, where it
    would turn every slow hit into a miss.
    """
    rpc = client._check_request_ready(request)
    key = _make_key(request.token_ids, request.request_id, start=0,
                    end=request.num_full_tokens, world_size=client._kv_world_size)
    started = time.monotonic()
    hit_tokens = None
    if _send_lookup(rpc, key, tp_size=len(client._workers)):
        hit_tokens = _poll_prefetch_status(
            rpc, key.request_id, max_polls=max(1, int(timeout_s / 0.02)), poll_interval=0.02)
    return LookupResult(
        hit_chunks=(hit_tokens or 0) // request.chunk_size,
        total_chunks=request.total_chunks,
        latency_ms=(time.monotonic() - started) * 1000,
        error=None if hit_tokens is not None else "timeout")


def run_trace(client, args, log):
    """Replay this client's share of a token trace; return tallies and latencies.

    The client also keeps its own record of every chunk it has stored, which is
    what lets a hit be attributed. A hit on a chunk this client stored came from
    this server (and from this session or another one); a hit on a chunk it
    never stored can only have come from a tier the servers share. A miss on a
    chunk it did store is one the cache has since lost.
    """
    trace = tracelib.Trace(args.trace)
    events = tracelib.schedule(trace, args.lanes, args.idle_cap, args.speedup,
                               duration=args.duration, max_sessions=args.max_sessions)
    chunk = client._chunk_size
    capacity = client._num_blocks * client._block_size // chunk * chunk
    t = collections.Counter()
    lat = {"lookup": [], "store": [], "retrieve": [], "lag": []}
    hit_gaps, lost_gaps = collections.Counter(), collections.Counter()
    seen = {}           # chunk key -> [last touched, session that stored it]
    state = {}          # session -> (tokens, chunk keys) of its previous request
    # Open for the whole replay; closed after the loop.
    out = open(args.log, "w") if args.log else None  # noqa: SIM115
    log(f"trace {args.trace}: {len(events)} requests scheduled over "
        f"{events[-1][0] if events else 0:.0f}s; this client is shard "
        f"{args.shard} of {args.shards}, route={args.route}")
    began = time.monotonic()

    for n, (due, s, i) in enumerate(events):
        sid, group, requests = trace.sessions[s]
        spec = requests[i]
        mine = tracelib.route(args.route, sid, group, i, args.shards) == args.shard
        # Every session's tokens are rebuilt from deltas, so a request that is
        # not ours still has to be applied -- but only cheaply, never hashed.
        previous_tokens, previous_keys = state.get(s, (None, ()))
        tokens = trace.tokens(spec, previous_tokens)
        last_of_session = i == len(requests) - 1
        if not mine:
            state[s] = (tokens, ())
            if last_of_session:
                del state[s]
            continue
        keys = tracelib.chunk_keys(tokens[:capacity], chunk, previous_keys, spec["keep"])
        state[s] = (tokens, keys)
        if last_of_session:
            del state[s]
        if len(tokens) > capacity:
            t["truncated"] += 1
        if not keys:
            t["skipped_short"] += 1
            continue

        wait = began + due - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        now = time.monotonic() - began
        lat["lag"].append(max(0.0, now - due) * 1000)

        request = client.create_request(
            n, request_id=f"trace-{args.shard}-{n}", request_kind="trace",
            token_ids=tokens[:len(keys) * chunk].tolist())
        row = {"t": round(now, 3), "due": round(due, 3), "sid": sid, "g": group, "i": i,
               "chunks": len(keys), "hit": 0}
        try:
            t["requests"] += 1
            lookup = patient_lookup(client, request, args.lookup_timeout)
            if not lookup.succeeded:
                t["errors"] += 1
                continue
            lat["lookup"].append(lookup.latency_ms)
            hit = lookup.hit_chunks
            if hit:
                done = client.retrieve(request, start_token=0, token_count=hit * chunk)
                if done is None or not done.succeeded:
                    t["errors"] += 1
                    continue
                lat["retrieve"].append(done.latency_ms)
            if hit < len(keys):
                done = client.store(request, start_token=hit * chunk,
                                    token_count=(len(keys) - hit) * chunk)
                if done is None or not done.succeeded:
                    t["errors"] += 1
                    continue
                lat["store"].append(done.latency_ms)

            tally = collections.Counter(chunks=len(keys), hit=hit)
            for j, key in enumerate(keys):
                known = seen.get(key)
                if j < hit:
                    if known is None:
                        tally["hit_remote"] += 1
                    else:
                        tally["hit_same_session" if known[1] == s
                              else "hit_other_session"] += 1
                        hit_gaps[tracelib.gap_bucket(now - known[0])] += 1
                elif known is None:
                    tally["new"] += 1
                else:
                    tally["lost"] += 1
                    lost_gaps[tracelib.gap_bucket(now - known[0])] += 1
                if known is None:
                    seen[key] = [now, s]
                else:
                    known[0] = now
            t.update(tally)
            t["full_hits"] += hit == len(keys)
            t["full_misses"] += hit == 0
            row.update(tally, lookup_ms=round(lookup.latency_ms, 2))
            if out:
                out.write(json.dumps(row, separators=(",", ":")) + "\n")
                out.flush()
        finally:
            client.release_request(request)
    if out:
        out.close()

    labels = tracelib.gap_labels()
    extra = {
        "trace": args.trace_name or os.path.basename(os.path.normpath(args.trace)),
        "route": args.route,
        "shard": [args.shard, args.shards], "speedup": args.speedup,
        # Replay seconds. Multiply by speedup for the trace's own clock.
        "hit_gap_s": {labels[b]: c for b, c in sorted(hit_gaps.items())},
        "lost_gap_s": {labels[b]: c for b, c in sorted(lost_gaps.items())},
    }
    tallies = {"requests": 0, "chunks": 0, "hit": 0, "hit_same_session": 0,
               "hit_other_session": 0, "hit_remote": 0, "lost": 0, "new": 0,
               "full_hits": 0, "full_misses": 0, "truncated": 0, "errors": 0, **t}
    return tallies, lat, extra


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--op", choices=("store", "restore", "verify", "trace"), default="store")
    tr = ap.add_argument_group("trace replay (--op trace)")
    tr.add_argument("--trace", help="token trace directory (see scripts/traces.py)")
    tr.add_argument("--trace-name", help="what to call the trace in the summary")
    tr.add_argument("--shard", type=int, default=0, help="this client's server, 0-based")
    tr.add_argument("--shards", type=int, default=1, help="servers the trace is spread over")
    tr.add_argument("--route", choices=tracelib.ROUTES, default="session",
                    help="what stays on one server: a session, a group, or nothing")
    tr.add_argument("--lanes", type=int, default=8, help="sessions in flight, fleet-wide")
    tr.add_argument("--idle-cap", type=float, default=60.0,
                    help="longest gap kept between a session's requests, trace seconds")
    tr.add_argument("--speedup", type=float, default=1.0, help="divide trace time by this")
    tr.add_argument("--duration", type=float, default=None,
                    help="stop scheduling after this many replay seconds")
    tr.add_argument("--max-sessions", type=int, default=None)
    tr.add_argument("--lookup-timeout", type=float, default=60.0,
                    help="seconds to wait for a lookup's prefix to be loaded from L2")
    tr.add_argument("--log", help="write one JSON line per request here")
    ap.add_argument("--order", choices=("sequential", "random"), default="sequential",
                    help="visit [--start, --end) in order, or sample it uniformly")
    ap.add_argument("--count", type=int, default=None,
                    help="requests to send (default: one per sequence in the window)")
    ap.add_argument("--seed", type=int, default=0, help="seed for --order random")
    ap.add_argument("--min-hit-rate", type=float, default=0.0,
                    help="restore and trace: exit non-zero if fewer than this fraction "
                         "of the chunks asked for were found")
    ap.add_argument("--progress-every", type=float, default=15,
                    help="seconds between progress lines; 0 to print only the "
                         "final summary")
    ap.add_argument("--client-index", type=int, default=0,
                    help="distinct per client talking to one server at the same "
                         "time: each index registers its own block of worker ids")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="print only the summary line")
    add_server_arguments(ap)
    args = ap.parse_args()
    if args.op == "trace":
        if not args.trace:
            ap.error("--op trace needs --trace")
        if not 0 <= args.shard < args.shards:
            ap.error("--shard must be in [0, --shards)")
    elif args.end is None or args.end <= args.start:
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
        if args.op == "trace":
            tallies, latencies, extra = run_trace(client, args, log)
            asked, found = tallies["chunks"], tallies["hit"]
        else:
            tallies, latencies = run(client, args, log)
            asked, found = tallies["chunks_requested"], tallies["chunks_hit"]
            extra = {"order": args.order, "window": [args.start, args.end]}
    finally:
        client.close()

    elapsed = time.monotonic() - started
    hit_rate = found / asked if asked else 0.0
    summary = {"op": args.op, **extra,
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
    if args.op in ("restore", "trace") and hit_rate < args.min_hit_rate:
        print(f"FAIL: hit rate {hit_rate:.2%} is below --min-hit-rate "
              f"{args.min_hit_rate:.2%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
