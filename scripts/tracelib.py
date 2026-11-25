"""Token traces: the on-disk format, the replay schedule, and chunk identity.

Shared by traces.py (which builds and analyses token traces) and stim.py (which
replays them against a server), so the two cannot disagree about when a request
is due, which server it goes to, or which chunks it is made of.

A token trace is a directory:

  meta.json    where it came from: source, tokenizer, counts
  index.jsonl  one line per request, grouped by session, in session order
  tokens.u32   little-endian uint32 token ids, only the NEW tokens of each request

A request's tokens are the first `keep` tokens of the previous request in its
session followed by its `new` tokens from tokens.u32. Agent sessions resend
their whole history every turn, so storing only what changed is what keeps a
trace of 100k-token contexts small.

Index fields: sid (session id), g (group: what sessions might share, e.g. a
benchmark or a repo), t (seconds since the session's first request), n (total
tokens), keep, off and new (see above), out (output tokens, informational).
"""
import hashlib
import heapq
import json
import os
import zlib

import numpy as np

GAP_BOUNDS = (1, 5, 15, 60, 300, 900, 3600)
ROUTES = ("session", "group", "request")


def gap_labels():
    return [f"le_{b}s" for b in GAP_BOUNDS] + ["gt_3600s"]


def gap_bucket(seconds):
    for i, bound in enumerate(GAP_BOUNDS):
        if seconds <= bound:
            return i
    return len(GAP_BOUNDS)


class TraceWriter:
    """Append requests to a token trace, storing each one as a delta."""

    def __init__(self, path, meta):
        os.makedirs(path, exist_ok=True)
        self.path, self.meta = path, dict(meta)
        # Held open across add() calls and closed by close().
        self._index = open(os.path.join(path, "index.jsonl"), "w")  # noqa: SIM115
        self._tokens = open(os.path.join(path, "tokens.u32"), "wb")  # noqa: SIM115
        self._offset = 0
        self._last = {}       # sid -> previous request's tokens
        self._sessions = set()
        self.requests = self.total_tokens = 0

    def add(self, sid, group, t, tokens, out=0):
        tokens = np.asarray(tokens, dtype="<u4")
        previous = self._last.get(sid)
        keep = 0
        if previous is not None:
            limit = min(len(previous), len(tokens))
            differ = np.nonzero(previous[:limit] != tokens[:limit])[0]
            keep = int(differ[0]) if len(differ) else limit
        new = tokens[keep:]
        self._tokens.write(new.tobytes())
        self._index.write(json.dumps(
            {"sid": sid, "g": group, "t": round(float(t), 3), "n": len(tokens),
             "keep": keep, "off": self._offset, "new": len(new), "out": int(out)},
            separators=(",", ":")) + "\n")
        self._offset += len(new)
        self._last[sid] = tokens
        self._sessions.add(sid)
        self.requests += 1
        self.total_tokens += len(tokens)

    def end_session(self, sid):
        self._last.pop(sid, None)

    def close(self):
        self._index.close()
        self._tokens.close()
        self.meta.update(sessions=len(self._sessions), requests=self.requests,
                         total_tokens=self.total_tokens, stored_tokens=self._offset)
        with open(os.path.join(self.path, "meta.json"), "w") as f:
            json.dump(self.meta, f, indent=2)
        return self.meta


class Trace:
    """A token trace opened for reading."""

    def __init__(self, path):
        self.path = path
        with open(os.path.join(path, "meta.json")) as f:
            self.meta = json.load(f)
        self.sessions = []            # [(sid, group, [request, ...])], in file order
        by_sid = {}
        with open(os.path.join(path, "index.jsonl")) as f:
            for line in f:
                r = json.loads(line)
                if r["sid"] not in by_sid:
                    by_sid[r["sid"]] = len(self.sessions)
                    self.sessions.append((r["sid"], r["g"], []))
                self.sessions[by_sid[r["sid"]]][2].append(r)
        self._tokens = np.memmap(os.path.join(path, "tokens.u32"), dtype="<u4", mode="r") \
            if os.path.getsize(os.path.join(path, "tokens.u32")) else np.zeros(0, "<u4")

    def tokens(self, request, previous):
        """A request's full token array, given the previous one in its session."""
        new = self._tokens[request["off"]:request["off"] + request["new"]]
        if not request["keep"]:
            return np.array(new, dtype="<u4")
        return np.concatenate((previous[:request["keep"]], new))


def schedule(trace, concurrency, idle_cap=60.0, speedup=1.0, stagger=0.5,
             duration=None, max_sessions=None):
    """When every request is due: [(due_seconds, session_index, request_index)].

    `concurrency` lanes each play one session at a time, back to back, taking
    sessions in file order. Inside a session the recorded gaps between request
    starts are kept, capped at `idle_cap` and divided by `speedup`. The
    schedule is open-loop -- it depends on the trace alone, never on how long
    the server took -- so every client replaying one trace computes the same
    one, and each can take its own share of it.
    """
    lanes = [(i * stagger / speedup, i) for i in range(concurrency)]
    heapq.heapify(lanes)
    events = []
    for s, (_sid, _group, requests) in enumerate(trace.sessions[:max_sessions]):
        start, lane = heapq.heappop(lanes)
        if duration is not None and start > duration:
            break
        due, last_t = start, requests[0]["t"]
        for i, request in enumerate(requests):
            due += min(max(request["t"] - last_t, 0.0), idle_cap) / speedup
            last_t = request["t"]
            if duration is None or due <= duration:
                events.append((due, s, i))
        heapq.heappush(lanes, (due + 1.0 / speedup, lane))
    events.sort()
    return events


def route(policy, sid, group, request_index, servers):
    """Which server (0-based) a request goes to.

    session  a session stays on one server: the best case for a local cache
    group    every session of a group shares a server: prefix-aware routing
    request  each request of a session moves to the next server: no affinity,
             so only a tier the servers share can turn history into hits
    """
    if policy == "group":
        return zlib.crc32(str(group).encode()) % servers
    base = zlib.crc32(str(sid).encode())
    return (base + (request_index if policy == "request" else 0)) % servers


def chunk_keys(tokens, chunk_size, previous_keys=(), keep=0):
    """Identity of each full chunk: a hash chained over everything before it.

    This mirrors what LMCache keys a chunk by -- its own tokens and every token
    in front of them -- without being LMCache's hash. It only has to agree with
    it about which chunks are the same, and two chunks are the same exactly when
    their token prefixes are. `previous_keys` and `keep` reuse the keys already
    computed for the part of the prefix a request shares with the last one.
    """
    reuse = min(keep // chunk_size, len(previous_keys))
    keys = list(previous_keys[:reuse])
    digest = keys[-1] if keys else b""
    data = np.ascontiguousarray(tokens, dtype="<u4")
    for start in range(reuse * chunk_size, len(data) - chunk_size + 1, chunk_size):
        digest = hashlib.blake2b(
            digest + data[start:start + chunk_size].tobytes(), digest_size=8).digest()
        keys.append(digest)
    return keys
