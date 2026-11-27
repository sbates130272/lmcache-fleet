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
n and out are for whoever reads the file; a Trace does not load them.
"""
import array
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


class _Requests:
    """One session's requests, each built from the index columns when asked for."""

    __slots__ = ("_trace", "_lo", "_hi")

    def __init__(self, trace, lo, hi):
        self._trace, self._lo, self._hi = trace, lo, hi

    def __len__(self):
        return self._hi - self._lo

    def __getitem__(self, i):
        if i < 0:
            i += self._hi - self._lo
        if not 0 <= i < self._hi - self._lo:
            raise IndexError(i)
        return self._trace._request(self._lo + i)

    def __iter__(self):
        return (self._trace._request(r) for r in range(self._lo, self._hi))


class _Sessions:
    """`[(sid, group, requests)]` in file order, without a tuple held per session."""

    __slots__ = ("_trace",)

    def __init__(self, trace):
        self._trace = trace

    def __len__(self):
        return len(self._trace._sid)

    def __getitem__(self, s):
        trace = self._trace
        if s < 0:
            s += len(trace._sid)
        if not 0 <= s < len(trace._sid):
            raise IndexError(s)
        return (trace._sid[s], trace._group[s],
                _Requests(trace, int(trace._starts[s]), int(trace._starts[s + 1])))

    def __iter__(self):
        return (self[s] for s in range(len(self)))


class Trace:
    """A token trace opened for reading.

    The index is held as one numpy column per field rather than a dict per
    request. Every client of a fleet loads the whole index, whatever its share
    of the requests, so a dict each -- about 760 bytes -- made memory the limit
    on servers times sessions long before CPU was. Columns are 24 bytes a
    request. Only what a replay reads is kept: t, keep, off and new.
    """

    def __init__(self, path):
        self.path = path
        with open(os.path.join(path, "meta.json")) as f:
            self.meta = json.load(f)
        # array.array rather than lists while reading: a list of boxed numbers
        # would bring the per-request cost straight back for the length of the
        # load, which is when every client of the fleet is doing it at once.
        t, off = array.array("d"), array.array("q")
        keep, new, session = array.array("q"), array.array("q"), array.array("q")
        self._sid, self._group = [], []
        by_sid, groups = {}, {}
        with open(os.path.join(path, "index.jsonl")) as f:
            for line in f:
                r = json.loads(line)
                s = by_sid.get(r["sid"])
                if s is None:
                    s = by_sid[r["sid"]] = len(self._sid)
                    self._sid.append(r["sid"])
                    # One string per group, not one per session that names it.
                    self._group.append(groups.setdefault(r["g"], r["g"]))
                session.append(s)
                t.append(r["t"])
                keep.append(r["keep"])
                off.append(r["off"])
                new.append(r["new"])
        session = np.frombuffer(session, dtype=np.int64) if session else np.zeros(0, np.int64)
        columns = [np.array(t, dtype=np.float64), np.array(keep, dtype=np.int64),
                   np.array(off, dtype=np.int64), np.array(new, dtype=np.int64)]
        if len(session) and (np.diff(session) < 0).any():
            # A session whose requests are not adjacent in the file: gather
            # them, keeping their order, as appending to a per-session list did.
            order = np.argsort(session, kind="stable")
            columns = [c[order] for c in columns]
        self._t, self._keep, self._off, self._new = columns
        self._starts = np.concatenate(
            ([0], np.cumsum(np.bincount(session, minlength=len(self._sid))))).astype(np.int64)
        self.sessions = _Sessions(self)
        self._tokens = np.memmap(os.path.join(path, "tokens.u32"), dtype="<u4", mode="r") \
            if os.path.getsize(os.path.join(path, "tokens.u32")) else np.zeros(0, "<u4")

    def _request(self, r):
        return {"t": float(self._t[r]), "keep": int(self._keep[r]),
                "off": int(self._off[r]), "new": int(self._new[r])}

    def tokens(self, request, previous):
        """A request's full token array, given the previous one in its session."""
        new = self._tokens[request["off"]:request["off"] + request["new"]]
        if not request["keep"]:
            return np.array(new, dtype="<u4")
        return np.concatenate((previous[:request["keep"]], new))


class Schedule:
    """`[(due_seconds, session_index, request_index)]` in replay order.

    Three arrays instead of a list of tuples, for the reason Trace holds
    columns: every client computes the whole schedule. It reads as the list it
    replaced -- len, indexing, iteration and equality all give tuples.
    """

    __slots__ = ("due", "session", "request")
    __hash__ = None

    def __init__(self, due, session, request):
        self.due, self.session, self.request = due, session, request

    def __len__(self):
        return len(self.due)

    def __getitem__(self, n):
        if isinstance(n, slice):
            return Schedule(self.due[n], self.session[n], self.request[n])
        return float(self.due[n]), int(self.session[n]), int(self.request[n])

    def __iter__(self):
        # In blocks: tolist() on the whole schedule would build the list of
        # boxed tuples this class exists to avoid.
        for lo in range(0, len(self.due), 65536):
            hi = lo + 65536
            yield from zip(self.due[lo:hi].tolist(), self.session[lo:hi].tolist(),
                           self.request[lo:hi].tolist(), strict=True)

    def __eq__(self, other):
        if isinstance(other, Schedule):
            return (np.array_equal(self.due, other.due)
                    and np.array_equal(self.session, other.session)
                    and np.array_equal(self.request, other.request))
        try:
            return list(self) == list(other)
        except TypeError:
            return NotImplemented


def schedule(trace, concurrency, idle_cap=60.0, speedup=1.0, stagger=0.5,
             duration=None, max_sessions=None):
    """When every request is due, as a Schedule of (due, session, request).

    `concurrency` lanes each play one session at a time, back to back, taking
    sessions in file order. Inside a session the recorded gaps between request
    starts are kept, capped at `idle_cap` and divided by `speedup`. The
    schedule is open-loop -- it depends on the trace alone, never on how long
    the server took -- so every client replaying one trace computes the same
    one, and each can take its own share of it.
    """
    lanes = [(i * stagger / speedup, i) for i in range(concurrency)]
    heapq.heapify(lanes)
    starts, t = trace._starts, trace._t
    sessions = len(range(len(trace.sessions))[:max_sessions])
    due = np.empty(int(starts[sessions]), dtype=np.float64)
    played = 0
    for s in range(sessions):
        lo, hi = int(starts[s]), int(starts[s + 1])
        start, lane = heapq.heappop(lanes)
        if duration is not None and start > duration:
            break
        # The session's start, then each capped gap, summed in order: cumsum
        # adds left to right, so a due time is the same float the running
        # `due += gap` of a plain loop would reach.
        steps = due[lo:hi]
        steps[0] = start
        np.subtract(t[lo + 1:hi], t[lo:hi - 1], out=steps[1:])
        np.clip(steps[1:], 0.0, idle_cap, out=steps[1:])
        steps[1:] /= speedup
        np.cumsum(steps, out=steps)
        heapq.heappush(lanes, (float(steps[-1]) + 1.0 / speedup, lane))
        played = s + 1
    end = int(starts[played])
    due = due[:end]
    counts = np.diff(starts[:played + 1])
    session = np.repeat(np.arange(played, dtype=np.int32), counts)
    request = (np.arange(end, dtype=np.int64) - np.repeat(starts[:played], counts)).astype(np.int32)
    if duration is not None:
        keep = due <= duration
        due, session, request = due[keep], session[keep], request[keep]
    # Sorted as tuples sort: by due time, then session, then request.
    order = np.lexsort((request, session, due))
    return Schedule(due[order], session[order], request[order])


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
