#!/usr/bin/env python3
"""Assertions on tracelib: the token trace format, the schedule, and routing.

Every trace-N of a fleet computes the schedule and the routing on its own and
keeps its share, so the fleet replays a trace exactly once only if those are
deterministic and the shares partition it. Nothing fails loudly when they do
not -- requests are just sent twice or never -- so it is asserted here.

    python3 scripts/check_tracelib.py     # needs numpy, nothing else
"""
import tempfile

import numpy as np
import tracelib


def build(path):
    """Three sessions that share a prefix, one of which rewrites its history."""
    rng = np.random.default_rng(0)
    shared = rng.integers(1000, 9000, size=700, dtype="<u4")
    writer = tracelib.TraceWriter(path, {"kind": "test"})
    expected = {}
    for s in range(3):
        sid, tokens, t = f"s{s}", shared, 0.0
        for turn in range(6):
            tokens = np.concatenate((tokens, rng.integers(1000, 9000, size=300, dtype="<u4")))
            if s == 1 and turn == 3:      # compaction: most of the prefix goes
                tokens = np.concatenate((shared[:256], tokens[-200:]))
            writer.add(sid, f"g{s % 2}", t, tokens, out=turn)
            expected[sid, turn] = tokens
            t += 10.0 if turn != 2 else 500.0
        writer.end_session(sid)
    return writer.close(), expected


with tempfile.TemporaryDirectory() as tmp:
    meta, expected = build(tmp)
    trace = tracelib.Trace(tmp)

    # Deltas rebuild every request exactly, and store less than they describe.
    assert meta["sessions"] == 3 and meta["requests"] == 18, meta
    assert meta["stored_tokens"] < meta["total_tokens"] / 3, meta
    for sid, _group, requests in trace.sessions:
        previous = None
        for turn, request in enumerate(requests):
            previous = trace.tokens(request, previous)
            assert np.array_equal(previous, expected[sid, turn]), (sid, turn)

    # Chunk keys: equal exactly as far as the token prefixes are, and the
    # incremental form agrees with hashing from scratch.
    a, b = expected["s0", 0], expected["s2", 0]
    ka, kb = tracelib.chunk_keys(a, 256), tracelib.chunk_keys(b, 256)
    assert len(ka) == len(a) // 256
    assert ka[:2] == kb[:2] and ka[2] != kb[2], "700 shared tokens are 2 full chunks"
    longer = expected["s0", 1]
    assert tracelib.chunk_keys(longer, 256, ka, len(a)) == tracelib.chunk_keys(longer, 256)
    assert tracelib.chunk_keys(longer, 256)[:len(ka)] == ka

    # The schedule is deterministic, ordered, complete, and honours the cap,
    # the speedup and the cut-offs.
    events = tracelib.schedule(trace, concurrency=2, idle_cap=60, speedup=1)
    assert events == tracelib.schedule(trace, concurrency=2, idle_cap=60, speedup=1)
    assert events == sorted(events) and len(events) == 18
    assert {(s, i) for _due, s, i in events} == {(s, i) for s in range(3) for i in range(6)}
    due = {(s, i): d for d, s, i in events}
    assert abs((due[0, 3] - due[0, 2]) - 60) < 1e-9, "a 500s gap is capped at 60"
    assert abs((due[0, 1] - due[0, 0]) - 10) < 1e-9
    assert due[2, 0] > due[0, 5], "a third session waits for a lane"
    fast = {(s, i): d for d, s, i in tracelib.schedule(trace, 2, 60, speedup=10)}
    assert abs((fast[0, 1] - fast[0, 0]) - 1) < 1e-9
    assert len(tracelib.schedule(trace, 2, max_sessions=1)) == 6
    assert all(d <= 30 for d, _s, _i in tracelib.schedule(trace, 2, duration=30))

    # Routing: every request has exactly one server, and each policy keeps
    # together what it says it does.
    for policy in tracelib.ROUTES:
        for servers in (1, 2, 5):
            for sid, group, requests in trace.sessions:
                picks = [tracelib.route(policy, sid, group, i, servers)
                         for i in range(len(requests))]
                assert all(0 <= p < servers for p in picks)
                if policy != "request":
                    assert len(set(picks)) == 1, (policy, picks)
                elif servers > 1:
                    assert picks[1] == (picks[0] + 1) % servers
    by_group = {}
    for sid, group, _ in trace.sessions:
        by_group.setdefault(group, set()).add(tracelib.route("group", sid, group, 0, 5))
    assert all(len(v) == 1 for v in by_group.values()), by_group

    assert [tracelib.gap_bucket(x) for x in (0.5, 1, 2, 3600, 3601)] == [0, 0, 1, 6, 7]
    assert len(tracelib.gap_labels()) == len(tracelib.GAP_BOUNDS) + 1

print("tracelib ok")
